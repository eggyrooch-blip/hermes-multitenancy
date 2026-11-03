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
    probe_unit_exec_paths,
    probe_zombie_tasks,
    run_all_probes,
    UNIT_EXEC_DETAIL_MAX,
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
    """Create a minimal multitenancy DB with the production billing table."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE multitenancy_billing_identities ("
        "employee_user_id TEXT PRIMARY KEY NOT NULL, "
        "profile_name TEXT NOT NULL DEFAULT '', "
        "email TEXT NOT NULL, "
        "litellm_user_id TEXT NOT NULL, "
        "team_id TEXT NOT NULL DEFAULT '', "
        "team_alias TEXT NOT NULL DEFAULT '', "
        "key_id TEXT NOT NULL DEFAULT '', "
        "credential_version INTEGER NOT NULL DEFAULT 0, "
        "expires_at INTEGER NOT NULL DEFAULT 0, "
        "migration_state TEXT NOT NULL DEFAULT 'legacy', "
        "created_at INTEGER NOT NULL, "
        "updated_at INTEGER NOT NULL)"
    )
    if billing_identities:
        for bi in billing_identities:
            conn.execute(
                "INSERT INTO multitenancy_billing_identities "
                "(employee_user_id, email, litellm_user_id, key_id, migration_state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    bi["user_id"],
                    f"{bi['user_id']}@example.com",
                    bi.get("litellm_user_id", "llm-" + bi["user_id"]),
                    bi.get("key_id", "sk-xxx"),
                    bi.get("migration_state", "enforced"),
                    int(time.time()),
                    int(time.time()),
                ),
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
    """All keys present and enforced → pass."""
    db = tmp_path / "multitenancy.db"
    _make_multitenancy_db(db, [
        {"user_id": "u1", "key_id": "sk-xxx", "migration_state": "enforced"},
        {"user_id": "u2", "key_id": "sk-yyy", "migration_state": "enforced"},
    ])
    result = probe_billing_drift(db, threshold=5)
    assert result.status == "pass"
    assert result.value == 0


def test_billing_drift_alert(tmp_path):
    """Many identities without keys → alert."""
    db = tmp_path / "multitenancy.db"
    _make_multitenancy_db(db, [
        {"user_id": f"u{i}", "key_id": "", "migration_state": "enforced"}
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
    _make_multitenancy_db(mt_db, [{"user_id": "u1", "key_id": "sk-x", "migration_state": "enforced"}])
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


# ---------------------------------------------------------------------------
# Probe 6: unit_exec_paths
# ---------------------------------------------------------------------------


def _make_unit_dir(tmp_path: Path) -> Path:
    unit_dir = tmp_path / "systemd-user"
    unit_dir.mkdir()
    return unit_dir


def test_unit_exec_paths_dead_path_alerts(tmp_path):
    """A unit whose ExecStart argv[0] doesn't exist → alert, detail names it."""
    unit_dir = _make_unit_dir(tmp_path)
    (unit_dir / "broken.service").write_text(
        "[Service]\nExecStart=/nonexistent/venv/bin/python -m foo bar\n"
    )

    r = probe_unit_exec_paths(unit_dir)
    assert r.status == "alert"
    assert r.value == 1.0
    assert "broken.service → /nonexistent/venv/bin/python" in r.detail


def test_unit_exec_paths_all_alive_passes(tmp_path):
    """Units (incl. drop-in ExecStartPre) pointing at real executables → pass."""
    unit_dir = _make_unit_dir(tmp_path)
    exe = tmp_path / "bin" / "runner"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)

    exe2 = tmp_path / "bin" / "pre-runner"
    exe2.write_text("#!/bin/sh\n")
    exe2.chmod(0o755)

    (unit_dir / "good.service").write_text(f"[Service]\nExecStart={exe} --flag\n")
    dropin = unit_dir / "good.service.d"
    dropin.mkdir()
    (dropin / "10-pre.conf").write_text(f"[Service]\nExecStartPre={exe2} pre\n")

    r = probe_unit_exec_paths(unit_dir)
    assert r.status == "pass"
    assert r.value == 0.0
    assert "checked 2 exec paths" in r.detail


def test_unit_exec_paths_prefixes_and_relative_skipped(tmp_path):
    """systemd prefixes are stripped; non-absolute argv[0] and reset lines are skipped."""
    unit_dir = _make_unit_dir(tmp_path)
    exe = tmp_path / "ok.sh"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)

    (unit_dir / "mixed.service").write_text(
        "[Service]\n"
        f"ExecStartPre=-@{exe} probe\n"   # prefixes stripped → alive
        "ExecStart=node server.js\n"      # relative → $PATH lookup, skipped
        "ExecStop=\n"                     # drop-in reset syntax, skipped
        "ExecReload=!/missing/reloader\n"  # prefix stripped → dead
        "ExecSearchPath=/some/dir:/other/dir\n"  # directory list, not a command → skipped
        "ExecStart=%h/bin/tool run\n"           # specifier → skipped, not expanded
    )

    r = probe_unit_exec_paths(unit_dir)
    assert r.status == "alert"
    assert r.value == 1.0
    assert "mixed.service → /missing/reloader" in r.detail
    assert str(exe) not in r.detail


def test_unit_exec_paths_missing_dir_passes(tmp_path):
    """No unit dir at all (fresh host) → pass, not a crash."""
    r = probe_unit_exec_paths(tmp_path / "nope")
    assert r.status == "pass"


def test_run_all_probes_includes_unit_dir_when_given(tmp_path):
    """unit_dir opt-in: omitted → 5 results (back-compat); given → 6th probe present."""
    kanban_db = tmp_path / "kanban.db"
    mt_db = tmp_path / "multitenancy.db"
    log = tmp_path / "gateway.log"
    now = int(time.time())
    _make_kanban_db(kanban_db, [{"id": "t1", "status": "done", "created_at": now}])
    _make_multitenancy_db(mt_db, [{"user_id": "u1", "key_id": "sk-x", "migration_state": "enforced"}])
    _write_log(log, [f"{_ts(10)} INFO gateway: ok"])
    unit_dir = _make_unit_dir(tmp_path)

    five = run_all_probes(gateway_log_paths=[log], kanban_db_path=kanban_db, multitenancy_db_path=mt_db)
    six = run_all_probes(
        gateway_log_paths=[log], kanban_db_path=kanban_db,
        multitenancy_db_path=mt_db, unit_dir=unit_dir,
    )
    assert len(five) == 5
    assert [r.name for r in six][-1] == "unit_exec_paths"
    assert six[-1].status == "pass"


def test_unit_exec_paths_quoted_and_continued_lines(tmp_path):
    """Quoted argv[0] and backslash-continued lines are real systemd syntax —
    both must be tokenized, dead and alive alike (review P1: naive tokenization)."""
    unit_dir = _make_unit_dir(tmp_path)
    exe = tmp_path / "spaced dir" / "runner"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)

    (unit_dir / "quoted-live.service").write_text(
        f'[Service]\nExecStart="{exe}" --flag\n'
    )
    (unit_dir / "quoted-dead.service").write_text(
        '[Service]\nExecStart="/definitely/missing/python" -m foo\n'
    )
    (unit_dir / "continued-dead.service").write_text(
        "[Service]\nExecStart=\\\n  /also/missing/tool \\\n  --opt\n"
    )
    (unit_dir / "prefixed-quoted-dead.service").write_text(
        '[Service]\nExecStart=-"/missing/prefixed"\n'
    )

    r = probe_unit_exec_paths(unit_dir)
    assert r.status == "alert"
    assert r.value == 3.0
    assert "quoted-dead.service → /definitely/missing/python" in r.detail
    assert "continued-dead.service → /also/missing/tool" in r.detail
    assert "prefixed-quoted-dead.service → /missing/prefixed" in r.detail
    assert str(exe) not in r.detail


def test_unit_exec_paths_unreadable_unit_alerts(tmp_path):
    """An unreadable unit may hide a dead executable — fail closed, not open
    (review P1: scan-errors-fail-open)."""
    if os.geteuid() == 0:
        pytest.skip("root reads through 0o000")
    unit_dir = _make_unit_dir(tmp_path)
    secret = unit_dir / "secret.service"
    secret.write_text("[Service]\nExecStart=/definitely/missing/python\n")
    secret.chmod(0o000)
    try:
        r = probe_unit_exec_paths(unit_dir)
    finally:
        secret.chmod(0o644)

    assert r.status == "alert"
    assert "secret.service → unreadable" in r.detail


def test_unit_exec_paths_dropin_owner_named(tmp_path):
    """Drop-ins report their owning unit; identical basename+path across units
    must not collapse (review P1: drop-in-owner-identity-lost)."""
    unit_dir = _make_unit_dir(tmp_path)
    for unit in ("alpha.service", "beta.service"):
        d = unit_dir / f"{unit}.d"
        d.mkdir()
        (d / "override.conf").write_text("[Service]\nExecStart=/missing/shared\n")

    r = probe_unit_exec_paths(unit_dir)
    assert r.value == 2.0
    assert "alpha.service → /missing/shared" in r.detail
    assert "beta.service → /missing/shared" in r.detail
    assert "override.conf" not in r.detail


def test_unit_exec_paths_detail_capped(tmp_path):
    """Dead-entry detail is bounded with an omitted-count suffix
    (review P1: unbounded-unit-input-and-alert-detail)."""
    unit_dir = _make_unit_dir(tmp_path)
    lines = "".join(f"ExecStartPre=/missing/bin{i}\n" for i in range(30))
    (unit_dir / "many.service").write_text("[Service]\n" + lines)

    r = probe_unit_exec_paths(unit_dir)
    assert r.status == "alert"
    assert r.value == 30.0
    assert r.detail.count(";") == UNIT_EXEC_DETAIL_MAX - 1
    assert "+10 more" in r.detail


def test_unit_exec_paths_oversized_file_fails_closed(tmp_path, monkeypatch):
    """A file past the read bound is flagged as dead, NOT silently truncated —
    a late dead ExecStart must never yield pass (review P1 round 2)."""
    import health_probes

    monkeypatch.setattr(health_probes, "UNIT_EXEC_MAX_BYTES", 64)
    unit_dir = _make_unit_dir(tmp_path)
    (unit_dir / "huge.service").write_text(
        "[Service]\n" + "# pad\n" * 50 + "ExecStart=/missing/late\n"
    )

    r = health_probes.probe_unit_exec_paths(unit_dir)
    assert r.status == "alert"
    assert "huge.service → oversized" in r.detail
    assert "/missing/late" not in r.detail


def test_unit_exec_paths_systemd_escapes_decoded(tmp_path):
    """C-style escapes (\\s = space) decode in quoted AND unquoted tokens;
    embedded quotes concatenate (review P1 round 2: naive tokenization)."""
    unit_dir = _make_unit_dir(tmp_path)
    exe = tmp_path / "sp ace" / "runner"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    escaped = str(exe).replace(" ", "\\s")

    (unit_dir / "escaped-live.service").write_text(
        f"[Service]\nExecStart={escaped} --flag\n"
    )
    (unit_dir / "escaped-dead.service").write_text(
        "[Service]\nExecStart=/missing/dir\\swith/binary --x\n"
    )
    (unit_dir / "quoted-escape-live.service").write_text(
        f'[Service]\nExecStart="{escaped}" --flag\n'
    )
    (unit_dir / "embedded-quote-live.service").write_text(
        f'[Service]\nExecStart={exe.parent.parent}/"sp ace"/runner --flag\n'
    )

    r = probe_unit_exec_paths(unit_dir)
    assert r.status == "alert"
    assert r.value == 1.0
    assert "escaped-dead.service → /missing/dir with/binary" in r.detail
    assert "escaped-live" not in r.detail
    assert "quoted-escape-live" not in r.detail
    assert "embedded-quote-live" not in r.detail


def test_unit_exec_paths_scan_error_alerts(tmp_path, monkeypatch):
    """Directory enumeration failing (permissions/IO) alerts instead of raising
    (review P1 round 2: scan-errors-fail-open)."""
    unit_dir = _make_unit_dir(tmp_path)

    def boom(self, pattern):
        raise PermissionError("scan denied")

    monkeypatch.setattr(type(unit_dir), "glob", boom)
    r = probe_unit_exec_paths(unit_dir)
    assert r.status == "alert"
    assert "unit scan → unreadable" in r.detail


def test_unit_exec_paths_too_many_files_fails_closed(tmp_path, monkeypatch):
    """Past the file-count bound the rest is flagged unscanned, never silently
    skipped (review P1 round 2: unbounded input)."""
    import health_probes

    monkeypatch.setattr(health_probes, "UNIT_EXEC_MAX_FILES", 2)
    unit_dir = _make_unit_dir(tmp_path)
    for i in range(3):
        (unit_dir / f"u{i}.service").write_text("[Service]\nExecStart=/bin/ls\n")

    r = health_probes.probe_unit_exec_paths(unit_dir)
    assert r.status == "alert"
    assert "3 files exceeds 2" in r.detail
