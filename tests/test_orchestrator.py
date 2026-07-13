import contextlib
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
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
        prompt = MODULE.render_prompt(
            "coder.md", plan="PLAN-SENTINEL", today="2026-07-13"
        )
        self.assertIn("PLAN-SENTINEL", prompt)
        self.assertIn("2026-07-13", prompt)
        self.assertNotIn("{{PLAN}}", prompt)
        self.assertNotIn("{{TODAY}}", prompt)

    def test_live_redaction_masks_common_credentials(self):
        token_fixture = "ghp_" + ("x" * 30)
        text = MODULE.redact_live_text(
            "OPENAI_API_KEY=top-secret AWS_SECRET_ACCESS_KEY=aws-secret "
            "password: hunter2 "
            f"token={token_fixture}"
        )
        self.assertNotIn("top-secret", text)
        self.assertNotIn("aws-secret", text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("ghp_", text)
        self.assertIn("[REDACTED]", text)

    def test_live_command_event_does_not_print_command_output(self):
        event = {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "python -m unittest",
                "aggregated_output": "API_KEY=must-not-appear",
                "status": "completed",
                "exit_code": 0,
            },
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            MODULE.display_codex_event(json.dumps(event), "LOCAL")
        rendered = output.getvalue()
        self.assertIn("exit=0", rendered)
        self.assertNotIn("must-not-appear", rendered)

    def test_watchdog_ignores_unstructured_telemetry_noise(self):
        self.assertFalse(
            MODULE.is_progress_event("2026-07-13 WARN reconnecting", True)
        )
        self.assertTrue(
            MODULE.is_progress_event('{"type":"item.started"}', True)
        )

    def test_streaming_json_events_are_displayed_and_logged(self):
        event = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "safe progress"},
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "stream.log"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = MODULE.run_command(
                    [sys.executable, "-c", f"print({event!r})"],
                    cwd=Path(directory),
                    timeout=5,
                    stdin_text=None,
                    log_path=log_path,
                    live=True,
                    live_channel="LOCAL",
                    json_events=True,
                )
            self.assertEqual(result.returncode, 0)
            self.assertIn("模型说明：safe progress", output.getvalue())
            self.assertIn("safe progress", log_path.read_text(encoding="utf-8"))

    @unittest.skipIf(
        os.environ.get("MVP_VALIDATION_SANDBOX") == "1",
        "父级验证 Seatbelt 不允许向子进程发送 SIGKILL",
    )
    def test_monitor_kills_stalled_local_process(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "stalled.log"
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                MODULE.run_command(
                    [sys.executable, "-c", "import time; time.sleep(2)"],
                    cwd=Path(directory),
                    timeout=5,
                    stdin_text=None,
                    log_path=log_path,
                    monitor=True,
                    idle_timeout=0.1,
                )
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertIn("error: timeout", log_path.read_text(encoding="utf-8"))

    def test_run_directory_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runs").mkdir()
            with mock.patch.object(MODULE, "ROOT", root):
                run_dir = MODULE.make_run_dir(root / "project")
            mode = stat.S_IMODE(run_dir.stat().st_mode)
            self.assertEqual(mode, 0o700)

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

    def test_documentation_failure_has_specific_finding(self):
        review = {"verdict": "pass", "findings": []}
        normalized = MODULE.normalize_review(
            review,
            [
                {"command": "test", "exit_code": 0},
                {"command": "documentation-contract", "exit_code": 1},
            ],
        )
        finding = normalized["findings"][0]
        self.assertEqual(normalized["verdict"], "fail")
        self.assertEqual(finding["file"], "docs/")
        self.assertIn("中文开发文档", finding["title"])

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

    def test_validation_config_rejects_blank_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            for commands in ("[]", '["   "]', '["python -m unittest", ""]'):
                (project / ".mvp-ai.toml").write_text(
                    f"[validation]\ncommands = {commands}\n",
                    encoding="utf-8",
                )
                with self.assertRaises(MODULE.WorkflowError):
                    MODULE.load_validation_commands(project)

    def test_development_document_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            today = "2026-07-13"
            content_by_path = {
                Path(f"docs/devlog/{today}.md"): (
                    "# 开发日志\n## 今日目标\n完成目标。\n## 今日进展\n完成进展。\n"
                    "## 修改内容\n修改模块。\n## 使用方法\n运行命令。\n## 验证结果\n"
                    "测试通过。\n## 后续事项\n继续优化和验证。\n"
                ),
                Path("docs/ai/PROJECT_OUTLINE.md"): (
                    "# 项目开发大纲\n## 项目目标\n实现项目目标。\n## 技术栈\n使用技术栈。\n"
                    "## 架构与关键路径\n描述架构路径。\n## 重要文件\n列出重要文件。\n"
                    "## 约束\n遵守项目约束。\n## 当前状态\n当前功能已经完成。\n"
                ),
                Path("docs/ai/TASK_PLAN.md"): (
                    "# AI 任务规划\n## 当前里程碑\n完成当前里程碑。\n## 已完成\n"
                    "任务已经完成。\n## 进行中\n暂无进行任务。\n## 待办\n继续后续任务。\n"
                    "## 验收标准\n测试全部通过。\n## 下一步\n开始下一项任务。\n"
                ),
            }
            for relative, content in content_by_path.items():
                path = project / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            outcome = MODULE.validate_development_documents(
                project, run_dir, "stage", today
            )
            self.assertEqual(outcome["exit_code"], 0)

            (project / "docs/ai/TASK_PLAN.md").unlink()
            outcome = MODULE.validate_development_documents(
                project, run_dir, "missing", today
            )
            self.assertEqual(outcome["exit_code"], 1)
            self.assertIn(
                "缺少 docs/ai/TASK_PLAN.md",
                Path(outcome["log"]).read_text(encoding="utf-8"),
            )

            task_target = root / "external-task-plan.md"
            task_target.write_text(content_by_path[Path("docs/ai/TASK_PLAN.md")])
            (project / "docs/ai/TASK_PLAN.md").symlink_to(task_target)
            outcome = MODULE.validate_development_documents(
                project, run_dir, "symlink", today
            )
            self.assertEqual(outcome["exit_code"], 1)
            self.assertIn(
                "不能是符号链接",
                Path(outcome["log"]).read_text(encoding="utf-8"),
            )

    def test_version_snapshot_omits_remote_url_and_never_claims_push(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            subprocess.run(
                [
                    "git",
                    "remote",
                    "add",
                    "origin",
                    "https://embedded-token@example.invalid/private.git",
                ],
                cwd=project,
                check=True,
            )
            snapshot = MODULE.version_control_snapshot(project)
        serialized = json.dumps(snapshot)
        self.assertEqual(snapshot["remotes"], ["origin"])
        self.assertNotIn("embedded-token", serialized)
        self.assertFalse(snapshot["commit_created"])
        self.assertFalse(snapshot["pushed"])

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
        self.assertIn("--json", command)
        self.assertTrue(run_command.call_args.kwargs["monitor"])

    @mock.patch.object(MODULE, "run_command")
    def test_local_coder_enables_structured_events_in_live_mode(self, run_command):
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
            live=True,
        )
        command = run_command.call_args.args[0]
        self.assertIn("--json", command)
        self.assertTrue(run_command.call_args.kwargs["live"])
        self.assertTrue(run_command.call_args.kwargs["json_events"])

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

    @unittest.skipIf(
        os.environ.get("MVP_VALIDATION_SANDBOX") == "1",
        "父级验证 Seatbelt 中不能可靠嵌套 sandbox-exec",
    )
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
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if (
                result.returncode == 71
                and "sandbox_apply: Operation not permitted" in result.stderr
            ):
                self.skipTest("父级 Seatbelt 不允许嵌套 sandbox-exec")
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
                valid_docs = {
                    "command": "documentation-contract",
                    "exit_code": 0,
                    "elapsed_seconds": 0.0,
                    "log": str(project / "docs.log"),
                }
                with mock.patch.object(
                    MODULE,
                    "validate_development_documents",
                    return_value=valid_docs,
                ):
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

    @mock.patch.object(MODULE, "write_summary")
    @mock.patch.object(MODULE, "call_supervisor")
    @mock.patch.object(MODULE, "call_reviewer")
    @mock.patch.object(MODULE, "validate_development_documents")
    @mock.patch.object(MODULE, "run_validations")
    @mock.patch.object(MODULE, "call_local_coder")
    @mock.patch.object(MODULE, "ensure_model_available")
    @mock.patch.object(MODULE, "validate_project")
    def test_watchdog_hands_local_failure_to_supervisor(
        self,
        validate_project,
        ensure_model_available,
        call_local_coder,
        run_validations,
        validate_development_documents,
        call_reviewer,
        call_supervisor,
        write_summary,
    ):
        call_local_coder.side_effect = MODULE.WorkflowError("ollama stopped")
        run_validations.return_value = [{"command": "test", "exit_code": 0}]
        validate_development_documents.return_value = {
            "command": "documentation-contract",
            "exit_code": 0,
        }
        call_reviewer.return_value = {
            "verdict": "pass",
            "summary": "fixed",
            "findings": [],
            "tests": [],
        }
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
                live=False,
            )
            with mock.patch.object(MODULE, "make_run_dir", return_value=project):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = MODULE.command_run(args)
            event = json.loads((project / "watchdog.json").read_text())

        self.assertEqual(result, 0)
        self.assertEqual(event["action"], "codex_supervisor_takeover")
        call_supervisor.assert_called_once()
        self.assertEqual(call_reviewer.call_count, 1)
        self.assertEqual(
            write_summary.call_args.kwargs["status"], "ready_for_user_review"
        )


if __name__ == "__main__":
    unittest.main()
