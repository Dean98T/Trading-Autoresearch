"""Shared maintenance gates used by writers and their supervisors."""

from __future__ import annotations

import functools
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, TypeVar


ROOT = Path(__file__).resolve().parent
MAINTENANCE_FLAG = ROOT / "logs" / "research_maintenance.flag"
_WRITER_PATTERNS = (
    "agent_research.py", "run_research.py", "run_systematic.py", "revalidate.py",
    "verification_remediation.py", "audit_saved_strategies.py", "auto_concept_research.py",
    "auto_process_review.py", "internet_strategy_discovery.py", "self_improvement_watchdog.py",
    "auto_concept_research.sh", "auto_process_review.sh",
)
_PYTHON_WRITERS = (
    "agent_research.py", "run_research.py", "run_systematic.py", "revalidate.py",
    "verification_remediation.py", "audit_saved_strategies.py", "auto_concept_research.py",
    "auto_process_review.py", "internet_strategy_discovery.py",
)
_SUPERVISORS = {
    "watchdog.sh": "research_maintenance.flag",
    "auto_concept_research.sh": "research_maintenance.flag",
    "auto_process_review.sh": "research_maintenance.flag",
    "self_improvement_watchdog.py": "maintenance_active",
}
F = TypeVar("F", bound=Callable[..., Any])


class MaintenanceActiveError(RuntimeError):
    pass


def maintenance_active(flag_path: Path = MAINTENANCE_FLAG) -> bool:
    return flag_path.exists()


def assert_research_enabled(flag_path: Path = MAINTENANCE_FLAG) -> None:
    if maintenance_active(flag_path):
        raise MaintenanceActiveError(f"research is paused while maintenance flag exists: {flag_path}")


def write_maintenance_flag(reason: str, maintenance_id: str, flag_path: Path = MAINTENANCE_FLAG) -> None:
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = flag_path.with_name(flag_path.name + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as flag:
        flag.write(f"maintenance_id={maintenance_id}\nreason={reason.strip()}\n")
        flag.flush()
        os.fsync(flag.fileno())
    os.replace(temporary, flag_path)


def clear_maintenance_flag(flag_path: Path = MAINTENANCE_FLAG) -> None:
    flag_path.unlink(missing_ok=True)


def active_research_processes() -> list[dict[str, str]]:
    """Read the process table and report supported writers without stopping them."""
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,args="], capture_output=True, text=True, check=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return [{"pid": "unknown", "command": "process table unavailable"}]
    found = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid, command = parts
        if int(pid) == os.getpid():
            continue
        if any(pattern in command for pattern in _WRITER_PATTERNS):
            found.append({"pid": pid, "command": command})
    return found


def supported_writer_compatibility(root: Path = ROOT) -> dict[str, bool]:
    status = {}
    for name in _PYTHON_WRITERS:
        path = root / name
        status[name] = path.is_file() and "assert_research_enabled" in path.read_text(encoding="utf-8")
    for name, marker in _SUPERVISORS.items():
        path = root / name
        status[name] = path.is_file() and marker in path.read_text(encoding="utf-8")
    return status


def lifecycle_writer(name: str) -> Callable[[F], F]:
    """Hold a durable writer lease over one public lifecycle entrypoint."""
    def decorate(function: F) -> F:
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            lifecycle = kwargs.get("lifecycle")
            if lifecycle is None:
                return function(*args, **kwargs)
            with lifecycle.writer_session(name):
                return function(*args, **kwargs)
        return wrapped  # type: ignore[return-value]
    return decorate
