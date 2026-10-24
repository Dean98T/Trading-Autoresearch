from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from research_lifecycle import ResearchLifecycle


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_audit_in_temp_cwd(tmp_path):
    for name in ("audit_saved_strategies", "results_db"):
        sys.modules.pop(name, None)
    old_cwd = Path.cwd()
    sys.path.insert(0, str(PROJECT_ROOT))
    os.chdir(tmp_path)
    try:
        module = importlib.import_module("audit_saved_strategies")
    finally:
        os.chdir(old_cwd)
        for name in ("audit_saved_strategies", "results_db"):
            sys.modules.pop(name, None)
    return module


def _qualified_result(lifecycle, attempt_id, source, symbol="BTCUSDT"):
    evaluation = lifecycle.begin_evaluation(attempt_id, period="train", symbols=[symbol])
    lifecycle.record_outcome(
        evaluation["evaluation_id"],
        symbol=symbol,
        period="train",
        technical_outcome="completed",
        performance_qualification="qualified",
        selection_eligibility="eligible",
        metrics={"sharpe_ratio": 0.9, "num_trades": 10},
    )
    lifecycle.publish_current_results(evaluation["evaluation_id"])


def test_saved_strategy_audit_invalidates_only_exact_archived_source(tmp_path):
    module = _load_audit_in_temp_cwd(tmp_path)
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    source_a = b'name = "same_label"\nvalue = 1\n'
    source_b = b'name = "same_label"\nvalue = 2\n'
    first = lifecycle.begin_attempt(
        strategy_name="same_label", origin="manual", candidate_source=source_a
    )
    second = lifecycle.begin_attempt(
        strategy_name="same_label", origin="manual", candidate_source=source_b
    )
    _qualified_result(lifecycle, first["attempt_id"], source_a)
    _qualified_result(lifecycle, second["attempt_id"], source_b)
    strategy_path = tmp_path / "same_label.py"
    strategy_path.write_bytes(source_a)

    result = module.invalidate_matching_attempts(
        lifecycle=lifecycle,
        strategy_path=strategy_path,
        reason="prefix look-ahead audit failed",
    )

    assert result["status"] == "invalidated"
    assert result["attempt_ids"] == [first["attempt_id"]]
    by_attempt = {row["attempt_id"]: row for row in lifecycle.current_results()}
    assert by_attempt[first["attempt_id"]]["selection_eligibility"] == "ineligible"
    assert by_attempt[second["attempt_id"]]["selection_eligibility"] == "eligible"
    assert lifecycle.read_artifact(first["attempt_id"], "candidate_source") == source_a
