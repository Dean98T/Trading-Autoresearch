import hashlib
import importlib.util
import sys
from pathlib import Path

from research_lifecycle import ResearchLifecycle


def test_verifier_materializes_only_attempt_qualified_archived_source(tmp_path):
    runner_path = Path(__file__).resolve().parents[1] / "independent-verification" / "run_verification.py"
    spec = importlib.util.spec_from_file_location("lifecycle_verification_runner", runner_path)
    runner = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    source = "name = 'same'\nVALUE = 17\n"
    attempt = lifecycle.begin_attempt(strategy_name="same", origin="manual", candidate_source=source)
    evaluation = lifecycle.begin_evaluation(attempt["attempt_id"], period="train", symbols=["BTCUSDT"])
    lifecycle.record_outcome(
        evaluation["evaluation_id"], symbol="BTCUSDT", period="train",
        technical_outcome="completed", performance_qualification="unqualified",
        selection_eligibility="ineligible", metrics={"sharpe_ratio": -1.0},
    )
    lifecycle.publish_current_results(evaluation["evaluation_id"])

    target_dir = runner.materialize_lifecycle_strategies(lifecycle, tmp_path / "verification-source", tmp_path / "empty")
    materialized = target_dir / f"{attempt['attempt_key']}.py"
    assert materialized.read_bytes() == source.encode()
    assert hashlib.sha256(materialized.read_bytes()).hexdigest() == attempt["source_hash"]
    assert not (target_dir / "same.py").exists()
