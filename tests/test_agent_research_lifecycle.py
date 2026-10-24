from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from llm_client import LLMTimeoutError
from research_lifecycle import ResearchLifecycle


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VALID_CODE = '''
name = "generated_fixture"
timeframe = "1h"
leverage = 1.0
import numpy as np
def generate_signals(prices):
    return np.zeros(len(prices))
'''
RAW_RESPONSE = f"```python\n{VALID_CODE}```\n"


def _load_agent_research_in_temp_cwd(tmp_path):
    """Keep results_db's import-time initialization inside the test directory."""
    sys.modules.pop("agent_research", None)
    sys.modules.pop("results_db", None)
    old_cwd = Path.cwd()
    sys.path.insert(0, str(PROJECT_ROOT))
    os.chdir(tmp_path)
    try:
        module = importlib.import_module("agent_research")
    finally:
        os.chdir(old_cwd)
        sys.modules.pop("agent_research", None)
        sys.modules.pop("results_db", None)
    return module


class FakeClient:
    provider = "codex"

    def __init__(self, callback):
        self.callback = callback

    def _get_model(self):
        return None

    def chat(self, message, system="", temperature=None):
        return self.callback(message, system, temperature)


def test_autonomous_request_archives_prompts_and_raw_response_before_return(tmp_path):
    module = _load_agent_research_in_temp_cwd(tmp_path)
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    prompts = {"system": "system prompt exactly", "user": "user prompt exactly"}

    def provider_call(message, system, temperature):
        attempt = lifecycle.list_attempts()[0]
        assert lifecycle.read_artifact(attempt["attempt_id"], "caller_prompt/system") == prompts["system"].encode()
        assert lifecycle.read_artifact(attempt["attempt_id"], "caller_prompt/user") == prompts["user"].encode()
        assert temperature == 0.8
        return RAW_RESPONSE

    result = module.request_candidate_with_lifecycle(
        lifecycle=lifecycle,
        client=FakeClient(provider_call),
        system_prompt=prompts["system"],
        user_prompt=prompts["user"],
        config={"llm": {"provider": "codex", "api_key": "not-archived"}},
        experiment_num=7,
    )

    attempt_id = result["attempt"]["attempt_id"]
    assert result["error"] is None
    assert result["raw_response"] == RAW_RESPONSE
    assert result["code"] == module.extract_code(RAW_RESPONSE)
    assert lifecycle.read_artifact(attempt_id, "caller_response") == RAW_RESPONSE.encode()
    assert lifecycle.read_artifact(attempt_id, "candidate_source") == result["code"].encode()
    attempt = lifecycle.get_attempt(attempt_id)
    assert attempt["metadata"]["actual_model"] == "unknown"
    assert attempt["metadata"]["provider_internal_retries"] == "unknown"
    assert b"not-archived" not in lifecycle.read_artifact(attempt_id, "configuration")
    event_types = [event["event_type"] for event in attempt["events"]]
    assert "generation_completed" in event_types
    assert "validation_passed" in event_types
    candidate_name = next(event for event in attempt["events"] if event["event_type"] == "candidate_named")
    assert candidate_name["payload"]["strategy_name"] == "generated_fixture"
    assert attempt["display_strategy_name"] == "generated_fixture"


def test_timeout_is_recorded_and_retry_gets_a_new_linked_attempt(tmp_path):
    module = _load_agent_research_in_temp_cwd(tmp_path)
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    timeout = module.request_candidate_with_lifecycle(
        lifecycle=lifecycle,
        client=FakeClient(lambda *_: (_ for _ in ()).throw(LLMTimeoutError("fixture timeout"))),
        system_prompt="system",
        user_prompt="request one",
        config={},
        experiment_num=1,
    )

    assert isinstance(timeout["error"], LLMTimeoutError)
    assert timeout["raw_response"] is None
    prior_events = lifecycle.get_attempt(timeout["attempt"]["attempt_id"])["events"]
    failure = next(event for event in prior_events if event["event_type"] == "generation_failed")
    assert failure["payload"]["technical_outcome"] == "timeout"
    assert failure["payload"]["performance_qualification"] == "not_measured"

    retry = module.request_candidate_with_lifecycle(
        lifecycle=lifecycle,
        client=FakeClient(lambda *_: RAW_RESPONSE),
        system_prompt="system",
        user_prompt="request retry",
        config={},
        experiment_num=2,
        parent_attempt_id=timeout["attempt"]["attempt_id"],
    )
    assert retry["attempt"]["attempt_id"] != timeout["attempt"]["attempt_id"]
    assert retry["attempt"]["parent_attempt_id"] == timeout["attempt"]["attempt_id"]


def test_restart_marks_uncertain_generation_unknown_and_links_resubmission(tmp_path):
    module = _load_agent_research_in_temp_cwd(tmp_path)
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    interrupted = lifecycle.begin_attempt(
        strategy_name="autonomous_4",
        origin="autonomous",
        input_artifacts={"caller_prompt/user": "request before restart"},
    )
    lifecycle.record_event(
        interrupted["attempt_id"],
        event_type="generation_requested",
        stage="generation",
        payload={"temperature": 0.8},
    )

    resumed = module.request_candidate_with_lifecycle(
        lifecycle=lifecycle,
        client=FakeClient(lambda *_: RAW_RESPONSE),
        system_prompt="system",
        user_prompt="new request after restart",
        config={},
        experiment_num=5,
    )

    previous = lifecycle.get_attempt(interrupted["attempt_id"])
    interruption = next(event for event in previous["events"] if event["event_type"] == "generation_interrupted")
    assert interruption["payload"]["technical_outcome"] == "unknown"
    assert resumed["attempt"]["parent_attempt_id"] == interrupted["attempt_id"]


def test_rejected_response_keeps_raw_text_and_candidate_without_performance(tmp_path):
    module = _load_agent_research_in_temp_cwd(tmp_path)
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    invalid_response = "not valid python, but caller-visible"

    result = module.request_candidate_with_lifecycle(
        lifecycle=lifecycle,
        client=FakeClient(lambda *_: invalid_response),
        system_prompt="system",
        user_prompt="request",
        config={},
        experiment_num=2,
    )

    attempt_id = result["attempt"]["attempt_id"]
    assert result["valid"] is False
    assert lifecycle.read_artifact(attempt_id, "caller_response") == invalid_response.encode()
    assert lifecycle.read_artifact(attempt_id, "candidate_source") == invalid_response.encode()
    validation = next(
        event for event in result["attempt"]["events"] if event["event_type"] == "validation_failed"
    )
    assert validation["payload"]["performance_qualification"] == "not_measured"
    assert lifecycle.current_results() == []


def test_autonomous_evaluation_records_partial_symbols_under_one_attempt(tmp_path):
    module = _load_agent_research_in_temp_cwd(tmp_path)
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    strategy_path = tmp_path / "generated_fixture.py"
    strategy_path.write_text(VALID_CODE, encoding="utf-8")
    attempt = lifecycle.begin_attempt(
        strategy_name="generated_fixture",
        origin="autonomous",
        candidate_source=VALID_CODE,
    )
    calls = []

    def evaluator(path, symbol, period):
        calls.append((symbol, period))
        if symbol == "BTCUSDT" and period == "test":
            raise TimeoutError("fixture timeout")
        if symbol == "ETHUSDT":
            return {"sharpe_ratio": -0.1, "num_trades": 8, "max_drawdown_pct": -10.0}
        return {"sharpe_ratio": 0.5, "num_trades": 8, "max_drawdown_pct": -10.0}

    result = module.evaluate_attempt_with_lifecycle(
        lifecycle=lifecycle,
        attempt_id=attempt["attempt_id"],
        strategy_path=str(strategy_path),
        symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        evaluator=evaluator,
        config={"data": {"train_start": "2021-01-01", "test_start": "2025-01-01"}},
    )

    assert calls == [
        ("BTCUSDT", "train"),
        ("BTCUSDT", "test"),
        ("ETHUSDT", "train"),
        ("SOLUSDT", "train"),
        ("SOLUSDT", "test"),
    ]
    rows = lifecycle.current_results(attempt_id=attempt["attempt_id"])
    by_symbol_period = {(row["symbol"], row["period"]): row for row in rows}
    assert by_symbol_period[("BTCUSDT", "test")]["technical_outcome"] == "timeout"
    assert by_symbol_period[("BTCUSDT", "test")]["metrics"] is None
    assert by_symbol_period[("ETHUSDT", "test")]["technical_outcome"] == "skipped"
    assert by_symbol_period[("ETHUSDT", "test")]["metrics"] is None
    assert by_symbol_period[("SOLUSDT", "test")]["selection_eligibility"] == "eligible"
    assert by_symbol_period[("BTCUSDT", "train")]["attempt_id"] == attempt["attempt_id"]
    assert by_symbol_period[("BTCUSDT", "train")]["evaluation_id"] != by_symbol_period[("BTCUSDT", "test")]["evaluation_id"]


def test_restart_prompt_feedback_uses_lifecycle_exposed_validation_and_current_train_rows(tmp_path):
    module = _load_agent_research_in_temp_cwd(tmp_path)
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    attempt = lifecycle.begin_attempt(
        strategy_name="prompt_fixture", origin="autonomous", candidate_source=VALID_CODE,
        metadata={"experiment_number": 17},
    )
    for period, sharpe in (("train", 0.4), ("test", 0.7)):
        evaluation = lifecycle.begin_evaluation(
            attempt["attempt_id"], period=period, symbols=["BTCUSDT"],
            context={"data_role": "exposed_validation" if period == "test" else "development"},
        )
        lifecycle.record_outcome(
            evaluation["evaluation_id"], symbol="BTCUSDT", period=period,
            technical_outcome="completed", performance_qualification="qualified",
            selection_eligibility="eligible", metrics={"sharpe_ratio": sharpe, "num_trades": 12},
        )
        lifecycle.publish_current_results(evaluation["evaluation_id"])

    history = module.load_recent_history_from_lifecycle(lifecycle, n=20)
    prompt = module.build_experiment_prompt(
        program="fixture", current_strategy=VALID_CODE, history=history,
        experiment_num=18, lifecycle=lifecycle,
    )
    assert len(history) == 1
    assert history[0]["name"] == "prompt_fixture"
    assert history[0]["avg_sharpe"] == 0.4
    assert "exposed validation" in prompt.lower()
    assert "not final holdout" in prompt.lower()
    assert "final holdout is reserved" in prompt.lower()


def test_lifecycle_prompt_feedback_excludes_reserved_legacy_holdout(tmp_path):
    module = _load_agent_research_in_temp_cwd(tmp_path)
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    lifecycle.enter_maintenance("legacy fixture import")
    row = {
        "commit": "unknown", "strategy": "holdout_sentinel", "symbol": "BTCUSDT",
        "sharpe": "999", "return_pct": "999", "cagr_pct": "999", "max_dd_pct": "0",
        "win_rate": "99", "profit_factor": "99", "trades": "999", "sortino": "99",
        "calmar": "99", "status": "keep", "description": "reserved fixture",
        "period": "final_holdout",
    }
    lifecycle.import_legacy_baseline(
        baseline_id="legacy-fixture", manifest_hash="fixture-hash", rows=[row],
        manifest={"source": "fixture"},
    )
    lifecycle.exit_maintenance()
    lifecycle.rebuild_current_results()

    history = module.load_recent_history_from_lifecycle(lifecycle, n=20)
    prompt = module.build_experiment_prompt(
        program="fixture", current_strategy=VALID_CODE, history=history,
        experiment_num=18, lifecycle=lifecycle,
    )

    assert "holdout_sentinel" not in repr(history)
    assert "999.000" not in prompt


def test_lifecycle_history_requires_exposed_validation_to_mark_an_attempt_kept(tmp_path):
    module = _load_agent_research_in_temp_cwd(tmp_path)
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    attempt = lifecycle.begin_attempt(
        strategy_name="train_only_winner", origin="autonomous", candidate_source=VALID_CODE,
    )
    train = lifecycle.begin_evaluation(attempt["attempt_id"], period="train", symbols=["BTCUSDT"])
    lifecycle.record_outcome(
        train["evaluation_id"], symbol="BTCUSDT", period="train",
        technical_outcome="completed", performance_qualification="qualified",
        selection_eligibility="eligible", metrics={"sharpe_ratio": 0.5, "num_trades": 12},
    )
    lifecycle.publish_current_results(train["evaluation_id"])
    test = lifecycle.begin_evaluation(attempt["attempt_id"], period="test", symbols=["BTCUSDT"])
    lifecycle.record_outcome(
        test["evaluation_id"], symbol="BTCUSDT", period="test",
        technical_outcome="completed", performance_qualification="unqualified",
        selection_eligibility="ineligible", metrics={"sharpe_ratio": -0.2, "num_trades": 12},
    )
    lifecycle.publish_current_results(test["evaluation_id"])

    history = module.load_recent_history_from_lifecycle(lifecycle)

    assert history[0]["status"] == "discard"
    assert history[0]["fail_reason"] == "exposed_validation_failure"


def test_lifecycle_best_focus_score_uses_current_exposed_validation_rows(tmp_path):
    module = _load_agent_research_in_temp_cwd(tmp_path)
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    attempt = lifecycle.begin_attempt(
        strategy_name="focus_fixture", origin="autonomous", candidate_source=VALID_CODE,
    )
    evaluation = lifecycle.begin_evaluation(
        attempt["attempt_id"], period="test", symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    )
    for symbol, sharpe in (("BTCUSDT", 1.0), ("ETHUSDT", 0.5), ("SOLUSDT", 2.0)):
        lifecycle.record_outcome(
            evaluation["evaluation_id"], symbol=symbol, period="test",
            technical_outcome="completed", performance_qualification="qualified",
            selection_eligibility="eligible", metrics={"sharpe_ratio": sharpe, "num_trades": 20},
        )
    lifecycle.publish_current_results(evaluation["evaluation_id"])

    assert module._lifecycle_best_focus_score(lifecycle) == module.compute_focus_score([
        {"symbol": "BTCUSDT", "sharpe": 1.0},
        {"symbol": "ETHUSDT", "sharpe": 0.5},
        {"symbol": "SOLUSDT", "sharpe": 2.0},
    ])
