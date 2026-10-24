import importlib
import os
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_results_db_in_temp_cwd(tmp_path):
    sys.modules.pop("results_db", None)
    old_cwd = Path.cwd()
    sys.path.insert(0, str(PROJECT_ROOT))
    os.chdir(tmp_path)
    try:
        return importlib.import_module("results_db")
    finally:
        os.chdir(old_cwd)
        sys.modules.pop("results_db", None)


def _metrics(strategy, sharpe):
    return {
        "strategy": strategy, "symbol": "BTCUSDT", "sharpe_ratio": sharpe,
        "total_return_pct": 10.0, "annual_return_pct": 2.0, "max_drawdown_pct": -5.0,
        "win_rate": 50.0, "profit_factor": 1.2, "num_trades": 8,
        "sortino_ratio": 0.3, "calmar_ratio": 0.2,
    }


def test_legacy_feedback_and_dashboard_queries_exclude_reserved_holdout_rows(tmp_path):
    db = _load_results_db_in_temp_cwd(tmp_path)
    db.DB_FILE = tmp_path / "results.db"
    db.append_results([_metrics("visible_train", 0.2)], "discard", "train", period="train")
    db.append_results([_metrics("visible_test", 0.4)], "keep", "test", period="test")
    with pytest.raises(ValueError, match="reserved final holdout"):
        db.append_results([_metrics("reserved_holdout_sentinel", 99.0)], "keep", "hidden", period="final_holdout")
    # Model an old direct write so every legacy reader is checked against persisted reserved rows.
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO results
               (git_commit, strategy, symbol, sharpe, return_pct, cagr_pct, max_dd_pct, win_rate,
                profit_factor, trades, sortino, calmar, status, description, period)
               VALUES ('fixture', 'reserved_holdout_sentinel', 'BTCUSDT', 99, 99, 99, 0, 99,
                       99, 999, 99, 99, 'keep', 'hidden', 'final_holdout')"""
        )
    db._stats_dirty = True
    db._concept_cache.clear()
    db._scatter_cache.clear()

    loaded = db.load_results()
    assert set(loaded["strategy"]) == {"visible_train", "visible_test"}
    assert db.query_stats()["total"] == 2
    assert set(db.query_recent_strategy_names(20)) == {"visible_train", "visible_test"}
    assert "reserved_holdout_sentinel" not in repr(db.query_concept_stats())
    assert all(item["family"] != "reserved_holdout" for item in db.query_exhausted_families(min_variants=1))
    for query in (db.query_top_rows, db.query_avg_rows):
        with pytest.raises(ValueError, match="reserved final holdout"):
            query("final_holdout")
    with pytest.raises(ValueError, match="reserved final holdout"):
        db.query_chart_data(period="final_holdout")
