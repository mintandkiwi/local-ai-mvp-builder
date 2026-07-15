# Implementation plan format

Create one Markdown plan per approved MVP under `${MVP_LOOP_PLAN_DIR:-$HOME/.local/share/local-ai-mvp-builder/plans}`. Keep it concrete enough that the local coder does not need to invent product decisions.

## Required sections

1. **Objective** — State the user-visible outcome.
2. **Approved scope** — List included capabilities and explicit non-goals.
3. **Repository context** — Record the project root, stack, relevant modules, and repository instructions.
4. **Risk classification** — In non-fenced prose, include exactly one complete declaration using one value, for example `Risk classification: low`, then explain why and list high-risk files/modules. CommonMark prose indentation of zero to three spaces is accepted; a Tab or four-plus spaces is an indented code block and is ignored. Never copy the `low|medium|high` placeholder literally. Missing, fenced-only, duplicated, alternative, or conflicting declarations default to `high` and cannot be lowered by the local model.
5. **Design** — Explain data flow, interfaces, error handling, and security/privacy boundaries.
6. **Implementation tasks** — Order small tasks and name allowed files or components.
7. **Acceptance criteria** — Use observable pass/fail statements.
8. **Validation** — List exact commands from `.mvp-ai.toml` and any focused checks.
9. **Risks and rollback** — Identify compatibility, destructive-action, and recovery concerns.
10. **Documentation** — Require the Chinese daily log, AI project outline, and AI task plan.

Do not include secrets, credentials, tokens, copied environment-file contents, private remote URLs, or user-specific absolute paths. Ask the user only when an unresolved choice would materially change the product.
