#!/usr/bin/env python3
"""Orchestrate local Codex coding and independent cloud Codex review loops."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULTS_PATH = ROOT / "config" / "defaults.toml"
MODELS_PATH = ROOT / "config" / "models.toml"
REVIEW_SCHEMA = ROOT / "schemas" / "review.schema.json"
PROMPTS = ROOT / "prompts"


class WorkflowError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    max_local_review_rounds: int
    coder_timeout_seconds: int
    review_timeout_seconds: int
    supervisor_timeout_seconds: int
    require_clean_worktree: bool
    codex_command: str
    ollama_host: str
    cloud_provider: str
    cloud_model: str
    cloud_reasoning_effort: str


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_settings() -> Settings:
    raw = load_toml(DEFAULTS_PATH)
    workflow = raw["workflow"]
    runtime = raw["runtime"]
    cloud = raw["cloud"]
    return Settings(
        max_local_review_rounds=int(workflow["max_local_review_rounds"]),
        coder_timeout_seconds=int(workflow["coder_timeout_seconds"]),
        review_timeout_seconds=int(workflow["review_timeout_seconds"]),
        supervisor_timeout_seconds=int(workflow["supervisor_timeout_seconds"]),
        require_clean_worktree=bool(workflow["require_clean_worktree"]),
        codex_command=str(runtime["codex_command"]),
        ollama_host=str(runtime["ollama_host"]),
        cloud_provider=str(cloud["provider"]),
        cloud_model=str(cloud["model"]),
        cloud_reasoning_effort=str(cloud["reasoning_effort"]),
    )


def render_prompt(name: str, **values: str) -> str:
    text = (PROMPTS / name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key.upper() + "}}", value)
    return text


def run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    stdin_text: str | None,
    log_path: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    loopback = "localhost,127.0.0.1,::1"
    env["NO_PROXY"] = loopback
    env["no_proxy"] = loopback
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            input=stdin_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        log_path.write_text(
            f"command: {json.dumps(command, ensure_ascii=False)}\n"
            f"exit_code: 124\n"
            f"elapsed_seconds: {elapsed:.2f}\n"
            f"error: timeout\n\n{output}",
            encoding="utf-8",
        )
        raise
    elapsed = time.monotonic() - started
    log_path.write_text(
        f"command: {json.dumps(command, ensure_ascii=False)}\n"
        f"exit_code: {result.returncode}\n"
        f"elapsed_seconds: {elapsed:.2f}\n\n"
        f"{result.stdout}",
        encoding="utf-8",
    )
    if check and result.returncode != 0:
        raise WorkflowError(
            f"命令失败（exit={result.returncode}），详情见 {log_path}"
        )
    return result


def git_output(project: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise WorkflowError(result.stderr.strip() or "Git 命令失败")
    return result.stdout


def validate_project(project: Path, require_clean: bool) -> None:
    if not project.is_dir():
        raise WorkflowError(f"项目目录不存在：{project}")
    inside = git_output(project, "rev-parse", "--is-inside-work-tree").strip()
    if inside != "true":
        raise WorkflowError("目标目录必须是 Git 仓库")
    if require_clean and git_output(project, "status", "--porcelain").strip():
        raise WorkflowError(
            "目标仓库不是干净状态。请先处理现有改动，或明确使用 --allow-dirty。"
        )


def load_validation_commands(project: Path) -> list[str]:
    config_path = project / ".mvp-ai.toml"
    if not config_path.exists():
        return []
    raw = load_toml(config_path)
    commands = raw.get("validation", {}).get("commands", [])
    if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
        raise WorkflowError(".mvp-ai.toml 中 validation.commands 必须是字符串数组")
    return commands


def seatbelt_escape(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def project_secret_regexes(project: Path) -> list[str]:
    root = re.escape(str(project)).replace('"', '\\"')
    prefix = rf"^{root}/(.*/)?"
    return [
        prefix + r"\.env(\..*)?$",
        prefix + r"(\.npmrc|\.pypirc|\.netrc|id_(rsa|dsa|ecdsa|ed25519))$",
        prefix + r"[^/]*\.(pem|key|p12|pfx)$",
        prefix + r"(credentials(\..*)?|service[-_]?account[^/]*\.json)$",
        prefix + r"[^/]*secret[^/]*\.(json|toml|ya?ml|ini)$",
    ]


def user_toolchain_read_roots(home: Path) -> list[Path]:
    roots = [home / ".local" / "bin", home / ".cargo" / "bin"]
    for executable in ("node", "npm", "npx", "python3", "uv", "cargo", "rustc"):
        found = shutil.which(executable)
        if not found:
            continue
        resolved = Path(found).resolve()
        parts = resolved.parts
        if ".nvm" in parts and "versions" in parts and "node" in parts:
            node_index = parts.index("node", parts.index("versions"))
            if len(parts) > node_index + 1:
                roots.append(Path(*parts[: node_index + 2]))
        elif resolved.is_relative_to(home / "miniconda3"):
            roots.append(home / "miniconda3")
    return roots


def write_validation_profile(project: Path, run_dir: Path) -> Path:
    project = project.resolve()
    run_dir = run_dir.resolve()
    home = Path.home()
    allowed_reads = [
        project,
        run_dir,
        Path("/System"),
        Path("/usr"),
        Path("/bin"),
        Path("/sbin"),
        Path("/Library"),
        Path("/Applications"),
        Path("/opt/homebrew"),
        *user_toolchain_read_roots(home),
    ]
    lines = [
        "(version 1)",
        "(deny default)",
        '(import "system.sb")',
        "(allow process*)",
        "(allow sysctl-read)",
        f'(allow file-write* (subpath "{seatbelt_escape(project)}"))',
        f'(allow file-write* (subpath "{seatbelt_escape(run_dir)}"))',
        f'(deny file-write* (subpath "{seatbelt_escape(project / ".git")}"))',
        "(deny network*)",
    ]
    lines.extend(
        f'(allow file-read* (subpath "{seatbelt_escape(path)}"))'
        for path in allowed_reads
        if path.exists()
    )
    lines.extend(
        f'(deny file-read* (regex #"{pattern}"))'
        for pattern in project_secret_regexes(project)
    )
    lines.extend(
        f'(deny file-write* (regex #"{pattern}"))'
        for pattern in project_secret_regexes(project)
    )
    profile = run_dir / "validation.sb"
    profile.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return profile


def validation_environment(run_dir: Path) -> dict[str, str]:
    run_dir = run_dir.resolve()
    safe_home = run_dir / "validation-home"
    safe_tmp = run_dir / "validation-tmp"
    safe_home.mkdir(exist_ok=True)
    safe_tmp.mkdir(exist_ok=True)
    allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SDKROOT",
        "DEVELOPER_DIR",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update(
        {
            "HOME": str(safe_home),
            "TMPDIR": str(safe_tmp),
            "XDG_CACHE_HOME": str(safe_home / ".cache"),
            "CI": "1",
            "NO_COLOR": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": str(safe_tmp / "pycache"),
            "PIP_CACHE_DIR": str(safe_tmp / "pip-cache"),
            "UV_CACHE_DIR": str(safe_tmp / "uv-cache"),
            "NPM_CONFIG_CACHE": str(safe_tmp / "npm-cache"),
        }
    )
    return env


def run_validations(
    project: Path, commands: list[str], run_dir: Path, stage: str
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    profile = write_validation_profile(project, run_dir)
    env = validation_environment(run_dir)
    for index, command in enumerate(commands, start=1):
        log_path = run_dir / f"{stage}-validation-{index}.log"
        started = time.monotonic()
        exit_code = 124
        output = ""
        try:
            result = subprocess.run(
                ["sandbox-exec", "-f", str(profile), "/bin/zsh", "-lc", command],
                cwd=project,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=1800,
                check=False,
                env=env,
            )
            exit_code = result.returncode
            output = result.stdout
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout or ""
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            output += "\nvalidation timed out after 1800 seconds\n"
        elapsed = time.monotonic() - started
        log_path.write_text(output, encoding="utf-8")
        outcomes.append(
            {
                "command": command,
                "exit_code": exit_code,
                "elapsed_seconds": round(elapsed, 2),
                "log": str(log_path),
            }
        )
    return outcomes


def validations_passed(outcomes: list[dict[str, Any]]) -> bool:
    return bool(outcomes) and all(item.get("exit_code") == 0 for item in outcomes)


def normalize_review(
    review: dict[str, Any], validations: list[dict[str, Any]]
) -> dict[str, Any]:
    findings = review.get("findings")
    if not isinstance(findings, list):
        raise WorkflowError("评审结果 findings 必须是数组")
    blocking = any(
        isinstance(item, dict) and item.get("severity") in {"P0", "P1", "P2"}
        for item in findings
    )
    if not validations_passed(validations):
        failed_commands = [
            str(item.get("command"))
            for item in validations
            if item.get("exit_code") != 0
        ]
        findings.append(
            {
                "severity": "P1",
                "title": "项目验证未通过",
                "file": ".mvp-ai.toml",
                "line": None,
                "description": (
                    "没有配置验证命令" if not validations else "验证命令返回非零状态"
                ),
                "required_fix": "修复代码或验证环境并确保这些命令成功："
                + ", ".join(failed_commands),
            }
        )
        blocking = True
    review["verdict"] = "fail" if blocking else review.get("verdict")
    return review


def model_id(alias_or_id: str) -> str:
    models = load_toml(MODELS_PATH)["models"]
    if alias_or_id in models:
        return str(models[alias_or_id]["id"])
    return alias_or_id


def ollama_models(host: str) -> set[str]:
    try:
        with urllib.request.urlopen(host.rstrip("/") + "/api/tags", timeout=5) as response:
            data = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"无法连接 Ollama：{exc}") from exc
    return {str(item["name"]) for item in data.get("models", [])}


def ensure_model_available(model: str, host: str) -> None:
    available = ollama_models(host)
    normalized = model if ":" in model else model + ":latest"
    if model not in available and normalized not in available:
        raise WorkflowError(
            f"本地尚未安装模型 {model}。请先运行：ollama pull {model}"
        )


def make_run_dir(project: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(str(project).encode()).hexdigest()[:8]
    path = ROOT / "runs" / f"{stamp}-{project.name}-{digest}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def call_local_coder(
    settings: Settings,
    project: Path,
    model: str,
    prompt: str,
    run_dir: Path,
    stage: str,
) -> None:
    last_message = run_dir / f"{stage}-last-message.txt"
    command = [
        settings.codex_command,
        "-a",
        "never",
        "exec",
        "--oss",
        "--local-provider",
        "ollama",
        "-m",
        model,
        "-c",
        "model_context_window=32768",
        "-C",
        str(project),
        "-s",
        "workspace-write",
        "--ephemeral",
        "--ignore-user-config",
        "-o",
        str(last_message),
        "-",
    ]
    run_command(
        command,
        cwd=project,
        timeout=settings.coder_timeout_seconds,
        stdin_text=prompt,
        log_path=run_dir / f"{stage}.log",
    )


def call_reviewer(
    settings: Settings,
    project: Path,
    prompt: str,
    run_dir: Path,
    round_number: int,
) -> dict[str, Any]:
    output_path = run_dir / f"review-{round_number}.json"
    command = [
        settings.codex_command,
        "-a",
        "never",
        "exec",
        "-c",
        f'model_provider="{settings.cloud_provider}"',
        "-c",
        f'model_reasoning_effort="{settings.cloud_reasoning_effort}"',
        "-m",
        settings.cloud_model,
        "-C",
        str(project),
        "-s",
        "read-only",
        "--ephemeral",
        "--ignore-user-config",
        "--output-schema",
        str(REVIEW_SCHEMA),
        "-o",
        str(output_path),
        "-",
    ]
    run_command(
        command,
        cwd=project,
        timeout=settings.review_timeout_seconds,
        stdin_text=prompt,
        log_path=run_dir / f"review-{round_number}.log",
    )
    try:
        review = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"无法解析结构化评审结果：{output_path}") from exc
    if review.get("verdict") not in {"pass", "fail"}:
        raise WorkflowError("评审结果缺少有效 verdict")
    return review


def call_supervisor(
    settings: Settings,
    project: Path,
    prompt: str,
    run_dir: Path,
) -> None:
    last_message = run_dir / "supervisor-last-message.txt"
    command = [
        settings.codex_command,
        "-a",
        "never",
        "exec",
        "-c",
        f'model_provider="{settings.cloud_provider}"',
        "-c",
        f'model_reasoning_effort="{settings.cloud_reasoning_effort}"',
        "-m",
        settings.cloud_model,
        "-C",
        str(project),
        "-s",
        "workspace-write",
        "--ephemeral",
        "--ignore-user-config",
        "-o",
        str(last_message),
        "-",
    ]
    run_command(
        command,
        cwd=project,
        timeout=settings.supervisor_timeout_seconds,
        stdin_text=prompt,
        log_path=run_dir / "supervisor.log",
    )


def write_summary(
    run_dir: Path,
    *,
    project: Path,
    model: str,
    status: str,
    review: dict[str, Any] | None,
    validations: list[dict[str, Any]],
) -> None:
    payload = {
        "project": str(project),
        "model": model,
        "status": status,
        "review": review,
        "validations": validations,
        "git_status": git_output(project, "status", "--short"),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def write_failure_summary(
    run_dir: Path,
    *,
    project: Path,
    model: str,
    error: BaseException,
    review: dict[str, Any] | None,
    validations: list[dict[str, Any]],
) -> None:
    try:
        status = git_output(project, "status", "--short")
    except Exception as git_error:
        status = f"unavailable: {git_error}"
    payload = {
        "project": str(project),
        "model": model,
        "status": "needs_manual_attention",
        "error": f"{type(error).__name__}: {error}",
        "review": review,
        "validations": validations,
        "git_status": status,
    }
    (run_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def command_doctor(args: argparse.Namespace) -> int:
    settings = load_settings()
    checks = {
        "codex": shutil.which(settings.codex_command),
        "ollama": shutil.which("ollama"),
        "aria2c": shutil.which("aria2c"),
        "git": shutil.which("git"),
        "ollama_models": sorted(ollama_models(settings.ollama_host)),
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if all(checks[name] for name in ("codex", "ollama", "aria2c", "git")) else 1


def command_init(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    if not project.exists():
        project.mkdir(parents=True)
    if not (project / ".git").exists():
        subprocess.run(["git", "init"], cwd=project, check=True)
    for source_name, target_name in (("AGENTS.md", "AGENTS.md"), ("mvp-ai.toml", ".mvp-ai.toml")):
        target = project / target_name
        if not target.exists():
            shutil.copy2(ROOT / "templates" / source_name, target)
            print(f"created {target}")
        else:
            print(f"kept existing {target}")
    print("请编辑 .mvp-ai.toml 的验证命令，并提交初始化文件作为干净基线。")
    return 0


def command_run(args: argparse.Namespace) -> int:
    settings = load_settings()
    rounds = settings.max_local_review_rounds
    if rounds != 2:
        raise WorkflowError("workflow.max_local_review_rounds 必须固定为 2")
    project = Path(args.project).expanduser().resolve()
    plan_path = Path(args.plan).expanduser().resolve()
    if not plan_path.is_file():
        raise WorkflowError(f"计划文件不存在：{plan_path}")
    plan = plan_path.read_text(encoding="utf-8")
    model = model_id(args.model)
    validate_project(project, settings.require_clean_worktree and not args.allow_dirty)
    ensure_model_available(model, settings.ollama_host)
    commands = load_validation_commands(project)
    if not commands:
        raise WorkflowError(
            ".mvp-ai.toml 必须至少配置一条 validation.commands，不能在无验证条件下运行"
        )
    run_dir = make_run_dir(project)
    (run_dir / "plan.md").write_text(plan, encoding="utf-8")
    all_validations: list[dict[str, Any]] = []
    latest_validations: list[dict[str, Any]] = []
    latest_review: dict[str, Any] | None = None

    def guarded(action):
        try:
            return action()
        except BaseException as exc:
            write_failure_summary(
                run_dir,
                project=project,
                model=model,
                error=exc,
                review=latest_review,
                validations=all_validations,
            )
            raise

    print(f"run_dir={run_dir}")
    print(f"model={model}")
    guarded(
        lambda: call_local_coder(
            settings,
            project,
            model,
            render_prompt("coder.md", plan=plan),
            run_dir,
            "coder-initial",
        )
    )
    latest_validations = guarded(
        lambda: run_validations(project, commands, run_dir, "coder-initial")
    )
    all_validations.extend(latest_validations)

    for round_number in range(1, rounds + 1):
        latest_review = guarded(
            lambda: normalize_review(
                call_reviewer(
                    settings,
                    project,
                    render_prompt(
                        "reviewer.md",
                        plan=plan,
                        validation=json.dumps(
                            latest_validations, ensure_ascii=False, indent=2
                        ),
                    ),
                    run_dir,
                    round_number,
                ),
                latest_validations,
            )
        )
        if latest_review["verdict"] == "pass":
            write_summary(
                run_dir,
                project=project,
                model=model,
                status="ready_for_user_review",
                review=latest_review,
                validations=all_validations,
            )
            print("PASS：已通过独立 Code Review，等待用户评审。")
            return 0
        if round_number < rounds:
            guarded(
                lambda: call_local_coder(
                    settings,
                    project,
                    model,
                    render_prompt(
                        "fixer.md",
                        plan=plan,
                        review=json.dumps(latest_review, ensure_ascii=False, indent=2),
                    ),
                    run_dir,
                    f"local-fix-{round_number}",
                )
            )
            latest_validations = guarded(
                lambda: run_validations(
                    project, commands, run_dir, f"local-fix-{round_number}"
                )
            )
            all_validations.extend(latest_validations)

    assert latest_review is not None
    print("本地模型两轮评审后仍有问题，切换监督者修复。")
    guarded(
        lambda: call_supervisor(
            settings,
            project,
            render_prompt(
                "supervisor.md",
                plan=plan,
                review=json.dumps(latest_review, ensure_ascii=False, indent=2),
            ),
            run_dir,
        )
    )
    latest_validations = guarded(
        lambda: run_validations(project, commands, run_dir, "supervisor")
    )
    all_validations.extend(latest_validations)
    final_review = guarded(
        lambda: normalize_review(
            call_reviewer(
                settings,
                project,
                render_prompt(
                    "reviewer.md",
                    plan=plan,
                    validation=json.dumps(
                        latest_validations, ensure_ascii=False, indent=2
                    ),
                ),
                run_dir,
                rounds + 1,
            ),
            latest_validations,
        )
    )
    status = "ready_for_user_review" if final_review["verdict"] == "pass" else "needs_manual_attention"
    write_summary(
        run_dir,
        project=project,
        model=model,
        status=status,
        review=final_review,
        validations=all_validations,
    )
    if final_review["verdict"] == "pass":
        print("PASS：监督者修复后通过最终评审，等待用户评审。")
        return 0
    print("BLOCKED：监督者修复后仍有问题，需要当前 Codex 任务人工处理。")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="本地 AI coding + Codex review 的受监督 MVP 工作流"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="检查 Codex、Ollama、Git 与本地模型")
    doctor.set_defaults(func=command_doctor)

    init = subparsers.add_parser("init", help="初始化目标 Git 项目的 AI 工作流文件")
    init.add_argument("--project", required=True)
    init.set_defaults(func=command_init)

    run = subparsers.add_parser("run", help="执行 coding、review、修复循环")
    run.add_argument("--project", required=True)
    run.add_argument("--plan", required=True)
    run.add_argument("--model", default="primary", help="模型别名或 Ollama 模型 ID")
    run.add_argument("--allow-dirty", action="store_true")
    run.set_defaults(func=command_run)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except subprocess.TimeoutExpired as exc:
        print(f"超时：{exc.cmd}", file=sys.stderr)
        return 124
    except WorkflowError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
