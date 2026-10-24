# Priority 0 Cutover Runbook

This runbook prepares and rehearses a move to durable research evidence. It does not authorize production activation or final-holdout evaluation. The production cutover remains blocked until its separate readiness criteria are accepted.

## 1. Choose explicit paths

Use stable paths for the lifecycle database, artifact archive, immutable baseline snapshots, isolated restore checks, and compatibility TSV. Keep the lifecycle ledger and artifact archive on storage with backups and enough free space. Do not point restore checks at the live database or working strategy directory.

Example values for a controlled deployment:

```bash
LEDGER=logs/research_lifecycle.sqlite3
ARCHIVE=research_artifacts
SNAPSHOTS=backups/research-baselines
RESTORE_CHECK=backups/restore-check
CURRENT_TSV=results-lifecycle.tsv
```

Replace these values with the reviewed deployment paths before running commands. Preserve any existing `results.db`, `results.tsv`, strategies, and market data as-is during the pilot.

## 2. Enter maintenance and confirm quiescence

Pause scheduled jobs and enter the shared maintenance gate:

```bash
python manage_research_maintenance.py enter \
  --lifecycle-ledger "$LEDGER" --lifecycle-artifacts "$ARCHIVE"
python manage_research_maintenance.py status \
  --lifecycle-ledger "$LEDGER" --lifecycle-artifacts "$ARCHIVE"
```

Proceed only when status reports `ready_for_maintenance_work: true`: the maintenance flag is present, lifecycle writer leases are quiescent, supported writer processes have exited, and every supported writer and supervisor passes the compatibility check. If leases remain after their owner has exited, run `recover` and inspect its result. Do not force-kill a writer based only on this tool's report.

## 3. Capture and restore-check the legacy baseline

Capture explicit files and source artifacts. Exclude credentials and private keys; sensitive-looking filenames are skipped or rejected by the capture tool.

```bash
python manage_research_baseline.py capture \
  --lifecycle-ledger "$LEDGER" --lifecycle-artifacts "$ARCHIVE" \
  --legacy-db results.db --legacy-tsv results.tsv \
  --artifact strategies --artifact strategy.py --artifact docs/strategies \
  --snapshot-root "$SNAPSHOTS"
```

Copy the reported snapshot directory into `SNAPSHOT`, then verify and restore it into the isolated restore-check directory:

```bash
SNAPSHOT="$SNAPSHOTS/<reported-baseline-id>"
python manage_research_baseline.py verify \
  --lifecycle-ledger "$LEDGER" --lifecycle-artifacts "$ARCHIVE" \
  --snapshot "$SNAPSHOT" --restore-target "$RESTORE_CHECK"
```

Confirm the result reports `ready: true` and `restored: true`. Compare the manifest and the restore contents before importing. The baseline retains unknown historical provenance; it does not reconstruct missing attempt history. Reserved final-holdout rows remain archived and are excluded from feedback and current-result exports.

## 4. Import and run the fixture rehearsal

Import only after the isolated restore check succeeds:

```bash
python manage_research_baseline.py import \
  --lifecycle-ledger "$LEDGER" --lifecycle-artifacts "$ARCHIVE" \
  --snapshot "$SNAPSHOT" --restore-target "$RESTORE_CHECK/import-check"
```

The import is idempotent for the same baseline identity and content. Then run the fixture-only end-to-end rehearsal, which creates its own temporary database and snapshot:

```bash
python priority_zero_cutover.py rehearse
```

The rehearsal must pass before continuing. It exercises capture, restore, import, evidence integrity, current-view rebuild, fresh TSV export, and rehearsal recording without touching production data.

## 5. Run deployment preflight

After baseline import, run preflight with the verified snapshot and a dedicated compatibility export:

```bash
python priority_zero_cutover.py preflight \
  --lifecycle-ledger "$LEDGER" --lifecycle-artifacts "$ARCHIVE" \
  --snapshot "$SNAPSHOT" --export "$CURRENT_TSV"
```

Resolve every reported blocker. Preflight requires active maintenance, no active lifecycle leases or supported writer processes, compatible entrypoints and supervisors, an imported baseline whose manifest hash matches the restored snapshot, valid evidence invariants, and a fresh current-view TSV. It always reports final-holdout readiness as blocked and production activation as not performed.

## 6. Resume only after the review gate

Keep maintenance active while reviewing the complete preflight output and rehearsal record. Starting research with lifecycle mode is a separate operational decision; pair the ledger and artifact arguments on every run and verify that dashboard and verification consumers point to the same ledger and fresh export.

When the approved pilot window is ready to resume, confirm there are no maintenance tasks or active writers, then exit the gate:

```bash
python manage_research_maintenance.py exit \
  --lifecycle-ledger "$LEDGER" --lifecycle-artifacts "$ARCHIVE"
```

Do not interpret a successful Priority 0 rehearsal as approval to inspect a reserved final holdout, alter evaluation windows, or switch production consumers. Those actions require a later, separately reviewed protocol and release decision.
