#!/usr/bin/env python3
"""
run_systematic.py - Systematic combinatorial strategy search
Tests ALL indicator combinations. Results ALWAYS match saved files.
"""
import argparse
import random
import time
from pathlib import Path

from strategy_generator import (
    TREND_INDICATORS, ENTRY_FILTERS, REGIME_FILTERS,
    generate_strategy, get_all_combos
)
from backtest import run_strategy_backtest
from evaluate import compute_metrics
from agent_research import append_results
from research_rules import test_symbol_pass, train_symbol_pass
from research_lifecycle import ResearchLifecycle
from research_maintenance import assert_research_enabled, lifecycle_writer
from validator import validate_strategy
from run_research import evaluate_lifecycle_attempt

STRATEGIES_DIR = Path("strategies")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def _lifecycle_source_snapshot() -> dict[str, bytes]:
    root = Path(__file__).resolve().parent
    names = (
        "strategy_generator.py",
        "run_systematic.py",
        "run_research.py",
        "backtest.py",
        "evaluate.py",
        "research_rules.py",
        "prepare.py",
        "mtf_data.py",
        "validator.py",
        "research_lifecycle.py",
    )
    return {name: (root / name).read_bytes() for name in names if (root / name).is_file()}


@lifecycle_writer("systematic-research")
def run_systematic_candidate_with_lifecycle(
    *,
    lifecycle: ResearchLifecycle,
    strategy_name: str,
    generator,
    template_parameters: dict,
    symbols: list[str],
    config: dict,
    output_dir: Path,
    evaluator=None,
) -> dict:
    """Record one systematic candidate before generation, then evaluate it."""
    attempt = lifecycle.begin_attempt(
        strategy_name=strategy_name,
        origin="systematic",
        configuration=config,
        source_snapshot=_lifecycle_source_snapshot(),
        metadata={"template_parameters": template_parameters},
        requested_period="train+test",
    )
    try:
        code = generator()
    except Exception as exc:
        lifecycle.record_event(
            attempt["attempt_id"],
            event_type="generation_failed",
            stage="generation",
            payload={
                "technical_outcome": "generation_error",
                "performance_qualification": "not_measured",
                "error_type": type(exc).__name__,
                "reason": str(exc)[:1000],
            },
        )
        return {"attempt": attempt, "status": "generation_error", "error": exc}

    if not isinstance(code, str):
        code = str(code)
    lifecycle.record_artifact(attempt["attempt_id"], "candidate_source", code)
    lifecycle.record_event(
        attempt["attempt_id"],
        event_type="candidate_named",
        stage="generation",
        payload={"strategy_name": strategy_name},
    )
    lifecycle.record_event(
        attempt["attempt_id"],
        event_type="generation_completed",
        stage="generation",
        payload={
            "technical_outcome": "completed",
            "performance_qualification": "not_measured",
            "candidate_bytes": len(code.encode("utf-8")),
        },
    )
    validation = validate_strategy(code)
    if not validation.valid:
        reason = "; ".join(validation.errors)
        lifecycle.record_event(
            attempt["attempt_id"],
            event_type="validation_failed",
            stage="validation",
            payload={
                "technical_outcome": "rejected",
                "performance_qualification": "not_measured",
                "selection_eligibility": "ineligible",
                "reason": reason,
            },
        )
        return {"attempt": lifecycle.get_attempt(attempt["attempt_id"]), "status": "rejected", "reason": reason}

    lifecycle.record_event(
        attempt["attempt_id"],
        event_type="validation_passed",
        stage="validation",
        payload={"technical_outcome": "completed", "performance_qualification": "not_measured"},
    )
    import tempfile

    with tempfile.TemporaryDirectory(prefix="systematic-candidate-") as scratch:
        candidate_path = Path(scratch) / f"{strategy_name}.py"
        candidate_path.write_text(code, encoding="utf-8")
        evaluation = evaluate_lifecycle_attempt(
            attempt_id=attempt["attempt_id"],
            strategy_path=str(candidate_path),
            symbols=symbols,
            config=config,
            lifecycle=lifecycle,
            evaluator=evaluator,
        )
    if evaluation["status"] == "keep":
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / f"{strategy_name}.py"
        temporary_destination = destination.with_suffix(".py.tmp")
        temporary_destination.write_text(code, encoding="utf-8")
        temporary_destination.replace(destination)
    return {"attempt": evaluation["attempt"], "status": evaluation["status"], "evaluation": evaluation}


def main():
    assert_research_enabled()
    parser = argparse.ArgumentParser(description="Systematic combinatorial strategy search")
    parser.add_argument("--lifecycle-ledger", type=Path, default=None, help="Opt in to durable research attempt recording")
    parser.add_argument("--lifecycle-artifacts", type=Path, default=None, help="Artifact archive required with --lifecycle-ledger")
    args = parser.parse_args()
    if bool(args.lifecycle_ledger) != bool(args.lifecycle_artifacts):
        parser.error("--lifecycle-ledger and --lifecycle-artifacts must be provided together")
    lifecycle = (
        ResearchLifecycle(args.lifecycle_ledger, args.lifecycle_artifacts)
        if args.lifecycle_ledger
        else None
    )
    config = None
    if lifecycle:
        from prepare import load_config

        config = load_config()

    combos = get_all_combos()
    random.shuffle(combos)
    STRATEGIES_DIR.mkdir(exist_ok=True)

    tested = 0
    kept = 0

    print(f"Systematic search: {len(combos)} combinations")

    for trend_name, entry_name, regime_name, tf in combos:
        trend_info = TREND_INDICATORS[trend_name]
        entry_info = ENTRY_FILTERS[entry_name]
        regime_info = REGIME_FILTERS[regime_name]
        trend_params = {k: v[0] for k, v in trend_info["params"].items()}
        entry_params = {k: v[0] for k, v in entry_info["params"].items()}
        regime_params = {k: v[0] for k, v in regime_info["params"].items()}

        name = f"gen_{trend_name}_{entry_name}_{regime_name}_{tf}_v1"
        
        # Skip if already saved and tested
        saved_path = STRATEGIES_DIR / f"{name}.py"
        if saved_path.exists():
            continue

        if lifecycle:
            result = run_systematic_candidate_with_lifecycle(
                lifecycle=lifecycle,
                strategy_name=name,
                generator=lambda: generate_strategy(
                    trend_name,
                    entry_name,
                    regime_name,
                    tf,
                    0.25,
                    trend_params,
                    entry_params,
                    regime_params,
                ),
                template_parameters={
                    "trend_indicator": trend_name,
                    "entry_filter": entry_name,
                    "regime_filter": regime_name,
                    "timeframe": tf,
                    "trend_parameters": trend_params,
                    "entry_parameters": entry_params,
                    "regime_parameters": regime_params,
                    "position_size": 0.25,
                },
                symbols=SYMBOLS,
                config=config,
                output_dir=STRATEGIES_DIR,
            )
            tested += 1
            if result["status"] == "keep":
                kept += 1
                print(f"✓ #{tested:>4d} {name} (lifecycle attempt {result['attempt']['attempt_key']})")
            elif tested % 20 == 0:
                print(f"  #{tested:>4d} {name} [{result['status']}]")
            continue

        try:
            code = generate_strategy(
                trend_name, entry_name, regime_name, tf, 0.25,
                trend_params, entry_params, regime_params
            )
        except Exception:
            continue

        # SAVE FIRST, then test from saved file — guarantees consistency
        saved_path.write_text(code)
        
        tested += 1
        t0 = time.time()
        any_kept = False

        for sym in SYMBOLS:
            try:
                # Train from SAVED file
                r = run_strategy_backtest(str(saved_path), sym, "train")
                m = compute_metrics(r)
                sharpe = m["sharpe_ratio"]
                trades = m["num_trades"]
                
                m["strategy"] = name
                m["symbol"] = sym
                train_pass = train_symbol_pass(m)
                append_results([m], "keep" if train_pass else "discard", name, "train")

                if not train_pass:
                    continue

                # Test from SAME saved file
                rt = run_strategy_backtest(str(saved_path), sym, "test")
                mt = compute_metrics(rt)
                mt["strategy"] = name
                mt["symbol"] = sym
                test_pass = test_symbol_pass(mt)
                append_results([mt], "keep" if test_pass else "discard", name, "test")

                if test_pass:
                    any_kept = True

            except Exception:
                continue

        dt = time.time() - t0
        if any_kept:
            kept += 1
            print(f"✓ #{tested:>4d} {name} ({dt:.1f}s)")
        else:
            # Remove strategy file if nothing kept
            if not any_kept and saved_path.exists():
                saved_path.unlink()
            if tested % 20 == 0:
                print(f"  #{tested:>4d} {trend_name}+{entry_name}+{regime_name} {tf} ({dt:.1f}s)")

        if tested % 100 == 0:
            print(f"\n  ── Progress: {tested}/{len(combos)}, {kept} kept ──\n")

    print(f"\nDONE: {tested} tested, {kept} kept")


if __name__ == "__main__":
    main()
