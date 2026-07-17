"""Tests for the 5-layer credential renewal hardening (SPEC: credential-renewal-hardening)."""
from __future__ import annotations

import json
import os
import time
import urllib.error
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from hermes_multitenancy import (
    credential_audit,
    credential_renewal_common as common,
    credential_renewal_worker,
    credential_reauth_notifier as notifier,
    feishu_uat_auth as fua,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _valid_payload(open_id: str = "ou_user_a") -> dict[str, Any]:
    """A payload that passes all five layers (valid scope + tokens)."""
    now_ms = int(time.time() * 1000)
    return {
        "app_id": "cli_test",
        "user_open_id": open_id,
        "access_token": "t_access_xxxx",
        "refresh_token": "t_refresh_xxxx",
        "expires_at": now_ms + 2 * 3600 * 1000,
        "refresh_expires_at": now_ms + 30 * 24 * 3600 * 1000,
        "scope": "im:message offline_access auth:user.id:read",
        "granted_at": now_ms,
    }


# ---------------------------------------------------------------------------
# L1 — write-time validation
# ---------------------------------------------------------------------------


def test_l1_validate_accepts_valid_payload():
    rejection = fua._l1_validate_uat(_valid_payload())
    assert rejection is None


def test_l1_rejects_empty_refresh_token():
    payload = _valid_payload()
    payload["refresh_token"] = ""
    rejection = fua._l1_validate_uat(payload)
    assert rejection is not None
    assert rejection["reason"] == common.REASON_EMPTY_REFRESH_TOKEN


def test_l1_rejects_scope_without_offline_access():
    payload = _valid_payload()
    payload["scope"] = "im:message auth:user.id:read"  # no offline_access
    rejection = fua._l1_validate_uat(payload)
    assert rejection is not None
    assert rejection["reason"] == common.REASON_SCOPE_STRIPPED_BY_FEISHU


def test_l1_writes_reject_marker_to_both_profile_and_legacy(tmp_path: Path):
    profile = "alice"
    open_id = "ou_user_a"
    fua._l1_write_reject_marker(
        tmp_path,
        profile,
        open_id,
        {"reason": common.REASON_EMPTY_REFRESH_TOKEN, "detail": "test"},
    )
    profile_marker = tmp_path / "profiles" / profile / "feishu_uat" / f"{open_id}.needs_reauth"
    legacy_marker = tmp_path / "feishu_uat" / f"{open_id}.needs_reauth"
    assert profile_marker.is_file()
    assert legacy_marker.is_file()
    body = common.read_needs_reauth_marker(profile_marker)
    assert body is not None
    assert body["reason"] == common.REASON_EMPTY_REFRESH_TOKEN
    assert body["detail"] == "test"
    assert body["layer"] == "L1"


def test_l1_normalise_refresh_response_rejects_empty_refresh_token():
    with pytest.raises(fua.FeishuUatAuthError):
        fua._normalise_refresh_response(
            {
                "access_token": "t_access",
                "refresh_token": "",
                "expires_in": 7200,
                "scope": "im:message offline_access",
            }
        )


def test_l1_normalise_refresh_response_rejects_scope_without_offline_access():
    with pytest.raises(fua.FeishuUatAuthError):
        fua._normalise_refresh_response(
            {
                "access_token": "t_access",
                "refresh_token": "t_refresh",
                "expires_in": 7200,
                "scope": "im:message",
            }
        )


def test_l1_normalise_refresh_response_accepts_valid():
    out = fua._normalise_refresh_response(
        {
            "access_token": "t_access",
            "refresh_token": "t_refresh",
            "expires_in": 7200,
            "refresh_token_expires_in": 30 * 24 * 3600,
            "scope": "im:message offline_access",
        }
    )
    assert out["access_token"] == "t_access"
    assert out["refresh_token"] == "t_refresh"


# ---------------------------------------------------------------------------
# L5 — startup audit
# ---------------------------------------------------------------------------


def _seed_uat(shared: Path, profile: str | None, open_id: str, payload: dict[str, Any]) -> Path:
    if profile is None:
        target = shared / "feishu_uat" / f"{open_id}.json"
    else:
        target = shared / "profiles" / profile / "feishu_uat" / f"{open_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def test_l5_audit_classifies_broken_uat(tmp_path: Path):
    # one valid
    _seed_uat(tmp_path, "alice", "ou_alice_a", _valid_payload("ou_alice_a"))
    # one with empty refresh_token (legacy path)
    bad_empty = _valid_payload("ou_bob_b")
    bad_empty["refresh_token"] = ""
    _seed_uat(tmp_path, None, "ou_bob_b", bad_empty)
    # one without offline_access
    bad_scope = _valid_payload("ou_carol_c")
    bad_scope["scope"] = "im:message auth:user.id:read"
    _seed_uat(tmp_path, "carol", "ou_carol_c", bad_scope)

    report = credential_audit.run_startup_audit(tmp_path)
    assert report.scanned == 3
    assert report.valid == 1
    assert report.needs_reauth == 2
    assert report.fresh_markers == 2

    bob_marker = tmp_path / "feishu_uat" / "ou_bob_b.needs_reauth"
    carol_marker = tmp_path / "profiles" / "carol" / "feishu_uat" / "ou_carol_c.needs_reauth"
    assert bob_marker.is_file()
    assert carol_marker.is_file()
    bob_body = common.read_needs_reauth_marker(bob_marker)
    assert bob_body is not None
    assert bob_body["reason"] == common.REASON_EMPTY_REFRESH_TOKEN
    carol_body = common.read_needs_reauth_marker(carol_marker)
    assert carol_body is not None
    assert carol_body["reason"] == common.REASON_SCOPE_STRIPPED_BY_FEISHU


def test_l5_audit_is_idempotent_same_reason(tmp_path: Path):
    bad = _valid_payload("ou_x")
    bad["refresh_token"] = ""
    _seed_uat(tmp_path, "x", "ou_x", bad)

    first = credential_audit.run_startup_audit(tmp_path)
    second = credential_audit.run_startup_audit(tmp_path)
    assert first.fresh_markers == 1
    assert second.fresh_markers == 0  # same reason, marker not rewritten


def test_l5_skips_fixtures_dir(tmp_path: Path):
    fixtures = tmp_path / "feishu_uat.fixtures.bak"
    fixtures.mkdir()
    (fixtures / "ou_fixture.json").write_text("{}")
    report = credential_audit.run_startup_audit(tmp_path)
    assert report.scanned == 0


def test_l5_audit_skips_marker_when_usable_uat_exists_elsewhere(tmp_path: Path):
    """A broken STALE COPY must not mark an open_id whose current token is fine.

    Regression (2026-07-08): legacy shared-home UAT (expired May copy) + valid
    profile-store UAT → startup audit wrote a refresh_token_expired marker →
    every cron job for the owner falsely deferred as "needs reauth".
    """
    open_id = "ou_dual"
    stale = _valid_payload(open_id)
    stale["refresh_expires_at"] = int(time.time() * 1000) - 3600 * 1000  # expired copy
    _seed_uat(tmp_path, None, open_id, stale)  # legacy shared-home copy
    _seed_uat(tmp_path, "kate", open_id, _valid_payload(open_id))  # operative, valid

    report = credential_audit.run_startup_audit(tmp_path)

    assert report.valid == 1
    assert report.needs_reauth == 0
    assert report.fresh_markers == 0
    assert not (tmp_path / "feishu_uat" / f"{open_id}.needs_reauth").is_file()


def test_recovery_clears_structural_marker_even_when_marker_is_newer(tmp_path: Path):
    """mtime deadlock: a startup audit (re)writes the marker AFTER the token's
    last refresh, so "valid UAT newer than marker" never holds. A LOCAL-STRUCTURAL
    marker must still be refuted by a currently-usable UAT."""
    open_id = "ou_deadlock"
    uat = _seed_uat(tmp_path, "kate", open_id, _valid_payload(open_id))
    marker = tmp_path / "feishu_uat" / f"{open_id}.needs_reauth"
    marker.parent.mkdir(parents=True, exist_ok=True)
    common.write_needs_reauth_marker(
        marker,
        reason=common.REASON_REFRESH_TOKEN_EXPIRED,
        detail="stale copy scan",
    )
    past = time.time() - 3600
    os.utime(uat, (past, past))  # valid UAT strictly OLDER than the marker

    assert common.clear_reauth_markers_if_uat_recovered(tmp_path, open_id, marker) is True
    assert not marker.is_file()


def test_recovery_keeps_authoritative_refresh_rejected_marker(tmp_path: Path):
    """Server-authoritative rejection outranks a valid-LOOKING local file —
    the structural fallback must not clear it."""
    open_id = "ou_srv"
    uat = _seed_uat(tmp_path, "kate", open_id, _valid_payload(open_id))
    marker = tmp_path / "feishu_uat" / f"{open_id}.needs_reauth"
    marker.parent.mkdir(parents=True, exist_ok=True)
    common.write_needs_reauth_marker(
        marker,
        reason=common.REASON_REFRESH_REJECTED,
        detail="feishu rejected refresh",
        extra={"authoritative": True, "refresh_class": "invalid"},
    )
    past = time.time() - 3600
    os.utime(uat, (past, past))

    assert common.clear_reauth_markers_if_uat_recovered(tmp_path, open_id, marker) is False
    assert marker.is_file()


# ---------------------------------------------------------------------------
# L3 — passive marker maintenance, no proactive DM
# ---------------------------------------------------------------------------


def test_credential_renewal_subsystem_does_not_start_proactive_reauth_notifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import hermes_multitenancy as plugin
    from hermes_multitenancy import feishu_uat_auth

    calls: list[str] = []
    monkeypatch.setattr(feishu_uat_auth, "resolve_shared_home", lambda: tmp_path)
    monkeypatch.setattr(plugin, "run_startup_audit", lambda shared_home: calls.append("audit"))
    monkeypatch.setattr(plugin, "ensure_renewal_worker_started", lambda shared_home: calls.append("renewal"))
    monkeypatch.setattr(
        plugin,
        "ensure_reauth_notifier_started",
        lambda shared_home: calls.append("notifier"),
        raising=False,
    )

    plugin._start_credential_renewal_subsystem()

    assert calls == ["audit", "renewal"]


def test_l3_scan_never_sends_dm_even_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    marker_dir = tmp_path / "feishu_uat"
    common.write_needs_reauth_marker(
        common.marker_path_for_open_id(marker_dir, "ou_user"),
        reason=common.REASON_EMPTY_REFRESH_TOKEN,
        detail="seeded by test",
    )
    sent: list[tuple[str, str, str]] = []

    monkeypatch.setenv("HERMES_CREDENTIAL_REAUTH_NOTIFIER_SEND", "1")
    monkeypatch.setattr(notifier, "_get_bot_token", lambda shared_home: "tenant-token", raising=False)
    monkeypatch.setattr(notifier, "_send_feishu_dm", lambda *args: sent.append(args) or True, raising=False)

    seen: dict[str, dict[str, Any]] = {}
    changed = notifier._scan_once(tmp_path, seen)

    assert changed is False
    assert sent == []
    assert seen == {}


def test_l3_open_id_from_marker_filename():
    p1 = Path("/some/dir/ou_user_a.needs_reauth")
    p2 = Path("/some/dir/ou_user_b.json.needs_reauth")
    assert notifier._open_id_from_marker(p1) == "ou_user_a"
    assert notifier._open_id_from_marker(p2) == "ou_user_b"


def test_l3_scan_preserves_actionable_marker_for_passive_task_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    marker_dir = tmp_path / "feishu_uat"
    common.write_needs_reauth_marker(
        common.marker_path_for_open_id(marker_dir, "ou_user"),
        reason=common.REASON_EMPTY_REFRESH_TOKEN,
        detail="seeded by test",
    )
    marker = marker_dir / "ou_user.needs_reauth"

    seen: dict[str, dict[str, Any]] = {}
    changed = notifier._scan_once(tmp_path, seen)

    assert changed is False
    assert seen == {}
    assert marker.exists()


# ---------------------------------------------------------------------------
# L4 — cron worker defers on marker
# ---------------------------------------------------------------------------


def test_l4_defers_job_when_marker_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from hermes_multitenancy import cron_worker

    open_id = "ou_user_def"
    profile_name = "deferred_profile"
    shared = tmp_path
    profile_home = shared / "profiles" / profile_name
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))

    marker_dir = shared / "feishu_uat"
    marker_dir.mkdir()
    common.write_needs_reauth_marker(
        common.marker_path_for_open_id(marker_dir, open_id),
        reason=common.REASON_EMPTY_REFRESH_TOKEN,
        detail="seeded by test",
        extra={"layer": "TEST"},
    )

    job = {"id": "JOB-DEFER", "name": "test job", "owner_open_id": open_id}
    result = cron_worker._l4_check_needs_reauth_and_defer(job)
    assert result is not None
    success, output, final_response, error = result
    assert success is False
    assert "DEFERRED" in output
    assert common.REASON_EMPTY_REFRESH_TOKEN in (error or "")
    assert "/feishu_auth" in output
    assert "本次任务" in output

    deferred_file = profile_home / "cron" / "output" / "JOB-DEFER.deferred.json"
    assert deferred_file.is_file()
    body = json.loads(deferred_file.read_text(encoding="utf-8"))
    assert body["deferred"] is True
    assert body["reason"] == common.REASON_EMPTY_REFRESH_TOKEN
    assert body["owner_open_id"] == open_id


def test_l4_does_not_defer_when_no_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from hermes_multitenancy import cron_worker

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "ok_profile"))
    monkeypatch.setenv("HERMES_SHARED_HOME", str(tmp_path))
    (tmp_path / "profiles" / "ok_profile").mkdir(parents=True)

    job = {"id": "JOB-OK", "name": "ok", "owner_open_id": "ou_no_marker_user"}
    result = cron_worker._l4_check_needs_reauth_and_defer(job)
    assert result is None


def test_l4_ignores_stale_marker_when_valid_uat_was_refreshed_later(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from hermes_multitenancy import cron_worker

    open_id = "ou_user_restored"
    profile_name = "restored_profile"
    shared = tmp_path
    profile_home = shared / "profiles" / profile_name
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))

    marker_dir = profile_home / "feishu_uat"
    common.write_needs_reauth_marker(
        common.marker_path_for_open_id(marker_dir, open_id),
        reason=common.REASON_REFRESH_REJECTED,
        detail="old refresh failure",
        extra={"layer": "L2", "profile": profile_name},
    )
    marker_path = marker_dir / f"{open_id}.needs_reauth"
    old_ts = time.time() - 60
    os.utime(marker_path, (old_ts, old_ts))

    uat_path = _seed_uat(shared, profile_name, open_id, _valid_payload(open_id))
    new_ts = time.time()
    os.utime(uat_path, (new_ts, new_ts))

    job = {"id": "JOB-RESTORED", "name": "restored", "owner_open_id": open_id}
    result = cron_worker._l4_check_needs_reauth_and_defer(job)

    assert result is None
    assert not marker_path.exists()
    assert not (profile_home / "cron" / "output" / "JOB-RESTORED.deferred.json").exists()


def test_l4_ignores_stale_marker_when_refreshed_uat_access_expired_but_refresh_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from hermes_multitenancy import cron_worker

    open_id = "ou_user_refreshable"
    profile_name = "refreshable_profile"
    shared = tmp_path
    profile_home = shared / "profiles" / profile_name
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))

    marker_dir = profile_home / "feishu_uat"
    common.write_needs_reauth_marker(
        common.marker_path_for_open_id(marker_dir, open_id),
        reason=common.REASON_REFRESH_REJECTED,
        detail="old authoritative refresh failure",
        extra={"layer": "L2", "profile": profile_name, "authoritative": True, "refresh_class": "invalid"},
    )
    marker_path = marker_dir / f"{open_id}.needs_reauth"
    old_ts = time.time() - 60
    os.utime(marker_path, (old_ts, old_ts))

    payload = _valid_payload(open_id)
    payload["expires_at"] = int((time.time() - 60) * 1000)
    assert common.classify_uat_payload(payload) is None
    assert common.payload_access_expired(payload) is True
    assert common.payload_refresh_expired(payload) is False
    uat_path = _seed_uat(shared, profile_name, open_id, payload)
    new_ts = time.time()
    os.utime(uat_path, (new_ts, new_ts))

    job = {"id": "JOB-REFRESHABLE", "name": "refreshable", "owner_open_id": open_id}
    result = cron_worker._l4_check_needs_reauth_and_defer(job)

    assert result is None
    assert not marker_path.exists()
    assert not (profile_home / "cron" / "output" / "JOB-REFRESHABLE.deferred.json").exists()


def test_l4_ignores_non_authoritative_refresh_rejected_when_current_uat_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from hermes_multitenancy import cron_worker

    open_id = "ou_user_infra"
    profile_name = "infra_profile"
    shared = tmp_path
    profile_home = shared / "profiles" / profile_name
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))

    _seed_uat(shared, profile_name, open_id, _valid_payload(open_id))
    marker_dir = profile_home / "feishu_uat"
    common.write_needs_reauth_marker(
        common.marker_path_for_open_id(marker_dir, open_id),
        reason=common.REASON_REFRESH_REJECTED,
        detail="RuntimeError: credential encryption key is required",
        extra={"layer": "L2", "profile": profile_name},
    )
    marker_path = marker_dir / f"{open_id}.needs_reauth"
    new_ts = time.time()
    os.utime(marker_path, (new_ts, new_ts))

    job = {"id": "JOB-INFRA", "name": "infra", "owner_open_id": open_id}
    result = cron_worker._l4_check_needs_reauth_and_defer(job)

    assert result is None
    assert not marker_path.exists()
    assert not (profile_home / "cron" / "output" / "JOB-INFRA.deferred.json").exists()


def test_l4_rechecks_older_actionable_marker_after_clearing_non_authoritative_newer_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from hermes_multitenancy import cron_worker

    open_id = "ou_user_masked"
    profile_name = "masked_profile"
    shared = tmp_path
    profile_home = shared / "profiles" / profile_name
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))

    legacy_marker_dir = shared / "feishu_uat"
    common.write_needs_reauth_marker(
        common.marker_path_for_open_id(legacy_marker_dir, open_id),
        reason=common.REASON_REFRESH_TOKEN_EXPIRED,
        detail="authoritative older marker",
        extra={"layer": "TEST"},
    )
    older_marker = legacy_marker_dir / f"{open_id}.needs_reauth"
    old_ts = time.time() - 60
    os.utime(older_marker, (old_ts, old_ts))

    profile_marker_dir = profile_home / "feishu_uat"
    common.write_needs_reauth_marker(
        common.marker_path_for_open_id(profile_marker_dir, open_id),
        reason=common.REASON_REFRESH_REJECTED,
        detail="RuntimeError: credential encryption key is required",
        extra={"layer": "L2", "profile": profile_name},
    )
    newer_marker = profile_marker_dir / f"{open_id}.needs_reauth"
    new_ts = time.time()
    os.utime(newer_marker, (new_ts, new_ts))

    job = {"id": "JOB-MASKED", "name": "masked", "owner_open_id": open_id}
    result = cron_worker._l4_check_needs_reauth_and_defer(job)

    assert result is not None
    success, output, _final_response, error = result
    assert success is False
    assert common.REASON_REFRESH_TOKEN_EXPIRED in output
    assert common.REASON_REFRESH_TOKEN_EXPIRED in (error or "")
    assert older_marker.exists()
    assert not newer_marker.exists()
    diagnostic = common.read_refresh_diagnostic_marker(
        profile_marker_dir / f"{open_id}.refresh_diagnostic"
    )
    assert diagnostic is not None
    assert diagnostic["reason"] == common.REASON_REFRESH_DIAGNOSTIC
    assert diagnostic["source_reason"] == common.REASON_REFRESH_REJECTED
    assert diagnostic["detail"] == "RuntimeError: credential encryption key is required"
    assert diagnostic["profile"] == profile_name
    assert diagnostic["layer"] == "L2"


def test_l4_skips_jobs_without_owner_open_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from hermes_multitenancy import cron_worker

    monkeypatch.setenv("HERMES_SHARED_HOME", str(tmp_path))
    job = {"id": "JOB-X"}
    assert cron_worker._l4_check_needs_reauth_and_defer(job) is None


# ---------------------------------------------------------------------------
# L2 — classify before refresh (no network)
# ---------------------------------------------------------------------------


def test_l2_classify_skips_broken_uat_before_network():
    """If the cached payload is already broken, L2 must not call Feishu."""
    payload = _valid_payload()
    payload["scope"] = "im:message"  # no offline_access
    reason = common.classify_uat_payload(payload)
    assert reason == common.REASON_SCOPE_STRIPPED_BY_FEISHU


def test_l2_classify_valid_payload_returns_none():
    assert common.classify_uat_payload(_valid_payload()) is None


def test_l2_classify_expired_access_with_live_refresh_is_refreshable_not_reauth():
    payload = _valid_payload()
    now_ms = int(time.time() * 1000)
    payload["expires_at"] = now_ms - 60_000
    payload["refresh_expires_at"] = now_ms + 7 * 24 * 3600 * 1000

    assert common.payload_access_expired(payload) is True
    assert common.payload_refresh_expired(payload) is False
    assert common.classify_uat_payload(payload) is None


def test_l2_refresh_preserves_existing_refresh_token_when_feishu_omits_new_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """OpenClaw-compatible refresh: new access token may arrive without a new refresh token."""
    payload = _valid_payload("ou_refreshable")
    payload["expires_at"] = int(time.time() * 1000) - 60_000
    stored: dict[str, Any] = {}

    monkeypatch.setattr(fua, "_assert_route", lambda *args, **kwargs: None)
    monkeypatch.setattr(fua, "_feishu_app_credentials", lambda shared: ("cli_test", "secret"))
    monkeypatch.setattr(fua, "_load_best_uat_payload", lambda *args, **kwargs: payload)
    monkeypatch.setattr(
        fua,
        "_refresh_access_token",
        lambda client_id, client_secret, refresh_token: {
            "code": 0,
            "access_token": "fresh-access",
            "expires_in": 7200,
            "scope": "im:message offline_access",
        },
    )
    monkeypatch.setattr(
        fua,
        "_store_uat",
        lambda shared_home, profile_name, open_id, refreshed: stored.update(refreshed),
    )

    refreshed = fua.refresh_uat_if_needed(
        profile_name="alice",
        open_id="ou_refreshable",
        shared_home=tmp_path,
        force=True,
    )

    assert refreshed is not None
    assert refreshed["access_token"] == "fresh-access"
    assert refreshed["refresh_token"] == "t_refresh_xxxx"
    assert stored["refresh_token"] == "t_refresh_xxxx"


def test_l2_infra_refresh_failure_does_not_write_user_reauth_marker(tmp_path: Path):
    credential_renewal_worker._record_failure(
        tmp_path,
        "alice",
        "ou_infra",
        RuntimeError("credential encryption key is required"),
    )

    marker = tmp_path / "profiles" / "alice" / "feishu_uat" / "ou_infra.needs_reauth"
    diagnostic = tmp_path / "profiles" / "alice" / "feishu_uat" / "ou_infra.refresh_diagnostic"
    assert not marker.exists()
    body = common.read_refresh_diagnostic_marker(diagnostic)
    assert body is not None
    assert body["reason"] == common.REASON_REFRESH_DIAGNOSTIC
    assert body["actionable"] is False
    assert "credential encryption key is required" in body["detail"]


def test_l2_authoritative_invalid_refresh_writes_reauth_marker(tmp_path: Path):
    credential_renewal_worker._record_failure(
        tmp_path,
        "alice",
        "ou_invalid",
        fua.FeishuUatAuthError(
            "Feishu UAT refresh failed: code=20026 msg=invalid refresh token",
            status=401,
            refresh_class="invalid",
        ),
    )

    marker = tmp_path / "profiles" / "alice" / "feishu_uat" / "ou_invalid.needs_reauth"
    body = common.read_needs_reauth_marker(marker)
    assert body is not None
    assert body["reason"] == common.REASON_REFRESH_REJECTED
    assert body["refresh_class"] == "invalid"
    assert body["authoritative"] is True


def test_l2_authoritative_reauth_marker_stops_refresh_until_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    profile_name = "alice"
    open_id = "ou_invalid"
    marker = (
        tmp_path
        / "profiles"
        / profile_name
        / "feishu_uat"
        / f"{open_id}.needs_reauth"
    )
    common.write_needs_reauth_marker(
        marker,
        reason=common.REASON_REFRESH_REJECTED,
        detail="Feishu rejected the refresh token",
        extra={
            "layer": "L2",
            "profile": profile_name,
            "authoritative": True,
            "refresh_class": "invalid",
        },
    )

    payload = _valid_payload(open_id)
    refreshes = []
    monkeypatch.setattr(fua, "_load_best_uat_payload", lambda *args, **kwargs: payload)
    monkeypatch.setattr(fua, "_payload_needs_refresh", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        fua,
        "refresh_uat_if_needed",
        lambda **kwargs: refreshes.append(kwargs),
    )

    first = credential_renewal_worker._refresh_one(
        tmp_path, profile_name, open_id, headroom_seconds=300
    )
    marker.unlink()
    second = credential_renewal_worker._refresh_one(
        tmp_path, profile_name, open_id, headroom_seconds=300
    )

    assert first == "skipped"
    assert second == "refreshed"
    assert len(refreshes) == 1
    assert refreshes[0]["force"] is True


@pytest.mark.parametrize("authoritative", [False, "false", 0, 1])
def test_l2_non_authoritative_refresh_marker_does_not_stop_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authoritative,
):
    profile_name = "alice"
    open_id = "ou_transient"
    common.write_needs_reauth_marker(
        tmp_path
        / "profiles"
        / profile_name
        / "feishu_uat"
        / f"{open_id}.needs_reauth",
        reason=common.REASON_REFRESH_REJECTED,
        detail="local timeout",
        extra={
            "layer": "L2",
            "profile": profile_name,
            "authoritative": authoritative,
            "refresh_class": "transient",
        },
    )

    payload = _valid_payload(open_id)
    refreshes = []
    monkeypatch.setattr(fua, "_load_best_uat_payload", lambda *args, **kwargs: payload)
    monkeypatch.setattr(fua, "_payload_needs_refresh", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        fua,
        "refresh_uat_if_needed",
        lambda **kwargs: refreshes.append(kwargs),
    )

    outcome = credential_renewal_worker._refresh_one(
        tmp_path, profile_name, open_id, headroom_seconds=300
    )

    assert outcome == "refreshed"
    assert len(refreshes) == 1


def test_l2_refresh_http_error_json_body_is_classified_invalid(monkeypatch: pytest.MonkeyPatch):
    payload = json.dumps(
        {"code": fua.LARK_REFRESH_ERROR["REFRESH_TOKEN_INVALID"], "msg": "invalid refresh token"}
    ).encode("utf-8")
    http_error = urllib.error.HTTPError(
        url="https://open.feishu.cn/open-apis/authen/v2/oauth/token",
        code=400,
        msg="Bad Request",
        hdrs={},
        fp=BytesIO(payload),
    )

    def raise_http_error(*args, **kwargs):
        raise http_error

    monkeypatch.setattr(fua.urllib.request, "urlopen", raise_http_error)

    with pytest.raises(fua.FeishuUatAuthError) as excinfo:
        fua._refresh_uat_token("refresh-token", "client-id", "client-secret")

    assert excinfo.value.refresh_class == "invalid"


def test_l2_refresh_http_error_unknown_json_body_is_diagnostic_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    payload = json.dumps({"code": 999999, "msg": "rate limit or app error"}).encode("utf-8")
    http_error = urllib.error.HTTPError(
        url="https://open.feishu.cn/open-apis/authen/v2/oauth/token",
        code=400,
        msg="Bad Request",
        hdrs={},
        fp=BytesIO(payload),
    )

    def raise_http_error(*args, **kwargs):
        raise http_error

    monkeypatch.setattr(fua.urllib.request, "urlopen", raise_http_error)

    with pytest.raises(fua.FeishuUatAuthError) as excinfo:
        fua._refresh_uat_token("refresh-token", "client-id", "client-secret")

    assert excinfo.value.refresh_class == "unknown"

    credential_renewal_worker._record_failure(tmp_path, "alice", "ou_unknown", excinfo.value)

    marker = tmp_path / "profiles" / "alice" / "feishu_uat" / "ou_unknown.needs_reauth"
    diagnostic = tmp_path / "profiles" / "alice" / "feishu_uat" / "ou_unknown.refresh_diagnostic"
    assert not marker.exists()
    assert common.read_refresh_diagnostic_marker(diagnostic) is not None


# ---------------------------------------------------------------------------
# Common — marker idempotency
# ---------------------------------------------------------------------------


def test_marker_round_trip(tmp_path: Path):
    marker = tmp_path / "ou_x.needs_reauth"
    common.write_needs_reauth_marker(
        marker, reason="r1", detail="d1", extra={"layer": "TEST"}
    )
    body = common.read_needs_reauth_marker(marker)
    assert body is not None
    assert body["reason"] == "r1"
    assert body["detail"] == "d1"
    assert body["layer"] == "TEST"


def test_iter_uat_locations_finds_both_layouts(tmp_path: Path):
    _seed_uat(tmp_path, None, "ou_legacy", _valid_payload("ou_legacy"))
    _seed_uat(tmp_path, "p1", "ou_p1", _valid_payload("ou_p1"))
    _seed_uat(tmp_path, "p2", "ou_p2", _valid_payload("ou_p2"))
    locations = list(common.iter_uat_locations(tmp_path))
    open_ids = sorted(loc.open_id for loc in locations)
    assert open_ids == ["ou_legacy", "ou_p1", "ou_p2"]
    legacy = [loc for loc in locations if loc.legacy]
    assert len(legacy) == 1
    assert legacy[0].open_id == "ou_legacy"
