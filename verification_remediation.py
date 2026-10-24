#!/usr/bin/env python3
"""
Run independent verification on recent strategies, purge invalid stored results,
rerun stale strategies, and refresh docs.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from results_db import delete_strategy, query_recent_strategy_names
from research_lifecycle import ResearchLifecycle, StaleCurrentViewError
from research_maintenance import assert_research_enabled
from validator import validate_file, validate_strategy


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "results.db"
DOCS_DIR = ROOT / "docs" / "strategies"
STRATEGIES_DIR = ROOT / "strategies"
RESULTS_TSV = ROOT / "results.tsv"
IV_DIR = ROOT / "independent-verification"
IV_RUNNER = IV_DIR / "run_verification.py"
IV_DEEP = IV_DIR / "deep_checks.py"
REVALIDATE = ROOT / "revalidate.py"
PYTHON_BIN = ROOT / ".venv" / "bin" / "python"
RESTART_FLAG = ROOT / "logs" / "restart_agent_research.flag"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit recent strategies and remediate invalid stored results.")
    parser.add_argument("--strategies", nargs="*", default=None, help="Specific strategy names to audit.")
    parser.add_argument("--all-filesystem", action="store_true", help="Audit every strategy file in strategies/.")
    parser.add_argument("--recent-limit", type=int, default=25, help="Recent distinct strategies to audit when --strategies is not set.")
    parser.add_argument("--workers", type=int, default=4, help="Independent verification worker count.")
    parser.add_argument("--tail-check", type=int, default=8, help="Tail window for prefix checks.")
    parser.add_argument("--max-prefix-checks", type=int, default=8, help="Number of prefix checkpoints per strategy.")
    parser.add_argument("--skip-deep", action="store_true", help="Skip deep verification checks.")
    parser.add_argument("--lifecycle-ledger", type=Path, default=None, help="Use durable invalidation and append-only reruns")
    parser.add_argument("--lifecycle-artifacts", type=Path, default=None, help="Artifact archive required with --lifecycle-ledger")
    parser.add_argument("--lifecycle-tsv", type=Path, default=None, help="Fresh atomic TSV snapshot consumed by verification")
    return parser.parse_args()


def log(msg: str) -> None:
    print(f"[verify-remediate] {msg}", flush=True)


def request_agent_restart(reason: str) -> None:
    RESTART_FLAG.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    RESTART_FLAG.write_text(f"{timestamp} {reason}\n", encoding="utf-8")
    log(f"requested agent_research restart: {reason}")


def selected_strategies(args: argparse.Namespace) -> list[str]:
    if args.strategies:
        return [name.removesuffix(".py") for name in args.strategies]
    if args.all_filesystem:
        return sorted(path.stem for path in STRATEGIES_DIR.glob("*.py"))
    return query_recent_strategy_names(args.recent_limit)


def selected_lifecycle_attempt_keys(args: argparse.Namespace, lifecycle: ResearchLifecycle) -> list[str]:
    attempts = lifecycle.list_attempts()
    rows = lifecycle.current_results()
    by_id = {attempt["attempt_id"]: attempt for attempt in attempts}
    if args.strategies:
        requested = {name.removesuffix(".py") for name in args.strategies}
        return sorted({
            row["strategy_key"] for row in rows
            if row["strategy_key"] in requested
            or by_id[row["attempt_id"]]["display_strategy_name"] in requested
        })
    if args.all_filesystem:
        return sorted({row["strategy_key"] for row in rows})
    limit = max(0, int(args.recent_limit))
    return sorted({row["strategy_key"] for row in rows})[-limit:] if limit else []


def prevalidate_lifecycle_attempts(
    lifecycle: ResearchLifecycle, attempt_keys: list[str]
) -> tuple[list[str], dict[str, list[str]], list[str]]:
    by_key = {attempt["attempt_key"]: attempt for attempt in lifecycle.list_attempts()}
    valid: list[str] = []
    invalid: dict[str, list[str]] = {}
    missing: list[str] = []
    for key in attempt_keys:
        attempt = by_key.get(key)
        if attempt is None:
            missing.append(key)
            continue
        try:
            source = lifecycle.read_artifact(attempt["attempt_id"], "candidate_source").decode("utf-8")
        except (KeyError, OSError, UnicodeDecodeError):
            missing.append(key)
            continue
        validation = validate_strategy(source)
        if validation.valid:
            valid.append(key)
        else:
            invalid[key] = validation.errors[:5]
    return valid, invalid, missing


def lifecycle_attempt_for_key(lifecycle: ResearchLifecycle, attempt_key: str) -> dict | None:
    return next((item for item in lifecycle.list_attempts() if item["attempt_key"] == attempt_key), None)


def require_lifecycle_export(lifecycle: ResearchLifecycle, path: Path) -> None:
    """Refuse independent verification when the exported view is not current."""
    lifecycle.require_fresh_export(path)


def strategy_path(strategy_name: str) -> Path:
    return STRATEGIES_DIR / f"{strategy_name}.py"


def prevalidate_strategies(strategies: list[str]) -> tuple[list[str], dict[str, list[str]], list[str]]:
    valid: list[str] = []
    invalid: dict[str, list[str]] = {}
    missing: list[str] = []

    for strategy_name in strategies:
        path = strategy_path(strategy_name)
        if not path.exists():
            missing.append(strategy_name)
            continue
        validation = validate_file(str(path))
        if validation.valid:
            valid.append(strategy_name)
            continue
        invalid[strategy_name] = validation.errors[:5]

    return valid, invalid, missing


def run_verification(strategies: list[str], args: argparse.Namespace, label: str) -> Path:
    run_dir = IV_DIR / "runs" / f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{label}"
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(PYTHON_BIN),
        str(IV_RUNNER),
        "--output-dir",
        str(run_dir),
        "--workers",
        str(args.workers),
        "--lookahead-symbol",
        "ALL",
        "--tail-check",
        str(args.tail_check),
        "--max-prefix-checks",
        str(args.max_prefix_checks),
        "--strategies",
        *strategies,
    ]
    if getattr(args, "lifecycle_ledger", None):
        lifecycle = ResearchLifecycle(args.lifecycle_ledger, args.lifecycle_artifacts)
        require_lifecycle_export(lifecycle, args.lifecycle_tsv)
        cmd.extend([
            "--results-file", str(args.lifecycle_tsv),
            "--lifecycle-ledger", str(args.lifecycle_ledger),
            "--lifecycle-artifacts", str(args.lifecycle_artifacts),
        ])
    log(f"running verifier for {len(strategies)} strategies -> {run_dir}")
    subprocess.run(cmd, cwd=ROOT, check=True)
    if not args.skip_deep:
        subprocess.run([str(PYTHON_BIN), str(IV_DEEP), "--run-dir", str(run_dir)], cwd=ROOT, check=True)
    return run_dir


def failing_strategies(run_dir: Path) -> tuple[set[str], set[str]]:
    summary = pd.read_csv(run_dir / "strategy_summary.csv")
    results = pd.read_csv(run_dir / "verification_results.csv")

    purge = set(
        summary.loc[
            summary["severity"].isin(["critical", "error"])
            | (summary["lookahead_pass"] == False)  # noqa: E712
            | (summary["io_sandbox_pass"] == False),  # noqa: E712
            "strategy_name",
        ].tolist()
    )

    rerun = set(
        results.loc[
            ((results["period"].isin(["train", "test"])) & (results["return_match"] == False))  # noqa: E712
            | ((results["period"].isin(["train", "test"])) & (results["sharpe_match"] == False)),  # noqa: E712
            "strategy_name",
        ].tolist()
    )

    deep_path = run_dir / "deep_checks_summary.csv"
    if deep_path.exists():
        deep = pd.read_csv(deep_path)
        purge.update(deep.loc[deep["dense_prefix_failures"] > 0, "strategy_name"].tolist())

    rerun.update(purge)
    return purge, rerun


def purge_strategy(strategy_name: str) -> None:
    log(f"purging stored results for {strategy_name}")
    delete_strategy(strategy_name)

    doc_path = DOCS_DIR / f"{strategy_name}.md"
    if doc_path.exists():
        doc_path.unlink()

    if RESULTS_TSV.exists():
        tmp_path = RESULTS_TSV.with_suffix(".tsv.tmp")
        with RESULTS_TSV.open("r", encoding="utf-8") as src, tmp_path.open("w", encoding="utf-8") as dst:
            for idx, line in enumerate(src):
                if idx == 0 or f"\t{strategy_name}\t" not in line:
                    dst.write(line)
        tmp_path.replace(RESULTS_TSV)


def rerun_strategy(strategy_name: str) -> bool:
    path = strategy_path(strategy_name)
    if not path.exists():
        log(f"skip rerun for missing strategy file {strategy_name}")
        return False
    log(f"rerunning {strategy_name}")
    proc = subprocess.run([str(PYTHON_BIN), str(REVALIDATE), "--strategy", strategy_name], cwd=ROOT)
    return proc.returncode == 0


def invalidate_lifecycle_strategy(lifecycle: ResearchLifecycle, strategy_name: str, reason: str) -> dict:
    from audit_saved_strategies import invalidate_matching_attempts

    return invalidate_matching_attempts(
        lifecycle=lifecycle,
        strategy_path=strategy_path(strategy_name),
        reason=reason,
    )


def rerun_lifecycle_strategy(
    *,
    lifecycle: ResearchLifecycle,
    strategy_name: str,
    strategy_path: Path,
    symbols: list[str],
    config: dict,
    evaluator=None,
    prefix_checker=None,
) -> dict:
    """Resolve by exact saved source and append a linked revalidation."""
    from revalidate import find_attempts_for_saved_source, revalidate_attempt_with_lifecycle

    matches = find_attempts_for_saved_source(lifecycle, strategy_name, strategy_path)
    if len(matches) != 1:
        return {"status": "ambiguous", "attempt_ids": matches}
    result = revalidate_attempt_with_lifecycle(
        lifecycle=lifecycle,
        attempt_id=matches[0],
        symbols=symbols,
        config=config,
        evaluator=evaluator,
        prefix_checker=prefix_checker,
    )
    return {"attempt_ids": matches, **result}


def fetch_rows(strategy_name: str, period: str) -> list[dict]:
    if not DB_PATH.exists():
        return []
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT symbol, sharpe, return_pct, max_dd_pct, trades, status
            FROM results
            WHERE strategy = ? AND period = ?
            ORDER BY symbol
            """,
            (strategy_name, period),
        ).fetchall()
    return [
        {
            "symbol": row[0],
            "sharpe": row[1],
            "return_pct": row[2],
            "max_dd_pct": row[3],
            "trades": row[4],
            "status": row[5],
        }
        for row in rows
    ]


def refresh_strategy_doc(strategy_name: str) -> None:
    path = strategy_path(strategy_name)
    if not path.exists():
        return

    train_rows = fetch_rows(strategy_name, "train")
    test_rows = fetch_rows(strategy_name, "test")
    if not train_rows and not test_rows:
        doc_path = DOCS_DIR / f"{strategy_name}.md"
        if doc_path.exists():
            doc_path.unlink()
        return

    code = path.read_text(encoding="utf-8").rstrip()

    def table(rows: list[dict]) -> str:
        if not rows:
            return ""
        return "".join(
            f"| {row['symbol']} | {row['sharpe']:.3f} | {row['return_pct']:+.1f}% | {row['max_dd_pct']:.1f}% | {row['trades']} | {str(row['status']).upper()} |\n"
            for row in rows
        )

    doc = (
        f"# Strategy: {strategy_name}\n\n"
        "## Train Results\n"
        "| Symbol | Sharpe | Return | Max DD | Trades | Status |\n"
        "|--------|--------|--------|--------|--------|--------|\n"
        f"{table(train_rows)}"
        "\n## Test Results (2025+)\n"
        "| Symbol | Sharpe | Return | Max DD | Trades | Status |\n"
        "|--------|--------|--------|--------|--------|--------|\n"
        f"{table(test_rows)}"
        f"\n## Code\n```python\n{code}\n```\n\n"
        f"## Last Updated\n{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
    )
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / f"{strategy_name}.md").write_text(doc, encoding="utf-8")


def main() -> int:
    assert_research_enabled()
    args = parse_args()
    if bool(args.lifecycle_ledger) != bool(args.lifecycle_artifacts):
        log("--lifecycle-ledger and --lifecycle-artifacts must be provided together")
        return 2
    lifecycle = (
        ResearchLifecycle(args.lifecycle_ledger, args.lifecycle_artifacts)
        if args.lifecycle_ledger
        else None
    )
    if lifecycle and not args.lifecycle_tsv:
        log("--lifecycle-tsv is required with --lifecycle-ledger")
        return 2
    if lifecycle:
        try:
            require_lifecycle_export(lifecycle, args.lifecycle_tsv)
        except StaleCurrentViewError as exc:
            log(f"verification refused: {exc}")
            return 2
    strategies = selected_lifecycle_attempt_keys(args, lifecycle) if lifecycle else selected_strategies(args)
    if not strategies:
        log("no strategies selected")
        return 0

    if lifecycle:
        valid_strategies, invalid_map, missing_files = prevalidate_lifecycle_attempts(lifecycle, strategies)
    else:
        valid_strategies, invalid_map, missing_files = prevalidate_strategies(strategies)
    if (invalid_map or missing_files) and lifecycle is None:
        request_agent_restart(
            f"validator removed invalid strategies before verification (invalid={len(invalid_map)}, missing={len(missing_files)})"
        )

    for strategy_name in missing_files:
        if lifecycle:
            log(f"missing source for {strategy_name}; legacy evidence was left unchanged")
        else:
            log(f"purging missing strategy {strategy_name}")
            purge_strategy(strategy_name)

    for strategy_name, errors in sorted(invalid_map.items()):
        log(f"purging validator-invalid strategy {strategy_name}")
        for error in errors:
            log(f"  - {error}")
        if lifecycle:
            attempt = lifecycle_attempt_for_key(lifecycle, strategy_name)
            if attempt:
                lifecycle.invalidate(
                    attempt_id=attempt["attempt_id"],
                    reason="static validation failed: " + "; ".join(errors),
                )
                log(f"invalidation recorded for {strategy_name}")
        else:
            purge_strategy(strategy_name)

    if not valid_strategies:
        log(
            f"completed audit: selected={len(strategies)} purge={len(invalid_map) + len(missing_files)} "
            "rerun=0 reran=0"
        )
        return 0

    if lifecycle and (invalid_map or missing_files):
        lifecycle.rebuild_current_results()
        lifecycle.export_current_tsv(args.lifecycle_tsv)

    initial_run = run_verification(valid_strategies, args, "initial")
    purge_set, rerun_set = failing_strategies(initial_run)
    if purge_set or rerun_set:
        if lifecycle is None:
            request_agent_restart(
                f"verification found invalid/stale strategies (purge={len(purge_set)}, rerun={len(rerun_set)})"
            )

    for strategy_name in sorted(purge_set):
        if lifecycle:
            attempt = lifecycle_attempt_for_key(lifecycle, strategy_name)
            if attempt:
                lifecycle.invalidate(
                    attempt_id=attempt["attempt_id"],
                    reason="independent verification reported invalid or failing evidence",
                )
                log(f"invalidation recorded for {strategy_name}")
        else:
            purge_strategy(strategy_name)

    reran: list[str] = []
    for strategy_name in sorted(rerun_set):
        if lifecycle:
            from prepare import load_config
            from revalidate import revalidate_attempt_with_lifecycle

            attempt = lifecycle_attempt_for_key(lifecycle, strategy_name)
            if attempt:
                result = revalidate_attempt_with_lifecycle(
                    lifecycle=lifecycle,
                    attempt_id=attempt["attempt_id"],
                    symbols=SYMBOLS,
                    config=load_config(),
                )
                reran.append(strategy_name)
                log(f"revalidation for {strategy_name}: {result['status']}")
        elif rerun_strategy(strategy_name):
            refresh_strategy_doc(strategy_name)
            reran.append(strategy_name)

    if reran and lifecycle:
        lifecycle.rebuild_current_results()
        lifecycle.export_current_tsv(args.lifecycle_tsv)

    if reran:
        verify_run = run_verification(reran, args, "rerun")
        purge_after_rerun, _ = failing_strategies(verify_run)
        for strategy_name in sorted(purge_after_rerun):
            if lifecycle:
                attempt = lifecycle_attempt_for_key(lifecycle, strategy_name)
                if attempt:
                    lifecycle.invalidate(
                        attempt_id=attempt["attempt_id"],
                        reason="post-remediation verification still failed",
                    )
            else:
                purge_strategy(strategy_name)
        if lifecycle and purge_after_rerun:
            lifecycle.rebuild_current_results()
            lifecycle.export_current_tsv(args.lifecycle_tsv)
        if purge_after_rerun and lifecycle is None:
            request_agent_restart(
                f"post-rerun verification still found invalid strategies (purge={len(purge_after_rerun)})"
            )

    log(
        f"completed audit: selected={len(strategies)} purge={len(purge_set) + len(invalid_map) + len(missing_files)} "
        f"rerun={len(rerun_set)} reran={len(reran)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
