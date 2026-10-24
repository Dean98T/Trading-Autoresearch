from dashboard import build_lifecycle_data, build_lifecycle_panel, get_lifecycle_source
from research_lifecycle import ResearchLifecycle


def test_dashboard_resolves_same_name_attempts_and_source_by_attempt_identity(tmp_path):
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    sources = ["name = 'shared'\nvalue = 1\n", "name = 'shared'\nvalue = 2\n"]
    attempts = [
        lifecycle.begin_attempt(
            strategy_name="shared", origin="manual", candidate_source=source,
            metadata={"requested_provider": "codex", "requested_model": "unknown", "actual_model": "unknown"},
        )
        for source in sources
    ]
    first_evaluation = lifecycle.begin_evaluation(
        attempts[0]["attempt_id"], period="train", symbols=["BTCUSDT", "ETHUSDT"],
    )
    lifecycle.record_outcome(
        first_evaluation["evaluation_id"], symbol="BTCUSDT", period="train",
        technical_outcome="completed", performance_qualification="qualified",
        selection_eligibility="eligible", metrics={"sharpe_ratio": 0.6},
    )
    lifecycle.publish_current_results(first_evaluation["evaluation_id"])
    lifecycle.invalidate(attempt_id=attempts[0]["attempt_id"], reason="look-ahead found")

    data = build_lifecycle_data(lifecycle)
    assert {attempt["attempt_key"] for attempt in data["attempts"]} == {
        attempt["attempt_key"] for attempt in attempts
    }
    first_rows = [row for row in data["results"] if row["attempt_id"] == attempts[0]["attempt_id"]]
    assert {row["technical_outcome"] for row in first_rows} == {"completed", "not_observed"}
    assert next(row for row in first_rows if row["symbol"] == "BTCUSDT")["selection_eligibility"] == "ineligible"
    assert data["legacy_test_label"] == "exposed validation"
    assert get_lifecycle_source(attempts[0]["attempt_id"], lifecycle) == (attempts[0]["attempt_key"], sources[0])
    panel = build_lifecycle_panel(lifecycle)
    assert attempts[0]["attempt_key"] in panel
    assert attempts[1]["attempt_key"] in panel
    assert "exposed validation" in panel
