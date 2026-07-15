import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
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
    @staticmethod
    def attach_validation_logs(path, *values):
        path = Path(path)
        if not path.exists():
            path.write_text("validation evidence\n", encoding="utf-8")
        for value in values:
            items = value if isinstance(value, list) else [value]
            for item in items:
                item["log"] = str(path)

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
        with mock.patch.object(Path, "read_text", return_value="{{MISSING}}"):
            with self.assertRaisesRegex(MODULE.WorkflowError, "未解析占位符"):
                MODULE.render_prompt("broken.md")

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
        command_text = "tool --api-key whimsical-value --password hunter-two"
        live = MODULE.redact_live_text(command_text)
        persisted = MODULE.redact_persistent_output(command_text)
        structured = MODULE.sanitize_json_value({"command": command_text})
        for rendered in (live, persisted, structured["command"]):
            self.assertNotIn("whimsical-value", rendered)
            self.assertNotIn("hunter-two", rendered)
        for command_text, secrets in (
            ("Authorization: Basic synthetic-credential", ("Basic", "synthetic-credential")),
            ("Proxy-Authorization: Bearer proxy-secret", ("Bearer", "proxy-secret")),
            ("password: multi word passphrase", ("multi word passphrase",)),
            ("tool --authorization auth-value", ("auth-value",)),
            ("tool --cookie cookie-value", ("cookie-value",)),
            ("tool --refresh-token refresh-value --id-token id-value", ("refresh-value", "id-value")),
        ):
            outputs = [
                MODULE.redact_live_text(command_text),
                MODULE.redact_persistent_output(command_text),
                MODULE.sanitize_json_value({"command": command_text})["command"],
            ]
            if command_text.startswith("tool "):
                outputs.append(" ".join(MODULE.sanitize_command(command_text.split())))
            for output in outputs:
                for secret in secrets:
                    self.assertNotIn(secret, output)
        derived = MODULE.sanitize_json_value(
            {
                "requestAuthorization": "Basic derived alpha beta",
                "sessionCookie": "cookie alpha beta",
                "securityToken": "security alpha beta",
                "bearerToken": "bearer alpha beta",
                "total_tokens": 42,
            }
        )
        self.assertEqual(derived["requestAuthorization"], "[REDACTED]")
        self.assertEqual(derived["sessionCookie"], "[REDACTED]")
        self.assertEqual(derived["securityToken"], "[REDACTED]")
        self.assertEqual(derived["bearerToken"], "[REDACTED]")
        self.assertEqual(derived["total_tokens"], 42)
        for assignment in (
            "securityToken=security alpha beta",
            "bearerToken=bearer alpha beta",
        ):
            self.assertNotIn("alpha beta", MODULE.redact_live_text(assignment))
        inline = MODULE.redact_persistent_output(
            "`Authorization: Basic alpha beta` 后续触发条件必须保留"
        )
        self.assertNotIn("Basic alpha beta", inline)
        self.assertIn("后续触发条件必须保留", inline)
        self.assertEqual(
            MODULE.redact_live_text(
                "tool --authorization alpha beta --verbose yes"
            ),
            "tool --authorization [REDACTED] --verbose yes",
        )

    def test_redaction_masks_private_remote_urls(self):
        for remote in (
            "https://git.example.invalid/private/repo.git",
            "ssh://git@git.example.invalid/private/repo.git",
            "git@git.example.invalid:private/repo.git",
        ):
            self.assertNotIn(remote, MODULE.redact_live_text(remote))
            self.assertNotIn(remote, MODULE.redact_sensitive_text(remote))

    def test_structured_redaction_masks_sensitive_json_keys(self):
        basic_auth = "dXNlcjpwYXNzd29yZA=="
        payload = MODULE.sanitize_json_value(
            {
                "metadata": {
                    "api_key": "must-not-persist",
                    "client_secret": "also-private",
                    "refreshToken": "camel-private",
                    "credentials": {"value": "nested-private"},
                    "auth": basic_auth,
                    "auths": {"registry.invalid": {"auth": basic_auth}},
                    "authConfig": {"username": "user", "auth": basic_auth},
                    "input_tokens": 123,
                    "soft_token_budget": 500000,
                }
            }
        )
        self.assertEqual(payload["metadata"]["api_key"], "[REDACTED]")
        self.assertEqual(payload["metadata"]["client_secret"], "[REDACTED]")
        self.assertEqual(payload["metadata"]["refreshToken"], "[REDACTED]")
        self.assertEqual(payload["metadata"]["credentials"], "[REDACTED]")
        self.assertEqual(payload["metadata"]["auth"], "[REDACTED]")
        self.assertEqual(payload["metadata"]["auths"], "[REDACTED]")
        self.assertEqual(payload["metadata"]["authConfig"], "[REDACTED]")
        self.assertEqual(payload["metadata"]["input_tokens"], 123)
        self.assertEqual(payload["metadata"]["soft_token_budget"], 500000)
        samples = (
            json.dumps({"auth": basic_auth}),
            json.dumps({"auths": {"registry.invalid": {"auth": basic_auth}}})
            + "\n"
            + json.dumps({"safe": "visible", "authConfig": basic_auth}),
            f"auth={basic_auth}",
            f"authConfig: {basic_auth}",
            f"tool --auth {basic_auth}",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                for rendered in (
                    MODULE.redact_live_text(sample),
                    MODULE.redact_persistent_output(sample),
                    MODULE.sanitize_json_value({"message": sample})["message"],
                ):
                    self.assertNotIn(basic_auth, rendered)

    def test_persistent_redaction_masks_quoted_multiline_and_modern_secrets(self):
        secrets = {
            "quoted-json-secret",
            "quoted-yaml-secret",
            "multiline-yaml-secret",
            "encrypted-private-material",
            "github_pat_" + ("x" * 30),
        }
        text = (
            '{\n  "api_key": "quoted-json-secret",\n'
            '  "safe": "visible"\n}\n'
            "password: 'quoted-yaml-secret'\n"
            "credentials: |\n  multiline-yaml-secret\n  second-line\n"
            "safe_value: still-visible\n"
            "-----BEGIN ENCRYPTED PRIVATE KEY-----\n"
            "encrypted-private-material\n"
            "-----END ENCRYPTED PRIVATE KEY-----\n"
            + "github_pat_"
            + ("x" * 30)
            + "\n"
        )
        redacted = MODULE.redact_persistent_output(text)
        for secret in secrets:
            self.assertNotIn(secret, redacted)
        self.assertIn("still-visible", redacted)
        self.assertIn("[REDACTED]", redacted)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(
                json.dumps({"metadata": {"api_key": "quoted-json-secret"}}),
                encoding="utf-8",
            )
            MODULE.redact_persisted_file(path)
            self.assertNotIn("quoted-json-secret", path.read_text(encoding="utf-8"))

    def test_truncated_and_mismatched_private_keys_never_persist(self):
        for text in (
            "prefix\n-----BEGIN PRIVATE KEY-----\ntruncated-key-material\n",
            "prefix\n-----BEGIN RSA PRIVATE KEY-----\nwrong-end-material\n"
            "-----END PRIVATE KEY-----\nafter\n",
        ):
            with self.subTest(text=text):
                self.assertNotIn("key-material", MODULE.redact_sensitive_text(text))
                self.assertNotIn("key-material", MODULE.redact_live_text(text))
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    command_log = root / "command.log"
                    with mock.patch.object(
                        MODULE.subprocess,
                        "run",
                        return_value=SimpleNamespace(returncode=0, stdout=text),
                    ):
                        MODULE.run_command(
                            ["tool"],
                            cwd=root,
                            timeout=5,
                            stdin_text=None,
                            log_path=command_log,
                        )
                    self.assertNotIn(
                        "key-material", command_log.read_text(encoding="utf-8")
                    )

                    with mock.patch.object(
                        MODULE.subprocess,
                        "run",
                        return_value=SimpleNamespace(returncode=1, stdout=text),
                    ):
                        validation = MODULE.run_validations(
                            root, ["test-command"], root, "pem-redaction"
                        )
                    self.assertNotIn(
                        "key-material",
                        Path(validation[0]["log"]).read_text(encoding="utf-8"),
                    )

                    for name in ("review-17.json", "coder-last-message.txt"):
                        artifact = root / name
                        artifact.write_text(text, encoding="utf-8")
                        MODULE.redact_persisted_file(artifact)
                        self.assertNotIn(
                            "key-material", artifact.read_text(encoding="utf-8")
                        )

    def test_persisted_evidence_redaction_failures_delete_original_and_block(self):
        for failure in ("read", "replace"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "last-message.txt"
                path.write_text("API_KEY=must-not-remain\n", encoding="utf-8")
                patcher = (
                    mock.patch.object(Path, "read_text", side_effect=PermissionError("denied"))
                    if failure == "read"
                    else mock.patch.object(
                        MODULE,
                        "atomic_write_text",
                        side_effect=OSError("replace denied"),
                    )
                )
                with patcher:
                    with self.assertRaises(MODULE.EvidenceRedactionError):
                        MODULE.redact_persisted_file(path)
            self.assertFalse(path.exists())

    def test_successful_child_exit_is_preserved_when_evidence_postprocessing_fails(self):
        settings = MODULE.load_settings()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            completed = subprocess.CompletedProcess([], 0, "", "")
            calls = (
                lambda: MODULE.call_local_coder(
                    settings, root, "model", "prompt", root, "coder"
                ),
                lambda: MODULE.call_reviewer(
                    settings, root, "prompt", root, 1
                ),
                lambda: MODULE.call_supervisor(
                    settings, root, "prompt", root, "supervisor"
                ),
            )
            for call in calls:
                with (
                    mock.patch.object(MODULE, "run_command", return_value=completed),
                    mock.patch.object(
                        MODULE, "redact_persisted_file",
                        side_effect=MODULE.EvidenceRedactionError("redaction failed"),
                    ),
                    self.assertRaises(MODULE.EvidenceRedactionError) as caught,
                ):
                    call()
                self.assertEqual(caught.exception.returncode, 0)
            timeout = subprocess.TimeoutExpired(["codex"], 1)
            for call in calls:
                with (
                    mock.patch.object(MODULE, "run_command", side_effect=timeout),
                    mock.patch.object(
                        MODULE, "redact_persisted_file",
                        side_effect=MODULE.EvidenceRedactionError("redaction failed"),
                    ),
                    self.assertRaises(MODULE.EvidenceRedactionError) as caught,
                ):
                    call()
                self.assertEqual(caught.exception.returncode, 124)

    def test_run_command_log_failure_preserves_successful_child_exit(self):
        completed = subprocess.CompletedProcess(["tool"], 0, "ok", "")
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(MODULE.subprocess, "run", return_value=completed),
                mock.patch.object(
                    MODULE, "atomic_write_text",
                    side_effect=MODULE.WorkflowError("log write failed"),
                ),
                self.assertRaises(MODULE.WorkflowError) as caught,
            ):
                MODULE.run_command(
                    ["tool"], cwd=Path(directory), timeout=1,
                    stdin_text=None, log_path=Path(directory) / "tool.log",
                )
        self.assertEqual(caught.exception.returncode, 0)

        timeout = subprocess.TimeoutExpired(["tool"], 1, output="partial")
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(MODULE.subprocess, "run", side_effect=timeout),
                mock.patch.object(
                    MODULE, "atomic_write_text",
                    side_effect=MODULE.WorkflowError("timeout log write failed"),
                ),
                self.assertRaises(MODULE.WorkflowError) as caught,
            ):
                MODULE.run_command(
                    ["tool"], cwd=Path(directory), timeout=1,
                    stdin_text=None, log_path=Path(directory) / "tool.log",
                )
        self.assertEqual(caught.exception.returncode, 124)

    def test_command_evidence_redacts_separate_secret_options_and_nested_text(self):
        command = [
            "tool",
            "--api-key",
            "separate-option-secret",
            "--message",
            'api_key: "nested-command-secret"',
        ]
        rendered = json.dumps(MODULE.sanitize_command(command))
        self.assertNotIn("separate-option-secret", rendered)
        self.assertNotIn("nested-command-secret", rendered)
        self.assertIn("--api-key", rendered)

    def test_embedded_pretty_structured_credentials_are_redacted_as_subtrees(self):
        text = (
            "tool output follows\n"
            "{\n"
            '  "credentials": {\n'
            '    "value": "SENSITIVE-NESTED-VALUE"\n'
            "  },\n"
            '  "refreshToken": "CAMEL-CASE-SECRET",\n'
            '  "safe": "visible"\n'
            "}\n"
        )
        redacted = MODULE.redact_persistent_output(text)
        self.assertNotIn("SENSITIVE-NESTED-VALUE", redacted)
        self.assertNotIn("CAMEL-CASE-SECRET", redacted)
        self.assertIn("visible", redacted)

    def test_malformed_sensitive_subtrees_drop_untrusted_remainder(self):
        for text in (
            '{"credentials": {\n"value": "LEAKED-NESTED"\n',
            '{"credentials": [\n"LEAKED-ARRAY"\n}\nafter',
        ):
            redacted = MODULE.redact_persistent_output(text)
            self.assertNotIn("LEAKED", redacted)
            self.assertIn("[REDACTED]", redacted)

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

    def test_live_top_level_failures_are_visible_redacted_progress(self):
        events = (
            {"type": "error", "message": "API_KEY=must-not-appear"},
            {
                "type": "turn.failed",
                "error": {"message": "password: must-not-appear"},
            },
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            for event in events:
                line = json.dumps(event)
                self.assertTrue(MODULE.is_progress_event(line, True))
                MODULE.display_codex_event(line, "LOCAL")
        rendered = output.getvalue()
        self.assertEqual(rendered.count("运行提示"), 2)
        self.assertNotIn("must-not-appear", rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_non_object_json_events_are_ignored(self):
        for value in (None, 42, [], True, "text"):
            line = json.dumps(value)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                MODULE.display_codex_event(line, "LOCAL")
            self.assertEqual(output.getvalue(), "")
            self.assertFalse(MODULE.is_progress_event(line, True))
        for usage in (None, [], "invalid", True):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                MODULE.display_codex_event(
                    json.dumps({"type": "turn.completed", "usage": usage}),
                    "LOCAL",
                )
            self.assertIn("输入 ? tokens", output.getvalue())

    def test_token_usage_prefers_exact_json_and_deduplicates_completion(self):
        first = json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
        )
        final = json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 12,
                    "cached_input_tokens": 4,
                    "output_tokens": 3,
                    "reasoning_output_tokens": 1,
                },
            }
        )
        usage = MODULE.parse_token_usage(first + "\n" + final + "\n" + final)
        self.assertEqual(usage["measurement"], "exact")
        self.assertEqual(usage["input_tokens"], 12)
        self.assertEqual(usage["total_tokens"], 15)
        for final_usage in (None, [], "invalid"):
            damaged = first + "\n" + json.dumps(
                {"type": "turn.completed", "usage": final_usage}
            )
            self.assertEqual(
                MODULE.parse_token_usage(damaged)["measurement"], "unavailable"
            )
        missing = first + "\n" + json.dumps({"type": "turn.completed"})
        self.assertEqual(
            MODULE.parse_token_usage(missing)["measurement"], "unavailable"
        )

    def test_boolean_usage_is_unavailable_not_numeric(self):
        for malformed in (
            None,
            [],
            "invalid",
            True,
            {"input_tokens": True, "output_tokens": 1},
        ):
            usage = MODULE.parse_token_usage(
                json.dumps({"type": "turn.completed", "usage": malformed})
            )
            self.assertEqual(usage["measurement"], "unavailable")
            self.assertIsNone(usage["total_tokens"])
        for field in ("cached_input_tokens", "reasoning_output_tokens"):
            for malformed in (True, -1, "bad", [], {}):
                values = {"input_tokens": 10, "output_tokens": 2, field: malformed}
                usage = MODULE.parse_token_usage(
                    json.dumps({"type": "turn.completed", "usage": values})
                )
                self.assertEqual(usage["measurement"], "unavailable")

    def test_persisted_json_usage_remains_exact_in_summary(self):
        event = json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 123,
                    "cached_input_tokens": 23,
                    "output_tokens": 7,
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            log = root / "agent.log"
            MODULE.run_command(
                [sys.executable, "-c", f"print({event!r})", "--json"],
                cwd=root,
                timeout=10,
                stdin_text=None,
                log_path=log,
            )
            persisted = log.read_text(encoding="utf-8")
            stage = MODULE.usage_stage(
                stage="coder-initial",
                backend="local",
                role="coder",
                output=persisted,
                elapsed_seconds=0.1,
                exit_code=0,
            )
            MODULE.write_summary(
                root,
                project=root,
                model="test",
                status="needs_manual_attention",
                review=None,
                validations=[],
                usage_stages=[stage],
            )
            summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
        self.assertIn('\"input_tokens\":123', persisted)
        self.assertEqual(stage["measurement"], "exact")
        self.assertEqual(summary["usage"]["stages"][0]["total_tokens"], 130)

    def test_token_usage_text_is_partial_and_large_numbers_are_preserved(self):
        usage = MODULE.parse_token_usage("tokens used: 9,223,372,036,854")
        self.assertEqual(usage["measurement"], "partial")
        self.assertEqual(usage["total_tokens"], 9223372036854)
        self.assertIsNone(usage["input_tokens"])

    def test_text_mode_ignores_embedded_json_usage_from_other_logs(self):
        output = (
            'command: ["codex", "exec"]\n'
            '{"type":"turn.completed","usage":'
            '{"input_tokens":641306,"output_tokens":4358}}\n'
            "tokens used\n95,030\n"
        )
        usage = MODULE.parse_token_usage(output)
        self.assertEqual(usage["measurement"], "partial")
        self.assertEqual(usage["total_tokens"], 95030)
        self.assertIsNone(usage["input_tokens"])

    def test_token_usage_missing_or_damaged_is_unavailable_not_zero(self):
        for output in (
            "",
            '{"type":"turn.completed","usage":',
            "tokens used: nope",
            "null\n42\n[]\ntrue",
        ):
            usage = MODULE.parse_token_usage(output)
            self.assertEqual(usage["measurement"], "unavailable")
            self.assertIsNone(usage["total_tokens"])
        for event_type in ("agent_message", "command_execution"):
            body = json.dumps(
                {"type": event_type, "text": "tokens used: 999999"}
            )
            usage = MODULE.parse_token_usage(body)
            self.assertEqual(usage["measurement"], "unavailable")

    def test_usage_summary_is_conservative_when_measurement_is_incomplete(self):
        stages = [
            MODULE.usage_stage(
                stage="review-1",
                backend="cloud-review",
                role="reviewer",
                output="tokens used 95,030",
                elapsed_seconds=1,
                exit_code=0,
            ),
            MODULE.usage_stage(
                stage="supervisor",
                backend="cloud-supervisor",
                role="supervisor",
                output="broken",
                elapsed_seconds=1,
                exit_code=1,
            ),
        ]
        summary = MODULE.summarize_usage(
            stages, soft_budget=100000, max_cloud_calls=3
        )
        self.assertEqual(summary["cloud_lower_bound"], 95030)
        self.assertFalse(summary["measurement_complete"])
        mixed = MODULE.summarize_usage(
            [
                {
                    "backend": "local", "measurement": "unavailable",
                    "total_tokens": None,
                },
                {
                    "backend": "cloud-review", "measurement": "exact",
                    "total_tokens": 100,
                },
            ],
            soft_budget=1000,
            max_cloud_calls=3,
        )
        self.assertFalse(mixed["measurement_complete"])
        self.assertEqual(mixed["budget"]["status"], "unknown")
        exceeded = MODULE.summarize_usage(
            mixed["stages"], soft_budget=50, max_cloud_calls=3
        )
        self.assertEqual(exceeded["budget"]["status"], "exceeded")
        cloud_mixed = MODULE.summarize_usage(
            [
                {
                    "backend": "cloud-review", "measurement": "exact",
                    "total_tokens": 100,
                },
                {
                    "backend": "cloud-supervisor", "measurement": "unavailable",
                    "total_tokens": None,
                },
            ],
            soft_budget=50,
            max_cloud_calls=3,
        )
        self.assertEqual(cloud_mixed["cloud_lower_bound"], 100)
        self.assertEqual(cloud_mixed["budget"]["status"], "exceeded")
        self.assertEqual(summary["budget"]["status"], "unknown")

    def test_risk_and_adaptive_review_decisions(self):
        self.assertEqual(
            MODULE.parse_risk_classification("Risk classification: low")["classification"],
            "low",
        )
        for indent in (" ", "  ", "   "):
            parsed = MODULE.parse_risk_classification(
                indent + "Risk classification: low"
            )
            self.assertEqual(parsed["classification"], "low")
            self.assertTrue(parsed["declared"])
        unknown = MODULE.parse_risk_classification("# no risk")
        self.assertEqual(unknown["classification"], "high")
        self.assertFalse(unknown["declared"])
        for ambiguous in (
            "Risk classification: low|medium|high",
            "Risk classification: low or high",
            "Risk classification: low\nRisk classification: low",
            "Risk classification: low\nRisk classification: high",
            "```text\nRisk classification: low\n```",
            "    Risk classification: low",
            "\tRisk classification: low",
            " \tRisk classification: low",
            "  \tRisk classification: low",
            "   \tRisk classification: low",
            "<!--\nRisk classification: low\n-->",
            "<!--\nRisk classification: low\n",
            "<script>\nRisk classification: low\n</script>",
            "<pre>\nRisk classification: low\n</pre>",
            "<style>\nRisk classification: low\n",
            "<textarea>\nRisk classification: low\n</textarea>",
            "<template>\nRisk classification: low\n</template>",
            "<div hidden>\nRisk classification: low\n</div>",
            "<section>\nRisk classification: low\n</section>",
            "<x-widget>\nRisk classification: low\n</x-widget>",
            "<![CDATA[\nRisk classification: low\n]]>",
            "<?control\nRisk classification: low\n?>",
            "<!DOCTYPE\nRisk classification: low\n>",
            "\fRisk classification: low",
            "\vRisk classification: low",
            "````text\nRisk classification: low\n```\n",
            "~~~text\nRisk classification: low\n",
            "Risk classification: low\nRisk classification: low|medium|high",
            "Risk classification: low\n风险分类：待选择",
        ):
            parsed = MODULE.parse_risk_classification(ambiguous)
            self.assertEqual(parsed["classification"], "high")
            self.assertFalse(parsed["declared"])
        visible_after_html = MODULE.parse_risk_classification(
            "<div hidden>\nhidden\n</div>\n\nRisk classification: low\n"
        )
        self.assertEqual(visible_after_html["classification"], "low")
        self.assertTrue(visible_after_html["declared"])
        p1 = {
            "verdict": "fail",
            "findings": [{"severity": "P1", "category": "reliability", "file": "a", "required_fix": "fix"}],
        }
        local_p2 = {
            "verdict": "fail",
            "findings": [{"severity": "P2", "category": "correctness", "file": "a", "required_fix": "fix"}],
        }
        self.assertEqual(MODULE.decide_review_action(p1), "supervisor_takeover")
        self.assertEqual(MODULE.decide_review_action(local_p2), "local_fix")
        p3_only = {
            "verdict": "fail",
            "findings": [
                {"severity": "P3", "category": "documentation", "file": "a", "required_fix": "polish"}
            ],
        }
        self.assertEqual(MODULE.decide_review_action(p3_only), "ready")
        compile_blocker = {
            "verdict": "fail",
            "findings": [
                {
                    "severity": "P2",
                    "category": "reliability",
                    "file": "build.py",
                    "required_fix": "fix compilation failure",
                }
            ],
        }
        self.assertEqual(
            MODULE.decide_review_action(compile_blocker),
            "supervisor_takeover",
        )
        for title in (
            "SQL injection",
            "跨站脚本 XSS",
            "路径穿越",
            "认证绕过",
            "数据破坏",
        ):
            security = {
                "verdict": "fail",
                "findings": [{
                    "severity": "P2", "category": "security", "title": title,
                    "file": "app.py", "required_fix": "修复安全边界",
                }],
            }
            self.assertEqual(
                MODULE.decide_review_action(security), "supervisor_takeover"
            )

    def test_context_capsule_is_bounded_valid_json_and_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            plan = run_dir / "plan.md"
            plan.write_text("Risk classification: low", encoding="utf-8")
            log = run_dir / "failure.log"
            log.write_text(
                "API_KEY=do-not-leak\n-----BEGIN PRIVATE KEY-----\nsecret\n"
                "-----END PRIVATE KEY-----\n" + "错误" * 5000,
                encoding="utf-8",
            )
            path, metadata = MODULE.write_context_capsule(
                project,
                run_dir,
                stage="large",
                plan_path=plan,
                risk={"classification": "low", "declared": True},
                validations=[{"command": "test", "exit_code": 1, "log": str(log)}],
                max_bytes=2048,
            )
            text = path.read_text(encoding="utf-8")
            json.loads(text)
            MODULE.verify_context_capsule(path)
            self.assertLessEqual(len(text.encode("utf-8")), 2048)
            self.assertNotIn("do-not-leak", text)
            self.assertNotIn("BEGIN PRIVATE KEY", text)
            self.assertTrue(metadata["truncated"])
            self.assertEqual(metadata["fields"], sorted(json.loads(text)))
            capsule_payload = json.loads(text)
            self.assertEqual(
                capsule_payload["review_evidence_role"],
                "prior_review_input_not_current_verdict",
            )
            if "scope_evidence_path" in capsule_payload:
                scope_path = Path(capsule_payload["scope_evidence_path"])
            else:
                scope_path = Path(capsule_payload["scope_evidence"]["path"])
                if not scope_path.is_absolute():
                    scope_path = (
                        Path(capsule_payload["evidence_base_dir"]) / scope_path
                    )
            scope_payload = json.loads(scope_path.read_text(encoding="utf-8"))
            self.assertIn("docs/ai/TASK_PLAN.md", scope_payload["allowed_files"])
            self.assertEqual(
                capsule_payload["base_commit"], scope_payload["base_commit"]
            )
            scope_path.write_text(
                "tampered", encoding="utf-8"
            )
            with self.assertRaisesRegex(MODULE.WorkflowError, "SHA-256"):
                MODULE.verify_context_capsule(path)

    def test_context_capsule_stays_bounded_with_escape_heavy_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            plan = run_dir / "plan.md"
            plan.write_text("Risk classification: low", encoding="utf-8")
            review = {
                "verdict": "fail",
                "summary": "API_KEY=do-not-leak",
                "findings": [
                    {
                        "severity": "P1",
                        "title": f"finding-{index}",
                        "file": "src/app.py",
                        "description": ('"\\\\' * 2000),
                        "required_fix": "修复" * 1000,
                    }
                    for index in range(30)
                ],
                "tests": [],
            }
            path, metadata = MODULE.write_context_capsule(
                project,
                run_dir,
                stage="escape-heavy",
                plan_path=plan,
                risk={"classification": "low", "declared": True},
                review=review,
                max_bytes=1024,
            )
            text = path.read_text(encoding="utf-8")
            payload = json.loads(text)
            MODULE.verify_context_capsule(path)
            evidence_value = payload.get("review_evidence_path") or payload[
                "review_evidence"
            ]["path"]
            evidence = Path(evidence_value)
            if not evidence.is_absolute():
                evidence = Path(payload["evidence_base_dir"]) / evidence
            self.assertLessEqual(len(text.encode("utf-8")), 1024)
            self.assertTrue(metadata["truncated"])
            self.assertTrue(evidence.is_file())
            evidence_payload = json.loads(evidence.read_text(encoding="utf-8"))
            self.assertEqual(len(evidence_payload["findings"]), 30)
            self.assertNotIn("do-not-leak", evidence.read_text(encoding="utf-8"))
            evidence.write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(MODULE.WorkflowError, "SHA-256"):
                MODULE.verify_context_capsule(path)

    def test_review_evidence_preserves_tail_after_sensitive_inline_example(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            plan = run_dir / "plan.md"
            plan.write_text("Risk classification: low", encoding="utf-8")
            review = {
                "verdict": "fail",
                "summary": "credential example",
                "findings": [
                    {
                        "severity": "P2",
                        "title": "example",
                        "file": "src/app.py",
                        "line": 1,
                        "description": (
                            "`Authorization: Basic alpha beta` 后续触发条件必须保留"
                        ),
                        "required_fix": "redact value only",
                    }
                ],
                "tests": [],
            }
            MODULE.write_context_capsule(
                project,
                run_dir,
                stage="review-tail",
                plan_path=plan,
                risk={"classification": "low", "declared": True},
                review=review,
            )
            evidence = json.loads(
                (run_dir / "context-review-tail-review.json").read_text(
                    encoding="utf-8"
                )
            )
        description = evidence["findings"][0]["description"]
        self.assertNotIn("Basic alpha beta", description)
        self.assertIn("后续触发条件必须保留", description)

    def test_context_capsule_final_fallback_keeps_validation_scope_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            for index in range(120):
                (project / f"very-long-changed-file-{index:03d}-name.txt").write_text(
                    "changed", encoding="utf-8"
                )
            plan = run_dir / "plan.md"
            plan.write_text("Risk classification: low", encoding="utf-8")
            log = run_dir / "validation.log"
            log.write_text("assertion failed", encoding="utf-8")
            path, _ = MODULE.write_context_capsule(
                project,
                run_dir,
                stage="validation-fix-large",
                plan_path=plan,
                risk={"classification": "low", "declared": True, "reason": "plan"},
                validations=[
                    {
                        "command": "python -m unittest",
                        "exit_code": 1,
                        "classification": "code_or_test",
                        "log": str(log),
                    }
                ],
                max_bytes=1024,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            MODULE.verify_context_capsule(path)
            scope_path = Path(payload["scope_evidence"]["path"])
            if not scope_path.is_absolute():
                scope_path = Path(payload["evidence_base_dir"]) / scope_path
            scope = json.loads(scope_path.read_text(encoding="utf-8"))
        payload_plan = Path(payload["plan"]["path"])
        if not payload_plan.is_absolute():
            payload_plan = Path(payload["evidence_base_dir"]) / payload_plan
        self.assertEqual(payload_plan.resolve(), plan.resolve())
        self.assertEqual(payload["risk"]["classification"], "low")
        self.assertEqual(payload["validation_evidence"]["count"], 1)
        self.assertEqual(payload["base_commit"], scope["base_commit"])
        self.assertEqual(scope["validation_failures"][0]["exit_code"], 1)
        self.assertGreater(len(scope["allowed_files"]), 100)

    def test_context_capsule_rejects_scope_base_commit_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            plan = run_dir / "plan.md"
            plan.write_text("Risk classification: low", encoding="utf-8")
            capsule, _ = MODULE.write_context_capsule(
                project, run_dir, stage="commit-mismatch", plan_path=plan,
                risk={"classification": "low", "declared": True},
            )
            payload = json.loads(capsule.read_text(encoding="utf-8"))
            scope = Path(payload["scope_evidence_path"])
            scope_payload = json.loads(scope.read_text(encoding="utf-8"))
            scope_payload["base_commit"] = "different"
            scope_text = json.dumps(scope_payload, ensure_ascii=False, indent=2)
            MODULE.atomic_write_text(scope, scope_text)
            payload["scope_evidence_sha256"] = hashlib.sha256(
                scope_text.encode("utf-8")
            ).hexdigest()
            MODULE.atomic_write_text(
                capsule, json.dumps(payload, ensure_ascii=False, indent=2)
            )
            with self.assertRaisesRegex(MODULE.WorkflowError, "base commit"):
                MODULE.verify_context_capsule(capsule)

    def test_porcelain_z_parser_preserves_all_status_paths(self):
        output = (
            " M README.md\0"
            "M  staged file.txt\0"
            "?? untracked name.txt\0"
            "R  renamed target.txt\0renamed source.txt\0"
            "C  copied target.txt\0copied source.txt\0"
        )
        self.assertEqual(
            MODULE.parse_porcelain_v1_z(output),
            [
                "README.md",
                "staged file.txt",
                "untracked name.txt",
                "renamed target.txt",
                "renamed source.txt",
                "copied target.txt",
                "copied source.txt",
            ],
        )

    def test_validation_evidence_hash_detects_log_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            plan = run_dir / "plan.md"
            plan.write_text("Risk classification: low", encoding="utf-8")
            log = run_dir / "failed.log"
            log.write_text('api_key: "secret"\nassertion failed\n', encoding="utf-8")
            passed_log = run_dir / "passed.log"
            passed_log.write_text("all checks passed\n", encoding="utf-8")
            capsule, _ = MODULE.write_context_capsule(
                project,
                run_dir,
                stage="hashed-validation",
                plan_path=plan,
                risk={"classification": "low", "declared": True},
                validations=[
                    {"command": "test", "exit_code": 1, "log": str(log)},
                    {
                        "command": "lint",
                        "exit_code": 0,
                        "log": str(passed_log),
                    },
                ],
            )
            payload = json.loads(capsule.read_text(encoding="utf-8"))
            manifest = json.loads(
                Path(payload["validation_evidence_path"]).read_text(encoding="utf-8")
            )
            artifact = manifest["artifacts"][0]
            self.assertEqual(len(manifest["artifacts"]), 2)
            self.assertNotIn("secret", log.read_text(encoding="utf-8"))
            self.assertTrue(
                MODULE.file_matches_sha256(
                    Path(artifact["log_path"]), artifact["log_sha256"]
                )
            )
            log.write_text("modified after capsule", encoding="utf-8")
            self.assertFalse(
                MODULE.file_matches_sha256(
                    Path(artifact["log_path"]), artifact["log_sha256"]
                )
            )
            with self.assertRaisesRegex(MODULE.WorkflowError, "SHA-256"):
                MODULE.verify_context_capsule(capsule)

    def test_validation_evidence_requires_log_path_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            plan = run_dir / "plan.md"
            plan.write_text("Risk classification: low", encoding="utf-8")
            with self.assertRaisesRegex(MODULE.WorkflowError, "缺少完整日志"):
                MODULE.write_context_capsule(
                    project,
                    run_dir,
                    stage="missing-log",
                    plan_path=plan,
                    risk={"classification": "low", "declared": True},
                    validations=[{"command": "test", "exit_code": 0, "log": None}],
                )

            log = run_dir / "test.log"
            log.write_text("ok", encoding="utf-8")
            capsule, _ = MODULE.write_context_capsule(
                project,
                run_dir,
                stage="missing-hash",
                plan_path=plan,
                risk={"classification": "low", "declared": True},
                validations=[{"command": "test", "exit_code": 0, "log": str(log)}],
            )
            payload = json.loads(capsule.read_text(encoding="utf-8"))
            manifest_path = Path(payload["validation_evidence_path"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"][0]["log_sha256"] = None
            manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2)
            MODULE.atomic_write_text(manifest_path, manifest_text)
            payload["validation_evidence_sha256"] = hashlib.sha256(
                manifest_text.encode("utf-8")
            ).hexdigest()
            MODULE.atomic_write_text(
                capsule, json.dumps(payload, ensure_ascii=False, indent=2)
            )
            with self.assertRaisesRegex(MODULE.WorkflowError, "路径或 SHA-256"):
                MODULE.verify_context_capsule(capsule)

    def test_environment_classifier_requires_specific_toolchain_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vague = root / "vague.log"
            vague.write_text("application SDK logic assertion failed", encoding="utf-8")
            outcomes = [{"exit_code": 1, "log": str(vague)}]
            self.assertEqual(
                MODULE.classify_validation_failure(outcomes), "baseline_validation"
            )
            missing = root / "missing.log"
            missing.write_text("xcrun: error: SDK not found", encoding="utf-8")
            outcomes = [{"exit_code": 1, "log": str(missing)}]
            self.assertEqual(
                MODULE.classify_validation_failure(outcomes),
                "environment_preflight",
            )
            mixed = [
                {"command": "tool", "exit_code": 127, "log": str(missing)},
                {"command": "test", "exit_code": 1, "log": str(vague)},
            ]
            self.assertEqual(
                MODULE.classify_validation_failure(
                    mixed, phase="validation-fix"
                ),
                "environment_preflight",
            )

    def test_validation_classifier_is_phase_aware_and_rejects_ambiguous_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = root / "application.log"
            application.write_text(
                "application assertion: permission denied", encoding="utf-8"
            )
            for phase, expected in (
                ("coder-initial", "coder_validation"),
                ("validation-fix", "local_fix_validation"),
                ("supervisor", "supervisor_validation"),
            ):
                outcomes = [
                    {"command": "test", "exit_code": 1, "log": str(application)}
                ]
                self.assertEqual(
                    MODULE.classify_validation_failure(outcomes, phase=phase),
                    expected,
                )
                self.assertEqual(outcomes[0]["classification"], "code_or_test")
            docs = [
                {"command": "documentation-contract", "exit_code": 1, "log": None}
            ]
            self.assertEqual(
                MODULE.classify_validation_failure(docs, phase="supervisor"),
                "documentation_failure",
            )
            self.assertEqual(docs[0]["classification"], "documentation")

    def test_environment_classifier_recognizes_probe_and_compiler_temp_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiler = root / "compiler.log"
            compiler.write_text(
                "clang: error: unable to make temporary file: Operation not permitted",
                encoding="utf-8",
            )
            for phase in ("baseline", "coder-initial", "validation-fix", "supervisor"):
                outcomes = [{"command": "build", "exit_code": 1, "log": str(compiler)}]
                self.assertEqual(
                    MODULE.classify_validation_failure(outcomes, phase=phase),
                    "environment_preflight",
                )
                self.assertEqual(outcomes[0]["classification"], "environment")
            probe = [
                {
                    "command": "environment-write-probe",
                    "exit_code": 1,
                    "log": None,
                }
            ]
            self.assertEqual(
                MODULE.classify_validation_failure(probe),
                "environment_preflight",
            )

    @mock.patch.object(MODULE.subprocess, "run")
    def test_preflight_validation_runs_cache_and_temp_probe_first(self, subprocess_run):
        subprocess_run.side_effect = [
            SimpleNamespace(returncode=1, stdout="cache write denied"),
            AssertionError("configured validation must not run after probe failure"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            outcomes = MODULE.run_validations(
                project,
                ["python -m unittest"],
                run_dir,
                "preflight",
                project_writable=False,
            )
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["command"], "environment-write-probe")
        self.assertEqual(outcomes[0]["exit_code"], 1)
        self.assertEqual(subprocess_run.call_count, 1)

    def test_context_capsule_diff_stat_includes_staged_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            source = project / "app.py"
            source.write_text("before\n", encoding="utf-8")
            readme = project / "README.md"
            readme.write_text("before\n", encoding="utf-8")
            old_name = project / "old name.txt"
            old_name.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=project, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "base",
                ],
                cwd=project,
                check=True,
            )
            source.write_text("after\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=project, check=True)
            readme.write_text("unstaged\n", encoding="utf-8")
            (project / "untracked file.txt").write_text("new\n", encoding="utf-8")
            subprocess.run(
                ["git", "mv", "old name.txt", "renamed file.txt"],
                cwd=project,
                check=True,
            )
            plan = run_dir / "plan.md"
            plan.write_text("Risk classification: low", encoding="utf-8")
            path, _ = MODULE.write_context_capsule(
                project,
                run_dir,
                stage="staged",
                plan_path=plan,
                risk={"classification": "low", "declared": True},
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("app.py", payload["diff_stat"])
        for expected in (
            "app.py",
            "README.md",
            "untracked file.txt",
            "renamed file.txt",
            "old name.txt",
        ):
            self.assertIn(expected, payload["changed_files"])
            self.assertIn(expected, payload["allowed_files"])

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

    def test_command_error_preserves_arbitrary_nonzero_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "failed.log"
            with self.assertRaises(MODULE.CommandError) as raised:
                MODULE.run_command(
                    [sys.executable, "-c", "raise SystemExit(7)"],
                    cwd=Path(directory),
                    timeout=10,
                    stdin_text=None,
                    log_path=log_path,
                )
        self.assertEqual(raised.exception.returncode, 7)
        self.assertEqual(raised.exception.log_path, log_path)

    @unittest.skipIf(
        os.environ.get("MVP_VALIDATION_SANDBOX") == "1",
        "父级验证 Seatbelt 不允许向超时子进程发送 SIGKILL",
    )
    def test_real_fake_codex_wrapper_exit_attribution_matrix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            fake = root / "fake-codex"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys, time\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "out = Path(args[args.index('-o') + 1])\n"
                "if '--oss' in args:\n"
                "    mode = os.environ.get('FAKE_CODER_MODE', 'success')\n"
                "    if mode == 'timeout': time.sleep(5)\n"
                "    if mode == 'fail7': raise SystemExit(7)\n"
                "    out.write_text('done', encoding='utf-8')\n"
                "    print(json.dumps({'type':'turn.completed','usage':"
                "{'input_tokens':10,'output_tokens':2}}))\n"
                "elif '--output-schema' in args:\n"
                "    mode = os.environ.get('FAKE_REVIEWER_MODE', 'success')\n"
                "    if mode == 'quota9': raise SystemExit(9)\n"
                "    if mode == 'malformed':\n"
                "        out.write_text('{', encoding='utf-8')\n"
                "    else:\n"
                "        out.write_text(json.dumps({'verdict':'pass','summary':'ok',"
                "'findings':[],'tests':[]}), encoding='utf-8')\n"
                "        print(json.dumps({'type':'turn.completed','usage':"
                "{'input_tokens':20,'output_tokens':3}}))\n"
                "else:\n"
                "    out.write_text('supervised', encoding='utf-8')\n"
                "    print(json.dumps({'type':'turn.completed','usage':"
                "{'input_tokens':30,'output_tokens':4}}))\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            settings = replace(
                MODULE.load_settings(),
                codex_command=str(fake),
                coder_timeout_seconds=3,
                local_stall_timeout_seconds=5,
                review_timeout_seconds=3,
            )
            stages = []

            with mock.patch.dict(os.environ, {"FAKE_CODER_MODE": "success"}):
                result = MODULE.call_local_coder(
                    settings, root, "model", "prompt", root, "coder-success"
                )
            stages.append(
                MODULE.usage_stage(
                    stage="coder-success",
                    backend="local",
                    role="coder",
                    output=(root / "coder-success.log").read_text(encoding="utf-8"),
                    elapsed_seconds=0.1,
                    exit_code=result.returncode,
                )
            )

            with mock.patch.dict(os.environ, {"FAKE_CODER_MODE": "fail7"}):
                with self.assertRaises(MODULE.CommandError) as failed:
                    MODULE.call_local_coder(
                        settings, root, "model", "prompt", root, "coder-fail"
                    )
            stages.append(
                MODULE.usage_stage(
                    stage="coder-fail",
                    backend="local",
                    role="coder",
                    output=(root / "coder-fail.log").read_text(encoding="utf-8"),
                    elapsed_seconds=0.1,
                    exit_code=failed.exception.returncode,
                )
            )

            with mock.patch.dict(os.environ, {"FAKE_CODER_MODE": "timeout"}):
                with self.assertRaises(subprocess.TimeoutExpired):
                    MODULE.call_local_coder(
                        settings, root, "model", "prompt", root, "coder-timeout"
                    )
            stages.append(
                MODULE.usage_stage(
                    stage="coder-timeout",
                    backend="local",
                    role="coder",
                    output=(root / "coder-timeout.log").read_text(encoding="utf-8"),
                    elapsed_seconds=1.0,
                    exit_code=124,
                )
            )

            with mock.patch.dict(os.environ, {"FAKE_REVIEWER_MODE": "malformed"}):
                with self.assertRaises(MODULE.StructuredResultError) as malformed:
                    MODULE.call_reviewer(settings, root, "prompt", root, 1)
            stages.append(
                MODULE.usage_stage(
                    stage="review-malformed",
                    backend="cloud-review",
                    role="reviewer",
                    output=(root / "review-1.log").read_text(encoding="utf-8"),
                    elapsed_seconds=0.1,
                    exit_code=malformed.exception.returncode,
                )
            )

            (root / "review-2.json").unlink(missing_ok=True)
            with mock.patch.dict(os.environ, {"FAKE_REVIEWER_MODE": "quota9"}):
                with self.assertRaises(MODULE.CommandError) as quota:
                    MODULE.call_reviewer(settings, root, "prompt", root, 2)
            stages.append(
                MODULE.usage_stage(
                    stage="review-quota",
                    backend="cloud-review",
                    role="reviewer",
                    output=(root / "review-2.log").read_text(encoding="utf-8"),
                    elapsed_seconds=0.1,
                    exit_code=quota.exception.returncode,
                )
            )
            MODULE.write_summary(
                root,
                project=root,
                model="fake",
                status="needs_manual_attention",
                review=None,
                validations=[],
                usage_stages=stages,
            )
            persisted = json.loads((root / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [stage["exit_code"] for stage in persisted["usage"]["stages"]],
            [0, 7, 124, 0, 9],
        )
        self.assertEqual(persisted["usage"]["stages"][0]["measurement"], "exact")

    def test_real_fake_codex_traverses_command_run_tracker_and_summary(self):
        scenarios = {
            "success": ([0, 0], None, None),
            "fail7": ([7], MODULE.CommandError, "supervisor_tool_failure"),
            "timeout": ([124], subprocess.TimeoutExpired, "supervisor_tool_failure"),
            "malformed": (
                [0, 0],
                MODULE.StructuredResultError,
                "reviewer_result_invalid",
            ),
            "quota9": ([0, 9], MODULE.CommandError, "reviewer_tool_failure"),
        }
        if os.environ.get("MVP_VALIDATION_SANDBOX") == "1":
            # The other four production paths remain mandatory in the formal
            # Seatbelt suite. Timeout is executed by the native host evidence
            # command because the parent sandbox forbids SIGKILL.
            scenarios.pop("timeout")
        for mode, (expected_exits, error_type, classification) in scenarios.items():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                project = Path(directory) / "project"
                project.mkdir()
                subprocess.run(["git", "init", "-q"], cwd=project, check=True)
                fake = project / "fake-codex"
                fake.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json, os, sys, time\n"
                    "from pathlib import Path\n"
                    "args = sys.argv[1:]\n"
                    "mode = os.environ.get('FAKE_CODEX_MODE', 'success')\n"
                    "out = Path(args[args.index('-o') + 1])\n"
                    "if '--output-schema' in args:\n"
                    "    if mode == 'quota9': raise SystemExit(9)\n"
                    "    if mode == 'malformed': out.write_text('{', encoding='utf-8')\n"
                    "    else:\n"
                    "        out.write_text(json.dumps({'verdict':'pass','summary':'ok',"
                    "'findings':[],'tests':[]}), encoding='utf-8')\n"
                    "        print(json.dumps({'type':'turn.completed','usage':"
                    "{'input_tokens':20,'output_tokens':3}}))\n"
                    "else:\n"
                    "    if mode == 'timeout': time.sleep(5)\n"
                    "    if mode == 'fail7': raise SystemExit(7)\n"
                    "    out.write_text('supervised', encoding='utf-8')\n"
                    "    print(json.dumps({'type':'turn.completed','usage':"
                    "{'input_tokens':30,'output_tokens':4}}))\n",
                    encoding="utf-8",
                )
                fake.chmod(0o755)
                plan = project / "approved.md"
                plan.write_text("Risk classification: high\n", encoding="utf-8")
                (project / ".mvp-ai.toml").write_text(
                    '[validation]\ncommands = ["test"]\n', encoding="utf-8"
                )
                run_dir = project / "run"
                run_dir.mkdir()
                settings = replace(
                    MODULE.load_settings(),
                    codex_command=str(fake),
                    supervisor_timeout_seconds=3,
                    review_timeout_seconds=3,
                )
                valid = [{"command": "test", "exit_code": 0, "log": None}]
                docs = {
                    "command": "documentation-contract",
                    "exit_code": 0,
                    "log": None,
                }
                self.attach_validation_logs(project / "mock-validation.log", valid, docs)
                args = SimpleNamespace(
                    project=str(project),
                    plan=str(plan),
                    model="primary",
                    allow_dirty=False,
                    live=False,
                )
                with (
                    mock.patch.object(MODULE, "load_settings", return_value=settings),
                    mock.patch.object(MODULE, "validate_project"),
                    mock.patch.object(MODULE, "preflight_host"),
                    mock.patch.object(MODULE, "make_run_dir", return_value=run_dir),
                    mock.patch.object(MODULE, "run_validations", return_value=valid),
                    mock.patch.object(
                        MODULE, "validate_development_documents", return_value=docs
                    ),
                    mock.patch.dict(os.environ, {"FAKE_CODEX_MODE": mode}),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    if error_type is None:
                        self.assertEqual(MODULE.command_run(args), 0)
                    else:
                        with self.assertRaises(error_type):
                            MODULE.command_run(args)
                summary = json.loads(
                    (run_dir / "summary.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    [item["exit_code"] for item in summary["usage"]["stages"]],
                    expected_exits,
                )
                self.assertEqual(
                    summary["workflow"]["failure_classification"], classification
                )
                if error_type is None:
                    self.assertEqual(summary["status"], "ready_for_user_review")
                else:
                    self.assertEqual(summary["status"], "needs_manual_attention")

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
            state = root / "state"
            project = root / "project"
            project.mkdir()
            with mock.patch.dict(
                os.environ, {"LOCAL_AI_MVP_STATE_DIR": str(state)}
            ):
                run_dir = MODULE.make_run_dir(project)
            mode = stat.S_IMODE(run_dir.stat().st_mode)
            self.assertEqual(mode, 0o700)
            self.assertFalse(run_dir.resolve().is_relative_to(project.resolve()))

    def test_invalid_state_directories_do_not_modify_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            outside = root / "outside"
            outside.mkdir()
            linked_parent = root / "linked-state"
            linked_parent.symlink_to(project, target_is_directory=True)
            original = set(project.iterdir())
            for state in (
                "relative-state",
                str(project / "state"),
                str(linked_parent / "nested"),
            ):
                with self.subTest(state=state), mock.patch.dict(
                    os.environ, {"LOCAL_AI_MVP_STATE_DIR": state}
                ), mock.patch.object(Path, "cwd", return_value=project):
                    with self.assertRaisesRegex(
                        MODULE.WorkflowError, "workspace 之外|符号链接"
                    ):
                        MODULE.make_run_dir(project)
                self.assertEqual(set(project.iterdir()), original)
            safe_state = outside / "state"
            safe_state.mkdir()
            (safe_state / "runs").symlink_to(project, target_is_directory=True)
            with mock.patch.dict(
                os.environ, {"LOCAL_AI_MVP_STATE_DIR": str(safe_state)}
            ):
                with self.assertRaisesRegex(MODULE.WorkflowError, "符号链接"):
                    MODULE.make_run_dir(project)
            self.assertEqual(set(project.iterdir()), original)

    def test_atomic_evidence_write_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            victim = root / "victim.txt"
            victim.write_text("keep", encoding="utf-8")
            link = root / "summary.json"
            link.symlink_to(victim)
            with self.assertRaisesRegex(MODULE.WorkflowError, "符号链接"):
                MODULE.atomic_write_text(link, "overwrite")
            self.assertEqual(victim.read_text(encoding="utf-8"), "keep")

    def test_root_project_agent_plan_tampering_is_blocked(self):
        valid = [{"command": "test", "exit_code": 0, "log": None}]
        docs = {
            "command": "documentation-contract",
            "exit_code": 0,
            "log": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            plan = Path(directory) / "approved.md"
            plan.write_text("Risk classification: high\n", encoding="utf-8")
            baseline_container = Path(directory) / "baseline"
            baseline_project = baseline_container / "workspace"
            baseline_project.mkdir(parents=True)
            self.attach_validation_logs(Path(directory) / "mock-validation.log", valid, docs)
            args = SimpleNamespace(
                project=str(MODULE.ROOT),
                plan=str(plan),
                model="primary",
                allow_dirty=False,
                live=False,
            )

            def tamper_plan(
                _settings, _project, _prompt, run_dir, _stage=None, **_kwargs
            ):
                (run_dir / "plan.md").write_text("tampered", encoding="utf-8")
                return subprocess.CompletedProcess([], 0, "", "")

            with (
                mock.patch.dict(
                    os.environ, {"LOCAL_AI_MVP_STATE_DIR": str(state)}
                ),
                mock.patch.object(MODULE, "validate_project"),
                mock.patch.object(MODULE, "preflight_host"),
                mock.patch.object(
                    MODULE,
                    "make_disposable_baseline_workspace",
                    return_value=(baseline_container, baseline_project),
                ),
                mock.patch.object(MODULE, "run_validations", return_value=valid),
                mock.patch.object(
                    MODULE, "validate_development_documents", return_value=docs
                ),
                mock.patch.object(
                    MODULE, "call_supervisor", side_effect=tamper_plan
                ),
                mock.patch.object(MODULE, "call_reviewer") as reviewer,
                mock.patch.object(MODULE, "write_failure_summary") as failure,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(MODULE.WorkflowError, "批准计划"):
                    MODULE.command_run(args)
            reviewer.assert_not_called()
            self.assertEqual(
                failure.call_args.kwargs["workflow"]["failure_classification"],
                "approved_plan_integrity",
            )
            run_dir = next((state / "runs").iterdir())
            self.assertFalse(
                run_dir.resolve().is_relative_to(MODULE.ROOT.resolve())
            )

    def test_reviewer_prompt_declares_uncommitted_scope(self):
        prompt = MODULE.render_prompt(
            "reviewer.md", plan="plan", validation="[]", today="2026-07-14"
        )
        self.assertIn("staged、unstaged 和 untracked", prompt)
        self.assertIn("prior_review_input_not_current_verdict", prompt)

    def test_review_schema_is_valid_json(self):
        schema = json.loads(MODULE.REVIEW_SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(schema["type"], "object")
        self.assertIn("verdict", schema["required"])

    def test_reviewer_rejects_non_object_json_roots(self):
        settings = SimpleNamespace(
            codex_command="codex",
            cloud_provider="openai",
            cloud_reasoning_effort="high",
            cloud_model="test-model",
            review_timeout_seconds=10,
        )
        for value in (None, 42, [], True, "text"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                run_dir = root / "run"
                run_dir.mkdir()

                def fake_run(*args, **kwargs):
                    (run_dir / "review-1.json").write_text(
                        json.dumps(value), encoding="utf-8"
                    )
                    return subprocess.CompletedProcess([], 0, "", "")

                with mock.patch.object(MODULE, "run_command", side_effect=fake_run):
                    with self.assertRaisesRegex(
                        MODULE.WorkflowError, "根节点必须是对象"
                    ):
                        MODULE.call_reviewer(
                            settings, root, "review", run_dir, 1
                        )

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

    def test_p3_only_review_is_normalized_to_non_blocking_pass(self):
        review = {
            "verdict": "fail",
            "findings": [
                {
                    "severity": "P3",
                    "file": "README.md",
                    "required_fix": "optional wording",
                }
            ],
        }
        normalized = MODULE.normalize_review(
            review, [{"command": "test", "exit_code": 0}]
        )
        self.assertEqual(normalized["verdict"], "pass")

    def test_risk_category_p3_never_normalizes_or_routes_to_ready(self):
        validations = [{"command": "test", "exit_code": 0}]
        for verdict in ("pass", "fail"):
            for category in (
                "security", "data_loss", "reliability", "other"
            ):
                with self.subTest(verdict=verdict, category=category):
                    review = {
                        "verdict": verdict,
                        "findings": [
                            {
                                "severity": "P3",
                                "category": category,
                                "file": "app.py",
                                "required_fix": "must investigate",
                            }
                        ],
                    }
                    normalized = MODULE.normalize_review(review, validations)
                    self.assertEqual(normalized["verdict"], "fail")
                    self.assertEqual(
                        MODULE.decide_review_action(normalized),
                        "supervisor_takeover",
                    )

    def test_empty_fail_stays_blocking_and_malformed_finding_is_rejected(self):
        empty_fail = {"verdict": "fail", "findings": []}
        with self.assertRaises(MODULE.StructuredResultError):
            MODULE.normalize_review(
                empty_fail, [{"command": "test", "exit_code": 0}]
            )
        self.assertEqual(
            MODULE.decide_review_action(empty_fail), "supervisor_takeover"
        )
        malformed = {
            "verdict": "fail",
            "findings": [{"severity": "PX", "file": "a"}],
        }
        with self.assertRaises(MODULE.StructuredResultError):
            MODULE.normalize_review(
                malformed, [{"command": "test", "exit_code": 0}]
            )

    def test_review_contract_rejects_incomplete_or_extra_fields(self):
        valid = {
            "verdict": "pass",
            "summary": "ok",
            "findings": [],
            "tests": [],
        }
        MODULE.validate_review_contract(valid)
        categorized = {
            "verdict": "fail",
            "summary": "security issue",
            "findings": [{
                "severity": "P2", "category": "security", "title": "SQL injection",
                "file": "app.py", "line": 1, "description": "unsafe query",
                "required_fix": "parameterize query",
            }],
            "tests": [],
        }
        MODULE.validate_review_contract(categorized)
        missing_category = json.loads(json.dumps(categorized))
        del missing_category["findings"][0]["category"]
        with self.assertRaises(MODULE.StructuredResultError):
            MODULE.validate_review_contract(missing_category)
        with self.assertRaises(MODULE.StructuredResultError):
            MODULE.validate_review_contract(
                {"verdict": "fail", "summary": "ambiguous", "findings": [], "tests": []}
            )
        for malformed in (
            {"verdict": "fail", "summary": "bad", "findings": []},
            {**valid, "extra": True},
            {
                **valid,
                "findings": [
                    {
                        "severity": "PX",
                        "title": "bad",
                        "file": "a",
                        "line": None,
                        "description": "bad",
                        "required_fix": "fix",
                    }
                ],
            },
        ):
            with self.assertRaises(MODULE.StructuredResultError):
                MODULE.validate_review_contract(malformed)

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

    @mock.patch.object(MODULE, "run_command")
    def test_local_coder_redacts_last_message_on_failure(self, run_command):
        settings = SimpleNamespace(
            codex_command="codex",
            coder_timeout_seconds=30,
            local_stall_timeout_seconds=300,
        )
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)

            def fail(*args, **kwargs):
                (run_dir / "failed-last-message.txt").write_text(
                    '{"credentials": [\n"must-not-persist"\n}', encoding="utf-8"
                )
                raise MODULE.WorkflowError("failed")

            run_command.side_effect = fail
            with self.assertRaises(MODULE.WorkflowError):
                MODULE.call_local_coder(
                    settings,
                    run_dir,
                    "model",
                    "prompt",
                    run_dir,
                    "failed",
                )
            persisted = (run_dir / "failed-last-message.txt").read_text(
                encoding="utf-8"
            )
        self.assertNotIn("must-not-persist", persisted)

    @mock.patch.object(MODULE.subprocess, "run")
    def test_codex_process_bypasses_proxy_for_loopback(self, subprocess_run):
        subprocess_run.return_value = SimpleNamespace(
            returncode=0,
            stdout=(
                "API_KEY=must-not-persist "
                "git@git.example.invalid:private/repo.git\n"
                '{"type":"item.completed","metadata":'
                '{"api_key":"json-secret"}}\n'
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "command.log"
            MODULE.run_command(
                ["codex", "https://git.example.invalid/private/repo.git"],
                cwd=Path(directory),
                timeout=1,
                stdin_text=None,
                log_path=log_path,
            )
            persisted = log_path.read_text(encoding="utf-8")
        env = subprocess_run.call_args.kwargs["env"]
        self.assertEqual(env["NO_PROXY"], "localhost,127.0.0.1,::1")
        self.assertEqual(env["no_proxy"], "localhost,127.0.0.1,::1")
        self.assertNotIn("must-not-persist", persisted)
        self.assertNotIn("private/repo.git", persisted)
        self.assertNotIn("json-secret", persisted)
        json_event = json.loads(persisted.splitlines()[-1])
        self.assertEqual(json_event["metadata"]["api_key"], "[REDACTED]")

    @mock.patch.object(MODULE.subprocess, "run")
    def test_command_log_redacts_malformed_sensitive_subtree(self, subprocess_run):
        subprocess_run.return_value = SimpleNamespace(
            returncode=0,
            stdout='{"credentials": [\n"malformed-command-secret"\n}',
        )
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "command.log"
            MODULE.run_command(
                ["tool"], cwd=Path(directory), timeout=1,
                stdin_text=None, log_path=log,
            )
            persisted = log.read_text(encoding="utf-8")
        self.assertNotIn("malformed-command-secret", persisted)

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
                    '{"verdict":"pass","summary":"API_KEY=must-not-persist",'
                    '"findings":[],"tests":[]}',
                    encoding="utf-8",
                )

            run_command.side_effect = write_review
            MODULE.call_reviewer(
                settings, Path(directory), "prompt", run_dir, 1
            )
            persisted_review = (run_dir / "review-1.json").read_text(
                encoding="utf-8"
            )
        command = run_command.call_args.args[0]
        self.assertIn('model_provider="openai"', command)
        self.assertIn("cloud-reviewer", command)
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertEqual(command[command.index("-s") + 1], "read-only")
        self.assertEqual(command[4], "-c")
        self.assertIn("--json", command)
        self.assertNotIn("must-not-persist", persisted_review)

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

    def test_validation_profile_denies_common_auth_paths_at_any_depth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            profile = MODULE.write_validation_profile(project, run_dir).read_text(
                encoding="utf-8"
            )
        for marker in (
            r"\.env(rc|\..*)?", r"[^/]+\.env", r"\.direnv", r"\.docker"
        ):
            self.assertIn(marker, profile)
        self.assertIn(r"\.git-credentials", profile)

    @unittest.skipIf(
        os.environ.get("MVP_VALIDATION_SANDBOX") == "1",
        "父级验证 Seatbelt 中不能可靠嵌套 sandbox-exec",
    )
    @unittest.skipUnless(sys.platform == "darwin", "requires macOS sandbox-exec")
    def test_validation_seatbelt_cannot_read_common_auth_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            (project / ".envrc").write_text("secret\n", encoding="utf-8")
            (project / "production.env").write_text(
                "ROOT_ENV_MARKER\n", encoding="utf-8"
            )
            (project / ".direnv").mkdir()
            (project / ".direnv" / "config").write_text("secret\n", encoding="utf-8")
            docker = project / "nested" / ".docker"
            docker.mkdir(parents=True)
            (docker / "config.json").write_text("secret\n", encoding="utf-8")
            (docker / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            (project / "nested" / "production.env").write_text(
                "NESTED_ENV_MARKER\n", encoding="utf-8"
            )
            credential_files = {
                "application_default_credentials.json": "ADC_MARKER",
                "client_credentials.json": "CLIENT_CREDENTIAL_MARKER",
                "firebase-credentials.json": "FIREBASE_CREDENTIAL_MARKER",
            }
            for name, marker in credential_files.items():
                (project / name).write_text(marker + "\n", encoding="utf-8")
            outcomes = MODULE.run_validations(
                project,
                [
                    "! /bin/cat .envrc >/dev/null 2>&1",
                    "! /bin/cat production.env >/dev/null 2>&1",
                    "! /bin/cat .direnv/config >/dev/null 2>&1",
                    "! /bin/cat nested/.docker/config.json >/dev/null 2>&1",
                    "! /bin/cat nested/production.env >/dev/null 2>&1",
                    "! /bin/cat application_default_credentials.json >/dev/null 2>&1",
                    "! /bin/cat client_credentials.json >/dev/null 2>&1",
                    "! /bin/cat firebase-credentials.json >/dev/null 2>&1",
                    "/bin/cat nested/.docker/Dockerfile >/dev/null 2>&1",
                ],
                run_dir,
                "secret-deny",
                project_writable=False,
            )
            logs = "\n".join(
                Path(item["log"]).read_text(encoding="utf-8")
                for item in outcomes
            )
        self.assertEqual(
            [item["exit_code"] for item in outcomes],
            [0, 0, 0, 0, 0, 0, 0, 0, 0],
        )
        self.assertNotIn("ROOT_ENV_MARKER", logs)
        self.assertNotIn("NESTED_ENV_MARKER", logs)
        for marker in credential_files.values():
            self.assertNotIn(marker, logs)

    @unittest.skipIf(
        os.environ.get("MVP_VALIDATION_SANDBOX") == "1",
        "父级验证 Seatbelt 中不能可靠嵌套 sandbox-exec",
    )
    @unittest.skipUnless(sys.platform == "darwin", "requires macOS sandbox-exec")
    def test_git_auth_config_is_sanitized_but_git_status_remains_usable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            (project / "source.txt").write_text("source\n", encoding="utf-8")
            worktree_secret_marker = "TRACKED_WORKTREE_SECRET_MARKER"
            (project / ".env").write_text(
                worktree_secret_marker + "\n", encoding="utf-8"
            )
            subprocess.run(["git", "add", "."], cwd=project, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "base"], cwd=project, check=True,
            )
            remote_marker = "REMOTE_AUTH_MARKER"
            header_marker = "EXTRA_HEADER_MARKER"
            subprocess.run(
                ["git", "remote", "add", "origin", f"https://user:{remote_marker}@example.invalid/repo"],
                cwd=project, check=True,
            )
            subprocess.run(
                ["git", "config", "http.extraHeader", f"Authorization: Bearer {header_marker}"],
                cwd=project, check=True,
            )
            (project / ".git" / "config.worktree").write_text(
                "[http]\n\textraHeader = Bearer WORKTREE_AUTH_MARKER\n",
                encoding="utf-8",
            )
            nested_config = project / ".git" / "worktrees" / "other" / "config"
            nested_config.parent.mkdir(parents=True)
            nested_config.write_text(
                "[http]\n\textraHeader = Bearer NESTED_AUTH_MARKER\n",
                encoding="utf-8",
            )
            metadata_markers = {
                project / ".git" / "FETCH_HEAD": "FETCH_HEAD_AUTH_MARKER",
                project / ".git" / "ORIG_HEAD": "ORIG_HEAD_AUTH_MARKER",
                project / ".git" / "logs" / "refs" / "remotes" / "origin" / "main":
                    "REFLOG_AUTH_MARKER",
                project / ".git" / "hooks" / "credential-helper":
                    "HOOK_AUTH_MARKER",
            }
            for path, marker in metadata_markers.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(marker + "\n", encoding="utf-8")
            (project / "staged.txt").write_text("staged\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "staged.txt"], cwd=project, check=True
            )
            (project / "source.txt").write_text(
                "unstaged trailing whitespace  \n", encoding="utf-8"
            )
            (project / "untracked.txt").write_text(
                "untracked\n", encoding="utf-8"
            )
            preexisting_child = project / "preexisting-child"
            preexisting_child.mkdir()
            subprocess.run(
                ["git", "init", "-q"], cwd=preexisting_child, check=True
            )
            child_auth_marker = "PREEXISTING_CHILD_AUTH_MARKER"
            subprocess.run(
                [
                    "git", "config", "http.extraHeader",
                    f"Authorization: Bearer {child_auth_marker}",
                ],
                cwd=preexisting_child,
                check=True,
            )

            container, workspace = MODULE.make_disposable_baseline_workspace(
                project, run_dir
            )
            try:
                baseline_config = (workspace / ".git" / "config").read_text(
                    encoding="utf-8"
                )
                self.assertIn("[core]", baseline_config)
                self.assertNotIn("origin", baseline_config)
                self.assertNotIn(remote_marker, baseline_config)
                self.assertNotIn(header_marker, baseline_config)
                self.assertFalse((workspace / ".git" / "config.worktree").exists())
                self.assertFalse((workspace / ".git" / "worktrees").exists())
                for relative in (
                    "FETCH_HEAD",
                    "ORIG_HEAD",
                    "logs/refs/remotes/origin/main",
                    "hooks/credential-helper",
                ):
                    self.assertFalse((workspace / ".git" / relative).exists())
                baseline = MODULE.run_validations(
                    workspace,
                    [
                        "! /bin/cat .git/logs/HEAD >/dev/null 2>&1",
                        "git status --porcelain >/dev/null",
                    ],
                    run_dir,
                    "git-config-baseline",
                    project_writable=False,
                )
            finally:
                MODULE.cleanup_baseline_workspace(container)

            formal = MODULE.run_validations(
                project,
                [
                    "! /bin/cat .git/config >/dev/null 2>&1",
                    "! /bin/cat .git/config.worktree >/dev/null 2>&1",
                    "! /bin/cat .git/worktrees/other/config >/dev/null 2>&1",
                    "! /bin/cat .git/FETCH_HEAD >/dev/null 2>&1",
                    "! /bin/cat .git/ORIG_HEAD >/dev/null 2>&1",
                    "! /bin/cat .git/logs/refs/remotes/origin/main >/dev/null 2>&1",
                    "! /bin/cat .git/hooks/credential-helper >/dev/null 2>&1",
                    "! /usr/bin/base64 .git/FETCH_HEAD >/dev/null 2>&1",
                    "! git show HEAD:.env >/dev/null 2>&1",
                    "git status --porcelain >/dev/null",
                    "git status --porcelain | /usr/bin/grep -q '^A  staged.txt$'",
                    "git status --porcelain | /usr/bin/grep -q '^ M source.txt$'",
                    "git status --porcelain | /usr/bin/grep -q '^?? untracked.txt$'",
                    "git diff --cached --name-only | /usr/bin/grep -q '^staged.txt$'",
                    "git diff --name-only | /usr/bin/grep -q '^source.txt$'",
                    "! git diff --check >/dev/null 2>&1",
                    "! /bin/cat preexisting-child/.git/config >/dev/null 2>&1",
                    "! git -C preexisting-child status >/dev/null 2>&1",
                    "mkdir validation-child && git -C validation-child init -q && "
                    "printf child > validation-child/file.txt && "
                    "git -C validation-child add file.txt && "
                    "git -C validation-child -c user.name=Test "
                    "-c user.email=test@example.invalid commit -qm child && "
                    "test -z \"$(git -C validation-child status --porcelain)\" && "
                    "test -z \"$(cd validation-child && git status --porcelain)\"",
                ],
                run_dir,
                "git-config-formal",
                project_writable=True,
            )
            logs = "\n".join(
                Path(item["log"]).read_text(encoding="utf-8")
                for item in baseline + formal
            )
        self.assertEqual(
            [item["exit_code"] for item in baseline + formal],
            [0] * 21,
        )
        for marker in (
            remote_marker,
            header_marker,
            "WORKTREE_AUTH_MARKER",
            "NESTED_AUTH_MARKER",
            *metadata_markers.values(),
            worktree_secret_marker,
            child_auth_marker,
        ):
            self.assertNotIn(marker, logs)

    @unittest.skipIf(
        os.environ.get("MVP_VALIDATION_SANDBOX") == "1",
        "父级验证 Seatbelt 中不能可靠嵌套 sandbox-exec",
    )
    @unittest.skipUnless(sys.platform == "darwin", "requires macOS sandbox-exec")
    def test_linked_worktree_uses_sanitized_git_view(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "main"
            worktree = root / "linked"
            run_dir = root / "run"
            main.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=main, check=True)
            (main / "source.txt").write_text("source\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=main, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "base"], cwd=main, check=True,
            )
            subprocess.run(
                ["git", "worktree", "add", "-q", "-b", "validation-linked", str(worktree)],
                cwd=main, check=True,
            )
            actual_git_dir = Path(
                subprocess.run(
                    ["git", "rev-parse", "--absolute-git-dir"], cwd=worktree,
                    text=True, stdout=subprocess.PIPE, check=True,
                ).stdout.strip()
            )
            worktree_marker = "LINKED_WORKTREE_AUTH_MARKER"
            (actual_git_dir / "config.worktree").write_text(
                f"[http]\n\textraHeader = Bearer {worktree_marker}\n",
                encoding="utf-8",
            )
            quoted_config = shlex.quote(str(actual_git_dir / "config.worktree"))
            outcomes = MODULE.run_validations(
                worktree,
                [
                    f"! /bin/cat {quoted_config} >/dev/null 2>&1",
                    "git status --porcelain >/dev/null",
                ],
                run_dir,
                "linked-worktree",
                project_writable=False,
            )
            logs = "\n".join(
                Path(item["log"]).read_text(encoding="utf-8")
                for item in outcomes
            )
        self.assertEqual([item["exit_code"] for item in outcomes], [0, 0])
        self.assertNotIn(worktree_marker, logs)

    @mock.patch.object(MODULE.subprocess, "run")
    def test_validation_logs_are_redacted(self, subprocess_run):
        subprocess_run.return_value = SimpleNamespace(
            returncode=1,
            stdout=(
                "API_KEY=must-not-persist\n"
                '{\n  "credentials": [\n'
                '    "nested-validation-secret"\n  }\n'
                "git@git.example.invalid:private/repo.git\n"
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            outcomes = MODULE.run_validations(
                project, ["test-command"], run_dir, "redacted"
            )
            persisted = Path(outcomes[0]["log"]).read_text(encoding="utf-8")
        self.assertNotIn("must-not-persist", persisted)
        self.assertNotIn("nested-validation-secret", persisted)
        self.assertNotIn("private/repo.git", persisted)

    def test_preflight_profile_denies_all_target_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            profile = MODULE.write_validation_profile(
                project, run_dir, project_writable=False
            ).read_text(encoding="utf-8")
        self.assertNotIn(
            f'(allow file-write* (subpath "{project.resolve()}"))', profile
        )
        self.assertIn(
            f'(allow file-write* (subpath "{run_dir.resolve()}"))', profile
        )
        self.assertIn(
            f'(deny file-read* (subpath "{project.resolve() / ".git"}"))',
            profile,
        )

    @unittest.skipIf(
        os.environ.get("MVP_VALIDATION_SANDBOX") == "1",
        "父级验证 Seatbelt 中不能可靠嵌套 sandbox-exec",
    )
    @unittest.skipUnless(sys.platform == "darwin", "requires macOS sandbox-exec")
    def test_disposable_baseline_allows_ignored_build_outputs_without_pollution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            (project / ".gitignore").write_text(
                "target/\n.env\n*.pem\n", encoding="utf-8"
            )
            (project / "source.txt").write_text("clean\n", encoding="utf-8")
            (project / ".env").write_text(
                "IGNORED_SECRET_MARKER=never-copy\n", encoding="utf-8"
            )
            (project / ".envrc").write_text("never-copy\n", encoding="utf-8")
            (project / "production.env").write_text("never-copy\n", encoding="utf-8")
            (project / ".direnv").mkdir()
            (project / ".direnv" / "config").write_text(
                "never-copy\n", encoding="utf-8"
            )
            docker = project / "deep" / ".docker"
            docker.mkdir(parents=True)
            (docker / "config.json").write_text("never-copy\n", encoding="utf-8")
            (docker / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            (project / "deep" / "production.env").write_text(
                "never-copy\n", encoding="utf-8"
            )
            for name in (
                "application_default_credentials.json",
                "client_credentials.json",
                "firebase-credentials.json",
            ):
                (project / name).write_text("never-copy\n", encoding="utf-8")
            (project / "credential_parser.py").write_text(
                "print('ordinary source')\n", encoding="utf-8"
            )
            locked = project / "nested"
            locked.mkdir()
            (locked / "private.pem").write_text(
                "ignored-private-marker\n", encoding="utf-8"
            )
            (locked / "ordinary.txt").write_text("keep in copy\n", encoding="utf-8")
            locked.chmod(0o500)
            container, workspace = MODULE.make_disposable_baseline_workspace(
                project, run_dir
            )
            try:
                self.assertFalse((workspace / ".env").exists())
                self.assertFalse((workspace / ".envrc").exists())
                self.assertFalse((workspace / "production.env").exists())
                self.assertFalse((workspace / ".direnv").exists())
                self.assertFalse(
                    (workspace / "deep" / ".docker" / "config.json").exists()
                )
                self.assertTrue(
                    (workspace / "deep" / ".docker" / "Dockerfile").is_file()
                )
                self.assertFalse((workspace / "deep" / "production.env").exists())
                for name in (
                    "application_default_credentials.json",
                    "client_credentials.json",
                    "firebase-credentials.json",
                ):
                    self.assertFalse((workspace / name).exists())
                self.assertTrue((workspace / "credential_parser.py").is_file())
                self.assertFalse((workspace / "nested" / "private.pem").exists())
                self.assertTrue((workspace / "nested" / "ordinary.txt").is_file())
                outcomes = MODULE.run_validations(
                    workspace,
                    [
                        "mkdir -p \"$HOME/locked\" && : > \"$HOME/locked/cache\" && "
                        "chmod 000 \"$HOME/locked\" && "
                        "mkdir -p target && printf built > target/output && "
                        "test -s target/output"
                    ],
                    run_dir,
                    "preflight",
                    project_writable=True,
                )
                self.assertEqual([item["exit_code"] for item in outcomes], [0, 0])
                self.assertTrue((workspace / "target" / "output").is_file())
                self.assertEqual(
                    list(root.glob(f".{run_dir.name}-preflight-validation-*")), []
                )
                (workspace / "source.txt").write_text("changed\n", encoding="utf-8")
            finally:
                MODULE.cleanup_baseline_workspace(container)
            self.assertFalse(container.exists())
            self.assertEqual(
                (project / "source.txt").read_text(encoding="utf-8"), "clean\n"
            )
            self.assertFalse((project / "target").exists())

    def test_baseline_copy_failure_cleans_partial_sanitized_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()

            def fail_after_partial_copy(command, **_kwargs):
                workspace = Path(command[-1])
                workspace.mkdir(parents=True, exist_ok=True)
                (workspace / "partial.txt").write_text("partial", encoding="utf-8")
                return SimpleNamespace(returncode=23, stdout="simulated copy failure")

            with mock.patch.object(
                MODULE.subprocess, "run", side_effect=fail_after_partial_copy
            ):
                with self.assertRaisesRegex(MODULE.WorkflowError, "无法创建"):
                    MODULE.make_disposable_baseline_workspace(project, run_dir)
            self.assertEqual(
                list(root.glob(f".{run_dir.name}-baseline-workspace-*")), []
            )

    def test_baseline_cleanup_failure_is_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            container = Path(directory) / "baseline"
            container.mkdir()
            (container / "leftover").write_text("x", encoding="utf-8")
            with mock.patch.object(
                MODULE.shutil, "rmtree", side_effect=OSError("busy")
            ):
                with self.assertRaises(MODULE.BaselineCleanupError):
                    MODULE.cleanup_baseline_workspace(container)
            self.assertTrue(container.exists())
            MODULE.cleanup_baseline_workspace(container)
            self.assertFalse(container.exists())

    def test_validation_cleanup_error_preserves_completed_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            completed = subprocess.CompletedProcess([], 0, stdout="passed\n")
            with (
                mock.patch.object(MODULE, "write_validation_profile", return_value=root / "validation.sb"),
                mock.patch.object(MODULE, "validation_environment", return_value=os.environ.copy()),
                mock.patch.object(MODULE.subprocess, "run", return_value=completed),
                mock.patch.object(
                    MODULE,
                    "cleanup_validation_workspace",
                    side_effect=MODULE.ValidationCleanupError("busy"),
                ),
            ):
                with self.assertRaises(MODULE.ValidationCleanupError) as raised:
                    MODULE.run_validations(project, ["test"], run_dir, "post-edit")
            self.assertEqual(len(raised.exception.outcomes), 1)
            self.assertEqual(raised.exception.outcomes[0]["command"], "test")
            self.assertEqual(raised.exception.outcomes[0]["exit_code"], 0)
            self.assertTrue(Path(raised.exception.outcomes[0]["log"]).is_file())

    def test_preflight_failures_keep_host_pass_and_use_precise_stage(self):
        cases = (
            ("local-model", "low", "model"),
            ("baseline-copy", "high", "copy"),
            ("baseline-validation-cleanup", "high", "validation-cleanup"),
            ("baseline-cleanup", "high", "cleanup"),
        )
        for expected_stage, risk, failure_point in cases:
            with self.subTest(stage=expected_stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = root / "project"
                run_dir = root / "run"
                baseline_container = root / "baseline"
                project.mkdir()
                run_dir.mkdir()
                baseline_container.mkdir()
                plan = root / "approved.md"
                plan.write_text(f"Risk classification: {risk}\n", encoding="utf-8")
                valid = [{"command": "test", "exit_code": 0}]
                args = SimpleNamespace(
                    project=str(project), plan=str(plan), model="primary",
                    allow_dirty=False, live=False,
                )
                model_error = (
                    MODULE.WorkflowError("model unavailable")
                    if failure_point == "model" else None
                )
                copy_result = (
                    MODULE.WorkflowError("copy failed")
                    if failure_point == "copy"
                    else (baseline_container, project)
                )
                cleanup_error = (
                    MODULE.BaselineCleanupError("cleanup failed")
                    if failure_point == "cleanup" else None
                )
                validation_error = (
                    MODULE.ValidationCleanupError("cleanup failed", outcomes=valid)
                    if failure_point == "validation-cleanup" else None
                )
                with (
                    mock.patch.object(MODULE, "validate_project"),
                    mock.patch.object(MODULE, "load_validation_commands", return_value=["test"]),
                    mock.patch.object(
                        MODULE, "freeze_project_preflight_state",
                        return_value=({"head": "base", "porcelain_sha256": "clean", "config_sha256": "config"}, ["test"]),
                    ),
                    mock.patch.object(MODULE, "make_run_dir", return_value=run_dir),
                    mock.patch.object(
                        MODULE, "preflight_host",
                        return_value={"stage": "host", "status": "passed", "classification": None},
                    ),
                    mock.patch.object(MODULE, "ensure_model_available", side_effect=model_error),
                    mock.patch.object(MODULE, "make_disposable_baseline_workspace", side_effect=copy_result if isinstance(copy_result, Exception) else None, return_value=copy_result if not isinstance(copy_result, Exception) else None),
                    mock.patch.object(
                        MODULE, "run_validations",
                        side_effect=validation_error,
                        return_value=valid,
                    ),
                    mock.patch.object(MODULE, "cleanup_baseline_workspace", side_effect=cleanup_error),
                    mock.patch.object(MODULE, "write_failure_summary") as failure,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    result = MODULE._command_run_locked(args)
                self.assertEqual(result, 2)
                preflight = failure.call_args.kwargs["preflight"]
                self.assertEqual(
                    [(item["stage"], item["status"]) for item in preflight],
                    [("host", "passed"), (expected_stage, "failed")],
                )
                if failure_point in {"cleanup", "validation-cleanup"}:
                    self.assertEqual(
                        failure.call_args.kwargs["validations"], valid
                    )

    def test_post_edit_cleanup_failure_summary_keeps_completed_validations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            baseline_container = root / "baseline"
            project.mkdir()
            run_dir.mkdir()
            baseline_container.mkdir()
            plan = root / "approved.md"
            plan.write_text("Risk classification: low\n", encoding="utf-8")
            baseline = [{"command": "baseline", "exit_code": 0, "log": str(root / "baseline.log")}]
            post_edit = [{"command": "post-edit", "exit_code": 0, "log": str(root / "post-edit.log")}]
            for item in baseline + post_edit:
                Path(item["log"]).write_text("passed\n", encoding="utf-8")
            cleanup = MODULE.ValidationCleanupError("busy", outcomes=post_edit)
            args = SimpleNamespace(
                project=str(project), plan=str(plan), model="primary",
                allow_dirty=False, live=False,
            )
            with (
                mock.patch.object(MODULE, "validate_project"),
                mock.patch.object(MODULE, "load_validation_commands", return_value=["test"]),
                mock.patch.object(
                    MODULE, "freeze_project_preflight_state",
                    return_value=({"head": "base", "porcelain_sha256": "clean", "config_sha256": "config"}, ["test"]),
                ),
                mock.patch.object(MODULE, "make_run_dir", return_value=run_dir),
                mock.patch.object(
                    MODULE, "preflight_host",
                    return_value={"stage": "host", "status": "passed", "classification": None},
                ),
                mock.patch.object(MODULE, "ensure_model_available"),
                mock.patch.object(
                    MODULE, "make_disposable_baseline_workspace",
                    return_value=(baseline_container, project),
                ),
                mock.patch.object(MODULE, "cleanup_baseline_workspace"),
                mock.patch.object(MODULE, "run_validations", side_effect=[baseline, cleanup]),
                mock.patch.object(MODULE, "verify_project_integrity_snapshot"),
                mock.patch.object(
                    MODULE, "project_content_snapshot",
                    return_value={"sha256": "stable", "head": "base", "changed_count": 0},
                ),
                mock.patch.object(
                    MODULE, "call_local_coder",
                    return_value=SimpleNamespace(returncode=0),
                ),
                mock.patch.object(MODULE, "write_failure_summary") as failure,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(MODULE.ValidationCleanupError):
                    MODULE._command_run_locked(args)
            validations = failure.call_args.kwargs["validations"]
            self.assertEqual(
                [item["command"] for item in validations],
                ["baseline", "post-edit"],
            )
            self.assertEqual(
                failure.call_args.kwargs["workflow"]["failure_classification"],
                "validation_cleanup_failure",
            )

    @unittest.skipUnless(
        hasattr(os, "chflags") and hasattr(stat, "UF_IMMUTABLE"),
        "requires immutable file flags",
    )
    def test_private_cleanup_reaches_immutable_file_below_mode_zero_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for cleanup, name in (
                (MODULE.cleanup_baseline_workspace, "baseline"),
                (MODULE.cleanup_validation_workspace, "validation"),
            ):
                container = root / name
                locked = container / "locked"
                locked.mkdir(parents=True)
                immutable = locked / "cache"
                immutable.write_text("private\n", encoding="utf-8")
                os.chflags(immutable, stat.UF_IMMUTABLE)
                locked.chmod(0o000)
                cleanup(container)
                self.assertFalse(container.exists())

    @unittest.skipUnless(
        hasattr(os, "chflags") and hasattr(stat, "UF_IMMUTABLE"),
        "requires immutable file flags",
    )
    def test_validation_exception_cleans_immutable_file_below_mode_zero_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()

            def create_locked_tree_then_fail(*_args, **kwargs):
                locked = Path(kwargs["env"]["HOME"]) / "locked"
                locked.mkdir(parents=True)
                immutable = locked / "cache"
                immutable.write_text("private\n", encoding="utf-8")
                os.chflags(immutable, stat.UF_IMMUTABLE)
                locked.chmod(0o000)
                raise RuntimeError("injected validation failure")

            with mock.patch.object(
                MODULE.subprocess, "run", side_effect=create_locked_tree_then_fail
            ):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    MODULE.run_validations(
                        project, ["test"], run_dir, "cleanup-exception"
                    )
            self.assertEqual(
                list(root.glob(f".{run_dir.name}-cleanup-exception-validation-*")),
                [],
            )

    def test_validation_initialization_failures_leave_no_private_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            cases = (
                mock.patch.object(
                    MODULE, "write_validation_profile",
                    side_effect=MODULE.WorkflowError("profile failed"),
                ),
                mock.patch.object(
                    MODULE, "validation_environment",
                    side_effect=MODULE.WorkflowError("environment failed"),
                ),
            )
            for index, patcher in enumerate(cases):
                with patcher, self.assertRaises(MODULE.WorkflowError):
                    MODULE.run_validations(
                        project, ["test"], run_dir, f"init-{index}"
                    )
                self.assertEqual(
                    list(root.glob(f".{run_dir.name}-init-{index}-validation-*")), []
                )

            real_chmod = Path.chmod
            calls = 0

            def fail_first_chmod(path, mode, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("chmod failed")
                return real_chmod(path, mode, *args, **kwargs)

            with mock.patch.object(Path, "chmod", fail_first_chmod):
                with self.assertRaisesRegex(OSError, "chmod failed"):
                    MODULE.run_validations(
                        project, ["test"], run_dir, "chmod-init"
                    )
            self.assertEqual(
                list(root.glob(f".{run_dir.name}-chmod-init-validation-*")), []
            )

    def test_project_run_lock_is_exclusive_and_reusable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            state = root / "state"
            project.mkdir()
            with mock.patch.dict(
                os.environ, {"LOCAL_AI_MVP_STATE_DIR": str(state)}
            ):
                first, path = MODULE.acquire_project_run_lock(project)
                try:
                    with self.assertRaisesRegex(MODULE.WorkflowError, "已有"):
                        MODULE.acquire_project_run_lock(project)
                finally:
                    MODULE.release_project_run_lock(first)
                second, second_path = MODULE.acquire_project_run_lock(project)
                MODULE.release_project_run_lock(second)
            self.assertEqual(path, second_path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_project_content_snapshot_covers_repeated_modified_staged_and_untracked_content(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            tracked = project / "tracked.txt"
            tracked.write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=project, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "base"], cwd=project, check=True,
            )
            tracked.write_text("first\n", encoding="utf-8")
            first = MODULE.project_content_snapshot(project)
            tracked.write_text("second\n", encoding="utf-8")
            second = MODULE.project_content_snapshot(project)
            self.assertNotEqual(first["sha256"], second["sha256"])
            subprocess.run(["git", "add", "tracked.txt"], cwd=project, check=True)
            staged = MODULE.project_content_snapshot(project)
            self.assertNotEqual(second["sha256"], staged["sha256"])
            untracked = project / "new.txt"
            untracked.write_text("one\n", encoding="utf-8")
            untracked_first = MODULE.project_content_snapshot(project)
            untracked.write_text("two\n", encoding="utf-8")
            untracked_second = MODULE.project_content_snapshot(project)
            self.assertNotEqual(untracked_first["sha256"], untracked_second["sha256"])
            untracked.chmod(0o755)
            executable = MODULE.project_content_snapshot(project)
            self.assertNotEqual(untracked_second["sha256"], executable["sha256"])

    def test_project_content_snapshot_streams_large_diff_and_untracked_file(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            tracked = project / "tracked.bin"
            tracked.write_bytes(b"base\n")
            subprocess.run(["git", "add", "."], cwd=project, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "base"], cwd=project, check=True,
            )
            with tracked.open("wb") as handle:
                handle.seek(4 * 1024 * 1024)
                handle.write(b"changed")
            untracked = project / "large-untracked.bin"
            with untracked.open("wb") as handle:
                handle.seek(4 * 1024 * 1024)
                handle.write(b"tail-one")
            original_read_bytes = Path.read_bytes

            def reject_whole_file_read(path):
                if path in {tracked, untracked}:
                    raise AssertionError("snapshot must stream large files")
                return original_read_bytes(path)

            with (
                mock.patch.object(Path, "read_bytes", reject_whole_file_read),
                mock.patch.object(
                    MODULE, "git_bytes",
                    side_effect=AssertionError("snapshot must stream Git output"),
                ),
            ):
                first = MODULE.project_content_snapshot(project)
                with untracked.open("r+b") as handle:
                    handle.seek(4 * 1024 * 1024)
                    handle.write(b"tail-two")
                second = MODULE.project_content_snapshot(project)
            self.assertNotEqual(first["sha256"], second["sha256"])

    def test_review_snapshot_change_blocks_normal_and_final_ready_paths(self):
        cases = [
            (risk, phase, index)
            for risk in ("low", "high")
            for phase, index in (
                ("during-review", 2),
                ("before-handoff", 4),
                ("after-ready-write", 5),
            )
        ]
        for risk, phase, failure_index in cases:
            with self.subTest(risk=risk, phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = root / "project"
                run_dir = root / "run"
                project.mkdir()
                run_dir.mkdir()
                subprocess.run(["git", "init", "-q"], cwd=project, check=True)
                (project / ".mvp-ai.toml").write_text(
                    '[validation]\ncommands = ["test"]\n', encoding="utf-8"
                )
                (project / "app.py").write_text("value = 1\n", encoding="utf-8")
                subprocess.run(["git", "add", "."], cwd=project, check=True)
                subprocess.run(
                    ["git", "-c", "user.name=Test", "-c",
                     "user.email=test@example.invalid", "commit", "-qm", "base"],
                    cwd=project, check=True,
                )
                plan = root / "plan.md"
                plan.write_text(f"Risk classification: {risk}\n", encoding="utf-8")
                log = run_dir / "validation.log"
                log.write_text("ok\n", encoding="utf-8")
                valid = [{"command": "test", "exit_code": 0, "log": str(log)}]
                docs_log = run_dir / "docs.log"
                docs_log.write_text("ok\n", encoding="utf-8")
                docs = {
                    "command": "documentation-contract", "exit_code": 0,
                    "log": str(docs_log),
                }
                passed = {
                    "verdict": "pass", "summary": "ok", "findings": [], "tests": []
                }
                args = SimpleNamespace(
                    project=str(project), plan=str(plan), model="primary",
                    allow_dirty=False, live=False,
                )
                completed = subprocess.CompletedProcess([], 0, "", "")
                checks = [None] * failure_index + [
                    MODULE.ProjectChangedError(f"changed {phase}")
                ]
                with (
                    mock.patch.object(MODULE, "make_run_dir", return_value=run_dir),
                    mock.patch.object(MODULE, "preflight_host"),
                    mock.patch.object(MODULE, "ensure_model_available"),
                    mock.patch.object(MODULE, "run_validations", return_value=valid),
                    mock.patch.object(
                        MODULE, "validate_development_documents", return_value=docs
                    ),
                    mock.patch.object(
                        MODULE, "call_local_coder", return_value=completed
                    ),
                    mock.patch.object(
                        MODULE, "call_supervisor", return_value=completed
                    ),
                    mock.patch.object(MODULE, "call_reviewer", return_value=passed),
                    mock.patch.object(
                        MODULE, "verify_project_content_snapshot", side_effect=checks
                    ),
                    mock.patch.object(MODULE, "write_failure_summary") as failure,
                    mock.patch.object(MODULE, "write_summary") as ready,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    with self.assertRaisesRegex(
                        MODULE.ProjectChangedError, "changed"
                    ):
                        MODULE.command_run(args)
                if phase == "after-ready-write":
                    ready.assert_called_once()
                    self.assertEqual(
                        ready.call_args.kwargs["status"], "ready_for_user_review"
                    )
                    self.assertIn("sha256", ready.call_args.kwargs["handoff_snapshot"])
                else:
                    ready.assert_not_called()
                self.assertEqual(
                    failure.call_args.kwargs["workflow"]["failure_classification"],
                    (
                        "project_changed_during_review"
                        if phase == "during-review"
                        else "project_changed_during_handoff"
                    ),
                )

    def test_validation_command_source_mutation_blocks_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            (project / ".mvp-ai.toml").write_text(
                '[validation]\ncommands = ["test"]\n', encoding="utf-8"
            )
            source = project / "app.py"
            source.write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=project, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c",
                 "user.email=test@example.invalid", "commit", "-qm", "base"],
                cwd=project, check=True,
            )
            plan = root / "plan.md"
            plan.write_text("Risk classification: low\n", encoding="utf-8")
            log = run_dir / "validation.log"
            log.write_text("ok\n", encoding="utf-8")
            valid = [{"command": "test", "exit_code": 0, "log": str(log)}]
            docs_log = run_dir / "docs.log"
            docs_log.write_text("ok\n", encoding="utf-8")
            docs = {
                "command": "documentation-contract", "exit_code": 0,
                "log": str(docs_log),
            }
            calls = 0

            def validations(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    source.write_text("value = 2\n", encoding="utf-8")
                return valid

            args = SimpleNamespace(
                project=str(project), plan=str(plan), model="primary",
                allow_dirty=False, live=False,
            )
            with (
                mock.patch.object(MODULE, "make_run_dir", return_value=run_dir),
                mock.patch.object(MODULE, "preflight_host"),
                mock.patch.object(MODULE, "ensure_model_available"),
                mock.patch.object(MODULE, "run_validations", side_effect=validations),
                mock.patch.object(
                    MODULE, "validate_development_documents", return_value=docs
                ),
                mock.patch.object(
                    MODULE, "call_local_coder",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ),
                mock.patch.object(MODULE, "call_reviewer") as reviewer,
                mock.patch.object(MODULE, "write_failure_summary") as failure,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(MODULE.ProjectChangedError):
                    MODULE.command_run(args)
            reviewer.assert_not_called()
            self.assertEqual(
                failure.call_args.kwargs["workflow"]["failure_classification"],
                "project_changed_after_validation",
            )

    def test_failure_summary_survives_missing_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            project.rename(root / "project-moved")
            MODULE.write_failure_summary(
                run_dir,
                project=project,
                model="primary",
                error=MODULE.ProjectChangedError("project moved"),
                review=None,
                validations=[],
                workflow={
                    "failure_classification": "project_changed_during_preflight"
                },
            )
            payload = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "needs_manual_attention")
            self.assertEqual(
                payload["workflow"]["failure_classification"],
                "project_changed_during_preflight",
            )
            self.assertIn("unavailable", payload["git_status"])

    def test_preflight_config_is_loaded_only_between_matching_snapshots(self):
        first = {"head": "a", "porcelain_sha256": "x", "config_sha256": "1"}
        second = {"head": "b", "porcelain_sha256": "y", "config_sha256": "2"}
        with (
            mock.patch.object(
                MODULE,
                "project_integrity_snapshot",
                side_effect=[first, second, second, second],
            ),
            mock.patch.object(
                MODULE,
                "load_validation_commands",
                side_effect=[["old-test"], ["new-test"]],
            ) as load,
        ):
            snapshot, commands = MODULE.freeze_project_preflight_state(Path("/project"))
        self.assertEqual(snapshot, second)
        self.assertEqual(commands, ["new-test"])
        self.assertEqual(load.call_count, 2)

    def test_missing_validation_host_is_environment_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing.log"
            missing.write_text(
                "zsh:1: command not found: missing-tool", encoding="utf-8"
            )
            denied = root / "denied.log"
            denied.write_text(
                "zsh:1: permission denied: ./not-executable", encoding="utf-8"
            )
            relative_missing = root / "relative-missing.log"
            relative_missing.write_text(
                "zsh:1: command not found: ./missing-script", encoding="utf-8"
            )
            application = root / "application.log"
            application.write_text("ordinary assertion failed", encoding="utf-8")
            outcomes = [{"command": "test", "exit_code": 127, "log": str(missing)}]
            self.assertEqual(
                MODULE.classify_validation_failure(outcomes),
                "environment_preflight",
            )
            self.assertEqual(outcomes[0]["classification"], "environment")
            for phase, expected in (
                ("coder-initial", "coder_validation"),
                ("validation-fix", "local_fix_validation"),
            ):
                outcomes = [
                    {"command": "./not-executable", "exit_code": 126, "log": str(denied)}
                ]
                self.assertEqual(
                    MODULE.classify_validation_failure(outcomes, phase=phase),
                    expected,
                )
                self.assertEqual(outcomes[0]["classification"], "code_or_test")
                outcomes = [
                    {
                        "command": "./missing-script",
                        "exit_code": 127,
                        "log": str(relative_missing),
                    }
                ]
                self.assertEqual(
                    MODULE.classify_validation_failure(outcomes, phase=phase),
                    expected,
                )
                self.assertEqual(outcomes[0]["classification"], "code_or_test")
            for phase, expected in (
                ("baseline", "baseline_validation"),
                ("coder-initial", "coder_validation"),
                ("validation-fix", "local_fix_validation"),
                ("supervisor", "supervisor_validation"),
            ):
                for code in (126, 127):
                    outcomes = [
                        {
                            "command": "application-test",
                            "exit_code": code,
                            "log": str(application),
                        }
                    ]
                    self.assertEqual(
                        MODULE.classify_validation_failure(outcomes, phase=phase),
                        expected,
                    )
                    self.assertEqual(
                        outcomes[0]["classification"], "code_or_test"
                    )

    def test_reviewer_host_preflight_fails_before_agents(self):
        settings = SimpleNamespace(codex_command="codex")
        with mock.patch.object(MODULE.shutil, "which", return_value=None):
            with self.assertRaisesRegex(MODULE.WorkflowError, "sandbox-exec"):
                MODULE.preflight_host(settings)

    def test_successful_reviewer_host_preflight_returns_auditable_checks(self):
        settings = MODULE.load_settings()
        help_text = " ".join(
            ["--json", "--output-schema", "--ephemeral", "--ignore-user-config", "--sandbox"]
        )

        def fake_run(command, **_kwargs):
            if command[-1] == "--version":
                output = "codex-cli 1.2.3\n"
            elif command[-1] == "--help":
                output = help_text
            else:
                output = ""
            return SimpleNamespace(returncode=0, stdout=output, stderr="")

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            with (
                mock.patch.object(MODULE.shutil, "which", return_value="/bin/tool"),
                mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run),
            ):
                result = MODULE.preflight_host(settings, project)
        self.assertEqual(result["stage"], "host")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["checks"]["reviewer_version"], "codex-cli 1.2.3")
        self.assertIn("--output-schema", result["checks"]["required_cli_capabilities"])
        self.assertTrue(result["checks"]["schema_readable"])
        self.assertTrue(result["checks"]["repository_read_probe"])

    def test_reviewer_host_preflight_rejects_broken_or_incompatible_binary(self):
        settings = SimpleNamespace(codex_command="codex-test")
        for script, message in (
            ("#!/bin/sh\nexit 3\n", "版本探测失败"),
            (
                "#!/bin/sh\n"
                "if [ \"$1\" = \"--version\" ]; then echo codex-test; exit 0; fi\n"
                "echo --json\nexit 0\n",
                "缺少所需 exec 能力",
            ),
        ):
            with tempfile.TemporaryDirectory() as directory:
                executable = Path(directory) / "codex-test"
                executable.write_text(script, encoding="utf-8")
                executable.chmod(0o755)

                def which(command):
                    return "/usr/bin/sandbox-exec" if command == "sandbox-exec" else str(executable)

                with mock.patch.object(MODULE.shutil, "which", side_effect=which):
                    with self.assertRaisesRegex(MODULE.WorkflowError, message):
                        MODULE.preflight_host(settings)

    def test_doctor_high_risk_does_not_require_or_query_ollama(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = Path(directory) / "plan.md"
            plan.write_text("Risk classification: high\n", encoding="utf-8")
            args = SimpleNamespace(plan=str(plan), model="primary")
            with (
                mock.patch.object(MODULE.shutil, "which", return_value="/tool"),
                mock.patch.object(MODULE, "ollama_models") as ollama_models,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                result = MODULE.command_doctor(args)
        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertFalse(payload["local_model_required"])
        self.assertNotIn("ollama", payload)
        ollama_models.assert_not_called()

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

    @unittest.skipIf(
        os.environ.get("MVP_VALIDATION_SANDBOX") == "1",
        "父级验证 Seatbelt 中不能可靠嵌套 sandbox-exec",
    )
    @unittest.skipUnless(sys.platform == "darwin", "requires macOS sandbox-exec")
    def test_validation_sandbox_cannot_mutate_evidence_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            victim = root / "victim.txt"
            victim.write_text("keep", encoding="utf-8")
            for writable in (False, True):
                with self.subTest(project_writable=writable):
                    run_dir = root / f"evidence-{writable}"
                    run_dir.mkdir()
                    plan = run_dir / "plan.md"
                    plan.write_text("approved", encoding="utf-8")
                    protected = [
                        run_dir / "review-1.json",
                        run_dir / "supervisor-takeover-last-message.txt",
                        run_dir / "summary.json",
                    ]
                    commands = [
                        f": > {str(plan)!r}",
                        *[
                            f"/bin/ln -s {str(victim)!r} {str(path)!r}"
                            for path in protected
                        ],
                    ]
                    outcomes = MODULE.run_validations(
                        project,
                        commands,
                        run_dir,
                        "malicious",
                        project_writable=writable,
                    )
                    self.assertTrue(
                        all(item["exit_code"] != 0 for item in outcomes)
                    )
                    self.assertEqual(plan.read_text(encoding="utf-8"), "approved")
                    self.assertTrue(all(not path.exists() for path in protected))
                    self.assertEqual(victim.read_text(encoding="utf-8"), "keep")

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
    @mock.patch.object(MODULE, "preflight_host")
    def test_first_p1_skips_second_pre_takeover_review(
        self,
        preflight_host,
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
            passed,
        ]
        run_validations.return_value = [{"command": "test", "exit_code": 0}]
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            plan = project / "plan.md"
            plan.write_text("# plan\nRisk classification: low\n", encoding="utf-8")
            self.attach_validation_logs(project / "mock-validation.log", run_validations.return_value)
            (project / ".mvp-ai.toml").write_text(
                '[validation]\ncommands = ["test"]\n', encoding="utf-8"
            )
            args = SimpleNamespace(
                project=str(project),
                plan=str(plan),
                model="primary",
                allow_dirty=False,
            )

            def update_validation_config(*_args, **_kwargs):
                (project / ".mvp-ai.toml").write_text(
                    '[validation]\ncommands = ["post-edit-test"]\n',
                    encoding="utf-8",
                )

            call_local_coder.side_effect = update_validation_config
            with mock.patch.object(MODULE, "make_run_dir", return_value=project):
                (project / "docs.log").write_text("docs ok", encoding="utf-8")
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
        self.assertEqual(call_local_coder.call_count, 1)
        self.assertEqual(call_reviewer.call_count, 2)
        self.assertEqual(call_supervisor.call_count, 1)
        self.assertEqual(run_validations.call_args_list[0].args[1], ["test"])
        self.assertTrue(
            all(
                call.args[1] == ["post-edit-test"]
                for call in run_validations.call_args_list[1:]
            )
        )
        self.assertEqual(
            [call.args[-1] for call in call_reviewer.call_args_list], [1, 2]
        )
        write_summary.assert_called_once()
        self.assertEqual(
            write_summary.call_args.kwargs["status"], "ready_for_user_review"
        )
        self.assertEqual(write_summary.call_args.kwargs["workflow"]["cloud_calls"], 3)

    def test_baseline_failure_starts_no_local_or_cloud_agent(self):
        failed = [{"command": "test", "exit_code": 1, "log": "/missing"}]
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            plan = project / "approved.md"
            plan.write_text("   Risk classification: low\n", encoding="utf-8")
            (project / ".mvp-ai.toml").write_text(
                '[validation]\ncommands = ["test"]\n', encoding="utf-8"
            )
            args = SimpleNamespace(
                project=str(project), plan=str(plan), model="primary", allow_dirty=False
            )
            with (
                mock.patch.object(MODULE, "validate_project"),
                mock.patch.object(MODULE, "preflight_host"),
                mock.patch.object(MODULE, "ensure_model_available"),
                mock.patch.object(MODULE, "make_run_dir", return_value=project),
                mock.patch.object(MODULE, "run_validations", return_value=failed),
                mock.patch.object(MODULE, "call_local_coder") as local,
                mock.patch.object(MODULE, "call_reviewer") as reviewer,
                mock.patch.object(MODULE, "call_supervisor") as supervisor,
                mock.patch.object(MODULE, "write_failure_summary") as failure_summary,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = MODULE.command_run(args)
        self.assertEqual(result, 2)
        local.assert_not_called()
        reviewer.assert_not_called()
        supervisor.assert_not_called()
        self.assertEqual(
            failure_summary.call_args.kwargs["workflow"]["cloud_calls"], 0
        )

    def test_project_change_during_baseline_preserves_user_file_and_starts_no_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            (project / ".mvp-ai.toml").write_text(
                '[validation]\ncommands = ["test"]\n', encoding="utf-8"
            )
            (project / "app.py").write_text("print('clean')\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=project, check=True)
            subprocess.run(
                [
                    "git", "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "base",
                ],
                cwd=project,
                check=True,
            )
            plan = root / "approved.md"
            plan.write_text("Risk classification: low\n", encoding="utf-8")
            log = run_dir / "baseline.log"
            log.write_text("ok\n", encoding="utf-8")
            valid = [{"command": "test", "exit_code": 0, "log": str(log)}]

            def mutate_real_project(*_args, **_kwargs):
                (project / "user-note.txt").write_text(
                    "do not overwrite\n", encoding="utf-8"
                )
                return valid

            args = SimpleNamespace(
                project=str(project), plan=str(plan), model="primary",
                allow_dirty=False, live=False,
            )
            with (
                mock.patch.object(MODULE, "preflight_host"),
                mock.patch.object(MODULE, "ensure_model_available"),
                mock.patch.object(MODULE, "make_run_dir", return_value=run_dir),
                mock.patch.object(
                    MODULE, "run_validations", side_effect=mutate_real_project
                ),
                mock.patch.object(MODULE, "call_local_coder") as local,
                mock.patch.object(MODULE, "call_reviewer") as reviewer,
                mock.patch.object(MODULE, "call_supervisor") as supervisor,
                mock.patch.object(MODULE, "write_failure_summary") as failure,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = MODULE.command_run(args)

            self.assertEqual(result, 2)
            self.assertEqual(
                (project / "user-note.txt").read_text(encoding="utf-8"),
                "do not overwrite\n",
            )
            local.assert_not_called()
            reviewer.assert_not_called()
            supervisor.assert_not_called()
            workflow = failure.call_args.kwargs["workflow"]
            self.assertEqual(workflow["cloud_calls"], 0)
            self.assertEqual(
                workflow["failure_classification"],
                "project_changed_during_preflight",
            )

    def test_project_change_immediately_before_first_model_call_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            run_dir = root / "run"
            project.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            (project / ".mvp-ai.toml").write_text(
                '[validation]\ncommands = ["test"]\n', encoding="utf-8"
            )
            subprocess.run(["git", "add", "."], cwd=project, check=True)
            subprocess.run(
                [
                    "git", "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "base",
                ],
                cwd=project,
                check=True,
            )
            plan = root / "approved.md"
            plan.write_text("Risk classification: low\n", encoding="utf-8")
            log = run_dir / "baseline.log"
            log.write_text("ok\n", encoding="utf-8")
            valid = [{"command": "test", "exit_code": 0, "log": str(log)}]
            real_render = MODULE.render_prompt

            def mutate_before_coder(name, **values):
                prompt = real_render(name, **values)
                if name == "coder.md":
                    (project / "late-user-note.txt").write_text(
                        "preserve me\n", encoding="utf-8"
                    )
                return prompt

            args = SimpleNamespace(
                project=str(project), plan=str(plan), model="primary",
                allow_dirty=False, live=False,
            )
            with (
                mock.patch.object(MODULE, "preflight_host"),
                mock.patch.object(MODULE, "ensure_model_available"),
                mock.patch.object(MODULE, "make_run_dir", return_value=run_dir),
                mock.patch.object(MODULE, "run_validations", return_value=valid),
                mock.patch.object(
                    MODULE, "render_prompt", side_effect=mutate_before_coder
                ),
                mock.patch.object(MODULE, "call_local_coder") as local,
                mock.patch.object(MODULE, "call_reviewer") as reviewer,
                mock.patch.object(MODULE, "call_supervisor") as supervisor,
                mock.patch.object(MODULE, "write_failure_summary") as failure,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = MODULE.command_run(args)
            self.assertEqual(result, 2)
            local.assert_not_called()
            reviewer.assert_not_called()
            supervisor.assert_not_called()
            self.assertEqual(
                (project / "late-user-note.txt").read_text(encoding="utf-8"),
                "preserve me\n",
            )
            self.assertEqual(
                failure.call_args.kwargs["workflow"]["failure_classification"],
                "project_changed_during_preflight",
            )
            self.assertEqual(failure.call_args.kwargs["workflow"]["cloud_calls"], 0)

    def test_mixed_environment_failure_stops_before_cloud_agents(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            plan = project / "approved.md"
            plan.write_text("Risk classification: low\n", encoding="utf-8")
            (project / ".mvp-ai.toml").write_text(
                '[validation]\ncommands = ["test"]\n', encoding="utf-8"
            )
            baseline_log = project / "baseline.log"
            baseline_log.write_text("ok", encoding="utf-8")
            environment_log = project / "environment.log"
            environment_log.write_text(
                "xcrun: error: SDK not found", encoding="utf-8"
            )
            test_log = project / "test.log"
            test_log.write_text("assertion failed", encoding="utf-8")
            docs_log = project / "docs.log"
            docs_log.write_text("ok", encoding="utf-8")
            baseline = [
                {"command": "test", "exit_code": 0, "log": str(baseline_log)}
            ]
            mixed = [
                {"command": "build", "exit_code": 1, "log": str(environment_log)},
                {"command": "test", "exit_code": 1, "log": str(test_log)},
            ]
            docs = {
                "command": "documentation-contract",
                "exit_code": 0,
                "log": str(docs_log),
            }
            args = SimpleNamespace(
                project=str(project), plan=str(plan), model="primary",
                allow_dirty=False, live=False,
            )
            with (
                mock.patch.object(MODULE, "validate_project"),
                mock.patch.object(MODULE, "preflight_host"),
                mock.patch.object(MODULE, "ensure_model_available"),
                mock.patch.object(MODULE, "make_run_dir", return_value=project),
                mock.patch.object(
                    MODULE, "run_validations", side_effect=[baseline, mixed]
                ),
                mock.patch.object(
                    MODULE, "validate_development_documents", return_value=docs
                ),
                mock.patch.object(MODULE, "call_local_coder", return_value=None) as local,
                mock.patch.object(MODULE, "call_reviewer") as reviewer,
                mock.patch.object(MODULE, "call_supervisor") as supervisor,
                mock.patch.object(MODULE, "write_failure_summary") as failure,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = MODULE.command_run(args)
        self.assertEqual(result, 2)
        self.assertEqual(local.call_count, 1)
        reviewer.assert_not_called()
        supervisor.assert_not_called()
        self.assertEqual(
            failure.call_args.kwargs["workflow"]["failure_classification"],
            "environment_validation",
        )

    def test_local_p2_fix_path_uses_two_reviews_without_takeover(self):
        p2 = {
            "verdict": "fail",
            "summary": "local",
            "findings": [
                {
                    "severity": "P2",
                    "category": "correctness",
                    "file": "src/a.py",
                    "required_fix": "修复边界",
                }
            ],
            "tests": [],
        }
        passed = {"verdict": "pass", "summary": "ok", "findings": [], "tests": []}
        valid = [{"command": "test", "exit_code": 0}]
        docs = {"command": "documentation-contract", "exit_code": 0}
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            plan = project / "approved.md"
            plan.write_text("   Risk classification: low\n", encoding="utf-8")
            self.attach_validation_logs(project / "mock-validation.log", valid, docs)
            (project / ".mvp-ai.toml").write_text(
                '[validation]\ncommands = ["test"]\n', encoding="utf-8"
            )
            args = SimpleNamespace(
                project=str(project), plan=str(plan), model="primary", allow_dirty=False
            )
            with (
                mock.patch.object(MODULE, "validate_project"),
                mock.patch.object(MODULE, "preflight_host"),
                mock.patch.object(MODULE, "ensure_model_available"),
                mock.patch.object(MODULE, "make_run_dir", return_value=project),
                mock.patch.object(MODULE, "run_validations", return_value=valid),
                mock.patch.object(
                    MODULE, "validate_development_documents", return_value=docs
                ),
                mock.patch.object(MODULE, "call_local_coder", return_value=None) as local,
                mock.patch.object(
                    MODULE, "call_reviewer", side_effect=[p2, passed]
                ) as reviewer,
                mock.patch.object(MODULE, "call_supervisor") as supervisor,
                mock.patch.object(MODULE, "write_summary") as summary,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = MODULE.command_run(args)
        self.assertEqual(result, 0)
        self.assertEqual(local.call_count, 2)
        self.assertEqual(reviewer.call_count, 2)
        supervisor.assert_not_called()
        self.assertEqual(summary.call_args.kwargs["workflow"]["cloud_calls"], 2)

    def test_fixer_evidence_redaction_failure_stops_without_supervisor(self):
        p2 = {
            "verdict": "fail", "summary": "local",
            "findings": [{
                "severity": "P2", "category": "correctness",
                "file": "src/a.py", "required_fix": "fix",
            }],
            "tests": [],
        }
        for stage in ("validation-fix", "local-fix"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                subprocess.run(["git", "init", "-q"], cwd=project, check=True)
                plan = project / "approved.md"
                plan.write_text("Risk classification: low\n", encoding="utf-8")
                (project / ".mvp-ai.toml").write_text(
                    '[validation]\ncommands = ["test"]\n', encoding="utf-8"
                )
                valid = [{"command": "test", "exit_code": 0}]
                failed = [{"command": "test", "exit_code": 1}]
                docs = {"command": "documentation-contract", "exit_code": 0}
                self.attach_validation_logs(
                    project / "mock-validation.log", valid, failed, docs
                )
                validations = (
                    [valid, failed] if stage == "validation-fix" else valid
                )
                args = SimpleNamespace(
                    project=str(project), plan=str(plan), model="primary",
                    allow_dirty=False, live=False,
                )
                with (
                    mock.patch.object(MODULE, "validate_project"),
                    mock.patch.object(MODULE, "preflight_host"),
                    mock.patch.object(MODULE, "ensure_model_available"),
                    mock.patch.object(MODULE, "make_run_dir", return_value=project),
                    mock.patch.object(
                        MODULE, "run_validations", side_effect=validations
                        if isinstance(validations, list) and validations and isinstance(validations[0], list)
                        else None,
                        return_value=valid if stage == "local-fix" else mock.DEFAULT,
                    ),
                    mock.patch.object(
                        MODULE, "validate_development_documents", return_value=docs
                    ),
                    mock.patch.object(
                        MODULE, "call_local_coder",
                        side_effect=[
                            None,
                            MODULE.EvidenceRedactionError("cannot redact fixer evidence"),
                        ],
                    ),
                    mock.patch.object(
                        MODULE, "call_reviewer",
                        return_value=p2,
                    ) as reviewer,
                    mock.patch.object(MODULE, "call_supervisor") as supervisor,
                    mock.patch.object(MODULE, "write_failure_summary") as failure,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    result = MODULE.command_run(args)
                self.assertEqual(result, 2)
                supervisor.assert_not_called()
                self.assertEqual(
                    reviewer.call_count, 0 if stage == "validation-fix" else 1
                )
                workflow = failure.call_args.kwargs["workflow"]
                self.assertEqual(
                    workflow["failure_classification"], "evidence_redaction_failure"
                )
                self.assertEqual(
                    workflow["cloud_calls"], 0 if stage == "validation-fix" else 1
                )

    def test_tracked_agent_reads_exact_usage_from_redacted_persisted_log(self):
        passed = {"verdict": "pass", "summary": "ok", "findings": [], "tests": []}
        valid = [{"command": "test", "exit_code": 0}]
        docs = {"command": "documentation-contract", "exit_code": 0}
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            plan = project / "approved.md"
            plan.write_text("Risk classification: low\n", encoding="utf-8")
            self.attach_validation_logs(project / "mock-validation.log", valid, docs)
            (project / ".mvp-ai.toml").write_text(
                '[validation]\ncommands = ["test"]\n', encoding="utf-8"
            )
            args = SimpleNamespace(
                project=str(project), plan=str(plan), model="primary", allow_dirty=False
            )

            def local_with_usage(*args, **kwargs):
                event = json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 40, "output_tokens": 2},
                    }
                )
                log_path = project / "coder-initial.log"
                return MODULE.run_command(
                    [sys.executable, "-c", f"print({event!r})", "--json"],
                    cwd=project,
                    timeout=10,
                    stdin_text=None,
                    log_path=log_path,
                )

            with (
                mock.patch.object(MODULE, "validate_project"),
                mock.patch.object(MODULE, "preflight_host"),
                mock.patch.object(MODULE, "ensure_model_available"),
                mock.patch.object(MODULE, "make_run_dir", return_value=project),
                mock.patch.object(MODULE, "run_validations", return_value=valid),
                mock.patch.object(
                    MODULE, "validate_development_documents", return_value=docs
                ),
                mock.patch.object(
                    MODULE, "call_local_coder", side_effect=local_with_usage
                ),
                mock.patch.object(MODULE, "call_reviewer", return_value=passed),
                mock.patch.object(MODULE, "write_summary") as summary,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = MODULE.command_run(args)
        self.assertEqual(result, 0)
        usage = summary.call_args.kwargs["usage_stages"][0]
        self.assertEqual(usage["measurement"], "exact")
        self.assertEqual(usage["total_tokens"], 42)

    def test_high_risk_plan_routes_directly_to_supervisor(self):
        passed = {"verdict": "pass", "summary": "ok", "findings": [], "tests": []}
        valid = [{"command": "test", "exit_code": 0}]
        docs = {"command": "documentation-contract", "exit_code": 0}
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            plan = project / "approved.md"
            plan.write_text("Risk classification: high\n", encoding="utf-8")
            self.attach_validation_logs(project / "mock-validation.log", valid, docs)
            (project / ".mvp-ai.toml").write_text(
                '[validation]\ncommands = ["test"]\n', encoding="utf-8"
            )
            args = SimpleNamespace(
                project=str(project), plan=str(plan), model="primary", allow_dirty=False
            )
            with (
                mock.patch.object(MODULE, "validate_project"),
                mock.patch.object(MODULE, "preflight_host"),
                mock.patch.object(MODULE, "ensure_model_available") as ensure_model,
                mock.patch.object(MODULE, "make_run_dir", return_value=project),
                mock.patch.object(MODULE, "run_validations", return_value=valid),
                mock.patch.object(
                    MODULE, "validate_development_documents", return_value=docs
                ),
                mock.patch.object(MODULE, "call_local_coder") as local,
                mock.patch.object(MODULE, "call_supervisor", return_value=None) as supervisor,
                mock.patch.object(MODULE, "call_reviewer", return_value=passed) as reviewer,
                mock.patch.object(MODULE, "write_summary") as summary,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = MODULE.command_run(args)
            supervisor_evidence = json.loads(
                (project / "context-supervisor-validation.json").read_text(
                    encoding="utf-8"
                )
            )
            baseline_artifact = supervisor_evidence["artifacts"][0]
            baseline_hash_matches = baseline_artifact["log_sha256"] == (
                MODULE.file_sha256(Path(baseline_artifact["log_path"]))
            )
        self.assertEqual(result, 0)
        ensure_model.assert_not_called()
        local.assert_not_called()
        supervisor.assert_called_once()
        reviewer.assert_called_once()
        self.assertEqual(
            summary.call_args.kwargs["workflow"]["takeover"]["reason"],
            "risk_high_direct_cloud",
        )
        preflight = summary.call_args.kwargs["preflight"]
        self.assertEqual(preflight[0]["stage"], "host")
        self.assertEqual(preflight[0]["status"], "passed")
        self.assertEqual(preflight[1]["stage"], "baseline")
        self.assertEqual(baseline_artifact["command"], "test")
        self.assertTrue(baseline_hash_matches)

    def test_capsule_failure_does_not_count_or_start_cloud_review(self):
        passed_validation = [{"command": "test", "exit_code": 0}]
        docs = {"command": "documentation-contract", "exit_code": 0}
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            plan = project / "approved.md"
            plan.write_text("Risk classification: low\n", encoding="utf-8")
            self.attach_validation_logs(
                project / "mock-validation.log", passed_validation, docs
            )
            (project / ".mvp-ai.toml").write_text(
                '[validation]\ncommands = ["test"]\n', encoding="utf-8"
            )
            args = SimpleNamespace(
                project=str(project), plan=str(plan), model="primary",
                allow_dirty=False, live=False,
            )
            with (
                mock.patch.object(MODULE, "validate_project"),
                mock.patch.object(MODULE, "preflight_host"),
                mock.patch.object(MODULE, "ensure_model_available"),
                mock.patch.object(MODULE, "make_run_dir", return_value=project),
                mock.patch.object(
                    MODULE, "run_validations", return_value=passed_validation
                ),
                mock.patch.object(
                    MODULE, "validate_development_documents", return_value=docs
                ),
                mock.patch.object(MODULE, "call_local_coder", return_value=None),
                mock.patch.object(MODULE, "call_reviewer") as reviewer,
                mock.patch.object(
                    MODULE, "write_context_capsule",
                    side_effect=MODULE.WorkflowError("capsule broken"),
                ),
                mock.patch.object(MODULE, "write_failure_summary") as failure,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(MODULE.WorkflowError, "capsule broken"):
                    MODULE.command_run(args)
        reviewer.assert_not_called()
        workflow = failure.call_args.kwargs["workflow"]
        self.assertEqual(workflow["cloud_calls"], 0)
        self.assertEqual(
            workflow["failure_classification"], "context_capsule_failure"
        )
        self.assertFalse(
            any(
                item["backend"].startswith("cloud")
                for item in failure.call_args.kwargs["usage_stages"]
            )
        )

    def test_reviewer_prompt_failure_does_not_count_or_start_cloud_review(self):
        original_render_prompt = MODULE.render_prompt
        valid = [{"command": "test", "exit_code": 0}]
        docs = {"command": "documentation-contract", "exit_code": 0}
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            plan = project / "approved.md"
            plan.write_text("Risk classification: low\n", encoding="utf-8")
            self.attach_validation_logs(
                project / "mock-validation.log", valid, docs
            )
            (project / ".mvp-ai.toml").write_text(
                '[validation]\ncommands = ["test"]\n', encoding="utf-8"
            )
            args = SimpleNamespace(
                project=str(project), plan=str(plan), model="primary",
                allow_dirty=False, live=False,
            )

            def fail_reviewer_prompt(name, **values):
                if name == "reviewer.md":
                    raise MODULE.WorkflowError("reviewer prompt broken")
                return original_render_prompt(name, **values)

            with (
                mock.patch.object(MODULE, "validate_project"),
                mock.patch.object(MODULE, "preflight_host"),
                mock.patch.object(MODULE, "ensure_model_available"),
                mock.patch.object(MODULE, "make_run_dir", return_value=project),
                mock.patch.object(MODULE, "run_validations", return_value=valid),
                mock.patch.object(
                    MODULE, "validate_development_documents", return_value=docs
                ),
                mock.patch.object(MODULE, "render_prompt", side_effect=fail_reviewer_prompt),
                mock.patch.object(MODULE, "call_local_coder", return_value=None) as local,
                mock.patch.object(MODULE, "call_reviewer") as reviewer,
                mock.patch.object(MODULE, "write_failure_summary") as failure,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(MODULE.WorkflowError, "reviewer prompt broken"):
                    MODULE.command_run(args)
        local.assert_called_once()
        reviewer.assert_not_called()
        workflow = failure.call_args.kwargs["workflow"]
        self.assertEqual(workflow["cloud_calls"], 0)
        self.assertEqual(
            workflow["failure_classification"], "reviewer_prompt_failure"
        )
        self.assertEqual(len(failure.call_args.kwargs["usage_stages"]), 1)

    def test_supervisor_preparation_failures_write_summary_without_cloud_call(self):
        original_render_prompt = MODULE.render_prompt
        for failure_kind, expected_classification in (
            ("capsule", "context_capsule_failure"),
            ("prompt", "supervisor_prompt_failure"),
        ):
            with self.subTest(failure_kind=failure_kind), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                subprocess.run(["git", "init", "-q"], cwd=project, check=True)
                plan = project / "approved.md"
                plan.write_text("Risk classification: high\n", encoding="utf-8")
                valid = [{"command": "test", "exit_code": 0}]
                docs = {"command": "documentation-contract", "exit_code": 0}
                self.attach_validation_logs(
                    project / "mock-validation.log", valid, docs
                )
                (project / ".mvp-ai.toml").write_text(
                    '[validation]\ncommands = ["test"]\n', encoding="utf-8"
                )
                args = SimpleNamespace(
                    project=str(project), plan=str(plan), model="primary",
                    allow_dirty=False, live=False,
                )

                def render_with_supervisor_failure(name, **values):
                    if name == "supervisor.md":
                        raise MODULE.WorkflowError("supervisor prompt broken")
                    return original_render_prompt(name, **values)

                capsule_patch = (
                    mock.patch.object(
                        MODULE, "write_context_capsule",
                        side_effect=MODULE.WorkflowError("capsule broken"),
                    )
                    if failure_kind == "capsule"
                    else contextlib.nullcontext()
                )
                prompt_patch = (
                    mock.patch.object(
                        MODULE, "render_prompt",
                        side_effect=render_with_supervisor_failure,
                    )
                    if failure_kind == "prompt"
                    else contextlib.nullcontext()
                )
                with (
                    mock.patch.object(MODULE, "validate_project"),
                    mock.patch.object(MODULE, "preflight_host"),
                    mock.patch.object(MODULE, "make_run_dir", return_value=project),
                    mock.patch.object(MODULE, "run_validations", return_value=valid),
                    mock.patch.object(
                        MODULE, "validate_development_documents", return_value=docs
                    ),
                    mock.patch.object(MODULE, "call_supervisor") as supervisor,
                    mock.patch.object(MODULE, "write_failure_summary") as failure,
                    capsule_patch,
                    prompt_patch,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    with self.assertRaises(MODULE.WorkflowError):
                        MODULE.command_run(args)
                supervisor.assert_not_called()
                workflow = failure.call_args.kwargs["workflow"]
                self.assertEqual(workflow["cloud_calls"], 0)
                self.assertEqual(
                    workflow["failure_classification"], expected_classification
                )
                self.assertFalse(
                    any(
                        item["backend"].startswith("cloud")
                        for item in failure.call_args.kwargs["usage_stages"]
                    )
                )

    def test_local_prompt_failures_do_not_create_phantom_agent_stages(self):
        original_render_prompt = MODULE.render_prompt
        p2 = {
            "verdict": "fail", "summary": "fix locally",
            "findings": [{"severity": "P2", "category": "correctness", "file": "src/a.py", "required_fix": "fix"}],
            "tests": [],
        }
        scenarios = (
            ("coder", "coder.md", "coder_prompt_failure", 0, 0, 0),
            ("validation-fixer", "fixer.md", "validation_fixer_prompt_failure", 1, 0, 1),
            ("review-fixer", "fixer.md", "review_fixer_prompt_failure", 1, 1, 2),
        )
        for scenario, failing_template, classification, local_calls, review_calls, stages in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                subprocess.run(["git", "init", "-q"], cwd=project, check=True)
                plan = project / "approved.md"
                plan.write_text("Risk classification: low\n", encoding="utf-8")
                valid = [{"command": "test", "exit_code": 0}]
                failed = [{"command": "test", "exit_code": 1}]
                docs = {"command": "documentation-contract", "exit_code": 0}
                self.attach_validation_logs(
                    project / "mock-validation.log", valid, failed, docs
                )
                (project / ".mvp-ai.toml").write_text(
                    '[validation]\ncommands = ["test"]\n', encoding="utf-8"
                )
                args = SimpleNamespace(
                    project=str(project), plan=str(plan), model="primary",
                    allow_dirty=False, live=False,
                )

                def fail_selected_prompt(name, **values):
                    if name == failing_template:
                        if scenario == "coder" or name == "fixer.md":
                            raise MODULE.WorkflowError("prompt unavailable")
                    return original_render_prompt(name, **values)

                validation_result = (
                    [valid, failed]
                    if scenario == "validation-fixer"
                    else valid
                )
                reviewer_result = p2 if scenario == "review-fixer" else {
                    "verdict": "pass", "summary": "ok", "findings": [], "tests": []
                }
                with (
                    mock.patch.object(MODULE, "validate_project"),
                    mock.patch.object(MODULE, "preflight_host"),
                    mock.patch.object(MODULE, "ensure_model_available"),
                    mock.patch.object(MODULE, "make_run_dir", return_value=project),
                    mock.patch.object(
                        MODULE, "run_validations",
                        side_effect=validation_result if isinstance(validation_result, list) and validation_result and isinstance(validation_result[0], list) else None,
                        return_value=valid,
                    ),
                    mock.patch.object(
                        MODULE, "validate_development_documents", return_value=docs
                    ),
                    mock.patch.object(MODULE, "render_prompt", side_effect=fail_selected_prompt),
                    mock.patch.object(MODULE, "call_local_coder", return_value=None) as local,
                    mock.patch.object(MODULE, "call_reviewer", return_value=reviewer_result) as reviewer,
                    mock.patch.object(MODULE, "call_supervisor") as supervisor,
                    mock.patch.object(MODULE, "write_failure_summary") as failure,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    with self.assertRaisesRegex(MODULE.WorkflowError, "prompt unavailable"):
                        MODULE.command_run(args)
                self.assertEqual(local.call_count, local_calls)
                self.assertEqual(reviewer.call_count, review_calls)
                supervisor.assert_not_called()
                workflow = failure.call_args.kwargs["workflow"]
                self.assertEqual(workflow["failure_classification"], classification)
                self.assertEqual(len(failure.call_args.kwargs["usage_stages"]), stages)

    def test_ambiguous_risk_plans_route_directly_to_supervisor(self):
        passed = {"verdict": "pass", "summary": "ok", "findings": [], "tests": []}
        valid = [{"command": "test", "exit_code": 0}]
        docs = {"command": "documentation-contract", "exit_code": 0}
        for plan_text in (
            "Risk classification: low|medium|high\n",
            "Risk classification: low\nRisk classification: high\n",
            "```text\nRisk classification: low\n```\n",
            "<!--\nRisk classification: low\n-->\n",
            "<!--\nRisk classification: low\n",
            "<script>\nRisk classification: low\n</script>\n",
            "<pre>\nRisk classification: low\n</pre>\n",
            "<style>\nRisk classification: low\n",
            "<textarea>\nRisk classification: low\n</textarea>\n",
            "<template>\nRisk classification: low\n</template>\n",
            "<div hidden>\nRisk classification: low\n</div>\n",
            "<![CDATA[\nRisk classification: low\n]]>\n",
        ):
            with self.subTest(plan=plan_text):
                with tempfile.TemporaryDirectory() as directory:
                    project = Path(directory)
                    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
                    plan = project / "approved.md"
                    plan.write_text(plan_text, encoding="utf-8")
                    self.attach_validation_logs(project / "mock-validation.log", valid, docs)
                    (project / ".mvp-ai.toml").write_text(
                        '[validation]\ncommands = ["test"]\n', encoding="utf-8"
                    )
                    args = SimpleNamespace(
                        project=str(project),
                        plan=str(plan),
                        model="primary",
                        allow_dirty=False,
                    )
                    with (
                        mock.patch.object(MODULE, "validate_project"),
                        mock.patch.object(MODULE, "preflight_host"),
                        mock.patch.object(
                            MODULE, "make_run_dir", return_value=project
                        ),
                        mock.patch.object(
                            MODULE, "run_validations", return_value=valid
                        ),
                        mock.patch.object(
                            MODULE,
                            "validate_development_documents",
                            return_value=docs,
                        ),
                        mock.patch.object(MODULE, "call_local_coder") as local,
                        mock.patch.object(
                            MODULE, "call_supervisor", return_value=None
                        ) as supervisor,
                        mock.patch.object(
                            MODULE, "call_reviewer", return_value=passed
                        ),
                        mock.patch.object(MODULE, "write_summary"),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        result = MODULE.command_run(args)
                self.assertEqual(result, 0)
                local.assert_not_called()
                supervisor.assert_called_once()

    def test_empty_fail_final_review_never_becomes_ready(self):
        failed = {"verdict": "fail", "summary": "ambiguous", "findings": [], "tests": []}
        valid = [{"command": "test", "exit_code": 0}]
        docs = {"command": "documentation-contract", "exit_code": 0}
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            plan = project / "approved.md"
            plan.write_text("Risk classification: high\n", encoding="utf-8")
            self.attach_validation_logs(project / "mock-validation.log", valid, docs)
            (project / ".mvp-ai.toml").write_text(
                '[validation]\ncommands = ["test"]\n', encoding="utf-8"
            )
            args = SimpleNamespace(
                project=str(project), plan=str(plan), model="primary", allow_dirty=False
            )
            with (
                mock.patch.object(MODULE, "validate_project"),
                mock.patch.object(MODULE, "preflight_host"),
                mock.patch.object(MODULE, "make_run_dir", return_value=project),
                mock.patch.object(MODULE, "run_validations", return_value=valid),
                mock.patch.object(
                    MODULE, "validate_development_documents", return_value=docs
                ),
                mock.patch.object(MODULE, "call_supervisor", return_value=None),
                mock.patch.object(MODULE, "call_reviewer", return_value=failed),
                mock.patch.object(MODULE, "write_failure_summary") as failure,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(MODULE.StructuredResultError):
                    MODULE.command_run(args)
        self.assertEqual(
            failure.call_args.kwargs["workflow"]["failure_classification"],
            "reviewer_result_invalid",
        )

    def test_final_review_findings_are_classified_in_summary(self):
        failed = {
            "verdict": "fail",
            "summary": "edge remains",
            "findings": [
                {
                    "severity": "P2",
                    "category": "correctness",
                    "title": "edge",
                    "file": "src/app.py",
                    "line": 1,
                    "description": "edge remains",
                    "required_fix": "fix edge",
                }
            ],
            "tests": [],
        }
        valid = [{"command": "test", "exit_code": 0}]
        docs = {"command": "documentation-contract", "exit_code": 0}
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            plan = project / "approved.md"
            plan.write_text("Risk classification: high\n", encoding="utf-8")
            self.attach_validation_logs(project / "mock-validation.log", valid, docs)
            (project / ".mvp-ai.toml").write_text(
                '[validation]\ncommands = ["test"]\n', encoding="utf-8"
            )
            args = SimpleNamespace(
                project=str(project), plan=str(plan), model="primary", allow_dirty=False
            )
            with (
                mock.patch.object(MODULE, "validate_project"),
                mock.patch.object(MODULE, "preflight_host"),
                mock.patch.object(MODULE, "make_run_dir", return_value=project),
                mock.patch.object(MODULE, "run_validations", return_value=valid),
                mock.patch.object(
                    MODULE, "validate_development_documents", return_value=docs
                ),
                mock.patch.object(MODULE, "call_supervisor", return_value=None),
                mock.patch.object(MODULE, "call_reviewer", return_value=failed),
                mock.patch.object(MODULE, "write_summary") as summary,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = MODULE.command_run(args)
        self.assertEqual(result, 2)
        self.assertEqual(
            summary.call_args.kwargs["workflow"]["failure_classification"],
            "final_review_findings",
        )

    def test_cloud_call_limit_rejected_before_any_agent_when_route_cannot_finish(self):
        base_settings = MODULE.load_settings()
        for risk, limit in (("high", 1), ("low", 2)):
            with self.subTest(risk=risk, limit=limit):
                with tempfile.TemporaryDirectory() as directory:
                    project = Path(directory)
                    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
                    plan = project / "approved.md"
                    plan.write_text(
                        f"Risk classification: {risk}\n", encoding="utf-8"
                    )
                    (project / ".mvp-ai.toml").write_text(
                        '[validation]\ncommands = ["test"]\n', encoding="utf-8"
                    )
                    args = SimpleNamespace(
                        project=str(project),
                        plan=str(plan),
                        model="primary",
                        allow_dirty=False,
                    )
                    with (
                        mock.patch.object(
                            MODULE,
                            "load_settings",
                            return_value=replace(
                                base_settings, cloud_max_calls_per_run=limit
                            ),
                        ),
                        mock.patch.object(MODULE, "validate_project"),
                        mock.patch.object(
                            MODULE, "make_run_dir", return_value=project
                        ),
                        mock.patch.object(MODULE, "preflight_host") as preflight,
                        mock.patch.object(MODULE, "call_local_coder") as local,
                        mock.patch.object(MODULE, "call_supervisor") as supervisor,
                        mock.patch.object(MODULE, "call_reviewer") as reviewer,
                        mock.patch.object(
                            MODULE, "write_failure_summary"
                        ) as failure,
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        result = MODULE.command_run(args)
                self.assertEqual(result, 2)
                preflight.assert_not_called()
                local.assert_not_called()
                supervisor.assert_not_called()
                reviewer.assert_not_called()
                self.assertEqual(
                    failure.call_args.kwargs["workflow"]["failure_classification"],
                    "cloud_call_limit_configuration",
                )

    def test_three_call_limit_reserves_takeover_and_final_review(self):
        base_settings = MODULE.load_settings()
        p2 = {
            "verdict": "fail",
            "summary": "localized",
            "findings": [
                {
                    "severity": "P2",
                    "category": "correctness",
                    "file": "src/a.py",
                    "required_fix": "fix edge",
                }
            ],
            "tests": [],
        }
        passed = {"verdict": "pass", "summary": "ok", "findings": [], "tests": []}
        valid = [{"command": "test", "exit_code": 0}]
        docs = {"command": "documentation-contract", "exit_code": 0}
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            plan = project / "approved.md"
            plan.write_text("Risk classification: low\n", encoding="utf-8")
            self.attach_validation_logs(project / "mock-validation.log", valid, docs)
            (project / ".mvp-ai.toml").write_text(
                '[validation]\ncommands = ["test"]\n', encoding="utf-8"
            )
            args = SimpleNamespace(
                project=str(project), plan=str(plan), model="primary", allow_dirty=False
            )
            with (
                mock.patch.object(
                    MODULE,
                    "load_settings",
                    return_value=replace(
                        base_settings, cloud_max_calls_per_run=3
                    ),
                ),
                mock.patch.object(MODULE, "validate_project"),
                mock.patch.object(MODULE, "preflight_host"),
                mock.patch.object(MODULE, "ensure_model_available"),
                mock.patch.object(MODULE, "make_run_dir", return_value=project),
                mock.patch.object(MODULE, "run_validations", return_value=valid),
                mock.patch.object(
                    MODULE, "validate_development_documents", return_value=docs
                ),
                mock.patch.object(MODULE, "call_local_coder", return_value=None) as local,
                mock.patch.object(MODULE, "call_supervisor", return_value=None) as supervisor,
                mock.patch.object(
                    MODULE, "call_reviewer", side_effect=[p2, passed]
                ) as reviewer,
                mock.patch.object(MODULE, "write_summary") as summary,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = MODULE.command_run(args)
        self.assertEqual(result, 0)
        self.assertEqual(local.call_count, 1)
        self.assertEqual(reviewer.call_count, 2)
        supervisor.assert_called_once()
        self.assertEqual(summary.call_args.kwargs["workflow"]["cloud_calls"], 3)
        self.assertEqual(
            summary.call_args.kwargs["workflow"]["takeover"]["reason"],
            "cloud_capacity_reserved_for_takeover_and_final_review",
        )

    def test_supervisor_validation_failure_skips_final_review(self):
        valid = [{"command": "test", "exit_code": 0}]
        failed = [{"command": "test", "exit_code": 1, "log": "/missing"}]
        docs = {"command": "documentation-contract", "exit_code": 0}
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            plan = project / "approved.md"
            plan.write_text("Risk classification: high\n", encoding="utf-8")
            self.attach_validation_logs(project / "mock-validation.log", valid, docs)
            (project / ".mvp-ai.toml").write_text(
                '[validation]\ncommands = ["test"]\n', encoding="utf-8"
            )
            args = SimpleNamespace(
                project=str(project),
                plan=str(plan),
                model="primary",
                allow_dirty=False,
            )
            with (
                mock.patch.object(MODULE, "validate_project"),
                mock.patch.object(MODULE, "preflight_host"),
                mock.patch.object(MODULE, "make_run_dir", return_value=project),
                mock.patch.object(
                    MODULE, "run_validations", side_effect=[valid, failed]
                ),
                mock.patch.object(
                    MODULE, "validate_development_documents", return_value=docs
                ),
                mock.patch.object(MODULE, "call_local_coder") as local,
                mock.patch.object(
                    MODULE, "call_supervisor", return_value=None
                ) as supervisor,
                mock.patch.object(MODULE, "call_reviewer") as reviewer,
                mock.patch.object(MODULE, "write_failure_summary") as failure,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = MODULE.command_run(args)
        self.assertEqual(result, 2)
        local.assert_not_called()
        supervisor.assert_called_once()
        reviewer.assert_not_called()
        self.assertEqual(
            failure.call_args.kwargs["workflow"]["failure_classification"],
            "supervisor_validation",
        )
        self.assertEqual(
            failure.call_args.kwargs["workflow"]["cloud_calls"], 1
        )

    def test_reviewer_tool_failure_is_classified_and_metered(self):
        valid = [{"command": "test", "exit_code": 0}]
        docs = {"command": "documentation-contract", "exit_code": 0}
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            plan = project / "approved.md"
            plan.write_text("Risk classification: low\n", encoding="utf-8")
            self.attach_validation_logs(project / "mock-validation.log", valid, docs)
            (project / ".mvp-ai.toml").write_text(
                '[validation]\ncommands = ["test"]\n', encoding="utf-8"
            )
            args = SimpleNamespace(
                project=str(project), plan=str(plan), model="primary", allow_dirty=False
            )
            with (
                mock.patch.object(MODULE, "validate_project"),
                mock.patch.object(MODULE, "preflight_host"),
                mock.patch.object(MODULE, "ensure_model_available"),
                mock.patch.object(MODULE, "make_run_dir", return_value=project),
                mock.patch.object(MODULE, "run_validations", return_value=valid),
                mock.patch.object(
                    MODULE, "validate_development_documents", return_value=docs
                ),
                mock.patch.object(MODULE, "call_local_coder", return_value=None),
                mock.patch.object(
                    MODULE,
                    "call_reviewer",
                    side_effect=MODULE.StructuredResultError("schema changed"),
                ),
                mock.patch.object(MODULE, "write_failure_summary") as failure,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(MODULE.WorkflowError, "schema changed"):
                    MODULE.command_run(args)
        self.assertEqual(
            failure.call_args.kwargs["workflow"]["failure_classification"],
            "reviewer_result_invalid",
        )
        review_stage = failure.call_args.kwargs["usage_stages"][-1]
        self.assertEqual(review_stage["backend"], "cloud-review")
        self.assertEqual(review_stage["measurement"], "unavailable")
        self.assertEqual(review_stage["exit_code"], 0)

    def test_summary_contract_redacts_secrets_and_never_claims_saving(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            run_dir = Path(directory) / "run"
            project.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            MODULE.write_summary(
                run_dir,
                project=project,
                model="model",
                status="needs_manual_attention",
                review={
                    "verdict": "fail",
                    "summary": "API_KEY=must-not-leak",
                    "findings": [],
                    "metadata": {"api_key": "structured-secret"},
                },
                validations=[],
                usage_stages=[],
            )
            text = (run_dir / "summary.json").read_text(encoding="utf-8")
            payload = json.loads(text)
        self.assertNotIn("must-not-leak", text)
        self.assertNotIn("structured-secret", text)
        self.assertEqual(payload["efficiency"]["claim"], "no_baseline")
        self.assertFalse(payload["usage"]["measurement_complete"])
        self.assertIn("workflow", payload)

    @mock.patch.object(MODULE, "write_summary")
    @mock.patch.object(MODULE, "call_supervisor")
    @mock.patch.object(MODULE, "call_reviewer")
    @mock.patch.object(MODULE, "validate_development_documents")
    @mock.patch.object(MODULE, "run_validations")
    @mock.patch.object(MODULE, "call_local_coder")
    @mock.patch.object(MODULE, "ensure_model_available")
    @mock.patch.object(MODULE, "validate_project")
    @mock.patch.object(MODULE, "preflight_host")
    def test_watchdog_hands_local_failure_to_supervisor(
        self,
        preflight_host,
        validate_project,
        ensure_model_available,
        call_local_coder,
        run_validations,
        validate_development_documents,
        call_reviewer,
        call_supervisor,
        write_summary,
    ):
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
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            call_local_coder.side_effect = MODULE.CommandError(
                "ollama stopped",
                returncode=7,
                log_path=project / "coder-initial.log",
            )
            plan = project / "plan.md"
            plan.write_text("# plan\nRisk classification: low\n", encoding="utf-8")
            self.attach_validation_logs(
                project / "mock-validation.log",
                run_validations.return_value,
                validate_development_documents.return_value,
            )
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
        stages = write_summary.call_args.kwargs["usage_stages"]
        self.assertEqual(stages[0]["stage"], "coder-initial")
        self.assertEqual(stages[0]["measurement"], "unavailable")
        self.assertEqual(stages[0]["exit_code"], 7)


if __name__ == "__main__":
    unittest.main()
