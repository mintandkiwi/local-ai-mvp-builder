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
BACKUP_RELATIVE_ROOT = Path(".local/share/local-ai-mvp-builder/backups")

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


def backup_path(target: Target, home: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    label = target.name.lower().replace(" ", "-")
    directory = home / BACKUP_RELATIVE_ROOT / label
    candidate = directory / stamp
    counter = 1
    while path_exists(candidate):
        candidate = directory / f"{stamp}.{counter}"
        counter += 1
    return candidate


def migrate_legacy_backups(
    target: Target, home: Path, *, dry_run: bool
) -> tuple[list[str], list[tuple[Path, Path]]]:
    """Move old sibling backups out of every frontend Skill discovery tree."""
    messages: list[str] = []
    moved: list[tuple[Path, Path]] = []
    legacy_paths = sorted(
        target.path.parent.glob(f"{target.path.name}.backup.*")
    )
    for legacy in legacy_paths:
        destination = backup_path(target, home)
        ensure_safe_parent(home, destination.parent, dry_run=dry_run)
        if dry_run:
            messages.append(f"计划迁移旧备份：{legacy} -> {destination}")
            continue
        try:
            legacy.rename(destination)
            moved.append((legacy, destination))
            if path_exists(legacy) or not path_exists(destination):
                raise InstallError(f"无法验证旧备份迁移：{legacy}")
        except Exception as exc:
            rollback_migrations(moved)
            raise InstallError(f"旧备份迁移失败且已回滚：{legacy}") from exc
        messages.append(f"已迁移旧备份：{legacy} -> {destination}")
    return messages, moved


def rollback_migrations(moved: list[tuple[Path, Path]]) -> None:
    failures: list[str] = []
    for legacy, destination in reversed(moved):
        try:
            if path_exists(destination) and not path_exists(legacy):
                destination.rename(legacy)
            if path_exists(destination) or not path_exists(legacy):
                failures.append(f"{destination} -> {legacy}")
        except OSError:
            failures.append(f"{destination} -> {legacy}")
    if failures:
        raise InstallError("无法回滚旧备份迁移：" + ", ".join(failures))


def preflight_target(target: Target, home: Path, *, force: bool) -> None:
    source = target.source.resolve(strict=True)
    ensure_safe_parent(home, target.path.parent, dry_run=True)
    existing = path_exists(target.path)
    current = same_link(target.path, source)
    if existing and not current and not force:
        raise InstallError(
            f"目标已存在且不是当前共享链接：{target.path}；"
            "请检查内容后显式使用 --force"
        )
    has_legacy = any(
        target.path.parent.glob(f"{target.path.name}.backup.*")
    )
    if has_legacy or (existing and not current):
        ensure_safe_parent(
            home, backup_path(target, home).parent, dry_run=True
        )


def install_target(
    target: Target, home: Path, *, dry_run: bool, force: bool
) -> str:
    source = target.source.resolve(strict=True)
    ensure_safe_parent(home, target.path.parent, dry_run=dry_run)
    current = same_link(target.path, source)
    existing = path_exists(target.path)
    backup: Path | None = None
    if existing and not current and not force:
        raise InstallError(
            f"目标已存在且不是当前共享链接：{target.path}；"
            "请检查内容后显式使用 --force"
        )
    if existing and not current:
        backup = backup_path(target, home)
        ensure_safe_parent(home, backup.parent, dry_run=dry_run)
    messages, moved = migrate_legacy_backups(
        target, home, dry_run=dry_run
    )

    if current:
        messages.append(f"已是最新：{target.name} -> {source}")
        return "\n".join(messages)

    if dry_run:
        if backup is not None:
            messages.append(
                f"计划备份：{target.path} -> {backup}\n"
                f"计划安装：{target.name} -> {source}"
            )
            return "\n".join(messages)
        messages.append(f"计划安装：{target.name} -> {source}")
        return "\n".join(messages)

    try:
        if backup is not None:
            target.path.rename(backup)
            if path_exists(target.path) or not path_exists(backup):
                raise InstallError(f"无法验证备份：{backup}")
        target.path.symlink_to(source, target_is_directory=source.is_dir())
        if not same_link(target.path, source):
            raise InstallError(f"安装后链接校验失败：{target.path}")
    except Exception as exc:
        recovery_failures: list[str] = []
        try:
            if path_exists(target.path):
                if target.path.is_dir() and not target.path.is_symlink():
                    shutil.rmtree(target.path)
                else:
                    target.path.unlink()
        except OSError as recovery_error:
            recovery_failures.append(f"清理失败目标：{recovery_error}")
        try:
            if backup is not None and path_exists(backup):
                backup.rename(target.path)
        except OSError as recovery_error:
            recovery_failures.append(f"恢复原目标：{recovery_error}")
        try:
            rollback_migrations(moved)
        except InstallError as rollback_error:
            recovery_failures.append(str(rollback_error))
        if recovery_failures:
            raise InstallError(
                "安装失败且恢复不完整：" + "; ".join(recovery_failures)
            ) from exc
        raise

    if backup is not None:
        messages.append(
            f"已备份：{backup}\n"
            f"已安装：{target.name} -> {source}"
        )
        return "\n".join(messages)
    messages.append(f"已安装：{target.name} -> {source}")
    return "\n".join(messages)


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

    selected_targets = targets_for(home, args.platforms)
    failures: list[str] = []
    for target in selected_targets:
        try:
            preflight_target(target, home, force=args.force)
        except (InstallError, OSError) as exc:
            failures.append(f"{target.name}: {exc}")
    if failures:
        print("未完成目标：", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        print(f"结果：完成 0，失败 {len(failures)}", file=sys.stderr)
        return 1

    completed: list[str] = []
    for target in selected_targets:
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
