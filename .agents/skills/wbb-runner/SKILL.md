---
name: wbb-runner
description: Launch and repair WorkBuddy Bench jobs with prebuilt task images in either handoff mode, which returns after startup is verified and checks only on request, or managed mode, which waits and uses native in-place resume until every task has three valid attempts within a bounded retry budget. Use for experiment execution and recovery, not ordinary reporting or retrying low scores.
---

# WBB Experiment Runner

Run from the WorkBuddy Bench repository root. This workflow spends model quota
and may mutate Harbor experiments during repair. It always uses prebuilt task
images.

## Establish the contract

Before launching, establish:

- one exact job slug and one explicit task image tag;
- a new run or exact existing experiment directories;
- execution mode: `handoff` or `managed`;
- a maximum total number of new repair attempts;
- any explicit `--retry-exception` or `--keep-exception` overrides.

Default to `handoff` when the user does not specify a mode. Treat requests such
as "run until complete" or "manage it to completion" as `managed`. State the
resolved mode and bounds before the first quota-consuming command.

Handle multiple jobs sequentially. The target is exactly three valid attempts
per task. Resolve effective `n_attempts` before a new launch and stop if it is
not `3`; do not edit job configuration without authorization. For an existing
experiment, verify that `lock.json` plans exactly three slots per task.

A valid attempt has a matching checksum, a non-null reward, and no retryable
Harbor crash exception. Reward `0` is valid when the attempt did not crash.
Never retry merely to obtain a higher score.

If the user asked only to inspect or plan, do not launch or resume.

## Modes and switching

### Handoff mode

Launch in a persistent terminal session, verify the readiness barrier below,
record the handoff, and end the turn. Do not keep polling, call a recurring
monitor, wait for task completion, or begin repair until the user asks again.

### Managed mode

After readiness, continue observing the active command. When the initial run
finishes, identify its exact experiments, preview native repair, and repair
within the approved global attempt budget. Stop at completion, a safety/error
condition, exhausted budget, or user interruption.

### Runtime switching

- `check <instance>` performs one bounded read-only status check and does not
  change mode.
- Switching `handoff` to `managed` attaches to the existing session or recovers
  from its recorded artifacts; never relaunch a still-running experiment.
- Switching `managed` to `handoff` stops agent polling at the next safe output
  boundary and returns. Do not send `INT`, `TERM`, `Ctrl-C`, or stop containers,
  Harbor, or the proxy.
- `continue <instance>` advances a finished phase: analyze it and, if already
  authorized and within budget, start the required repair under the current
  mode.

If more than one active workflow could match, require an exact instance id or
experiment path before checking, switching, or continuing.

## Record resumable control state

Keep one `wbb-runner-state.json` beside the workflow's printed `manifest.json`.
For a workflow beginning from an existing experiment, use the manifest parent
created by its first resume dry-run. Record:

- schema version, mode, and last-observed phase;
- job, tag, instance id, terminal session id and runner PID when available;
- operator log, manifest, launch plan/shard logs, and exact experiment paths;
- total/used repair budget and exception overrides;
- last observation time and last error or stop condition.

Update this file after readiness, each explicit status check, a mode switch,
repair launch, and completion. It is a handoff record, not live truth: always
revalidate process identity, logs, and Harbor artifacts before acting on it.

## Launch with prebuilt images

Read and follow [`../wbb-run-prebuilt/SKILL.md`](../wbb-run-prebuilt/SKILL.md)
for initial dry-run, exact task selection, image preflight, launch authorization,
and operator logging. Never build missing images in this skill.

Start the complete logged run block in a persistent terminal execution session
that can continue after control returns. Capture its session id and do not close
or interrupt it when entering `handoff`.

For a new run, record Harbor job directories present before launch, the printed
instance id, manifest, launch plan/logs, and operator log. Identify newly created
real Harbor experiments by `config.json`, `lock.json`, and direct trial
directories. Do not mistake `.launches/`, `resumed-trials/`, or
`*.attempt-history/` for experiments, or select a directory only because it has
the newest timestamp. Confirm recorded instance identity under concurrency.

Repair multiple shard experiments sequentially, one exact directory per
in-place command. For an existing experiment, skip the initial launch.

Cross-experiment `--resume-job` is not the repair mechanism: it reuses matching-
checksum, non-null-reward trials but does not exclude every reward-bearing crash
handled by in-place resume. Use it only when explicitly requested and its source
trials have already been checked or repaired. With fewer than three reusable
attempts, `sharded_eval` reruns that task with the full job-level attempt count.

## Readiness barrier

Observe startup only until all applicable evidence exists:

- dry-run, prebuilt image, harness, model, and proxy checks passed;
- the runner terminal is still active with no immediate fatal exit;
- instance id, manifest, and operator log paths were captured;
- sharded runs wrote a plan and emitted `started shard`, PID, and log for every
  planned shard; or a direct run created a real Harbor experiment with
  `config.json`, `lock.json`, and initial trial activity.

Do not require a task to finish successfully; that is progress monitoring, not
startup verification. If the command exits before readiness, report failure. If
it completes while readiness is being checked, record the actual completed
state rather than claiming it is running.

In `handoff`, update control state and return immediately after this barrier. In
`managed`, continue with the same session.

## On-demand status and advancement

For `check`, poll the recorded session at most once, verify relevant PIDs when
available, inspect only bounded log tails, and count current Harbor trials and
finished shards. Report `running`, `finished`, `failed`, or `identity-unknown`
with evidence, update the last-observed state, and return without repair.

For `continue`, first perform the same one-shot check. If still running, report
and return unless the user explicitly switched to `managed`. If finished, find
the exact real experiment directories and run the native repair preview below.

## Native in-place repair

Use identical exception-policy flags for preview and execution:

```bash
uv run ./scripts/run.sh \
  --job <job-slug> \
  --task-image-tag <tag> \
  --resume-in-place <exact-experiment-dir> \
  --max-extra-attempts <remaining-budget> \
  --dry-run
```

Review `valid`, `attempts_needed`, `archive reasons`, and every listed trial.
The native plan validates recorded identity, task contents, checksums, prebuilt
images, proxy/model identity, planned slots, results, and budget before moving
anything.

The maintained default retries API, network, agent-startup, Docker/environment,
verifier, cancellation, and missing-result failures, including crashes carrying
a reward. It keeps genuine evaluation outcomes such as agent wall-clock timeout
or output-length failure when they carry a reward.

- Add `--retry-exception <ExactHarborType>` for a confirmed new crash type.
- Add `--keep-exception <ExactHarborType>` to override a default retry type.
- Never use `--no-retry-crashed` without an explicit request for legacy
  missing-reward-only behavior.

Types are exact, case-sensitive `exception_info.exception_type` values.
`--keep-exception` cannot make a null reward valid. If classification requires
message/log regexes, stop and update the core policy instead of moving trials
with an ad-hoc skill script.

After an approved preview, start the same logged command without `--dry-run`.
One invocation already archives invalid trials and loops until all planned slots
are valid or its budget is exhausted; do not add another quarantine layer.

In `handoff`, observe the repair only until its dry-run/preflight passed, the
actual resume session remains active, and Harbor has begun filling the planned
vacancies; then update state and return. In `managed`, wait for it and continue.

Track `extra_attempts_used` against the workflow-wide bound. Before spending
remaining budget after an exhausted invocation, inspect new archive reasons.
Stop when the same unresolved infrastructure cause repeats, identity/checksum/
image/proxy validation fails, budget is exhausted, or the user interrupts.

Direct child directories named exactly `report/` or `report-wbb/` are ignored
by native resume so derived and versioned reports may remain inside the
experiment. Any other unrelated child directory still blocks resume; report
its exact path and ask before moving it. Never relocate or delete unrelated
output automatically.

## Completion and handoff

Finish each experiment with the same dry-run and exception overrides. Claim
completion only when it reports `attempts_needed=0` and `valid=planned`; the
three-slot lock then proves every task in that experiment has three valid
attempts. Do not change image tags, task contents, experiment identity, or
delete attempt history between retries.

Every return must report mode, last-observed phase, job/tag/instance, active
session or PID if known, exact experiment and log/state paths, budget used and
remaining, and the next explicit commands the user can request: `check`,
`continue`, or a mode switch.
