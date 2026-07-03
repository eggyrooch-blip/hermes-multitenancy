"""HIGH-2 regression (audit 2026-07-03): the L4 reauth-defer while-loop must make
forward progress. `clear_needs_reauth_marker` used to swallow a failed unlink and
return None while the caller set `cleared=True` unconditionally — so a marker that
can't be deleted (root-owned / read-only FS) is re-found every pass and the loop
spins at 100% CPU inside the cron subprocess forever.

FAILS on pre-fix code (loop never terminates for an undeletable marker).
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from hermes_multitenancy import credential_renewal_common as crc
from hermes_multitenancy import cron_worker as cw


def test_clear_returns_true_when_removed_or_absent(tmp_path):
    m = tmp_path / "marker"
    m.write_text("{}")
    assert crc.clear_needs_reauth_marker(m) is True
    assert not m.exists()
    assert crc.clear_needs_reauth_marker(m) is True  # already absent → still True


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses directory permissions")
def test_clear_returns_false_when_unlink_fails(tmp_path):
    d = tmp_path / "ro"
    d.mkdir()
    m = d / "marker"
    m.write_text("{}")
    os.chmod(d, 0o500)  # r-x, no write → child cannot be unlinked
    try:
        assert crc.clear_needs_reauth_marker(m) is False
        assert m.exists()  # marker physically remains
    finally:
        os.chmod(d, 0o700)  # restore so pytest tmp cleanup works


def test_l4_loop_terminates_when_marker_cannot_be_cleared(monkeypatch, tmp_path):
    """A non-authoritative marker that keeps failing to clear must NOT spin the
    while-loop: the pass finds no forward progress and returns (defer skipped)."""
    marker = tmp_path / "m"
    calls = {"find": 0}

    def fake_find(shared, oid):
        calls["find"] += 1
        if calls["find"] > 5:
            raise AssertionError("L4 while-loop is spinning — no forward progress")
        return marker

    monkeypatch.setattr(cw, "_resolve_shared_home", lambda: tmp_path)
    monkeypatch.setattr(cw, "find_marker_for_open_id", fake_find)
    monkeypatch.setattr(cw, "_clear_stale_reauth_markers_if_uat_recovered", lambda *a, **k: False)
    monkeypatch.setattr(cw, "read_needs_reauth_marker", lambda p: {"reason": "refresh_diagnostic"})
    monkeypatch.setattr(cw, "marker_requires_reauth", lambda body: False)  # non-authoritative
    monkeypatch.setattr(cw, "_iter_reauth_markers_for_open_id", lambda s, o: [marker])
    monkeypatch.setattr(cw, "preserve_reauth_marker_as_refresh_diagnostic", lambda *a, **k: None)
    monkeypatch.setattr(cw, "clear_needs_reauth_marker", lambda p: False)  # unlink keeps failing

    result = cw._l4_check_needs_reauth_and_defer({"owner_open_id": "ou_x", "id": "j1"})

    assert result is None          # deferral skipped → job proceeds, not wedged
    assert calls["find"] == 1      # loop exited after ONE pass — no spin
