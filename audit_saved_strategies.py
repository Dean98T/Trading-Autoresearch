#!/usr/bin/env python3
"""
Audit saved strategies for static-rule violations and prefix look-ahead.

Optionally purge invalid strategies from:
- strategies/
- docs/strategies/
- results.db
"""

import argparse
import hashlib
from pathlib import Path

from research_lifecycle import ResearchLifecycle
from research_maintenance import assert_research_enabled
from results_db import delete_strategy
from validator import run_prefix_lookahead_check, validate_file

STRATEGIES_DIR = Path("strategies")
DOCS_DIR = Path("docs/strategies")


def purge_strategy(strategy_name: str) -> None:
    strategy_path = STRATEGIES_DIR / f"{strategy_name}.py"
    doc_path = DOCS_DIR / f"{strategy_name}.md"
    delete_strategy(strategy_name)
    if strategy_path.exists():
        strategy_path.unlink()
    if doc_path.exists():
        doc_path.unlink()


def invalidate_matching_attempts(
    *, lifecycle: ResearchLifecycle, strategy_path: Path, reason: str
) -> dict:
    """Invalidate only attempts whose archived source matches the audited file."""
    if not strategy_path.is_file():
        return {"status": "missing_source", "attempt_ids": []}
    source_hash = hashlib.sha256(strategy_path.read_bytes()).hexdigest()
    matches = [attempt for attempt in lifecycle.list_attempts() if attempt["source_hash"] == source_hash]
    if not matches:
        return {"status": "ambiguous_legacy", "attempt_ids": []}
    attempt_ids = sorted({attempt["attempt_id"] for attempt in matches})
    for attempt_id in attempt_ids:
        lifecycle.invalidate(attempt_id=attempt_id, reason=reason)
    return {"status": "invalidated", "attempt_ids": attempt_ids}


def _purge_or_invalidate(strategy_name: str, reason: str, lifecycle: ResearchLifecycle | None) -> None:
    if lifecycle is None:
        purge_strategy(strategy_name)
        return
    result = invalidate_matching_attempts(
        lifecycle=lifecycle,
        strategy_path=STRATEGIES_DIR / f"{strategy_name}.py",
        reason=reason,
    )
    if result["status"] == "ambiguous_legacy":
        print(f"[INCOMPLETE] {strategy_name}: no exact attempt source match; legacy evidence was not changed")
    elif result["status"] == "missing_source":
        print(f"[INCOMPLETE] {strategy_name}: source missing; legacy evidence was not changed")
    else:
        print(f"[INVALIDATED] {strategy_name}: {len(result['attempt_ids'])} attempt(s); source and history retained")


def main() -> int:
    assert_research_enabled()
    parser = argparse.ArgumentParser(description="Audit saved strategies and optionally purge invalid ones")
    parser.add_argument("--strategy", help="Single strategy name without .py", default=None)
    parser.add_argument("--symbol", default="BTCUSDT", help="Symbol used for prefix audit")
    parser.add_argument("--skip-prefix", action="store_true", help="Skip dynamic prefix look-ahead audit")
    parser.add_argument("--purge-invalid", action="store_true", help="Delete invalid strategy files/docs and DB rows")
    parser.add_argument("--lifecycle-ledger", type=Path, default=None, help="Use durable invalidation instead of destructive purge")
    parser.add_argument("--lifecycle-artifacts", type=Path, default=None, help="Artifact archive required with --lifecycle-ledger")
    args = parser.parse_args()

    if bool(args.lifecycle_ledger) != bool(args.lifecycle_artifacts):
        parser.error("--lifecycle-ledger and --lifecycle-artifacts must be provided together")
    lifecycle = (
        ResearchLifecycle(args.lifecycle_ledger, args.lifecycle_artifacts)
        if args.lifecycle_ledger
        else None
    )

    if args.strategy:
        paths = [STRATEGIES_DIR / f"{args.strategy}.py"]
    else:
        paths = sorted(STRATEGIES_DIR.glob("*.py"))

    total = 0
    static_fail = 0
    prefix_fail = 0
    kept = 0

    for path in paths:
        if not path.exists():
            continue
        total += 1
        strategy_name = path.stem

        validation = validate_file(str(path))
        if not validation.valid:
            static_fail += 1
            print(f"[INVALID:STATIC] {strategy_name}")
            for err in validation.errors[:5]:
                print(f"  - {err}")
            if args.purge_invalid:
                _purge_or_invalidate(strategy_name, "static validation failed: " + "; ".join(validation.errors[:5]), lifecycle)
            continue

        if not args.skip_prefix:
            ok, message = run_prefix_lookahead_check(str(path), symbol=args.symbol)
            if not ok:
                prefix_fail += 1
                print(f"[INVALID:PREFIX] {strategy_name}")
                print(f"  - {message}")
                if args.purge_invalid:
                    _purge_or_invalidate(strategy_name, "prefix look-ahead audit failed: " + message, lifecycle)
                continue

        kept += 1
        print(f"[OK] {strategy_name}")

    print()
    print(f"Audited: {total}")
    print(f"Static invalid: {static_fail}")
    print(f"Prefix invalid: {prefix_fail}")
    print(f"Valid remaining: {kept}")
    if args.purge_invalid:
        print("Invalid strategies were purged from strategies/, docs/strategies/, and results.db")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
