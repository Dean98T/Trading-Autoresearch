#!/usr/bin/env python3
"""
run_research.py - Autonomous Research Loop Runner
==================================================
Runs backtest experiments on strategy.py and tracks results.
Can be used standalone or driven by an LLM agent.

Usage:
    python run_research.py                    # Run current strategy on all symbols
    python run_research.py --symbol BTCUSDT   # Single symbol
    python run_research.py --test             # Run on test period
    python run_research.py --full             # Train + test evaluation
"""

import argparse
import subprocess
from datetime import datetime
from pathlib import Path

import numpy as np

from backtest import run_strategy_backtest
from evaluate import compute_metrics, print_metrics
from research_lifecycle import ResearchLifecycle
from research_maintenance import assert_research_enabled, lifecycle_writer
from results_db import append_results as _db_append
from prepare import load_config
from research_rules import test_symbol_pass, train_symbol_pass
from validator import validate_file, validate_strategy, summarize_validation_errors


def get_git_commit() -> str:
    """Get current git commit hash (short)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def ensure_results_file(results_path: Path = None):
    """No-op: results.db is auto-initialized by results_db module."""
    pass


def run_experiment(
    symbols: list[str],
    period: str = "train",
    strategy_path: str = "strategy.py",
    config: dict = None,
    description: str = "",
) -> dict:
    """
    Run a single experiment across specified symbols.

    Returns dict with:
        - results: {symbol: BacktestResult}
        - metrics: {symbol: dict}
        - avg_sharpe: float
        - status: "keep" | "discard" | "crash"
    """
    if config is None:
        config = load_config()

    validation = validate_file(strategy_path)
    if not validation.valid:
        error = summarize_validation_errors(validation)
        print(f"\nERROR: strategy rejected before backtest: {error}")
        return {
            "results": {},
            "metrics": {},
            "avg_sharpe": 0.0,
            "status": "crash",
            "error": error,
        }

    results = {}
    all_metrics = {}
    total_time = 0

    for symbol in symbols:
        try:
            result = run_strategy_backtest(
                strategy_path=strategy_path,
                symbol=symbol,
                period=period,
                config=config,
            )
            metrics = compute_metrics(result)
            results[symbol] = result
            all_metrics[symbol] = metrics
            total_time += result.backtest_duration_s

            print_metrics(metrics, f"{symbol} {period.upper()}")

        except Exception as e:
            print(f"\nERROR running {symbol}: {e}")
            return {
                "results": results,
                "metrics": all_metrics,
                "avg_sharpe": 0.0,
                "status": "crash",
                "error": str(e),
            }

    # Compute average metrics across symbols
    if all_metrics:
        avg_sharpe = np.mean([m["sharpe_ratio"] for m in all_metrics.values()])
        avg_return = np.mean([m["total_return_pct"] for m in all_metrics.values()])
        avg_dd = np.mean([m["max_drawdown_pct"] for m in all_metrics.values()])
    else:
        avg_sharpe = avg_return = avg_dd = 0.0

    print(f"\n{'=' * 55}")
    print(f"  AGGREGATE ({period.upper()})")
    print(f"{'=' * 55}")
    print(f"  Avg Sharpe:     {avg_sharpe:.3f}")
    print(f"  Avg Return:     {avg_return:+.2f}%")
    print(f"  Avg Max DD:     {avg_dd:.2f}%")
    print(f"  Total Time:     {total_time:.1f}s")
    print(f"{'=' * 55}")

    return {
        "results": results,
        "metrics": all_metrics,
        "avg_sharpe": avg_sharpe,
        "status": "keep",
    }


def log_results(
    experiment: dict,
    results_path: Path = None,
    description: str = "",
):
    """Append experiment results to results.db."""
    results = []
    for symbol, metrics in experiment["metrics"].items():
        result = experiment["results"][symbol]
        m = dict(metrics)
        m["strategy"] = result.strategy_name
        m["symbol"] = symbol
        results.append(m)
    _db_append(results, experiment["status"], description)


def log_single_result(metrics: dict, status: str, description: str, period: str) -> None:
    payload = [dict(metrics)]
    _db_append(payload, status, description, period)


def _lifecycle_source_snapshot() -> dict[str, bytes]:
    """Capture the current working copies of files that define an evaluation."""
    root = Path(__file__).resolve().parent
    names = (
        "backtest.py",
        "evaluate.py",
        "research_rules.py",
        "prepare.py",
        "mtf_data.py",
        "validator.py",
        "research_lifecycle.py",
        "run_research.py",
    )
    return {
        name: (root / name).read_bytes()
        for name in names
        if (root / name).is_file()
    }


def _strategy_label(code: str, fallback: str) -> str:
    import ast

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return fallback
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(getattr(target, "id", None) == "name" for target in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    return fallback


def _evaluate_for_lifecycle(strategy_path: str, symbol: str, period: str, config: dict) -> dict:
    result = run_strategy_backtest(
        strategy_path=strategy_path,
        symbol=symbol,
        period=period,
        config=config,
    )
    return compute_metrics(result)


def _begin_manual_attempt(strategy_path: str, config: dict, lifecycle: ResearchLifecycle, period: str):
    path = Path(strategy_path)
    candidate_source = path.read_bytes()
    code = candidate_source.decode("utf-8")
    llm_config = config.get("llm", {}) if isinstance(config, dict) else {}
    provider = llm_config.get("provider", "unknown") if isinstance(llm_config, dict) else "unknown"
    attempt = lifecycle.begin_attempt(
        strategy_name=_strategy_label(code, path.stem),
        origin="manual",
        candidate_source=candidate_source,
        configuration=config,
        source_snapshot=_lifecycle_source_snapshot(),
        metadata={"requested_provider": provider},
        requested_period=period,
    )
    return path, code, attempt, validate_strategy(code)


def _run_lifecycle_period(
    *,
    path: Path,
    code: str,
    attempt: dict,
    symbols: list[str],
    period: str,
    config: dict,
    lifecycle: ResearchLifecycle,
    evaluator,
    validation,
    skip_reasons: dict[str, str] | None = None,
    parent_evaluation_id: str | None = None,
) -> dict:
    data_config = config.get("data", {}) if isinstance(config, dict) else {}
    evaluation = lifecycle.begin_evaluation(
        attempt["attempt_id"],
        period=period,
        symbols=symbols,
        parent_evaluation_id=parent_evaluation_id,
        context={
            "requested_window": {
                "start": data_config.get(f"{period}_start", "unknown"),
                "end": data_config.get(f"{period}_end", "unknown"),
            },
            "actual_coverage": "unknown",
            "input_identity": "unknown",
            "data_role": "exposed_validation" if period == "test" else "development",
        },
    )
    outcomes = []
    metrics_by_symbol = {}
    passes = train_symbol_pass if period == "train" else test_symbol_pass
    for symbol in symbols:
        if skip_reasons and symbol in skip_reasons:
            outcomes.append(
                lifecycle.record_outcome(
                    evaluation["evaluation_id"],
                    symbol=symbol,
                    period=period,
                    technical_outcome="skipped",
                    performance_qualification="not_measured",
                    selection_eligibility="ineligible",
                    reason=skip_reasons[symbol],
                )
            )
            continue
        if not validation.valid:
            outcomes.append(
                lifecycle.record_outcome(
                    evaluation["evaluation_id"],
                    symbol=symbol,
                    period=period,
                    technical_outcome="validation_failed",
                    performance_qualification="not_measured",
                    selection_eligibility="ineligible",
                    reason=summarize_validation_errors(validation),
                )
            )
            continue
        try:
            metrics = dict(evaluator(str(path), symbol, period, config))
            metrics["strategy"] = attempt["strategy_name"]
            metrics["symbol"] = symbol
            qualified = passes(metrics)
            metrics_by_symbol[symbol] = metrics
            outcomes.append(
                lifecycle.record_outcome(
                    evaluation["evaluation_id"],
                    symbol=symbol,
                    period=period,
                    technical_outcome="completed",
                    performance_qualification="qualified" if qualified else "unqualified",
                    selection_eligibility="eligible" if qualified else "ineligible",
                    metrics=metrics,
                )
            )
        except Exception as exc:
            outcomes.append(
                lifecycle.record_outcome(
                    evaluation["evaluation_id"],
                    symbol=symbol,
                    period=period,
                    technical_outcome="error",
                    performance_qualification="not_measured",
                    selection_eligibility="ineligible",
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )

    lifecycle.publish_current_results(evaluation["evaluation_id"])
    return {"evaluation": evaluation, "outcomes": outcomes, "metrics": metrics_by_symbol}


@lifecycle_writer("manual-research")
def run_lifecycle_experiment(
    *,
    strategy_path: str,
    symbols: list[str],
    period: str,
    config: dict,
    lifecycle: ResearchLifecycle,
    evaluator=None,
) -> dict:
    """Run one opt-in manual evaluation through the durable research lifecycle.

    ``evaluator`` is a deterministic test seam with the same arguments as the
    production evaluator and returns the existing ``compute_metrics`` mapping.
    It is called only after the attempt, exact source, safe configuration, and
    current working evaluation code have been durably archived.
    """
    if period not in {"train", "test"}:
        raise ValueError("Priority 0 manual development evaluations accept only train or test")
    if not symbols:
        raise ValueError("at least one symbol is required")
    path, code, attempt, validation = _begin_manual_attempt(strategy_path, config, lifecycle, period)
    result = _run_lifecycle_period(
        path=path,
        code=code,
        attempt=attempt,
        symbols=symbols,
        period=period,
        config=config,
        lifecycle=lifecycle,
        evaluator=evaluator or _evaluate_for_lifecycle,
        validation=validation,
    )
    outcomes = result["outcomes"]
    if any(item["performance_qualification"] == "qualified" for item in outcomes):
        status = "keep"
    elif any(item["technical_outcome"] == "completed" for item in outcomes):
        status = "discard"
    else:
        status = "crash"
    return {"attempt": attempt, **result, "status": status}


@lifecycle_writer("manual-full-research")
def run_lifecycle_full_experiment(
    *,
    strategy_path: str,
    symbols: list[str],
    config: dict,
    lifecycle: ResearchLifecycle,
    evaluator=None,
) -> dict:
    """Run train first, then exposed validation only for train-qualified symbols."""
    if not symbols:
        raise ValueError("at least one symbol is required")
    _, _, attempt, _ = _begin_manual_attempt(strategy_path, config, lifecycle, "full")
    return evaluate_lifecycle_attempt(
        attempt_id=attempt["attempt_id"],
        strategy_path=strategy_path,
        symbols=symbols,
        config=config,
        lifecycle=lifecycle,
        evaluator=evaluator,
    )


@lifecycle_writer("revalidation")
def evaluate_lifecycle_attempt(
    *,
    attempt_id: str,
    strategy_path: str,
    symbols: list[str],
    config: dict,
    lifecycle: ResearchLifecycle,
    evaluator=None,
) -> dict:
    """Evaluate the exact archived source without allocating a new attempt."""
    if not symbols:
        raise ValueError("at least one symbol is required")
    attempt = lifecycle.get_attempt(attempt_id)
    if attempt is None:
        raise KeyError(f"unknown experiment attempt: {attempt_id}")
    evaluate = evaluator or _evaluate_for_lifecycle
    path = Path(strategy_path)
    source = lifecycle.read_artifact(attempt_id, "candidate_source")
    if path.read_bytes() != source:
        raise ValueError("evaluation source does not match the attempt's archived candidate")
    code = source.decode("utf-8")
    validation = validate_strategy(code)
    previous_train = lifecycle.latest_evaluation(attempt_id, "train")
    previous_test = lifecycle.latest_evaluation(attempt_id, "test")
    train = _run_lifecycle_period(
        path=path,
        code=code,
        attempt=attempt,
        symbols=symbols,
        period="train",
        config=config,
        lifecycle=lifecycle,
        evaluator=evaluate,
        validation=validation,
        parent_evaluation_id=previous_train["evaluation_id"] if previous_train else None,
    )
    train_by_symbol = {row["symbol"]: row for row in train["outcomes"]}
    skip_reasons = {}
    for symbol in symbols:
        outcome = train_by_symbol[symbol]
        if outcome["performance_qualification"] != "qualified":
            if outcome["technical_outcome"] == "error":
                reason = "test skipped because train evaluation errored"
            elif outcome["technical_outcome"] == "validation_failed":
                reason = "test skipped because static validation failed"
            else:
                reason = "test skipped because train performance did not qualify"
            skip_reasons[symbol] = reason
    test = _run_lifecycle_period(
        path=path,
        code=code,
        attempt=attempt,
        symbols=symbols,
        period="test",
        config=config,
        lifecycle=lifecycle,
        evaluator=evaluate,
        validation=validation,
        skip_reasons=skip_reasons,
        parent_evaluation_id=previous_test["evaluation_id"] if previous_test else None,
    )
    if any(row["performance_qualification"] == "qualified" for row in test["outcomes"]):
        status = "keep"
    elif any(row["technical_outcome"] == "completed" for row in test["outcomes"]):
        status = "discard"
    else:
        status = "crash" if all(row["technical_outcome"] in {"error", "validation_failed"} for row in train["outcomes"]) else "discard"
    return {"attempt": attempt, "train": train, "test": test, "status": status}


def main():
    assert_research_enabled()
    parser = argparse.ArgumentParser(description="Run trading strategy research experiment")
    parser.add_argument("--strategy", default="strategy.py", help="Strategy file path")
    parser.add_argument("--symbol", default=None, help="Single symbol (default: all)")
    parser.add_argument("--test", action="store_true", help="Run on test period")
    parser.add_argument("--full", action="store_true", help="Run both train and test")
    parser.add_argument("--lifecycle-ledger", type=Path, default=None, help="Opt in to a dedicated durable research ledger")
    parser.add_argument("--lifecycle-artifacts", type=Path, default=None, help="Artifact archive path required with --lifecycle-ledger")
    parser.add_argument("--description", "-d", default="", help="Experiment description")
    args = parser.parse_args()

    if bool(args.lifecycle_ledger) != bool(args.lifecycle_artifacts):
        parser.error("--lifecycle-ledger and --lifecycle-artifacts must be provided together")

    config = load_config()
    symbols = [args.symbol] if args.symbol else config["data"]["symbols"]
    results_path = Path(config["research"]["results_file"])

    periods = []
    if args.test:
        periods = ["test"]
    else:
        periods = ["train"]

    if args.full:
        print(f"\n{'#' * 60}")
        print("# Running experiment: FULL per-symbol flow")
        print(f"# Strategy: {args.strategy}")
        print(f"# Symbols: {', '.join(symbols)}")
        print(f"# Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'#' * 60}")

        if args.lifecycle_ledger:
            lifecycle = ResearchLifecycle(args.lifecycle_ledger, args.lifecycle_artifacts)
            experiment = run_lifecycle_full_experiment(
                strategy_path=args.strategy,
                symbols=symbols,
                config=config,
                lifecycle=lifecycle,
            )
            print(
                f"\nRecorded attempt {experiment['attempt']['attempt_key']} "
                f"with status={experiment['status']} in {args.lifecycle_ledger}"
            )
            return

        for symbol in symbols:
            try:
                train_result = run_strategy_backtest(
                    strategy_path=args.strategy,
                    symbol=symbol,
                    period="train",
                    config=config,
                )
                train_metrics = compute_metrics(train_result)
                train_metrics["strategy"] = train_result.strategy_name
                train_metrics["symbol"] = symbol
                train_status = "keep" if train_symbol_pass(train_metrics) else "discard"
                print_metrics(train_metrics, f"{symbol} TRAIN")
                log_single_result(train_metrics, train_status, args.description or "train run", "train")

                if train_status != "keep":
                    print(f"{symbol} test skipped: train failed")
                    continue

                test_result = run_strategy_backtest(
                    strategy_path=args.strategy,
                    symbol=symbol,
                    period="test",
                    config=config,
                )
                test_metrics = compute_metrics(test_result)
                test_metrics["strategy"] = test_result.strategy_name
                test_metrics["symbol"] = symbol
                test_status = "keep" if test_symbol_pass(test_metrics) else "discard"
                print_metrics(test_metrics, f"{symbol} TEST")
                log_single_result(test_metrics, test_status, args.description or "test run", "test")

            except Exception as e:
                print(f"\nERROR running {symbol}: {e}")

        print(f"\nResults logged to {results_path}")
        return

    for period in periods:
        print(f"\n{'#' * 60}")
        print(f"# Running experiment: {period.upper()} period")
        print(f"# Strategy: {args.strategy}")
        print(f"# Symbols: {', '.join(symbols)}")
        print(f"# Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'#' * 60}")

        if args.lifecycle_ledger:
            lifecycle = ResearchLifecycle(args.lifecycle_ledger, args.lifecycle_artifacts)
            experiment = run_lifecycle_experiment(
                strategy_path=args.strategy,
                symbols=symbols,
                period=period,
                config=config,
                lifecycle=lifecycle,
            )
            print(
                f"\nRecorded attempt {experiment['attempt']['attempt_key']} "
                f"with status={experiment['status']} in {args.lifecycle_ledger}"
            )
            continue

        experiment = run_experiment(
            symbols=symbols,
            period=period,
            strategy_path=args.strategy,
            config=config,
            description=args.description,
        )

        # Log results
        desc = args.description or f"{period} run"
        log_results(experiment, results_path, description=desc)

        print(f"\nResults logged to {results_path}")


if __name__ == "__main__":
    main()
