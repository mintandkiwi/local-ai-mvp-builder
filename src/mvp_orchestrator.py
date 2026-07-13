#!/usr/bin/env python3
"""Orchestrate local Codex coding and independent cloud Codex review loops."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import selectors
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

SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([a-z0-9_-]*(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"token|password|passwd|secret)[a-z0-9_-]*)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
KNOWN_TOKEN = re.compile(
    r"\b(sk-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9_]{10,}|"
    r"AKIA[0-9A-Z]{12,}|Bearer\s+[A-Za-z0-9._~+/=-]{10,})\b",
    re.IGNORECASE,
)


class WorkflowError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    max_local_review_rounds: int
    coder_timeout_seconds: int
    local_stall_timeout_seconds: int
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
        local_stall_timeout_seconds=int(workflow["local_stall_timeout_seconds"]),
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


def redact_live_text(text: str, limit: int = 600) -> str:
    """Redact common credential forms before displaying child-process events."""
    clean = SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", text)
    clean = KNOWN_TOKEN.sub("[REDACTED]", clean)
    clean = clean.replace("-----BEGIN PRIVATE KEY-----", "[REDACTED PRIVATE KEY]")
    clean = clean.replace("-----BEGIN RSA PRIVATE KEY-----", "[REDACTED PRIVATE KEY]")
    clean = clean.strip()
    if len(clean) > limit:
        return clean[:limit].rstrip() + "…"
    return clean


def emit_progress(channel: str, message: str) -> None:
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] [{channel}] {redact_live_text(message)}", flush=True)


def display_codex_event(line: str, channel: str) -> None:
    """Render safe summaries from `codex exec --json` JSONL output."""
    stripped = line.strip()
    if not stripped:
        return
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        if "ERROR:" in stripped or "Error:" in stripped:
            emit_progress(channel, stripped)
        return

    event_type = str(event.get("type", ""))
    if event_type == "thread.started":
        emit_progress(channel, "本地模型会话已建立")
        return
    if event_type == "turn.started":
        emit_progress(channel, "本地模型开始处理计划")
        return
    if event_type == "turn.completed":
        usage = event.get("usage", {})
        emit_progress(
            channel,
            "本轮完成"
            f"（输入 {usage.get('input_tokens', '?')} tokens，"
            f"输出 {usage.get('output_tokens', '?')} tokens）",
        )
        return

    if event_type not in {"item.started", "item.updated", "item.completed"}:
        return
    item = event.get("item")
    if not isinstance(item, dict):
        return
    item_type = str(item.get("type", ""))
    completed = event_type == "item.completed"

    if item_type == "agent_message" and completed:
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            emit_progress(channel, "模型说明：" + text)
    elif item_type == "reasoning" and completed:
        summary = item.get("summary") or item.get("text")
        if isinstance(summary, str) and summary.strip():
            emit_progress(channel, "模型提供的推理摘要：" + summary)
    elif item_type == "command_execution":
        command = str(item.get("command", "命令"))
        if event_type == "item.started":
            emit_progress(channel, "执行命令：" + command)
        elif completed:
            exit_code = item.get("exit_code")
            status = item.get("status", "completed")
            emit_progress(channel, f"命令结束：{status}，exit={exit_code}")
    elif item_type in {"file_change", "file_write", "file_edit"} and completed:
        paths: list[str] = []
        changes = item.get("changes")
        if isinstance(changes, list):
            for change in changes:
                if isinstance(change, dict) and change.get("path"):
                    paths.append(str(change["path"]))
        path = item.get("path")
        if path:
            paths.append(str(path))
        emit_progress(channel, "文件改动：" + (", ".join(paths) or "已应用"))
    elif item_type == "error" and completed:
        message = item.get("message")
        if isinstance(message, str):
            emit_progress(channel, "运行提示：" + message)


def is_progress_event(line: str, json_events: bool) -> bool:
    if not json_events:
        return True
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return False
    return str(event.get("type", "")) in {
        "thread.started",
        "turn.started",
        "turn.completed",
        "item.started",
        "item.updated",
        "item.completed",
    }


def run_streaming_process(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    stdin_text: str | None,
    env: dict[str, str],
    channel: str,
    json_events: bool,
    display: bool,
    idle_timeout: int | None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    if process.stdin is not None:
        try:
            process.stdin.write(stdin_text or "")
            process.stdin.close()
        except BrokenPipeError:
            pass
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output: list[str] = []
    deadline = time.monotonic() + timeout
    last_output_at = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                process.kill()
                tail = process.stdout.read()
                if tail:
                    output.append(tail)
                process.wait()
                raise subprocess.TimeoutExpired(
                    command, timeout, output="".join(output)
                )
            idle_remaining = (
                idle_timeout - (now - last_output_at)
                if idle_timeout is not None
                else remaining
            )
            if idle_timeout is not None and idle_remaining <= 0:
                process.kill()
                tail = process.stdout.read()
                if tail:
                    output.append(tail)
                process.wait()
                raise subprocess.TimeoutExpired(
                    command, idle_timeout, output="".join(output)
                )
            events = selector.select(
                timeout=min(0.25, remaining, idle_remaining)
            )
            for key, _ in events:
                line = key.fileobj.readline()
                if not line:
                    selector.unregister(key.fileobj)
                    continue
                if is_progress_event(line, json_events):
                    last_output_at = time.monotonic()
                output.append(line)
                if display:
                    if json_events:
                        display_codex_event(line, channel)
                    else:
                        emit_progress(channel, line)
            if process.poll() is not None:
                tail = process.stdout.read()
                if tail:
                    output.append(tail)
                    if display:
                        for line in tail.splitlines():
                            if json_events:
                                display_codex_event(line, channel)
                            else:
                                emit_progress(channel, line)
                break
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
    return subprocess.CompletedProcess(command, process.returncode, "".join(output))


def run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    stdin_text: str | None,
    log_path: Path,
    check: bool = True,
    live: bool = False,
    monitor: bool = False,
    live_channel: str = "PROCESS",
    json_events: bool = False,
    idle_timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    loopback = "localhost,127.0.0.1,::1"
    env["NO_PROXY"] = loopback
    env["no_proxy"] = loopback
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        if live or monitor:
            result = run_streaming_process(
                command,
                cwd=cwd,
                timeout=timeout,
                stdin_text=stdin_text,
                env=env,
                channel=live_channel,
                json_events=json_events,
                display=live,
                idle_timeout=idle_timeout,
            )
        else:
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
    if not commands or any(not command.strip() for command in commands):
        raise WorkflowError(
            ".mvp-ai.toml 中 validation.commands 必须至少包含一条非空验证命令"
        )
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
        Path("/private/var/select"),
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
    read_ancestors = {
        parent
        for path in allowed_reads
        if path.exists()
        for parent in path.resolve().parents
    }
    lines.extend(
        f'(allow file-read* (literal "{seatbelt_escape(path)}"))'
        for path in sorted(read_ancestors, key=str)
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
    safe_bin = run_dir / "validation-bin"
    safe_home.mkdir(exist_ok=True)
    safe_tmp.mkdir(exist_ok=True)
    safe_bin.mkdir(exist_ok=True)
    allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SDKROOT",
        "DEVELOPER_DIR",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    developer_git = Path("/Applications/Xcode.app/Contents/Developer/usr/bin/git")
    git_link = safe_bin / "git"
    if developer_git.is_file() and not git_link.exists():
        git_link.symlink_to(developer_git)
    env["PATH"] = f"{safe_bin}{os.pathsep}{env.get('PATH', '')}"
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
            "MVP_VALIDATION_SANDBOX": "1",
        }
    )
    return env


def run_validations(
    project: Path,
    commands: list[str],
    run_dir: Path,
    stage: str,
    live: bool = False,
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    profile = write_validation_profile(project, run_dir)
    env = validation_environment(run_dir)
    for index, command in enumerate(commands, start=1):
        log_path = run_dir / f"{stage}-validation-{index}.log"
        if live:
            emit_progress("VALIDATE", f"开始：{command}")
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
        if live:
            result_text = "通过" if exit_code == 0 else f"失败（exit={exit_code}）"
            emit_progress(
                "VALIDATE",
                f"{result_text}，耗时 {elapsed:.2f}s；完整输出：{log_path}",
            )
    return outcomes


def development_document_requirements(today: str) -> list[tuple[Path, tuple[str, ...]]]:
    return [
        (
            Path("docs/devlog") / f"{today}.md",
            (
                "今日目标",
                "今日进展",
                "修改内容",
                "使用方法",
                "验证结果",
                "后续事项",
            ),
        ),
        (
            Path("docs/ai/PROJECT_OUTLINE.md"),
            (
                "项目目标",
                "技术栈",
                "架构与关键路径",
                "重要文件",
                "约束",
                "当前状态",
            ),
        ),
        (
            Path("docs/ai/TASK_PLAN.md"),
            (
                "当前里程碑",
                "已完成",
                "进行中",
                "待办",
                "验收标准",
                "下一步",
            ),
        ),
    ]


def validate_development_documents(
    project: Path, run_dir: Path, stage: str, today: str
) -> dict[str, Any]:
    problems: list[str] = []
    for relative, headings in development_document_requirements(today):
        path = project / relative
        if path.is_symlink():
            problems.append(f"{relative} 不能是符号链接")
            continue
        if not path.is_file():
            problems.append(f"缺少 {relative}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(re.findall(r"[\u4e00-\u9fff]", text)) < 20:
            problems.append(f"{relative} 中文内容不足")
        missing = [heading for heading in headings if heading not in text]
        if missing:
            problems.append(f"{relative} 缺少章节：{', '.join(missing)}")
        changed = git_output(project, "status", "--porcelain", "--", str(relative))
        if not changed.strip():
            problems.append(f"{relative} 未在本次开发中更新")

    log_path = run_dir / f"{stage}-documentation.log"
    if problems:
        output = "中文开发文档校验失败：\n- " + "\n- ".join(problems) + "\n"
        exit_code = 1
    else:
        output = "中文开发日志、AI 项目大纲和任务规划均已更新。\n"
        exit_code = 0
    log_path.write_text(output, encoding="utf-8")
    return {
        "command": "documentation-contract",
        "exit_code": exit_code,
        "elapsed_seconds": 0.0,
        "log": str(log_path),
    }


def safe_git_output(project: Path, *args: str) -> str | None:
    try:
        value = git_output(project, *args).strip()
    except WorkflowError:
        return None
    return value or None


def version_control_snapshot(project: Path) -> dict[str, Any]:
    branch = safe_git_output(project, "branch", "--show-current")
    head = safe_git_output(project, "rev-parse", "--short", "HEAD")
    remote_text = safe_git_output(project, "remote") or ""
    remotes = [line for line in remote_text.splitlines() if line]
    return {
        "branch": branch,
        "base_commit": head,
        "remotes": remotes,
        "remote_configured": bool(remotes),
        "suggested_commit_message": (
            f"chore: 整理 {project.name} {dt.date.today().isoformat()} 开发成果"
        ),
        "commit_created": False,
        "pushed": False,
    }


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
        documentation_failed = "documentation-contract" in failed_commands
        findings.append(
            {
                "severity": "P1",
                "title": (
                    "中文开发文档未更新"
                    if documentation_failed
                    else "项目验证未通过"
                ),
                "file": "docs/" if documentation_failed else ".mvp-ai.toml",
                "line": None,
                "description": (
                    "中文开发日志、AI 项目大纲或任务规划未满足文档契约"
                    if documentation_failed
                    else (
                        "没有配置验证命令"
                        if not validations
                        else "验证命令返回非零状态"
                    )
                ),
                "required_fix": (
                    "按要求更新 docs/devlog/YYYY-MM-DD.md、"
                    "docs/ai/PROJECT_OUTLINE.md 和 docs/ai/TASK_PLAN.md"
                    if documentation_failed
                    else "修复代码或验证环境并确保这些命令成功："
                    + ", ".join(failed_commands)
                ),
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
    path.chmod(0o700)
    return path


def call_local_coder(
    settings: Settings,
    project: Path,
    model: str,
    prompt: str,
    run_dir: Path,
    stage: str,
    live: bool = False,
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
        "--json",
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
        live=live,
        monitor=True,
        live_channel="LOCAL",
        json_events=True,
        idle_timeout=getattr(settings, "local_stall_timeout_seconds", 300),
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
        "version_control": version_control_snapshot(project),
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
        "version_control": version_control_snapshot(project),
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


def command_check_config(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        raise WorkflowError(f"项目目录不存在：{project}")
    commands = load_validation_commands(project)
    print(
        json.dumps(
            {"project": str(project), "validation_commands": len(commands)},
            ensure_ascii=False,
        )
    )
    return 0


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


def watchdog_review(run_dir: Path, stage: str, error: Exception) -> dict[str, Any]:
    message = redact_live_text(f"{type(error).__name__}: {error}")
    event = {
        "timestamp": dt.datetime.now().astimezone().isoformat(),
        "stage": stage,
        "error": message,
        "action": "codex_supervisor_takeover",
    }
    (run_dir / "watchdog.json").write_text(
        json.dumps(event, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "verdict": "fail",
        "summary": f"本地 AI 在 {stage} 阶段异常，看门狗已触发 Codex 接管。",
        "findings": [
            {
                "severity": "P1",
                "title": "本地 AI 执行异常",
                "file": "mvp-loop",
                "line": None,
                "description": message,
                "required_fix": "由 Codex 监督者根据原始计划检查现有改动并完成实现。",
            }
        ],
        "tests": [],
    }


def command_run(args: argparse.Namespace) -> int:
    settings = load_settings()
    live = bool(getattr(args, "live", False))
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
    commands = load_validation_commands(project)
    if not commands:
        raise WorkflowError(
            ".mvp-ai.toml 必须至少配置一条 validation.commands，不能在无验证条件下运行"
        )
    run_dir = make_run_dir(project)
    (run_dir / "plan.md").write_text(plan, encoding="utf-8")
    today = dt.date.today().isoformat()
    all_validations: list[dict[str, Any]] = []
    latest_validations: list[dict[str, Any]] = []
    latest_review: dict[str, Any] | None = None

    def guarded(action):
        try:
            return action()
        except Exception as exc:
            write_failure_summary(
                run_dir,
                project=project,
                model=model,
                error=exc,
                review=latest_review,
                validations=all_validations,
            )
            raise

    def validate_stage(stage: str) -> list[dict[str, Any]]:
        outcomes = run_validations(project, commands, run_dir, stage, live=live)
        documentation = validate_development_documents(
            project, run_dir, stage, today
        )
        outcomes.append(documentation)
        all_validations.extend(outcomes)
        if live:
            result = "通过" if documentation["exit_code"] == 0 else "失败"
            emit_progress("DOCS", f"中文开发文档校验{result}")
        return outcomes

    def reviewer_prompt(validations: list[dict[str, Any]]) -> str:
        return render_prompt(
            "reviewer.md",
            plan=plan,
            today=today,
            validation=json.dumps(validations, ensure_ascii=False, indent=2),
        )

    def supervisor_takeover(
        review_payload: dict[str, Any], reason: str
    ) -> int:
        nonlocal latest_review, latest_validations
        latest_review = review_payload
        print(f"切换 Codex 监督者：{reason}")
        if live:
            emit_progress("WATCHDOG", f"{reason}；Codex 接管修改")
        guarded(
            lambda: call_supervisor(
                settings,
                project,
                render_prompt(
                    "supervisor.md",
                    plan=plan,
                    today=today,
                    review=json.dumps(
                        review_payload, ensure_ascii=False, indent=2
                    ),
                ),
                run_dir,
            )
        )
        latest_validations = guarded(lambda: validate_stage("supervisor"))
        final_review = guarded(
            lambda: normalize_review(
                call_reviewer(
                    settings,
                    project,
                    reviewer_prompt(latest_validations),
                    run_dir,
                    rounds + 1,
                ),
                latest_validations,
            )
        )
        if live:
            emit_progress(
                "REVIEW",
                f"最终确认 verdict={final_review['verdict']}，"
                f"findings={len(final_review['findings'])}",
            )
        status = (
            "ready_for_user_review"
            if final_review["verdict"] == "pass"
            else "needs_manual_attention"
        )
        write_summary(
            run_dir,
            project=project,
            model=model,
            status=status,
            review=final_review,
            validations=all_validations,
        )
        if final_review["verdict"] == "pass":
            print("PASS：Codex 接管后通过验证与最终评审，等待用户评审。")
            return 0
        print("BLOCKED：Codex 接管后仍有问题，需要当前任务继续处理。")
        return 2

    print(f"run_dir={run_dir}")
    print(f"model={model}")
    if live:
        emit_progress("WORKFLOW", f"启动受监督流程；运行记录：{run_dir}")
        emit_progress("LOCAL", f"启动本地编码模型 {model}")
    try:
        ensure_model_available(model, settings.ollama_host)
        call_local_coder(
            settings,
            project,
            model,
            render_prompt("coder.md", plan=plan, today=today),
            run_dir,
            "coder-initial",
            live=live,
        )
    except Exception as exc:
        return supervisor_takeover(
            watchdog_review(run_dir, "coder-initial", exc),
            "本地 AI 启动、运行或响应异常",
        )
    if live:
        emit_progress("LOCAL", "初始编码阶段结束，开始项目验证")
    latest_validations = guarded(lambda: validate_stage("coder-initial"))

    for round_number in range(1, rounds + 1):
        if live:
            emit_progress("REVIEW", f"开始第 {round_number} 轮独立 Code Review")
        latest_review = guarded(
            lambda: normalize_review(
                call_reviewer(
                    settings,
                    project,
                    render_prompt(
                        "reviewer.md",
                        plan=plan,
                        today=today,
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
        if live:
            emit_progress(
                "REVIEW",
                f"第 {round_number} 轮 verdict={latest_review['verdict']}，"
                f"findings={len(latest_review['findings'])}",
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
            if live:
                emit_progress("LOCAL", "评审未通过，将 findings 返回本地模型修复")
            try:
                call_local_coder(
                    settings,
                    project,
                    model,
                    render_prompt(
                        "fixer.md",
                        plan=plan,
                        today=today,
                        review=json.dumps(latest_review, ensure_ascii=False, indent=2),
                    ),
                    run_dir,
                    f"local-fix-{round_number}",
                    live=live,
                )
            except Exception as exc:
                return supervisor_takeover(
                    watchdog_review(
                        run_dir, f"local-fix-{round_number}", exc
                    ),
                    "本地 AI 修复阶段异常",
                )
            latest_validations = guarded(
                lambda: validate_stage(f"local-fix-{round_number}")
            )

    assert latest_review is not None
    return supervisor_takeover(
        latest_review, "本地模型达到两轮 Code Review 失败阈值"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="本地 AI coding + Codex review 的受监督 MVP 工作流"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="检查 Codex、Ollama、Git 与本地模型")
    doctor.set_defaults(func=command_doctor)

    check_config = subparsers.add_parser(
        "check-config", help="检查项目是否配置了真实验证命令"
    )
    check_config.add_argument("--project", required=True)
    check_config.set_defaults(func=command_check_config)

    init = subparsers.add_parser("init", help="初始化目标 Git 项目的 AI 工作流文件")
    init.add_argument("--project", required=True)
    init.set_defaults(func=command_init)

    run = subparsers.add_parser("run", help="执行 coding、review、修复循环")
    run.add_argument("--project", required=True)
    run.add_argument("--plan", required=True)
    run.add_argument("--model", default="primary", help="模型别名或 Ollama 模型 ID")
    run.add_argument("--allow-dirty", action="store_true")
    run.add_argument(
        "--live",
        action="store_true",
        help="实时显示经过脱敏的模型与工作流事件；完整原始输出仍写入日志",
    )
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
