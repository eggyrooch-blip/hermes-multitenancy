"""CRIT-2 regression (audit 2026-07-03): the isolated cron-job subprocess must
be bounded by a timeout AND, on timeout, the whole job tree must be killed —
not just the direct wrapper. Otherwise one hung job (or a hung tool/agent
grandchild holding the captured pipes) blocks its worker thread and holds the
profile tick-lock forever; a handful exhaust the ThreadPoolExecutor and halt
cron delivery for all ~1259 profiles until restart.

These FAIL on pre-fix code (no timeout, no process-group kill).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from hermes_multitenancy import cron_worker


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class _FakePopen:
    """Stands in for subprocess.Popen: records communicate() timeouts, can raise
    TimeoutExpired on the first communicate to exercise the timeout path."""

    def __init__(self, *, timeout_first=False, stdout='{"success": true}', returncode=0):
        self.pid = 424242
        self._timeout_first = timeout_first
        self._stdout = stdout
        self.returncode = returncode
        self.communicate_timeouts: list = []

    def communicate(self, input=None, timeout=None):
        self.communicate_timeouts.append(timeout)
        if self._timeout_first and len(self.communicate_timeouts) == 1:
            raise subprocess.TimeoutExpired(cmd="python", timeout=timeout)
        return (self._stdout, "")


def test_timeout_seconds_default_env_and_bounds(monkeypatch):
    monkeypatch.delenv("HERMES_CRON_JOB_TIMEOUT", raising=False)
    assert cron_worker._cron_job_timeout_seconds() == 1800.0

    monkeypatch.setenv("HERMES_CRON_JOB_TIMEOUT", "120")
    assert cron_worker._cron_job_timeout_seconds() == 120.0

    for bad in ("not-a-number", "-5", "inf", "1e309", "nan"):  # invalid/non-finite → default
        monkeypatch.setenv("HERMES_CRON_JOB_TIMEOUT", bad)
        assert cron_worker._cron_job_timeout_seconds() == 1800.0

    # huge-but-finite must be clamped to the ceiling, not left to disable the watchdog
    monkeypatch.setenv("HERMES_CRON_JOB_TIMEOUT", "1e308")
    assert cron_worker._cron_job_timeout_seconds() == float(cron_worker._CRON_JOB_TIMEOUT_MAX_SECONDS)


def test_subprocess_runs_in_new_session_bounded_by_timeout(monkeypatch, tmp_path):
    fake = _FakePopen()
    captured: dict = {}

    def fake_popen(*a, **k):
        captured.update(k)
        return fake

    monkeypatch.setattr(cron_worker.subprocess, "Popen", fake_popen)
    cron_worker._run_job_for_profile_subprocess(tmp_path, {"id": "j1", "name": "n"})

    assert captured.get("start_new_session") is True, "child must lead its own process group"
    assert fake.communicate_timeouts[0] is not None and fake.communicate_timeouts[0] > 0


def test_timeout_kills_process_group_and_returns_failure(monkeypatch, tmp_path):
    """On timeout the WHOLE job tree is killed (process group), and a failure
    result is returned so the future completes and the tick-lock releases."""
    fake = _FakePopen(timeout_first=True)
    monkeypatch.setattr(cron_worker.subprocess, "Popen", lambda *a, **k: fake)

    killed: dict = {}
    monkeypatch.setattr(cron_worker, "_kill_cron_job_process_group",
                        lambda p: killed.__setitem__("called", True))

    result = cron_worker._run_job_for_profile_subprocess(tmp_path, {"id": "j1", "name": "nightly"})

    assert killed.get("called") is True  # not just the direct child
    assert result["success"] is False
    assert "timeout" in (result.get("error") or "").lower()


def test_kill_process_group_targets_the_group(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(cron_worker.os, "getpgid", lambda pid: 999)
    monkeypatch.setattr(cron_worker.os, "killpg", lambda pgid, sig: calls.update(pgid=pgid, sig=sig))

    cron_worker._kill_cron_job_process_group(SimpleNamespace(pid=123, kill=lambda: None))

    assert calls["pgid"] == 999
    assert calls["sig"] == cron_worker.signal.SIGKILL


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups only")
def test_kill_process_group_kills_real_descendant(tmp_path):
    """Real-process proof (not mocked): a group leader spawns a grandchild;
    killing the group must take BOTH down — a naive direct-child kill would
    leave the grandchild holding the captured pipes and hang the worker."""
    pidfile = tmp_path / "gc.pid"
    leader_src = (
        "import subprocess, sys, time\n"
        "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open(sys.argv[1], 'w').write(str(gc.pid))\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", leader_src, str(pidfile)],
        start_new_session=True,
    )
    try:
        for _ in range(600):  # wait for the grandchild pid to land
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.05)
        gc_pid = int(pidfile.read_text().strip())
        assert _pid_alive(gc_pid), "grandchild should be running before the kill"

        cron_worker._kill_cron_job_process_group(proc)
        proc.wait(timeout=5)

        deadline = time.time() + 3
        while time.time() < deadline and _pid_alive(gc_pid):
            time.sleep(0.05)
        assert not _pid_alive(gc_pid), "grandchild survived — process-group kill failed"
    finally:
        cron_worker._kill_cron_job_process_group(proc)  # belt-and-suspenders cleanup
