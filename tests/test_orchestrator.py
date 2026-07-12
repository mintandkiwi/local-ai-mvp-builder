import contextlib
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "mvp_orchestrator.py"
SPEC = importlib.util.spec_from_file_location("mvp_orchestrator", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

FAST_PULL_PATH = Path(__file__).resolve().parents[1] / "src" / "fast_pull.py"
FAST_SPEC = importlib.util.spec_from_file_location("fast_pull", FAST_PULL_PATH)
FAST_PULL = importlib.util.module_from_spec(FAST_SPEC)
assert FAST_SPEC and FAST_SPEC.loader
sys.modules[FAST_SPEC.name] = FAST_PULL
FAST_SPEC.loader.exec_module(FAST_PULL)


class OrchestratorTests(unittest.TestCase):
    def test_fast_pull_model_parsing(self):
        self.assertEqual(
            FAST_PULL.parse_model("qwen3-coder:30b"),
            ("library/qwen3-coder", "30b"),
        )
        with self.assertRaises(ValueError):
            FAST_PULL.parse_model("../escape:latest")

    def test_fast_pull_quarantines_same_size_corrupt_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            blob = Path(directory) / "sha256-bad"
            blob.write_bytes(b"wrong")
            with contextlib.redirect_stdout(io.StringIO()):
                reused = FAST_PULL.reuse_or_quarantine_blob(
                    blob, len(b"wrong"), "0" * 64
                )
            self.assertFalse(reused)
            self.assertFalse(blob.exists())
            self.assertEqual(len(list(Path(directory).glob("*.corrupt-*"))), 1)

    def test_fast_pull_handles_completed_temp_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "valid.fast-pull"
            valid.write_bytes(b"valid")
            digest = hashlib.sha256(b"valid").hexdigest()
            self.assertTrue(
                FAST_PULL.completed_temp_is_valid(valid, len(b"valid"), digest)
            )
            corrupt = root / "corrupt.fast-pull"
            corrupt.write_bytes(b"wrong")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(
                    FAST_PULL.completed_temp_is_valid(
                        corrupt, len(b"wrong"), "0" * 64
                    )
                )
            self.assertFalse(corrupt.exists())
            self.assertEqual(len(list(root.glob("corrupt.fast-pull.corrupt-*"))), 1)

    def test_fast_pull_preserves_in_progress_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory) / "partial.fast-pull"
            temp.write_bytes(b"partial")
            Path(str(temp) + ".aria2").write_bytes(b"control")
            self.assertFalse(
                FAST_PULL.completed_temp_is_valid(temp, len(b"partial"), "0" * 64)
            )
            self.assertTrue(temp.exists())

    def test_model_alias_resolves(self):
        self.assertEqual(MODULE.model_id("primary"), "qwen3-coder:30b")

    def test_prompt_replacement(self):
        prompt = MODULE.render_prompt("coder.md", plan="PLAN-SENTINEL")
        self.assertIn("PLAN-SENTINEL", prompt)
        self.assertNotIn("{{PLAN}}", prompt)

    def test_reviewer_prompt_declares_uncommitted_scope(self):
        prompt = MODULE.render_prompt(
            "reviewer.md", plan="plan", validation="[]"
        )
        self.assertIn("staged、unstaged 和 untracked", prompt)

    def test_review_schema_is_valid_json(self):
        schema = json.loads(MODULE.REVIEW_SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(schema["type"], "object")
        self.assertIn("verdict", schema["required"])

    def test_blocking_finding_overrides_pass(self):
        review = {
            "verdict": "pass",
            "findings": [{"severity": "P2"}],
        }
        normalized = MODULE.normalize_review(
            review, [{"command": "test", "exit_code": 0}]
        )
        self.assertEqual(normalized["verdict"], "fail")

    def test_missing_validation_overrides_pass(self):
        review = {"verdict": "pass", "findings": []}
        normalized = MODULE.normalize_review(review, [])
        self.assertEqual(normalized["verdict"], "fail")
        self.assertEqual(normalized["findings"][0]["severity"], "P1")

    def test_validation_config(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / ".mvp-ai.toml").write_text(
                '[validation]\ncommands = ["python -m unittest"]\n',
                encoding="utf-8",
            )
            self.assertEqual(
                MODULE.load_validation_commands(project), ["python -m unittest"]
            )

    @mock.patch.object(MODULE, "run_command")
    def test_local_coder_places_global_approval_before_exec(self, run_command):
        settings = SimpleNamespace(
            codex_command="codex", coder_timeout_seconds=30
        )
        MODULE.call_local_coder(
            settings,
            Path("/tmp/project"),
            "model",
            "prompt",
            Path("/tmp"),
            "stage",
        )
        command = run_command.call_args.args[0]
        self.assertEqual(command[:4], ["codex", "-a", "never", "exec"])
        self.assertNotIn("-a", command[4:])
        self.assertIn("--ignore-user-config", command)

    @mock.patch.object(MODULE.subprocess, "run")
    def test_codex_process_bypasses_proxy_for_loopback(self, subprocess_run):
        subprocess_run.return_value = SimpleNamespace(returncode=0, stdout="ok")
        with tempfile.TemporaryDirectory() as directory:
            MODULE.run_command(
                ["codex"],
                cwd=Path(directory),
                timeout=1,
                stdin_text=None,
                log_path=Path(directory) / "command.log",
            )
        env = subprocess_run.call_args.kwargs["env"]
        self.assertEqual(env["NO_PROXY"], "localhost,127.0.0.1,::1")
        self.assertEqual(env["no_proxy"], "localhost,127.0.0.1,::1")

    @mock.patch.object(MODULE, "run_command")
    def test_reviewer_is_pinned_to_cloud_and_read_only(self, run_command):
        settings = SimpleNamespace(
            codex_command="codex",
            cloud_provider="openai",
            cloud_model="cloud-reviewer",
            cloud_reasoning_effort="high",
            review_timeout_seconds=30,
        )
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)

            def write_review(*args, **kwargs):
                (run_dir / "review-1.json").write_text(
                    '{"verdict":"pass","summary":"ok","findings":[],"tests":[]}',
                    encoding="utf-8",
                )

            run_command.side_effect = write_review
            MODULE.call_reviewer(
                settings, Path(directory), "prompt", run_dir, 1
            )
        command = run_command.call_args.args[0]
        self.assertIn('model_provider="openai"', command)
        self.assertIn("cloud-reviewer", command)
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertEqual(command[command.index("-s") + 1], "read-only")
        self.assertEqual(command[4], "-c")

    def test_validation_profile_denies_network_git_and_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            profile = MODULE.write_validation_profile(project, run_dir).read_text(
                encoding="utf-8"
            )
        self.assertIn("(deny network*)", profile)
        self.assertIn(str(project / ".git"), profile)
        self.assertIn("(deny file-read* (regex", profile)
        self.assertIn("service", profile)

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS sandbox-exec")
    def test_validation_sandbox_denies_nested_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            (project / "nested").mkdir(parents=True)
            run_dir.mkdir()
            (project / "nested" / ".env.test").write_text("SECRET=1")
            (project / "private.pem").write_text("not-a-real-key")
            (project / "service-account-dev.json").write_text("{}")
            (project / "nested" / "app-secrets.toml").write_text("token='x'")
            (project / "public.txt").write_text("public")
            external = root / "external" / ".aws"
            external.mkdir(parents=True)
            (external / "credentials").write_text("token=x")
            profile = MODULE.write_validation_profile(project, run_dir)
            command = (
                "test ! -r nested/.env.test && "
                "test ! -r private.pem && "
                "test ! -r service-account-dev.json && "
                "test ! -r nested/app-secrets.toml && "
                f"test ! -r {external / 'credentials'} && "
                "test -r public.txt"
            )
            result = subprocess.run(
                ["sandbox-exec", "-f", str(profile), "/bin/zsh", "-lc", command],
                cwd=project,
                env=MODULE.validation_environment(run_dir),
                check=False,
            )
            self.assertEqual(result.returncode, 0)
            original = (project / "nested" / ".env.test").read_text()
            for mutation in (
                "mv nested/.env.test nested/leaked.txt",
                "rm nested/.env.test",
                "printf changed > nested/.env.test",
            ):
                result = subprocess.run(
                    ["sandbox-exec", "-f", str(profile), "/bin/zsh", "-lc", mutation],
                    cwd=project,
                    env=MODULE.validation_environment(run_dir),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((project / "nested" / ".env.test").read_text(), original)
                self.assertFalse((project / "nested" / "leaked.txt").exists())

    def test_cli_does_not_allow_shortening_review_protocol(self):
        parser = MODULE.build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "run",
                        "--project",
                        "/tmp/project",
                        "--plan",
                        "/tmp/plan.md",
                        "--max-local-reviews",
                        "1",
                    ]
                )

    @mock.patch.object(MODULE, "write_summary")
    @mock.patch.object(MODULE, "call_supervisor")
    @mock.patch.object(MODULE, "call_reviewer")
    @mock.patch.object(MODULE, "run_validations")
    @mock.patch.object(MODULE, "call_local_coder")
    @mock.patch.object(MODULE, "ensure_model_available")
    @mock.patch.object(MODULE, "validate_project")
    def test_failed_reviews_follow_exact_takeover_sequence(
        self,
        validate_project,
        ensure_model_available,
        call_local_coder,
        run_validations,
        call_reviewer,
        call_supervisor,
        write_summary,
    ):
        fail = {
            "verdict": "fail",
            "summary": "bug",
            "findings": [{"severity": "P1"}],
            "tests": [],
        }
        passed = {
            "verdict": "pass",
            "summary": "fixed",
            "findings": [],
            "tests": [],
        }
        call_reviewer.side_effect = [
            json.loads(json.dumps(fail)),
            json.loads(json.dumps(fail)),
            passed,
        ]
        run_validations.return_value = [{"command": "test", "exit_code": 0}]
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            plan = project / "plan.md"
            plan.write_text("# plan", encoding="utf-8")
            (project / ".mvp-ai.toml").write_text(
                '[validation]\ncommands = ["test"]\n', encoding="utf-8"
            )
            args = SimpleNamespace(
                project=str(project),
                plan=str(plan),
                model="primary",
                allow_dirty=False,
            )
            with mock.patch.object(MODULE, "make_run_dir", return_value=project):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = MODULE.command_run(args)

        self.assertEqual(result, 0)
        self.assertEqual(call_local_coder.call_count, 2)
        self.assertEqual(call_reviewer.call_count, 3)
        self.assertEqual(call_supervisor.call_count, 1)
        self.assertEqual(
            [call.args[-1] for call in call_reviewer.call_args_list], [1, 2, 3]
        )
        write_summary.assert_called_once()
        self.assertEqual(
            write_summary.call_args.kwargs["status"], "ready_for_user_review"
        )


if __name__ == "__main__":
    unittest.main()
