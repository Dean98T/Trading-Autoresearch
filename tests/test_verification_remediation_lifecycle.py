from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

from research_lifecycle import ResearchLifecycle


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE = '''
name = "remediation_fixture"
timeframe = "1h"
leverage = 1.0
import numpy as np
def generate_signals(prices):
    return np.zeros(len(prices))
'''


def _load_remediation_in_temp_cwd(tmp_path):
    for name in ("verification_remediation", "revalidate", "results_db"):
        sys.modules.pop(name, None)
    old_cwd = Path.cwd()
    sys.path.insert(0, str(PROJECT_ROOT))
    os.chdir(tmp_path)
    try:
        module = importlib.import_module("verification_remediation")
    finally:
        os.chdir(old_cwd)
        for name in ("verification_remediation", "revalidate", "results_db"):
            sys.modules.pop(name, None)
    return module


def test_remediation_reruns_exact_saved_attempt_without_destructive_purge(tmp_path):
    module = _load_remediation_in_temp_cwd(tmp_path)
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir()
    path = strategy_dir / "remediation_fixture.py"
    path.write_text(CODE, encoding="utf-8")
    attempt = lifecycle.begin_attempt(
        strategy_name="remediation_fixture", origin="manual", candidate_source=CODE
    )
    evaluation_calls = []

    def evaluator(candidate_path, symbol, period, config):
        evaluation_calls.append((symbol, period))
        return {"sharpe_ratio": 0.5, "num_trades": 8, "max_drawdown_pct": -10.0}

    result = module.rerun_lifecycle_strategy(
        lifecycle=lifecycle,
        strategy_name="remediation_fixture",
        strategy_path=path,
        symbols=["BTCUSDT"],
        config={},
        evaluator=evaluator,
        prefix_checker=lambda candidate_path: (True, "fixture pass"),
    )

    assert result["status"] == "keep"
    assert result["attempt"]["attempt_id"] == attempt["attempt_id"]
    assert evaluation_calls == [("BTCUSDT", "train"), ("BTCUSDT", "test")]
    assert path.read_text(encoding="utf-8") == CODE
    assert len(lifecycle.list_attempts()) == 1


def test_remediation_requires_a_fresh_lifecycle_export(tmp_path):
    module = _load_remediation_in_temp_cwd(tmp_path)
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    attempt = lifecycle.begin_attempt(
        strategy_name="freshness_fixture", origin="manual", candidate_source=CODE,
    )
    evaluation = lifecycle.begin_evaluation(attempt["attempt_id"], period="train", symbols=["BTCUSDT"])
    lifecycle.record_outcome(
        evaluation["evaluation_id"], symbol="BTCUSDT", period="train",
        technical_outcome="completed", performance_qualification="qualified",
        selection_eligibility="eligible", metrics={"sharpe_ratio": 0.5, "num_trades": 8},
    )
    lifecycle.publish_current_results(evaluation["evaluation_id"])
    export = tmp_path / "current.tsv"
    lifecycle.export_current_tsv(export)
    module.require_lifecycle_export(lifecycle, export)

    lifecycle.invalidate(attempt_id=attempt["attempt_id"], reason="fixture invalidation")
    with pytest.raises(Exception, match="stale"):
        module.require_lifecycle_export(lifecycle, export)
