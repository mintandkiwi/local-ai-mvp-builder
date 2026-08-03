#!/usr/bin/env python3
"""Report daily token usage: Claude Code (orchestrator) vs OpenCode/DeepSeek (executor).

Data sources:
- Claude Code session transcripts: ~/.claude/projects/**/*.jsonl
  (each assistant message carries exact per-call usage)
- OpenCode SQLite stores: the host db plus every isolated run runtime under
  ~/.local/share/local-ai-mvp-builder/runs/*/opencode-runtime/data/opencode/
  (step parts carry tokens and real USD cost)

All dates are grouped in UTC+8 (Beijing). Claude usage is reported in tokens
(subscription, no per-call price); DeepSeek usage additionally shows the real
USD cost recorded by OpenCode.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BEIJING = timezone(timedelta(hours=8))
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
OPENCODE_HOST_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
RUNS_ROOT = (
    Path.home() / ".local" / "share" / "local-ai-mvp-builder" / "runs"
)


def beijing_date(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=BEIJING).date().isoformat()


def blank_row() -> dict:
    return {
        "claude_input": 0,
        "claude_output": 0,
        "claude_cache_read": 0,
        "claude_cache_create": 0,
        "ds_input": 0,
        "ds_output": 0,
        "ds_reasoning": 0,
        "ds_cache_read": 0,
        "ds_cost_usd": 0.0,
    }


def scan_claude(days: dict) -> int:
    """Sum Claude Code transcript usage into ``days``; return message count."""
    count = 0
    if not CLAUDE_PROJECTS.is_dir():
        return 0
    for transcript in CLAUDE_PROJECTS.glob("*/*.jsonl"):
        seen_message_ids: set = set()
        try:
            lines = transcript.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            message_id = message.get("id")
            if message_id:
                if message_id in seen_message_ids:
                    continue
                seen_message_ids.add(message_id)
            timestamp = event.get("timestamp")
            if not isinstance(timestamp, str):
                continue
            try:
                moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            day = moment.astimezone(BEIJING).date().isoformat()
            row = days[day]
            row["claude_input"] += usage.get("input_tokens", 0) or 0
            row["claude_output"] += usage.get("output_tokens", 0) or 0
            row["claude_cache_read"] += (
                usage.get("cache_read_input_tokens", 0) or 0
            )
            row["claude_cache_create"] += (
                usage.get("cache_creation_input_tokens", 0) or 0
            )
            count += 1
    return count


def opencode_dbs() -> list[Path]:
    dbs = []
    if OPENCODE_HOST_DB.is_file():
        dbs.append(OPENCODE_HOST_DB)
    if RUNS_ROOT.is_dir():
        dbs.extend(
            sorted(
                RUNS_ROOT.glob(
                    "*/opencode-runtime/data/opencode/opencode.db"
                )
            )
        )
    return dbs


def scan_opencode(days: dict) -> int:
    """Sum OpenCode step-part tokens/cost into ``days``; return part count."""
    count = 0
    for db_path in opencode_dbs():
        tmp = tempfile.mkdtemp(prefix="token-report-")
        try:
            local = Path(tmp) / "opencode.db"
            for suffix in ("", "-wal", "-shm"):
                source = Path(str(db_path) + suffix)
                if source.exists():
                    shutil.copy2(source, Path(str(local) + suffix))
            try:
                connection = sqlite3.connect(local)
            except sqlite3.Error:
                continue
            try:
                rows = connection.execute(
                    "SELECT time_created, data FROM part"
                )
                for time_created, data in rows:
                    if not isinstance(time_created, (int, float)):
                        continue
                    try:
                        part = json.loads(data)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    tokens = part.get("tokens")
                    if not isinstance(tokens, dict):
                        continue
                    day = beijing_date(int(time_created))
                    row = days[day]
                    row["ds_input"] += tokens.get("input", 0) or 0
                    row["ds_output"] += tokens.get("output", 0) or 0
                    row["ds_reasoning"] += tokens.get("reasoning", 0) or 0
                    cache = tokens.get("cache")
                    if isinstance(cache, dict):
                        row["ds_cache_read"] += cache.get("read", 0) or 0
                    cost = part.get("cost")
                    if isinstance(cost, (int, float)):
                        row["ds_cost_usd"] += cost
                    count += 1
            except sqlite3.Error:
                continue
            finally:
                connection.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return count


def render(days: dict, since: str) -> str:
    header = (
        f"{'date':<11} {'claude_in':>10} {'claude_out':>10} "
        f"{'claude_cache':>12} {'ds_in':>8} {'ds_out':>8} "
        f"{'ds_cache':>10} {'ds_cost':>9}"
    )
    lines = [header, "-" * len(header)]
    shown = {d: r for d, r in sorted(days.items()) if d >= since}
    totals = blank_row()
    for day, row in shown.items():
        lines.append(
            f"{day:<11} {row['claude_input']:>10,} {row['claude_output']:>10,} "
            f"{row['claude_cache_read']:>12,} {row['ds_input']:>8,} "
            f"{row['ds_output']:>8,} {row['ds_cache_read']:>10,} "
            f"${row['ds_cost_usd']:>8.4f}"
        )
        for key in totals:
            totals[key] += row[key]
    lines.append("-" * len(header))
    lines.append(
        f"{'total':<11} {totals['claude_input']:>10,} "
        f"{totals['claude_output']:>10,} {totals['claude_cache_read']:>12,} "
        f"{totals['ds_input']:>8,} {totals['ds_output']:>8,} "
        f"{totals['ds_cache_read']:>10,} ${totals['ds_cost_usd']:>8.4f}"
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="统计最近 N 天(北京时间,默认 7)",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    days: dict = defaultdict(blank_row)
    claude_messages = scan_claude(days)
    opencode_parts = scan_opencode(days)

    since = (datetime.now(tz=BEIJING).date() - timedelta(days=args.days - 1))
    since_text = since.isoformat()
    if args.json:
        print(
            json.dumps(
                {d: r for d, r in sorted(days.items()) if d >= since_text},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(render(days, since_text))
        print(
            f"\n(Claude 消息 {claude_messages} 条,OpenCode step "
            f"{opencode_parts} 条;Claude 为订阅计量,DeepSeek 为实际扣费)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
