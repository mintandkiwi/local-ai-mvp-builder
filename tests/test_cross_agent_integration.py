import os
import re
import subprocess
import sys
import tempfile
import unittest
import importlib.util
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "bin" / "install-agent-skills"
LAUNCHER = REPO_ROOT / "bin" / "mvp-loop-supervised"
SKILL_SOURCE = REPO_ROOT / "integrations" / "skills" / "local-ai-mvp-builder"

INSTALLER_MODULE_PATH = REPO_ROOT / "src" / "agent_skill_installer.py"
INSTALLER_SPEC = importlib.util.spec_from_file_location(
    "agent_skill_installer_under_test", INSTALLER_MODULE_PATH
)
INSTALLER_MODULE = importlib.util.module_from_spec(INSTALLER_SPEC)
assert INSTALLER_SPEC and INSTALLER_SPEC.loader
sys.modules[INSTALLER_SPEC.name] = INSTALLER_MODULE
INSTALLER_SPEC.loader.exec_module(INSTALLER_MODULE)

PLATFORM_PATHS = (
    Path(".codex/skills/local-ai-mvp-builder"),
    Path(".claude/skills/local-ai-mvp-builder"),
    Path(".gemini/config/skills/local-ai-mvp-builder"),
    Path(".gemini/antigravity-cli/skills/local-ai-mvp-builder"),
)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)

    def tearDown(self):
        self.temporary.cleanup()

    def run_installer(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(INSTALLER), *args],
            cwd=Path(self.temporary.name),
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_dry_run_writes_nothing(self):
        result = self.run_installer("--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertIn("演练完成", result.stdout)

    def test_installs_all_platform_links_and_is_idempotent(self):
        first = self.run_installer()
        self.assertEqual(first.returncode, 0, first.stderr)

        for relative in PLATFORM_PATHS:
            target = self.home / relative
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), SKILL_SOURCE.resolve())
            self.assertTrue((target / "SKILL.md").is_file())

        launcher = self.home / ".local/bin/mvp-loop-supervised"
        self.assertTrue(launcher.is_symlink())
        self.assertEqual(launcher.resolve(), LAUNCHER.resolve())

        second = self.run_installer()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("已是最新", second.stdout)
        backups = [path for path in self.home.rglob("*") if ".backup." in path.name]
        self.assertEqual(backups, [])

    def test_conflict_is_rejected_without_data_loss(self):
        target = self.home / PLATFORM_PATHS[0]
        target.mkdir(parents=True)
        marker = target / "user-file.txt"
        marker.write_text("keep me", encoding="utf-8")

        result = self.run_installer("--platforms", "codex")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep me")
        self.assertFalse(target.is_symlink())
        self.assertIn("未完成目标", result.stderr)

    def test_force_creates_recoverable_backup_before_replacement(self):
        target = self.home / PLATFORM_PATHS[0]
        target.mkdir(parents=True)
        (target / "user-file.txt").write_text("original", encoding="utf-8")

        result = self.run_installer("--platforms", "codex", "--force")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(target.is_symlink())
        backups = list(target.parent.glob(f"{target.name}.backup.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(
            (backups[0] / "user-file.txt").read_text(encoding="utf-8"),
            "original",
        )
        self.assertIn("已备份", result.stdout)

    def test_failed_link_creation_restores_original_target(self):
        target_path = self.home / PLATFORM_PATHS[0]
        target_path.mkdir(parents=True)
        marker = target_path / "user-file.txt"
        marker.write_text("original", encoding="utf-8")
        target = INSTALLER_MODULE.Target(
            "codex", target_path, SKILL_SOURCE
        )

        with mock.patch.object(
            INSTALLER_MODULE.Path,
            "symlink_to",
            side_effect=OSError("injected link failure"),
        ):
            with self.assertRaisesRegex(OSError, "injected link failure"):
                INSTALLER_MODULE.install_target(
                    target, self.home, dry_run=False, force=True
                )

        self.assertTrue(target_path.is_dir())
        self.assertFalse(target_path.is_symlink())
        self.assertEqual(marker.read_text(encoding="utf-8"), "original")
        self.assertEqual(
            list(target_path.parent.glob(f"{target_path.name}.backup.*")), []
        )

    def test_symlinked_parent_is_rejected(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (self.home / ".codex").symlink_to(outside, target_is_directory=True)

        result = self.run_installer("--platforms", "codex")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertIn("拒绝经过软链接父目录写入", result.stderr)


class SupervisedLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.plan = self.root / "approved plan.md"
        self.plan.write_text("# Approved plan\n", encoding="utf-8")
        (self.project / ".mvp-ai.toml").write_text(
            '[project]\nname = "test"\n\n'
            '[validation]\ncommands = ["python3 -m unittest"]\n',
            encoding="utf-8",
        )
        self._init_git(self.project)

        self.mock_bin = self.root / "mock-bin"
        self.mock_bin.mkdir()
        self.invocation_log = self.root / "python-invocations.log"
        mock_python = self.mock_bin / "python3"
        mock_python.write_text(
            "#!/bin/sh\n"
            "case \" $* \" in\n"
            "  *\" check-config \"*) exec \"$MVP_TEST_REAL_PYTHON\" \"$@\" ;;\n"
            "esac\n"
            "printf '%s\\n' \"$*\" >> \"$MVP_TEST_LOG\"\n"
            "exit 0\n",
            encoding="utf-8",
        )
        mock_python.chmod(0o755)
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.mock_bin}{os.pathsep}{self.env['PATH']}"
        self.env["MVP_TEST_LOG"] = str(self.invocation_log)
        self.env["MVP_TEST_REAL_PYTHON"] = os.environ.get(
            "MVP_TEST_REAL_PYTHON", sys.executable
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _init_git(self, project: Path) -> None:
        subprocess.run(
            ["git", "init", "-q", str(project)], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(project), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(project),
                "-c",
                "user.name=Test User",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "test baseline",
                "--allow-empty",
            ],
            check=True,
            capture_output=True,
        )

    def run_launcher(
        self, *args: str, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(LAUNCHER), *args],
            cwd=cwd or self.root,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def invocation_lines(self) -> list[str]:
        return self.invocation_log.read_text(encoding="utf-8").splitlines()

    def test_help_works_from_arbitrary_cwd_through_symlink(self):
        link_dir = self.root / "path-bin"
        link_dir.mkdir()
        link = link_dir / "mvp-loop-supervised"
        link.symlink_to(LAUNCHER)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()

        result = subprocess.run(
            [str(link), "--help"],
            cwd=elsewhere,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--project", result.stdout)
        self.assertIn("--display summary|live", result.stdout)

    def test_summary_and_live_map_to_real_orchestrator_run(self):
        summary = self.run_launcher(
            "--project",
            str(self.project),
            "--plan",
            str(self.plan),
            "--display",
            "summary",
        )
        self.assertEqual(summary.returncode, 0, summary.stderr)
        lines = self.invocation_lines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("src/mvp_orchestrator.py doctor"))
        self.assertIn("src/mvp_orchestrator.py run --project", lines[1])
        self.assertIn(f"--plan {self.plan.resolve()}", lines[1])
        self.assertNotIn("--live", lines[1])

        self.invocation_log.unlink()
        live = self.run_launcher(
            "--project",
            str(self.project),
            "--plan",
            str(self.plan),
            "--model",
            "secondary",
            "--display",
            "live",
        )
        self.assertEqual(live.returncode, 0, live.stderr)
        self.assertIn("--model secondary --live", self.invocation_lines()[1])

    def test_untracked_file_triggers_dirty_worktree_gate(self):
        (self.project / "untracked.txt").write_text("dirty", encoding="utf-8")

        result = self.run_launcher(
            "--project", str(self.project), "--plan", str(self.plan)
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("不是干净状态", result.stderr)
        self.assertFalse(self.invocation_log.exists())

    def test_missing_plan_and_config_fail_before_doctor(self):
        missing_plan = self.run_launcher(
            "--project", str(self.project), "--plan", str(self.root / "missing.md")
        )
        self.assertEqual(missing_plan.returncode, 1)
        self.assertIn("计划文件不存在", missing_plan.stderr)

        no_config = self.root / "no-config"
        no_config.mkdir()
        self._init_git(no_config)
        missing_config = self.run_launcher(
            "--project", str(no_config), "--plan", str(self.plan)
        )
        self.assertEqual(missing_config.returncode, 1)
        self.assertIn("缺少 .mvp-ai.toml", missing_config.stderr)
        self.assertFalse(self.invocation_log.exists())

    def test_blank_validation_command_fails_before_doctor(self):
        (self.project / ".mvp-ai.toml").write_text(
            '[project]\nname = "test"\n\n'
            '[validation]\ncommands = ["   "]\n',
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(self.project), "add", ".mvp-ai.toml"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.project),
                "-c",
                "user.name=Test User",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "blank validation fixture",
            ],
            check=True,
            capture_output=True,
        )

        result = self.run_launcher(
            "--project", str(self.project), "--plan", str(self.plan)
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("validation.commands", result.stderr)
        self.assertFalse(self.invocation_log.exists())

    def test_allow_dirty_and_unknown_values_are_rejected(self):
        rejected = self.run_launcher(
            "--project",
            str(self.project),
            "--plan",
            str(self.plan),
            "--allow-dirty",
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("不允许 --allow-dirty", rejected.stderr)

        bad_display = self.run_launcher(
            "--project",
            str(self.project),
            "--plan",
            str(self.plan),
            "--display",
            "verbose",
        )
        self.assertEqual(bad_display.returncode, 2)
        self.assertIn("summary 或 live", bad_display.stderr)
        self.assertFalse(self.invocation_log.exists())


class SharedSkillTests(unittest.TestCase):
    def test_skill_package_has_valid_public_shape(self):
        skill = (SKILL_SOURCE / "SKILL.md").read_text(encoding="utf-8")
        match = re.match(r"\A---\n(.*?)\n---\n", skill, re.DOTALL)
        self.assertIsNotNone(match, "SKILL.md must start with YAML frontmatter")
        frontmatter = match.group(1)
        self.assertRegex(frontmatter, r"(?m)^name: local-ai-mvp-builder$")
        self.assertRegex(frontmatter, r"(?m)^description: .+$")
        self.assertEqual(
            set(
                line.split(":", 1)[0]
                for line in frontmatter.splitlines()
                if ":" in line
            ),
            {"name", "description"},
        )

        self.assertTrue((SKILL_SOURCE / "agents/openai.yaml").is_file())
        self.assertTrue((SKILL_SOURCE / "references/plan-format.md").is_file())
        self.assertTrue((SKILL_SOURCE / "references/project-docs.md").is_file())
        self.assertIn(
            "${MVP_LOOP_PLAN_DIR:-$HOME/.local/share/local-ai-mvp-builder/plans}",
            skill,
        )
        self.assertNotIn("CODEX_HOME", skill)
        self.assertNotIn("active Codex takeover agent", skill)
        self.assertIn("Do not let the current frontend edit the project", skill)
        self.assertIn("resume the failed run from Codex", skill)
        self.assertFalse(any(SKILL_SOURCE.rglob("*.bak")))

    def test_new_shell_entries_parse_and_files_have_no_trailing_space(self):
        for script in (
            REPO_ROOT / "bin/mvp-loop",
            LAUNCHER,
            INSTALLER,
            REPO_ROOT / "bin/pull-models",
        ):
            result = subprocess.run(
                ["sh", "-n", str(script)], text=True, capture_output=True, check=False
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        paths = [INSTALLER, LAUNCHER, REPO_ROOT / "src/agent_skill_installer.py"]
        paths.extend(path for path in SKILL_SOURCE.rglob("*") if path.is_file())
        paths.append(Path(__file__))
        for path in paths:
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                self.assertEqual(
                    line,
                    line.rstrip(),
                    f"trailing whitespace at {path}:{number}",
                )


if __name__ == "__main__":
    unittest.main()
