#!/usr/bin/env python3
"""Safely link the repository Skill and supervised launcher into user locations."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE = ROOT / "integrations" / "skills" / "local-ai-mvp-builder"
LAUNCHER_SOURCE = ROOT / "bin" / "mvp-loop-supervised"

PLATFORM_PATHS = {
    "codex": Path(".codex/skills/local-ai-mvp-builder"),
    "claude": Path(".claude/skills/local-ai-mvp-builder"),
    "antigravity-ide": Path(".gemini/config/skills/local-ai-mvp-builder"),
    "antigravity-cli": Path(
        ".gemini/antigravity-cli/skills/local-ai-mvp-builder"
    ),
}
PLATFORM_ALIASES = {
    "gemini-ide": "antigravity-ide",
    "gemini-cli": "antigravity-cli",
}


class InstallError(RuntimeError):
    """A recoverable installation failure for one target."""


@dataclass(frozen=True)
class Target:
    name: str
    path: Path
    source: Path


def path_exists(path: Path) -> bool:
    """Return true for files, directories, and broken symlinks."""
    return path.exists() or path.is_symlink()


def parse_platforms(value: str) -> list[str]:
    requested = [item.strip() for item in value.split(",") if item.strip()]
    if not requested or "all" in requested:
        if requested not in ([], ["all"]):
            raise argparse.ArgumentTypeError("all 不能与其他平台同时使用")
        return list(PLATFORM_PATHS)

    normalized = [PLATFORM_ALIASES.get(item, item) for item in requested]
    unknown = [item for item in normalized if item not in PLATFORM_PATHS]
    if unknown:
        raise argparse.ArgumentTypeError(
            "未知平台：" + ", ".join(unknown)
        )
    if len(set(normalized)) != len(normalized):
        raise argparse.ArgumentTypeError("平台列表包含重复项")
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="安全部署 Local AI MVP Builder 到 Agent Skills 全局目录"
    )
    parser.add_argument(
        "--platforms",
        default=parse_platforms("all"),
        type=parse_platforms,
        metavar="LIST",
        help=(
            "逗号分隔的平台：codex,claude,antigravity-ide,"
            "antigravity-cli（默认：all）"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只报告动作，不写入任何文件"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="备份冲突目标后替换；默认拒绝普通文件或目录冲突",
    )
    return parser


def ensure_safe_parent(home: Path, parent: Path, *, dry_run: bool) -> None:
    """Reject symlinked path components below HOME before creating parents."""
    try:
        relative = parent.relative_to(home)
    except ValueError as exc:
        raise InstallError(f"目标不在 HOME 内：{parent}") from exc

    current = home
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise InstallError(f"拒绝经过软链接父目录写入：{current}")
        if path_exists(current) and not current.is_dir():
            raise InstallError(f"父路径不是目录：{current}")
    if not dry_run:
        parent.mkdir(parents=True, exist_ok=True)


def same_link(path: Path, source: Path) -> bool:
    if not path.is_symlink():
        return False
    try:
        return path.resolve(strict=False) == source.resolve(strict=True)
    except (OSError, RuntimeError):
        return False


def backup_path(target: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    candidate = target.with_name(f"{target.name}.backup.{stamp}")
    counter = 1
    while path_exists(candidate):
        candidate = target.with_name(
            f"{target.name}.backup.{stamp}.{counter}"
        )
        counter += 1
    return candidate


def install_target(
    target: Target, home: Path, *, dry_run: bool, force: bool
) -> str:
    source = target.source.resolve(strict=True)
    ensure_safe_parent(home, target.path.parent, dry_run=dry_run)

    if same_link(target.path, source):
        return f"已是最新：{target.name} -> {source}"

    existing = path_exists(target.path)
    backup: Path | None = None
    if existing and not force:
        raise InstallError(
            f"目标已存在且不是当前共享链接：{target.path}；"
            "请检查内容后显式使用 --force"
        )
    if existing:
        backup = backup_path(target.path)

    if dry_run:
        if backup is not None:
            return (
                f"计划备份：{target.path} -> {backup}\n"
                f"计划安装：{target.name} -> {source}"
            )
        return f"计划安装：{target.name} -> {source}"

    if backup is not None:
        target.path.rename(backup)
        if path_exists(target.path) or not path_exists(backup):
            raise InstallError(f"无法验证备份：{backup}")

    try:
        target.path.symlink_to(source, target_is_directory=source.is_dir())
        if not same_link(target.path, source):
            raise InstallError(f"安装后链接校验失败：{target.path}")
    except Exception:
        if path_exists(target.path):
            if target.path.is_dir() and not target.path.is_symlink():
                shutil.rmtree(target.path)
            else:
                target.path.unlink()
        if backup is not None and path_exists(backup):
            backup.rename(target.path)
        raise

    if backup is not None:
        return (
            f"已备份：{backup}\n"
            f"已安装：{target.name} -> {source}"
        )
    return f"已安装：{target.name} -> {source}"


def targets_for(home: Path, platforms: list[str]) -> list[Target]:
    targets = [
        Target(platform, home / PLATFORM_PATHS[platform], SKILL_SOURCE)
        for platform in platforms
    ]
    targets.append(
        Target("PATH launcher", home / ".local/bin/mvp-loop-supervised", LAUNCHER_SOURCE)
    )
    return targets


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raw_home = os.environ.get("HOME")
    if not raw_home:
        print("错误：HOME 未设置，拒绝推测用户安装目录", file=sys.stderr)
        return 2
    home_input = Path(raw_home).expanduser()
    if not home_input.is_absolute():
        print("错误：HOME 必须是绝对路径", file=sys.stderr)
        return 2
    try:
        home = home_input.resolve(strict=True)
    except OSError as exc:
        print(f"错误：HOME 不可访问：{exc}", file=sys.stderr)
        return 2
    if not home.is_dir():
        print(f"错误：HOME 不是目录：{home}", file=sys.stderr)
        return 2

    missing = [path for path in (SKILL_SOURCE, LAUNCHER_SOURCE) if not path.exists()]
    if missing:
        print("错误：仓库安装源不完整：" + ", ".join(map(str, missing)), file=sys.stderr)
        return 2

    failures: list[str] = []
    completed: list[str] = []
    for target in targets_for(home, args.platforms):
        try:
            completed.append(
                install_target(
                    target, home, dry_run=args.dry_run, force=args.force
                )
            )
        except (InstallError, OSError) as exc:
            failures.append(f"{target.name}: {exc}")

    print("\n".join(completed))
    if failures:
        print("未完成目标：", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        print(
            f"结果：完成 {len(completed)}，失败 {len(failures)}",
            file=sys.stderr,
        )
        return 1
    action = "演练完成" if args.dry_run else "安装完成"
    print(f"结果：{action}，共 {len(completed)} 个目标")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
