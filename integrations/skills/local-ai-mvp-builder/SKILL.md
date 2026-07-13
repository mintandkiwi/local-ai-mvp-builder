---
name: local-ai-mvp-builder
description: Plan, build, continue, document, or version an MVP through a supervised local-AI workflow shared by Codex, Claude Code, Antigravity IDE, and Antigravity CLI. Use when a user asks to implement a project with local Qwen, Ollama, mvp-loop, or a watchdog-backed coding and review loop.
---

# Local AI MVP Builder

Use the PATH command `mvp-loop-supervised` as the only coding execution path. Treat every frontend Agent as an entry point: Codex CLI with the Ollama provider drives local Qwen coding, while cloud Codex remains the reviewer and takeover supervisor.

## Non-negotiable gates

- Start only after the user selects a project and approves a concrete plan.
- Never pass or imitate `--allow-dirty`; require a clean target Git worktree, including no untracked files.
- Require the target project to contain `.mvp-ai.toml` with at least one real `validation.commands` entry.
- Keep the fixed two-round local review/fix threshold and the Codex takeover/final-review sequence.
- Do not commit, push, change branches, rewrite Git history, publish, or create a pull request without separate user authorization.
- Require every successful run to update the Chinese daily log, AI project outline, and AI task plan described in [references/project-docs.md](references/project-docs.md).
- Report completion only when `summary.json` says `ready_for_user_review`.

## Workflow

### 1. Select and inspect the project

Confirm the absolute target path, objective, repository instructions, stack, existing tests, and security constraints. If the project lacks Git, `.mvp-ai.toml`, or real validation commands, stop and give the user an actionable setup error; do not weaken the gates.

### 2. Write the approved plan

Read [references/plan-format.md](references/plan-format.md) completely. Store plans outside the target worktree under:

```text
${MVP_LOOP_PLAN_DIR:-$HOME/.local/share/local-ai-mvp-builder/plans}
```

Keep the plan acceptance-driven and free of credentials, environment-file contents, and personal machine paths.

### 3. Run the supervised loop

Use summary mode by default:

```sh
mvp-loop-supervised \
  --project "/absolute/path/to/project" \
  --plan "/absolute/path/to/approved-plan.md" \
  --model primary \
  --display summary
```

Use `--model secondary` only when the primary model is unavailable or the user explicitly requests it. Use `--display live` only when detailed, redacted terminal events are useful.

The launcher must run doctor, reject a dirty target, validate the plan and project configuration, and invoke the repository orchestrator with the `run` command. The loop then performs:

1. Local Qwen implements the plan and Chinese documentation.
2. Configured project validations and the documentation contract run.
3. Cloud Codex reviews the full uncommitted diff.
4. The first failed review returns findings to local Qwen for repair.
5. A second failed review hands the diff to cloud Codex for takeover.
6. Local startup errors, crashes, timeouts, or five minutes without progress trigger immediate watchdog takeover.
7. After takeover, validations and an independent final review run again.

Keep conversation updates concise: report start, occasional heartbeat, review verdict, watchdog takeover, and final status. Detailed events stay in the permission-restricted run directory.

### 4. Handle the result

- For `ready_for_user_review`, inspect the final diff and all three required Chinese documents, then report validation evidence, residual risks, branch/base snapshot, and run directory without committing.
- For `needs_manual_attention`, stop the shared workflow and report the recorded blocker and artifact paths. Do not let the current frontend edit the project: Claude Code and Antigravity are entry points, not takeover backends. Ask the user to resume the failed run from Codex for manual remediation, fresh validations, and an independent final review.

### 5. Version or publish only with authorization

Keep the result as an uncommitted diff until the user accepts it. After acceptance, propose a scoped Conventional Commit and SemVer change when applicable. Only explicit publication approval permits a commit, push, tag, release, repository creation, or pull request; never expose private remotes, credentials, run logs, user configuration, or model weights.

## Model policy

- `primary`: `qwen3-coder:30b`
- `secondary`: `qwen3.6:35b`
- Keep only one large Ollama model resident on a 48 GB machine.
