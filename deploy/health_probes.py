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
# Probe 6: systemd user-unit Exec* paths
# ---------------------------------------------------------------------------

#: Any Exec* line whose absolute argv[0] is missing/non-executable alerts.
UNIT_EXEC_THRESHOLD = 0
#: Alert detail keeps at most this many entries (Feishu payload / argv budget).
UNIT_EXEC_DETAIL_MAX = 20
#: Per-entry display bound inside detail (paths are ~100 chars in practice).
UNIT_EXEC_ENTRY_MAX = 256
#: Per-file read bound. Unit files are KBs; a bigger file is flagged, not scanned.
UNIT_EXEC_MAX_BYTES = 1_000_000
#: Directory bound. Past this we flag and stop — keeps seen/dead bounded.
UNIT_EXEC_MAX_FILES = 1000

# 只匹配「值是命令行」的 Exec 指令；ExecSearchPath= 的值是目录列表，会假红。
_EXEC_LINE_PATTERN = re.compile(
    r"^Exec(?:Start|StartPre|StartPost|Condition|Reload|Stop|StopPost)\s*=\s*(.*)$"
)


def _unit_logical_lines(text: str):
    """systemd joins lines ending with ``\\`` into one logical line."""
    buf: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.endswith("\\"):
            buf.append(line[:-1].strip())
            continue
        if buf:
            buf.append(line)
            line = " ".join(part for part in buf if part)
            buf = []
        yield line
    if buf:
        yield " ".join(part for part in buf if part)


#: systemd C-style escapes we decode (systemd.syntax(7)); unknown escapes keep
#: the escaped char literally, matching "best effort, never crash".
_EXEC_ESCAPES = {"s": " ", "t": "\t", "n": "\n", "r": "\r", "\\": "\\", '"': '"', "'": "'"}


def _exec_argv0(rest: str) -> str:
    """First token of an Exec command line, decoded like systemd would.

    Strips executable prefixes (``@-:+!``), then walks a small state machine:
    double/single quotes may wrap the whole token or part of it, C-style
    escapes (``\\s`` → space, ``\\t``, ``\\xHH``, …) are decoded inside and
    outside quotes, and an unquoted token ends at the first unescaped
    whitespace.
    """
    rest = rest.lstrip()
    i = 0
    while i < len(rest) and rest[i] in "@-:+!":
        i += 1
    out: list[str] = []
    quote = ""
    j = i
    while j < len(rest):
        c = rest[j]
        if c == "\\" and j + 1 < len(rest):
            nxt = rest[j + 1]
            if nxt == "x" and j + 3 < len(rest):
                try:
                    out.append(chr(int(rest[j + 2 : j + 4], 16)))
                    j += 4
                    continue
                except ValueError:
                    pass
            out.append(_EXEC_ESCAPES.get(nxt, nxt))
            j += 2
            continue
        if quote:
            if c == quote:
                quote = ""
            else:
                out.append(c)
        elif c in "\"'":
            quote = c
        elif c.isspace():
            break
        else:
            out.append(c)
        j += 1
    return "".join(out)


def _owning_unit(f: Path) -> str:
    """``foo.service.d/override.conf`` belongs to ``foo.service``."""
    return f.parent.name[:-2] if f.parent.name.endswith(".service.d") else f.name


def probe_unit_exec_paths(
    unit_dir: Path,
    *,
    threshold: float = UNIT_EXEC_THRESHOLD,
) -> ProbeResult:
    """Check that every user unit's Exec* argv[0] points at a real executable.

    2026-08-15: hermes-lark-skill-sync.service 的 ExecStart 指向 release 换布局后
    消失的 venv python，203/EXEC 静默失败数周才被发现。这里扫 ``*.service`` 与
    ``*.service.d/*.conf`` 的每条 ``Exec*=`` 行，取 argv[0]（剥掉 systemd 的
    ``@-:+!`` 前缀）；绝对路径必须存在且可执行，非绝对路径（走 $PATH 解析）跳过。
    只读探测：open/stat，绝无写入。
    """
    dead: list[str] = []
    seen: set[tuple[str, str]] = set()
    checked = 0
    # fail closed：列目录本身炸了（权限/IO）也必须变成告警，而不是探针崩掉
    # 只留一行本地日志——那正是 203/EXEC 静默数周的形状
    try:
        if not unit_dir.is_dir():
            return ProbeResult("unit_exec_paths", "pass", 0, threshold, "unit dir not found")
        files = sorted(unit_dir.glob("*.service")) + sorted(unit_dir.glob("*.service.d/*.conf"))
    except OSError as e:
        return ProbeResult(
            "unit_exec_paths", "alert", 1.0, threshold,
            f"unit scan → unreadable ({e.__class__.__name__})",
        )
    if len(files) > UNIT_EXEC_MAX_FILES:
        dead.append(f"unit scan → {len(files)} files exceeds {UNIT_EXEC_MAX_FILES}, rest unscanned")
        files = files[:UNIT_EXEC_MAX_FILES]
    for f in files:
        unit = _owning_unit(f)
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                text = fh.read(UNIT_EXEC_MAX_BYTES + 1)
        except OSError:
            # fail closed：读不了的单元可能正藏着死路径，必须告警而非跳过
            key = (unit, "<unreadable>")
            if key not in seen:
                seen.add(key)
                dead.append(f"{unit} → unreadable")
            continue
        if len(text) > UNIT_EXEC_MAX_BYTES:
            # fail closed：超界文件截断后扫，死路径可能恰好在截断点之后
            dead.append(f"{unit} → oversized (>{UNIT_EXEC_MAX_BYTES} bytes, unscanned)")
            continue
        for line in _unit_logical_lines(text):
            m = _EXEC_LINE_PATTERN.match(line)
            if not m:
                continue
            argv0 = _exec_argv0(m.group(1))
            if not argv0:
                continue  # `ExecStart=` 置空是 drop-in 重置语法，不是路径
            if not argv0.startswith("/") or "%" in argv0:
                continue  # $PATH 解析 / systemd specifier，本探针不展开
            key = (unit, argv0)
            if key in seen:
                continue
            seen.add(key)
            checked += 1
            if not (os.path.isfile(argv0) and os.access(argv0, os.X_OK)):
                shown_path = argv0 if len(argv0) <= UNIT_EXEC_ENTRY_MAX else argv0[:UNIT_EXEC_ENTRY_MAX] + "…"
                dead.append(f"{unit} → {shown_path}")

    status = "alert" if len(dead) > threshold else "pass"
    if dead:
        shown = dead[:UNIT_EXEC_DETAIL_MAX]
        omitted = len(dead) - len(shown)
        detail = "; ".join(shown) + (f" … +{omitted} more" if omitted else "")
    else:
        detail = f"checked {checked} exec paths"
    return ProbeResult(
        name="unit_exec_paths",
        status=status,
        value=float(len(dead)),
        threshold=threshold,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Orchestrator: run all probes
# ---------------------------------------------------------------------------


def run_all_probes(
    *,
    gateway_log_paths: list[Path],
    kanban_db_path: Path,
    multitenancy_db_path: Path,
    unit_dir: Optional[Path] = None,
) -> list[ProbeResult]:
    """Run all probes and return results in order.

    ``unit_dir`` is opt-in (None skips probe 6) so existing callers are unchanged.
    """
    results = [
        probe_api_error_rate(gateway_log_paths),
        probe_queue_backlog(kanban_db_path),
        probe_zombie_tasks(kanban_db_path),
        probe_notify_failures(gateway_log_paths),
        probe_billing_drift(multitenancy_db_path),
    ]
    if unit_dir is not None:
        results.append(probe_unit_exec_paths(unit_dir))
    return results


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
    parser.add_argument("--unit-dir", type=Path, help="systemd user-unit dir to check Exec* paths (omit to skip)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    results = run_all_probes(
        gateway_log_paths=args.gateway_log or [],
        kanban_db_path=args.kanban_db or Path("/dev/null"),
        multitenancy_db_path=args.multitenancy_db or Path("/dev/null"),
        unit_dir=args.unit_dir,
    )

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        for r in results:
            print(format_alert_text(r))
