import csv
import sqlite3

from research_baseline import capture_legacy_baseline, import_legacy_baseline, verify_baseline
from research_lifecycle import ResearchLifecycle


def test_baseline_snapshot_restore_and_repeat_import_preserve_legacy_values(tmp_path):
    legacy_db = tmp_path / "legacy.db"
    with sqlite3.connect(legacy_db) as conn:
        conn.execute("CREATE TABLE results (strategy TEXT, symbol TEXT, period TEXT, sharpe REAL)")
        conn.execute("INSERT INTO results VALUES ('old_name', 'BTCUSDT', 'test', 1.2345)")
    tsv = tmp_path / "results.tsv"
    header = ["commit", "strategy", "symbol", "sharpe", "return_pct", "cagr_pct", "max_dd_pct",
              "win_rate", "profit_factor", "trades", "sortino", "calmar", "status", "description", "period"]
    row = ["abc123", "old_name", "BTCUSDT", "1.23450", "12.00", "3.50", "-8.00", "55.0", "1.20", "10", "0.40", "0.35", "keep", "legacy", "test"]
    reserved_row = ["secret", "holdout_sentinel", "BTCUSDT", "999.0", "999", "999", "0", "100", "99", "999", "99", "99", "keep", "must never be exported", "final_holdout"]
    with tsv.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, delimiter="\t", lineterminator="\n")
        writer.writerow(header)
        writer.writerow(row)
        writer.writerow(reserved_row)
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir()
    (strategy_dir / "old_name.py").write_text("name = 'old_name'\n", encoding="utf-8")
    original_db = legacy_db.read_bytes()
    original_tsv = tsv.read_bytes()
    original_strategy = (strategy_dir / "old_name.py").read_bytes()

    lifecycle = ResearchLifecycle(tmp_path / "lifecycle.sqlite3", tmp_path / "archive")
    lifecycle.enter_maintenance("baseline capture and import fixture")
    captured = capture_legacy_baseline(
        lifecycle=lifecycle, legacy_db=legacy_db, legacy_tsv=tsv,
        artifact_paths=[strategy_dir], snapshot_root=tmp_path / "snapshots",
    )
    snapshot_dir = tmp_path / "snapshots" / captured["baseline_id"]
    restore_target = tmp_path / "restore-check"
    verified = verify_baseline(snapshot_dir, restore_target)
    assert verified["ready"] is True
    assert verified["restored"] is True
    imported = import_legacy_baseline(
        lifecycle=lifecycle, snapshot_dir=snapshot_dir, restore_target=tmp_path / "restore-check-repeat",
    )
    repeated = import_legacy_baseline(
        lifecycle=lifecycle, snapshot_dir=snapshot_dir, restore_target=tmp_path / "restore-check-repeat-2",
    )

    assert imported["imported"] is True
    assert repeated["imported"] is False
    assert len(lifecycle.legacy_results()) == 1
    legacy = next(item for item in lifecycle.legacy_results() if item["strategy"] == "old_name")
    assert legacy["strategy"] == "old_name"
    assert legacy["sharpe"] == "1.23450"
    assert legacy["evidence_role"] == "exposed_validation"
    assert legacy["provenance_status"] == "unknown"
    reserved = next(item for item in lifecycle.legacy_archive_rows() if item["strategy"] == "holdout_sentinel")
    assert reserved["evidence_role"] == "reserved_holdout"
    lifecycle.rebuild_current_results()
    exported = tmp_path / "development.tsv"
    lifecycle.export_current_tsv(exported)
    assert "holdout_sentinel" not in exported.read_text(encoding="utf-8")
    assert legacy_db.read_bytes() == original_db
    assert tsv.read_bytes() == original_tsv
    assert (strategy_dir / "old_name.py").read_bytes() == original_strategy
