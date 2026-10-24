from __future__ import annotations

import hashlib
import csv
from concurrent.futures import ThreadPoolExecutor

import pytest

from research_lifecycle import ResearchLifecycle


def test_same_name_attempts_keep_distinct_exact_sources_and_safe_snapshots(tmp_path):
    lifecycle = ResearchLifecycle(
        ledger_path=tmp_path / "ledger.sqlite3",
        artifact_dir=tmp_path / "artifacts",
    )

    first_source = 'name = "momentum"\nvalue = 1\n'
    second_source = 'name = "momentum"\nvalue = 2\n'
    first = lifecycle.begin_attempt(
        strategy_name="momentum",
        origin="manual",
        candidate_source=first_source,
        configuration={"provider": "codex", "api_key": "must-not-be-stored"},
        source_snapshot={"backtest.py": "working-tree version A"},
        metadata={"requested_model": "test-model", "actual_model": "unknown"},
    )
    second = lifecycle.begin_attempt(
        strategy_name="momentum",
        origin="manual",
        candidate_source=second_source,
        configuration={"provider": "codex"},
        source_snapshot={"backtest.py": "working-tree version B"},
    )

    assert first["attempt_id"] != second["attempt_id"]
    assert first["attempt_key"] != second["attempt_key"]
    assert first["source_hash"] == hashlib.sha256(first_source.encode()).hexdigest()
    assert lifecycle.read_artifact(first["attempt_id"], "candidate_source") == first_source.encode()
    assert lifecycle.read_artifact(second["attempt_id"], "candidate_source") == second_source.encode()
    assert lifecycle.read_artifact(first["attempt_id"], "source_snapshot/backtest.py") == b"working-tree version A"
    assert lifecycle.read_artifact(second["attempt_id"], "source_snapshot/backtest.py") == b"working-tree version B"

    history = lifecycle.list_attempts(strategy_name="momentum")
    assert {item["attempt_id"] for item in history} == {first["attempt_id"], second["attempt_id"]}
    assert "must-not-be-stored" not in repr(history)
    first_record = next(item for item in history if item["attempt_id"] == first["attempt_id"])
    assert first_record["metadata"]["actual_model"] == "unknown"
    archived_config = lifecycle.read_artifact(first["attempt_id"], "configuration")
    assert b"must-not-be-stored" not in archived_config
    assert b"[REDACTED]" in archived_config


def test_completed_losing_evaluation_remains_in_attempt_specific_current_view(tmp_path):
    lifecycle = ResearchLifecycle(
        ledger_path=tmp_path / "ledger.sqlite3",
        artifact_dir=tmp_path / "artifacts",
    )
    attempt = lifecycle.begin_attempt(
        strategy_name="momentum",
        origin="manual",
        candidate_source='name = "momentum"\n',
    )
    evaluation = lifecycle.begin_evaluation(
        attempt["attempt_id"],
        period="train",
        symbols=["BTCUSDT"],
        context={"window": "development"},
    )

    lifecycle.record_outcome(
        evaluation["evaluation_id"],
        symbol="BTCUSDT",
        period="train",
        technical_outcome="completed",
        performance_qualification="unqualified",
        selection_eligibility="ineligible",
        metrics={"sharpe_ratio": -0.2, "num_trades": 7},
    )
    lifecycle.publish_current_results(evaluation["evaluation_id"])

    rows = lifecycle.current_results()
    assert len(rows) == 1
    assert rows[0]["attempt_id"] == attempt["attempt_id"]
    assert rows[0]["strategy_key"] == attempt["attempt_key"]
    assert rows[0]["strategy_name"] == "momentum"
    assert rows[0]["evaluation_id"] == evaluation["evaluation_id"]
    assert rows[0]["technical_outcome"] == "completed"
    assert rows[0]["performance_qualification"] == "unqualified"
    assert rows[0]["selection_eligibility"] == "ineligible"
    assert rows[0]["metrics"] == {"sharpe_ratio": -0.2, "num_trades": 7}


def test_attempt_creation_fails_closed_when_candidate_cannot_be_archived(tmp_path):
    blocked_archive = tmp_path / "not-a-directory"
    blocked_archive.write_text("file blocks directory creation")
    lifecycle = ResearchLifecycle(
        ledger_path=tmp_path / "ledger.sqlite3",
        artifact_dir=blocked_archive,
    )

    with pytest.raises(OSError):
        lifecycle.begin_attempt(
            strategy_name="cannot-run",
            origin="manual",
            candidate_source="candidate source",
        )

    assert lifecycle.list_attempts() == []


def test_invalidation_removes_only_scoped_evidence_from_selection_and_keeps_history(tmp_path):
    lifecycle = ResearchLifecycle(
        ledger_path=tmp_path / "ledger.sqlite3",
        artifact_dir=tmp_path / "artifacts",
    )
    source = 'name = "audited"\n'
    attempt = lifecycle.begin_attempt(
        strategy_name="audited",
        origin="manual",
        candidate_source=source,
    )
    evaluation = lifecycle.begin_evaluation(
        attempt["attempt_id"],
        period="train",
        symbols=["BTCUSDT", "ETHUSDT"],
    )
    for symbol in ("BTCUSDT", "ETHUSDT"):
        lifecycle.record_outcome(
            evaluation["evaluation_id"],
            symbol=symbol,
            period="train",
            technical_outcome="completed",
            performance_qualification="qualified",
            selection_eligibility="eligible",
            metrics={"sharpe_ratio": 0.8, "num_trades": 12},
        )
    lifecycle.publish_current_results(evaluation["evaluation_id"])

    invalidation = lifecycle.invalidate(
        attempt_id=attempt["attempt_id"],
        reason="prefix look-ahead audit failed",
        evaluation_id=evaluation["evaluation_id"],
        symbol="BTCUSDT",
    )
    lifecycle.publish_current_results(evaluation["evaluation_id"])

    rows = lifecycle.current_results(attempt_id=attempt["attempt_id"])
    by_symbol = {row["symbol"]: row for row in rows}
    assert invalidation["reason"] == "prefix look-ahead audit failed"
    assert by_symbol["BTCUSDT"]["selection_eligibility"] == "ineligible"
    assert by_symbol["BTCUSDT"]["invalidation_reason"] == "prefix look-ahead audit failed"
    assert by_symbol["BTCUSDT"]["metrics"] == {"sharpe_ratio": 0.8, "num_trades": 12}
    assert by_symbol["ETHUSDT"]["selection_eligibility"] == "eligible"
    assert [row["symbol"] for row in lifecycle.current_results(selection_eligible_only=True)] == ["ETHUSDT"]
    assert lifecycle.read_artifact(attempt["attempt_id"], "candidate_source") == source.encode()
    assert any(event["event_type"] == "invalidated" for event in lifecycle.get_attempt(attempt["attempt_id"])["events"])


def test_rebuild_and_atomic_tsv_export_preserve_attempt_identity_and_freshness(tmp_path):
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    first = lifecycle.begin_attempt(strategy_name="same", origin="manual", candidate_source="x=1")
    second = lifecycle.begin_attempt(strategy_name="same", origin="manual", candidate_source="x=2")
    for attempt, sharpe in ((first, 0.5), (second, 0.9)):
        evaluation = lifecycle.begin_evaluation(attempt["attempt_id"], period="train", symbols=["BTCUSDT", "ETHUSDT"])
        lifecycle.record_outcome(
            evaluation["evaluation_id"], symbol="BTCUSDT", period="train",
            technical_outcome="completed", performance_qualification="qualified",
            selection_eligibility="eligible", metrics={"sharpe_ratio": sharpe, "total_return_pct": 12.0,
                "annual_return_pct": 3.0, "max_drawdown_pct": -8.0, "win_rate": 55.0,
                "profit_factor": 1.3, "num_trades": 10, "sortino_ratio": 0.4, "calmar_ratio": 0.3},
        )
        lifecycle.publish_current_results(evaluation["evaluation_id"])

    tsv_path = tmp_path / "results.tsv"
    first_export = lifecycle.export_current_tsv(tsv_path)
    rows = list(csv.DictReader(tsv_path.open(encoding="utf-8"), delimiter="\t"))
    assert len(rows) == 4  # the unobserved ETH rows remain explicit
    assert {row["strategy"] for row in rows} == {first["attempt_key"], second["attempt_key"]}
    assert lifecycle.export_freshness(tsv_path)["fresh"] is True

    lifecycle.invalidate(attempt_id=first["attempt_id"], reason="audit failed")
    assert lifecycle.export_freshness(tsv_path)["fresh"] is False
    with pytest.raises(Exception, match="stale"):
        lifecycle.require_fresh_export(tsv_path)

    lifecycle.rebuild_current_results()
    lifecycle.export_current_tsv(tsv_path)
    assert lifecycle.export_freshness(tsv_path)["fresh"] is True
    assert first_export["sha256"] != hashlib.sha256(tsv_path.read_bytes()).hexdigest()
    assert len(lifecycle.current_results()) == 4


def test_ordinary_lifecycle_cannot_create_reserved_final_holdout_evidence(tmp_path):
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    with pytest.raises(ValueError, match="final holdout"):
        lifecycle.begin_attempt(
            strategy_name="bad-alias", origin="manual", candidate_source="x=1",
            requested_period="final_holdout",
        )

    attempt = lifecycle.begin_attempt(strategy_name="candidate", origin="manual", candidate_source="x=1")
    with pytest.raises(ValueError, match="final holdout"):
        lifecycle.begin_evaluation(attempt["attempt_id"], period="holdout", symbols=["BTCUSDT"])


def test_maintenance_blocks_new_research_and_can_be_explicitly_resumed(tmp_path):
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    state = lifecycle.enter_maintenance("baseline capture")
    assert state["state"] == "active"
    with pytest.raises(RuntimeError, match="maintenance"):
        lifecycle.begin_attempt(strategy_name="blocked", origin="manual", candidate_source="x=1")
    resumed = lifecycle.exit_maintenance()
    assert resumed["state"] == "inactive"
    assert lifecycle.begin_attempt(strategy_name="allowed", origin="manual", candidate_source="x=1")


def test_maintenance_waits_for_registered_writer_lease(tmp_path):
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    with lifecycle.writer_session("fixture-writer"):
        status = lifecycle.enter_maintenance("quiescence fixture")
        assert status["state"] == "active"
        assert status["quiescent"] is False
        with pytest.raises(RuntimeError, match="writer session"):
            lifecycle.exit_maintenance()
    status = lifecycle.maintenance_status()
    assert status["quiescent"] is True
    assert lifecycle.exit_maintenance()["state"] == "inactive"


def test_recovery_rebuild_is_idempotent_and_does_not_create_evaluations(tmp_path):
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    attempt = lifecycle.begin_attempt(strategy_name="recover", origin="manual", candidate_source="x=1")
    evaluation = lifecycle.begin_evaluation(attempt["attempt_id"], period="train", symbols=["BTCUSDT"])
    lifecycle.record_outcome(
        evaluation["evaluation_id"], symbol="BTCUSDT", period="train",
        technical_outcome="completed", performance_qualification="qualified",
        selection_eligibility="eligible", metrics={"sharpe_ratio": 0.7},
    )

    with pytest.raises(Exception, match="stale"):
        lifecycle.require_fresh_view()
    lifecycle.rebuild_current_results()
    after_first = lifecycle.current_results()
    lifecycle.rebuild_current_results()
    after_second = lifecycle.current_results()

    assert after_second == after_first
    assert len(lifecycle.get_attempt(attempt["attempt_id"])["evaluations"]) == 1


def test_failed_atomic_export_keeps_previous_file_and_committed_evidence(tmp_path, monkeypatch):
    import research_lifecycle

    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    attempt = lifecycle.begin_attempt(strategy_name="export", origin="manual", candidate_source="x=1")
    evaluation = lifecycle.begin_evaluation(attempt["attempt_id"], period="train", symbols=["BTCUSDT"])
    lifecycle.record_outcome(
        evaluation["evaluation_id"], symbol="BTCUSDT", period="train",
        technical_outcome="completed", performance_qualification="unqualified",
        selection_eligibility="ineligible", metrics={"sharpe_ratio": -0.4},
    )
    lifecycle.publish_current_results(evaluation["evaluation_id"])
    path = tmp_path / "results.tsv"
    lifecycle.export_current_tsv(path)
    old_export = path.read_bytes()

    original_replace = research_lifecycle.os.replace
    monkeypatch.setattr(research_lifecycle.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("simulated export failure")))
    with pytest.raises(OSError, match="simulated"):
        lifecycle.export_current_tsv(path)
    monkeypatch.setattr(research_lifecycle.os, "replace", original_replace)

    assert path.read_bytes() == old_export
    assert lifecycle.get_attempt(attempt["attempt_id"])["evaluations"][0]["outcomes"][0]["metrics"] == {"sharpe_ratio": -0.4}
    assert lifecycle.export_freshness(path)["fresh"] is True


def test_concurrent_same_name_writers_keep_attempt_and_publication_contexts_separate(tmp_path):
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")

    def write_attempt(index: int):
        source = f"x = {index}\n"
        attempt = lifecycle.begin_attempt(strategy_name="concurrent", origin="systematic", candidate_source=source)
        evaluation = lifecycle.begin_evaluation(attempt["attempt_id"], period="train", symbols=["BTCUSDT", "ETHUSDT"])
        lifecycle.record_outcome(
            evaluation["evaluation_id"], symbol="BTCUSDT", period="train",
            technical_outcome="completed", performance_qualification="qualified",
            selection_eligibility="eligible", metrics={"sharpe_ratio": float(index)},
        )
        lifecycle.publish_current_results(evaluation["evaluation_id"])
        return attempt["attempt_id"], source.encode(), evaluation["evaluation_id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        written = list(pool.map(write_attempt, range(8)))

    rows = lifecycle.current_results()
    assert len({attempt_id for attempt_id, _, _ in written}) == 8
    assert len(rows) == 16
    for attempt_id, source, evaluation_id in written:
        attempt = lifecycle.get_attempt(attempt_id)
        matches = [row for row in rows if row["attempt_id"] == attempt_id]
        assert attempt["source_hash"] == hashlib.sha256(source).hexdigest()
        assert {row["evaluation_id"] for row in matches} == {evaluation_id}
        assert {row["strategy_key"] for row in matches} == {attempt["attempt_key"]}
