"""Tests for the five health-check probes (deploy/health_probes.py).

Each test drives the real probe function against a temp fixture and verifies
the pass/alert decision, measured value, and threshold.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

# Import the probes module from the deploy directory
DEPLOY_DIR = Path(__file__).parent.parent / "deploy"
sys.path.insert(0, str(DEPLOY_DIR))

from health_probes import (  # noqa: E402
    probe_api_error_rate,
    probe_billing_drift,
    probe_notify_failures,
    probe_queue_backlog,
    probe_zombie_tasks,
    run_all_probes,
    format_alert_text,
    ProbeResult,
)


# ---------------------------------------------------------------------------
# Helpers: create temp DBs with realistic schema
# ---------------------------------------------------------------------------


def _make_kanban_db(db_path: Path, tasks: list[dict] | None = None) -> None:
    """Create a minimal kanban DB with the tasks table for probe testing."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE tasks ("
        "id TEXT PRIMARY KEY, title TEXT, status TEXT, "
        "priority INTEGER DEFAULT 0, created_at INTEGER, "
        "last_heartbeat_at INTEGER)"
    )
    if tasks:
        for t in tasks:
            conn.execute(
                "INSERT INTO tasks (id, title, status, priority, created_at, last_heartbeat_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    t.get("id", "t1"),
                    t.get("title", "test"),
                    t.get("status", "done"),
                    t.get("priority", 0),
                    t.get("created_at", int(time.time())),
                    t.get("last_heartbeat_at"),
                ),
            )
    conn.commit()
    conn.close()


def _make_multitenancy_db(
    db_path: Path,
    billing_identities: list[dict] | None = None,
) -> None:
    """Create a minimal multitenancy DB with billing_identity table."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE billing_identity ("
        "id INTEGER PRIMARY KEY, "
        "user_id TEXT NOT NULL, "
        "key_value TEXT, "
        "status TEXT NOT NULL DEFAULT 'active')"
    )
    if billing_identities:
        for bi in billing_identities:
            conn.execute(
                "INSERT INTO billing_identity (user_id, key_value, status) "
                "VALUES (?, ?, ?)",
                (bi["user_id"], bi.get("key_value"), bi.get("status", "active")),
            )
    conn.commit()
    conn.close()


def _write_log(log_path: Path, lines: list[str]) -> None:
    """Write lines to a log file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as f:
        for line in lines:
            f.write(line + "\n")


def _ts(seconds_ago: float = 0) -> str:
    """Return a timestamp string for N seconds ago."""
    t = time.localtime(time.time() - seconds_ago)
    return time.strftime("%Y-%m-%d %H:%M:%S", t)


# ---------------------------------------------------------------------------
# Probe 1: API error rate
# ---------------------------------------------------------------------------


def test_api_error_rate_healthy(tmp_path):
    """Low error rate → pass."""
    log = tmp_path / "gateway.log"
    _write_log(log, [
        f"{_ts(10)} INFO gateway: request ok",
        f"{_ts(10)} INFO gateway: request ok",
        f"{_ts(10)} INFO gateway: request ok",
        f"{_ts(10)} INFO gateway: request ok",
        f"{_ts(10)} INFO gateway: request ok",
        f"{_ts(10)} INFO gateway: request ok",
        f"{_ts(10)} INFO gateway: request ok",
        f"{_ts(10)} INFO gateway: request ok",
        f"{_ts(10)} INFO gateway: request ok",
        f"{_ts(10)} INFO gateway: request ok",
        f"{_ts(10)} ERROR gateway: timeout",
    ])
    result = probe_api_error_rate([log], window_seconds=300, threshold=0.10)
    assert result.status == "pass"
    assert result.name == "api_error_rate"
    assert result.value == pytest.approx(1 / 11, abs=0.01)


def test_api_error_rate_alert(tmp_path):
    """High error rate → alert."""
    log = tmp_path / "gateway.log"
    _write_log(log, [
        f"{_ts(10)} INFO gateway: request ok",
        f"{_ts(10)} ERROR gateway: timeout",
        f"{_ts(10)} CRITICAL gateway: crash",
    ])
    result = probe_api_error_rate([log], window_seconds=300, threshold=0.10)
    assert result.status == "alert"
    assert result.value == pytest.approx(2 / 3, abs=0.01)


# ---------------------------------------------------------------------------
# Probe 2: Queue backlog
# ---------------------------------------------------------------------------


def test_queue_backlog_healthy(tmp_path):
    """Few stale tasks → pass."""
    db = tmp_path / "kanban.db"
    now = int(time.time())
    _make_kanban_db(db, [
        {"id": "t1", "status": "done", "created_at": now - 3600},
        {"id": "t2", "status": "todo", "created_at": now - 10},  # fresh, not stale
    ])
    result = probe_queue_backlog(db, threshold=20)
    assert result.status == "pass"
    assert result.value == 0  # only the fresh todo counts; done is excluded


def test_queue_backlog_alert(tmp_path):
    """Many stale tasks → alert."""
    db = tmp_path / "kanban.db"
    now = int(time.time())
    tasks = [
        {"id": f"t{i}", "status": "todo", "created_at": now - 3600}
        for i in range(25)
    ]
    _make_kanban_db(db, tasks)
    result = probe_queue_backlog(db, threshold=20)
    assert result.status == "alert"
    assert result.value == 25


# ---------------------------------------------------------------------------
# Probe 3: Zombie tasks
# ---------------------------------------------------------------------------


def test_zombie_tasks_healthy(tmp_path):
    """No stale heartbeats → pass."""
    db = tmp_path / "kanban.db"
    now = int(time.time())
    _make_kanban_db(db, [
        {"id": "t1", "status": "running", "last_heartbeat_at": now - 60},
        {"id": "t2", "status": "done", "last_heartbeat_at": None},
    ])
    result = probe_zombie_tasks(db, threshold=0)
    assert result.status == "pass"
    assert result.value == 0


def test_zombie_tasks_alert(tmp_path):
    """Stale heartbeat on running task → alert."""
    db = tmp_path / "kanban.db"
    now = int(time.time())
    _make_kanban_db(db, [
        {"id": "t1", "status": "running", "last_heartbeat_at": now - 900},
        {"id": "t2", "status": "claimed", "last_heartbeat_at": now - 1200},
    ])
    result = probe_zombie_tasks(db, threshold=0, heartbeat_timeout=600)
    assert result.status == "alert"
    assert result.value == 2


# ---------------------------------------------------------------------------
# Probe 4: Notification failures
# ---------------------------------------------------------------------------


def test_notify_failures_healthy(tmp_path):
    """Few failures → pass."""
    log = tmp_path / "gateway.log"
    _write_log(log, [
        f"{_ts(10)} INFO gateway: notification sent",
        f"{_ts(10)} WARNING gateway: delivery error (retrying)",
    ])
    result = probe_notify_failures([log], threshold=3)
    assert result.status == "pass"
    assert result.value == 1


def test_notify_failures_alert(tmp_path):
    """Many failures → alert."""
    log = tmp_path / "gateway.log"
    _write_log(log, [
        f"{_ts(10)} ERROR gateway: delivery error",
        f"{_ts(10)} ERROR gateway: send failed",
        f"{_ts(10)} ERROR gateway: notify fail: timeout",
        f"{_ts(10)} ERROR gateway: notify error: connection refused",
    ])
    result = probe_notify_failures([log], threshold=3)
    assert result.status == "alert"
    assert result.value == 4


# ---------------------------------------------------------------------------
# Probe 5: Billing drift
# ---------------------------------------------------------------------------


def test_billing_drift_healthy(tmp_path):
    """All keys present → pass."""
    db = tmp_path / "multitenancy.db"
    _make_multitenancy_db(db, [
        {"user_id": "u1", "key_value": "sk-xxx", "status": "active"},
        {"user_id": "u2", "key_value": "sk-yyy", "status": "active"},
    ])
    result = probe_billing_drift(db, threshold=5)
    assert result.status == "pass"
    assert result.value == 0


def test_billing_drift_alert(tmp_path):
    """Many identities without keys → alert."""
    db = tmp_path / "multitenancy.db"
    _make_multitenancy_db(db, [
        {"user_id": f"u{i}", "key_value": None, "status": "active"}
        for i in range(8)
    ])
    result = probe_billing_drift(db, threshold=5)
    assert result.status == "alert"
    assert result.value == 8


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def test_run_all_probes_healthy(tmp_path):
    """All healthy fixtures → all pass."""
    kanban_db = tmp_path / "kanban.db"
    mt_db = tmp_path / "multitenancy.db"
    log = tmp_path / "gateway.log"
    now = int(time.time())

    _make_kanban_db(kanban_db, [{"id": "t1", "status": "done", "created_at": now}])
    _make_multitenancy_db(mt_db, [{"user_id": "u1", "key_value": "sk-x", "status": "active"}])
    _write_log(log, [f"{_ts(10)} INFO gateway: ok"])

    results = run_all_probes(
        gateway_log_paths=[log],
        kanban_db_path=kanban_db,
        multitenancy_db_path=mt_db,
    )
    assert len(results) == 5
    assert all(r.status == "pass" for r in results), [
        (r.name, r.status) for r in results
    ]


def test_format_alert_text():
    """Alert text formatting includes key fields."""
    r = ProbeResult("test_probe", "alert", 42.0, 10.0, "something broke")
    text = format_alert_text(r, host="hermes-1")
    assert "test_probe" in text
    assert "ALERT" in text
    assert "42.0" in text
    assert "hermes-1" in text
    assert "something broke" in text
