from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from hermes_multitenancy import credential_renewal_common as common
from hermes_multitenancy import credential_reauth_notifier as N


def _write_marker(
    shared: Path,
    profile: str,
    open_id: str,
    *,
    ts: int,
    reason: str = "refresh_rejected",
    extra: dict | None = None,
):
    d = shared / "profiles" / profile / "feishu_uat"
    d.mkdir(parents=True, exist_ok=True)
    body = {"reason": reason, "ts": ts, "detail": "x", "profile": profile}
    if extra:
        body.update(extra)
    (d / f"{open_id}.needs_reauth").write_text(
        json.dumps(body),
        encoding="utf-8",
    )


def _write_valid_uat(
    shared: Path,
    profile: str,
    open_id: str,
    *,
    access_expired: bool = False,
) -> None:
    now_ms = int(time.time() * 1000)
    d = shared / "profiles" / profile / "feishu_uat"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{open_id}.json").write_text(
        json.dumps(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "scope": "im:message offline_access",
                "expires_at": now_ms + (-60_000 if access_expired else 3600_000),
                "refresh_expires_at": now_ms + 7 * 24 * 3600_000,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(N, "_get_bot_token", lambda _sh: "tok", raising=False)
    monkeypatch.setattr(
        N,
        "_send_feishu_dm",
        lambda token, recipient, text: calls.append((recipient, text)) or True,
        raising=False,
    )
    monkeypatch.setenv("HERMES_CREDENTIAL_REAUTH_NOTIFIER_SEND", "1")
    monkeypatch.delenv("HERMES_CREDENTIAL_REAUTH_NOTIFIER_MAX_AGE_SECONDS", raising=False)
    return calls


def test_stale_marker_is_not_sent(sent, tmp_path):
    now = int(time.time())
    _write_marker(
        tmp_path,
        "olduser",
        "ou_old",
        ts=now - 3 * 24 * 3600,
        reason=common.REASON_EMPTY_REFRESH_TOKEN,
    )  # 3 days old (06-09 style)
    N._scan_once(tmp_path, {})
    assert sent == []  # gated by default 24h freshness window


def test_fresh_marker_is_not_sent_from_background_scan(sent, tmp_path):
    now = int(time.time())
    seen = {}
    _write_marker(
        tmp_path,
        "stt",
        "ou_stt",
        ts=now - 60,
        reason=common.REASON_EMPTY_REFRESH_TOKEN,
    )  # just rejected
    marker = tmp_path / "profiles" / "stt" / "feishu_uat" / "ou_stt.needs_reauth"

    changed = N._scan_once(tmp_path, seen)

    assert changed is False
    assert sent == []
    assert seen == {}
    assert marker.exists()


def test_gate_disabled_with_zero_still_does_not_send_dm(sent, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CREDENTIAL_REAUTH_NOTIFIER_MAX_AGE_SECONDS", "0")
    now = int(time.time())
    _write_marker(
        tmp_path,
        "olduser",
        "ou_old",
        ts=now - 30 * 24 * 3600,
        reason=common.REASON_EMPTY_REFRESH_TOKEN,
    )
    N._scan_once(tmp_path, {})
    assert sent == []


def test_resolve_max_marker_age_env(monkeypatch):
    monkeypatch.delenv("HERMES_CREDENTIAL_REAUTH_NOTIFIER_MAX_AGE_SECONDS", raising=False)
    assert N._resolve_max_marker_age() == N._DEFAULT_MAX_MARKER_AGE_SECONDS
    monkeypatch.setenv("HERMES_CREDENTIAL_REAUTH_NOTIFIER_MAX_AGE_SECONDS", "3600")
    assert N._resolve_max_marker_age() == 3600
    monkeypatch.setenv("HERMES_CREDENTIAL_REAUTH_NOTIFIER_MAX_AGE_SECONDS", "garbage")
    assert N._resolve_max_marker_age() == N._DEFAULT_MAX_MARKER_AGE_SECONDS


def test_dryrun_scan_does_not_record_notification_state(sent, tmp_path, monkeypatch):
    now = int(time.time())
    _write_marker(tmp_path, "stt", "ou_stt", ts=now - 60, reason=common.REASON_EMPTY_REFRESH_TOKEN)
    seen = {}

    monkeypatch.delenv("HERMES_CREDENTIAL_REAUTH_NOTIFIER_SEND", raising=False)

    changed = N._scan_once(tmp_path, seen)

    assert changed is False
    assert sent == []
    assert seen == {}


def test_repeated_background_scans_do_not_send_or_record_dedupe_state(sent, tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_CREDENTIAL_REAUTH_NOTIFIER_SEND", raising=False)
    now = int(time.time())
    _write_marker(tmp_path, "stt", "ou_stt", ts=now - 60, reason=common.REASON_EMPTY_REFRESH_TOKEN)
    seen = {}

    changed1 = N._scan_once(tmp_path, seen)
    changed2 = N._scan_once(tmp_path, seen)

    assert changed1 is False and changed2 is False
    assert sent == []
    assert seen == {}


def test_send_enabled_marker_still_does_not_send_from_background_scan(sent, tmp_path):
    now = int(time.time())
    _write_marker(tmp_path, "stt", "ou_stt", ts=now - 60, reason=common.REASON_EMPTY_REFRESH_TOKEN)
    seen = {}
    N._scan_once(tmp_path, seen)
    N._scan_once(tmp_path, seen)
    assert sent == []
    assert seen == {}


def test_marker_with_no_ts_is_not_gated(sent, tmp_path):
    d = tmp_path / "profiles" / "p" / "feishu_uat"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ou_nots.needs_reauth").write_text(
        json.dumps({"reason": common.REASON_EMPTY_REFRESH_TOKEN}),
        encoding="utf-8",
    )
    N._scan_once(tmp_path, {})
    assert sent == []


def test_authoritative_refresh_rejected_marker_is_not_sent_by_background_scan(sent, tmp_path):
    now = int(time.time())
    _write_marker(
        tmp_path,
        "stt",
        "ou_stt",
        ts=now - 60,
        reason=common.REASON_REFRESH_REJECTED,
        extra={"authoritative": True, "refresh_class": "invalid"},
    )
    marker = tmp_path / "profiles" / "stt" / "feishu_uat" / "ou_stt.needs_reauth"

    N._scan_once(tmp_path, {})

    assert sent == []
    assert marker.exists()


def test_authoritative_marker_is_cleared_not_sent_when_newer_valid_uat_exists(sent, tmp_path):
    now = int(time.time())
    _write_marker(
        tmp_path,
        "stt",
        "ou_stt",
        ts=now - 60,
        reason=common.REASON_REFRESH_REJECTED,
        extra={"authoritative": True, "refresh_class": "invalid"},
    )
    marker = tmp_path / "profiles" / "stt" / "feishu_uat" / "ou_stt.needs_reauth"
    old_ts = time.time() - 60
    marker.touch()

    os.utime(marker, (old_ts, old_ts))
    _write_valid_uat(tmp_path, "stt", "ou_stt")

    N._scan_once(tmp_path, {})

    assert sent == []
    assert not marker.exists()


def test_authoritative_marker_is_cleared_when_newer_uat_has_valid_refresh_only(sent, tmp_path):
    now = int(time.time())
    _write_marker(
        tmp_path,
        "stt",
        "ou_stt",
        ts=now - 60,
        reason=common.REASON_REFRESH_REJECTED,
        extra={"authoritative": True, "refresh_class": "invalid"},
    )
    marker = tmp_path / "profiles" / "stt" / "feishu_uat" / "ou_stt.needs_reauth"
    old_ts = time.time() - 60
    marker.touch()
    os.utime(marker, (old_ts, old_ts))

    _write_valid_uat(tmp_path, "stt", "ou_stt", access_expired=True)

    N._scan_once(tmp_path, {})

    assert sent == []
    assert not marker.exists()


def test_non_authoritative_refresh_rejected_with_valid_uat_is_cleared_not_sent(sent, tmp_path):
    now = int(time.time())
    _write_valid_uat(tmp_path, "stt", "ou_stt")
    _write_marker(
        tmp_path,
        "stt",
        "ou_stt",
        ts=now - 60,
        reason=common.REASON_REFRESH_REJECTED,
        extra={"layer": "L2", "detail": "RuntimeError: credential encryption key is required"},
    )

    N._scan_once(tmp_path, {})

    assert sent == []
    assert not (tmp_path / "profiles" / "stt" / "feishu_uat" / "ou_stt.needs_reauth").exists()
    diagnostic = common.read_refresh_diagnostic_marker(
        tmp_path / "profiles" / "stt" / "feishu_uat" / "ou_stt.refresh_diagnostic"
    )
    assert diagnostic is not None
    assert diagnostic["reason"] == common.REASON_REFRESH_DIAGNOSTIC
    assert diagnostic["source_reason"] == common.REASON_REFRESH_REJECTED
    assert diagnostic["detail"] == "RuntimeError: credential encryption key is required"
    assert diagnostic["profile"] == "stt"
    assert diagnostic["layer"] == "L2"
