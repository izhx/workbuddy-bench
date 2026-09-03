# WB-Bench-SEC Report Workflow

Generate an analysis report from one `WB-Bench-SEC` single Harbor run artifact
directory. Analyze only the evaluation results under the input path. Use
`WB-Bench-SEC` as the display name, with no version suffix.

## Dataset Context

The public dataset id is `wb-bench-sec`; the current runtime dataset id is
`wb-bench-sec-v1.0`. If the user provides a WorkBuddy Bench repository or
dataset directory, read task metadata from a path shaped like
`/path/to/workbuddy-bench/datasets/wb-bench-sec-v1.0`. If no dataset source path
is provided, analyze only the `RUN_DIR` artifacts and metadata resolved by the
task-native analyzer; do not invent a dataset source path in the report.

The current dataset metadata declares 60 tasks across six security domains:

- `agent-security`: 6 tasks.
- `blackbox-testing`: 13 tasks.
- `whitebox-testing`: 14 tasks.
- `vulnerability-exploitation`: 7 tasks.
- `security-operation`: 8 tasks.
- `malware-analysis`: 12 tasks.

The declared difficulty distribution is 15 `easy`, 10 `medium`, 34 `hard`, and
1 `expert`. These full-release counts are context only. Use the run's
`lock.json` as the planned task set, so a deliberate selected-task run is not
misreported as incomplete. When repository documentation and executable
metadata disagree, prefer the current `dataset.toml`, task directories, and
the run's recorded plan.

SEC uses task-native programmatic scoring and `diff_capture = "none"`; it does
not use the WorkBuddy LLM judge or patch-based grading. The canonical per-trial
aggregate is `result.json -> verifier_result.rewards.reward`. Files such as
`verifier/reward.txt`, `reward.json`, `rewards.json`, and their equivalents
under `steps/<step>/verifier/` are consistency and component evidence. Do not
reconstruct an aggregate from heterogeneous component files when the canonical
Harbor reward is absent.

## Input

Required:

- `RUN_DIR`: a single Harbor run directory. Standard layout:
  `<RUN_DIR>/result.json`, `config.json`, `job.log`, and
  `<task-id>__<attempt-id>/` trial subdirectories (see SKILL.md "Input Shape").
- `REPORT_DIR`: directory for the report deliverables (`metrics.json` and
  `report.md`). Resolve from the user request, or default to `<RUN_DIR>/report/`.

If the user does not provide `RUN_DIR`, ask them to provide a valid single
Harbor run artifact directory. Do not guess a default path.

Optional:

- A compressed result artifact. Extract it first, then locate the concrete
  single-run `RUN_DIR`.
- A WorkBuddy Bench repository root or dataset directory, used only to enrich
  the report with `dataset.toml` and task-level `task.toml` metadata.

## Core Workflow

Run the shared steps in SKILL.md "Shared Workflow". For SEC this means using
the repository-local `wbbench-score-job` analyzer with `--stdout` to create
`<REPORT_DIR>/metrics.json`, then writing the richer SEC analysis to
`<REPORT_DIR>/report.md`.

1. Confirm `metrics.json.dataset.subset == "sec"`. Read these fields before
   interpreting any capability result:
   - `run.coverage`, `run.missing_tasks`, `run.unexpected_tasks`, and expected
     versus covered trial slots
   - `score.reward`, `score.pass_rate`, `score.coverage_adjusted_reward`, and
     `score.score_sources`
   - `validity.status`, `validity.unqualified_model_score_usable`, and
     `anomalies.summary`
2. Read task metadata when a dataset root is available:
   - `<DATASET_ROOT>/dataset.toml`
   - `<DATASET_ROOT>/tasks/<task-id>/task.toml`
   - Prefer `metadata.category`, `metadata.subcategory`,
     `metadata.difficulty`, `metadata.cwe`, `metadata.tags`, and declared
     `steps`.
3. For low-score, failed, anomalous, and high-variance tasks, read evidence
   from both the top-level trial and every recorded step:
   - `result.json`, including `exception_info`, `agent_result`, and every
     `step_results[].exception_info` / `step_results[].agent_result`
   - `verifier/reward.txt`, `reward.json`, `rewards.json`,
     `score-details.json`, `ctrf.json`, `test-stdout.txt`, and `verify.log`
   - `steps/<step>/verifier/` equivalents for multi-step tasks
   - `artifacts/manifest.json` and referenced output artifacts
   - `agent/trajectory.json`, `agent/cc-output.txt`, or
     `agent/cbc-output.txt` when present
4. Separate evaluation validity from security capability. API, network,
   Docker/build, environment-startup, verifier, timeout, cancellation, missing
   result, missing score, and missing-attempt failures are not direct evidence
   of weak model capability.
5. For every positive score on a runtime-failed, zero-token, or
   required-output-missing trial, state that the score may be a verifier default
   and cite the exact evidence path. Required outputs commonly include files
   such as `findings.json`, `report.jsonl`, or a task-specific PoC/report.

## SEC Metrics Schema

The SEC `metrics.json` uses the task-native analyzer schema:

- `score.reward`: primary task-balanced score; average attempts within each
  planned task, then average planned tasks.
- `score.pass_rate`: mean per-task fraction of attempts whose final reward is
  at least `1.0`.
- `score.coverage_adjusted_reward`: diagnostic that fills missing planned
  attempt slots with zero; do not relabel it as the primary score.
- `breakdowns.category` and `breakdowns.difficulty`: task-balanced grouped
  metrics.
- `per_task`: per-task reward range and trial evidence, including canonical and
  native artifact rewards, token totals, and exception categories.
- `anomalies`: categorized runtime, completeness, score-consistency, and
  required-output findings with evidence paths.
- `validity.unqualified_model_score_usable`: whether the score can be discussed
  without infrastructure or completeness qualification.

If `validity.unqualified_model_score_usable` is false, present
`score.reward` only as the recorded run result with explicit caveats; do not
present it as a valid measurement of model capability. Apply the same rule when
`positive_score_on_failed_trial` or `positive_score_with_missing_output` is
present, even if the analyzer's base validity flag has not been lowered.

## Report Structure

Use these sections in order, translating headings into the report language when
appropriate:

1. Report title with `WB-Bench-SEC` and localized evaluation-report wording.
2. Data, scoring, and validity check:
   - run path, model, harness, recorded dataset id, planned task source, task
     count, attempts per task, and trial-slot coverage
   - task-native scoring contract and `reward`, `pass_rate`, and
     `coverage_adjusted_reward` semantics
   - `validity.status`, whether the unqualified score is usable, score sources,
     and anomaly counts
3. Executive summary:
   - recorded `reward` and `pass_rate`, plus the validity conclusion before any
     capability interpretation
   - top/bottom tasks and, for repeated attempts, tasks with the largest reward
     range
4. Security-domain and difficulty breakdown:
   - aggregate by the six declared categories and by difficulty
   - use subcategory, CWE, or tags only when supplied by task metadata
5. Evaluation failures and score contamination:
   - group infrastructure/completeness anomalies separately from verifier
     consistency and missing-output anomalies
   - explicitly list positive failed, zero-token, and positive
     required-output-missing trials
6. Security capability analysis:
   - discuss only trials whose evidence supports a model-behavior conclusion
   - bind findings to security domain, task id, reward, expected artifact, and
     verifier evidence
7. Multi-step execution and trajectory:
   - distinguish top-level failures from step-specific failures
   - summarize token/tool activity, produced artifacts, and where the chain
     stopped when those facts are recorded
8. Representative cases:
   - select 3 to 6 tasks covering valid strengths, valid weaknesses, and
     evaluation failures; do not use invalid trials as capability examples
9. Improvement recommendations:
   - separate model/agent improvements from benchmark infrastructure and
     verifier fixes

## Writing Rules

- Match the report language to the user's explicit language request. If no
  language is specified, use the language of the user's request.
- Keep `WB-Bench-SEC`, `wb-bench-sec-v1.0`, metric names, file paths, JSON keys,
  category names, and anomaly names in canonical form.
- Use only metrics and artifact evidence under the input `RUN_DIR` plus a
  dataset root the user supplied or the analyzer resolved. Do not write an
  inferred dataset source path into the report.
- Never report SEC as `WB-Bench-Code`, and do not expect `verifier/score.json`,
  code patches, `diff_capture`, or LLM-judge artifacts as its scoring contract.
- Do not replace the canonical Harbor aggregate with a value reconstructed from
  native component files. Report disagreement as an anomaly.
- Inspect nested `step_results` and `steps/*/verifier/`; top-level statistics can
  omit multi-step failures.
- Do not attribute infrastructure, harness, verifier, or missing-output failures
  to model capability.
- Bind every judgment to task id, category/difficulty, score, anomaly, required
  output, verifier evidence, or trajectory evidence.
