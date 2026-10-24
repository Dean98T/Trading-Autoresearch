"""Durable research lifecycle for auditable strategy experiment attempts.

The public interface in this module is the supported seam for new research
writers. SQLite stores authoritative metadata and artifact references; exact
artifact bytes live in a content-addressed archive beside the configured
ledger. Existing ``results.db`` consumers remain independent of this pilot.
"""

from __future__ import annotations

import hashlib
import csv
import io
import json
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ResearchLifecycle:
    """Record experiment identities and their immutable input artifacts.

    Construct with isolated paths in tests or explicit paths for an opt-in
    research run. ``begin_attempt`` must succeed before any generation or
    evaluation work is started. If a required artifact cannot be retained, it
    raises and leaves no accepted attempt in the ledger.
    """

    def __init__(self, ledger_path: str | Path, artifact_dir: str | Path):
        self.ledger_path = Path(ledger_path)
        self.artifact_dir = Path(artifact_dir)
        self._initialize_ledger()

    def list_attempts(self, strategy_name: str | None = None, origin: str | None = None) -> list[dict[str, Any]]:
        """Return recorded attempt identities and their exact artifact refs."""
        query = "SELECT * FROM attempts WHERE 1=1"
        params: list[Any] = []
        if strategy_name is not None:
            query += " AND strategy_name=?"
            params.append(strategy_name)
        if origin is not None:
            query += " AND origin=?"
            params.append(origin)
        query += " ORDER BY created_at, attempt_id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._attempt_dict(conn, row) for row in rows]

    def begin_attempt(
        self,
        *,
        strategy_name: str,
        origin: str,
        candidate_source: str | bytes | None = None,
        configuration: Mapping[str, Any] | None = None,
        source_snapshot: Mapping[str, str | bytes] | None = None,
        input_artifacts: Mapping[str, str | bytes] | None = None,
        metadata: Mapping[str, Any] | None = None,
        parent_attempt_id: str | None = None,
        requested_period: str | None = None,
    ) -> dict[str, Any]:
        """Create an attempt and durably archive all supplied inputs first."""
        if not strategy_name.strip():
            raise ValueError("strategy_name must not be empty")
        if not origin.strip():
            raise ValueError("origin must not be empty")
        _reject_holdout_period(requested_period)
        _reject_reserved_evidence_fields(configuration or {})
        _reject_reserved_evidence_fields(metadata or {})
        metadata_role = (metadata or {}).get("evidence_role", (metadata or {}).get("role"))
        if metadata_role is not None:
            _reject_holdout_period(str(metadata_role))
        self.assert_research_enabled()

        attempt_id = str(uuid.uuid4())
        key_label = re.sub(r"[^A-Za-z0-9._-]+", "_", strategy_name.strip()).strip("._-")
        attempt_key = f"{key_label or 'strategy'}@{attempt_id}"
        created_at = _now()
        archived: list[tuple[str, str, int]] = []

        if candidate_source is not None:
            content_hash = self._archive(candidate_source)
            archived.append(("candidate_source", content_hash, _byte_length(candidate_source)))
        else:
            content_hash = None

        config_hash = None
        if configuration is not None:
            safe_config = _redact(configuration)
            serialized_config = _json_bytes(safe_config)
            config_hash = self._archive(serialized_config)
            archived.append(("configuration", config_hash, len(serialized_config)))

        for name, contents in (source_snapshot or {}).items():
            safe_name = _safe_artifact_name(f"source_snapshot/{name}")
            artifact_hash = self._archive(contents)
            archived.append((safe_name, artifact_hash, _byte_length(contents)))
        for name, contents in (input_artifacts or {}).items():
            safe_name = _safe_artifact_name(name)
            if safe_name in {item[0] for item in archived}:
                raise ValueError(f"duplicate artifact name: {safe_name}")
            artifact_hash = self._archive(contents)
            archived.append((safe_name, artifact_hash, _byte_length(contents)))

        safe_metadata = _redact(metadata or {})
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO attempts
                   (attempt_id, attempt_key, strategy_name, origin, parent_attempt_id,
                    created_at, requested_period, source_hash, config_hash, evidence_role, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'development', ?)""",
                (
                    attempt_id,
                    attempt_key,
                    strategy_name.strip(),
                    origin.strip(),
                    parent_attempt_id,
                    created_at,
                    requested_period,
                    content_hash,
                    config_hash,
                    json.dumps(safe_metadata, sort_keys=True, separators=(",", ":")),
                ),
            )
            conn.executemany(
                """INSERT INTO attempt_artifacts
                   (attempt_id, name, content_hash, byte_length, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                [(attempt_id, name, digest, length, created_at) for name, digest, length in archived],
            )
            conn.execute(
                """INSERT INTO attempt_events
                   (event_id, attempt_id, event_type, stage, observed_at, payload_json)
                   VALUES (?, ?, 'attempt_started', 'proposal', ?, '{}')""",
                (str(uuid.uuid4()), attempt_id, created_at),
            )

        return self.get_attempt(attempt_id)  # type: ignore[return-value]

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        """Return one attempt by its durable identity, or ``None`` if absent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if row is None:
                return None
            result = self._attempt_dict(conn, row)
            evaluations = conn.execute(
                "SELECT * FROM evaluations WHERE attempt_id=? ORDER BY started_at, evaluation_id",
                (attempt_id,),
            ).fetchall()
            result["evaluations"] = []
            for evaluation in evaluations:
                outcomes = conn.execute(
                    """SELECT symbol, period, technical_outcome, performance_qualification,
                              selection_eligibility, metrics_json, reason, recorded_at
                       FROM evaluation_outcomes WHERE evaluation_id=? ORDER BY symbol""",
                    (evaluation["evaluation_id"],),
                ).fetchall()
                result["evaluations"].append({
                    "evaluation_id": evaluation["evaluation_id"],
                    "source_hash": evaluation["source_hash"],
                    "period": evaluation["period"],
                    "symbols": json.loads(evaluation["symbols_json"]),
                    "context": json.loads(evaluation["context_json"]),
                    "parent_evaluation_id": evaluation["parent_evaluation_id"],
                    "started_at": evaluation["started_at"],
                    "outcomes": [
                        {
                            "symbol": outcome["symbol"],
                            "period": outcome["period"],
                            "technical_outcome": outcome["technical_outcome"],
                            "performance_qualification": outcome["performance_qualification"],
                            "selection_eligibility": outcome["selection_eligibility"],
                            "metrics": json.loads(outcome["metrics_json"]) if outcome["metrics_json"] else None,
                            "reason": outcome["reason"],
                            "recorded_at": outcome["recorded_at"],
                        }
                        for outcome in outcomes
                    ],
                })
            result["invalidations"] = [dict(item) for item in conn.execute(
                """SELECT invalidation_id, evaluation_id, symbol, reason, created_at
                   FROM invalidations WHERE attempt_id=? ORDER BY created_at, invalidation_id""",
                (attempt_id,),
            ).fetchall()]
            return result

    def read_artifact(self, attempt_id: str, name: str) -> bytes:
        """Read an archived artifact after verifying its content hash."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content_hash FROM attempt_artifacts WHERE attempt_id=? AND name=?",
                (attempt_id, name),
            ).fetchone()
        if row is None:
            raise KeyError(f"artifact {name!r} is not recorded for attempt {attempt_id}")
        digest = row[0]
        contents = self._artifact_path(digest).read_bytes()
        if hashlib.sha256(contents).hexdigest() != digest:
            raise OSError(f"archived artifact failed integrity check: {digest}")
        return contents

    def record_artifact(self, attempt_id: str, name: str, contents: str | bytes) -> dict[str, Any]:
        """Append an immutable artifact reference to an existing attempt."""
        self.assert_research_enabled()
        safe_name = _safe_artifact_name(name)
        digest = self._archive(contents)
        recorded_at = _now()
        with self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone() is None:
                raise KeyError(f"unknown experiment attempt: {attempt_id}")
            conn.execute(
                """INSERT INTO attempt_artifacts
                   (attempt_id, name, content_hash, byte_length, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (attempt_id, safe_name, digest, _byte_length(contents), recorded_at),
            )
        return {
            "attempt_id": attempt_id,
            "name": safe_name,
            "content_hash": digest,
            "byte_length": _byte_length(contents),
            "created_at": recorded_at,
        }

    def record_event(
        self,
        attempt_id: str,
        *,
        event_type: str,
        stage: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append an observed lifecycle event; existing events are never edited."""
        if not event_type.strip() or not stage.strip():
            raise ValueError("event_type and stage must not be empty")
        if event_type != "generation_interrupted":
            self.assert_research_enabled()
        event_id = str(uuid.uuid4())
        observed_at = _now()
        safe_payload = _redact(payload or {})
        with self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone() is None:
                raise KeyError(f"unknown experiment attempt: {attempt_id}")
            payload_json = json.dumps(safe_payload, sort_keys=True, separators=(",", ":"))
            conn.execute(
                """INSERT INTO attempt_events
                   (event_id, attempt_id, event_type, stage, observed_at, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (event_id, attempt_id, event_type, stage, observed_at, payload_json),
            )
        return {
            "event_id": event_id,
            "attempt_id": attempt_id,
            "event_type": event_type,
            "stage": stage,
            "observed_at": observed_at,
            "payload": safe_payload,
        }

    def mark_latest_unfinished_generation(self, origin: str = "autonomous") -> str | None:
        """Mark the newest uncertain generation after restart and return its ID."""
        terminal_types = {"generation_completed", "generation_failed", "generation_interrupted"}
        with self._connect() as conn:
            attempts = conn.execute(
                "SELECT attempt_id FROM attempts WHERE origin=? ORDER BY created_at DESC, attempt_id DESC",
                (origin,),
            ).fetchall()
            for row in attempts:
                events = conn.execute(
                    "SELECT event_type FROM attempt_events WHERE attempt_id=?",
                    (row["attempt_id"],),
                ).fetchall()
                types = {event["event_type"] for event in events}
                if "generation_requested" not in types or types & terminal_types:
                    continue
                attempt_id = row["attempt_id"]
                break
            else:
                return None
        self.record_event(
            attempt_id,
            event_type="generation_interrupted",
            stage="generation",
            payload={
                "technical_outcome": "unknown",
                "performance_qualification": "not_measured",
                "reason": "process restart occurred before a terminal generation observation",
            },
        )
        return attempt_id

    def begin_evaluation(
        self,
        attempt_id: str,
        *,
        period: str,
        symbols: list[str],
        context: Mapping[str, Any] | None = None,
        parent_evaluation_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a distinct evaluation tied to the attempt's exact source."""
        if not period.strip():
            raise ValueError("period must not be empty")
        if not symbols:
            raise ValueError("an evaluation must name at least one symbol")
        _reject_holdout_period(period)
        _reject_reserved_evidence_fields(context or {})
        if period.casefold() not in {"train", "test"}:
            raise ValueError("ordinary evaluations must use train or exposed-validation period")
        self.assert_research_enabled()
        with self._connect() as conn:
            attempt = conn.execute(
                "SELECT source_hash FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise KeyError(f"unknown experiment attempt: {attempt_id}")
            if parent_evaluation_id is not None:
                parent = conn.execute(
                    "SELECT attempt_id, period FROM evaluations WHERE evaluation_id=?",
                    (parent_evaluation_id,),
                ).fetchone()
                if parent is None or parent["attempt_id"] != attempt_id or parent["period"] != period:
                    raise ValueError("parent evaluation must match the attempt and period")
            source_hash = attempt["source_hash"]
            if source_hash is None:
                source = conn.execute(
                    """SELECT content_hash FROM attempt_artifacts
                       WHERE attempt_id=? AND name='candidate_source'""",
                    (attempt_id,),
                ).fetchone()
                source_hash = source["content_hash"] if source else None
            if source_hash is None:
                raise ValueError("evaluation requires an archived candidate source")
            evaluation_id = str(uuid.uuid4())
            started_at = _now()
            safe_context = _redact(context or {})
            conn.execute(
                """INSERT INTO evaluations
                   (evaluation_id, attempt_id, source_hash, period, symbols_json,
                    context_json, parent_evaluation_id, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    evaluation_id,
                    attempt_id,
                    source_hash,
                    period,
                    json.dumps(list(dict.fromkeys(symbols)), separators=(",", ":")),
                    json.dumps(safe_context, sort_keys=True, separators=(",", ":")),
                    parent_evaluation_id,
                    started_at,
                ),
            )
            conn.execute(
                """INSERT INTO attempt_events
                   (event_id, attempt_id, event_type, stage, observed_at, payload_json)
                   VALUES (?, ?, 'evaluation_started', 'evaluation', ?, ?)""",
                (
                    str(uuid.uuid4()),
                    attempt_id,
                    started_at,
                    json.dumps(
                        {"evaluation_id": evaluation_id, "period": period, "symbols": list(dict.fromkeys(symbols))},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
        return self.get_evaluation(evaluation_id)  # type: ignore[return-value]

    def get_evaluation(self, evaluation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluations WHERE evaluation_id=?", (evaluation_id,)
            ).fetchone()
            if row is None:
                return None
            return {
                "evaluation_id": row["evaluation_id"],
                "attempt_id": row["attempt_id"],
                "source_hash": row["source_hash"],
                "period": row["period"],
                "symbols": json.loads(row["symbols_json"]),
                "context": json.loads(row["context_json"]),
                "parent_evaluation_id": row["parent_evaluation_id"],
                "started_at": row["started_at"],
            }

    def latest_evaluation(self, attempt_id: str, period: str) -> dict[str, Any] | None:
        """Return the newest immutable evaluation for one attempt and period."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT evaluation_id FROM evaluations
                   WHERE attempt_id=? AND period=?
                   ORDER BY started_at DESC, evaluation_id DESC LIMIT 1""",
                (attempt_id, period),
            ).fetchone()
        return self.get_evaluation(row["evaluation_id"]) if row else None

    def record_outcome(
        self,
        evaluation_id: str,
        *,
        symbol: str,
        period: str,
        technical_outcome: str,
        performance_qualification: str,
        selection_eligibility: str,
        metrics: Mapping[str, Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Append one symbol outcome without fabricating unmeasured metrics."""
        self.assert_research_enabled()
        with self._connect() as conn:
            evaluation = conn.execute(
                "SELECT attempt_id, period FROM evaluations WHERE evaluation_id=?",
                (evaluation_id,),
            ).fetchone()
            if evaluation is None:
                raise KeyError(f"unknown evaluation: {evaluation_id}")
            if period != evaluation["period"]:
                raise ValueError("outcome period must match its evaluation context")
            recorded_at = _now()
            metrics_json = (
                json.dumps(_redact(metrics), sort_keys=True, separators=(",", ":"))
                if metrics is not None
                else None
            )
            outcome_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO evaluation_outcomes
                   (outcome_id, evaluation_id, attempt_id, symbol, period,
                    technical_outcome, performance_qualification, selection_eligibility,
                    metrics_json, reason, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    outcome_id,
                    evaluation_id,
                    evaluation["attempt_id"],
                    symbol,
                    period,
                    technical_outcome,
                    performance_qualification,
                    selection_eligibility,
                    metrics_json,
                    reason,
                    recorded_at,
                ),
            )
            conn.execute(
                """INSERT INTO attempt_events
                   (event_id, attempt_id, event_type, stage, observed_at, payload_json)
                   VALUES (?, ?, 'symbol_outcome', ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    evaluation["attempt_id"],
                    f"{period}:{symbol}",
                    recorded_at,
                    json.dumps(
                        {
                            "evaluation_id": evaluation_id,
                            "outcome_id": outcome_id,
                            "technical_outcome": technical_outcome,
                            "performance_qualification": performance_qualification,
                            "selection_eligibility": selection_eligibility,
                            "reason": reason,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
        return {
            "outcome_id": outcome_id,
            "evaluation_id": evaluation_id,
            "attempt_id": evaluation["attempt_id"],
            "symbol": symbol,
            "period": period,
            "technical_outcome": technical_outcome,
            "performance_qualification": performance_qualification,
            "selection_eligibility": selection_eligibility,
            "metrics": dict(metrics) if metrics is not None else None,
            "reason": reason,
            "recorded_at": recorded_at,
        }

    def publish_current_results(self, evaluation_id: str) -> None:
        """Rebuild current results from committed evidence, without new measurements."""
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM evaluations WHERE evaluation_id=?", (evaluation_id,)).fetchone() is None:
                raise KeyError(f"unknown evaluation: {evaluation_id}")
        self.rebuild_current_results()

    def rebuild_current_results(self) -> dict[str, Any]:
        """Replay the newest committed evaluation per attempt/period into the current view."""
        published_at = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous_rows = {
                (row["attempt_id"], row["symbol"], row["period"]): dict(row)
                for row in conn.execute("SELECT * FROM current_results").fetchall()
            }
            conn.execute("DELETE FROM current_results")
            latest = conn.execute(
                """SELECT e.*, a.attempt_key, a.strategy_name, a.evidence_role
                   FROM evaluations e JOIN attempts a USING (attempt_id)
                   WHERE a.evidence_role != 'reserved_holdout'
                     AND e.evaluation_id=(
                       SELECT e2.evaluation_id FROM evaluations e2
                       WHERE e2.attempt_id=e.attempt_id AND e2.period=e.period
                       ORDER BY e2.started_at DESC, e2.evaluation_id DESC LIMIT 1
                     )
                   ORDER BY e.started_at, e.evaluation_id"""
            ).fetchall()
            for evaluation in latest:
                outcome_rows = conn.execute(
                    "SELECT * FROM evaluation_outcomes WHERE evaluation_id=? ORDER BY symbol",
                    (evaluation["evaluation_id"],),
                ).fetchall()
                outcome_by_symbol = {row["symbol"]: row for row in outcome_rows}
                for symbol in json.loads(evaluation["symbols_json"]):
                    outcome = outcome_by_symbol.get(symbol)
                    technical = outcome["technical_outcome"] if outcome else "not_observed"
                    qualification = outcome["performance_qualification"] if outcome else "not_measured"
                    eligibility = outcome["selection_eligibility"] if outcome else "unknown"
                    metrics_json = outcome["metrics_json"] if outcome else None
                    reason = outcome["reason"] if outcome else "no terminal symbol outcome was recorded"
                    invalidation = conn.execute(
                        """SELECT reason FROM invalidations
                           WHERE attempt_id=?
                             AND (evaluation_id IS NULL OR evaluation_id=?)
                             AND (symbol IS NULL OR symbol=?)
                           ORDER BY created_at DESC, invalidation_id DESC LIMIT 1""",
                        (evaluation["attempt_id"], evaluation["evaluation_id"], symbol),
                    ).fetchone()
                    invalidation_reason = invalidation["reason"] if invalidation else None
                    if invalidation_reason:
                        eligibility = "ineligible"
                    key = (evaluation["attempt_id"], symbol, evaluation["period"])
                    previous = previous_rows.get(key)
                    unchanged = previous is not None and all(
                        previous[column] == value
                        for column, value in (
                            ("evaluation_id", evaluation["evaluation_id"]),
                            ("source_hash", evaluation["source_hash"]),
                            ("technical_outcome", technical),
                            ("performance_qualification", qualification),
                            ("selection_eligibility", eligibility),
                            ("metrics_json", metrics_json),
                            ("reason", reason),
                            ("invalidation_reason", invalidation_reason),
                        )
                    )
                    conn.execute(
                        """INSERT INTO current_results
                           (attempt_id, strategy_key, strategy_name, evaluation_id, source_hash,
                            symbol, period, technical_outcome, performance_qualification,
                            selection_eligibility, metrics_json, reason, invalidation_reason, published_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            evaluation["attempt_id"], evaluation["attempt_key"], evaluation["strategy_name"],
                            evaluation["evaluation_id"], evaluation["source_hash"], symbol, evaluation["period"],
                            technical, qualification, eligibility, metrics_json, reason, invalidation_reason,
                            previous["published_at"] if unchanged else published_at,
                        ),
                    )
            revisions = conn.execute(
                "SELECT current_revision FROM lifecycle_state WHERE id=1"
            ).fetchone()
            current_revision = int(revisions["current_revision"])
            conn.execute(
                "UPDATE lifecycle_state SET view_revision=? WHERE id=1", (current_revision,)
            )
        return {"view_revision": current_revision, "published_at": published_at}

    def export_current_tsv(self, path: str | Path) -> dict[str, Any]:
        """Atomically export the coherent current view in the legacy TSV schema."""
        self.require_fresh_view()
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        columns = [
            "commit", "strategy", "symbol", "sharpe", "return_pct", "cagr_pct", "max_dd_pct",
            "win_rate", "profit_factor", "trades", "sortino", "calmar", "status", "description", "period",
        ]
        rows = self._tsv_rows()
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=columns, delimiter="\t", lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        contents = buffer.getvalue().encode("utf-8")
        digest = hashlib.sha256(contents).hexdigest()
        fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as temporary:
                temporary.write(contents)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, destination)
            try:
                directory_fd = os.open(destination.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except (AttributeError, OSError):
                pass
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        with self._connect() as conn:
            current = conn.execute("SELECT current_revision, view_revision FROM lifecycle_state WHERE id=1").fetchone()
            if current["current_revision"] != current["view_revision"]:
                raise StaleCurrentViewError("current view changed while exporting; repeat export after rebuild")
            conn.execute(
                """UPDATE lifecycle_state SET export_revision=?, export_sha256=?, export_path=? WHERE id=1""",
                (current["view_revision"], digest, str(destination.resolve())),
            )
        return {"path": str(destination), "sha256": digest, "revision": current["view_revision"], "row_count": len(rows)}

    def export_freshness(self, path: str | Path) -> dict[str, Any]:
        destination = Path(path)
        with self._connect() as conn:
            state = conn.execute("SELECT * FROM lifecycle_state WHERE id=1").fetchone()
        expected_hash = state["export_sha256"]
        actual_hash = None
        if destination.is_file():
            actual_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
        fresh = (
            int(state["current_revision"]) == int(state["view_revision"])
            and int(state["export_revision"]) == int(state["view_revision"])
            and state["export_path"] == str(destination.resolve())
            and expected_hash is not None
            and actual_hash == expected_hash
        )
        return {
            "fresh": fresh,
            "current_revision": int(state["current_revision"]),
            "view_revision": int(state["view_revision"]),
            "export_revision": int(state["export_revision"]),
            "expected_sha256": expected_hash,
            "actual_sha256": actual_hash,
        }

    def require_fresh_view(self) -> None:
        with self._connect() as conn:
            state = conn.execute("SELECT current_revision, view_revision FROM lifecycle_state WHERE id=1").fetchone()
        if int(state["current_revision"]) != int(state["view_revision"]):
            raise StaleCurrentViewError("current result view is stale; rebuild it before making decisions")

    def require_fresh_export(self, path: str | Path) -> None:
        self.require_fresh_view()
        if not self.export_freshness(path)["fresh"]:
            raise StaleCurrentViewError("current-result TSV export is stale or corrupt; export it again before use")

    def _tsv_rows(self) -> list[dict[str, Any]]:
        rows = []
        for current in self.current_results():
            metrics = current["metrics"] or {}
            technical = current["technical_outcome"]
            if technical == "completed":
                status = "keep" if current["selection_eligibility"] == "eligible" else "discard"
            elif technical in {"error", "timeout", "crash", "failed"}:
                status = "crash"
            else:
                status = "discard"
            rows.append({
                "commit": "",
                "strategy": current["strategy_key"],
                "symbol": current["symbol"],
                "sharpe": _tsv_number(metrics.get("sharpe_ratio")),
                "return_pct": _tsv_number(metrics.get("total_return_pct")),
                "cagr_pct": _tsv_number(metrics.get("annual_return_pct")),
                "max_dd_pct": _tsv_number(metrics.get("max_drawdown_pct")),
                "win_rate": _tsv_number(metrics.get("win_rate")),
                "profit_factor": _tsv_number(metrics.get("profit_factor")),
                "trades": _tsv_number(metrics.get("num_trades")),
                "sortino": _tsv_number(metrics.get("sortino_ratio")),
                "calmar": _tsv_number(metrics.get("calmar_ratio")),
                "status": status,
                "description": current["reason"] or current["invalidation_reason"] or "",
                "period": current["period"],
            })
        rows.extend(self._legacy_tsv_rows())
        return sorted(rows, key=lambda row: (row["strategy"], row["period"], row["symbol"]))

    def _legacy_tsv_rows(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            try:
                found = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='legacy_results'").fetchone()
            except sqlite3.Error:
                return []
            if not found:
                return []
            rows = conn.execute(
                "SELECT * FROM legacy_results WHERE evidence_role != 'reserved_holdout' ORDER BY strategy, period, symbol"
            ).fetchall()
        return [dict(row) for row in rows]

    def enter_maintenance(self, reason: str, maintenance_id: str | None = None) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("maintenance reason must not be empty")
        started_at = _now()
        maintenance_id = maintenance_id or str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                """UPDATE maintenance_state SET state='active', maintenance_id=?, reason=?, started_at=?, ended_at=NULL
                   WHERE id=1""",
                (maintenance_id, reason.strip(), started_at),
            )
        return self.maintenance_status()

    def exit_maintenance(self) -> dict[str, Any]:
        ended_at = _now()
        with self._connect() as conn:
            state = conn.execute("SELECT state FROM maintenance_state WHERE id=1").fetchone()
            if state["state"] != "active":
                raise RuntimeError("maintenance is not active")
            writers = conn.execute("SELECT COUNT(*) FROM writer_sessions").fetchone()[0]
            if writers:
                raise RuntimeError(f"cannot exit maintenance while {writers} lifecycle writer session(s) remain active")
            conn.execute("UPDATE maintenance_state SET state='inactive', ended_at=? WHERE id=1", (ended_at,))
        return self.maintenance_status()

    def maintenance_status(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM maintenance_state WHERE id=1").fetchone()
            writers = conn.execute(
                "SELECT writer_id, writer, pid, started_at FROM writer_sessions ORDER BY started_at"
            ).fetchall()
        return {**dict(row), "active_writers": [dict(item) for item in writers], "quiescent": not writers}

    @contextmanager
    def writer_session(self, writer: str):
        """Register an active writer so maintenance can wait for safe boundaries."""
        writer_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = conn.execute("SELECT state FROM maintenance_state WHERE id=1").fetchone()
            if state["state"] == "active":
                raise RuntimeError("research is paused while maintenance is active")
            conn.execute(
                "INSERT INTO writer_sessions (writer_id, writer, pid, started_at) VALUES (?, ?, ?, ?)",
                (writer_id, writer, os.getpid(), _now()),
            )
        try:
            from research_maintenance import assert_research_enabled
            assert_research_enabled()
            yield
        finally:
            with self._connect() as conn:
                conn.execute("DELETE FROM writer_sessions WHERE writer_id=?", (writer_id,))

    def recover_stale_writer_sessions(self) -> list[str]:
        """Remove leases whose owning process has exited; active maintenance stays active."""
        with self._connect() as conn:
            rows = conn.execute("SELECT writer_id, pid FROM writer_sessions").fetchall()
        stale = []
        for row in rows:
            try:
                os.kill(int(row["pid"]), 0)
            except ProcessLookupError:
                stale.append(row["writer_id"])
            except PermissionError:
                continue
        if stale:
            with self._connect() as conn:
                conn.executemany("DELETE FROM writer_sessions WHERE writer_id=?", [(item,) for item in stale])
        return stale

    def import_legacy_baseline(
        self, *, baseline_id: str, manifest_hash: str, rows: list[Mapping[str, Any]], manifest: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Import a verified TSV snapshot while keeping historical provenance unknown."""
        if not baseline_id.strip() or not manifest_hash.strip():
            raise ValueError("baseline identity and manifest hash are required")
        self.assert_maintenance_operation_allowed("baseline_import")
        required = {
            "commit", "strategy", "symbol", "sharpe", "return_pct", "cagr_pct", "max_dd_pct",
            "win_rate", "profit_factor", "trades", "sortino", "calmar", "status", "description", "period",
        }
        normalized = []
        for row in rows:
            missing = required - set(row)
            if missing:
                raise ValueError(f"legacy row is missing columns: {sorted(missing)}")
            normalized.append({key: "" if row[key] is None else str(row[key]) for key in required})
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT manifest_hash, row_count FROM legacy_baselines WHERE baseline_id=?", (baseline_id,)
            ).fetchone()
            if existing:
                if existing["manifest_hash"] != manifest_hash or existing["row_count"] != len(normalized):
                    raise ValueError("baseline ID already exists with different content")
                return {"baseline_id": baseline_id, "row_count": len(normalized), "imported": False}
            conn.execute(
                """INSERT INTO legacy_baselines (baseline_id, manifest_hash, row_count, manifest_json, imported_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (baseline_id, manifest_hash, len(normalized), json.dumps(_redact(manifest), sort_keys=True), _now()),
            )
            for row in normalized:
                key = (row["strategy"], row["symbol"], row["period"])
                previous = conn.execute(
                    "SELECT * FROM legacy_results WHERE strategy=? AND symbol=? AND period=?", key
                ).fetchone()
                if previous:
                    if any(previous[column] != row[column] for column in required - {"strategy", "symbol", "period"}):
                        raise ValueError(f"conflicting legacy baseline row for {key}")
                    continue
                conn.execute(
                    """INSERT INTO legacy_results
                       (baseline_id, "commit", strategy, symbol, sharpe, return_pct, cagr_pct, max_dd_pct,
                        win_rate, profit_factor, trades, sortino, calmar, status, description, period,
                        provenance_status, evidence_role)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?)""",
                    (
                        baseline_id, row["commit"], row["strategy"], row["symbol"], row["sharpe"],
                        row["return_pct"], row["cagr_pct"], row["max_dd_pct"], row["win_rate"],
                        row["profit_factor"], row["trades"], row["sortino"], row["calmar"],
                        row["status"], row["description"], row["period"],
                        _legacy_evidence_role(row["period"]),
                    ),
                )
            count = conn.execute("SELECT COUNT(*) FROM legacy_results").fetchone()[0]
            conn.execute(
                "UPDATE lifecycle_state SET current_revision=current_revision+1 WHERE id=1"
            )
        return {"baseline_id": baseline_id, "row_count": count, "imported": True}

    def legacy_results(self) -> list[dict[str, Any]]:
        """Read nonreserved historical feedback without claiming reconstructed attempts."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM legacy_results WHERE evidence_role != 'reserved_holdout' ORDER BY strategy, period, symbol"
            ).fetchall()
        return [dict(row) for row in rows]

    def legacy_archive_rows(self) -> list[dict[str, Any]]:
        """Read the preserved archive for audit; reserved rows must not guide research."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM legacy_results ORDER BY strategy, period, symbol").fetchall()
        return [dict(row) for row in rows]

    def legacy_baselines(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT baseline_id, manifest_hash, row_count, manifest_json, imported_at FROM legacy_baselines ORDER BY imported_at"
            ).fetchall()
        return [
            {
                **{key: row[key] for key in ("baseline_id", "manifest_hash", "row_count", "imported_at")},
                "manifest": json.loads(row["manifest_json"]),
            }
            for row in rows
        ]

    def verify_evidence_invariants(self) -> dict[str, Any]:
        """Check ledger integrity and every referenced content-addressed artifact."""
        failures: list[str] = []
        checked_artifacts = 0
        with self._connect() as conn:
            foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                failures.append(f"foreign-key violations: {len(foreign_key_errors)}")
            refs = conn.execute(
                "SELECT attempt_id, name, content_hash FROM attempt_artifacts ORDER BY attempt_id, name"
            ).fetchall()
            attempts = conn.execute("SELECT attempt_id, source_hash FROM attempts").fetchall()
        for ref in refs:
            try:
                contents = self.read_artifact(ref["attempt_id"], ref["name"])
                if hashlib.sha256(contents).hexdigest() != ref["content_hash"]:
                    failures.append(f"artifact hash mismatch: {ref['attempt_id']}:{ref['name']}")
                checked_artifacts += 1
            except (KeyError, OSError) as exc:
                failures.append(f"artifact unavailable: {ref['attempt_id']}:{ref['name']}: {exc}")
        for attempt in attempts:
            if attempt["source_hash"] is None:
                continue
            try:
                source = self.read_artifact(attempt["attempt_id"], "candidate_source")
            except (KeyError, OSError):
                failures.append(f"candidate source unavailable: {attempt['attempt_id']}")
                continue
            if hashlib.sha256(source).hexdigest() != attempt["source_hash"]:
                failures.append(f"candidate source hash mismatch: {attempt['attempt_id']}")
        return {
            "ready": not failures,
            "checked_artifacts": checked_artifacts,
            "checked_attempts": len(attempts),
            "failures": failures,
        }

    def record_cutover_rehearsal(self, *, cutover_id: str, preflight: Mapping[str, Any]) -> dict[str, Any]:
        """Record a fixture rehearsal; this method cannot activate production writers."""
        self.assert_maintenance_operation_allowed("cutover_preflight")
        if not cutover_id.strip() or not preflight.get("ready"):
            raise ValueError("a ready preflight and cutover rehearsal identity are required")
        if self.maintenance_status()["state"] != "active":
            raise RuntimeError("cutover rehearsal records require active maintenance")
        recorded_at = _now()
        with self._connect() as conn:
            state = conn.execute("SELECT current_revision, view_revision, export_sha256 FROM lifecycle_state WHERE id=1").fetchone()
            baseline_id = preflight.get("baseline_id")
            conn.execute(
                """INSERT INTO cutover_records (cutover_id, mode, baseline_id, recorded_at,
                       ledger_revision, view_revision, export_sha256, report_json)
                   VALUES (?, 'rehearsal', ?, ?, ?, ?, ?, ?)""",
                (
                    cutover_id, baseline_id, recorded_at, state["current_revision"], state["view_revision"],
                    state["export_sha256"], json.dumps(_redact(preflight), sort_keys=True),
                ),
            )
        return {"cutover_id": cutover_id, "mode": "rehearsal", "recorded_at": recorded_at}

    def assert_maintenance_operation_allowed(self, operation: str) -> None:
        """Allow only named recovery/baseline operations while maintenance is active."""
        with self._connect() as conn:
            row = conn.execute("SELECT state, reason FROM maintenance_state WHERE id=1").fetchone()
        if row["state"] == "active" and operation not in {"baseline_capture", "baseline_import", "recovery", "publication", "cutover_preflight"}:
            raise RuntimeError(f"operation {operation!r} is unavailable during maintenance")

    def assert_research_writable(self) -> None:
        self.assert_research_enabled()
        with self._connect() as conn:
            revisions = conn.execute("SELECT current_revision, view_revision FROM lifecycle_state WHERE id=1").fetchone()
        if revisions["current_revision"] != revisions["view_revision"]:
            raise StaleCurrentViewError("research is paused because the current result view is stale")

    def assert_research_enabled(self) -> None:
        from research_maintenance import assert_research_enabled
        assert_research_enabled()
        with self._connect() as conn:
            state = conn.execute("SELECT state FROM maintenance_state WHERE id=1").fetchone()
        if state["state"] == "active":
            raise RuntimeError("research is paused while maintenance is active")

    def current_results(
        self,
        *,
        attempt_id: str | None = None,
        selection_eligible_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Return the current projection while retaining full attempt identity."""
        query = "SELECT cr.* FROM current_results cr JOIN attempts a USING (attempt_id) WHERE a.evidence_role != 'reserved_holdout'"
        params: list[Any] = []
        if attempt_id is not None:
            query += " AND cr.attempt_id=?"
            params.append(attempt_id)
        if selection_eligible_only:
            query += " AND cr.selection_eligibility='eligible'"
        query += " ORDER BY cr.strategy_key, cr.period, cr.symbol"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "attempt_id": row["attempt_id"],
                "strategy_key": row["strategy_key"],
                "strategy_name": row["strategy_name"],
                "evaluation_id": row["evaluation_id"],
                "source_hash": row["source_hash"],
                "symbol": row["symbol"],
                "period": row["period"],
                "technical_outcome": row["technical_outcome"],
                "performance_qualification": row["performance_qualification"],
                "selection_eligibility": row["selection_eligibility"],
                "metrics": json.loads(row["metrics_json"]) if row["metrics_json"] else None,
                "reason": row["reason"],
                "invalidation_reason": row["invalidation_reason"],
                "published_at": row["published_at"],
            }
            for row in rows
        ]

    def invalidate(
        self,
        *,
        attempt_id: str,
        reason: str,
        evaluation_id: str | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """Append a scoped invalidation and immediately exclude its view rows."""
        if not reason.strip():
            raise ValueError("invalidation reason must not be empty")
        self.assert_research_enabled()
        invalidation_id = str(uuid.uuid4())
        created_at = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone() is None:
                raise KeyError(f"unknown experiment attempt: {attempt_id}")
            before_revision = conn.execute(
                "SELECT current_revision, view_revision FROM lifecycle_state WHERE id=1"
            ).fetchone()
            if evaluation_id is not None and conn.execute(
                "SELECT 1 FROM evaluations WHERE evaluation_id=? AND attempt_id=?",
                (evaluation_id, attempt_id),
            ).fetchone() is None:
                raise ValueError("evaluation scope does not belong to the specified attempt")
            conn.execute(
                """INSERT INTO invalidations
                   (invalidation_id, attempt_id, evaluation_id, symbol, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (invalidation_id, attempt_id, evaluation_id, symbol, reason.strip(), created_at),
            )
            where = ["attempt_id=?"]
            params: list[Any] = [reason.strip(), attempt_id]
            if evaluation_id is not None:
                where.append("evaluation_id=?")
                params.append(evaluation_id)
            if symbol is not None:
                where.append("symbol=?")
                params.append(symbol)
            conn.execute(
                "UPDATE current_results SET selection_eligibility='ineligible', invalidation_reason=? WHERE "
                + " AND ".join(where),
                params,
            )
            conn.execute(
                """INSERT INTO attempt_events
                   (event_id, attempt_id, event_type, stage, observed_at, payload_json)
                   VALUES (?, ?, 'invalidated', 'selection', ?, ?)""",
                (
                    str(uuid.uuid4()),
                    attempt_id,
                    created_at,
                    json.dumps(
                        {
                            "invalidation_id": invalidation_id,
                            "evaluation_id": evaluation_id,
                            "symbol": symbol,
                            "reason": reason.strip(),
                            "selection_eligibility": "ineligible",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            if before_revision["current_revision"] == before_revision["view_revision"]:
                conn.execute("UPDATE lifecycle_state SET view_revision=current_revision WHERE id=1")
        return {
            "invalidation_id": invalidation_id,
            "attempt_id": attempt_id,
            "evaluation_id": evaluation_id,
            "symbol": symbol,
            "reason": reason.strip(),
            "created_at": created_at,
        }

    def _initialize_ledger(self) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY,
                    attempt_key TEXT NOT NULL UNIQUE,
                    strategy_name TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    parent_attempt_id TEXT,
                    created_at TEXT NOT NULL,
                    requested_period TEXT,
                    source_hash TEXT,
                    config_hash TEXT,
                    evidence_role TEXT NOT NULL DEFAULT 'development',
                    metadata_json TEXT NOT NULL,
                    FOREIGN KEY(parent_attempt_id) REFERENCES attempts(attempt_id)
                );
                CREATE TABLE IF NOT EXISTS attempt_artifacts (
                    attempt_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    byte_length INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(attempt_id, name),
                    FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id)
                );
                CREATE TABLE IF NOT EXISTS attempt_events (
                    event_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id)
                );
                CREATE TABLE IF NOT EXISTS evaluations (
                    evaluation_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    source_hash TEXT,
                    period TEXT NOT NULL,
                    symbols_json TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    parent_evaluation_id TEXT,
                    started_at TEXT NOT NULL,
                    FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id),
                    FOREIGN KEY(parent_evaluation_id) REFERENCES evaluations(evaluation_id)
                );
                CREATE TABLE IF NOT EXISTS evaluation_outcomes (
                    outcome_id TEXT PRIMARY KEY,
                    evaluation_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    period TEXT NOT NULL,
                    technical_outcome TEXT NOT NULL,
                    performance_qualification TEXT NOT NULL,
                    selection_eligibility TEXT NOT NULL,
                    metrics_json TEXT,
                    reason TEXT,
                    recorded_at TEXT NOT NULL,
                    UNIQUE(evaluation_id, symbol, period),
                    FOREIGN KEY(evaluation_id) REFERENCES evaluations(evaluation_id),
                    FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id)
                );
                CREATE TABLE IF NOT EXISTS current_results (
                    attempt_id TEXT NOT NULL,
                    strategy_key TEXT NOT NULL,
                    strategy_name TEXT NOT NULL,
                    evaluation_id TEXT NOT NULL,
                    source_hash TEXT,
                    symbol TEXT NOT NULL,
                    period TEXT NOT NULL,
                    technical_outcome TEXT NOT NULL,
                    performance_qualification TEXT NOT NULL,
                    selection_eligibility TEXT NOT NULL,
                    metrics_json TEXT,
                    reason TEXT,
                    invalidation_reason TEXT,
                    published_at TEXT NOT NULL,
                    PRIMARY KEY(attempt_id, symbol, period),
                    FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id),
                    FOREIGN KEY(evaluation_id) REFERENCES evaluations(evaluation_id)
                );
                CREATE TABLE IF NOT EXISTS invalidations (
                    invalidation_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    evaluation_id TEXT,
                    symbol TEXT,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id),
                    FOREIGN KEY(evaluation_id) REFERENCES evaluations(evaluation_id)
                );
                CREATE TABLE IF NOT EXISTS lifecycle_state (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    current_revision INTEGER NOT NULL DEFAULT 0,
                    view_revision INTEGER NOT NULL DEFAULT 0,
                    export_revision INTEGER NOT NULL DEFAULT -1,
                    export_sha256 TEXT,
                    export_path TEXT
                );
                INSERT OR IGNORE INTO lifecycle_state (id) VALUES (1);
                CREATE TABLE IF NOT EXISTS maintenance_state (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    state TEXT NOT NULL DEFAULT 'inactive',
                    maintenance_id TEXT,
                    reason TEXT,
                    started_at TEXT,
                    ended_at TEXT
                );
                INSERT OR IGNORE INTO maintenance_state (id) VALUES (1);
                CREATE TABLE IF NOT EXISTS writer_sessions (
                    writer_id TEXT PRIMARY KEY,
                    writer TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    started_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS legacy_baselines (
                    baseline_id TEXT PRIMARY KEY,
                    manifest_hash TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    manifest_json TEXT NOT NULL,
                    imported_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS legacy_results (
                    baseline_id TEXT NOT NULL,
                    "commit" TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    sharpe TEXT NOT NULL,
                    return_pct TEXT NOT NULL,
                    cagr_pct TEXT NOT NULL,
                    max_dd_pct TEXT NOT NULL,
                    win_rate TEXT NOT NULL,
                    profit_factor TEXT NOT NULL,
                    trades TEXT NOT NULL,
                    sortino TEXT NOT NULL,
                    calmar TEXT NOT NULL,
                    status TEXT NOT NULL,
                    description TEXT NOT NULL,
                    period TEXT NOT NULL,
                    provenance_status TEXT NOT NULL,
                    evidence_role TEXT NOT NULL,
                    PRIMARY KEY(strategy, symbol, period),
                    FOREIGN KEY(baseline_id) REFERENCES legacy_baselines(baseline_id)
                );
                CREATE TABLE IF NOT EXISTS cutover_records (
                    cutover_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    baseline_id TEXT,
                    recorded_at TEXT NOT NULL,
                    ledger_revision INTEGER NOT NULL,
                    view_revision INTEGER NOT NULL,
                    export_sha256 TEXT,
                    report_json TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS evaluation_outcomes_bump_current_revision
                AFTER INSERT ON evaluation_outcomes BEGIN
                    UPDATE lifecycle_state SET current_revision=current_revision+1 WHERE id=1;
                END;
                CREATE TRIGGER IF NOT EXISTS invalidations_bump_current_revision
                AFTER INSERT ON invalidations BEGIN
                    UPDATE lifecycle_state SET current_revision=current_revision+1 WHERE id=1;
                END;
                CREATE TRIGGER IF NOT EXISTS attempts_are_immutable_update
                BEFORE UPDATE ON attempts BEGIN
                    SELECT RAISE(ABORT, 'attempt records are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS attempts_are_immutable_delete
                BEFORE DELETE ON attempts BEGIN
                    SELECT RAISE(ABORT, 'attempt records are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS attempt_artifacts_are_immutable_update
                BEFORE UPDATE ON attempt_artifacts BEGIN
                    SELECT RAISE(ABORT, 'artifact references are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS attempt_artifacts_are_immutable_delete
                BEFORE DELETE ON attempt_artifacts BEGIN
                    SELECT RAISE(ABORT, 'artifact references are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS attempt_events_are_immutable_update
                BEFORE UPDATE ON attempt_events BEGIN
                    SELECT RAISE(ABORT, 'attempt events are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS attempt_events_are_immutable_delete
                BEFORE DELETE ON attempt_events BEGIN
                    SELECT RAISE(ABORT, 'attempt events are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS evaluations_are_immutable_update
                BEFORE UPDATE ON evaluations BEGIN
                    SELECT RAISE(ABORT, 'evaluation records are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS evaluations_are_immutable_delete
                BEFORE DELETE ON evaluations BEGIN
                    SELECT RAISE(ABORT, 'evaluation records are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS evaluation_outcomes_are_immutable_update
                BEFORE UPDATE ON evaluation_outcomes BEGIN
                    SELECT RAISE(ABORT, 'evaluation outcomes are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS evaluation_outcomes_are_immutable_delete
                BEFORE DELETE ON evaluation_outcomes BEGIN
                    SELECT RAISE(ABORT, 'evaluation outcomes are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS invalidations_are_immutable_update
                BEFORE UPDATE ON invalidations BEGIN
                    SELECT RAISE(ABORT, 'invalidation records are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS invalidations_are_immutable_delete
                BEFORE DELETE ON invalidations BEGIN
                    SELECT RAISE(ABORT, 'invalidation records are immutable');
                END;
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(current_results)")}
            if "invalidation_reason" not in columns:
                conn.execute("ALTER TABLE current_results ADD COLUMN invalidation_reason TEXT")
            attempt_columns = {row[1] for row in conn.execute("PRAGMA table_info(attempts)")}
            if "evidence_role" not in attempt_columns:
                conn.execute("ALTER TABLE attempts ADD COLUMN evidence_role TEXT NOT NULL DEFAULT 'development'")

    @contextmanager
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.ledger_path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _archive(self, contents: str | bytes) -> str:
        data = contents.encode("utf-8") if isinstance(contents, str) else bytes(contents)
        digest = hashlib.sha256(data).hexdigest()
        destination = self._artifact_path(digest)
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise OSError(f"content-addressed artifact is corrupt: {destination}")
            return digest

        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".artifact-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as artifact_file:
                artifact_file.write(data)
                artifact_file.flush()
                os.fsync(artifact_file.fileno())
            try:
                os.link(temporary_name, destination)
            except FileExistsError:
                if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                    raise OSError(f"content-addressed artifact is corrupt: {destination}")
            return digest
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    def _artifact_path(self, content_hash: str) -> Path:
        return self.artifact_dir / content_hash[:2] / content_hash

    @staticmethod
    def _attempt_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        artifacts = conn.execute(
            """SELECT name, content_hash, byte_length, created_at
               FROM attempt_artifacts WHERE attempt_id=? ORDER BY name""",
            (row["attempt_id"],),
        ).fetchall()
        events = conn.execute(
            """SELECT event_id, event_type, stage, observed_at, payload_json
               FROM attempt_events WHERE attempt_id=? ORDER BY observed_at, event_id""",
            (row["attempt_id"],),
        ).fetchall()
        source_hash = row["source_hash"]
        if source_hash is None:
            source = conn.execute(
                """SELECT content_hash FROM attempt_artifacts
                   WHERE attempt_id=? AND name='candidate_source'""",
                (row["attempt_id"],),
            ).fetchone()
            source_hash = source["content_hash"] if source else None
        display_strategy_name = row["strategy_name"]
        for event in reversed(events):
            if event["event_type"] != "candidate_named":
                continue
            payload = json.loads(event["payload_json"])
            display_strategy_name = payload.get("strategy_name") or display_strategy_name
            break
        return {
            "attempt_id": row["attempt_id"],
            "attempt_key": row["attempt_key"],
            "strategy_name": row["strategy_name"],
            "display_strategy_name": display_strategy_name,
            "origin": row["origin"],
            "evidence_role": row["evidence_role"] if "evidence_role" in row.keys() else "development",
            "parent_attempt_id": row["parent_attempt_id"],
            "created_at": row["created_at"],
            "requested_period": row["requested_period"],
            "source_hash": source_hash,
            "config_hash": row["config_hash"],
            "metadata": json.loads(row["metadata_json"]),
            "artifacts": [dict(item) for item in artifacts],
            "events": [
                {
                    "event_id": item["event_id"],
                    "event_type": item["event_type"],
                    "stage": item["stage"],
                    "observed_at": item["observed_at"],
                    "payload": json.loads(item["payload_json"]),
                }
                for item in events
            ],
        }


_SECRET_KEY_PARTS = (
    "secret",
    "password",
    "token",
    "credential",
    "authorization",
    "apikey",
    "privatekey",
)


def _redact(value: Any, key: str = "") -> Any:
    compact_key = re.sub(r"[^a-z0-9]", "", key.lower())
    if any(part in compact_key for part in _SECRET_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _byte_length(value: str | bytes) -> int:
    return len(value.encode("utf-8")) if isinstance(value, str) else len(value)


def _safe_artifact_name(name: str) -> str:
    normalized = name.replace("\\", "/").strip("/")
    if not normalized or any(part in ("", ".", "..") for part in normalized.split("/")):
        raise ValueError(f"invalid artifact name: {name!r}")
    return normalized


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class StaleCurrentViewError(RuntimeError):
    """A consumer tried to use a result view that must be rebuilt or re-exported."""


def _reject_holdout_period(period: str | None) -> None:
    if period is None:
        return
    normalized = re.sub(r"[^a-z0-9]", "", period.casefold())
    if normalized in {"holdout", "finalholdout", "reservedholdout", "finaltest", "blindtest"}:
        raise ValueError("final holdout evidence is reserved and unavailable to ordinary research")


def _reject_reserved_evidence_fields(value: Any, key: str = "") -> None:
    normalized_key = re.sub(r"[^a-z0-9]", "", key.casefold())
    if isinstance(value, Mapping):
        for child_key, child_value in value.items():
            _reject_reserved_evidence_fields(child_value, str(child_key))
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _reject_reserved_evidence_fields(child, key)
        return
    if isinstance(value, str) and any(part in normalized_key for part in ("period", "role", "partition", "dataset")):
        _reject_holdout_period(value)


def _legacy_evidence_role(period: str) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", period.casefold())
    if normalized in {"holdout", "finalholdout", "reservedholdout", "finaltest", "blindtest"}:
        return "reserved_holdout"
    return "exposed_validation" if normalized == "test" else "development"


def _tsv_number(value: Any) -> Any:
    return "" if value is None else value
