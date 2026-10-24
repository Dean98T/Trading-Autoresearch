from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from research_lifecycle import ResearchLifecycle


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VALID_CODE = '''
name = "revalidate_fixture"
timeframe = "1h"
leverage = 1.0
import numpy as np
def generate_signals(prices):
    return np.zeros(len(prices))
'''


def _load_revalidate_in_temp_cwd(tmp_path):
    for name in ("revalidate", "verification_remediation", "results_db"):
        sys.modules.pop(name, None)
    old_cwd = Path.cwd()
    sys.path.insert(0, str(PROJECT_ROOT))
    os.chdir(tmp_path)
    try:
        module = importlib.import_module("revalidate")
    finally:
        os.chdir(old_cwd)
        for name in ("revalidate", "verification_remediation", "results_db"):
            sys.modules.pop(name, None)
    return module


def test_revalidation_appends_linked_evaluations_for_archived_source(tmp_path):
    module = _load_revalidate_in_temp_cwd(tmp_path)
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    attempt = lifecycle.begin_attempt(
        strategy_name="revalidate_fixture",
        origin="manual",
        candidate_source=VALID_CODE,
    )
    calls = []

    def evaluator(path, symbol, period, config):
        calls.append((symbol, period))
        return {"sharpe_ratio": 0.5, "num_trades": 8, "max_drawdown_pct": -10.0}

    first = module.revalidate_attempt_with_lifecycle(
        lifecycle=lifecycle,
        attempt_id=attempt["attempt_id"],
        symbols=["BTCUSDT"],
        config={},
        evaluator=evaluator,
        prefix_checker=lambda path: (True, "fixture pass"),
    )
    second = module.revalidate_attempt_with_lifecycle(
        lifecycle=lifecycle,
        attempt_id=attempt["attempt_id"],
        symbols=["BTCUSDT"],
        config={},
        evaluator=evaluator,
        prefix_checker=lambda path: (True, "fixture pass"),
    )

    first_train_id = first["train"]["evaluation"]["evaluation_id"]
    second_train_id = second["train"]["evaluation"]["evaluation_id"]
    first_test_id = first["test"]["evaluation"]["evaluation_id"]
    second_test_id = second["test"]["evaluation"]["evaluation_id"]
    assert first_train_id != second_train_id
    assert first_test_id != second_test_id
    assert lifecycle.get_evaluation(second_train_id)["parent_evaluation_id"] == first_train_id
    assert lifecycle.get_evaluation(second_test_id)["parent_evaluation_id"] == first_test_id
    assert len(lifecycle.list_attempts()) == 1
    assert calls == [("BTCUSDT", "train"), ("BTCUSDT", "test")] * 2
    current = lifecycle.current_results(attempt_id=attempt["attempt_id"])
    assert {row["evaluation_id"] for row in current} == {second_train_id, second_test_id}
