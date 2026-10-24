#!/usr/bin/env python3
"""Publish the lifecycle current-result view to a legacy-schema TSV."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research_lifecycle import ResearchLifecycle, StaleCurrentViewError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lifecycle-ledger", type=Path, required=True)
    parser.add_argument("--lifecycle-artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Destination TSV path")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild the derived current view from committed evidence first")
    parser.add_argument("--check", action="store_true", help="Check export freshness without writing")
    return parser.parse_args()


def run(lifecycle: ResearchLifecycle, output: Path, *, rebuild: bool = False, check: bool = False) -> dict:
    if check:
        freshness = lifecycle.export_freshness(output)
        if not freshness["fresh"]:
            raise StaleCurrentViewError("current-result TSV is stale or corrupt")
        return freshness
    if rebuild:
        lifecycle.rebuild_current_results()
    return lifecycle.export_current_tsv(output)


def main() -> int:
    args = parse_args()
    lifecycle = ResearchLifecycle(args.lifecycle_ledger, args.lifecycle_artifacts)
    try:
        result = run(lifecycle, args.output, rebuild=args.rebuild, check=args.check)
    except (OSError, StaleCurrentViewError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
