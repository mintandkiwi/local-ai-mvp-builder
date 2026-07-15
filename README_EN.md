# Local AI MVP Builder

[中文](README.md) | [English](README_EN.md)

Local AI MVP Builder provides one repository-managed Agent Skill that connects Codex, Claude Code, Antigravity IDE, and Antigravity CLI to a supervised MVP development loop. All four frontends are workflow entry points: Codex CLI drives local Qwen coding through the Ollama provider, while cloud Codex remains responsible for structured code review, watchdog takeover, and final approval.

The workflow never commits, pushes, switches branches, creates repositories, or publishes releases automatically. Final changes remain in the target repository for human review.

## Architecture

```text
Codex / Claude Code / Antigravity IDE / Antigravity CLI
                         │
            shared local-ai-mvp-builder Skill
                         │ PATH
                mvp-loop-supervised
                         │
 doctor + clean Git + risk/plan/config + baseline gates
                         │
              src/mvp_orchestrator.py run
                 ├─ local Qwen coder
                 ├─ project validations
                 ├─ cloud Codex reviewer
                 └─ cloud Codex takeover supervisor
```

`integrations/skills/local-ai-mvp-builder/` is the single source of truth. The installer creates symlinks from every supported platform to that directory, preventing copied Skills from drifting. `~/.local/bin/mvp-loop-supervised` also points to the repository launcher.

## macOS Requirements

- Python 3.11 or later, including the standard-library `tomllib`
- POSIX shell, Git, and macOS `sandbox-exec`
- [Ollama](https://ollama.com/) with the local `qwen3-coder:30b` model; `qwen3.6:35b` is optional
- Codex CLI authenticated for both the Ollama and OpenAI providers
- `aria2c` for parallel model downloads with integrity checks

Download models and verify dependencies:

```sh
./bin/pull-models
./bin/pull-models --with-secondary
./bin/mvp-loop doctor
```

The downloader uses the local `127.0.0.1:7890` proxy only when it is detected. Do not keep multiple large models resident at the same time on a 48 GB machine.

## Install the PATH Launcher and Agent Skills

Preview all actions first; dry-run does not create files:

```sh
./bin/install-agent-skills --dry-run
```

Install for all supported platforms:

```sh
./bin/install-agent-skills
```

Ensure the user command directory is on `PATH`, for example in `~/.zprofile`:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Open a new terminal and verify the launcher:

```sh
mvp-loop-supervised --help
```

Default platform mappings:

| Platform | Skill destination |
| --- | --- |
| Codex | `~/.codex/skills/local-ai-mvp-builder` |
| Claude Code | `~/.claude/skills/local-ai-mvp-builder` |
| Antigravity IDE | `~/.gemini/config/skills/local-ai-mvp-builder` |
| Antigravity CLI | `~/.gemini/antigravity-cli/skills/local-ai-mvp-builder` |

Install only selected platforms with a comma-separated list:

```sh
./bin/install-agent-skills --platforms codex,claude
./bin/install-agent-skills --platforms antigravity-ide,antigravity-cli
```

The installer rejects conflicts with ordinary files, directories, or symlinks to another source, while continuing to report the status of other targets. Use `--force` only after confirming that replacement is intended:

```sh
./bin/install-agent-skills --platforms codex --force
```

Before any migration, `--force` performs a read-only conflict and parent-path preflight for every target. It then moves the old target to `~/.local/share/local-ai-mvp-builder/backups/<platform>/<timestamp>` and verifies that the backup exists before installing. Legacy `local-ai-mvp-builder.backup.*` directories left under Skill discovery paths are migrated out so stale copies cannot be discovered as duplicate Skills. If a migration or later link operation fails, the installer rolls back moved legacy backups and original targets and reports any recovery failure. It rejects writes through symlinked parents under `HOME` and never reads or copies authentication files.

After installation, confirm that `local-ai-mvp-builder` appears in each frontend's Skill list, or explicitly reference `$local-ai-mvp-builder` in a task. Antigravity versions may differ in how they discover directory symlinks. If the Skill is missing, confirm that the destination `SKILL.md` is readable, restart the frontend, and inspect its Skill list. Do not create a copied second source.

## Initialize a Target Project

The target must be a Git repository with a clean worktree. A compatible entry point can scaffold the minimum files for a new project:

```sh
./bin/mvp-loop init --project "/absolute/path/to/project"
```

Edit `.mvp-ai.toml` in the target project and add at least one real test, lint, type-check, or build command. The initialized files must first become a user-approved clean Git baseline; the tool does not commit them for you.

Store the approved plan outside the target worktree. The default agent-neutral location is:

```text
${MVP_LOOP_PLAN_DIR:-$HOME/.local/share/local-ai-mvp-builder/plans}
```

## Run

Normally, invoke `$local-ai-mvp-builder` from any installed frontend. You can also call the shared launcher directly:

```sh
mvp-loop-supervised \
  --project "/absolute/path/to/project" \
  --plan "$HOME/.local/share/local-ai-mvp-builder/plans/project-plan.md" \
  --model primary \
  --display summary
```

- `--model secondary` selects `qwen3.6:35b`.
- `--display live` shows redacted tool calls, file changes, model-provided reasoning summaries, Token usage, and top-level failure events. It never displays or stores private raw chains of thought. The default `summary` mode keeps only essential status updates.
- There is no dirty-worktree bypass. Tracked, staged, and untracked changes all block startup.
- A missing plan, missing `.mvp-ai.toml`, invalid Git repository, or failed doctor check exits nonzero with a Chinese error message.
- The non-code-block body of the plan must contain exactly one complete declaration such as `Risk classification: low`. CommonMark body text may have zero to three leading spaces; a Tab or at least four spaces is treated as an indented code block and ignored. Literal `low|medium|high` placeholders, duplicate declarations, conflicts, or a valid declaration mixed with an invalid risk-like control line all route conservatively as high risk. The local model cannot lower risk.

Before any model can modify files, the orchestrator acquires an external project lock, verifies the reviewer executable, version, structured-output capabilities, and repository readability, and creates a private disposable copy that excludes known `.env` files, private keys, credential-shaped data files, and authentication files. In the same Seatbelt environment used for formal validation, it probes temporary and tool-cache write access before running the clean baseline with equivalent project-write permissions. The documentation-update gate is not part of this clean baseline. Build artifacts stay in the disposable copy, which is then removed with verified cleanup. Immediately before the first model call, the orchestrator rechecks HEAD, the complete porcelain state, and the `.mvp-ai.toml` snapshot. An unwritable cache, missing SDK or command, concurrent project change, incomplete cleanup, or failed baseline produces `needs_manual_attention` with zero local and cloud model calls, leaving the real workspace and user files unchanged.

Validation commands use a temporary Git wrapper that routes each target correctly. The controller copies allowed paths from the original HEAD into a synthetic parentless commit, then copies allowed paths from the current index into a synthetic index while leaving the real worktree unchanged. This preserves staged, unstaged, untracked, and `git diff --check` semantics while exposing only a private `GIT_DIR` without history, remotes, or authentication data. Every root or nested `.git` that existed before validation is enumerated and denied. Test repositories created during validation, inside either the project or temporary directories, use normal per-working-directory discovery for `init/add/commit/status` and `git -C`. The baseline copy excludes the entire original `.git`, so validation code cannot read existing `FETCH_HEAD`, reflogs, hooks, remotes, `http.extraHeader`, or sensitive historical objects.

The default `adaptive` strategy routes by risk and evidence. Low- and medium-risk tasks start with local Qwen. High, undeclared, or ambiguous risk, including valid declarations mixed with placeholders or invalid declarations, sends critical implementation directly to the Codex supervisor. A post-coding validation failure receives one focused local repair before any reviewer call. After validation passes, every review finding has a schema-enforced `category`. P0/P1 findings, any-severity `security`, `data_loss`, `reliability`, unclassifiable `other`, or at least five blocking findings trigger immediate takeover. At most four localized P2 findings in correctness, documentation, performance, or testing may receive one local repair followed by a second review. P3-only results are non-blocking only when all categories are from that safe set. The orchestrator reserves cloud capacity for both supervisor work and mandatory final review. Insufficient capacity fails as `cloud_call_limit_configuration` before any cloud mutation. With only three calls available on a low/medium route, it prefers takeover after review one instead of risking the final gate on a second review. After takeover it validates again and runs independent final review only when validation succeeds. Local startup failure, crash, timeout, or five minutes without progress also triggers watchdog takeover. `workflow.strategy = "legacy"` exists only for rollback and must not be described as Token-optimized.

Run records live outside the target workspace under `~/.local/share/local-ai-mvp-builder/runs/`, with each directory restricted to mode `0700`. This prevents a workspace-write agent from altering approved plans, logs, or summaries. Persisted command output, validation logs, structured reviews, and last messages reject symlinks, use atomic writes, and are redacted while retaining valid numeric Token telemetry. Even a highly compacted capsule retains hashed references to complete scope, validation-manifest, and review evidence. A fixed-memory content snapshot binds staged, unstaged, and every untracked file after final validation; it is rechecked before and after review and again before ready handoff. Large diffs and files are streamed rather than loaded fully into memory. Any drift blocks reuse of the old verdict. A run can be handed to the user only when `summary.json.status` is `ready_for_user_review`.

### Usage and Efficiency Evidence

`summary.json.usage.stages` records one unique stage entry for every coder, fixer, reviewer, and supervisor invocation. `turn.completed.usage` is `exact`; a legacy `tokens used` total is only `partial`; missing, corrupt, timed-out, or incompatible telemetry is `unavailable`/`null`, never zero. `cloud_lower_bound` is the currently provable cloud Token lower bound. `measurement_complete` is true only when every invoked stage is exact.

Configure `cloud.soft_token_budget` in `config/defaults.toml`; zero means unset. `cloud.max_calls_per_run` limits cloud calls. The soft budget warns but never interrupts a security or data fix midway. Incomplete telemetry makes the budget status `unknown`, not “within budget.” An ordinary run must keep `efficiency.claim` as `no_baseline`. Only a separately approved, equal-task direct-Codex comparison with complete measurements may report `measured_saving`; otherwise the result is `no_baseline`, `inconclusive`, or `regression`. Local Tokens must not be converted into claimed cloud savings.

## Documentation Contract for Every Development Run

Every successful run must update:

- `docs/devlog/YYYY-MM-DD.md`: Chinese goals, progress, changes, usage, validation results, and follow-ups.
- `docs/ai/PROJECT_OUTLINE.md`: architecture, entry points, boundaries, and current state for future AI agents.
- `docs/ai/TASK_PLAN.md`: stable task IDs, status, acceptance criteria, and next actions.

The orchestrator checks that these files exist, contain enough Chinese text, include the required sections, and are actually changed in the current Git diff. Missing any requirement blocks `ready_for_user_review`.

## Upgrade, Uninstall, and Restore

The Skill and launcher are symlinks, so repository updates take effect immediately. Rerun the installer after updating the repository to perform an idempotent check:

```sh
./bin/install-agent-skills
```

Before uninstalling, confirm that each destination is a symlink, then remove the selected platform and PATH entries:

```sh
test -L "$HOME/.codex/skills/local-ai-mvp-builder" && rm "$HOME/.codex/skills/local-ai-mvp-builder"
test -L "$HOME/.claude/skills/local-ai-mvp-builder" && rm "$HOME/.claude/skills/local-ai-mvp-builder"
test -L "$HOME/.gemini/config/skills/local-ai-mvp-builder" && rm "$HOME/.gemini/config/skills/local-ai-mvp-builder"
test -L "$HOME/.gemini/antigravity-cli/skills/local-ai-mvp-builder" && rm "$HOME/.gemini/antigravity-cli/skills/local-ai-mvp-builder"
test -L "$HOME/.local/bin/mvp-loop-supervised" && rm "$HOME/.local/bin/mvp-loop-supervised"
```

If `--force` created a backup, inspect the chosen timestamp under `~/.local/share/local-ai-mvp-builder/backups/<platform>/`. Remove the current link, then move that backup back to the platform's original path. Do not leave backups inside a Skill discovery directory, and never use a wildcard that could overwrite multiple backups.

## Security and Publication Boundaries

- Local coding uses a restricted workspace. Validation commands run under macOS Seatbelt with network, Git metadata writes, and sensitive-file reads denied.
- The Skill and launcher do not grant commit, push, tag, release, or PR permissions.
- Claude Code and Antigravity do not currently act as reviewer or supervisor; those backends remain cloud Codex.
- Never commit local run evidence, model weights, credentials, user Agent configuration, real Tokens, private remote addresses, or personal absolute paths.
- `summary.json.version_control` records only a safe branch, base commit, remote name, and suggested commit message; it never records a remote URL.

See [Hardware and Model Assessment](docs/hardware-and-model-assessment.md), [Implementation Plan](docs/implementation-plan.md), and [Verification Report](docs/verification-report.md) for more background. This project is licensed under the [MIT License](LICENSE).
