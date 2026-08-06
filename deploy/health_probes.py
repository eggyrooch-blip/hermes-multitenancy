"""Five health-check probes for Hermes production monitoring.

Each probe is a standalone function that takes known paths/parameters and
returns a structured dict:

    {"name", "status", "value", "threshold", "detail"}

where ``status`` is "pass" or "alert".

Design principles:
- Read-only: probes never mutate production state.
- Known-path-only: no ``find /`` — all DB paths are passed in explicitly.
- Self-contained: each probe has its own threshold constant for easy tuning.
- Independently testable: every probe works against a temp DB or temp log
  file, no gateway or systemd needed.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """Structured result from a single health-check probe."""
    name: str
    status: str  # "pass" | "alert"
    value: float
    threshold: float
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "value": self.value,
            "threshold": self.threshold,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Probe 1: API error rate
# ---------------------------------------------------------------------------

#: Fraction of log lines containing ERROR/CRITICAL that counts as "error rate".
API_ERROR_RATE_THRESHOLD = 0.10  # 10%
_API_ERROR_PATTERN = re.compile(
    r"\b(ERROR|CRITICAL)\b", re.IGNORECASE
)


def probe_api_error_rate(
    log_paths: list[Path],
    *,
    window_seconds: int = 300,
    threshold: float = API_ERROR_RATE_THRESHOLD,
    now: Optional[float] = None,
) -> ProbeResult:
    """Check API error rate from gateway log files.

    Scans the last ``window_seconds`` of log entries and computes the fraction
    of lines that contain ERROR or CRITICAL level markers.
    """
    now = now or time.time()
    cutoff = now - window_seconds
    total_lines = 0
    error_lines = 0

    for log_path in log_paths:
        if not log_path.exists():
            continue
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    total_lines += 1
                    # Best-effort timestamp extraction (format: "2026-08-07 10:32:15")
                    ts_match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                    if ts_match:
                        try:
                            line_ts = time.mktime(time.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S"))
                            if line_ts < cutoff:
                                continue  # outside window
                        except ValueError:
                            pass  # keep line if we can't parse timestamp
                    if _API_ERROR_PATTERN.search(line):
                        error_lines += 1
        except OSError:
            continue

    rate = error_lines / total_lines if total_lines > 0 else 0.0
    status = "alert" if rate > threshold else "pass"
    return ProbeResult(
        name="api_error_rate",
        status=status,
        value=round(rate, 4),
        threshold=threshold,
        detail=f"{error_lines} errors in {total_lines} log lines (last {window_seconds}s)"
        if status == "alert"
        else f"{error_lines} errors in {total_lines} lines",
    )


# ---------------------------------------------------------------------------
# Probe 2: Queue backlog
# ---------------------------------------------------------------------------

QUEUE_BACKLOG_THRESHOLD = 20.0


def probe_queue_backlog(
    kanban_db_path: Path,
    *,
    max_age_minutes: int = 30,
    threshold: float = QUEUE_BACKLOG_THRESHOLD,
    now: Optional[float] = None,
) -> ProbeResult:
    """Check for stale tasks stuck in todo/claimed status."""
    now = now or time.time()
    cutoff = int(now - max_age_minutes * 60)
    count = 0

    if not kanban_db_path.exists():
        return ProbeResult("queue_backlog", "pass", 0, threshold, "kanban DB not found")

    try:
        conn = sqlite3.connect(f"file:{kanban_db_path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT COUNT(*) as n FROM tasks "
            "WHERE status IN ('todo', 'claimed', 'scheduled', 'ready') "
            "AND created_at < ?",
            (cutoff,),
        ).fetchone()
        count = row["n"] if row else 0
        conn.close()
    except sqlite3.Error:
        return ProbeResult("queue_backlog", "pass", 0, threshold, "kanban DB unreadable")

    status = "alert" if count > threshold else "pass"
    return ProbeResult(
        name="queue_backlog",
        status=status,
        value=float(count),
        threshold=threshold,
        detail=f"{count} tasks stuck >{max_age_minutes}min in todo/claimed/scheduled/ready"
        if status == "alert"
        else f"{count} stale tasks",
    )


# ---------------------------------------------------------------------------
# Probe 3: Zombie tasks (no heartbeat)
# ---------------------------------------------------------------------------

ZOMBIE_TASK_THRESHOLD = 0.0
HEARTBEAT_TIMEOUT_SECONDS = 600  # 10 minutes


def probe_zombie_tasks(
    kanban_db_path: Path,
    *,
    heartbeat_timeout: int = HEARTBEAT_TIMEOUT_SECONDS,
    threshold: float = ZOMBIE_TASK_THRESHOLD,
    now: Optional[float] = None,
) -> ProbeResult:
    """Check for claimed/running tasks with stale heartbeats."""
    now = now or time.time()
    cutoff = int(now - heartbeat_timeout)
    count = 0

    if not kanban_db_path.exists():
        return ProbeResult("zombie_tasks", "pass", 0, threshold, "kanban DB not found")

    try:
        conn = sqlite3.connect(f"file:{kanban_db_path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT COUNT(*) as n FROM tasks "
            "WHERE status IN ('claimed', 'running') "
            "AND (last_heartbeat_at IS NULL OR last_heartbeat_at < ?)",
            (cutoff,),
        ).fetchone()
        count = row["n"] if row else 0
        conn.close()
    except sqlite3.Error:
        return ProbeResult("zombie_tasks", "pass", 0, threshold, "kanban DB unreadable")

    status = "alert" if count > threshold else "pass"
    return ProbeResult(
        name="zombie_tasks",
        status=status,
        value=float(count),
        threshold=threshold,
        detail=f"{count} tasks with heartbeat >{heartbeat_timeout}s stale"
        if status == "alert"
        else f"{count} zombie tasks",
    )


# ---------------------------------------------------------------------------
# Probe 4: Notification failures
# ---------------------------------------------------------------------------

NOTIFY_FAILURE_THRESHOLD = 3.0
_NOTIFY_FAIL_PATTERN = re.compile(
    r"(delivery error|send failed|notify.*(fail|error))", re.IGNORECASE
)


def probe_notify_failures(
    log_paths: list[Path],
    *,
    window_seconds: int = 300,
    threshold: float = NOTIFY_FAILURE_THRESHOLD,
    now: Optional[float] = None,
) -> ProbeResult:
    """Check for notification delivery failures in gateway logs."""
    now = now or time.time()
    cutoff = now - window_seconds
    failures = 0

    for log_path in log_paths:
        if not log_path.exists():
            continue
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    ts_match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                    if ts_match:
                        try:
                            line_ts = time.mktime(time.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S"))
                            if line_ts < cutoff:
                                continue
                        except ValueError:
                            pass
                    if _NOTIFY_FAIL_PATTERN.search(line):
                        failures += 1
        except OSError:
            continue

    status = "alert" if failures > threshold else "pass"
    return ProbeResult(
        name="notify_failures",
        status=status,
        value=float(failures),
        threshold=threshold,
        detail=f"{failures} notification failures in last {window_seconds}s"
        if status == "alert"
        else f"{failures} notification failures",
    )


# ---------------------------------------------------------------------------
# Probe 5: Billing drift (employees with billing identity but no valid key)
# ---------------------------------------------------------------------------

BILLING_DRIFT_THRESHOLD = 5.0


def probe_billing_drift(
    multitenancy_db_path: Path,
    *,
    threshold: float = BILLING_DRIFT_THRESHOLD,
) -> ProbeResult:
    """Check for employees with billing identity but no active key.

    Queries the production table ``multitenancy_billing_identities`` for rows
    where ``key_id`` is empty (no personal key provisioned) or
    ``migration_state`` is not ``enforced`` (identity not fully activated).

    Until the LiteLLM budget callback 403 fix lands (双周11), this probe
    ships with a high threshold so it detects but doesn't alert.
    """
    if not multitenancy_db_path.exists():
        return ProbeResult("billing_drift", "pass", 0, threshold, "multitenancy DB not found")

    count = 0
    try:
        conn = sqlite3.connect(f"file:{multitenancy_db_path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row

        # Check if billing tables exist
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

        if "multitenancy_billing_identities" in tables:
            # Count employees with billing identity but no key_id, or not yet enforced.
            # Production schema: key_id TEXT (empty string = no key),
            # migration_state TEXT ('enforced' = fully activated).
            row = conn.execute(
                "SELECT COUNT(*) as n FROM multitenancy_billing_identities "
                "WHERE key_id = '' OR key_id IS NULL "
                "OR migration_state != 'enforced'"
            ).fetchone()
            count = row["n"] if row else 0
        else:
            count = 0  # billing not set up yet — not an alert
        conn.close()
    except sqlite3.Error:
        return ProbeResult("billing_drift", "pass", 0, threshold, "multitenancy DB unreadable")

    status = "alert" if count > threshold else "pass"
    return ProbeResult(
        name="billing_drift",
        status=status,
        value=float(count),
        threshold=threshold,
        detail=f"{count} employees with billing identity but no active key"
        if status == "alert"
        else f"{count} billing orphans",
    )


# ---------------------------------------------------------------------------
# Orchestrator: run all probes
# ---------------------------------------------------------------------------


def run_all_probes(
    *,
    gateway_log_paths: list[Path],
    kanban_db_path: Path,
    multitenancy_db_path: Path,
) -> list[ProbeResult]:
    """Run all five probes and return results in order."""
    return [
        probe_api_error_rate(gateway_log_paths),
        probe_queue_backlog(kanban_db_path),
        probe_zombie_tasks(kanban_db_path),
        probe_notify_failures(gateway_log_paths),
        probe_billing_drift(multitenancy_db_path),
    ]


def format_alert_text(result: ProbeResult, host: str = "") -> str:
    """Format a probe result as a Feishu alert message."""
    emoji = "🔴" if result.status == "alert" else "🟢"
    lines = [
        f"{emoji} [{result.name}] {result.status.upper()}",
    ]
    if host:
        lines.append(f"host: {host}")
    lines.append(f"value: {result.value} (threshold: {result.threshold})")
    if result.detail:
        lines.append(f"detail: {result.detail}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Hermes health-check probes")
    parser.add_argument("--kanban-db", type=Path, help="Path to kanban.db")
    parser.add_argument("--multitenancy-db", type=Path, help="Path to multitenancy.db")
    parser.add_argument("--gateway-log", action="append", type=Path, help="Gateway log path (repeatable)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    results = run_all_probes(
        gateway_log_paths=args.gateway_log or [],
        kanban_db_path=args.kanban_db or Path("/dev/null"),
        multitenancy_db_path=args.multitenancy_db or Path("/dev/null"),
    )

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        for r in results:
            print(format_alert_text(r))
