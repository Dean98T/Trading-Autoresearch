#!/usr/bin/env python3
"""Enter, inspect, recover, or exit the coordinated research maintenance gate."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from research_lifecycle import ResearchLifecycle
from research_maintenance import (
    MAINTENANCE_FLAG,
    active_research_processes,
    clear_maintenance_flag,
    supported_writer_compatibility,
    write_maintenance_flag,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("enter", "status", "recover", "exit"))
    parser.add_argument("--lifecycle-ledger", type=Path, required=True)
    parser.add_argument("--lifecycle-artifacts", type=Path, required=True)
    parser.add_argument("--reason", default="operator maintenance")
    return parser.parse_args()


def status(lifecycle: ResearchLifecycle) -> dict:
    lifecycle_status = lifecycle.maintenance_status()
    processes = active_research_processes()
    compatibility = supported_writer_compatibility()
    return {
        **lifecycle_status,
        "maintenance_flag": str(MAINTENANCE_FLAG),
        "maintenance_flag_present": MAINTENANCE_FLAG.exists(),
        "active_research_processes": processes,
        "supported_writer_compatibility": compatibility,
        "ready_for_maintenance_work": (
            lifecycle_status["state"] == "active"
            and lifecycle_status["quiescent"]
            and not processes
            and all(compatibility.values())
            and MAINTENANCE_FLAG.exists()
        ),
    }


def run(args: argparse.Namespace) -> tuple[int, dict]:
    lifecycle = ResearchLifecycle(args.lifecycle_ledger, args.lifecycle_artifacts)
    if args.action == "enter":
        maintenance_id = str(uuid.uuid4())
        write_maintenance_flag(args.reason, maintenance_id)
        lifecycle.enter_maintenance(args.reason, maintenance_id=maintenance_id)
        result = status(lifecycle)
        return (0 if result["ready_for_maintenance_work"] else 2), result
    if args.action == "recover":
        stale = lifecycle.recover_stale_writer_sessions()
        result = {**status(lifecycle), "removed_stale_writer_ids": stale}
        return (0 if result["ready_for_maintenance_work"] else 2), result
    if args.action == "status":
        result = status(lifecycle)
        return (0 if result["state"] == "inactive" or result["ready_for_maintenance_work"] else 2), result

    current = status(lifecycle)
    if current["state"] != "active" or not current["maintenance_flag_present"]:
        return 2, {**current, "error": "maintenance must be active before exit"}
    if current["active_writers"] or current["active_research_processes"]:
        return 2, {**current, "error": "supported writers must be quiescent before exit"}
    lifecycle.exit_maintenance()
    clear_maintenance_flag()
    return 0, status(lifecycle)


def main() -> int:
    code, result = run(parse_args())
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
