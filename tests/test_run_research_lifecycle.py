from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

from research_lifecycle import ResearchLifecycle


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_run_research_in_temp_cwd(tmp_path):
    """Import the legacy entrypoint where its auto-init DB stays temporary."""
    sys.modules.pop("run_research", None)
    sys.modules.pop("results_db", None)
    old_cwd = Path.cwd()
    sys.path.insert(0, str(PROJECT_ROOT))
    os.chdir(tmp_path)
    try:
        module = importlib.import_module("run_research")
    finally:
        os.chdir(old_cwd)
        sys.modules.pop("run_research", None)
        sys.modules.pop("results_db", None)
    return module


VALID_STRATEGY = '''
name = "manual_fixture"
timeframe = "1h"
leverage = 1.0
import numpy as np
def generate_signals(prices):
    return np.zeros(len(prices))
'''


def test_manual_lifecycle_entrypoint_records_attempt_before_evaluation(tmp_path):
    run_research = _load_run_research_in_temp_cwd(tmp_path)
    strategy_path = tmp_path / "manual_fixture.py"
    strategy_path.write_text(VALID_STRATEGY, encoding="utf-8")
    lifecycle = ResearchLifecycle(
        ledger_path=tmp_path / "ledger.sqlite3",
        artifact_dir=tmp_path / "artifacts",
    )
    evaluator_calls = []

    def evaluator(path, symbol, period, config):
        attempts = lifecycle.list_attempts()
        assert len(attempts) == 1
        assert lifecycle.read_artifact(attempts[0]["attempt_id"], "candidate_source") == VALID_STRATEGY.encode()
        evaluator_calls.append((symbol, period))
        return {
            "sharpe_ratio": -0.2,
            "total_return_pct": -5.0,
            "annual_return_pct": -3.0,
            "max_drawdown_pct": -12.0,
            "win_rate": 40.0,
            "profit_factor": 0.8,
            "num_trades": 8,
            "sortino_ratio": -0.1,
            "calmar_ratio": -0.2,
        }

    result = run_research.run_lifecycle_experiment(
        strategy_path=str(strategy_path),
        symbols=["BTCUSDT"],
        period="train",
        config={"provider": {"api_token": "do-not-archive"}},
        lifecycle=lifecycle,
        evaluator=evaluator,
    )

    assert evaluator_calls == [("BTCUSDT", "train")]
    assert result["status"] == "discard"
    current = lifecycle.current_results()
    assert len(current) == 1
    assert current[0]["attempt_id"] == result["attempt"]["attempt_id"]
    assert current[0]["technical_outcome"] == "completed"
    assert current[0]["performance_qualification"] == "unqualified"
    assert current[0]["selection_eligibility"] == "ineligible"
    assert current[0]["metrics"]["sharpe_ratio"] == -0.2
    config_artifact = lifecycle.read_artifact(result["attempt"]["attempt_id"], "configuration")
    assert b"do-not-archive" not in config_artifact


def test_manual_lifecycle_entrypoint_never_calls_evaluator_after_archive_failure(tmp_path):
    run_research = _load_run_research_in_temp_cwd(tmp_path)
    strategy_path = tmp_path / "manual_fixture.py"
    strategy_path.write_text(VALID_STRATEGY, encoding="utf-8")
    blocked_archive = tmp_path / "not-a-directory"
    blocked_archive.write_text("file blocks artifact directory")
    lifecycle = ResearchLifecycle(
        ledger_path=tmp_path / "ledger.sqlite3",
        artifact_dir=blocked_archive,
    )
    evaluator_calls = []

    with pytest.raises(OSError):
        run_research.run_lifecycle_experiment(
            strategy_path=str(strategy_path),
            symbols=["BTCUSDT"],
            period="train",
            config={},
            lifecycle=lifecycle,
            evaluator=lambda *args: evaluator_calls.append(args),
        )

    assert evaluator_calls == []
    assert lifecycle.list_attempts() == []


def test_full_lifecycle_keeps_symbol_outcomes_independent_and_skips_failed_train(tmp_path):
    run_research = _load_run_research_in_temp_cwd(tmp_path)
    strategy_path = tmp_path / "manual_fixture.py"
    strategy_path.write_text(VALID_STRATEGY, encoding="utf-8")
    lifecycle = ResearchLifecycle(
        ledger_path=tmp_path / "ledger.sqlite3",
        artifact_dir=tmp_path / "artifacts",
    )
    calls = []

    def evaluator(path, symbol, period, config):
        calls.append((symbol, period))
        if symbol == "BTCUSDT" and period == "test":
            raise TimeoutError("fixture timeout")
        if symbol == "ETHUSDT":
            return {"sharpe_ratio": -0.1, "num_trades": 8, "max_drawdown_pct": -10.0}
        return {"sharpe_ratio": 0.5, "num_trades": 8, "max_drawdown_pct": -10.0}

    result = run_research.run_lifecycle_full_experiment(
        strategy_path=str(strategy_path),
        symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        config={},
        lifecycle=lifecycle,
        evaluator=evaluator,
    )

    assert calls == [
        ("BTCUSDT", "train"),
        ("ETHUSDT", "train"),
        ("SOLUSDT", "train"),
        ("BTCUSDT", "test"),
        ("SOLUSDT", "test"),
    ]
    assert result["status"] == "keep"
    current = lifecycle.current_results(attempt_id=result["attempt"]["attempt_id"])
    by_symbol_period = {(row["symbol"], row["period"]): row for row in current}
    assert by_symbol_period[("BTCUSDT", "test")]["technical_outcome"] == "error"
    assert by_symbol_period[("BTCUSDT", "test")]["metrics"] is None
    assert by_symbol_period[("ETHUSDT", "train")]["performance_qualification"] == "unqualified"
    assert by_symbol_period[("ETHUSDT", "test")]["technical_outcome"] == "skipped"
    assert by_symbol_period[("ETHUSDT", "test")]["metrics"] is None
    assert by_symbol_period[("SOLUSDT", "test")]["selection_eligibility"] == "eligible"
    assert by_symbol_period[("BTCUSDT", "train")]["evaluation_id"] != by_symbol_period[("BTCUSDT", "test")]["evaluation_id"]


def test_existing_attempt_can_be_evaluated_without_creating_a_replacement_attempt(tmp_path):
    run_research = _load_run_research_in_temp_cwd(tmp_path)
    strategy_path = tmp_path / "manual_fixture.py"
    strategy_path.write_text(VALID_STRATEGY, encoding="utf-8")
    lifecycle = ResearchLifecycle(
        ledger_path=tmp_path / "ledger.sqlite3",
        artifact_dir=tmp_path / "artifacts",
    )
    attempt = lifecycle.begin_attempt(
        strategy_name="manual_fixture",
        origin="systematic",
        candidate_source=VALID_STRATEGY,
    )

    result = run_research.evaluate_lifecycle_attempt(
        attempt_id=attempt["attempt_id"],
        strategy_path=str(strategy_path),
        symbols=["BTCUSDT"],
        config={},
        lifecycle=lifecycle,
        evaluator=lambda path, symbol, period, config: {
            "sharpe_ratio": 0.5,
            "num_trades": 8,
            "max_drawdown_pct": -10.0,
        },
    )

    assert result["attempt"]["attempt_id"] == attempt["attempt_id"]
    assert len(lifecycle.list_attempts()) == 1
    assert result["status"] == "keep"
