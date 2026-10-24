#!/usr/bin/env python3
"""Manage an isolated, verifiable snapshot of legacy research evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research_baseline import capture_legacy_baseline, import_legacy_baseline, verify_baseline
from research_lifecycle import ResearchLifecycle


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture", "verify", "import"))
    parser.add_argument("--lifecycle-ledger", type=Path, required=True)
    parser.add_argument("--lifecycle-artifacts", type=Path, required=True)
    parser.add_argument("--legacy-db", type=Path)
    parser.add_argument("--legacy-tsv", type=Path)
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    parser.add_argument("--snapshot-root", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--restore-target", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    lifecycle = ResearchLifecycle(args.lifecycle_ledger, args.lifecycle_artifacts)
    if args.action == "capture":
        if not args.legacy_db or not args.legacy_tsv or not args.snapshot_root:
            raise SystemExit("capture requires --legacy-db, --legacy-tsv, and --snapshot-root")
        result = capture_legacy_baseline(
            lifecycle=lifecycle, legacy_db=args.legacy_db, legacy_tsv=args.legacy_tsv,
            artifact_paths=args.artifact, snapshot_root=args.snapshot_root,
        )
    elif args.action == "verify":
        if not args.snapshot:
            raise SystemExit("verify requires --snapshot")
        result = verify_baseline(args.snapshot, args.restore_target)
    else:
        if not args.snapshot or not args.restore_target:
            raise SystemExit("import requires --snapshot and --restore-target")
        result = import_legacy_baseline(
            lifecycle=lifecycle, snapshot_dir=args.snapshot, restore_target=args.restore_target,
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
