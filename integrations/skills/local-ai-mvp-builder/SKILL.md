---
name: local-ai-mvp-builder
description: Plan, build, continue, document, or version an MVP through a supervised OpenCode-first local-AI workflow shared by Codex, Claude Code, Antigravity IDE, and Antigravity CLI. Use when a user asks to implement a project with OpenCode, a local Ollama-compatible model, mvp-loop, or a watchdog-backed coding and review loop.
---

# Local AI MVP Builder

Use the PATH command `mvp-loop-supervised` as the only coding execution path. Frontend Agents only start the workflow. OpenCode is the primary local coding Agent for low/medium-risk work; `codex-ollama` is an explicit backup, never a silent fallback. Cloud Codex performs independent review and supervised takeover.

Store approved plans outside the target worktree under `${MVP_LOOP_PLAN_DIR:-$HOME/.local/share/local-ai-mvp-builder/plans}`.

## Non-negotiable gates

- Start only after the user selects a project and approves a concrete plan formatted with [references/plan-format.md](references/plan-format.md).
- Never pass or imitate `--allow-dirty`; require a clean target Git worktree, including untracked files.
- Require `.mvp-ai.toml` with at least one real validation command.
- Before the initial clean check, acquire the external project lock shared by every frontend. Require a bounded model-free reviewer version/capability/repository-read probe, record its redacted structured success or failure in `summary.json`, run explicit temporary/cache write probes, and establish a clean baseline in a private disposable workspace that excludes known secret paths and the entire original `.git` tree. Route Git during baseline and formal validation: copy allowed original-HEAD paths into a parentless synthetic HEAD and allowed current-index paths into a synthetic index, so staged/unstaged/untracked and diff-check semantics remain observable without copying history, remotes, hooks, reflogs, auth headers, or secret paths. Enumerate and deny every pre-existing root or nested `.git`, but preserve normal per-working-directory discovery for child repositories created during validation, including `git -C`. Give the copy the same project-write isolation as formal validation, verify its removal, and recheck HEAD, full porcelain state, and validation config before the first model call. Any concurrent change or cleanup failure stops with zero model calls and leaves user files untouched. Baseline/environment failure stops with zero model calls.
- For the OpenCode route, create a second sanitized disposable implementation workspace. Exclude repository-local `opencode.json`/`opencode.jsonc` and `.opencode`, and prohibit the local Agent from editing or promoting those OpenCode control files. If the approved plan needs to change one, route it directly to the cloud supervisor before creating the candidate. Generate a run-private Agent and OpenCode configuration, deny non-edit capabilities by default, disable plugins/MCP/subagents/web access, use a minimal environment that excludes inherited credentials and proxy variables, require only the configured exact loopback Ollama `host:port`, isolate HOME/XDG state, and add a system process sandbox. On supported OpenCode versions, Agent-Markdown granular edit maps are not effective: use the verified scalar edit permission only in the disposable candidate, then enforce scope through the candidate Git-visible-diff gate and transactional promotion. Send the complete approved prompt through a private OpenCode `--file` attachment; argv may contain only a fixed instruction, and timeout/watchdog evidence must not contain the prompt body. The local Agent never writes the real target. After independent review pass, rerun validation in the candidate and reject any unreviewed Git-visible side effect; then promote only plan-declared paths plus the three required Chinese documents. Before transactional promotion completes, any failure rolls back; after approved files are written, backup-cleanup faults are recorded as pending instead of attempting an unsafe partial rollback. Do not imply or perform an automatic Git commit.
- Preserve P0–P2 blocking standards, project validation, the Chinese documentation contract in [references/project-docs.md](references/project-docs.md), and independent final review after takeover.
- Never commit, push, change branches, rewrite Git history, publish, or create a pull request without separate authorization.
- Report completion only when `summary.json.status` is `ready_for_user_review`.

## Risk and adaptive routing

- `low`: local-first.
- `medium`: local-first only with executable baseline tests.
- `high` or undeclared: local model cannot implement critical code; route directly to the cloud supervisor, then validate and independently review.
- Accept risk only from exactly one complete non-fenced declaration and no other visible risk-like control line. Literal placeholders, fenced examples, duplicates, alternatives, conflicts, and a valid declaration mixed with any invalid/placeholder declaration are ambiguous and route conservatively as `high`.
- After local coding, validation failure gets one focused local repair before any cloud review. A deterministic environment failure then stops.
- Review is at most two pre-takeover rounds and every finding has a schema-validated category. Pass ends the loop only after category policy is applied; P0/P1, any-severity `security`, `data_loss`, `reliability`, unknown `other`, or at least five blocking findings trigger immediate takeover. No more than four localized P2 findings categorized as correctness/documentation/performance/testing may receive one local fix and second review. P3-only results normalize to advisory pass only when every category is correctness/documentation/performance/testing.
- Reserve two cloud calls before any supervisor mutation: one for supervisor and one for mandatory independent final review. Reject an underfunded route with `cloud_call_limit_configuration`; if only three calls are available on a low/medium route, prefer takeover after review one over a second review that could strand the final gate.
- Local crash, timeout, or five minutes without progress triggers watchdog takeover. After takeover, validate again; run the independent final review only after validation passes, otherwise stop without spending that review call.
- `workflow.strategy = "legacy"` is rollback-only and must not be described as Token-optimized.

## Run and inspect

```sh
mvp-loop-supervised \
  --project "/absolute/path/to/project" \
  --plan "/absolute/path/to/approved-plan.md" \
  --model primary \
  --local-backend opencode \
  --display summary
```

Use `secondary` only when requested or the primary is unavailable. Use `live` only for redacted diagnostic events. The permission-restricted run directory holds the full plan, redacted persisted logs with valid numeric usage telemetry intact, bounded context capsules, and hashed complete scope/validation/review evidence. The review capsule binds the post-validation staged, unstaged, and untracked content snapshot; verify it before and after review and before ready handoff. Verify every path/SHA-256 pair before use, including each log inside the validation manifest; use `evidence_base_dir` only to resolve relative paths.

Read [references/evidence-contract.md](references/evidence-contract.md) when interpreting Token measurement, takeover reasons, budgets, or efficiency claims. A run without a complete direct-Codex comparison must remain `no_baseline`; never infer cloud savings from local Token counts.

For `needs_manual_attention`, report the recorded failure category and artifacts. Do not claim delivery or let another frontend silently bypass the supervisor/final-review gates. Real A/B trials require separate user approval for cloud Token spend.

## Backend policy

- `opencode`: default primary local Agent backend.
- `codex-ollama`: explicit backup for compatibility or OpenCode diagnosis.
- `primary` and `secondary` are deployment-owned aliases; never document a maintainer's machine, concrete local model, quantization, or resource limits as public defaults.
