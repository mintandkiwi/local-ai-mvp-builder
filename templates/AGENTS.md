# Repository instructions

## Development

- Read the approved project plan before editing.
- Keep changes scoped to the current task.
- Do not commit, push, change branches, or rewrite Git history.
- Add regression tests for bug fixes and run the configured validation commands.
- Never expose secrets from environment files or credential stores.
- After every development run, update the Chinese daily log at `docs/devlog/YYYY-MM-DD.md` and the AI-readable files `docs/ai/PROJECT_OUTLINE.md` and `docs/ai/TASK_PLAN.md`.

## Code Review

- Treat correctness, security, data loss, regressions, and missing tests as blocking findings.
- Every blocking finding must include a concrete trigger and a required fix.
- Optional style preferences are non-blocking.
