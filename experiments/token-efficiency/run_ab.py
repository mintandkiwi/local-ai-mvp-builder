#!/usr/bin/env python3
"""Run paired TE-010 local-first versus direct-Codex experiments.

Private run evidence is stored outside the repository. Public aggregation omits
machine specifications, provider model identifiers, prompts, and raw logs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from src import mvp_orchestrator as m  # noqa: E402


TODAY = "2026-07-15"
VALIDATION_COMMANDS = ["python3 -m unittest discover -s tests -v"]


TASKS: dict[str, dict[str, str]] = {
    "slug-normalizer": {
        "risk": "low",
        "objective": (
            "新增 normalize_slug(value, max_length=48)：使用 Unicode NFKC 规范化，"
            "转小写 ASCII，将连续非字母数字字符折叠成单个连字符，去除首尾连字符，"
            "按 max_length 截断后再次去除尾部连字符；空结果或非法 max_length 抛出 ValueError。"
        ),
        "module": "mvp_sample/slug.py",
        "test_hint": "覆盖 Unicode、重复分隔符、截断、空结果和非法长度。",
        "evaluator": r'''
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1])))
from mvp_sample.slug import normalize_slug
assert normalize_slug("  Hello, World!  ") == "hello-world"
assert normalize_slug("Ｆｕｌｌ　Ｗｉｄｔｈ") == "full-width"
assert normalize_slug("a___b---c") == "a-b-c"
assert normalize_slug("alpha beta gamma", 10) == "alpha-beta"
for value, limit in (("---", 48), ("abc", 0), ("abc", -1)):
    try:
        normalize_slug(value, limit)
    except ValueError:
        pass
    else:
        raise AssertionError((value, limit))
print("hidden evaluator: pass")
''',
    },
    "retry-schedule": {
        "risk": "medium",
        "objective": (
            "新增 build_retry_schedule(attempts, base_delay, multiplier=2.0, max_delay=60.0)，"
            "返回每次重试前的确定性延迟列表；指数增长并封顶，禁止布尔值、非有限数、"
            "attempts<1、base_delay<0、multiplier<1 或 max_delay<0，非法输入抛出 ValueError。"
        ),
        "module": "mvp_sample/retry.py",
        "test_hint": "覆盖指数增长、封顶、零延迟、布尔值、NaN/Infinity 和边界参数。",
        "evaluator": r'''
import math
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1])))
from mvp_sample.retry import build_retry_schedule
assert build_retry_schedule(4, 0.5) == [0.5, 1.0, 2.0, 4.0]
assert build_retry_schedule(5, 2, 3, 10) == [2.0, 6.0, 10.0, 10.0, 10.0]
assert build_retry_schedule(3, 0) == [0.0, 0.0, 0.0]
bad = [
    (True, 1, 2, 10), (0, 1, 2, 10), (2, -1, 2, 10),
    (2, 1, 0.5, 10), (2, 1, 2, -1), (2, math.nan, 2, 10),
    (2, 1, math.inf, 10),
]
for args in bad:
    try:
        build_retry_schedule(*args)
    except ValueError:
        pass
    else:
        raise AssertionError(args)
print("hidden evaluator: pass")
''',
    },
    "record-batcher": {
        "risk": "medium",
        "objective": (
            "新增 batch_records(records, max_items, max_bytes)，以单次迭代方式把 bytes 记录"
            "按条数和总字节双上限分批，保持顺序且不产生空批次；非 bytes 记录抛出 TypeError，"
            "单条超限或非法上限抛出 ValueError。"
        ),
        "module": "mvp_sample/batching.py",
        "test_hint": "覆盖生成器、双上限、顺序、空输入、单条超限、错误类型和非法上限。",
        "evaluator": r'''
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1])))
from mvp_sample.batching import batch_records
seen = []
def source():
    for item in (b"aa", b"bbb", b"c", b"dddd"):
        seen.append(item)
        yield item
assert batch_records(source(), 2, 5) == [[b"aa", b"bbb"], [b"c", b"dddd"]]
assert seen == [b"aa", b"bbb", b"c", b"dddd"]
assert batch_records([], 2, 5) == []
assert batch_records([b"a", b"b", b"c"], 2, 99) == [[b"a", b"b"], [b"c"]]
for records, items, size, error in (
    ([b"toolong"], 2, 3, ValueError), (["x"], 2, 3, TypeError),
    ([b"x"], 0, 3, ValueError), ([b"x"], 2, 0, ValueError),
):
    try:
        batch_records(records, items, size)
    except error:
        pass
    else:
        raise AssertionError((records, items, size))
print("hidden evaluator: pass")
''',
    },
}


def atomic_json(path: Path, value: Any) -> None:
    m.atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def run_checked(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {command}\n{result.stdout}")
    return result


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frozen_workflow_revision() -> str:
    """Return the current commit only when the workflow repository is clean."""
    status = run_checked(
        ["git", "status", "--porcelain=v1", "--untracked-files=normal"], cwd=REPO
    ).stdout
    if status.strip():
        raise RuntimeError(
            "workflow repository must be clean before preparing or running an experiment"
        )
    return run_checked(["git", "rev-parse", "HEAD"], cwd=REPO).stdout.strip()


def verify_frozen_workflow(root: Path) -> None:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    expected = manifest.get("workflow_revision")
    actual = frozen_workflow_revision()
    if not isinstance(expected, str) or actual != expected:
        raise RuntimeError(
            "workflow revision changed after experiment preparation; start a new root"
        )


def base_files() -> dict[str, str]:
    return {
        ".gitignore": "__pycache__/\n*.pyc\n",
        ".mvp-ai.toml": textwrap.dedent(
            """
            [project]
            name = "te010-sample"

            [validation]
            commands = ["python3 -m unittest discover -s tests -v"]
            """
        ).lstrip(),
        "AGENTS.md": textwrap.dedent(
            """
            # Repository instructions

            Implement only the approved plan. Do not modify existing baseline tests.
            Add focused tests for new behavior. Do not commit or access files outside this repository.
            Maintain the three required Chinese development documents.
            """
        ).lstrip(),
        "README.md": textwrap.dedent(
            """
            # TE-010 Sample Project

            A dependency-free Python fixture used for paired coding-workflow experiments.
            Run `python3 -m unittest discover -s tests -v`.
            """
        ).lstrip(),
        "mvp_sample/__init__.py": '"""Small experiment package."""\n',
        "mvp_sample/core.py": textwrap.dedent(
            """
            def identity(value):
                # Return value unchanged for the clean baseline smoke test.
                return value
            """
        ).lstrip(),
        "tests/test_baseline.py": textwrap.dedent(
            """
            import unittest

            from mvp_sample.core import identity


            class BaselineTests(unittest.TestCase):
                def test_identity(self):
                    self.assertEqual(identity("ok"), "ok")


            if __name__ == "__main__":
                unittest.main()
            """
        ).lstrip(),
        f"docs/devlog/{TODAY}.md": textwrap.dedent(
            f"""
            # {TODAY} 开发日志

            ## 今日目标
            建立可重复的小型 Python 实验基线，等待执行已批准功能任务。

            ## 今日进展
            已创建干净仓库、基础模块和可通过的单元测试。

            ## 修改内容
            当前只有实验基线，没有实现待测功能。

            ## 使用方法
            使用 Python unittest 命令运行基础验证。

            ## 验证结果
            基线测试应当通过，正式结果由实验运行记录提供。

            ## 后续事项
            按批准计划实现功能、补充测试并更新本文档。
            """
        ).lstrip(),
        "docs/ai/PROJECT_OUTLINE.md": textwrap.dedent(
            """
            # 项目开发大纲

            ## 项目目标
            为工作流 A/B 测试提供无依赖、无敏感数据的 Python 项目。

            ## 技术栈
            Python 标准库与 unittest。

            ## 架构与关键路径
            mvp_sample 保存实现，tests 保存公开回归测试。

            ## 重要文件
            .mvp-ai.toml 定义验证命令，AGENTS.md 定义修改边界。

            ## 约束
            不修改基础测试，不使用网络、凭据或生产数据。

            ## 当前状态
            干净基线已经建立，待执行批准任务。
            """
        ).lstrip(),
        "docs/ai/TASK_PLAN.md": textwrap.dedent(
            """
            # AI 任务规划

            ## 当前里程碑
            完成一项受控 Python 功能任务。

            ## 已完成
            已建立基础仓库和可执行验证。

            ## 进行中
            等待 coding agent 执行批准计划。

            ## 待办
            实现功能、增加测试、更新中文文档。

            ## 验收标准
            项目验证和独立评审全部通过。

            ## 下一步
            阅读外部计划并保持最小修改范围。
            """
        ).lstrip(),
    }


def plan_text(task_id: str, task: dict[str, str]) -> str:
    return textwrap.dedent(
        f"""
        # TE-010 paired task: {task_id}

        ## Objective
        {task['objective']}

        ## Approved scope
        只新增 `{task['module']}`、对应公开单元测试并更新三份中文开发文档。不得修改现有 `tests/test_baseline.py`，不得增加第三方依赖、网络访问、持久化或发布行为。

        ## Repository context
        当前实验 worktree 是 Python 标准库项目。实现位于 `mvp_sample/`，测试位于 `tests/`，验证由 `.mvp-ai.toml` 定义。遵守 `AGENTS.md`。

        ## Risk classification
        Risk classification: {task['risk']}

        任务只涉及纯函数和隔离测试数据；没有凭据、生产数据、数据库迁移、并发共享状态或外部副作用。高风险文件：无。

        ## Design
        使用确定性、无副作用的标准库实现。参数校验必须在产生结果前完成，错误类型按目标描述固定。不要读取环境变量或仓库外文件。

        ## Implementation tasks
        1. 新增 `{task['module']}` 并实现目标函数。
        2. 新增聚焦单元测试；不得删除或弱化现有测试。{task['test_hint']}
        3. 更新本日中文开发日志、AI 项目大纲和 AI 任务规划。

        ## Acceptance criteria
        - 目标描述中的正常、边界和错误路径行为全部成立。
        - `tests/test_baseline.py` 内容与基线完全一致。
        - 项目验证、仓库外隐藏 evaluator 和独立终审全部通过。
        - 没有 P0、P1 或 P2 finding。

        ## Validation
        逐字运行：`python3 -m unittest discover -s tests -v`。

        ## Risks and rollback
        风险是边界条件遗漏或测试断言不足。回滚方式是删除新增模块和测试；不得改写 Git 历史。

        ## Documentation
        同一次修改必须更新 `docs/devlog/{TODAY}.md`、`docs/ai/PROJECT_OUTLINE.md` 和 `docs/ai/TASK_PLAN.md`，内容使用中文并与实际验证一致。
        """
    ).lstrip()


def prepare(root: Path) -> None:
    workflow_revision = frozen_workflow_revision()
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        raise RuntimeError(f"experiment root is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / "plans").mkdir(mode=0o700)
    (root / "evaluators").mkdir(mode=0o700)
    (root / "projects").mkdir(mode=0o700)
    (root / "evidence").mkdir(mode=0o700)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "created_date": TODAY,
        "workflow_revision": workflow_revision,
        "tasks": {},
        "order": [
            ["slug-normalizer", "local-first"],
            ["slug-normalizer", "direct-codex"],
            ["retry-schedule", "direct-codex"],
            ["retry-schedule", "local-first"],
            ["record-batcher", "local-first"],
            ["record-batcher", "direct-codex"],
        ],
        "public_privacy": "No machine specification or concrete local model identifier",
    }
    for task_id, task in TASKS.items():
        base = root / "projects" / f"{task_id}-base"
        base.mkdir()
        for relative, content in base_files().items():
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        run_checked(["git", "init", "-b", "main"], cwd=base)
        run_checked(["git", "config", "user.name", "TE-010 Fixture"], cwd=base)
        run_checked(["git", "config", "user.email", "fixture@example.invalid"], cwd=base)
        run_checked(["git", "add", "--all"], cwd=base)
        run_checked(["git", "commit", "-m", "test: establish clean experiment baseline"], cwd=base)
        baseline = run_checked(["git", "rev-parse", "HEAD"], cwd=base).stdout.strip()
        plan = root / "plans" / f"{task_id}.md"
        plan.write_text(plan_text(task_id, task), encoding="utf-8")
        evaluator = root / "evaluators" / f"{task_id}.py"
        evaluator.write_text(textwrap.dedent(task["evaluator"]).lstrip(), encoding="utf-8")
        for arm in ("local-first", "direct-codex"):
            destination = root / "projects" / f"{task_id}-{arm}"
            run_checked(["git", "clone", "--quiet", str(base), str(destination)], cwd=root)
        manifest["tasks"][task_id] = {
            "risk": task["risk"],
            "base_commit": baseline,
            "plan_sha256": sha256(plan),
            "evaluator_sha256": sha256(evaluator),
            "baseline_test_sha256": sha256(base / "tests/test_baseline.py"),
        }
    atomic_json(root / "manifest.json", manifest)
    print(root)


def latest_summary(state_root: Path) -> Path:
    candidates = sorted((state_root / "runs").glob("*/summary.json"))
    if not candidates:
        raise RuntimeError(f"no summary under {state_root}")
    return candidates[-1]


def run_hidden_evaluator(root: Path, task_id: str, project: Path, log_path: Path) -> int:
    result = subprocess.run(
        [sys.executable, str(root / "evaluators" / f"{task_id}.py"), str(project)],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=120,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    m.atomic_write_text(log_path, m.redact_persistent_output(result.stdout))
    return result.returncode


def local_result(root: Path, task_id: str) -> None:
    verify_frozen_workflow(root)
    project = root / "projects" / f"{task_id}-local-first"
    plan = root / "plans" / f"{task_id}.md"
    state = root / "evidence" / task_id / "local-first-state"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    runner_log = root / "evidence" / task_id / "local-first-runner.log"
    started = time.monotonic()
    result = subprocess.run(
        [
            str(REPO / "bin/mvp-loop-supervised"),
            "--project", str(project),
            "--plan", str(plan),
            "--model", "primary",
            "--display", "summary",
        ],
        cwd=REPO,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=10800,
        env={**os.environ, "LOCAL_AI_MVP_STATE_DIR": str(state)},
    )
    wall = round(time.monotonic() - started, 2)
    m.atomic_write_text(runner_log, m.redact_persistent_output(result.stdout))
    verify_frozen_workflow(root)
    summary_path = latest_summary(state)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    evaluator_log = root / "evidence" / task_id / "local-first-hidden-evaluator.log"
    evaluator_exit = run_hidden_evaluator(root, task_id, project, evaluator_log)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    baseline_unchanged = (
        sha256(project / "tests/test_baseline.py")
        == manifest["tasks"][task_id]["baseline_test_sha256"]
    )
    payload = {
        "arm": "local-first",
        "task": task_id,
        "runner_exit_code": result.returncode,
        "wall_seconds": wall,
        "summary_path": str(summary_path),
        "status": summary.get("status"),
        "review_verdict": (summary.get("review") or {}).get("verdict"),
        "cloud_total": (summary.get("usage") or {}).get("cloud_lower_bound"),
        "local_total": (summary.get("usage") or {}).get("local_total"),
        "measurement_complete": (summary.get("usage") or {}).get("measurement_complete"),
        "cloud_calls": (summary.get("workflow") or {}).get("cloud_calls"),
        "takeover": (summary.get("workflow") or {}).get("takeover"),
        "finding_counts": (summary.get("workflow") or {}).get("finding_counts"),
        "hidden_evaluator_exit_code": evaluator_exit,
        "baseline_test_unchanged": baseline_unchanged,
        "success": (
            result.returncode == 0
            and summary.get("status") == "ready_for_user_review"
            and (summary.get("review") or {}).get("verdict") == "pass"
            and evaluator_exit == 0
            and baseline_unchanged
        ),
    }
    atomic_json(root / "evidence" / task_id / "local-first-result.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def direct_prompt(plan: str) -> str:
    return textwrap.dedent(
        f"""
        你是 TE-010 对照组的直接 Codex coding agent。严格实现以下已批准计划。

        规则：
        1. 先读 AGENTS.md、README、配置和现有测试；只修改计划需要的文件。
        2. 不提交、推送、切换分支或访问仓库外文件。
        3. 不修改现有 tests/test_baseline.py；新增能证明行为的测试。
        4. 运行项目验证并如实记录结果，不降低断言或删除测试。
        5. 同步更新三份规定的中文开发文档。

        项目计划：

        {plan}
        """
    ).lstrip()


def usage_from_log(path: Path, *, stage: str, role: str) -> dict[str, Any]:
    return m.usage_stage(
        stage=stage,
        backend="cloud-review" if role == "reviewer" else "cloud-supervisor",
        role=role,
        output=path.read_text(encoding="utf-8", errors="replace"),
        elapsed_seconds=0.0,
        exit_code=0,
    )


def direct_result(root: Path, task_id: str) -> None:
    verify_frozen_workflow(root)
    project = root / "projects" / f"{task_id}-direct-codex"
    plan = root / "plans" / f"{task_id}.md"
    task_run = root / "evidence" / task_id / "direct-codex"
    task_run.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings = m.load_settings()
    risk = m.parse_risk_classification(plan.read_text(encoding="utf-8"))
    usage: list[dict[str, Any]] = []
    validations: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    started = time.monotonic()

    baseline = m.run_validations(
        project, VALIDATION_COMMANDS, task_run, "baseline", project_writable=False
    )
    validations.extend(baseline)
    if not m.validations_passed(baseline):
        raise RuntimeError(f"direct baseline failed: {task_id}")

    m.call_supervisor(
        settings,
        project,
        direct_prompt(plan.read_text(encoding="utf-8")),
        task_run,
        stage="direct-coder",
    )
    usage.append(usage_from_log(task_run / "direct-coder.log", stage="direct-coder", role="coder"))

    post = m.run_validations(
        project, VALIDATION_COMMANDS, task_run, "direct-1", project_writable=True
    )
    post.append(m.validate_development_documents(project, task_run, "direct-1", TODAY))
    validations.extend(post)

    snapshot = m.project_content_snapshot(project)
    capsule, _ = m.write_context_capsule(
        project,
        task_run,
        stage="direct-review-1",
        plan_path=plan,
        risk=risk,
        validations=post,
        project_snapshot=snapshot,
    )
    m.verify_context_capsule(capsule)
    prompt = m.render_prompt(
        "reviewer.md", PLAN=str(plan), TODAY=TODAY, VALIDATION=capsule.read_text(encoding="utf-8")
    )
    before = m.project_content_snapshot(project)
    review = m.call_reviewer(settings, project, prompt, task_run, 1)
    after = m.project_content_snapshot(project)
    if before != after:
        raise RuntimeError("project changed during direct review")
    review = m.normalize_review(review, post)
    reviews.append(review)
    usage.append(usage_from_log(task_run / "review-1.log", stage="direct-review-1", role="reviewer"))

    if review.get("verdict") != "pass":
        fix_capsule, _ = m.write_context_capsule(
            project,
            task_run,
            stage="direct-fix",
            plan_path=plan,
            risk=risk,
            validations=post,
            review=review,
            project_snapshot=m.project_content_snapshot(project),
        )
        m.verify_context_capsule(fix_capsule)
        fix_prompt = textwrap.dedent(
            f"""
            你是 TE-010 对照组的直接 Codex 修复 agent。根据只读评审 finding 修复当前实现，
            保持计划范围，不修改基础测试，不提交或推送，并更新中文开发文档。

            计划路径：{plan}
            证据 capsule：{fix_capsule}
            """
        ).lstrip()
        m.call_supervisor(settings, project, fix_prompt, task_run, stage="direct-fixer")
        usage.append(usage_from_log(task_run / "direct-fixer.log", stage="direct-fixer", role="fixer"))
        post = m.run_validations(
            project, VALIDATION_COMMANDS, task_run, "direct-2", project_writable=True
        )
        post.append(m.validate_development_documents(project, task_run, "direct-2", TODAY))
        validations.extend(post)
        final_capsule, _ = m.write_context_capsule(
            project,
            task_run,
            stage="direct-review-2",
            plan_path=plan,
            risk=risk,
            validations=post,
            review=review,
            project_snapshot=m.project_content_snapshot(project),
        )
        m.verify_context_capsule(final_capsule)
        final_prompt = m.render_prompt(
            "reviewer.md",
            PLAN=str(plan),
            TODAY=TODAY,
            VALIDATION=final_capsule.read_text(encoding="utf-8"),
        )
        before = m.project_content_snapshot(project)
        review = m.call_reviewer(settings, project, final_prompt, task_run, 2)
        after = m.project_content_snapshot(project)
        if before != after:
            raise RuntimeError("project changed during direct final review")
        review = m.normalize_review(review, post)
        reviews.append(review)
        usage.append(usage_from_log(task_run / "review-2.log", stage="direct-review-2", role="reviewer"))

    verify_frozen_workflow(root)
    evaluator_log = root / "evidence" / task_id / "direct-codex-hidden-evaluator.log"
    evaluator_exit = run_hidden_evaluator(root, task_id, project, evaluator_log)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    baseline_unchanged = (
        sha256(project / "tests/test_baseline.py")
        == manifest["tasks"][task_id]["baseline_test_sha256"]
    )
    totals = [item.get("total_tokens") for item in usage]
    exact = all(item.get("measurement") == "exact" and isinstance(item.get("total_tokens"), int) for item in usage)
    cloud_total = sum(totals) if exact else None
    success = (
        review.get("verdict") == "pass"
        and m.validations_passed(post)
        and evaluator_exit == 0
        and baseline_unchanged
    )
    payload = {
        "arm": "direct-codex",
        "task": task_id,
        "wall_seconds": round(time.monotonic() - started, 2),
        "status": "ready_for_user_review" if success else "needs_manual_attention",
        "review_verdict": review.get("verdict"),
        "cloud_total": cloud_total,
        "local_total": None,
        "measurement_complete": exact,
        "cloud_calls": len(usage),
        "usage_stages": usage,
        "reviews": reviews,
        "hidden_evaluator_exit_code": evaluator_exit,
        "baseline_test_unchanged": baseline_unchanged,
        "success": success,
    }
    atomic_json(task_run / "direct-summary.json", payload)
    atomic_json(root / "evidence" / task_id / "direct-codex-result.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def aggregate(root: Path) -> None:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for task_id in TASKS:
        for arm in ("local-first", "direct-codex"):
            path = root / "evidence" / task_id / f"{arm}-result.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows.append(
                {
                    "task": task_id,
                    "risk": manifest["tasks"][task_id]["risk"],
                    "arm": arm,
                    "status": payload["status"],
                    "success": payload["success"],
                    "cloud_tokens": payload["cloud_total"],
                    "local_tokens": payload["local_total"],
                    "cloud_calls": payload["cloud_calls"],
                    "wall_seconds": payload["wall_seconds"],
                    "measurement_complete": payload["measurement_complete"],
                    "hidden_evaluator_passed": payload["hidden_evaluator_exit_code"] == 0,
                    "baseline_test_unchanged": payload["baseline_test_unchanged"],
                }
            )
    local = [row for row in rows if row["arm"] == "local-first"]
    direct = [row for row in rows if row["arm"] == "direct-codex"]
    complete = all(row["measurement_complete"] and isinstance(row["cloud_tokens"], int) for row in rows)
    quality_equal = all(row["success"] for row in rows)
    local_median = statistics.median(row["cloud_tokens"] for row in local) if complete else None
    direct_median = statistics.median(row["cloud_tokens"] for row in direct) if complete else None
    savings = (
        (1 - local_median / direct_median) * 100
        if complete and direct_median and local_median is not None
        else None
    )
    if complete and quality_equal and savings is not None and savings >= 25:
        claim = "measured_saving"
    elif complete and quality_equal and savings is not None and savings < 0:
        claim = "regression"
    else:
        claim = "inconclusive"
    result = {
        "schema_version": 1,
        "as_of": TODAY,
        "method": {
            "paired_tasks": len(TASKS),
            "counterbalanced_order": manifest["order"],
            "same_base_within_pair": True,
            "same_plan_validation_and_review_standard": True,
            "public_model_identity": "deployment aliases only",
        },
        "rows": rows,
        "summary": {
            "local_first_successes": sum(row["success"] for row in local),
            "direct_codex_successes": sum(row["success"] for row in direct),
            "local_first_cloud_token_median": local_median,
            "direct_codex_cloud_token_median": direct_median,
            "cloud_token_median_saving_percent": round(savings, 2) if savings is not None else None,
            "measurement_complete": complete,
            "quality_equal": quality_equal,
            "acceptance_threshold_percent": 25,
            "efficiency_claim": claim,
        },
        "limitations": [
            "Three small dependency-free Python tasks do not represent every production workload.",
            "Wall time depends on transient local and cloud service conditions.",
            "Token comparison measures configured workflow backends, not financial cost.",
        ],
    }
    atomic_json(root / "public-results.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "run-local", "run-direct", "aggregate"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--task", choices=tuple(TASKS))
    args = parser.parse_args()
    if args.command in {"run-local", "run-direct"} and not args.task:
        parser.error("--task is required for run-local/run-direct")
    return args


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    if args.command == "prepare":
        prepare(root)
    elif args.command == "run-local":
        local_result(root, args.task)
    elif args.command == "run-direct":
        direct_result(root, args.task)
    else:
        aggregate(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
