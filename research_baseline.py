"""Capture, restore-check, and import an explicit legacy evidence baseline."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research_lifecycle import ResearchLifecycle


_TSV_COLUMNS = (
    "commit", "strategy", "symbol", "sharpe", "return_pct", "cagr_pct", "max_dd_pct",
    "win_rate", "profit_factor", "trades", "sortino", "calmar", "status", "description", "period",
)
_SECRET_NAMES = {".env", "credentials", "secrets", "id_rsa", "id_ed25519"}


def capture_legacy_baseline(
    *, lifecycle: ResearchLifecycle, legacy_db: Path, legacy_tsv: Path,
    artifact_paths: list[Path], snapshot_root: Path,
) -> dict[str, Any]:
    """Take a consistent SQLite backup plus byte-verified copies of named artifacts."""
    _require_maintenance(lifecycle)
    if not legacy_db.is_file() or not legacy_tsv.is_file():
        raise FileNotFoundError("both the legacy database and TSV must exist before baseline capture")
    for path in artifact_paths:
        if not path.exists():
            raise FileNotFoundError(f"baseline artifact does not exist: {path}")

    snapshot_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".baseline-", dir=snapshot_root))
    try:
        _sqlite_backup(legacy_db, staging / "results.db")
        shutil.copyfile(legacy_tsv, staging / "results.tsv")
        files = [_manifest_file(staging, staging / "results.db"), _manifest_file(staging, staging / "results.tsv")]
        skipped: list[str] = []
        artifact_root = staging / "artifacts"
        for index, source in enumerate(artifact_paths):
            base = artifact_root / f"{index:03d}-{_safe_name(source.name)}"
            if source.is_dir():
                for item in sorted(source.rglob("*")):
                    if item.is_symlink():
                        skipped.append(f"{source.name}/{item.relative_to(source).as_posix()} (symlink)")
                        continue
                    if not item.is_file():
                        continue
                    if _is_secret(item.name):
                        skipped.append(f"{source.name}/{item.relative_to(source).as_posix()} (sensitive filename)")
                        continue
                    target = base / item.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(item, target)
                    files.append(_manifest_file(staging, target))
            elif source.is_file():
                if _is_secret(source.name):
                    raise ValueError(f"refusing to snapshot sensitive-looking artifact: {source.name}")
                base.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, base)
                files.append(_manifest_file(staging, base))

        identity_bytes = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
        baseline_id = hashlib.sha256(identity_bytes).hexdigest()
        manifest = {
            "baseline_id": baseline_id,
            "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "provenance_status": "unknown_for_historical_attempts",
            "attempt_history_reconstructed": False,
            "files": files,
            "skipped_artifacts": skipped,
        }
        manifest_path = staging / "manifest.json"
        _write_json_fsync(manifest_path, manifest)
        manifest_hash = _sha256(manifest_path)
        destination = snapshot_root / baseline_id
        if destination.exists():
            existing = verify_baseline(destination)
            manifest_hash = existing["manifest_hash"]
            shutil.rmtree(staging)
        else:
            os.replace(staging, destination)
        return {
            "baseline_id": baseline_id,
            "manifest_hash": manifest_hash,
            "snapshot_dir": str(destination),
            "files": len(files),
            "skipped_artifacts": skipped,
        }
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def verify_baseline(snapshot_dir: Path, restore_target: Path | None = None) -> dict[str, Any]:
    """Verify every manifest hash, SQLite integrity, and optional isolated restore."""
    manifest_path = snapshot_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_id = hashlib.sha256(
        json.dumps(manifest["files"], sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if expected_id != manifest["baseline_id"]:
        raise ValueError("baseline identity does not match the manifest file list")
    for entry in manifest["files"]:
        path = snapshot_dir / entry["path"]
        if not path.is_file() or path.stat().st_size != entry["size"] or _sha256(path) != entry["sha256"]:
            raise ValueError(f"baseline file failed integrity check: {entry['path']}")
    db_path = snapshot_dir / "results.db"
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        status = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if status != "ok":
        raise ValueError(f"baseline database integrity check failed: {status}")
    with (snapshot_dir / "results.tsv").open(encoding="utf-8", newline="") as tsv:
        reader = csv.DictReader(tsv, delimiter="\t")
        if not reader.fieldnames or not set(_TSV_COLUMNS).issubset(reader.fieldnames):
            raise ValueError("baseline TSV does not match the established results schema")
        row_count = sum(1 for _ in reader)
    if restore_target is not None:
        restore_target.mkdir(parents=True, exist_ok=True)
        for entry in manifest["files"]:
            relative = Path(entry["path"])
            if relative.parts[0] == "results.db":
                target = restore_target / "results.db"
            elif relative.parts[0] == "results.tsv":
                target = restore_target / "results.tsv"
            else:
                target = restore_target / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(snapshot_dir / relative, target)
            if _sha256(target) != entry["sha256"]:
                raise OSError(f"restored file failed integrity check: {relative}")
        with sqlite3.connect(f"file:{restore_target / 'results.db'}?mode=ro", uri=True) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("restored database failed integrity check")
    return {
        "baseline_id": manifest["baseline_id"],
        "manifest_hash": _sha256(manifest_path),
        "file_count": len(manifest["files"]),
        "row_count": row_count,
        "restored": restore_target is not None,
        "ready": True,
    }


def import_legacy_baseline(
    *, lifecycle: ResearchLifecycle, snapshot_dir: Path, restore_target: Path,
) -> dict[str, Any]:
    """Restore-check first, then import literal TSV values as unknown-provenance rows."""
    _require_maintenance(lifecycle)
    verification = verify_baseline(snapshot_dir, restore_target)
    manifest_path = snapshot_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with (snapshot_dir / "results.tsv").open(encoding="utf-8", newline="") as tsv:
        reader = csv.DictReader(tsv, delimiter="\t")
        rows = list(reader)
    result = lifecycle.import_legacy_baseline(
        baseline_id=manifest["baseline_id"],
        manifest_hash=verification["manifest_hash"],
        rows=rows,
        manifest=manifest,
    )
    result["verification"] = verification
    return result


def _require_maintenance(lifecycle: ResearchLifecycle) -> None:
    if lifecycle.maintenance_status()["state"] != "active":
        raise RuntimeError("baseline operations require active maintenance")
    lifecycle.assert_maintenance_operation_allowed("baseline_capture")


def _sqlite_backup(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    target_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(target_connection)
        result = target_connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise ValueError(f"database backup integrity check failed: {result}")
    finally:
        target_connection.close()
        source_connection.close()


def _manifest_file(root: Path, path: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_fsync(path: Path, value: dict[str, Any]) -> None:
    data = json.dumps(value, sort_keys=True, indent=2).encode("utf-8")
    with path.open("wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())


def _safe_name(name: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in name)
    return safe or f"artifact-{uuid.uuid4().hex[:8]}"


def _is_secret(name: str) -> bool:
    lowered = name.casefold()
    return lowered in _SECRET_NAMES or lowered.startswith(".env.") or lowered.endswith((".pem", ".key"))
