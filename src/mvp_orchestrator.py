#!/usr/bin/env python3
"""Orchestrate local Codex coding and independent cloud Codex review loops."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import selectors
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULTS_PATH = ROOT / "config" / "defaults.toml"
MODELS_PATH = ROOT / "config" / "models.toml"
REVIEW_SCHEMA = ROOT / "schemas" / "review.schema.json"
PROMPTS = ROOT / "prompts"
OPENCODE_AGENT_TEMPLATE = (
    ROOT / "integrations" / "opencode" / "agents" / "local-mvp-coder.md"
)

SENSITIVE_FIELD_FRAGMENT = (
    r"(?:(?!(?:input|output|cached[_-]?input|reasoning[_-]?output|total|soft)"
    r"[_-]?tokens?(?:\b|_))[a-z0-9_-]*token[a-z0-9_-]*|"
    r"auths?|auth[_-]?config|"
    r"[a-z0-9_-]*(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"refresh[_-]?token|id[_-]?token|session[_-]?token|csrf[_-]?token|"
    r"password|passwd|secret|client[_-]?secret|private[_-]?key|credential|"
    r"credentials|authorization|cookie)[a-z0-9_-]*)"
)
SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?P<prefix>(?<![a-z0-9_-])[\"']?"
    + SENSITIVE_FIELD_FRAGMENT
    + r"[\"']?\s*[:=]\s*)"
    r"(?P<value>(?![ \t]*[\"']?\[REDACTED\])[^\r\n]+)"
)
INLINE_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?P<prefix>`[^`\r\n]*?(?<![a-z0-9_-])[\"']?"
    + SENSITIVE_FIELD_FRAGMENT
    + r"[\"']?\s*[:=]\s*)(?P<value>[^`\r\n]+)(?P<suffix>`)"
)
YAML_MULTILINE_SECRET = re.compile(
    r"(?i)^(?P<indent>[ \t]*)(?P<key>[\"']?"
    + SENSITIVE_FIELD_FRAGMENT
    + r"[\"']?)(?P<separator>\s*:\s*)"
    r"(?P<value>(?:[>|][+-]?)?\s*(?:#.*)?)$"
)
STRUCTURED_SECRET_START = re.compile(
    r"(?i)(?P<prefix>(?<![a-z0-9_-])[\"']?"
    + SENSITIVE_FIELD_FRAGMENT
    + r"[\"']?\s*[:=]\s*)(?P<open>[{\[])"
)
KNOWN_TOKEN = re.compile(
    r"\b(sk-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9_]{10,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{10,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|AIza[A-Za-z0-9_-]{20,}|"
    r"(?:AKIA|ASIA)[0-9A-Z]{12,}|"
    r"Bearer\s+[A-Za-z0-9._~+/=-]{10,})\b",
    re.IGNORECASE,
)
PRIVATE_KEY_BEGIN = re.compile(
    r"-----BEGIN (?P<label>[A-Z0-9 ]*PRIVATE KEY[A-Z0-9 ]*)-----",
    re.IGNORECASE,
)
PRIVATE_REMOTE = re.compile(
    r"(?i)(?:\b(?:https?|ssh|git)://[^\s\"'<>]+|"
    r"\b[a-z0-9._-]+@[a-z0-9.-]+:[^\s\"'<>]+)"
)
SENSITIVE_JSON_KEY = re.compile(r"(?i)^" + SENSITIVE_FIELD_FRAGMENT + r"$")
SENSITIVE_CLI_OPTION = re.compile(
    r"(?i)^-{1,2}(?:auth|auths|auth[_-]?config|api[_-]?key|access[_-]?token|auth[_-]?token|token|"
    r"refresh[_-]?token|id[_-]?token|password|passwd|secret|"
    r"client[_-]?secret|private[_-]?key|credential|credentials|"
    r"authorization|proxy[_-]?authorization|cookie|set[_-]?cookie)$"
)
SENSITIVE_CLI_PAIR = re.compile(
    r"(?i)(?P<prefix>(?<![a-z0-9_-])-{1,2}(?:auth|auths|auth[_-]?config|api[_-]?key|"
    r"access[_-]?token|auth[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"token|password|passwd|secret|client[_-]?secret|private[_-]?key|"
    r"credential|credentials|authorization|proxy[_-]?authorization|"
    r"cookie|set[_-]?cookie)\s+)(?P<value>"
    r"(?![ \t]*[\"']?\[REDACTED\])[^\r\n]+?)(?=\s+-{1,2}[a-z0-9]|$)"
)
INLINE_SENSITIVE_CLI_PAIR = re.compile(
    r"(?i)(?P<prefix>`[^`\r\n]*?-{1,2}(?:auth|auths|auth[_-]?config|api[_-]?key|access[_-]?token|"
    r"auth[_-]?token|refresh[_-]?token|id[_-]?token|token|password|"
    r"passwd|secret|client[_-]?secret|private[_-]?key|credential|"
    r"credentials|authorization|proxy[_-]?authorization|cookie|"
    r"set[_-]?cookie)\s+)(?P<value>[^`\r\n]+)(?P<suffix>`)"
)
TOKENS_USED = re.compile(r"(?im)\btokens used\s*[:=]?\s*([0-9][0-9,]*)")
RISK_LINE = re.compile(
    r"(?i)^[ ]{0,3}(?:risk classification|风险分类)\s*[:：-]\s*"
    r"(low|medium|high|低|中|高)\s*$"
)
RISK_LIKE_LINE = re.compile(
    r"(?i)^[ ]{0,3}(?:risk classification|风险分类)\s*[:：-]"
)
HTML_BLOCK_TAGS = (
    "address|article|aside|base|basefont|blockquote|body|caption|center|col|"
    "colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|"
    "footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|"
    "li|link|main|menu|menuitem|nav|noframes|ol|optgroup|option|p|param|"
    "search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul"
)
HTML_BLOCK_START = re.compile(
    rf"^[ ]{{0,3}}</?(?:{HTML_BLOCK_TAGS})(?:\s|/?>|$)", re.IGNORECASE
)
HTML_COMPLETE_TAG_LINE = re.compile(
    r"^[ ]{0,3}</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>[ \t]*$"
)
BASELINE_SECRET_EXCLUDES = (
    ".git",
    ".aws",
    ".ssh",
    ".gnupg",
    ".env",
    ".env.*",
    ".envrc",
    "*.env",
    ".direnv",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    ".git/config",
    ".git/config.worktree",
    ".git/worktrees",
    ".git/modules/*/config",
    "**/.git/config",
    "**/.git/config.worktree",
    "**/.git/worktrees",
    "**/.git/modules/*/config",
    ".docker/config.json",
    "**/.docker/config.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "credentials",
    "credentials.*",
    "*credential*.json",
    "*credential*.toml",
    "*credential*.yaml",
    "*credential*.yml",
    "*credential*.ini",
    "service-account*.json",
    "service_account*.json",
    "*secret*.json",
    "*secret*.toml",
    "*secret*.yaml",
    "*secret*.yml",
    "*secret*.ini",
)

# OpenCode merges a repository's ``opencode.json`` and ``.opencode`` directory
# after the private runtime configuration. Those files can define MCP servers,
# plugins, or replacement agents, so they must never be visible in the
# disposable workspace used by the supervised OpenCode route. This is kept
# separate from ``BASELINE_SECRET_EXCLUDES``: ordinary baseline validation
# should still exercise a project's normal files, while only the implementation
# Agent needs this OpenCode-specific isolation.
OPENCODE_PROJECT_CONTROL_EXCLUDES = (
    "opencode.json",
    "opencode.jsonc",
    ".opencode",
)


class WorkflowError(RuntimeError):
    pass


class BaselineCleanupError(WorkflowError):
    pass


class ProjectChangedError(WorkflowError):
    pass


class EvidenceRedactionError(WorkflowError):
    def __init__(self, message: str, *, returncode: int | None = None):
        super().__init__(message)
        self.returncode = returncode


class ValidationCleanupError(WorkflowError):
    def __init__(
        self,
        message: str,
        *,
        outcomes: list[dict[str, Any]] | None = None,
    ):
        super().__init__(message)
        self.outcomes = list(outcomes or [])


class CommandError(WorkflowError):
    def __init__(self, message: str, *, returncode: int, log_path: Path):
        super().__init__(message)
        self.returncode = returncode
        self.log_path = log_path


class StructuredResultError(WorkflowError):
    """The child exited successfully but its required artifact was malformed."""

    returncode = 0


@dataclass(frozen=True)
class Settings:
    max_local_review_rounds: int
    strategy: str
    severe_finding_threshold: int
    coder_timeout_seconds: int
    local_stall_timeout_seconds: int
    review_timeout_seconds: int
    supervisor_timeout_seconds: int
    require_clean_worktree: bool
    context_capsule_max_bytes: int
    codex_command: str
    ollama_host: str
    local_backend: str
    opencode_command: str
    opencode_agent: str
    opencode_provider: str
    opencode_temperature: float
    opencode_low_max_steps: int
    opencode_medium_max_steps: int
    deepseek_model: str
    deepseek_base_url: str
    deepseek_api_key_env: str
    cloud_provider: str
    cloud_model: str
    cloud_reasoning_effort: str
    cloud_max_calls_per_run: int
    cloud_soft_token_budget: int | None


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_settings() -> Settings:
    raw = load_toml(DEFAULTS_PATH)
    workflow = raw["workflow"]
    runtime = raw["runtime"]
    local = raw.get("local", {})
    cloud = raw["cloud"]
    deepseek = raw.get("local", {}).get("deepseek", {})
    return Settings(
        max_local_review_rounds=int(workflow["max_local_review_rounds"]),
        strategy=str(workflow.get("strategy", "adaptive")),
        severe_finding_threshold=int(workflow.get("severe_finding_threshold", 5)),
        coder_timeout_seconds=int(workflow["coder_timeout_seconds"]),
        local_stall_timeout_seconds=int(workflow["local_stall_timeout_seconds"]),
        review_timeout_seconds=int(workflow["review_timeout_seconds"]),
        supervisor_timeout_seconds=int(workflow["supervisor_timeout_seconds"]),
        require_clean_worktree=bool(workflow["require_clean_worktree"]),
        context_capsule_max_bytes=int(
            workflow.get("context_capsule_max_bytes", 16384)
        ),
        codex_command=str(runtime["codex_command"]),
        ollama_host=str(runtime["ollama_host"]),
        local_backend=str(local.get("backend", "opencode")),
        opencode_command=str(local.get("opencode_command", "opencode")),
        opencode_agent=str(local.get("opencode_agent", "local-mvp-coder")),
        opencode_provider=str(local.get("opencode_provider", "ollama")),
        opencode_temperature=float(local.get("opencode_temperature", 0.1)),
        opencode_low_max_steps=int(local.get("opencode_low_max_steps", 10)),
        opencode_medium_max_steps=int(
            local.get("opencode_medium_max_steps", 16)
        ),
        deepseek_model=str(deepseek.get("model", "deepseek-v4-pro")),
        deepseek_base_url=str(deepseek.get("base_url", "https://api.deepseek.com")),
        deepseek_api_key_env=str(deepseek.get("api_key_env", "DEEPSEEK_API_KEY")),
        cloud_provider=str(cloud["provider"]),
        cloud_model=str(cloud["model"]),
        cloud_reasoning_effort=str(cloud["reasoning_effort"]),
        cloud_max_calls_per_run=int(cloud.get("max_calls_per_run", 3)),
        cloud_soft_token_budget=(
            int(cloud.get("soft_token_budget", 0)) or None
        ),
    )


def render_prompt(name: str, **values: str) -> str:
    text = (PROMPTS / name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key.upper() + "}}", value)
    unresolved = sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", text)))
    if unresolved:
        raise WorkflowError(
            f"提示词模板 {name} 存在未解析占位符：{', '.join(unresolved)}"
        )
    return text


def _redact_assignment(match: re.Match[str]) -> str:
    value = match.group("value")
    if len(value) >= 2 and value[0] in {"\"", "'"} and value[-1] == value[0]:
        replacement = value[0] + "[REDACTED]" + value[0]
    else:
        replacement = "[REDACTED]"
    return match.group("prefix") + replacement


def _redact_inline_value(match: re.Match[str]) -> str:
    return match.group("prefix") + "[REDACTED]" + match.group("suffix")


def _redact_yaml_multiline_values(text: str) -> str:
    """Replace sensitive YAML block/nested values without retaining their body."""
    lines = text.splitlines(keepends=True)
    rendered: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        content = line.rstrip("\r\n")
        newline = line[len(content) :]
        match = YAML_MULTILINE_SECRET.match(content)
        if not match:
            rendered.append(line)
            index += 1
            continue
        value = match.group("value").strip()
        if value and not value.startswith(("|", ">", "#")):
            rendered.append(line)
            index += 1
            continue
        parent_indent = len(match.group("indent").expandtabs(8))
        rendered.append(
            match.group("indent")
            + match.group("key")
            + match.group("separator")
            + "\"[REDACTED]\""
            + newline
        )
        index += 1
        while index < len(lines):
            nested = lines[index].rstrip("\r\n")
            if not nested.strip():
                index += 1
                continue
            raw_indent = nested[: len(nested) - len(nested.lstrip(" \t"))]
            indentation = len(raw_indent.expandtabs(8))
            if indentation <= parent_indent:
                break
            index += 1
    return "".join(rendered)


def _redact_structured_subtrees(text: str) -> str:
    """Replace object/list values assigned to sensitive fields as one subtree."""
    rendered: list[str] = []
    cursor = 0
    while True:
        match = STRUCTURED_SECRET_START.search(text, cursor)
        if not match:
            rendered.append(text[cursor:])
            break
        opening = match.group("open")
        expected = "}" if opening == "{" else "]"
        stack = [expected]
        quote: str | None = None
        escaped = False
        end = match.end("open")
        while end < len(text) and stack:
            character = text[end]
            if quote is not None:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = None
            elif character in {"\"", "'"}:
                quote = character
            elif character == "{":
                stack.append("}")
            elif character == "[":
                stack.append("]")
            elif character in {"}", "]"}:
                if character == stack[-1]:
                    stack.pop()
                else:
                    break
            end += 1
        rendered.append(text[cursor : match.start("open")])
        rendered.append('"[REDACTED]"')
        if stack:
            # A malformed sensitive subtree has no trustworthy resumption
            # boundary. Drop the remainder rather than leaking later lines.
            cursor = len(text)
        else:
            cursor = end
    return "".join(rendered)


def _redact_private_key_blocks(text: str) -> str:
    """Redact complete and truncated PEM private keys conservatively."""
    rendered: list[str] = []
    cursor = 0
    while match := PRIVATE_KEY_BEGIN.search(text, cursor):
        rendered.append(text[cursor : match.start()])
        rendered.append("[REDACTED PRIVATE KEY]")
        label = re.escape(match.group("label"))
        ending = re.compile(
            rf"-----END {label}-----", re.IGNORECASE
        ).search(text, match.end())
        if ending is None:
            # A malformed or truncated key has no trustworthy resumption
            # boundary. Drop the remainder instead of persisting key material.
            cursor = len(text)
            break
        cursor = ending.end()
    rendered.append(text[cursor:])
    return "".join(rendered)


def redact_live_text(text: str, limit: int = 600) -> str:
    """Redact common credential forms before displaying child-process events."""
    clean = redact_sensitive_text(text).strip()
    if len(clean) > limit:
        return clean[:limit].rstrip() + "…"
    return clean


def redact_sensitive_text(text: str) -> str:
    """Redact secrets from persistent evidence without silently truncating it."""
    clean = _redact_private_key_blocks(text)
    clean = _redact_yaml_multiline_values(clean)
    clean = _redact_structured_subtrees(clean)
    # Preserve prose after inline-code examples while removing the complete
    # value inside the backticks. The generic fallbacks below handle raw logs.
    clean = INLINE_SENSITIVE_CLI_PAIR.sub(_redact_inline_value, clean)
    clean = INLINE_SECRET_ASSIGNMENT.sub(_redact_inline_value, clean)
    clean = SENSITIVE_CLI_PAIR.sub(_redact_assignment, clean)
    clean = SECRET_ASSIGNMENT.sub(_redact_assignment, clean)
    clean = KNOWN_TOKEN.sub("[REDACTED]", clean)
    clean = PRIVATE_REMOTE.sub("[REDACTED PRIVATE REMOTE]", clean)
    return clean


def sanitize_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, list):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            sanitized[text_key] = (
                "[REDACTED]"
                if is_sensitive_json_key(text_key)
                else sanitize_json_value(item)
            )
        return sanitized
    return value


def is_sensitive_json_key(key: str) -> bool:
    """Recognize credential fields without treating Token telemetry as secret."""
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    normalized = normalized.replace("-", "_").lower()
    telemetry = {
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "reasoning_output_tokens",
        "total_tokens",
        "soft_token_budget",
        "cloud_token_difference",
    }
    return normalized not in telemetry and bool(
        SENSITIVE_JSON_KEY.fullmatch(normalized)
    )


def sanitize_command(command: list[str]) -> list[str]:
    """Redact command evidence, including multi-argument secret values."""
    sanitized: list[str] = []
    redact_next = False
    for argument in command:
        if redact_next:
            # A shell-like evidence string cannot prove where an unquoted
            # multiword credential ends. Redact positional arguments until the
            # next option; real argv callers normally supply a single value.
            if not str(argument).startswith("-"):
                sanitized.append("[REDACTED]")
                continue
            redact_next = False
        cleaned = redact_sensitive_text(str(argument))
        sanitized.append(cleaned)
        if SENSITIVE_CLI_OPTION.fullmatch(str(argument)):
            redact_next = True
    return sanitized


def redact_persistent_output(text: str) -> str:
    """Redact plain text and JSONL while preserving parseable JSON events."""
    try:
        whole_value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    else:
        suffix = "\n" if text.endswith("\n") else ""
        return json.dumps(
            sanitize_json_value(whole_value),
            ensure_ascii=False,
            separators=(",", ":"),
        ) + suffix
    prepared = _redact_private_key_blocks(text)
    prepared = _redact_yaml_multiline_values(prepared)
    prepared = _redact_structured_subtrees(prepared)
    rendered: list[str] = []
    for line in prepared.splitlines():
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            rendered.append(redact_sensitive_text(line))
        else:
            rendered.append(
                json.dumps(
                    sanitize_json_value(value),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
    suffix = "\n" if prepared.endswith("\n") else ""
    return "\n".join(rendered) + suffix


def redact_persisted_file(path: Path) -> None:
    """Redact child-tool evidence and fail closed if an existing file is unsafe."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            redacted = redact_persistent_output(text)
        else:
            redacted = json.dumps(
                sanitize_json_value(value), ensure_ascii=False, indent=2
            )
        atomic_write_text(path, redacted)
    except FileNotFoundError:
        return
    except Exception as exc:
        try:
            if path.exists() or path.is_symlink():
                if hasattr(os, "chflags") and not path.is_symlink():
                    os.chflags(path, 0, follow_symlinks=False)
                if not path.is_symlink():
                    path.chmod(0o600)
                path.unlink()
        except OSError:
            pass
        raise EvidenceRedactionError(f"证据文件脱敏失败：{path}") from exc


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically write private evidence without following destination links."""
    if path.is_symlink():
        raise WorkflowError(f"拒绝写入符号链接证据文件：{path}")
    if path.exists() and not path.is_file():
        raise WorkflowError(f"证据目标不是普通文件：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise WorkflowError(f"拒绝替换符号链接证据文件：{path}")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _usage_number(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def parse_token_usage(output: str) -> dict[str, Any]:
    """Parse one invocation's usage, preferring the last complete JSON event.

    Repeated ``turn.completed`` lines are treated as retransmission and never
    summed. The legacy ``tokens used`` footer is a total-only lower bound.
    """
    lines = output.splitlines()
    command_header = next(
        (line for line in lines[:5] if line.startswith("command: ")), None
    )
    command_parts: list[Any] = []
    if command_header is not None:
        try:
            parsed_command = json.loads(command_header.removeprefix("command: "))
            if isinstance(parsed_command, list):
                command_parts = parsed_command
        except json.JSONDecodeError:
            pass
    explicit_json_events = "--json" in command_parts or any(
        command_parts[index : index + 2] == ["--format", "json"]
        for index in range(len(command_parts) - 1)
    )
    json_events_expected = command_header is None or explicit_json_events
    completed: list[Any] = []
    opencode_steps: list[dict[str, Any]] = []
    saw_json_event = False
    for line in lines if json_events_expected else []:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        saw_json_event = True
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.completed":
            completed.append(event.get("usage"))
        if event.get("type") == "step_finish":
            part = event.get("part")
            if isinstance(part, dict):
                opencode_steps.append(part)
    if completed:
        usage = completed[-1]
        if isinstance(usage, dict):
            input_tokens = _usage_number(usage.get("input_tokens"))
            output_tokens = _usage_number(usage.get("output_tokens"))
            optional_fields = ("cached_input_tokens", "reasoning_output_tokens")
            optional_valid = all(
                field not in usage or _usage_number(usage[field]) is not None
                for field in optional_fields
            )
            if (
                input_tokens is not None
                and output_tokens is not None
                and optional_valid
            ):
                return {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": _usage_number(
                        usage.get("cached_input_tokens")
                    ),
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": _usage_number(
                        usage.get("reasoning_output_tokens")
                    ),
                    "total_tokens": input_tokens + output_tokens,
                    "measurement": "exact",
                }
        return unavailable_usage()
    if opencode_steps:
        totals = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": 0,
        }
        for part in opencode_steps:
            tokens = part.get("tokens")
            if not isinstance(tokens, dict):
                return unavailable_usage()
            input_tokens = _usage_number(tokens.get("input"))
            output_tokens = _usage_number(tokens.get("output"))
            reasoning_tokens = _usage_number(tokens.get("reasoning", 0))
            total_tokens = _usage_number(tokens.get("total"))
            cache = tokens.get("cache", {})
            if not isinstance(cache, dict):
                return unavailable_usage()
            cached_tokens = _usage_number(cache.get("read", 0))
            if None in (
                input_tokens,
                output_tokens,
                reasoning_tokens,
                total_tokens,
                cached_tokens,
            ):
                return unavailable_usage()
            if total_tokens < input_tokens + output_tokens:
                return unavailable_usage()
            totals["input_tokens"] += input_tokens
            totals["cached_input_tokens"] += cached_tokens
            totals["output_tokens"] += output_tokens
            totals["reasoning_output_tokens"] += reasoning_tokens
            totals["total_tokens"] += total_tokens
        return {**totals, "measurement": "exact"}
    if explicit_json_events or saw_json_event:
        return unavailable_usage()
    matches = TOKENS_USED.findall(output)
    if matches:
        total = int(matches[-1].replace(",", ""))
        return {
            "input_tokens": None,
            "cached_input_tokens": None,
            "output_tokens": None,
            "reasoning_output_tokens": None,
            "total_tokens": total,
            "measurement": "partial",
        }
    return unavailable_usage()


def unavailable_usage() -> dict[str, Any]:
    return {
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "reasoning_output_tokens": None,
        "total_tokens": None,
        "measurement": "unavailable",
    }


def usage_stage(
    *,
    stage: str,
    backend: str,
    role: str,
    output: str,
    elapsed_seconds: float,
    exit_code: int,
) -> dict[str, Any]:
    record = {
        "stage": stage,
        "backend": backend,
        "role": role,
        **parse_token_usage(output),
        "elapsed_seconds": round(elapsed_seconds, 2),
        "exit_code": exit_code,
    }
    return record


def summarize_usage(
    stages: list[dict[str, Any]],
    *,
    soft_budget: int | None,
    max_cloud_calls: int,
) -> dict[str, Any]:
    def measured_total(selected: list[dict[str, Any]]) -> int | None:
        totals = [item.get("total_tokens") for item in selected]
        known = [value for value in totals if isinstance(value, int)]
        return sum(known) if known else None

    local = [item for item in stages if item.get("backend") == "local"]
    review = [item for item in stages if item.get("backend") == "cloud-review"]
    supervisor = [
        item for item in stages if item.get("backend") == "cloud-supervisor"
    ]
    cloud = review + supervisor
    cloud_lower_bound = measured_total(cloud)
    complete = bool(stages) and all(
        item.get("measurement") == "exact" for item in stages
    )
    if soft_budget is None:
        budget_status = "not_configured"
    elif not cloud:
        budget_status = "unknown"
    elif cloud_lower_bound is not None and cloud_lower_bound > soft_budget:
        budget_status = "exceeded"
    elif any(item.get("measurement") == "unavailable" for item in cloud):
        budget_status = "unknown"
    elif complete:
        budget_status = "within_budget"
    else:
        budget_status = "unknown"
    return {
        "stages": stages,
        "local_total": measured_total(local),
        "cloud_review_total": measured_total(review),
        "cloud_supervisor_total": measured_total(supervisor),
        "cloud_lower_bound": cloud_lower_bound,
        "total_measured": measured_total(stages),
        "measurement_complete": complete,
        "budget": {
            "soft_token_budget": soft_budget,
            "max_cloud_calls": max_cloud_calls,
            "status": budget_status,
        },
    }


def _visible_markdown_without_html_blocks(plan: str) -> str:
    """Remove CommonMark raw HTML blocks before parsing control metadata."""
    visible = re.sub(r"<!--.*?(?:-->|$)", "", plan, flags=re.DOTALL)
    visible = re.sub(r"<!\[CDATA\[.*?(?:\]\]>|$)", "", visible, flags=re.DOTALL)
    visible = re.sub(r"<\?.*?(?:\?>|$)", "", visible, flags=re.DOTALL)
    visible = re.sub(
        r"<![A-Z].*?(?:>|$)", "", visible, flags=re.DOTALL
    )
    visible = re.sub(
        r"<(?P<tag>script|pre|style|textarea|template)\b[^>]*>.*?"
        r"(?:</(?P=tag)\s*>|$)",
        "",
        visible,
        flags=re.DOTALL | re.IGNORECASE,
    )

    rendered: list[str] = []
    in_blank_terminated_block = False
    for line in visible.split("\n"):
        if in_blank_terminated_block:
            rendered.append("")
            if not line.strip():
                in_blank_terminated_block = False
            continue
        if HTML_BLOCK_START.match(line) or HTML_COMPLETE_TAG_LINE.fullmatch(line):
            in_blank_terminated_block = True
            rendered.append("")
            continue
        rendered.append(line)
    return "\n".join(rendered)


def parse_risk_classification(plan: str) -> dict[str, Any]:
    matches: list[str] = []
    invalid_control_line = False
    fence_character: str | None = None
    fence_length = 0
    visible_plan = _visible_markdown_without_html_blocks(plan)
    # Markdown line boundaries are LF/CRLF; Python splitlines() also treats
    # form-feed and vertical-tab as lines, which could expose hidden claims.
    for line in visible_plan.split("\n"):
        fence_match = re.match(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$", line)
        if fence_match:
            marker = fence_match.group(1)
            remainder = fence_match.group(2)
            if fence_character is None:
                fence_character = marker[0]
                fence_length = len(marker)
            elif (
                marker[0] == fence_character
                and len(marker) >= fence_length
                and not remainder.strip()
            ):
                fence_character = None
                fence_length = 0
            continue
        if fence_character is not None:
            continue
        # Four-column indentation is a Markdown code block. Tabs advance to
        # the next four-column stop, while 0-3 spaces remain visible prose.
        indentation = 0
        for character in line:
            if character == " ":
                indentation += 1
            elif character == "\t":
                indentation += 4 - (indentation % 4)
            else:
                break
        if indentation >= 4:
            continue
        match = RISK_LINE.fullmatch(line)
        if match:
            matches.append(match.group(1))
        elif RISK_LIKE_LINE.match(line):
            invalid_control_line = True
    aliases = {"低": "low", "中": "medium", "高": "high"}
    declared = (
        aliases.get(matches[0], matches[0]).lower()
        if len(matches) == 1 and not invalid_control_line
        else None
    )
    if invalid_control_line:
        reason = "ambiguous_invalid_declaration_defaults_to_high"
    elif len(matches) > 1:
        reason = "ambiguous_multiple_declarations_defaults_to_high"
    elif not matches:
        reason = "undeclared_defaults_to_high"
    else:
        reason = "plan"
    return {
        "classification": declared or "high",
        "declared": declared is not None,
        "reason": reason,
    }


def classify_findings(review: dict[str, Any]) -> dict[str, int]:
    counts = {severity: 0 for severity in ("P0", "P1", "P2", "P3")}
    for item in review.get("findings", []):
        if isinstance(item, dict) and item.get("severity") in counts:
            counts[str(item["severity"])] += 1
    return counts


def decide_review_action(
    review: dict[str, Any], *, severe_threshold: int = 5
) -> str:
    findings = [item for item in review.get("findings", []) if isinstance(item, dict)]
    if any(
        item.get("category")
        in {"security", "data_loss", "reliability", "other"}
        for item in findings
    ):
        return "supervisor_takeover"
    if review.get("verdict") == "pass":
        return "ready"
    counts = classify_findings(review)
    blocking = counts["P0"] + counts["P1"] + counts["P2"]
    if blocking == 0:
        return (
            "ready"
            if findings
            and all(item.get("severity") == "P3" for item in findings)
            else "supervisor_takeover"
        )
    if (
        counts["P0"]
        or counts["P1"]
        or blocking >= severe_threshold
        or any(
            item.get("category")
            in {"security", "data_loss", "reliability", "other"}
            for item in findings
        )
    ):
        return "supervisor_takeover"
    if 0 < blocking <= 4 and all(
        item.get("severity") in {"P2", "P3"}
        and item.get("category")
        in {"correctness", "documentation", "performance", "testing"}
        and item.get("file")
        and item.get("required_fix")
        for item in findings
    ):
        return "local_fix"
    return "supervisor_takeover"


def validation_counts(outcomes: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "environment_blocked": 0, "timed_out": 0}
    for item in outcomes:
        code = item.get("exit_code")
        if code == 0:
            counts["passed"] += 1
        elif code == 124:
            counts["timed_out"] += 1
        elif item.get("classification") == "environment":
            counts["environment_blocked"] += 1
        else:
            counts["failed"] += 1
    return counts


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
    if not isinstance(event, dict):
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
        if not isinstance(usage, dict):
            usage = {}
        emit_progress(
            channel,
            "本轮完成"
            f"（输入 {usage.get('input_tokens', '?')} tokens，"
            f"输出 {usage.get('output_tokens', '?')} tokens）",
        )
        return
    if event_type in {"error", "turn.failed"}:
        error = event.get("error")
        nested_message = error.get("message") if isinstance(error, dict) else None
        message = event.get("message") or nested_message or "模型运行失败"
        emit_progress(channel, "运行提示：" + str(message))
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


def display_opencode_event(line: str, channel: str) -> None:
    """Render safe summaries from ``opencode run --format json`` events."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    if not isinstance(event, dict):
        return
    event_type = str(event.get("type", ""))
    part = event.get("part")
    if not isinstance(part, dict):
        part = {}
    if event_type == "step_start":
        emit_progress(channel, "OpenCode Agent 开始下一步")
    elif event_type == "step_finish":
        tokens = part.get("tokens")
        if not isinstance(tokens, dict):
            tokens = {}
        emit_progress(
            channel,
            "OpenCode 步骤结束"
            f"（输入 {tokens.get('input', '?')} tokens，"
            f"输出 {tokens.get('output', '?')} tokens）",
        )
    elif event_type == "tool_use":
        tool = str(part.get("tool", "tool"))
        state = part.get("state")
        if not isinstance(state, dict):
            state = {}
        status = str(state.get("status", "unknown"))
        emit_progress(channel, f"OpenCode 工具：{tool}（{status}）")
    elif event_type == "error":
        message = event.get("message") or part.get("message") or "运行失败"
        emit_progress(channel, "OpenCode 运行提示：" + str(message))
    elif event_type == "text" and part.get("synthetic") is True:
        summary = part.get("text")
        if isinstance(summary, str) and summary.strip():
            emit_progress(channel, "OpenCode 摘要：" + summary)


def is_progress_event(line: str, json_events: bool) -> bool:
    if not json_events:
        return True
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return False
    if not isinstance(event, dict):
        return False
    return str(event.get("type", "")) in {
        "thread.started",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "error",
        "item.started",
        "item.updated",
        "item.completed",
        "step_start",
        "tool_use",
        "step_finish",
        "text",
    }


def display_json_event(line: str, channel: str) -> None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        display_codex_event(line, channel)
        return
    if isinstance(event, dict) and event.get("type") in {
        "step_start", "tool_use", "step_finish", "text"
    }:
        display_opencode_event(line, channel)
    else:
        display_codex_event(line, channel)


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
                        display_json_event(line, channel)
                    else:
                        emit_progress(channel, line)
            if process.poll() is not None:
                tail = process.stdout.read()
                if tail:
                    output.append(tail)
                    if display:
                        for line in tail.splitlines():
                            if json_events:
                                display_json_event(line, channel)
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
    extra_env: dict[str, str] | None = None,
    command_for_log: list[str] | None = None,
    clean_environment: bool = False,
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    env = (
        clean_child_environment()
        if clean_environment
        else os.environ.copy()
    )
    env.setdefault("NO_COLOR", "1")
    loopback = "localhost,127.0.0.1,::1"
    env["NO_PROXY"] = loopback
    env["no_proxy"] = loopback
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra_env or {})
    logged_command = command_for_log or command
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
        # ``command_for_log`` is deliberately used for subprocess errors as
        # well as evidence.  A caller may send an approved prompt over stdin
        # (rather than argv); retaining the original exception command would
        # otherwise make it too easy for a future invocation change to leak a
        # sensitive positional argument through watchdog.json.
        if command_for_log is not None:
            safe_command = sanitize_command(logged_command)
            exc.cmd = safe_command
            exc.args = (safe_command, exc.timeout)
            setattr(exc, "mvp_safe_command", safe_command)
        elapsed = time.monotonic() - started
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        header = (
            f"command: {json.dumps(sanitize_command(logged_command), ensure_ascii=False)}\n"
            f"exit_code: 124\n"
            f"elapsed_seconds: {elapsed:.2f}\n"
            f"error: timeout\n\n"
        )
        try:
            atomic_write_text(
                log_path,
                redact_sensitive_text(header) + redact_persistent_output(output),
            )
        except Exception as evidence_error:
            evidence_error.returncode = 124
            raise
        exc.returncode = 124
        raise
    elapsed = time.monotonic() - started
    header = (
        f"command: {json.dumps(sanitize_command(logged_command), ensure_ascii=False)}\n"
        f"exit_code: {result.returncode}\n"
        f"elapsed_seconds: {elapsed:.2f}\n\n"
    )
    try:
        atomic_write_text(
            log_path,
            redact_sensitive_text(header)
            + redact_persistent_output(result.stdout or ""),
        )
    except Exception as exc:
        try:
            exc.returncode = result.returncode
        except Exception:
            pass
        raise
    if check and result.returncode != 0:
        raise CommandError(
            f"命令失败（exit={result.returncode}），详情见 {log_path}",
            returncode=result.returncode,
            log_path=log_path,
        )
    return result


def clean_child_environment() -> dict[str, str]:
    """Return a non-secret environment for a local untrusted Agent process."""
    path_entries = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    for executable in (
        "git", "python3", "node", "npm", "pnpm", "uv", "cargo", "rustc"
    ):
        resolved = shutil.which(executable)
        if resolved:
            path_entries.append(str(Path(resolved).resolve().parent))
    unique_paths = list(dict.fromkeys(path_entries))
    env = {"PATH": os.pathsep.join(unique_paths)}
    for key in ("LANG", "LC_ALL", "TZ", "TERM"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


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


def git_bytes(project: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=project, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if result.returncode != 0:
        raise WorkflowError(result.stderr.decode(errors="replace").strip() or "Git 命令失败")
    return result.stdout


def acquire_project_run_lock(project: Path) -> tuple[int, Path]:
    """Acquire a non-blocking external lock shared by all builder frontends."""
    state_root = Path(
        os.environ.get(
            "LOCAL_AI_MVP_STATE_DIR",
            str(Path.home() / ".local/share/local-ai-mvp-builder"),
        )
    ).expanduser()
    if not state_root.is_absolute():
        state_root = Path.cwd() / state_root
    if state_root.is_symlink():
        raise WorkflowError("运行记录目录不能是符号链接")
    state_root = state_root.resolve(strict=False)
    locks_candidate = state_root / "locks"
    if locks_candidate.is_symlink():
        raise WorkflowError("项目锁目录不能是符号链接")
    locks_root = locks_candidate.resolve(strict=False)
    project_root = project.resolve(strict=False)
    if state_root.is_relative_to(project_root) or locks_root.is_relative_to(
        project_root
    ):
        raise WorkflowError("项目锁目录必须位于目标 workspace 之外")
    locks_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    locks_root.chmod(0o700)
    digest = hashlib.sha256(str(project.resolve(strict=False)).encode()).hexdigest()
    lock_path = locks_root / f"{digest}.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise WorkflowError(f"无法安全打开项目锁：{lock_path}") from exc
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"pid={os.getpid()}\n".encode())
        os.fsync(descriptor)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise WorkflowError("该项目已有 Local AI MVP Builder 运行中") from exc
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, lock_path


def release_project_run_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def project_integrity_snapshot(project: Path) -> dict[str, str]:
    """Capture the model-relevant Git/config state without storing file names."""
    status = git_output(
        project, "status", "--porcelain=v1", "-z", "-uall"
    )
    config = project / ".mvp-ai.toml"
    config_hash = file_sha256(config) if config.is_file() else "missing"
    try:
        head = git_output(project, "rev-parse", "--verify", "HEAD").strip()
    except WorkflowError:
        head = "unborn"
    return {
        "head": head,
        "porcelain_sha256": hashlib.sha256(status.encode()).hexdigest(),
        "config_sha256": config_hash,
    }


def _stream_file_fingerprint(path: Path) -> tuple[int, bytes]:
    """Return length and SHA-256 without retaining the file body in memory."""
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            length += len(chunk)
            digest.update(chunk)
    return length, digest.digest()


def _stream_git_fingerprint(project: Path, *args: str) -> tuple[int, bytes]:
    """Hash potentially large Git output incrementally."""
    with tempfile.TemporaryFile() as error_file:
        process = subprocess.Popen(
            ["git", *args],
            cwd=project,
            stdout=subprocess.PIPE,
            stderr=error_file,
        )
        assert process.stdout is not None
        digest = hashlib.sha256()
        length = 0
        try:
            while chunk := process.stdout.read(1024 * 1024):
                length += len(chunk)
                digest.update(chunk)
        finally:
            process.stdout.close()
        returncode = process.wait()
        error_file.seek(0)
        error = error_file.read(4096)
    if returncode != 0:
        raise WorkflowError(
            error.decode(errors="replace").strip() or "Git 命令失败"
        )
    return length, digest.digest()


def _project_content_snapshot_once(project: Path) -> dict[str, Any]:
    """Hash staged, unstaged and untracked content without persisting names."""
    project = project.resolve(strict=True)
    paths = git_changed_files(project)
    digest = hashlib.sha256()
    git_inputs = (
        (b"status", ("status", "--porcelain=v1", "-z", "-uall")),
        (
            b"cached",
            ("diff", "--cached", "--binary", "--full-index", "--no-ext-diff", "--", "."),
        ),
        (
            b"unstaged",
            ("diff", "--binary", "--full-index", "--no-ext-diff", "--", "."),
        ),
    )
    for label, args in git_inputs:
        length, fingerprint = _stream_git_fingerprint(project, *args)
        digest.update(label + b"\0" + length.to_bytes(8, "big") + fingerprint)
    for relative in sorted(paths):
        encoded_name = relative.encode("utf-8", errors="surrogateescape")
        candidate = project / relative
        digest.update(b"path\0" + len(encoded_name).to_bytes(8, "big") + encoded_name)
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            digest.update(b"missing\0")
            continue
        digest.update(
            f"type={stat.S_IFMT(info.st_mode):o};mode={stat.S_IMODE(info.st_mode):o}\0".encode()
        )
        if candidate.is_symlink():
            value = os.readlink(candidate).encode("utf-8", errors="surrogateescape")
            length = len(value)
            fingerprint = hashlib.sha256(value).digest()
        elif stat.S_ISREG(info.st_mode):
            length, fingerprint = _stream_file_fingerprint(candidate)
        else:
            value = f"size={info.st_size};mtime_ns={info.st_mtime_ns}".encode()
            length = len(value)
            fingerprint = hashlib.sha256(value).digest()
        digest.update(length.to_bytes(8, "big") + fingerprint)
    return {
        "sha256": digest.hexdigest(),
        "head": safe_git_output(project, "rev-parse", "HEAD") or "unborn",
        "changed_count": len(paths),
    }


def project_content_snapshot(project: Path, attempts: int = 3) -> dict[str, Any]:
    for _attempt in range(attempts):
        before = _project_content_snapshot_once(project)
        after = _project_content_snapshot_once(project)
        if before == after:
            return after
    raise ProjectChangedError("无法冻结稳定的 staged/unstaged/untracked 内容快照")


def verify_project_content_snapshot(project: Path, expected: dict[str, Any]) -> None:
    if project_content_snapshot(project) != expected:
        raise ProjectChangedError("项目内容在验证或评审快照之后发生变化")


def promoted_target_content_snapshot(
    target: Path, expected_candidate: dict[str, Any]
) -> dict[str, Any]:
    """Freeze the exact reviewed candidate content after transactional copy.

    Candidate workspaces use a synthetic Git HEAD, so their ``head`` values
    intentionally differ from the real target.  The content digest and
    changed-file count must nevertheless be identical after promotion.  A
    mismatch means an external writer changed the target during the promotion
    window; do not create a ready-for-user handoff for that mixed state.
    """
    current = project_content_snapshot(target)
    if (
        current.get("sha256") != expected_candidate.get("sha256")
        or current.get("changed_count") != expected_candidate.get("changed_count")
    ):
        raise ProjectChangedError(
            "事务式回写后目标项目包含未评审的并发内容变化"
        )
    return current


def verify_project_integrity_snapshot(
    project: Path, expected: dict[str, str]
) -> None:
    try:
        current = project_integrity_snapshot(project)
    except Exception as exc:
        raise ProjectChangedError("基线期间无法重新核对目标项目快照") from exc
    if current != expected:
        raise ProjectChangedError("基线期间目标项目发生变化，未启动任何模型")


def validate_project(project: Path, require_clean: bool) -> None:
    if not project.is_dir():
        raise WorkflowError(f"项目目录不存在：{project}")
    inside = git_output(project, "rev-parse", "--is-inside-work-tree").strip()
    if inside != "true":
        raise WorkflowError("目标目录必须是 Git 仓库")
    if require_clean and git_output(project, "status", "--porcelain").strip():
        raise WorkflowError(
            "目标仓库不是干净状态。请先处理现有改动后再启动受监督工作流。"
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


def freeze_project_preflight_state(
    project: Path, attempts: int = 3
) -> tuple[dict[str, str], list[str]]:
    """Read validation config only when surrounding project snapshots agree."""
    for _attempt in range(attempts):
        before = project_integrity_snapshot(project)
        commands = load_validation_commands(project)
        after = project_integrity_snapshot(project)
        if before == after:
            return after, commands
    raise WorkflowError("无法冻结稳定的项目预检快照")


def seatbelt_escape(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def project_secret_regexes(project: Path) -> list[str]:
    root = re.escape(str(project)).replace('"', '\\"')
    prefix = rf"^{root}/(.*/)?"
    return [
        prefix + r"(\.aws|\.ssh|\.gnupg|\.direnv)(/.*)?$",
        prefix + r"(.*/)?\.docker/config\.json$",
        prefix + r"\.env(rc|\..*)?$",
        prefix + r"[^/]+\.env$",
        prefix + r"(\.npmrc|\.pypirc|\.netrc|\.git-credentials|id_(rsa|dsa|ecdsa|ed25519))$",
        prefix + r"[^/]*\.(pem|key|p12|pfx)$",
        prefix + r"(credentials(\..*)?|[^/]*credentials?[^/]*\.(json|toml|ya?ml|ini)|service[-_]?account[^/]*\.json)$",
        prefix + r"[^/]*secret[^/]*\.(json|toml|ya?ml|ini)$",
    ]


def prepare_validation_git_view(
    project: Path, validation_dir: Path
) -> tuple[Path, list[Path], list[Path]]:
    """Build sanitized synthetic HEAD/index while preserving worktree diffs."""
    project = project.resolve(strict=True)
    validation_dir = validation_dir.resolve(strict=True)
    safe_git_dir = validation_dir / "git-metadata"
    developer_git = Path(
        "/Applications/Xcode.app/Contents/Developer/usr/bin/git"
    )
    real_git = (
        str(developer_git)
        if developer_git.is_file()
        else shutil.which("git")
    )
    if not real_git:
        raise WorkflowError("验证环境缺少 git")
    git_env = {
        **os.environ,
        "GIT_CONFIG": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    existing_git_metadata: list[Path] = []

    def metadata_walk_error(error: OSError) -> None:
        raise WorkflowError(f"无法枚举预存 Git 元数据：{error}")

    for root, directories, files in os.walk(
        project, topdown=True, followlinks=False, onerror=metadata_walk_error
    ):
        root_path = Path(root)
        if ".git" in directories:
            existing_git_metadata.append(root_path / ".git")
            directories.remove(".git")
        if ".git" in files:
            existing_git_metadata.append(root_path / ".git")

    def invoke_git(
        command: list[str],
        *,
        env: dict[str, str],
        stdin: bytes | None = None,
        allow_failure: bool = False,
    ) -> tuple[int, bytes, bytes]:
        try:
            process = subprocess.Popen(
                command,
                cwd=project,
                stdin=subprocess.PIPE if stdin is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            stdout, stderr = process.communicate(input=stdin, timeout=1800)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise WorkflowError("建立去敏 Git 验证视图超时") from exc
        except OSError as exc:
            raise WorkflowError(f"无法建立去敏 Git 验证视图: {exc}") from exc
        if process.returncode != 0 and not allow_failure:
            raise WorkflowError(
                redact_live_text(
                    (stderr.strip() or stdout.strip()).decode(errors="replace")
                )
                or "无法建立去敏 Git 验证视图"
            )
        return process.returncode, stdout, stderr

    def run_real(
        *args: str, allow_failure: bool = False
    ) -> tuple[int, bytes, bytes]:
        return invoke_git(
            [real_git, *args], env=git_env, allow_failure=allow_failure
        )

    def run_safe(
        *args: str,
        stdin: bytes | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> bytes:
        env = {
            **git_env,
            "GIT_AUTHOR_NAME": "Local AI MVP Validation",
            "GIT_AUTHOR_EMAIL": "validation@example.invalid",
            "GIT_COMMITTER_NAME": "Local AI MVP Validation",
            "GIT_COMMITTER_EMAIL": "validation@example.invalid",
            **(extra_env or {}),
        }
        return invoke_git(
            [
                real_git,
                f"--git-dir={safe_git_dir}",
                f"--work-tree={project}",
                *args,
            ],
            env=env,
            stdin=stdin,
        )[1]

    def allowed_path(raw_path: bytes) -> bool:
        return not is_baseline_secret_path(Path(os.fsdecode(raw_path)))

    copied_blobs: dict[bytes, bytes] = {}

    def copy_blob(object_id: bytes) -> bytes:
        if object_id in copied_blobs:
            return copied_blobs[object_id]
        _code, content, _error = run_real(
            "cat-file", "blob", object_id.decode()
        )
        safe_id = run_safe(
            "hash-object", "-w", "--stdin", stdin=content
        ).strip()
        copied_blobs[object_id] = safe_id
        return safe_id

    inside, _stdout, _stderr = run_real(
        "rev-parse", "--is-inside-work-tree", allow_failure=True
    )
    if inside != 0:
        run_safe("init", "-q")
        exclusions: set[str] = set()
        for pattern in BASELINE_SECRET_EXCLUDES:
            normalized = pattern.removeprefix("**/")
            exclusions.add(f":(exclude,glob)**/{normalized}")
            exclusions.add(f":(exclude,glob)**/{normalized}/**")
        run_safe("add", "-A", "--", ".", *sorted(exclusions))
        run_safe(
            "commit", "--allow-empty", "-qm",
            "sanitized validation snapshot",
        )
        return safe_git_dir, [], existing_git_metadata

    _code, object_format, _error = run_real(
        "rev-parse", "--show-object-format"
    )
    init_args = ["init", "-q"]
    rendered_format = object_format.decode().strip()
    if rendered_format and rendered_format != "sha1":
        init_args.append(f"--object-format={rendered_format}")
    run_safe(*init_args)
    atomic_write_text(
        safe_git_dir / "config",
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n",
    )

    head_index = validation_dir / "head.index"
    head_env = {"GIT_INDEX_FILE": str(head_index)}
    run_safe("read-tree", "--empty", extra_env=head_env)
    head_exists, tree_output, _error = run_real(
        "ls-tree", "-rz", "--full-tree", "HEAD", allow_failure=True
    )
    if head_exists == 0:
        head_entries = bytearray()
        for record in tree_output.split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, object_id = metadata.split(b" ", 2)
            if not allowed_path(raw_path):
                continue
            safe_id = (
                copy_blob(object_id)
                if object_type == b"blob"
                else object_id
            )
            head_entries.extend(
                mode + b" " + safe_id + b" 0\t" + raw_path + b"\0"
            )
        if head_entries:
            run_safe(
                "update-index", "-z", "--index-info",
                stdin=bytes(head_entries), extra_env=head_env,
            )
        tree_id = run_safe("write-tree", extra_env=head_env).strip()
        commit_id = run_safe(
            "commit-tree", tree_id.decode(),
            stdin=b"sanitized validation baseline\n",
        ).strip()
        run_safe("update-ref", "refs/heads/mvp-validation", commit_id.decode())
        run_safe("symbolic-ref", "HEAD", "refs/heads/mvp-validation")

    run_safe("read-tree", "--empty")
    _code, index_output, _error = run_real("ls-files", "--stage", "-z")
    index_entries = bytearray()
    for record in index_output.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, object_id, stage = metadata.split(b" ", 2)
        if not allowed_path(raw_path):
            continue
        safe_id = object_id
        if mode != b"160000" and object_id.strip(b"0"):
            safe_id = copy_blob(object_id)
        index_entries.extend(
            mode + b" " + safe_id + b" " + stage + b"\t" + raw_path + b"\0"
        )
    if index_entries:
        run_safe(
            "update-index", "-z", "--index-info", stdin=bytes(index_entries)
        )
    return safe_git_dir, [], existing_git_metadata


def install_validation_git_wrapper(
    validation_dir: Path, project: Path, safe_git_dir: Path
) -> None:
    """Use sanitized metadata only for the validated project, not child repos."""
    developer_git = Path(
        "/Applications/Xcode.app/Contents/Developer/usr/bin/git"
    )
    real_git = (
        str(developer_git)
        if developer_git.is_file()
        else shutil.which("git")
    )
    if not real_git:
        raise WorkflowError("验证环境缺少 git")
    wrapper = validation_dir / "bin" / "git"
    script = f"""#!/bin/zsh
set -eu
real_git={shlex.quote(str(Path(real_git).resolve()))}
project={shlex.quote(str(project.resolve()))}
safe_git_dir={shlex.quote(str(safe_git_dir.resolve()))}
target="$PWD"
args=("$@")
i=1
command=""
while (( i <= $# )); do
  arg="${{args[$i]}}"
  case "$arg" in
    -C)
      (( i += 1 ))
      (( i <= $# )) || break
      value="${{args[$i]}}"
      if [[ "$value" = /* ]]; then target="$value"; else target="$target/$value"; fi
      ;;
    -C*)
      value="${{arg#-C}}"
      if [[ "$value" = /* ]]; then target="$value"; else target="$target/$value"; fi
      ;;
    --git-dir|--work-tree)
      exec env -u GIT_DIR -u GIT_WORK_TREE -u GIT_CONFIG "$real_git" "$@"
      ;;
    --git-dir=*|--work-tree=*)
      exec env -u GIT_DIR -u GIT_WORK_TREE -u GIT_CONFIG "$real_git" "$@"
      ;;
    --)
      break
      ;;
    -c|--config-env|--exec-path)
      (( i += 1 ))
      ;;
    -*)
      ;;
    *)
      command="$arg"
      break
      ;;
  esac
  (( i += 1 ))
done
if [[ "$command" = init || "$command" = clone ]]; then
  (( i += 1 ))
  while (( i <= $# )); do
    value="${{args[$i]}}"
    if [[ "$value" != -* ]]; then
      if [[ "$value" = /* ]]; then target="$value"; else target="$target/$value"; fi
    fi
    (( i += 1 ))
  done
fi
target="${{target:A}}"
child_repo=0
probe="$target"
while [[ "$probe" = "$project"/* ]]; do
  if [[ -e "$probe/.git" || -L "$probe/.git" ]]; then
    child_repo=1
    break
  fi
  probe="${{probe:h}}"
done
if (( child_repo )) || \
   [[ ( "$command" = init || "$command" = clone ) && "$target" != "$project" ]]; then
  exec env -u GIT_DIR -u GIT_WORK_TREE -u GIT_CONFIG "$real_git" "$@"
fi
if [[ "$target" = "$project" || "$target" = "$project"/* ]]; then
  exec env GIT_DIR="$safe_git_dir" GIT_WORK_TREE="$project" \
    GIT_CONFIG="$safe_git_dir/config" "$real_git" "$@"
fi
exec env -u GIT_DIR -u GIT_WORK_TREE -u GIT_CONFIG "$real_git" "$@"
"""
    atomic_write_text(wrapper, script)
    wrapper.chmod(0o700)


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


def write_validation_profile(
    project: Path,
    run_dir: Path,
    *,
    project_writable: bool = True,
    validation_dir: Path | None = None,
    extra_read_roots: list[Path] | None = None,
    extra_deny_paths: list[Path] | None = None,
) -> Path:
    project = project.resolve()
    run_dir = run_dir.resolve()
    validation_dir = (validation_dir or run_dir).resolve()
    home = Path.home()
    allowed_reads = [
        project,
        validation_dir,
        Path("/System"),
        Path("/usr"),
        Path("/bin"),
        Path("/sbin"),
        Path("/Library"),
        Path("/Applications"),
        Path("/opt/homebrew"),
        Path("/private/var/select"),
        *user_toolchain_read_roots(home),
        *list(extra_read_roots or []),
    ]
    lines = [
        "(version 1)",
        "(deny default)",
        '(import "system.sb")',
        "(allow process*)",
        "(allow sysctl-read)",
        f'(allow file-write* (subpath "{seatbelt_escape(validation_dir)}"))',
        "(deny network*)",
    ]
    if project_writable:
        lines.append(
            f'(allow file-write* (subpath "{seatbelt_escape(project)}"))'
        )
    if validation_dir != run_dir:
        lines.append(
            f'(deny file-write* (subpath "{seatbelt_escape(run_dir)}"))'
        )
    lines.append(
        f'(deny file-write* (subpath "{seatbelt_escape(project / ".git")}"))'
    )
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
        (
            f'(deny file-read* (literal "{seatbelt_escape(project / ".git")}"))',
            f'(deny file-read* (subpath "{seatbelt_escape(project / ".git")}"))',
        )
    )
    lines.extend(
        f'(deny file-read* (regex #"{pattern}"))'
        for pattern in project_secret_regexes(project)
    )
    lines.extend(
        f'(deny file-write* (regex #"{pattern}"))'
        for pattern in project_secret_regexes(project)
    )
    lines.extend(
        f'(deny file-read* (literal "{seatbelt_escape(path.resolve())}"))'
        for path in extra_deny_paths or []
    )
    lines.extend(
        f'(deny file-read* (subpath "{seatbelt_escape(path.resolve())}"))'
        for path in extra_deny_paths or []
    )
    lines.extend(
        f'(deny file-write* (literal "{seatbelt_escape(path.resolve())}"))'
        for path in extra_deny_paths or []
    )
    lines.extend(
        f'(deny file-write* (subpath "{seatbelt_escape(path.resolve())}"))'
        for path in extra_deny_paths or []
    )
    profile = run_dir / "validation.sb"
    atomic_write_text(profile, "\n".join(lines) + "\n")
    return profile


def validation_environment(validation_dir: Path) -> dict[str, str]:
    validation_dir = validation_dir.resolve()
    safe_home = validation_dir / "home"
    safe_tmp = validation_dir / "tmp"
    safe_bin = validation_dir / "bin"
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
    env["PATH"] = f"{safe_bin}{os.pathsep}{env.get('PATH', '')}"
    env.update(
        {
            "HOME": str(safe_home),
            "TMPDIR": str(safe_tmp),
            "XDG_CACHE_HOME": str(safe_home / ".cache"),
            "CI": "1",
            "NO_COLOR": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": str(safe_tmp / "pycache"),
            "PIP_CACHE_DIR": str(safe_tmp / "pip-cache"),
            "UV_CACHE_DIR": str(safe_tmp / "uv-cache"),
            "NPM_CONFIG_CACHE": str(safe_tmp / "npm-cache"),
            "MVP_VALIDATION_SANDBOX": "1",
        }
    )
    return env


def make_disposable_baseline_workspace(
    project: Path,
    run_dir: Path,
    *,
    exclude_opencode_project_controls: bool = False,
) -> tuple[Path, Path]:
    """Create a private, sanitized workspace for writable checks or coding.

    The OpenCode implementation workspace excludes repository-local OpenCode
    controls. OpenCode merges those controls after ``OPENCODE_CONFIG`` even
    with ``--pure``; retaining them would let an untrusted project add MCP,
    plugin, or Agent behavior outside the supervisor's per-run policy.
    """
    project = project.resolve(strict=True)
    container = Path(
        tempfile.mkdtemp(
            prefix=f".{run_dir.name}-baseline-workspace-",
            dir=run_dir.parent,
        )
    )
    container.chmod(0o700)
    workspace = container / "workspace"
    try:
        workspace.mkdir(mode=0o700)
        command = ["/usr/bin/rsync", "-a"]
        for pattern in BASELINE_SECRET_EXCLUDES:
            command.append(f"--exclude={pattern}")
        if exclude_opencode_project_controls:
            for pattern in OPENCODE_PROJECT_CONTROL_EXCLUDES:
                command.append(f"--exclude={pattern}")
        command.extend([str(project) + "/", str(workspace) + "/"])
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=1800,
            check=False,
        )
        if result.returncode != 0:
            raise WorkflowError(
                "无法创建可丢弃的基线工作副本："
                + redact_live_text(result.stdout or f"exit={result.returncode}")
            )
        if not workspace.is_dir() or workspace.resolve() == project:
            raise WorkflowError("基线工作副本创建结果无效")
        leaked = [
            path
            for path in workspace.rglob("*")
            if is_baseline_secret_path(path.relative_to(workspace))
        ]
        if leaked:
            raise WorkflowError("基线工作副本包含被禁止的敏感路径")
        if exclude_opencode_project_controls:
            leaked_controls = [
                path
                for path in workspace.rglob("*")
                if is_opencode_project_control_path(path.relative_to(workspace))
            ]
            if leaked_controls:
                raise WorkflowError("实现工作副本包含 OpenCode 项目控制文件")
        git_binary = Path(
            "/Applications/Xcode.app/Contents/Developer/usr/bin/git"
        )
        git_command = str(git_binary) if git_binary.is_file() else "git"
        git_env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        for args in (
            ("init", "-q"),
            ("add", "-A"),
            (
                "-c", "user.name=Local AI MVP Baseline",
                "-c", "user.email=baseline@example.invalid",
                "commit", "--allow-empty", "-qm", "sanitized baseline",
            ),
        ):
            initialized = subprocess.run(
                [git_command, *args],
                cwd=workspace,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=1800,
                check=False,
                env=git_env,
            )
            if initialized.returncode != 0:
                raise WorkflowError(
                    "无法初始化去敏基线 Git 元数据："
                    + redact_live_text(
                        initialized.stdout
                        or f"exit={initialized.returncode}"
                    )
                )
        return container, workspace
    except Exception:
        cleanup_baseline_workspace(container)
        raise


def is_baseline_secret_path(relative: Path) -> bool:
    rendered = relative.as_posix().lower()
    basename = relative.name.lower()
    if any(part.lower() == ".git" for part in relative.parts):
        return True
    return any(
        fnmatch.fnmatchcase(basename, pattern.lower())
        or fnmatch.fnmatchcase(rendered, pattern.lower())
        or rendered.startswith(pattern.lower().rstrip("/") + "/")
        for pattern in BASELINE_SECRET_EXCLUDES
    )


def is_opencode_project_control_path(relative: Path) -> bool:
    """Whether a path can be loaded as repository-local OpenCode control."""
    return (
        relative.name.lower() in {"opencode.json", "opencode.jsonc"}
        or any(part.lower() == ".opencode" for part in relative.parts)
    )


def cleanup_baseline_workspace(container: Path) -> None:
    """Remove a private baseline tree and fail closed if anything remains."""
    cleanup_private_tree(
        container, BaselineCleanupError, "基线工作副本"
    )


def _safe_promotion_path(relative: str) -> Path:
    path = Path(relative)
    if (
        not relative
        or path.is_absolute()
        or ".." in path.parts
        or is_baseline_secret_path(path)
    ):
        raise WorkflowError("实现工作区包含禁止回写的路径")
    return path


def _promotion_path_allowed(relative: str, allowed: set[str]) -> bool:
    if relative in allowed:
        return True
    return any(
        entry.endswith("/") and relative.startswith(entry)
        for entry in allowed
    )


def _reject_symlink_path(root: Path, relative: Path, *, include_leaf: bool) -> None:
    current = root
    parts = relative.parts if include_leaf else relative.parts[:-1]
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise WorkflowError("事务式回写拒绝符号链接路径")


def promote_implementation_workspace(
    workspace: Path,
    target: Path,
    run_dir: Path,
    plan_path: Path,
    expected_target_snapshot: dict[str, str],
    validate_action,
    *,
    run_date: str | None = None,
) -> tuple[list[str], Any, str | None]:
    """Validate then atomically promote an approved disposable-tree diff.

    ``validate_action`` receives the disposable implementation workspace, not
    the real target.  Validation commands are allowed to create build output;
    keeping them in that disposable tree prevents an unreviewed artifact from
    reaching the target.  They must leave the Git-visible candidate diff
    unchanged before the transaction may start. Once all reviewed files have
    been written, promotion is complete; private-backup cleanup then becomes
    non-transactional garbage collection and can only produce a warning.
    """
    workspace = workspace.resolve(strict=True)
    target = target.resolve(strict=True)
    verify_project_integrity_snapshot(target, expected_target_snapshot)
    changed = git_changed_files(workspace)
    allowed = set(plan_declared_project_paths(workspace, plan_path))
    today = run_date or dt.date.today().isoformat()
    allowed.update(
        {
            f"docs/devlog/{today}.md",
            "docs/ai/PROJECT_OUTLINE.md",
            "docs/ai/TASK_PLAN.md",
        }
    )
    if not changed:
        raise WorkflowError("实现工作区没有可回写的变更")
    reviewed_candidate_snapshot = project_content_snapshot(workspace)
    for relative in changed:
        safe = _safe_promotion_path(relative)
        if is_opencode_project_control_path(safe):
            raise WorkflowError("OpenCode 项目控制文件不允许由本地 Agent 回写")
        if not _promotion_path_allowed(safe.as_posix(), allowed):
            raise WorkflowError(f"实现工作区越出批准范围：{safe.as_posix()}")
        _reject_symlink_path(workspace, safe, include_leaf=True)
        _reject_symlink_path(target, safe, include_leaf=True)
        source = workspace / safe
        if source.exists() and not source.is_file():
            raise WorkflowError("事务式回写仅支持常规文件")
        destination = target / safe
        if destination.exists() and not destination.is_file():
            raise WorkflowError("目标路径不是常规文件，拒绝回写")

    backup_container = Path(
        tempfile.mkdtemp(prefix=f".{run_dir.name}-promotion-backup-", dir=run_dir.parent)
    )
    backup_container.chmod(0o700)
    missing_before: set[str] = set()
    backed_up: set[str] = set()
    promoted_paths: list[str] = []
    promotion_complete = False

    def regular_files_match(left: Path, right: Path) -> bool:
        try:
            left_info = left.lstat()
            right_info = right.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(left_info.st_mode) or not stat.S_ISREG(right_info.st_mode):
            return False
        if (
            stat.S_IMODE(left_info.st_mode) != stat.S_IMODE(right_info.st_mode)
            or left_info.st_mtime_ns != right_info.st_mtime_ns
        ):
            return False
        return _stream_file_fingerprint(left) == _stream_file_fingerprint(right)

    def target_matches_preimage(relative: str) -> bool:
        safe = Path(relative)
        destination = target / safe
        if relative in missing_before:
            return not os.path.lexists(destination)
        backup = backup_container / safe
        return regular_files_match(destination, backup)

    def target_matches_candidate(relative: str) -> bool:
        safe = Path(relative)
        source = workspace / safe
        destination = target / safe
        if not source.exists():
            return not os.path.lexists(destination)
        return regular_files_match(destination, source)

    def restore_target() -> None:
        for relative in reversed(promoted_paths):
            safe = Path(relative)
            destination = target / safe
            backup = backup_container / safe
            # A user may have edited a previously promoted path while a later
            # path was being processed.  Never replace that newer user edit.
            if not target_matches_candidate(relative):
                continue
            if relative in missing_before:
                destination.unlink(missing_ok=True)
                continue
            if relative not in backed_up:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.rollback.", dir=destination.parent
            )
            os.close(descriptor)
            temporary_path = Path(temporary_name)
            try:
                shutil.copy2(backup, temporary_path)
                os.replace(temporary_path, destination)
            finally:
                temporary_path.unlink(missing_ok=True)

    try:
        validation_result = validate_action(workspace)
        try:
            verify_project_content_snapshot(workspace, reviewed_candidate_snapshot)
        except ProjectChangedError as exc:
            raise WorkflowError(
                "事务式回写候选在验证期间产生了未评审的 Git 可见改动"
            ) from exc
        # Candidate validation may be long-running.  Do not overwrite a
        # concurrent user edit made after the earlier pre-promotion snapshot.
        verify_project_integrity_snapshot(target, expected_target_snapshot)

        for relative in changed:
            safe = Path(relative)
            destination = target / safe
            backup = backup_container / safe
            if destination.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup)
                backed_up.add(relative)
            else:
                missing_before.add(relative)

        for relative in changed:
            safe = Path(relative)
            source = workspace / safe
            destination = target / safe
            if not source.exists():
                if not target_matches_preimage(relative):
                    raise ProjectChangedError(
                        "事务式回写期间目标项目发生并发变化"
                    )
                destination.unlink(missing_ok=True)
                promoted_paths.append(relative)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            _reject_symlink_path(target, safe, include_leaf=False)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", dir=destination.parent
            )
            os.close(descriptor)
            temporary_path = Path(temporary_name)
            try:
                shutil.copy2(source, temporary_path)
                if not target_matches_preimage(relative):
                    raise ProjectChangedError(
                        "事务式回写期间目标项目发生并发变化"
                    )
                os.replace(temporary_path, destination)
                promoted_paths.append(relative)
            finally:
                temporary_path.unlink(missing_ok=True)

        promotion_complete = True
        try:
            cleanup_private_tree(backup_container, WorkflowError, "事务式回写备份")
        except Exception:
            return (
                changed,
                validation_result,
                "事务式回写备份清理待处理；已保留已验证目标，不公开内部路径或异常详情",
            )
        return changed, validation_result, None
    except Exception as exc:
        if not promotion_complete:
            restore_target()
            verify_project_integrity_snapshot(target, expected_target_snapshot)
            try:
                cleanup_private_tree(
                    backup_container, WorkflowError, "事务式回写备份"
                )
            except Exception as cleanup_exc:
                raise cleanup_exc from exc
        raise


def cleanup_validation_workspace(container: Path) -> None:
    cleanup_private_tree(
        container, ValidationCleanupError, "验证临时目录"
    )


def cleanup_private_tree(
    container: Path, error_type: type[WorkflowError], label: str
) -> None:
    if not container.exists() and not container.is_symlink():
        return
    try:
        def make_removable(candidate: Path, mode: int) -> None:
            if candidate.is_symlink():
                return
            if hasattr(os, "chflags"):
                os.chflags(candidate, 0, follow_symlinks=False)
            candidate.chmod(mode)

        # First restore traversal permissions from the root downward. os.walk
        # cannot discover immutable files below a 000 directory unless the
        # parent is normalized before descent.
        make_removable(container, 0o700)
        for root, directories, files in os.walk(
            container, topdown=True, followlinks=False
        ):
            traversable: list[str] = []
            for name in directories:
                candidate = Path(root) / name
                if candidate.is_symlink():
                    continue
                make_removable(candidate, 0o700)
                traversable.append(name)
            directories[:] = traversable
            for name in files:
                make_removable(Path(root) / name, 0o600)

        # Repeat bottom-up after every real directory is traversable so nested
        # flags and permissions are cleared immediately before deletion.
        for root, directories, files in os.walk(
            container, topdown=False, followlinks=False
        ):
            for name in files:
                candidate = Path(root) / name
                if candidate.is_symlink():
                    continue
                try:
                    make_removable(candidate, 0o600)
                except OSError:
                    pass
            for name in directories:
                candidate = Path(root) / name
                if candidate.is_symlink():
                    continue
                try:
                    make_removable(candidate, 0o700)
                except OSError:
                    pass
        if not container.is_symlink():
            make_removable(container, 0o700)
        shutil.rmtree(container)
    except Exception as exc:
        raise error_type(f"无法完整清理{label}：{container}") from exc
    if container.exists() or container.is_symlink():
        raise error_type(f"{label}清理后仍然存在：{container}")


def run_validations(
    project: Path,
    commands: list[str],
    run_dir: Path,
    stage: str,
    live: bool = False,
    project_writable: bool = True,
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    validation_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{run_dir.name}-{stage}-validation-",
            dir=run_dir.parent,
        )
    )
    try:
        validation_dir.chmod(0o700)
        safe_git_dir, git_read_roots, git_secret_paths = prepare_validation_git_view(
            project, validation_dir
        )
        profile = write_validation_profile(
            project,
            run_dir,
            project_writable=project_writable,
            validation_dir=validation_dir,
            extra_read_roots=git_read_roots,
            extra_deny_paths=git_secret_paths,
        )
        env = validation_environment(validation_dir)
        install_validation_git_wrapper(validation_dir, project, safe_git_dir)
        env["GIT_CONFIG"] = str(safe_git_dir / "config")
        environment_probe = (
            'set -eu; for probe_dir in "$TMPDIR" "$XDG_CACHE_HOME" "$PIP_CACHE_DIR" '
            '"$UV_CACHE_DIR" "$NPM_CONFIG_CACHE"; do /bin/mkdir -p -- "$probe_dir"; '
            'probe="$probe_dir/.mvp-write-probe-$$"; : > "$probe"; /bin/rm -f -- "$probe"; done'
        )
        entries = (
            [("environment-write-probe", environment_probe)]
            if stage == "preflight"
            else []
        ) + [(command, command) for command in commands]
        for index, (label, command) in enumerate(entries, start=1):
            log_path = run_dir / f"{stage}-validation-{index}.log"
            if live:
                emit_progress("VALIDATE", f"开始：{label}")
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
            atomic_write_text(log_path, redact_persistent_output(output))
            outcomes.append(
                {
                    "command": label,
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
            if label == "environment-write-probe" and exit_code != 0:
                break
    finally:
        try:
            cleanup_validation_workspace(validation_dir)
        except ValidationCleanupError as exc:
            # Commands may already have completed and their redacted logs are
            # durable. Preserve that audit trail even though cleanup remains a
            # blocking failure and this function cannot return normally.
            exc.outcomes = list(outcomes)
            raise
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
    atomic_write_text(log_path, output)
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


def parse_porcelain_v1_z(output: str) -> list[str]:
    """Parse Git porcelain v1 -z without trimming status-prefix spaces."""
    records = output.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2] != " ":
            raise WorkflowError("无法解析 git status --porcelain=v1 -z 输出")
        status = record[:2]
        path = record[3:]
        if path:
            paths.append(path)
        if "R" in status or "C" in status:
            if index >= len(records) or not records[index]:
                raise WorkflowError("git rename/copy 状态缺少原路径")
            paths.append(records[index])
            index += 1
    return list(dict.fromkeys(paths))


def git_changed_files(project: Path) -> list[str]:
    return parse_porcelain_v1_z(
        git_output(project, "status", "--porcelain=v1", "-z", "-uall")
    )


def plan_declared_project_paths(project: Path, plan_path: Path) -> list[str]:
    """Return safe repository-relative paths explicitly named by the plan.

    Direct-cloud routes start from a clean tree, so changed files and review
    findings alone cannot describe the supervisor's approved mutation scope.
    Inline-code paths in the approved plan provide that missing evidence. A
    named directory is retained as a prefix and expanded to its current files;
    planned new files must still be named explicitly or live below that prefix.
    """
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError:
        return []

    safe_basenames = {
        ".mvp-ai.toml",
        ".gitignore",
        "package.json",
        "package-lock.json",
        "PRIVACY.md",
        "README.md",
    }
    exclusion_markers = (
        "outside scope",
        "out of scope",
        "must not",
        "do not modify",
        "do not create",
        "do not use",
        "do not stage",
        "do not commit",
        "excluded",
        "exclude",
        "禁止",
        "不得",
        "不要修改",
        "不要创建",
        "不要使用",
        "排除",
    )
    declared: set[str] = set()
    sentence_boundary = re.compile(
        r"(?:[.!?。！？;；](?:[ \t]+|\r?\n)|\r?\n[ \t]*\r?\n)"
    )
    for match in re.finditer(r"`([^`\r\n]+)`", text):
        raw = match.group(1).strip().replace("\\", "/")
        context_start = 0
        for boundary in sentence_boundary.finditer(text, 0, match.start()):
            context_start = boundary.end()
        next_boundary = sentence_boundary.search(text, match.end())
        context_end = next_boundary.start() if next_boundary else len(text)
        context = text[context_start:context_end].lower()
        if (
            not raw
            or any(character.isspace() for character in raw)
            or raw.startswith(("$", "-", "http://", "https://"))
            or any(marker in context for marker in exclusion_markers)
            or re.fullmatch(r"[^/\s`]+\.\.[^/\s`]+", raw)
            or re.fullmatch(r"\d{8}-\d{6}-[^/\s`]+", raw)
        ):
            continue
        is_directory = raw.endswith("/")
        normalized = raw.rstrip("/")
        relative = Path(normalized)
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or is_baseline_secret_path(relative)
        ):
            continue
        if (
            not is_directory
            and "/" not in normalized
            and normalized not in safe_basenames
            and not relative.suffix
        ):
            continue

        if is_directory:
            prefix = relative.as_posix().rstrip("/") + "/"
            declared.add(prefix)
            root = project / relative
            if root.is_dir():
                for candidate in root.rglob("*"):
                    if not candidate.is_file():
                        continue
                    candidate_relative = candidate.relative_to(project)
                    if not is_baseline_secret_path(candidate_relative):
                        declared.add(candidate_relative.as_posix())
            continue
        declared.add(relative.as_posix())
    return sorted(declared)


def plan_declares_opencode_project_controls(project: Path, plan_path: Path) -> bool:
    """Whether an approved plan must bypass the local OpenCode candidate."""
    return any(
        is_opencode_project_control_path(Path(path))
        for path in plan_declared_project_paths(project, plan_path)
    )


def plan_declared_validation_commands(plan_path: Path) -> list[str]:
    """Extract exact planned post-edit validation commands from inline code."""
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError:
        return []
    commands: list[str] = []
    for match in re.finditer(r"`([^`\r\n]+)`", text):
        command = match.group(1).strip()
        if command.startswith(("npm ", "node ", "git ", "python ", "python3 ")):
            commands.append(command)
    return list(dict.fromkeys(commands))


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_matches_sha256(path: Path, expected: str) -> bool:
    try:
        return file_sha256(path) == expected
    except OSError:
        return False


def verify_context_capsule(path: Path) -> None:
    """Verify every evidence hash before a capsule is consumed by an agent."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"无法解析 context capsule：{path}") from exc
    if not isinstance(payload, dict):
        raise WorkflowError(f"context capsule 根节点必须是对象：{path}")
    base_dir = Path(str(payload.get("evidence_base_dir") or path.parent))

    def verify_reference(
        label: str, value_path: Any, value_hash: Any, *, optional: bool = False
    ) -> Path | None:
        if value_path is None and value_hash is None and optional:
            return None
        if not isinstance(value_path, str) or not isinstance(value_hash, str):
            raise WorkflowError(f"{label} 证据路径或 SHA-256 缺失")
        evidence_path = Path(value_path)
        if not evidence_path.is_absolute():
            evidence_path = base_dir / evidence_path
        if not file_matches_sha256(evidence_path, value_hash):
            raise WorkflowError(f"{label} 证据 SHA-256 不匹配：{evidence_path}")
        return evidence_path

    plan = payload.get("plan")
    if not isinstance(plan, dict):
        raise WorkflowError("capsule 缺少 plan 证据")
    verify_reference("plan", plan.get("path"), plan.get("sha256"))

    if isinstance(payload.get("scope_evidence"), dict):
        scope = payload["scope_evidence"]
        scope_path = verify_reference(
            "scope", scope.get("path"), scope.get("sha256")
        )
        validation = payload.get("validation_evidence")
        review = payload.get("review_evidence")
        if not isinstance(validation, dict):
            raise WorkflowError("capsule 缺少 validation evidence")
        validation_path = verify_reference(
            "validation manifest",
            validation.get("path"),
            validation.get("sha256"),
        )
        if isinstance(review, dict):
            verify_reference(
                "review", review.get("path"), review.get("sha256")
            )
    else:
        scope_path = verify_reference(
            "scope",
            payload.get("scope_evidence_path"),
            payload.get("scope_evidence_sha256"),
        )
        validation_path = verify_reference(
            "validation manifest",
            payload.get("validation_evidence_path"),
            payload.get("validation_evidence_sha256"),
        )
        verify_reference(
            "review",
            payload.get("review_evidence_path"),
            payload.get("review_evidence_sha256"),
            optional=True,
        )
    assert scope_path is not None
    try:
        scope_manifest = json.loads(scope_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError("scope evidence 无法解析") from exc
    capsule_commit = payload.get("base_commit")
    scope_commit = (
        scope_manifest.get("base_commit")
        if isinstance(scope_manifest, dict)
        else None
    )
    if not isinstance(capsule_commit, str) or not capsule_commit:
        raise WorkflowError("capsule 缺少 base commit")
    if capsule_commit != scope_commit:
        raise WorkflowError("capsule 与 scope 的 base commit 不匹配")
    capsule_snapshot = payload.get("project_snapshot")
    scope_snapshot = (
        scope_manifest.get("project_snapshot")
        if isinstance(scope_manifest, dict)
        else None
    )
    if (
        not isinstance(capsule_snapshot, dict)
        or not isinstance(capsule_snapshot.get("sha256"), str)
        or capsule_snapshot != scope_snapshot
    ):
        raise WorkflowError("capsule 与 scope 的项目内容快照不匹配")
    assert validation_path is not None
    try:
        manifest = json.loads(validation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError("validation manifest 无法解析") from exc
    artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
    if not isinstance(artifacts, list):
        raise WorkflowError("validation manifest artifacts 必须是数组")
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise WorkflowError("validation manifest artifact 必须是对象")
        log_path = artifact.get("log_path")
        log_hash = artifact.get("log_sha256")
        verify_reference(f"validation log {index + 1}", log_path, log_hash)


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


def _tail_error_window(path: str | None, limit: int = 2000) -> str | None:
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return redact_sensitive_text(text[-limit:])


def _truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    suffix = "…[TRUNCATED]"
    room = max(0, max_bytes - len(suffix.encode("utf-8")))
    clipped = encoded[:room]
    while clipped:
        try:
            return clipped.decode("utf-8") + suffix, True
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return suffix[:max_bytes], True


def write_context_capsule(
    project: Path,
    run_dir: Path,
    *,
    stage: str,
    plan_path: Path,
    risk: dict[str, Any],
    validations: list[dict[str, Any]] | None = None,
    review: dict[str, Any] | None = None,
    project_snapshot: dict[str, Any] | None = None,
    max_bytes: int = 16384,
    run_date: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Write bounded, redacted navigation evidence; never embed full logs/diffs."""
    validations = validations or []
    review_evidence_path: Path | None = None
    if review is not None:
        review_evidence_path = run_dir / f"context-{stage}-review.json"
        atomic_write_text(
            review_evidence_path,
            json.dumps(
                sanitize_json_value(review), ensure_ascii=False, indent=2
            ),
        )
    changed_files = git_changed_files(project)
    diff_stat = safe_git_output(
        project, "diff", "--stat", "HEAD", "--", "."
    ) or ""
    validation_failures: list[dict[str, Any]] = []
    validation_artifacts: list[dict[str, Any]] = []
    for item in validations:
        log_value = item.get("log")
        log_path = Path(str(log_value)) if log_value else None
        if not log_path or not log_path.is_file():
            raise WorkflowError("validation outcome 缺少完整日志证据")
        redact_persisted_file(log_path)
        artifact = {
            "command": item.get("command"),
            "exit_code": item.get("exit_code"),
            "classification": item.get("classification"),
            "log_path": str(log_path),
            "log_sha256": file_sha256(log_path),
        }
        validation_artifacts.append(artifact)
        if item.get("exit_code") != 0:
            validation_failures.append(
                {
                    **artifact,
                    "error_window": _tail_error_window(
                        str(log_path) if log_path else None
                    ),
                }
            )
    validation_manifest_path = run_dir / f"context-{stage}-validation.json"
    planned_validation_commands = plan_declared_validation_commands(plan_path)
    validation_manifest_text = json.dumps(
        sanitize_json_value(
            {
                "schema_version": 1,
                "stage": stage,
                "validation_phase": (
                    "pre_implementation_baseline"
                    if stage == "supervisor" and not changed_files
                    else "post_implementation"
                ),
                "planned_post_edit_commands": planned_validation_commands,
                "artifacts": validation_artifacts,
            }
        ),
        ensure_ascii=False,
        indent=2,
    )
    atomic_write_text(validation_manifest_path, validation_manifest_text)
    validation_manifest_sha256 = hashlib.sha256(
        validation_manifest_text.encode("utf-8")
    ).hexdigest()
    plan_declared_paths = plan_declared_project_paths(project, plan_path)
    allowed_files = sorted(
        set(changed_files)
        | set(plan_declared_paths)
        | {
            str(item.get("file"))
            for item in (review or {}).get("findings", [])
            if isinstance(item, dict) and item.get("file")
        }
        | {
            f"docs/devlog/{run_date or dt.date.today().isoformat()}.md",
            "docs/ai/PROJECT_OUTLINE.md",
            "docs/ai/TASK_PLAN.md",
        }
    )
    base_commit = safe_git_output(project, "rev-parse", "HEAD") or "unborn"
    source_snapshot = project_snapshot or project_content_snapshot(project)
    snapshot_hash = source_snapshot.get("sha256") if isinstance(source_snapshot, dict) else None
    if not isinstance(snapshot_hash, str) or not snapshot_hash:
        raise WorkflowError("项目内容快照缺少 SHA-256")
    # HEAD is already bound separately as base_commit; keep only the content
    # digest so the mandatory final capsule remains viable at 1024 bytes.
    project_snapshot = {"sha256": snapshot_hash}
    scope_payload = sanitize_json_value(
        {
            "schema_version": 1,
            "stage": stage,
            "base_commit": base_commit,
            "project_snapshot": project_snapshot,
            "plan": {
                "path": str(plan_path),
                "sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
            },
            "risk": risk,
            "changed_files": changed_files,
            "allowed_files": allowed_files,
            "plan_declared_paths": plan_declared_paths,
            "planned_post_edit_commands": planned_validation_commands,
            "validation_phase": (
                "pre_implementation_baseline"
                if stage == "supervisor" and not changed_files
                else "post_implementation"
            ),
            "validation_failures": validation_failures,
            "evidence_policy": "validation logs contain complete redacted output",
        }
    )
    scope_evidence_path = run_dir / f"context-{stage}-scope.json"
    scope_evidence_text = json.dumps(
        scope_payload, ensure_ascii=False, indent=2
    )
    atomic_write_text(scope_evidence_path, scope_evidence_text)
    scope_evidence_sha256 = hashlib.sha256(
        scope_evidence_text.encode("utf-8")
    ).hexdigest()
    review_evidence_sha256 = (
        hashlib.sha256(review_evidence_path.read_bytes()).hexdigest()
        if review_evidence_path
        else None
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "stage": stage,
        "review_evidence_role": "prior_review_input_not_current_verdict",
        "base_commit": base_commit,
        "project_snapshot": project_snapshot,
        "plan": {
            "path": str(plan_path),
            "sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        },
        "risk": risk,
        "review_evidence_path": (
            str(review_evidence_path) if review_evidence_path else None
        ),
        "review_evidence_sha256": review_evidence_sha256,
        "scope_evidence_path": str(scope_evidence_path),
        "scope_evidence_sha256": scope_evidence_sha256,
        "validation_evidence_path": str(validation_manifest_path),
        "validation_evidence_sha256": validation_manifest_sha256,
        "changed_files": changed_files,
        "diff_stat": diff_stat,
        "validation_failures": validation_failures,
        "findings": review.get("findings", []) if review else [],
        "allowed_files": allowed_files,
    }
    redacted_payload = sanitize_json_value(payload)
    rendered = json.dumps(redacted_payload, ensure_ascii=False, indent=2)
    truncated = False
    if len(rendered.encode("utf-8")) > max_bytes:
        # Preserve a valid, auditable JSON index. Full evidence remains at the
        # referenced repository/log paths; every shortened field is explicit.
        truncated = True
        compact = {
            "schema_version": 1,
            "stage": stage,
            "base_commit": redacted_payload["base_commit"],
            "project_snapshot": redacted_payload["project_snapshot"],
            "review_evidence_role": redacted_payload["review_evidence_role"],
            "plan": redacted_payload["plan"],
            "risk": redacted_payload["risk"],
            "review_evidence_path": redacted_payload[
                "review_evidence_path"
            ],
            "review_evidence_sha256": redacted_payload[
                "review_evidence_sha256"
            ],
            "scope_evidence_path": redacted_payload["scope_evidence_path"],
            "scope_evidence_sha256": redacted_payload[
                "scope_evidence_sha256"
            ],
            "validation_evidence_path": redacted_payload[
                "validation_evidence_path"
            ],
            "validation_evidence_sha256": redacted_payload[
                "validation_evidence_sha256"
            ],
            "changed_files": redacted_payload["changed_files"][:50],
            "diff_stat": _truncate_utf8(
                str(redacted_payload["diff_stat"]), 512
            )[0],
            "validation_failures": [],
            "findings": [],
            "allowed_files": redacted_payload["allowed_files"],
            "truncated": True,
            "evidence_policy": "read referenced paths for complete evidence",
        }
        for item in redacted_payload["validation_failures"][:10]:
            compact["validation_failures"].append(
                {
                    **{
                        key: item.get(key)
                        for key in (
                            "command",
                            "exit_code",
                            "classification",
                            "log_path",
                            "log_sha256",
                        )
                    },
                    "error_window": _truncate_utf8(
                        str(item.get("error_window") or ""), 512
                    )[0],
                }
            )
        for item in redacted_payload["findings"][:20]:
            if not isinstance(item, dict):
                continue
            compact["findings"].append(
                {
                    key: (
                        _truncate_utf8(str(item.get(key) or ""), 384)[0]
                        if key in {"description", "required_fix"}
                        else item.get(key)
                    )
                    for key in (
                        "severity",
                        "title",
                        "file",
                        "line",
                        "description",
                        "required_fix",
                    )
                }
            )
        rendered = json.dumps(compact, ensure_ascii=False, indent=2)
        if len(rendered.encode("utf-8")) > max_bytes:
            source = json.dumps(compact["findings"], ensure_ascii=False)
            compact_plan = dict(compact["plan"])
            compact_plan_path = Path(str(compact_plan.get("path") or ""))
            if compact_plan_path.parent.resolve() == run_dir.resolve():
                compact_plan["path"] = compact_plan_path.name
            base = {
                "schema_version": 1,
                "stage": stage,
                "base_commit": compact["base_commit"],
                "project_snapshot": compact["project_snapshot"],
                "review_evidence_role": "prior_review_input_not_current_verdict",
                "truncated": True,
                "plan": compact_plan,
                "risk": {"classification": compact["risk"].get("classification")},
                "evidence_base_dir": str(run_dir),
                "scope_evidence": {
                    "path": Path(compact["scope_evidence_path"]).name,
                    "sha256": compact["scope_evidence_sha256"],
                },
                "validation_evidence": {
                    "path": Path(compact["validation_evidence_path"]).name,
                    "sha256": compact["validation_evidence_sha256"],
                    "count": len(validation_artifacts),
                },
                "review_evidence": (
                    {
                        "path": Path(compact["review_evidence_path"]).name,
                        "sha256": compact["review_evidence_sha256"],
                    }
                    if compact["review_evidence_path"]
                    else None
                ),
                "evidence_excerpt": "",
            }
            low, high = 0, len(source)
            # The final index must remain viable even when the absolute run
            # directory is long. Whitespace is not evidence, so use compact
            # JSON before deciding that the mandatory references cannot fit.
            best = json.dumps(
                base, ensure_ascii=False, separators=(",", ":")
            )
            if len(best.encode("utf-8")) > max_bytes:
                raise WorkflowError(
                    "context capsule 上限不足以保存强制证据引用；"
                    f"需要至少 {len(best.encode('utf-8'))} bytes"
                )
            while low <= high:
                middle = (low + high) // 2
                excerpt = source[:middle]
                if middle < len(source):
                    excerpt += "…[TRUNCATED]"
                candidate = dict(base, evidence_excerpt=excerpt)
                candidate_text = json.dumps(
                    candidate, ensure_ascii=False, separators=(",", ":")
                )
                if len(candidate_text.encode("utf-8")) <= max_bytes:
                    best = candidate_text
                    low = middle + 1
                else:
                    high = middle - 1
            rendered = best
    path = run_dir / f"context-{stage}.json"
    atomic_write_text(path, rendered)
    metadata = {
        "stage": stage,
        "path": str(path),
        "bytes": len(rendered.encode("utf-8")),
        "max_bytes": max_bytes,
        "truncated": truncated,
        "fields": sorted(json.loads(rendered)),
    }
    return path, metadata


def classify_validation_failure(
    outcomes: list[dict[str, Any]], *, phase: str = "baseline"
) -> str | None:
    failed = [item for item in outcomes if item.get("exit_code") != 0]
    if not failed:
        return None
    environment_patterns = (
        re.compile(
            r"(?im)^(?:/bin/)?(?:zsh|sh|bash)(?::\d+)?:\s*"
            r"command not found:\s*(?!\.{1,2}/)\S+\s*$"
        ),
        re.compile(r"(?im)^env: .*no such file or directory\s*$"),
        re.compile(r"(?im)^sandbox-exec: "),
        re.compile(r"(?im)^xcrun: error:"),
        re.compile(r"(?im)^xcode-select: error:.*invalid active developer path"),
        re.compile(r"(?im)^rustup(?: error)?:.*toolchain.*(?:not installed|not found)"),
        re.compile(
            r"(?im)^(?:clang(?:\+\+)?|gcc|g\+\+|cc): error: unable to "
            r"(?:make|create) temporary file: (?:operation not permitted|"
            r"permission denied|read-only file system)"
        ),
        re.compile(
            r"(?im)^(?:npm|pip|uv|cargo)(?: error)?:.*cache.*"
            r"(?:operation not permitted|permission denied|read-only file system)"
        ),
    )
    for item in failed:
        window = (_tail_error_window(item.get("log")) or "").lower()
        command = str(item.get("command") or "")
        if command == "documentation-contract":
            item["classification"] = "documentation"
        elif command == "environment-write-probe":
            item["classification"] = "environment"
        elif item.get("exit_code") == 124:
            item["classification"] = "timeout"
        elif any(pattern.search(window) for pattern in environment_patterns):
            item["classification"] = "environment"
        else:
            item["classification"] = "code_or_test"
    classifications = {str(item.get("classification")) for item in failed}
    if "environment" in classifications:
        return "environment_preflight"
    if classifications == {"documentation"}:
        return "documentation_failure"
    if classifications == {"timeout"}:
        return "validation_timeout"
    normalized_phase = phase.lower().replace("_", "-")
    if normalized_phase == "baseline":
        return "baseline_validation"
    if normalized_phase in {"coder", "coder-initial"}:
        return "coder_validation"
    if normalized_phase in {"validation-fix", "local-fix"} or normalized_phase.startswith(
        "local-fix-"
    ):
        return "local_fix_validation"
    if normalized_phase == "supervisor":
        return "supervisor_validation"
    return "post_edit_validation"


def preflight_host(
    settings: Settings, project: Path | None = None
) -> dict[str, Any]:
    if not shutil.which("sandbox-exec"):
        raise WorkflowError("验证宿主缺少 sandbox-exec")
    executable = shutil.which(settings.codex_command)
    if not executable:
        raise WorkflowError(f"reviewer 命令宿主不可用：{settings.codex_command}")
    version = ""
    required_capabilities: list[str] = []
    for command, label in (
        ([executable, "--version"], "版本探测"),
        ([executable, "exec", "--help"], "exec 能力探测"),
    ):
        try:
            result = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=10,
                check=False,
                env={**os.environ, "NO_COLOR": "1"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkflowError(f"reviewer {label}失败：{exc}") from exc
        if result.returncode != 0:
            raise WorkflowError(
                f"reviewer {label}失败（exit={result.returncode}）"
            )
        if label == "exec 能力探测":
            required = {
                "--json",
                "--output-schema",
                "--ephemeral",
                "--ignore-user-config",
                "--sandbox",
            }
            missing = sorted(flag for flag in required if flag not in result.stdout)
            if missing:
                raise WorkflowError(
                    "reviewer 缺少所需 exec 能力：" + ", ".join(missing)
                )
            required_capabilities = sorted(required)
        else:
            version = redact_sensitive_text(result.stdout.strip().splitlines()[0][:200])
    if project is not None:
        if not project.is_dir() or not os.access(project, os.R_OK | os.X_OK):
            raise WorkflowError(f"reviewer 无法读取项目目录：{project}")
        if not REVIEW_SCHEMA.is_file() or not os.access(REVIEW_SCHEMA, os.R_OK):
            raise WorkflowError(f"reviewer 无法读取评审 schema：{REVIEW_SCHEMA}")
        git_output(project, "rev-parse", "--is-inside-work-tree")
        git_output(project, "status", "--porcelain=v1", "-uall")
        git_output(project, "diff", "--no-ext-diff", "--stat", "HEAD", "--", ".")
    return {
        "stage": "host",
        "status": "passed",
        "classification": None,
        "checks": {
            "sandbox_exec": True,
            "reviewer_version": version or "available",
            "required_cli_capabilities": required_capabilities,
            "schema_readable": bool(project is not None),
            "repository_read_probe": bool(project is not None),
        },
    }


def finding_counts_by_round(
    reviews: list[tuple[str, dict[str, Any]]]
) -> dict[str, Any]:
    per_round = {
        name: classify_findings(review) for name, review in reviews
    }
    totals = {severity: 0 for severity in ("P0", "P1", "P2", "P3")}
    for counts in per_round.values():
        for severity, count in counts.items():
            totals[severity] += count
    return {"total": totals, "by_round": per_round}


def validations_passed(outcomes: list[dict[str, Any]]) -> bool:
    return bool(outcomes) and all(item.get("exit_code") == 0 for item in outcomes)


def validate_review_contract(review: dict[str, Any]) -> None:
    """Validate the review schema even when a CLI ignores output-schema."""
    required_top = {"verdict", "summary", "findings", "tests"}
    if set(review) != required_top:
        raise StructuredResultError("评审结果顶层字段不符合 schema")
    if review.get("verdict") not in {"pass", "fail"}:
        raise StructuredResultError("评审结果缺少有效 verdict")
    if not isinstance(review.get("summary"), str):
        raise StructuredResultError("评审结果 summary 必须是字符串")
    tests = review.get("tests")
    if not isinstance(tests, list) or not all(isinstance(item, str) for item in tests):
        raise StructuredResultError("评审结果 tests 必须是字符串数组")
    findings = review.get("findings")
    if not isinstance(findings, list):
        raise StructuredResultError("评审结果 findings 必须是数组")
    if review.get("verdict") == "fail" and not findings:
        raise StructuredResultError("评审结果为 fail 时必须提供 finding")
    required_finding = {
        "severity",
        "category",
        "title",
        "file",
        "line",
        "description",
        "required_fix",
    }
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != required_finding:
            raise StructuredResultError("finding 字段不符合 schema")
        if finding.get("severity") not in {"P0", "P1", "P2", "P3"}:
            raise StructuredResultError("finding severity 不符合 schema")
        if finding.get("category") not in {
            "security", "data_loss", "correctness", "documentation",
            "performance", "reliability", "testing", "other",
        }:
            raise StructuredResultError("finding category 不符合 schema")
        for key in ("title", "file", "description", "required_fix"):
            if not isinstance(finding.get(key), str):
                raise StructuredResultError(f"finding {key} 必须是字符串")
        line = finding.get("line")
        if line is not None and (type(line) is not int or line < 1):
            raise StructuredResultError("finding line 必须是正整数或 null")


def normalize_review(
    review: dict[str, Any], validations: list[dict[str, Any]]
) -> dict[str, Any]:
    if review.get("verdict") not in {"pass", "fail"}:
        raise StructuredResultError("评审结果缺少有效 verdict")
    findings = review.get("findings")
    if not isinstance(findings, list):
        raise StructuredResultError("评审结果 findings 必须是数组")
    if review.get("verdict") == "fail" and not findings:
        raise StructuredResultError("评审结果为 fail 时必须提供 finding")
    if any(
        not isinstance(item, dict)
        or item.get("severity") not in {"P0", "P1", "P2", "P3"}
        for item in findings
    ):
        raise StructuredResultError("评审结果包含无效 finding")
    blocking = any(
        isinstance(item, dict) and item.get("severity") in {"P0", "P1", "P2"}
        for item in findings
    )
    blocking = blocking or any(
        isinstance(item, dict)
        and item.get("category")
        in {"security", "data_loss", "reliability", "other"}
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
                "category": "testing",
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
    if blocking:
        review["verdict"] = "fail"
    elif review["verdict"] == "fail":
        # Only a nonempty, schema-valid P3-only result may be normalized from
        # fail to advisory pass. Empty or ambiguous failures stay blocking.
        review["verdict"] = "pass" if findings else "fail"
    else:
        review["verdict"] = "pass"
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


def opencode_capability_probe(settings: Settings) -> dict[str, Any]:
    executable = shutil.which(settings.opencode_command)
    required_flags = {
        "--model", "--agent", "--format", "--session", "--dir", "--pure"
    }
    if settings.opencode_provider == "deepseek":
        api_key_configured = bool(
            os.environ.get(settings.deepseek_api_key_env, "").strip()
        )
        if not executable:
            return {
                "stage": "opencode-capability",
                "status": "failed",
                "classification": "local_backend_preflight",
                "command_available": False,
                "required_flags": sorted(required_flags),
                "missing_flags": sorted(required_flags),
                "provider": "deepseek",
                "api_key_configured": api_key_configured,
            }
        try:
            result = subprocess.run(
                [executable, "run", "--help"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=15,
                check=False,
                env={**os.environ, "NO_COLOR": "1"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "stage": "opencode-capability",
                "status": "failed",
                "classification": "local_backend_preflight",
                "command_available": True,
                "error": redact_live_text(str(exc)),
                "provider": "deepseek",
                "api_key_configured": api_key_configured,
            }
        missing = sorted(flag for flag in required_flags if flag not in result.stdout)
        return {
            "stage": "opencode-capability",
            "status": "passed" if result.returncode == 0 and not missing else "failed",
            "classification": (
                None
                if result.returncode == 0 and not missing
                else "local_backend_preflight"
            ),
            "command_available": True,
            "exit_code": result.returncode,
            "required_flags": sorted(required_flags),
            "missing_flags": missing,
            "provider": "deepseek",
            "api_key_configured": api_key_configured,
        }
    # ollama
    if not is_loopback_ollama_endpoint(settings.ollama_host):
        return {
            "stage": "opencode-capability",
            "status": "failed",
            "classification": "local_backend_preflight",
            "command_available": bool(executable),
            "ollama_endpoint_loopback": False,
            "error": "OpenCode 主后端只允许回环 Ollama endpoint",
        }
    if not executable:
        return {
            "stage": "opencode-capability",
            "status": "failed",
            "classification": "local_backend_preflight",
            "command_available": False,
            "required_flags": sorted(required_flags),
            "missing_flags": sorted(required_flags),
        }
    try:
        result = subprocess.run(
            [executable, "run", "--help"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            check=False,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "stage": "opencode-capability",
            "status": "failed",
            "classification": "local_backend_preflight",
            "command_available": True,
            "error": redact_live_text(str(exc)),
        }
    missing = sorted(flag for flag in required_flags if flag not in result.stdout)
    return {
        "stage": "opencode-capability",
        "status": "passed" if result.returncode == 0 and not missing else "failed",
        "classification": (
            None
            if result.returncode == 0 and not missing
            else "local_backend_preflight"
        ),
        "command_available": True,
        "ollama_endpoint_loopback": True,
        "exit_code": result.returncode,
        "required_flags": sorted(required_flags),
        "missing_flags": missing,
    }


def ensure_opencode_available(settings: Settings) -> dict[str, Any]:
    probe = opencode_capability_probe(settings)
    if probe["status"] != "passed":
        missing = ", ".join(probe.get("missing_flags", [])) or "capability probe"
        raise WorkflowError(f"OpenCode 后端预检失败：{missing}")
    return probe


def is_loopback_ollama_endpoint(value: str) -> bool:
    """OpenCode may contact only the host-local Ollama HTTP endpoint."""
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        and not parsed.username
        and not parsed.password
        and (port is None or 1 <= port <= 65535)
    )


def ollama_seatbelt_remote(value: str) -> str:
    """Return the one Seatbelt-valid loopback host:port for OpenCode.

    macOS Seatbelt's ``remote ip`` predicate accepts ``localhost`` (and a
    port), but rejects numeric IPv4/IPv6 literals even when they are loopback.
    Normalizing every accepted loopback URL to ``localhost:<port>`` preserves
    the exact configured port without producing an invalid sandbox profile.
    """
    if not is_loopback_ollama_endpoint(value):
        raise WorkflowError("OpenCode 主后端只允许有效的回环 Ollama endpoint")
    parsed = urllib.parse.urlsplit(value)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"localhost:{port}"


def _validate_deepseek_base_url(value: str) -> int:
    """Validate the DeepSeek base URL configuration and return the port.

    Only HTTPS endpoints with an exact allowed host and no embedded userinfo
    are permitted.  Invalid configurations fail closed with WorkflowError.
    """
    if not value:
        raise WorkflowError("DeepSeek base_url 不能为空")
    try:
        parsed = urllib.parse.urlsplit(value)
    except (TypeError, ValueError) as exc:
        raise WorkflowError(f"DeepSeek base_url 格式无效：{value}") from exc
    if parsed.scheme != "https":
        raise WorkflowError("DeepSeek base_url 必须使用 https 协议")
    if parsed.username or parsed.password:
        raise WorkflowError("DeepSeek base_url 不得包含 userinfo")
    allowed_hosts = {parsed.hostname} if parsed.hostname == "api.deepseek.com" else set()
    if parsed.hostname not in allowed_hosts:
        raise WorkflowError(f"DeepSeek base_url 域名不在允许列表中：{parsed.hostname}")
    return parsed.port or 443


def deepseek_seatbelt_remote(base_url: str) -> str:
    """Return the Seatbelt-valid network permission for the DeepSeek provider.

    macOS Seatbelt's ``remote ip`` predicate only accepts ``*`` (any host)
    or ``localhost``-based addresses; domain-name restrictions such as
    ``api.deepseek.com`` are unsupported and cause ``sandbox-exec`` to
    fail with a ``kleene star`` syntax error.  The actual network
    convergence is enforced by four layers:

    * The Agent has no web-fetch / MCP tools (webfetch, MCP, skill, task
      are all denied).
    * The bash allowlist contains no network commands.
    * ``_validate_deepseek_base_url`` requires an exact https API base URL
      before the runtime is ever created.
    * The API key is only meaningful for the DeepSeek API endpoint.
    """
    port = _validate_deepseek_base_url(base_url)
    return f"*:{port}"


def _opencode_agent_body() -> str:
    text = OPENCODE_AGENT_TEMPLATE.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) != 3 or not parts[2].strip():
        raise WorkflowError("OpenCode Agent 模板格式无效")
    return parts[2].strip() + "\n"


def _yaml_rule(key: str, value: str, indent: int = 4) -> str:
    return " " * indent + json.dumps(key, ensure_ascii=False) + f": {value}"


def prepare_opencode_runtime(
    settings: Settings,
    project: Path,
    model: str,
    run_dir: Path,
    *,
    risk: dict[str, Any],
    run_date: str | None = None,
) -> tuple[Path, dict[str, str]]:
    """Create an isolated OpenCode config without loading global user state."""
    is_deepseek = settings.opencode_provider == "deepseek"
    if is_deepseek:
        deepseek_remote = deepseek_seatbelt_remote(settings.deepseek_base_url)
        api_key_env = settings.deepseek_api_key_env
        api_key_value = os.environ.get(api_key_env, "").strip()
        if not api_key_value:
            raise WorkflowError(f"DeepSeek API 密钥环境变量 {api_key_env} 未设置或为空")
        provider_model = f"deepseek/{settings.deepseek_model}"
    else:
        if not is_loopback_ollama_endpoint(settings.ollama_host):
            raise WorkflowError("OpenCode 主后端只允许回环 Ollama endpoint")
        provider_model = f"ollama/{model}"
    runtime = run_dir / "opencode-runtime"
    if runtime.is_symlink():
        raise WorkflowError("OpenCode 运行目录不能是符号链接")
    # Keep the generated config and Agent in OpenCode's native XDG layout:
    # $XDG_CONFIG_HOME/opencode/{opencode.json,agents/...}.  OPENCODE_CONFIG
    # is retained as a pin, but must not be relied on to relocate discovery.
    config_home = runtime / "config"
    config_dir = config_home / "opencode"
    agents_dir = config_dir / "agents"
    home = runtime / "home"
    data = runtime / "data"
    state = runtime / "state"
    cache = runtime / "cache"
    temporary = runtime / "tmp"
    for directory in (runtime, config_dir, agents_dir, home, data, state, cache, temporary):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)

    # DeepSeek 路线需要 models.dev 注册表才能初始化 provider。开放环境
    # opencode 会 fetch https://models.dev/api.json，隔离沙箱网络不可达，
    # 所以必须把宿主缓存 ~/.cache/opencode/models.json 种入隔离缓存目录。
    # 该文件为公开模型注册表数据，不含凭据；openapi key 已于 config 中设为
    # {env:...} 占位。
    if is_deepseek:
        host_cache_dir = Path.home() / ".cache" / "opencode"
        host_models = host_cache_dir / "models.json"
        if not host_models.is_file() or host_models.is_symlink():
            raise WorkflowError(
                f"DeepSeek 路线需要 hosts models.dev 缓存，但 {host_models} "
                "不存在或不是常规文件。请在常规环境先运行 opencode 一次以生成缓存。"
            )
        isolated_cache_dir = cache / "opencode"
        isolated_cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        isolated_cache_dir.chmod(0o700)
        shutil.copy2(host_models, isolated_cache_dir / "models.json")

    plan_path = run_dir / "plan.md"
    allowed = set(plan_declared_project_paths(project, plan_path))
    if any(is_opencode_project_control_path(Path(path)) for path in allowed):
        raise WorkflowError(
            "计划包含 OpenCode 项目控制文件；此类变更必须由云端 supervisor 接管"
        )
    bash_rules = [_yaml_rule("*", "deny")]
    for command in load_validation_commands(project):
        bash_rules.append(_yaml_rule(command, "allow"))
    for command in ("git status --short*", "git diff --check*", "git diff *"):
        bash_rules.append(_yaml_rule(command, "allow"))

    steps = (
        settings.opencode_medium_max_steps
        if risk.get("classification") == "medium"
        else settings.opencode_low_max_steps
    )
    if not 1 <= steps <= 32:
        raise WorkflowError("OpenCode Agent steps 必须在 1 到 32 之间")
    if not 0.0 <= settings.opencode_temperature <= 0.3:
        raise WorkflowError("OpenCode Agent temperature 必须在 0.0 到 0.3 之间")

    frontmatter = [
        "---",
        "description: 受 Local AI MVP Builder 监督的最小变更本地编码 Agent",
        "mode: primary",
        f"model: {provider_model}",
        f"temperature: {settings.opencode_temperature}",
        f"steps: {steps}",
        "permission:",
        _yaml_rule("*", "deny", 2),
        "  read:",
        _yaml_rule("*", "allow"),
        _yaml_rule("*.env", "deny"),
        _yaml_rule("*.env.*", "deny"),
        _yaml_rule(".git/**", "deny"),
        "  edit:",
        # OpenCode 1.18.x accepts granular edit maps in opencode.json, but
        # does not apply an Agent-Markdown granular map to its edit tool.  A
        # scalar allow is therefore required for a non-interactive run to
        # make any scoped change.  The Agent still receives the exact scope
        # in its prompt; the candidate-diff gate and transactional promotion
        # remain the authoritative scope enforcement layers. The system
        # sandbox confines this Agent to the disposable candidate, so this
        # cannot grant access to the real target workspace.
        "    allow",
        "  bash:",
        *bash_rules,
        "  list: allow",
        "  grep: allow",
        "  todowrite: allow",
        "  task: deny",
        "  skill: deny",
        "  webfetch: deny",
        # OpenCode 向上查找 .git 判定项目根；候选副本刻意无 .git，
        # 将候选目录误判为外部目录。把 external_directory 设为 allow，
        # 真正的文件系统边界由 Seatbelt profile 强制（候选副本之外全部
        # deny），本地 Agent 永远无法接触真实目标。
        "  external_directory: allow",
        "  question: deny",
        "  doom_loop: deny",
        "---",
        "",
        _opencode_agent_body(),
    ]
    agent_path = agents_dir / f"{settings.opencode_agent}.md"
    atomic_write_text(agent_path, "\n".join(frontmatter))

    if is_deepseek:
        config = {
            "$schema": "https://opencode.ai/config.json",
            "model": provider_model,
            "provider": {
                "deepseek": {
                    "npm": "@ai-sdk/deepseek",
                    "name": "DeepSeek cloud inference",
                    "apiKey": "{env:" + api_key_env + "}",
                    "options": {"baseURL": settings.deepseek_base_url.rstrip("/")},
                    "models": {settings.deepseek_model: {"name": "DeepSeek V4 Pro"}},
                }
            },
            "mcp": {},
        }
    else:
        config = {
            "$schema": "https://opencode.ai/config.json",
            "model": f"ollama/{model}",
            "provider": {
                "ollama": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Local inference",
                    "options": {"baseURL": settings.ollama_host.rstrip("/") + "/v1"},
                    "models": {model: {"name": "Local deployment model"}},
                }
            },
            "mcp": {},
        }
    config_path = config_dir / "opencode.json"
    atomic_write_text(
        config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    )
    environment = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(data),
        "XDG_STATE_HOME": str(state),
        "XDG_CACHE_HOME": str(cache),
        "TMPDIR": str(temporary),
        "OPENCODE_CONFIG": str(config_path),
        "OPENCODE_CONFIG_DIR": str(config_dir),
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "no_proxy": "localhost,127.0.0.1,::1",
    }
    return config_path, environment


def write_opencode_profile(
    workspace: Path,
    runtime: Path,
    run_dir: Path,
    executable: Path,
    provider_host: str,
) -> Path:
    """Deny OpenCode access outside its sanitized workspace and runtime."""
    workspace = workspace.resolve(strict=True)
    runtime = runtime.resolve(strict=True)
    run_dir = run_dir.resolve(strict=True)
    executable = executable.resolve(strict=True)
    allowed_reads = [
        workspace,
        runtime,
        run_dir,
        executable,
        executable.parent,
        Path("/System"),
        Path("/usr"),
        Path("/bin"),
        Path("/sbin"),
        Path("/Library"),
        Path("/Applications"),
        Path("/opt/homebrew"),
        Path("/private/var/select"),
        *user_toolchain_read_roots(Path.home()),
    ]
    lines = [
        "(version 1)",
        "(deny default)",
        '(import "system.sb")',
        "(allow process*)",
        "(allow sysctl-read)",
        f'(allow network-outbound (remote ip "{provider_host}"))',
        # macOS 域名解析走 mDNSResponder 的 unix socket,Seatbelt 将其归为
        # network-outbound;实测 curl/bun 均被 deny(1) 拦在此(2026-08-01)。
        '(allow network-outbound (remote unix-socket (path "/private/var/run/mDNSResponder")))',
        f'(allow file-write* (subpath "{seatbelt_escape(workspace)}"))',
        f'(allow file-write* (subpath "{seatbelt_escape(runtime)}"))',
    ]
    lines.extend(
        f'(allow file-read* (subpath "{seatbelt_escape(path)}"))'
        for path in allowed_reads
        if path.is_dir()
    )
    lines.extend(
        f'(allow file-read* (literal "{seatbelt_escape(path)}"))'
        for path in allowed_reads
        if path.exists()
    )
    ancestors = {
        parent
        for path in allowed_reads
        if path.exists()
        for parent in path.resolve().parents
    }
    lines.extend(
        f'(allow file-read* (literal "{seatbelt_escape(path)}"))'
        for path in sorted(ancestors, key=str)
    )
    lines.extend(
        (
            f'(deny file-read* (subpath "{seatbelt_escape(workspace / ".git")}"))',
            f'(deny file-write* (subpath "{seatbelt_escape(workspace / ".git")}"))',
            f'(deny file-read* (literal "{seatbelt_escape(workspace / "opencode.json")}"))',
            f'(deny file-write* (literal "{seatbelt_escape(workspace / "opencode.json")}"))',
            f'(deny file-read* (literal "{seatbelt_escape(workspace / "opencode.jsonc")}"))',
            f'(deny file-write* (literal "{seatbelt_escape(workspace / "opencode.jsonc")}"))',
            f'(deny file-read* (subpath "{seatbelt_escape(workspace / ".opencode")}"))',
            f'(deny file-write* (subpath "{seatbelt_escape(workspace / ".opencode")}"))',
        )
    )
    lines.extend(
        f'(deny file-read* (regex #"{pattern}"))'
        for pattern in project_secret_regexes(workspace)
    )
    lines.extend(
        f'(deny file-write* (regex #"{pattern}"))'
        for pattern in project_secret_regexes(workspace)
    )
    profile = run_dir / "opencode.sb"
    atomic_write_text(profile, "\n".join(lines) + "\n")
    return profile


def opencode_output_metadata(output: str) -> tuple[str, str]:
    sessions: set[str] = set()
    final_text = ""
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        session = event.get("sessionID")
        if isinstance(session, str) and session:
            sessions.add(session)
        part = event.get("part")
        if event.get("type") == "text" and isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                final_text = text
    if len(sessions) != 1:
        raise StructuredResultError("OpenCode 输出必须包含唯一 Session ID")
    if not final_text.strip():
        raise StructuredResultError("OpenCode 输出缺少最终文本摘要")
    return next(iter(sessions)), final_text


def make_run_dir(project: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(str(project).encode()).hexdigest()[:8]
    state_root = Path(
        os.environ.get(
            "LOCAL_AI_MVP_STATE_DIR",
            str(Path.home() / ".local/share/local-ai-mvp-builder"),
        )
    ).expanduser()
    if not state_root.is_absolute():
        state_root = Path.cwd() / state_root
    if state_root.is_symlink():
        raise WorkflowError("运行记录目录不能是符号链接")
    state_root = state_root.resolve(strict=False)
    project_root = project.resolve()
    runs_candidate = state_root / "runs"
    if runs_candidate.is_symlink():
        raise WorkflowError("运行记录 runs 目录不能是符号链接")
    runs_root = runs_candidate.resolve(strict=False)
    if state_root.is_relative_to(project_root) or runs_root.is_relative_to(
        project_root
    ):
        raise WorkflowError("运行记录目录必须位于目标 workspace 之外")
    if state_root.is_symlink() or runs_root.is_symlink():
        raise WorkflowError("运行记录目录不能是符号链接")
    runs_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    runs_root.chmod(0o700)
    path = runs_root / f"{stamp}-{project.name}-{digest}"
    path.mkdir(parents=True, exist_ok=False)
    path.chmod(0o700)
    if path.resolve().is_relative_to(project_root):
        raise WorkflowError("运行记录目录必须位于目标 workspace 之外")
    return path


def call_opencode_coder(
    settings: Settings,
    project: Path,
    model: str,
    prompt: str,
    run_dir: Path,
    stage: str,
    *,
    risk: dict[str, Any],
    session_id: str | None = None,
    live: bool = False,
    run_date: str | None = None,
) -> subprocess.CompletedProcess[str]:
    ensure_opencode_available(settings)
    config_path, environment = prepare_opencode_runtime(
        settings, project, model, run_dir, risk=risk, run_date=run_date
    )
    executable_value = shutil.which(settings.opencode_command)
    if not executable_value:
        raise WorkflowError("OpenCode 可执行文件在能力预检后消失")
    executable = Path(executable_value)
    is_deepseek = settings.opencode_provider == "deepseek"
    if is_deepseek:
        provider_host = deepseek_seatbelt_remote(settings.deepseek_base_url)
    else:
        provider_host = ollama_seatbelt_remote(settings.ollama_host)
    profile = write_opencode_profile(
        project,
        config_path.parent.parent.parent,
        run_dir,
        executable,
        provider_host,
    )
    # subprocess cwd does not rewrite an inherited PWD. OpenCode consults PWD
    # while resolving its workspace, so retain the sanitized implementation
    # directory rather than leaking the frontend Agent's repository path.
    environment = {
        **environment,
        "PWD": str(project.resolve()),
        "OLDPWD": str(project.resolve()),
    }
    if is_deepseek:
        api_key_env = settings.deepseek_api_key_env
        api_key_value = os.environ.get(api_key_env, "").strip()
        if api_key_value:
            environment[api_key_env] = api_key_value
    runtime = config_path.parent.parent.parent
    # ``opencode run`` documents ``--file`` as its non-positional task input.
    # Store the complete prompt in the private runtime only for the duration
    # of this child invocation; argv contains a fixed, non-sensitive cue so
    # process inspection and timeout/watchdog evidence cannot expose the
    # approved plan or capsule body.
    prompt_path = runtime / "tmp" / f"{stage}-approved-prompt.md"
    atomic_write_text(prompt_path, prompt)
    model_ref = f"deepseek/{settings.deepseek_model}" if is_deepseek else f"ollama/{model}"
    command = [
        "sandbox-exec",
        "-f",
        str(profile),
        str(executable),
        "run",
        "--pure",
        "--format",
        "json",
        "--model",
        model_ref,
        "--agent",
        settings.opencode_agent,
        "--dir",
        str(project),
        "--file",
        str(prompt_path),
        "--title",
        f"Local AI MVP Builder {stage}",
    ]
    if session_id:
        command.extend(["--session", session_id])
    # Keep the only positional message last. This avoids relying on argument
    # parsing behavior for options placed after a positional message when the
    # focused fixer resumes an OpenCode Session.
    command.append("Read the attached approved task and implement it within its stated scope.")
    logged_command = list(command)
    try:
        result = run_command(
            command,
            cwd=project,
            timeout=settings.coder_timeout_seconds,
            stdin_text=None,
            log_path=run_dir / f"{stage}.log",
            live=live,
            monitor=True,
            live_channel="OPENCODE",
            json_events=True,
            idle_timeout=settings.local_stall_timeout_seconds,
            extra_env=environment,
            command_for_log=logged_command,
            clean_environment=True,
        )
    finally:
        prompt_path.unlink(missing_ok=True)
    actual_session, final_text = opencode_output_metadata(result.stdout or "")
    if session_id and actual_session != session_id:
        raise StructuredResultError("OpenCode fixer 未续接批准 Session")
    atomic_write_text(
        run_dir / f"{stage}-last-message.txt",
        redact_persistent_output(final_text),
    )
    setattr(result, "session_id", actual_session)
    return result


def call_local_coder(
    settings: Settings,
    project: Path,
    model: str,
    prompt: str,
    run_dir: Path,
    stage: str,
    live: bool = False,
    *,
    backend: str = "codex-ollama",
    risk: dict[str, Any] | None = None,
    session_id: str | None = None,
    run_date: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if backend == "opencode":
        return call_opencode_coder(
            settings,
            project,
            model,
            prompt,
            run_dir,
            stage,
            risk=risk or {"classification": "low"},
            session_id=session_id,
            live=live,
            run_date=run_date,
        )
    if backend != "codex-ollama":
        raise WorkflowError(f"未知本地后端：{backend}")
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
    result: subprocess.CompletedProcess[str] | None = None
    primary_error: BaseException | None = None
    try:
        result = run_command(
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
        return result
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            redact_persisted_file(last_message)
        except BaseException as exc:
            reported = getattr(primary_error, "returncode", None)
            if isinstance(primary_error, subprocess.TimeoutExpired):
                reported = 124
            if type(reported) is not int and result is not None:
                reported = result.returncode
            if type(reported) is int:
                exc.returncode = reported
            raise


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
        "--json",
        "--output-schema",
        str(REVIEW_SCHEMA),
        "-o",
        str(output_path),
        "-",
    ]
    process: subprocess.CompletedProcess[str] | None = None
    primary_error: BaseException | None = None
    try:
        process = run_command(
            command,
            cwd=project,
            timeout=settings.review_timeout_seconds,
            stdin_text=prompt,
            log_path=run_dir / f"review-{round_number}.log",
        )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            redact_persisted_file(output_path)
        except BaseException as exc:
            reported = getattr(primary_error, "returncode", None)
            if isinstance(primary_error, subprocess.TimeoutExpired):
                reported = 124
            if type(reported) is not int and process is not None:
                reported = process.returncode
            if type(reported) is int:
                exc.returncode = reported
            raise
    try:
        review = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StructuredResultError(
            f"无法解析结构化评审结果：{output_path}"
        ) from exc
    if not isinstance(review, dict):
        raise StructuredResultError(
            f"结构化评审结果根节点必须是对象：{output_path}"
        )
    review = sanitize_json_value(review)
    validate_review_contract(review)
    try:
        atomic_write_text(
            output_path, json.dumps(review, ensure_ascii=False, indent=2)
        )
    except Exception as exc:
        exc.returncode = process.returncode if process is not None else 0
        raise
    return review


def call_supervisor(
    settings: Settings,
    project: Path,
    prompt: str,
    run_dir: Path,
    stage: str = "supervisor",
) -> subprocess.CompletedProcess[str]:
    last_message = run_dir / f"{stage}-last-message.txt"
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
        "--json",
        "-o",
        str(last_message),
        "-",
    ]
    result: subprocess.CompletedProcess[str] | None = None
    primary_error: BaseException | None = None
    try:
        result = run_command(
            command,
            cwd=project,
            timeout=settings.supervisor_timeout_seconds,
            stdin_text=prompt,
            log_path=run_dir / f"{stage}.log",
            json_events=True,
        )
        return result
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            redact_persisted_file(last_message)
        except BaseException as exc:
            reported = getattr(primary_error, "returncode", None)
            if isinstance(primary_error, subprocess.TimeoutExpired):
                reported = 124
            if type(reported) is not int and result is not None:
                reported = result.returncode
            if type(reported) is int:
                exc.returncode = reported
            raise


def write_summary(
    run_dir: Path,
    *,
    project: Path,
    model: str,
    status: str,
    review: dict[str, Any] | None,
    validations: list[dict[str, Any]],
    usage_stages: list[dict[str, Any]] | None = None,
    workflow: dict[str, Any] | None = None,
    risk: dict[str, Any] | None = None,
    reviews: list[tuple[str, dict[str, Any]]] | None = None,
    capsules: list[dict[str, Any]] | None = None,
    preflight: list[dict[str, Any]] | None = None,
    settings: Settings | None = None,
    git_status_override: str | None = None,
    version_control_override: dict[str, Any] | None = None,
    handoff_snapshot: dict[str, Any] | None = None,
) -> None:
    usage_stages = usage_stages or []
    workflow = dict(workflow or {})
    settings = settings or load_settings()
    workflow.setdefault(
        "cloud_calls",
        sum(
            item.get("backend") in {"cloud-review", "cloud-supervisor"}
            for item in usage_stages
        ),
    )
    workflow.setdefault(
        "takeover",
        {
            "occurred": False,
            "classification": None,
            "reason": None,
            "round": None,
        },
    )
    workflow["validation_counts"] = validation_counts(validations)
    workflow["finding_counts"] = finding_counts_by_round(reviews or [])
    workflow["strategy"] = settings.strategy
    payload = {
        "project": str(project),
        "model": model,
        "status": status,
        "review": review,
        "validations": validations,
        "preflight": preflight or [],
        "risk": risk or {"classification": "high", "declared": False},
        "usage": summarize_usage(
            usage_stages,
            soft_budget=settings.cloud_soft_token_budget,
            max_cloud_calls=settings.cloud_max_calls_per_run,
        ),
        "workflow": workflow,
        "efficiency": {
            "claim": "no_baseline",
            "baseline_run": None,
            "cloud_token_difference": None,
        },
        "context_capsules": capsules or [],
        "handoff_snapshot": handoff_snapshot,
        "git_status": (
            git_status_override
            if git_status_override is not None
            else git_output(project, "status", "--short")
        ),
        "version_control": (
            version_control_override
            if version_control_override is not None
            else version_control_snapshot(project)
        ),
    }
    serialized = json.dumps(
        sanitize_json_value(payload), ensure_ascii=False, indent=2
    )
    atomic_write_text(run_dir / "summary.json", serialized)


def write_failure_summary(
    run_dir: Path,
    *,
    project: Path,
    model: str,
    error: BaseException,
    review: dict[str, Any] | None,
    validations: list[dict[str, Any]],
    usage_stages: list[dict[str, Any]] | None = None,
    workflow: dict[str, Any] | None = None,
    risk: dict[str, Any] | None = None,
    reviews: list[tuple[str, dict[str, Any]]] | None = None,
    capsules: list[dict[str, Any]] | None = None,
    preflight: list[dict[str, Any]] | None = None,
    settings: Settings | None = None,
) -> None:
    try:
        status = git_output(project, "status", "--short")
    except Exception as git_error:
        status = f"unavailable: {git_error}"
    try:
        version_control = version_control_snapshot(project)
    except Exception as git_error:
        version_control = {
            "branch": None,
            "base_commit": None,
            "remotes": [],
            "remote_configured": False,
            "commit_created": False,
            "pushed": False,
            "error": f"unavailable: {git_error}",
        }
    write_summary(
        run_dir,
        project=project,
        model=model,
        status="needs_manual_attention",
        review=review,
        validations=validations,
        usage_stages=usage_stages,
        workflow=workflow,
        risk=risk,
        reviews=reviews,
        capsules=capsules,
        preflight=preflight,
        settings=settings,
        git_status_override=status,
        version_control_override=version_control,
    )
    summary_path = run_dir / "summary.json"
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    payload["error"] = redact_sensitive_text(f"{type(error).__name__}: {error}")
    payload["git_status"] = redact_sensitive_text(status)
    atomic_write_text(
        summary_path, json.dumps(payload, ensure_ascii=False, indent=2)
    )


def command_doctor(args: argparse.Namespace) -> int:
    settings = load_settings()
    plan_value = getattr(args, "plan", None)
    model_alias = getattr(args, "model", "primary")
    risk: dict[str, Any] | None = None
    if plan_value:
        plan_path = Path(plan_value).expanduser().resolve()
        if not plan_path.is_file():
            raise WorkflowError(f"计划文件不存在：{plan_path}")
        risk = parse_risk_classification(
            plan_path.read_text(encoding="utf-8")
        )
    # Older Python callers constructed a minimal Namespace before the backend
    # selector existed. Keep that programmatic surface pinned to the former
    # Codex/Ollama path; every current CLI parser exposes the option and thus
    # follows the configured OpenCode default.
    local_backend = (
        getattr(args, "local_backend", None) or settings.local_backend
        if hasattr(args, "local_backend")
        else "codex-ollama"
    )
    if local_backend not in {"opencode", "codex-ollama"}:
        raise WorkflowError("local.backend 必须是 opencode 或 codex-ollama")
    opencode_control_plan = bool(
        plan_value
        and local_backend == "opencode"
        and plan_declares_opencode_project_controls(Path.cwd(), plan_path)
    )
    local_model_required = not risk or (
        risk["classification"] != "high" and not opencode_control_plan
    )
    checks = {
        "codex": shutil.which(settings.codex_command),
        "sandbox_exec": shutil.which("sandbox-exec"),
        "aria2c": shutil.which("aria2c"),
        "git": shutil.which("git"),
        "risk": risk,
        "local_model_required": local_model_required,
        "local_backend": local_backend,
        "opencode_control_plan": opencode_control_plan,
    }
    required = ["codex", "sandbox_exec", "git"]
    if local_model_required:
        checks["ollama"] = shutil.which("ollama")
        required.append("ollama")
        if local_backend == "opencode":
            checks["opencode"] = opencode_capability_probe(settings)
            checks["opencode_available"] = checks["opencode"]["status"] == "passed"
            required.append("opencode_available")
            # Never probe an arbitrary endpoint merely to report that the
            # OpenCode route would reject it. The capability probe validates
            # loopback syntax before any Ollama HTTP request.
            if checks["opencode"].get("ollama_endpoint_loopback") is False:
                print(json.dumps(checks, ensure_ascii=False, indent=2))
                return 1
        if checks["ollama"]:
            models = sorted(ollama_models(settings.ollama_host))
            selected_model = model_id(model_alias)
            normalized = (
                selected_model
                if ":" in selected_model
                else selected_model + ":latest"
            )
            checks["ollama_models"] = models
            checks["selected_model"] = selected_model
            checks["selected_model_available"] = (
                selected_model in models or normalized in models
            )
            required.append("selected_model_available")
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if all(checks.get(name) for name in required) else 1


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
    atomic_write_text(
        run_dir / "watchdog.json",
        json.dumps(event, ensure_ascii=False, indent=2),
    )
    return {
        "verdict": "fail",
        "summary": f"本地 AI 在 {stage} 阶段异常，看门狗已触发 Codex 接管。",
        "findings": [
            {
                "severity": "P1",
                "category": "reliability",
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
    project = Path(args.project).expanduser().resolve()
    descriptor, _lock_path = acquire_project_run_lock(project)
    try:
        return _command_run_locked(args)
    finally:
        release_project_run_lock(descriptor)


def _command_run_locked(args: argparse.Namespace) -> int:
    settings = load_settings()
    if bool(getattr(args, "allow_dirty", False)):
        raise WorkflowError("受监督工作流不允许 --allow-dirty")
    local_backend = (
        getattr(args, "local_backend", None) or settings.local_backend
        if hasattr(args, "local_backend")
        else "codex-ollama"
    )
    live = bool(getattr(args, "live", False))
    rounds = settings.max_local_review_rounds
    if rounds < 1 or rounds > 2:
        raise WorkflowError("workflow.max_local_review_rounds 必须在 1 到 2 之间")
    if settings.strategy not in {"adaptive", "legacy"}:
        raise WorkflowError("workflow.strategy 必须是 adaptive 或 legacy")
    if local_backend not in {"opencode", "codex-ollama"}:
        raise WorkflowError("local.backend 必须是 opencode 或 codex-ollama")
    if settings.opencode_provider not in {"ollama", "deepseek"}:
        raise WorkflowError("local.opencode_provider 必须是 ollama 或 deepseek")
    if not 0.0 <= settings.opencode_temperature <= 0.3:
        raise WorkflowError("local.opencode_temperature 必须在 0.0 到 0.3 之间")
    if not 1 <= settings.opencode_low_max_steps <= 32:
        raise WorkflowError("local.opencode_low_max_steps 必须在 1 到 32 之间")
    if not 1 <= settings.opencode_medium_max_steps <= 32:
        raise WorkflowError("local.opencode_medium_max_steps 必须在 1 到 32 之间")
    if settings.severe_finding_threshold < 1:
        raise WorkflowError("workflow.severe_finding_threshold 必须是正整数")
    if settings.context_capsule_max_bytes < 1024:
        raise WorkflowError("workflow.context_capsule_max_bytes 不能小于 1024")
    if settings.cloud_max_calls_per_run < 1:
        raise WorkflowError("cloud.max_calls_per_run 必须是正整数")
    if (
        settings.cloud_soft_token_budget is not None
        and settings.cloud_soft_token_budget < 1
    ):
        raise WorkflowError("cloud.soft_token_budget 必须是正整数或 0")
    project = Path(args.project).expanduser().resolve()
    target_project = project
    plan_path = Path(args.plan).expanduser().resolve()
    if not plan_path.is_file():
        raise WorkflowError(f"计划文件不存在：{plan_path}")
    plan = plan_path.read_text(encoding="utf-8")
    approved_plan_sha256 = hashlib.sha256(plan.encode("utf-8")).hexdigest()
    risk = parse_risk_classification(plan)
    model = model_id(args.model)
    validate_project(project, settings.require_clean_worktree)
    commands = load_validation_commands(project)
    if not commands:
        raise WorkflowError(
            ".mvp-ai.toml 必须至少配置一条 validation.commands，不能在无验证条件下运行"
        )
    run_dir = make_run_dir(project)
    atomic_write_text(run_dir / "plan.md", plan)
    stored_plan = run_dir / "plan.md"
    # Production run directories are external. Re-check after their setup so
    # the frozen snapshot cannot miss a concurrent change between the first
    # clean gate and preflight. Some unit fixtures inject an in-project run_dir;
    # snapshot after those fixture-owned writes to avoid a false positive.
    if not run_dir.resolve().is_relative_to(project):
        validate_project(project, settings.require_clean_worktree)
    initial_project_snapshot, commands = freeze_project_preflight_state(project)
    if not commands:
        raise WorkflowError(
            ".mvp-ai.toml 必须至少配置一条 validation.commands，不能在无验证条件下运行"
        )
    opencode_control_plan = (
        local_backend == "opencode"
        and plan_declares_opencode_project_controls(target_project, stored_plan)
    )
    today = dt.date.today().isoformat()
    all_validations: list[dict[str, Any]] = []
    latest_validations: list[dict[str, Any]] = []
    latest_review: dict[str, Any] | None = None
    usage_stages: list[dict[str, Any]] = []
    opencode_session_id: str | None = None
    implementation_container: Path | None = None
    implementation_project: Path | None = None
    implementation_promoted = False
    review_history: list[tuple[str, dict[str, Any]]] = []
    capsules: list[dict[str, Any]] = []
    preflight: list[dict[str, Any]] = []
    workflow: dict[str, Any] = {
        "cloud_calls": 0,
        "takeover": {
            "occurred": False,
            "classification": None,
            "reason": None,
            "round": None,
        },
        "failure_classification": None,
        "risk_route": (
            "direct-cloud"
            if risk["classification"] == "high" or opencode_control_plan
            else "local-first"
        ),
        "local_backend": local_backend,
        "local_session_id": None,
        "implementation_workspace": {
            "used": False,
            "promoted": False,
            "changed_files": 0,
            "backup_cleanup_pending": False,
            "backup_cleanup_warning": None,
        },
        "approved_plan": {
            "source_path": str(plan_path),
            "sha256": approved_plan_sha256,
            "stored_path": str(stored_plan),
        },
    }

    def verify_approved_plan() -> None:
        if not file_matches_sha256(stored_plan, approved_plan_sha256):
            workflow["failure_classification"] = "approved_plan_integrity"
            raise WorkflowError("批准计划副本 SHA-256 不匹配")

    def summary_kwargs(*, verify_plan: bool = True) -> dict[str, Any]:
        if verify_plan:
            verify_approved_plan()
        return {
            "usage_stages": usage_stages,
            "workflow": workflow,
            "risk": risk,
            "reviews": review_history,
            "capsules": capsules,
            "preflight": preflight,
            "settings": settings,
        }

    def cleanup_implementation() -> None:
        nonlocal implementation_container, implementation_project
        if implementation_container is None:
            return
        cleanup_baseline_workspace(implementation_container)
        implementation_container = None
        implementation_project = None

    def finish(code: int) -> int:
        cleanup_implementation()
        if (
            workflow["implementation_workspace"]["used"]
            and not implementation_promoted
        ):
            verify_project_integrity_snapshot(
                target_project, initial_project_snapshot
            )
        return code

    def guarded(action):
        try:
            return action()
        except Exception as exc:
            if isinstance(exc, EvidenceRedactionError):
                workflow["failure_classification"] = "evidence_redaction_failure"
            elif isinstance(exc, ValidationCleanupError):
                workflow["failure_classification"] = "validation_cleanup_failure"
            elif isinstance(exc, ProjectChangedError) and not workflow.get(
                "failure_classification"
            ):
                workflow["failure_classification"] = "project_changed_after_validation"
            cleanup_implementation()
            write_failure_summary(
                run_dir,
                project=target_project,
                model=model,
                error=exc,
                review=latest_review,
                validations=all_validations,
                **summary_kwargs(verify_plan=False),
            )
            raise

    def validate_stage(stage: str) -> list[dict[str, Any]]:
        enforce_content_snapshot = not run_dir.resolve().is_relative_to(project)
        before_validation = (
            project_content_snapshot(project) if enforce_content_snapshot else None
        )
        stage_commands = load_validation_commands(project)
        try:
            outcomes = run_validations(
                project, stage_commands, run_dir, stage, live=live
            )
        except ValidationCleanupError as exc:
            all_validations.extend(exc.outcomes)
            raise
        documentation = validate_development_documents(
            project, run_dir, stage, today
        )
        outcomes.append(documentation)
        all_validations.extend(outcomes)
        if before_validation is not None:
            verify_project_content_snapshot(project, before_validation)
        if live:
            result = "通过" if documentation["exit_code"] == 0 else "失败"
            emit_progress("DOCS", f"中文开发文档校验{result}")
        return outcomes

    def capsule(
        stage: str,
        validations: list[dict[str, Any]] | None = None,
        review: dict[str, Any] | None = None,
        project_snapshot: dict[str, Any] | None = None,
    ) -> str:
        try:
            verify_approved_plan()
            path, metadata = write_context_capsule(
                project,
                run_dir,
                stage=stage,
                plan_path=stored_plan,
                risk=risk,
                validations=validations,
                review=review,
                project_snapshot=project_snapshot,
                max_bytes=settings.context_capsule_max_bytes,
                run_date=today,
            )
            verify_context_capsule(path)
            verify_approved_plan()
            capsules.append(metadata)
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            if not workflow.get("failure_classification"):
                workflow["failure_classification"] = "context_capsule_failure"
            raise

    def tracked_agent(
        *,
        stage: str,
        backend: str,
        role: str,
        log_path: Path,
        action,
    ):
        verify_approved_plan()
        if not usage_stages and not run_dir.resolve().is_relative_to(project):
            try:
                verify_project_integrity_snapshot(
                    target_project, initial_project_snapshot
                )
            except Exception:
                workflow["failure_classification"] = (
                    "project_changed_during_preflight"
                )
                raise
        if backend.startswith("cloud"):
            if workflow["cloud_calls"] >= settings.cloud_max_calls_per_run:
                workflow["failure_classification"] = (
                    "cloud_call_limit_configuration"
                )
                raise WorkflowError(
                    "已达到 cloud.max_calls_per_run；为保留独立终审而停止"
                )
            workflow["cloud_calls"] += 1
        started = time.monotonic()
        exit_code = 1
        raw_output = ""
        try:
            result = action()
            exit_code = int(getattr(result, "returncode", 0) or 0)
            stdout = getattr(result, "stdout", "") or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            if not isinstance(stdout, str):
                stdout = ""
            raw_output = stdout
            return result
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            stdout = exc.stdout or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            raw_output = stdout
            raise
        except Exception as exc:
            reported = getattr(exc, "returncode", None)
            if type(reported) is int:
                exit_code = reported
            raise
        finally:
            # usage 必须用脱敏前的原始 stdout 解析:落盘日志会把 opencode
            # step_finish 的 "tokens" 用量字典按密钥子树规则抹成 [REDACTED],
            # 从日志重读只能得到 measurement=unavailable。parse_token_usage
            # 只提取数值字段,原始输出不会离开内存。
            output = raw_output
            if not output:
                try:
                    output = log_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
            usage_stages.append(
                usage_stage(
                    stage=stage,
                    backend=backend,
                    role=role,
                    output=output,
                    elapsed_seconds=time.monotonic() - started,
                    exit_code=exit_code,
                )
            )
            verify_approved_plan()
            if backend.startswith("cloud") and settings.cloud_soft_token_budget:
                current = summarize_usage(
                    usage_stages,
                    soft_budget=settings.cloud_soft_token_budget,
                    max_cloud_calls=settings.cloud_max_calls_per_run,
                )
                if current["budget"]["status"] == "exceeded":
                    if not workflow.get("budget_warning"):
                        print(
                            "WARNING：已测云端 Token 下限超过软预算；"
                            "不会中断安全修复或独立终审。"
                        )
                    workflow["budget_warning"] = True

    def reviewer_prompt(
        validations: list[dict[str, Any]], label: str,
        project_snapshot: dict[str, Any],
    ) -> str:
        validation_capsule = capsule(
            label,
            validations,
            review=latest_review,
            project_snapshot=project_snapshot,
        )
        try:
            return render_prompt(
                "reviewer.md",
                plan=str(stored_plan),
                today=today,
                validation=validation_capsule,
            )
        except Exception:
            if not workflow.get("failure_classification"):
                workflow["failure_classification"] = "reviewer_prompt_failure"
            raise

    review_number = 0
    review_snapshots: dict[str, dict[str, Any]] = {}

    def cloud_review(validations: list[dict[str, Any]], label: str) -> dict[str, Any]:
        nonlocal review_number
        review_number += 1
        enforce_content_snapshot = not run_dir.resolve().is_relative_to(project)
        expected_snapshot = project_content_snapshot(project)
        try:
            prompt = reviewer_prompt(validations, label, expected_snapshot)
        except Exception:
            if not workflow.get("failure_classification"):
                workflow["failure_classification"] = "context_capsule_failure"
            raise
        try:
            if enforce_content_snapshot:
                verify_project_content_snapshot(project, expected_snapshot)
            raw = tracked_agent(
                stage=label,
                backend="cloud-review",
                role="reviewer",
                log_path=run_dir / f"review-{review_number}.log",
                action=lambda: call_reviewer(
                    settings,
                    project,
                    prompt,
                    run_dir,
                    review_number,
                ),
            )
            if enforce_content_snapshot:
                verify_project_content_snapshot(project, expected_snapshot)
            normalized = normalize_review(raw, validations)
            if enforce_content_snapshot:
                verify_project_content_snapshot(project, expected_snapshot)
        except ProjectChangedError:
            workflow["failure_classification"] = "project_changed_during_review"
            raise
        except StructuredResultError:
            workflow["failure_classification"] = "reviewer_result_invalid"
            raise
        except Exception:
            if not workflow.get("failure_classification"):
                workflow["failure_classification"] = "reviewer_tool_failure"
            raise
        review_history.append((label, normalized))
        review_snapshots[label] = expected_snapshot
        return normalized

    def write_ready_handoff(
        review_payload: dict[str, Any], label: str
    ) -> None:
        nonlocal implementation_promoted
        expected = review_snapshots[label]
        enforce = not run_dir.resolve().is_relative_to(project)
        try:
            if enforce:
                verify_project_content_snapshot(project, expected)
            handoff_snapshot = expected
            if implementation_project is not None:
                def validate_promotion_candidate(
                    candidate: Path,
                ) -> list[dict[str, Any]]:
                    outcomes = run_validations(
                        candidate,
                        load_validation_commands(candidate),
                        run_dir,
                        "promotion-candidate",
                        live=live,
                    )
                    documentation = validate_development_documents(
                        candidate,
                        run_dir,
                        "promotion-candidate",
                        today,
                    )
                    outcomes.append(documentation)
                    all_validations.extend(outcomes)
                    if not validations_passed(outcomes):
                        raise WorkflowError("事务式回写候选验证未通过")
                    return outcomes

                (
                    changed,
                    _candidate_validations,
                    backup_cleanup_warning,
                ) = promote_implementation_workspace(
                    implementation_project,
                    target_project,
                    run_dir,
                    stored_plan,
                    initial_project_snapshot,
                    validate_promotion_candidate,
                    run_date=today,
                )
                implementation_promoted = True
                workflow["implementation_workspace"].update(
                    {
                        "promoted": True,
                        "changed_files": len(changed),
                        "backup_cleanup_pending": bool(backup_cleanup_warning),
                        "backup_cleanup_warning": backup_cleanup_warning,
                    }
                )
                # The target and candidate have different Git HEAD values,
                # but their reviewed staged/unstaged/untracked content must
                # match exactly.  This catches an unrelated concurrent target
                # edit after the per-file preimage checks but before handoff.
                handoff_snapshot = promoted_target_content_snapshot(
                    target_project, expected
                )
                cleanup_implementation()
            write_summary(
                run_dir,
                project=target_project,
                model=model,
                status="ready_for_user_review",
                review=review_payload,
                validations=all_validations,
                handoff_snapshot={"sha256": handoff_snapshot["sha256"]},
                **summary_kwargs(),
            )
            if implementation_promoted or enforce:
                verify_project_content_snapshot(target_project, handoff_snapshot)
        except ProjectChangedError:
            workflow["failure_classification"] = "project_changed_during_handoff"
            raise

    def supervisor_takeover(
        review_payload: dict[str, Any], reason: str
    ) -> int:
        nonlocal latest_review, latest_validations
        latest_review = review_payload
        remaining_cloud_calls = (
            settings.cloud_max_calls_per_run - workflow["cloud_calls"]
        )
        if remaining_cloud_calls < 2:
            workflow["failure_classification"] = (
                "cloud_call_limit_configuration"
            )
            write_failure_summary(
                run_dir,
                project=target_project,
                model=model,
                error=WorkflowError(
                    "云端调用额度不足以同时完成 supervisor 与强制 final review"
                ),
                review=review_payload,
                validations=all_validations,
                **summary_kwargs(),
            )
            print("BLOCKED：云端调用上限未预留 supervisor + final review 容量。")
            return finish(2)
        if reason == "risk_high_direct_cloud":
            takeover_classification = "risk"
        elif reason.startswith("adaptive_") or reason.startswith("第二轮"):
            takeover_classification = "review_findings"
        elif "validation" in reason:
            takeover_classification = "validation_failure"
        else:
            takeover_classification = "watchdog"
        workflow["takeover"] = {
            "occurred": True,
            "classification": takeover_classification,
            "reason": reason,
            "round": review_number,
        }
        print(f"切换 Codex 监督者：{reason}")
        if live:
            emit_progress("WATCHDOG", f"{reason}；Codex 接管修改")
        def prepare_supervisor() -> tuple[str, str]:
            try:
                takeover_capsule = capsule(
                    "supervisor", latest_validations, review_payload
                )
            except Exception:
                if not workflow.get("failure_classification"):
                    workflow["failure_classification"] = "context_capsule_failure"
                raise
            try:
                supervisor_prompt = render_prompt(
                    "supervisor.md",
                    plan=str(stored_plan),
                    today=today,
                    review=takeover_capsule,
                )
            except Exception:
                if not workflow.get("failure_classification"):
                    workflow["failure_classification"] = "supervisor_prompt_failure"
                raise
            return takeover_capsule, supervisor_prompt

        _takeover_capsule, supervisor_prompt = guarded(prepare_supervisor)

        def invoke_supervisor():
            try:
                return tracked_agent(
                    stage="supervisor-takeover",
                    backend="cloud-supervisor",
                    role="supervisor",
                    log_path=run_dir / "supervisor-takeover.log",
                    action=lambda: call_supervisor(
                        settings,
                        project,
                        supervisor_prompt,
                        run_dir,
                        "supervisor-takeover",
                    ),
                )
            except Exception:
                if not workflow.get("failure_classification"):
                    workflow["failure_classification"] = "supervisor_tool_failure"
                raise

        guarded(invoke_supervisor)
        latest_validations = guarded(lambda: validate_stage("supervisor"))
        if not validations_passed(latest_validations):
            failure_class = classify_validation_failure(
                latest_validations, phase="supervisor"
            )
            workflow["failure_classification"] = (
                "environment_validation"
                if failure_class == "environment_preflight"
                else failure_class or "supervisor_validation"
            )
            write_failure_summary(
                run_dir,
                project=target_project,
                model=model,
                error=WorkflowError("Codex 接管后项目验证未通过"),
                review=review_payload,
                validations=all_validations,
                **summary_kwargs(),
            )
            print("BLOCKED：Codex 接管后验证未通过，未消耗最终 Review 调用。")
            return finish(2)
        final_review = guarded(lambda: cloud_review(latest_validations, "final-review"))
        if live:
            emit_progress(
                "REVIEW",
                f"最终确认 verdict={final_review['verdict']}，"
                f"findings={len(final_review['findings'])}",
            )
        if final_review["verdict"] != "pass":
            workflow["failure_classification"] = "final_review_findings"
            write_summary(
                run_dir,
                project=target_project,
                model=model,
                status="needs_manual_attention",
                review=final_review,
                validations=all_validations,
                **summary_kwargs(),
            )
        else:
            guarded(lambda: write_ready_handoff(final_review, "final-review"))
        if final_review["verdict"] == "pass":
            print("PASS：Codex 接管后通过验证与最终评审，等待用户评审。")
            return finish(0)
        print("BLOCKED：Codex 接管后仍有问题，需要当前任务继续处理。")
        return finish(2)

    minimum_cloud_calls = (
        2 if risk["classification"] == "high" or opencode_control_plan else 3
    )
    if settings.cloud_max_calls_per_run < minimum_cloud_calls:
        workflow["failure_classification"] = "cloud_call_limit_configuration"
        preflight.append(
            {
                "stage": "cloud-capacity",
                "status": "failed",
                "classification": "cloud_call_limit_configuration",
                "required_calls": minimum_cloud_calls,
                "configured_calls": settings.cloud_max_calls_per_run,
            }
        )
        write_failure_summary(
            run_dir,
            project=target_project,
            model=model,
            error=WorkflowError(
                "cloud.max_calls_per_run 不足以保留 supervisor + final review"
            ),
            review=None,
            validations=all_validations,
            **summary_kwargs(),
        )
        return finish(2)

    print(f"run_dir={run_dir}")
    print(f"model={model}")
    if live:
        emit_progress("WORKFLOW", f"启动受监督流程；运行记录：{run_dir}")
        emit_progress("LOCAL", f"启动本地编码模型 {model}")
    # All checks occur before a model can mutate the target. Baseline validation
    # uses the production sandbox in a sanitized disposable workspace, so
    # build outputs have the same write permissions as formal validation while
    # the real target remains clean. It skips the development-document gate.
    preflight_stage = "host"
    try:
        host_preflight = preflight_host(settings, project)
        preflight.append(
            host_preflight
            if isinstance(host_preflight, dict)
            else {
                "stage": "host", "status": "passed",
                "classification": None,
            }
        )
        if risk["classification"] != "high" and not opencode_control_plan:
            if local_backend == "opencode":
                preflight_stage = "opencode-capability"
                opencode_probe = ensure_opencode_available(settings)
                preflight.append(opencode_probe)
            preflight_stage = "local-model"
            ensure_model_available(model, settings.ollama_host)
        preflight_stage = "baseline-copy"
        baseline_container, baseline_project = make_disposable_baseline_workspace(
            project, run_dir
        )
        baseline: list[dict[str, Any]] = []
        baseline_error: Exception | None = None
        try:
            preflight_stage = "baseline-validation"
            try:
                baseline = run_validations(
                    baseline_project,
                    commands,
                    run_dir,
                    "preflight",
                    live=live,
                    project_writable=True,
                )
            except ValidationCleanupError as exc:
                baseline = exc.outcomes
                baseline_error = exc
                raise
            except Exception as exc:
                baseline_error = exc
                raise
        finally:
            all_validations.extend(baseline)
            if baseline_error is None:
                preflight_stage = "baseline-cleanup"
            cleanup_baseline_workspace(baseline_container)
        preflight_stage = "project-integrity"
        verify_project_integrity_snapshot(project, initial_project_snapshot)
        latest_validations = baseline
        failure_class = classify_validation_failure(baseline, phase="baseline")
        preflight.append(
            {
                "stage": "baseline",
                "status": "passed" if failure_class is None else "failed",
                "classification": failure_class,
                "validations": baseline,
            }
        )
        if failure_class is not None:
            workflow["failure_classification"] = failure_class
            write_failure_summary(
                run_dir,
                project=target_project,
                model=model,
                error=WorkflowError("干净基线验证未通过，未启动任何模型"),
                review=None,
                validations=all_validations,
                **summary_kwargs(),
            )
            return finish(2)
    except Exception as exc:
        failure_stage = preflight_stage
        if isinstance(exc, BaselineCleanupError):
            workflow["failure_classification"] = "baseline_cleanup_failure"
            failure_stage = "baseline-cleanup"
        elif isinstance(exc, ValidationCleanupError):
            workflow["failure_classification"] = "validation_cleanup_failure"
            failure_stage = "baseline-validation-cleanup"
        elif isinstance(exc, EvidenceRedactionError):
            workflow["failure_classification"] = "evidence_redaction_failure"
        elif isinstance(exc, ProjectChangedError):
            workflow["failure_classification"] = (
                "project_changed_during_preflight"
            )
        elif not workflow.get("failure_classification"):
            workflow["failure_classification"] = "environment_preflight"
        preflight.append(
            {
                "stage": failure_stage,
                "status": "failed",
                "classification": workflow["failure_classification"],
                "error": redact_live_text(str(exc)),
            }
        )
        write_failure_summary(
            run_dir,
            project=project,
            model=model,
            error=exc,
            review=None,
            validations=all_validations,
            **summary_kwargs(),
        )
        return 2

    if risk["classification"] == "high":
        risk_review = {
            "verdict": "fail",
            "summary": "高风险或未声明风险计划由云端监督者直接实现。",
            "findings": [],
            "tests": [],
        }
        return supervisor_takeover(risk_review, "risk_high_direct_cloud")

    # Repository-local OpenCode control files are deliberately absent from a
    # local candidate. If the approved plan needs to change them, send the
    # entire task to the cloud supervisor while it still operates on the real
    # target rather than creating a candidate that cannot complete the scope.
    if opencode_control_plan:
        control_review = {
            "verdict": "fail",
            "summary": "计划包含 OpenCode 项目控制文件，改由云端监督者直接实现。",
            "findings": [],
            "tests": [],
        }
        return supervisor_takeover(
            control_review, "opencode_project_control_direct_cloud"
        )

    if local_backend == "opencode":
        try:
            verify_project_integrity_snapshot(
                target_project, initial_project_snapshot
            )
            implementation_container, implementation_project = (
                make_disposable_baseline_workspace(
                    target_project,
                    run_dir,
                    exclude_opencode_project_controls=True,
                )
            )
            project = implementation_project
            workflow["implementation_workspace"]["used"] = True
            preflight.append(
                {
                    "stage": "implementation-workspace",
                    "status": "passed",
                    "classification": None,
                    "sanitized": True,
                    "target_writable_by_local_agent": False,
                }
            )
        except Exception as exc:
            workflow["failure_classification"] = "implementation_workspace_failure"
            preflight.append(
                {
                    "stage": "implementation-workspace",
                    "status": "failed",
                    "classification": "implementation_workspace_failure",
                    "error": redact_live_text(str(exc)),
                }
            )
            write_failure_summary(
                run_dir,
                project=target_project,
                model=model,
                error=exc,
                review=None,
                validations=all_validations,
                **summary_kwargs(),
            )
            return finish(2)

    def prepare_local_prompt(
        template: str, classification: str, **values: str
    ) -> str:
        try:
            return render_prompt(template, **values)
        except Exception:
            workflow["failure_classification"] = classification
            raise

    coder_prompt = guarded(
        lambda: prepare_local_prompt(
            "coder.md", "coder_prompt_failure", plan=plan, today=today
        )
    )
    try:
        local_coder_result = tracked_agent(
            stage="coder-initial",
            backend="local",
            role="coder",
            log_path=run_dir / "coder-initial.log",
            action=lambda: call_local_coder(
                settings,
                project,
                model,
                coder_prompt,
                run_dir,
                "coder-initial",
                live=live,
                backend=local_backend,
                risk=risk,
                run_date=today,
            ),
        )
        if local_backend == "opencode":
            opencode_session_id = getattr(local_coder_result, "session_id", None)
            if not isinstance(opencode_session_id, str) or not opencode_session_id:
                raise StructuredResultError("OpenCode coder 缺少 Session ID")
            workflow["local_session_id"] = opencode_session_id
    except (ProjectChangedError, EvidenceRedactionError) as exc:
        if isinstance(exc, EvidenceRedactionError):
            workflow["failure_classification"] = "evidence_redaction_failure"
        write_failure_summary(
            run_dir,
            project=target_project,
            model=model,
            error=exc,
            review=None,
            validations=all_validations,
            **summary_kwargs(),
        )
        return finish(2)
    except Exception as exc:
        return supervisor_takeover(
            watchdog_review(run_dir, "coder-initial", exc),
            "本地 AI 启动、运行或响应异常",
        )
    if live:
        emit_progress("LOCAL", "初始编码阶段结束，开始项目验证")
    latest_validations = guarded(lambda: validate_stage("coder-initial"))
    if not validations_passed(latest_validations):
        coder_failure_class = classify_validation_failure(
            latest_validations, phase="coder-initial"
        )
        if coder_failure_class == "environment_preflight":
            workflow["failure_classification"] = "environment_validation"
            write_failure_summary(
                run_dir,
                project=target_project,
                model=model,
                error=WorkflowError("编码后出现确定性验证环境问题"),
                review=None,
                validations=all_validations,
                **summary_kwargs(),
            )
            return finish(2)
        fix_capsule = guarded(
            lambda: capsule("validation-fix", latest_validations)
        )
        validation_fix_prompt = guarded(
            lambda: prepare_local_prompt(
                "fixer.md",
                "validation_fixer_prompt_failure",
                plan=str(stored_plan),
                today=today,
                review=fix_capsule,
            )
        )
        try:
            validation_fix_result = tracked_agent(
                stage="validation-fix",
                backend="local",
                role="fixer",
                log_path=run_dir / "validation-fix.log",
                action=lambda: call_local_coder(
                    settings,
                    project,
                    model,
                    validation_fix_prompt,
                    run_dir,
                    "validation-fix",
                    live=live,
                    backend=local_backend,
                    risk=risk,
                    session_id=opencode_session_id,
                    run_date=today,
                ),
            )
            if local_backend == "opencode" and getattr(
                validation_fix_result, "session_id", None
            ) != opencode_session_id:
                raise StructuredResultError("OpenCode validation fixer Session 不一致")
        except (EvidenceRedactionError, ProjectChangedError) as exc:
            workflow["failure_classification"] = (
                "evidence_redaction_failure"
                if isinstance(exc, EvidenceRedactionError)
                else "project_changed_during_local_fix"
            )
            write_failure_summary(
                run_dir,
                project=target_project,
                model=model,
                error=exc,
                review=None,
                validations=all_validations,
                **summary_kwargs(),
            )
            return finish(2)
        except Exception as exc:
            return supervisor_takeover(
                watchdog_review(run_dir, "validation-fix", exc),
                "本地验证修复阶段异常",
            )
        latest_validations = guarded(lambda: validate_stage("validation-fix"))
        if not validations_passed(latest_validations):
            failure_class = classify_validation_failure(
                latest_validations, phase="validation-fix"
            )
            if failure_class == "environment_preflight":
                workflow["failure_classification"] = "environment_validation"
                write_failure_summary(
                    run_dir,
                    project=target_project,
                    model=model,
                    error=WorkflowError("确定性验证环境问题仍存在"),
                    review=None,
                    validations=all_validations,
                    **summary_kwargs(),
                )
                return finish(2)
            return supervisor_takeover(
                {
                    "verdict": "fail",
                    "summary": "一次本地验证修复后项目验证仍失败。",
                    "findings": [],
                    "tests": [],
                },
                "validation_failed_after_local_fix",
            )

    for round_number in range(1, rounds + 1):
        if live:
            emit_progress("REVIEW", f"开始第 {round_number} 轮独立 Code Review")
        latest_review = guarded(
            lambda: cloud_review(latest_validations, f"review-{round_number}")
        )
        if live:
            emit_progress(
                "REVIEW",
                f"第 {round_number} 轮 verdict={latest_review['verdict']}，"
                f"findings={len(latest_review['findings'])}",
            )
        if latest_review["verdict"] == "pass":
            guarded(
                lambda: write_ready_handoff(
                    latest_review, f"review-{round_number}"
                )
            )
            print("PASS：已通过独立 Code Review，等待用户评审。")
            return finish(0)
        action = (
            "local_fix"
            if settings.strategy == "legacy" and round_number < rounds
            else decide_review_action(
                latest_review,
                severe_threshold=settings.severe_finding_threshold,
            )
        )
        if action == "supervisor_takeover":
            return supervisor_takeover(
                latest_review,
                f"adaptive_{round_number}_review_takeover",
            )
        if (
            action == "local_fix"
            and round_number < rounds
            and settings.cloud_max_calls_per_run - workflow["cloud_calls"] < 3
        ):
            return supervisor_takeover(
                latest_review,
                "cloud_capacity_reserved_for_takeover_and_final_review",
            )
        if action == "local_fix" and round_number < rounds:
            if live:
                emit_progress("LOCAL", "评审未通过，将 findings 返回本地模型修复")
            fix_context = guarded(
                lambda: capsule(
                    f"local-fix-{round_number}",
                    latest_validations,
                    latest_review,
                )
            )
            review_fix_prompt = guarded(
                lambda: prepare_local_prompt(
                    "fixer.md",
                    "review_fixer_prompt_failure",
                    plan=str(stored_plan),
                    today=today,
                    review=fix_context,
                )
            )
            try:
                review_fix_result = tracked_agent(
                    stage=f"local-fix-{round_number}",
                    backend="local",
                    role="fixer",
                    log_path=run_dir / f"local-fix-{round_number}.log",
                    action=lambda: call_local_coder(
                        settings,
                        project,
                        model,
                        review_fix_prompt,
                        run_dir,
                        f"local-fix-{round_number}",
                        live=live,
                        backend=local_backend,
                        risk=risk,
                        session_id=opencode_session_id,
                        run_date=today,
                    ),
                )
                if local_backend == "opencode" and getattr(
                    review_fix_result, "session_id", None
                ) != opencode_session_id:
                    raise StructuredResultError("OpenCode review fixer Session 不一致")
            except (EvidenceRedactionError, ProjectChangedError) as exc:
                workflow["failure_classification"] = (
                    "evidence_redaction_failure"
                    if isinstance(exc, EvidenceRedactionError)
                    else "project_changed_during_local_fix"
                )
                write_failure_summary(
                    run_dir,
                    project=target_project,
                    model=model,
                    error=exc,
                    review=latest_review,
                    validations=all_validations,
                    **summary_kwargs(),
                )
                return finish(2)
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
            if not validations_passed(latest_validations):
                failure_class = classify_validation_failure(
                    latest_validations, phase=f"local-fix-{round_number}"
                )
                if failure_class == "environment_preflight":
                    workflow["failure_classification"] = "environment_validation"
                    write_failure_summary(
                        run_dir,
                        project=target_project,
                        model=model,
                        error=WorkflowError(
                            "局部修复后出现确定性验证环境问题"
                        ),
                        review=latest_review,
                        validations=all_validations,
                        **summary_kwargs(),
                    )
                    return finish(2)
                return supervisor_takeover(
                    latest_review,
                    "local_fix_validation_failed",
                )

    assert latest_review is not None
    return supervisor_takeover(latest_review, "第二轮 Review 仍有阻塞 finding")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="本地 AI coding + Codex review 的受监督 MVP 工作流"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="检查 Codex、Ollama、Git 与本地模型")
    doctor.add_argument("--plan", help="按计划风险决定是否需要本地模型")
    doctor.add_argument("--model", default="primary", help="本地模型别名或 ID")
    doctor.add_argument(
        "--local-backend",
        choices=("opencode", "codex-ollama"),
        help="本地 Agent 后端；默认读取 config/defaults.toml",
    )
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
    run.add_argument(
        "--local-backend",
        choices=("opencode", "codex-ollama"),
        help="本地 Agent 后端；默认使用 OpenCode",
    )
    run.add_argument(
        "--live",
        action="store_true",
        help="实时显示经过脱敏的模型与工作流事件；完整脱敏输出写入日志",
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
