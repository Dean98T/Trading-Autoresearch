from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from research_lifecycle import ResearchLifecycle


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE = '''
name = "template_fixture"
timeframe = "1h"
leverage = 1.0
import numpy as np
def generate_signals(prices):
    return np.zeros(len(prices))
'''


def _load_systematic_in_temp_cwd(tmp_path):
    for name in ("run_systematic", "run_research", "agent_research", "results_db"):
        sys.modules.pop(name, None)
    old_cwd = Path.cwd()
    sys.path.insert(0, str(PROJECT_ROOT))
    os.chdir(tmp_path)
    try:
        module = importlib.import_module("run_systematic")
    finally:
        os.chdir(old_cwd)
        for name in ("run_systematic", "run_research", "agent_research", "results_db"):
            sys.modules.pop(name, None)
    return module


def test_systematic_candidate_is_archived_before_evaluation_and_kept_by_identity(tmp_path):
    module = _load_systematic_in_temp_cwd(tmp_path)
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    output_dir = tmp_path / "strategies"
    calls = []

    def generate():
        attempts = lifecycle.list_attempts(origin="systematic")
        assert len(attempts) == 1
        assert attempts[0]["origin"] == "systematic"
        return CODE

    def evaluator(path, symbol, period, config):
        attempts = lifecycle.list_attempts(origin="systematic")
        assert lifecycle.read_artifact(attempts[0]["attempt_id"], "candidate_source") == CODE.encode()
        calls.append((symbol, period))
        return {"sharpe_ratio": 0.5, "num_trades": 8, "max_drawdown_pct": -10.0}

    result = module.run_systematic_candidate_with_lifecycle(
        lifecycle=lifecycle,
        strategy_name="template_fixture",
        generator=generate,
        template_parameters={"trend": "ema", "period": 20},
        symbols=["BTCUSDT"],
        config={},
        output_dir=output_dir,
        evaluator=evaluator,
    )

    assert result["status"] == "keep"
    assert result["attempt"]["origin"] == "systematic"
    assert calls == [("BTCUSDT", "train"), ("BTCUSDT", "test")]
    assert (output_dir / "template_fixture.py").read_text(encoding="utf-8") == CODE
    current = lifecycle.current_results(attempt_id=result["attempt"]["attempt_id"])
    assert {row["period"] for row in current} == {"train", "test"}
