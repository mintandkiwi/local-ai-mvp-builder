# Implementation plan format

Create one Markdown plan per approved MVP under `${MVP_LOOP_PLAN_DIR:-$HOME/.local/share/local-ai-mvp-builder/plans}`. Keep it concrete enough that the local coder does not need to invent product decisions.

## Required sections

1. **Objective** — State the user-visible outcome.
2. **Approved scope** — List included capabilities and explicit non-goals.
3. **Repository context** — Record the project root, stack, relevant modules, and repository instructions.
4. **Design** — Explain data flow, interfaces, error handling, and security/privacy boundaries.
5. **Implementation tasks** — Order small tasks and name allowed files or components.
6. **Acceptance criteria** — Use observable pass/fail statements.
7. **Validation** — List exact commands from `.mvp-ai.toml` and any focused checks.
8. **Risks and rollback** — Identify compatibility, destructive-action, and recovery concerns.
9. **Documentation** — Require the Chinese daily log, AI project outline, and AI task plan.

Do not include secrets, credentials, tokens, copied environment-file contents, private remote URLs, or user-specific absolute paths. Ask the user only when an unresolved choice would materially change the product.
