"""Preflight and fixture-only rehearsal for the gated Priority 0 cutover."""

from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Callable

from research_baseline import capture_legacy_baseline, import_legacy_baseline, verify_baseline
from research_lifecycle import ResearchLifecycle, StaleCurrentViewError
from research_maintenance import (
    MAINTENANCE_FLAG,
    active_research_processes,
    supported_writer_compatibility,
    write_maintenance_flag,
)


def run_preflight(
    *, lifecycle: ResearchLifecycle, snapshot_dir: Path, export_path: Path,
    maintenance_flag: Path = MAINTENANCE_FLAG,
    process_checker: Callable[[], list] = active_research_processes,
    compatibility_checker: Callable[[], dict[str, bool]] = supported_writer_compatibility,
) -> dict:
    """Check the preserved baseline, supported writers, evidence, views, and export."""
    blockers = []
    maintenance = lifecycle.maintenance_status()
    if maintenance["state"] != "active":
        blockers.append("maintenance is not active")
    if not maintenance_flag.is_file():
        blockers.append("shared maintenance flag is missing")
    if maintenance["active_writers"]:
        blockers.append(f"{len(maintenance['active_writers'])} lifecycle writer session(s) remain active")

    processes = process_checker()
    if processes:
        blockers.append(f"{len(processes)} supported writer process(es) remain active")
    compatibility = compatibility_checker()
    incompatible = sorted(name for name, compatible in compatibility.items() if not compatible)
    if incompatible:
        blockers.append("unsupported writer/supervisor versions: " + ", ".join(incompatible))

    try:
        baseline = verify_baseline(snapshot_dir)
        imported = next(
            (item for item in lifecycle.legacy_baselines() if item["baseline_id"] == baseline["baseline_id"]),
            None,
        )
        if imported is None:
            blockers.append("verified baseline has not been imported into the lifecycle ledger")
        elif imported["manifest_hash"] != baseline["manifest_hash"]:
            blockers.append("imported baseline manifest differs from the verified snapshot")
    except (OSError, ValueError, sqlite3.Error) as exc:
        baseline = {"ready": False, "error": str(exc)}
        blockers.append(f"legacy baseline failed restore verification: {exc}")

    evidence = lifecycle.verify_evidence_invariants()
    if not evidence["ready"]:
        blockers.append("evidence invariant check failed")

    try:
        lifecycle.rebuild_current_results()
        lifecycle.export_current_tsv(export_path)
        lifecycle.require_fresh_export(export_path)
        export = lifecycle.export_freshness(export_path)
    except (OSError, StaleCurrentViewError) as exc:
        export = {"fresh": False, "error": str(exc)}
        blockers.append(f"current-result view/export is not ready: {exc}")

    return {
        "ready": not blockers,
        "blockers": blockers,
        "baseline_id": baseline.get("baseline_id"),
        "baseline": baseline,
        "evidence": evidence,
        "export": export,
        "maintenance": maintenance,
        "active_research_processes": processes,
        "supported_writer_compatibility": compatibility,
        "final_holdout_readiness": {
            "ready": False,
            "status": "blocked_by_later_work",
            "requirements": [
                "accepted backtest accounting and walk-forward evaluation",
                "verified data and model provenance",
                "frozen protocol, candidate batch, window, and success criteria",
                "demonstrated evaluator and filesystem access isolation",
                "operator-controlled single release at the predetermined endpoint",
            ],
        },
        "production_activation": "not performed by Priority 0 preflight",
    }


def rehearse_fixture(tmp_root: Path | None = None) -> dict:
    """Exercise snapshot, restoration, import, rebuild, export, and recovery on fixture files."""
    with tempfile.TemporaryDirectory(prefix="priority-zero-rehearsal-", dir=tmp_root) as scratch:
        root = Path(scratch)
        legacy_db = root / "legacy.db"
        with sqlite3.connect(legacy_db) as conn:
            conn.execute("CREATE TABLE results (strategy TEXT, symbol TEXT, period TEXT, sharpe REAL)")
            conn.execute("INSERT INTO results VALUES ('fixture', 'BTCUSDT', 'train', 0.25)")
        legacy_tsv = root / "legacy.tsv"
        header = ["commit", "strategy", "symbol", "sharpe", "return_pct", "cagr_pct", "max_dd_pct",
                  "win_rate", "profit_factor", "trades", "sortino", "calmar", "status", "description", "period"]
        with legacy_tsv.open("w", encoding="utf-8", newline="") as tsv:
            writer = csv.writer(tsv, delimiter="\t", lineterminator="\n")
            writer.writerow(header)
            writer.writerow(["fixture", "fixture", "BTCUSDT", "0.25", "1.0", "0.2", "-5.0", "50", "1.1", "5", "0.2", "0.1", "discard", "fixture", "train"])
        artifacts = root / "strategies"
        artifacts.mkdir()
        (artifacts / "fixture.py").write_text("name = 'fixture'\n", encoding="utf-8")

        lifecycle = ResearchLifecycle(root / "lifecycle.sqlite3", root / "archive")
        maintenance_flag = root / "research_maintenance.flag"
        maintenance = lifecycle.enter_maintenance("fixture cutover rehearsal")
        write_maintenance_flag("fixture cutover rehearsal", maintenance["maintenance_id"], maintenance_flag)
        snapshot = capture_legacy_baseline(
            lifecycle=lifecycle, legacy_db=legacy_db, legacy_tsv=legacy_tsv,
            artifact_paths=[artifacts], snapshot_root=root / "snapshots",
        )
        snapshot_dir = Path(snapshot["snapshot_dir"])
        restore_target = root / "restore-check"
        imported = import_legacy_baseline(
            lifecycle=lifecycle, snapshot_dir=snapshot_dir, restore_target=restore_target,
        )
        report = run_preflight(
            lifecycle=lifecycle, snapshot_dir=snapshot_dir, export_path=root / "current.tsv",
            maintenance_flag=maintenance_flag,
            process_checker=lambda: [],
            compatibility_checker=lambda: {"fixture_writer": True},
        )
        if report["ready"]:
            report["rehearsal_record"] = lifecycle.record_cutover_rehearsal(
                cutover_id=str(uuid.uuid4()), preflight=report,
            )
        report["import"] = imported
        report["snapshot"] = snapshot
        report["restoration"] = verify_baseline(snapshot_dir, restore_target)
        return report


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "rehearse"))
    parser.add_argument("--lifecycle-ledger", type=Path)
    parser.add_argument("--lifecycle-artifacts", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--export", type=Path)
    args = parser.parse_args()
    if args.action == "rehearse":
        result = rehearse_fixture()
    else:
        if not all((args.lifecycle_ledger, args.lifecycle_artifacts, args.snapshot, args.export)):
            parser.error("preflight requires --lifecycle-ledger, --lifecycle-artifacts, --snapshot, and --export")
        lifecycle = ResearchLifecycle(args.lifecycle_ledger, args.lifecycle_artifacts)
        result = run_preflight(lifecycle=lifecycle, snapshot_dir=args.snapshot, export_path=args.export)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
