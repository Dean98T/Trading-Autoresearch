# Autoresearch Operations

## Purpose

This document describes the autoresearch guardrails and the gated durable-evidence pilot. The lifecycle ledger is opt-in while Priority 0 is being rehearsed; the existing legacy path remains available until a separate production cutover is approved.

## Current Runtime Flow

### Main loop

- `watchdog.sh` keeps `agent_research.py` alive.
- `agent_research.py` generates strategies, runs validation and backtests, writes results, and saves passing strategies.
- In legacy mode, `results.db` is the primary experiment store and `results.tsv` is a compatibility mirror.
- In lifecycle mode, the append-only lifecycle ledger and content-addressed artifact archive are the durable record. `current_results` and the exported TSV are rebuildable views.
- `test` is historical exposed validation. It is available to research selection and does not represent an untouched final holdout.
- Final-holdout evidence remains reserved. Ordinary attempts and evaluations cannot name it, and reserved legacy rows are retained only in the baseline archive.

### Scheduled concept research

- `auto_concept_research.sh` is the scheduled entrypoint.
- It is protected by a lock so only one self-improvement cycle can run at a time.
- It runs `internet_strategy_discovery.py` first to fetch fresh public ideas.
- It then runs `auto_process_review.py` to summarize recent result patterns.
- It then runs `auto_concept_research.py`, which reads both the web digest and the process review before proposing new combinations.
- It then runs `verification_remediation.py` in the same scheduled job.
- On success it touches `logs/auto_concept_research.last_success`.
- The intended cron entry is:

```cron
0 0,12 * * * /home/trading-llm-auto-research/auto_concept_research.sh
```

## Restart And Resume Behavior

### Why restart is required

If verification finds a broken strategy class or core research code is patched, the running `agent_research.py` process must be restarted so the live loop picks up the new guardrails.

`agent_research.py` also checks whether the self-improvement artifacts are stale. If they are older than 12 hours or missing, it asynchronously triggers `auto_concept_research.sh` so cron failure does not freeze concept discovery.

### How restart is triggered

`watchdog.sh` now restarts the research loop when any of the following happens:

- `agent_research.py` is dead
- the loop is silent for too long
- duplicate research-loop processes are detected
- core watched files changed on disk
- `verification_remediation.py` writes `logs/restart_agent_research.flag`

### How resume works

`agent_research.py` persists state in `logs/agent_research_state.json`.

The next experiment number is resolved from both:

- the persisted state file
- the highest `exp#NNN` found in `results.db`

If the process dies during an experiment, the same experiment slot is retried on restart. If an experiment completed, the next experiment number continues from there. The loop must not restart from experiment `1` unless there is no prior state and no recorded history.

## Verification And Remediation Flow

`verification_remediation.py` is the repo-level repair pass. In legacy mode it:

1. selects recent or explicitly named strategies
2. runs `independent-verification/run_verification.py`
3. optionally runs deep checks
4. purges invalid strategies from `results.db`, `results.tsv`, and `docs/strategies/`
5. reruns failed or stale strategies with the current engine
6. refreshes strategy docs from the repaired DB rows
7. re-audits rerun strategies
8. requests an autoresearch restart if invalid results were detected

This is the mechanism that makes the system operationally self-correcting after new issues are found.

With `--lifecycle-ledger`, remediation requires a fresh `--lifecycle-tsv`, resolves each claim to an exact archived attempt, records invalidation events, and appends a revalidation evaluation. It does not delete attempt history or source artifacts. Keep the lifecycle arguments paired; do not run lifecycle verification against a stale TSV.

## Durable Evidence Pilot

Manual attempts can be recorded with `run_research.py --lifecycle-ledger PATH --lifecycle-artifacts PATH`. Autonomous and systematic entrypoints expose the same paired options. Use the lifecycle ledger as the source for dashboard views and verification only when those arguments are supplied. This pilot does not turn on the final holdout or perform a production cutover.

The operating sequence, including maintenance, baseline restore checks, rehearsal, and explicit blockers, is in [the Priority 0 cutover runbook](priority-zero-cutover-runbook.md).

## Look-Ahead Guardrails

### Static rejection

Both `agent_research.py` and `validator.py` reject obvious causal violations, including:

- `.shift(-N)`
- `np.roll(..., -N)`
- direct future indexing such as `prices.iloc[i+1]`
- manual multi-timeframe indexing patterns such as `i // N` outside the approved MTF helper flow

### Dynamic prefix testing

The runtime look-ahead check in `agent_research.py` compares the entire signal prefix, not only the final bar of a prefix.

This matters for bugs where:

- a historical bar changes only near the tail of a truncated sample
- the final bar still appears stable
- future-data existence changes whether earlier signals are valid

The corrected check now catches those cases.

## Independent Verification Improvements

`independent-verification/run_verification.py` now:

- supports `30m`, `6h`, and `12h`
- reads claimed results from `results.db` first
- imports repo modules correctly for strategy dependencies such as `mtf_data`
- allows canonical local market-data reads needed by verified strategies
- emits artifacts that the remediation pass can use to purge and rerun bad results

## Important Files

- `internet_strategy_discovery.py`
- `agent_research.py`
- `watchdog.sh`
- `auto_concept_research.sh`
- `auto_concept_research.py`
- `auto_process_review.py`
- `verification_remediation.py`
- `validator.py`
- `results_db.py`
- `docs/latest_strategy_discovery.md`
- `logs/agent_research_state.json`
- `logs/restart_agent_research.flag`
- `independent-verification/run_verification.py`

## Operator Notes

- After patching core research logic, do not rely on an already-running `agent_research.py` process. Restart it through the watchdog path so the new logic becomes live.
- In legacy mode, allow the rerun flow to repopulate results after a purge before trusting dashboard summaries. In lifecycle mode, check the invalidation and revalidation records, rebuild the current view, and export a fresh TSV.
- Treat the store selected for the running mode as authoritative: `results.db` for legacy mode, or the lifecycle ledger plus its archived artifacts for the pilot.
