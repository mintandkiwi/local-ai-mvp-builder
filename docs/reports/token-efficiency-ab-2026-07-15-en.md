# Local AI MVP Builder Token-Efficiency A/B Interim Report

[中文](token-efficiency-ab-2026-07-15.md) | [English](token-efficiency-ab-2026-07-15-en.md) | [Machine-readable results](../../experiments/token-efficiency/results/2026-07-15-interim.json)

## Validation conclusion

### Overall assessment: ready to share as interim evidence, not sufficient to claim validated savings

As of 2026-07-15, one of the three preregistered pairs produced an operationally complete exploratory observation. The direct-Codex arm of the second task stopped at the cloud fixer stage because of an external usage limit. The other three arms were not started to avoid creating incomparable data.

Both arms of the exploratory observation passed project validation, the Chinese documentation contract, an out-of-repository hidden evaluator, and independent final review. However, local-first used **11.79% more cloud Tokens** and took **42.60% more wall time** than direct-Codex. The Skill working revision changed between the two arms because of validation-driven remediation, violating the fixed-workflow requirement. The eligible pair count is therefore **0/3**. These numbers are diagnostic only: they prove neither a general regression nor savings, and `efficiency_claim` remains `inconclusive`.

## Question

With success rate, P0–P2 standards, project validation, and independent final review held constant, does the current adaptive local-first workflow reduce median cloud Tokens by at least 25% across three low/medium-risk tasks compared with direct-Codex?

## Methodology review

- Three tasks were fixed before execution: `slug-normalizer` (low), `retry-schedule` (medium), and `record-batcher` (medium).
- Each pair used two worktrees cloned from the same base commit.
- Both arms used the same plan, project validation, Chinese documentation contract, hidden evaluator, and independent cloud-review standard.
- The clean baseline contained only passing smoke tests. The hidden evaluator was not included in model prompts.
- Execution order was counterbalanced in the manifest and arms ran serially.
- Local-first used the current Skill and its real adaptive routing. Direct-Codex used a cloud coding agent followed by the same validation and review gate. A failed first review required a fix and another final review in either arm.
- The preregistration required one frozen Skill revision across both arms. In practice, a separate functional probe exposed supervisor capsule scope/evidence defects and triggered source remediation after local-first started but before direct-Codex started. This observation is excluded from acceptance statistics.
- The primary metric is the sum of `total_tokens` for all cloud coding, reviewer, supervisor, and fixer stages. Local Tokens are reported separately and are not converted into cloud savings or financial cost.
- Success required final-review pass, project-validation pass, hidden-evaluator pass, and an unchanged baseline-test hash.
- The preregistered threshold required three complete pairs, equal quality, complete telemetry, and at least 25% lower median cloud Tokens for local-first.

## Operationally complete exploratory observation

| Task | Arm | Success | Cloud Tokens | Local Tokens | Cloud calls | Wall time | First-review blockers |
|---|---|---:|---:|---:|---:|---:|---:|
| slug-normalizer | local-first | Yes | 1,025,809 | 681,342 | 3 | 1,112.14 s | 7 P2 |
| slug-normalizer | direct-Codex | Yes | 917,581 | N/A | 4 | 779.88 s | 2 P2 |

Calculation checks:

- Cloud-Token saving: `1 - 1,025,809 / 917,581 = -11.79%`.
- The negative saving is a regression of 108,228 cloud Tokens.
- Wall-time change: `1,112.14 / 779.88 - 1 = +42.60%`.
- Local-first made one fewer cloud call, but takeover and final-review contexts were larger, so total cloud Tokens were higher.
- Local-first produced seven P2 findings and crossed the takeover threshold. Direct-Codex produced two P2 findings and used a cloud fixer. Both ultimately passed all quality gates.
- The numbers are real observations, but `eligible_for_acceptance_statistics = false` because the Skill working revision drifted.

## Incomplete run

The direct-Codex arm of `retry-schedule` completed its coder and first review:

- Direct coder: 282,610 Tokens, exact.
- Review 1: 269,995 Tokens, exact.
- Known cloud lower bound: 552,605 Tokens.
- Direct fixer: external cloud usage limit, unavailable Token telemetry, exit 1.
- No final review was obtained; status is `needs_manual_attention`, and the run is excluded from paired statistics.

The local-first arm of `retry-schedule` and both `record-batcher` arms were not started. Continuing after the quota interruption would have exposed arms to unequal external conditions.

## Issues found

1. **High: workflow revision drift.** The two arms did not use one frozen Skill working revision, so the eligible pair count is 0/3.
2. **High: insufficient sample.** Only one exploratory observation completed operationally, so the preregistered median cannot be computed.
3. **High: the first local-first sample required takeover.** Supervisor and final-review context rereads offset the intended replacement of cloud coding.
4. **Medium: fixed review cost is large.** Identical high-standard review preserves the task-level quality standard but dominates these small tasks.
5. **Medium: external quota interruption.** Unknown usage is not treated as zero and is excluded from statistics.
6. **Low: wall time is service-sensitive.** It describes this run but is not yet a stable latency estimate.

## Calculation and evidence checks

- Both arms of the complete pair have exact usage telemetry.
- The task base commit, plan, validation, and hidden evaluator were identical, but the Skill working revision was not; this is why the observation is excluded.
- The baseline-test SHA-256 remained unchanged in both arms.
- Both arms passed final validation, documentation, hidden evaluation, and independent review.
- The interrupted stage is `unavailable`; only a lower bound is reported.
- All public integers and formulas can be recomputed from the JSON artifact.

## Suggested improvements

1. Freeze and commit one Skill revision first. After quota recovery, rerun all three pairs from a new experiment root instead of continuing this half-run.
2. Reduce finding-irrelevant repetition in supervisor and final-review capsules without weakening review evidence.
3. Add a deterministic local quality gate before the first cloud review for small tasks, catching basic edge cases before takeover becomes necessary.
4. Scaffold same-day history preservation, stable task IDs, and executable next steps in the Chinese documentation templates.
5. Keep the 25% threshold unchanged and classify the full rerun only as `measured_saving`, `regression`, or `inconclusive` based on complete evidence.

## Required caveats

- This is an interim report, not proof of Token savings.
- The only operationally complete exploratory observation regressed, but revision drift means it cannot prove that the current frozen Skill will regress.
- Three small dependency-free Python tasks do not represent every production workload.
- The report excludes machine specifications, concrete local-model identity, credentials, raw prompts, and private logs.
- Token counts are relative usage under the configured workflows, not financial cost.

## Reproduction

- Harness: [run_ab.py](../../experiments/token-efficiency/run_ab.py)
- Public results: [2026-07-15-interim.json](../../experiments/token-efficiency/results/2026-07-15-interim.json)
- Entry point: `python3 experiments/token-efficiency/run_ab.py --help`

The Skill repository must be a clean commit before a rerun. `prepare` records HEAD in the manifest, and every arm verifies it before and after execution:

```sh
python3 experiments/token-efficiency/run_ab.py prepare --root <external-run-root>
python3 experiments/token-efficiency/run_ab.py run-local --root <external-run-root> --task slug-normalizer
python3 experiments/token-efficiency/run_ab.py run-direct --root <external-run-root> --task slug-normalizer
# Complete the other pairs in manifest order, then:
python3 experiments/token-efficiency/run_ab.py aggregate --root <external-run-root>
```

Complete raw evidence remains in a permission-restricted directory outside the repository.
