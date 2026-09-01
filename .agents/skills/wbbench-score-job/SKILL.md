---
name: wbbench-score-job
description: Calculate task-balanced scores and audit runtime anomalies for one WorkBuddy Bench Harbor job or run directory across the Office, Web, Code, and SEC subsets. Use for read-only result analysis, not for launching, resuming, or repairing evaluations.
---

# WB-Bench Job Scoring

Analyze exactly one Harbor run and keep the input artifacts read-only. The input
may be a run directory or a job directory containing exactly one run. If a job
directory contains multiple runs, do not pick the newest one; ask for the exact
run directory.

## Run the deterministic analyzer

From the WorkBuddy Bench repository root:

```bash
uv run python .agents/skills/wbbench-score-job/scripts/analyze_job.py \
  <JOB_OR_RUN_DIR> \
  --language zh
```

Match `--language` to the user's request (`zh` or `en`). The script writes
`score-analysis.json` and `score-report.md`. Each default invocation creates a
new UTC-timestamped version directory:

```text
<RUN_DIR>/report-wbb/<YYYY-MM-DD__HH-MM-SSZ>/
  score-analysis.json
  score-report.md
```

This also applies when the input is the parent job directory. Use
`--output-dir <REPORT_DIR>` only when the user requests another location, or
`--stdout` when no report files should be written. If an explicitly selected
output version already exists, stop or obtain authorization before `--force`.

Native in-place resume ignores the exact direct child `report-wbb/`, including
all timestamped report versions beneath it; it continues to reject unrelated
child directories.

The analyzer uses only Python's standard library. It resolves the dataset from
the recorded run artifacts, obtains the planned task set from `lock.json`, and
uses local `task.toml` metadata when available. Pass `--dataset-root` only when
the dataset is outside the current repository. Do not silently substitute a
different dataset revision.

## Interpret the output

Read both deliverables before reporting:

- Treat `score.reward` as the primary task-balanced score: average attempts
  within each task, then average tasks. `score.pass_rate` is the fraction of
  full-score attempts, not a generic test pass percentage.
- Use `score.coverage_adjusted_reward` only as an incomplete-run diagnostic. It
  zero-fills missing planned attempt slots; do not relabel it as the benchmark's
  primary score.
- Report `validity.status`, planned-versus-observed coverage, missing or extra
  attempts, score sources, and anomaly counts before interpreting capability.
- Do not attribute API, Docker build, environment startup, verifier, missing
  result, or missing score-artifact failures to model capability. If
  `validity.unqualified_model_score_usable` is false, present the numeric score
  only as the recorded run result with its caveat.
- For SEC, explicitly report positive scores on API-failed, zero-token, or
  required-output-missing trials. These can be verifier defaults rather than
  model performance.

Read [references/subset-scoring.md](references/subset-scoring.md) when explaining
why score sources or anomaly checks differ among Office, Web, Code, and SEC.

## Boundaries

- Apart from creating a timestamped version under `<RUN_DIR>/report-wbb/`, do
  not edit, move, quarantine, resume, or rerun Harbor artifacts.
- Do not combine multiple runs, datasets, models, or shards into one score.
- Do not infer a run when multiple candidates exist.
- Keep exact task/trial evidence paths in the report and distinguish observed
  artifacts from interpretation.
