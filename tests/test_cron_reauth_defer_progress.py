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
    marker.touch()
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
    monkeypatch.setattr(cw, "clear_non_actionable_reauth_marker", lambda *a, **k: False)

    result = cw._l4_check_needs_reauth_and_defer({"owner_open_id": "ou_x", "id": "j1"})

    assert result is None          # deferral skipped → job proceeds, not wedged
    # One bounded re-read distinguishes an authoritative replacement from the
    # same undeletable non-actionable marker; it still never loops.
    assert calls["find"] == 2


def test_l4_defers_when_non_actionable_marker_is_replaced_during_cleanup(
    monkeypatch,
    tmp_path,
):
    open_id = "ou_replaced"
    marker = tmp_path / "profiles" / "alice" / "feishu_uat" / f"{open_id}.needs_reauth"
    crc.write_needs_reauth_marker(
        marker,
        reason=crc.REASON_REFRESH_REJECTED,
        detail="transient infrastructure failure",
        extra={"profile": "alice"},
    )

    def replace_with_authoritative(*_args, **_kwargs):
        crc.write_needs_reauth_marker(
            marker,
            reason=crc.REASON_REFRESH_REJECTED,
            detail="Feishu rejected the replacement token",
            extra={
                "profile": "alice",
                "authoritative": True,
                "refresh_class": "invalid",
            },
        )
        return False

    monkeypatch.setattr(cw, "_resolve_shared_home", lambda: tmp_path)
    monkeypatch.setattr(cw, "_current_profile_home", lambda: tmp_path / "profiles" / "alice")
    monkeypatch.setattr(cw, "find_marker_for_open_id", lambda *_args: marker)
    monkeypatch.setattr(
        cw,
        "_clear_stale_reauth_markers_if_uat_recovered",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(cw, "_iter_reauth_markers_for_open_id", lambda *_args: [marker])
    monkeypatch.setattr(
        cw,
        "clear_non_actionable_reauth_marker",
        replace_with_authoritative,
    )

    result = cw._l4_check_needs_reauth_and_defer(
        {"owner_open_id": open_id, "id": "JOB-REPLACED"}
    )

    assert result is not None
    success, output, _final_response, error = result
    assert success is False
    assert crc.REASON_REFRESH_REJECTED in output
    assert crc.REASON_REFRESH_REJECTED in (error or "")
    assert crc.marker_requires_reauth(crc.read_needs_reauth_marker(marker) or {})
