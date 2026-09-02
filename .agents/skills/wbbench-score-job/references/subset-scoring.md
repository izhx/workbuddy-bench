# Subset scoring contracts

All four subsets report scores in `[0, 1]` and use task-balanced aggregation:
attempts are averaged within a task, then task means are averaged. Their
canonical per-trial score artifacts differ.

## Office

- Dataset id: `wb-bench-office-v1.0` (50 tasks in the full release).
- Canonical source: `verifier/score.json`, field priority `reward`, `overall`,
  `test_pass_rate`, then `tests_passed / tests_total` for legacy artifacts.
- The CompositeVerifier may be Rule-only or Rule+Judge. Presence of
  `llm_judge_component_score` means `tests_passed / tests_total` is only a
  component and must not replace or rebase the composite `overall` score.
- Missing Judge artifacts are not anomalous in a Rule-only run.

## Web

- Dataset id: `wb-bench-web-v1.0` (70 tasks in the full release).
- Canonical source: `verifier/score.json` using the same field priority.
- The fixed composite judge may combine rule, LLM, VLM, or agent-judge evidence
  and penalties. Use the final `overall`; do not reconstruct it from individual
  test counts or judge components.
- `pass_rate` means full final score, not browser-test pass percentage.

## Code

- Dataset id: `wb-bench-code-v1.0` (80 tasks in the full release).
- Canonical source: `verifier/score.json` using the same field priority.
- For a pure count score, attempts that report different `tests_total` values
  are rebased to `tests_passed / max(tests_total)` within that task so a shrunk
  test suite cannot inflate a retry. Composite scores are never rebased.
- Failed tests and a valid zero score are model outcomes; a missing or malformed
  canonical score artifact is an evaluation anomaly scored as zero.

## SEC

- Dataset id: `wb-bench-sec-v1.0` (60 tasks in the full release).
- Scoring is task-native and `diff_capture = "none"`; tasks may emit
  `reward.txt`, `reward.json`, or component-only `rewards.json`, including under
  `steps/<step>/verifier/` for multi-step tasks.
- Canonical aggregate source: trial `result.json` field
  `verifier_result.rewards.reward`. The analyzer checks task-native files for
  consistency but does not guess an aggregate from heterogeneous component
  files when the Harbor result is missing.
- Inspect both top-level `exception_info` and every
  `step_results[].exception_info`; top-level job statistics can omit nested
  multi-step failures.
- A nonzero native reward is not sufficient evidence of a valid model response.
  Cross-check API/runtime exceptions, token counts, and missing required outputs
  such as `findings.json` or `report.jsonl`. Flag a positive score on those
  trials as possible default-score contamination.

## Completeness and anomaly rules

`lock.json` is the preferred source of planned tasks because it preserves an
intentional selected-task run. `config.json.n_attempts` defines planned attempts
per task. The full-release counts above are context only and must not override a
smaller explicit lock plan.

The primary score includes discovered trials with missing canonical scores as
zero and includes planned tasks that never ran as zero-valued tasks. If only
some planned attempts exist, the primary task mean follows the discovered
attempts, while `coverage_adjusted_reward` additionally zero-fills missing
attempt slots. Always report the coverage gap alongside either number.

Runtime anomaly categories include API failure, Docker/build failure,
environment startup failure, verifier failure, agent timeout, cancellation,
missing/malformed result, missing/malformed canonical score, score disagreement,
zero-token execution, and SEC required-output missing. Exception excerpts are
truncated; the evidence path identifies the complete source artifact.
