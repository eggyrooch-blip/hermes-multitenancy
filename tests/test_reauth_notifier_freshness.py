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


def _write_valid_uat(shared: Path, profile: str, open_id: str) -> None:
    now_ms = int(time.time() * 1000)
    d = shared / "profiles" / profile / "feishu_uat"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{open_id}.json").write_text(
        json.dumps(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "scope": "im:message offline_access",
                "expires_at": now_ms + 3600_000,
                "refresh_expires_at": now_ms + 7 * 24 * 3600_000,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(N, "_get_bot_token", lambda _sh: "tok")
    monkeypatch.setattr(N, "_send_feishu_dm", lambda token, recipient, text: calls.append((recipient, text)) or True)
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


def test_fresh_marker_is_sent(sent, tmp_path):
    now = int(time.time())
    _write_marker(
        tmp_path,
        "stt",
        "ou_stt",
        ts=now - 60,
        reason=common.REASON_EMPTY_REFRESH_TOKEN,
    )  # just rejected
    N._scan_once(tmp_path, {})
    assert len(sent) == 1
    recipient, text = sent[0]
    assert recipient == "ou_stt"
    assert "/feishu_auth" in text  # actionable re-auth prompt


def test_gate_disabled_with_zero_sends_stale(sent, tmp_path, monkeypatch):
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
    assert len(sent) == 1  # gate off -> even ancient markers send


def test_resolve_max_marker_age_env(monkeypatch):
    monkeypatch.delenv("HERMES_CREDENTIAL_REAUTH_NOTIFIER_MAX_AGE_SECONDS", raising=False)
    assert N._resolve_max_marker_age() == N._DEFAULT_MAX_MARKER_AGE_SECONDS
    monkeypatch.setenv("HERMES_CREDENTIAL_REAUTH_NOTIFIER_MAX_AGE_SECONDS", "3600")
    assert N._resolve_max_marker_age() == 3600
    monkeypatch.setenv("HERMES_CREDENTIAL_REAUTH_NOTIFIER_MAX_AGE_SECONDS", "garbage")
    assert N._resolve_max_marker_age() == N._DEFAULT_MAX_MARKER_AGE_SECONDS


def test_dryrun_seen_does_not_block_first_live_send(sent, tmp_path, monkeypatch):
    # codex review: a marker seen during dry-run must still send once SEND=1 is flipped
    # (a dry-run seen entry must not win the 24h dedupe).
    now = int(time.time())
    _write_marker(tmp_path, "stt", "ou_stt", ts=now - 60, reason=common.REASON_EMPTY_REFRESH_TOKEN)
    seen = {}
    # First pass: dry-run (SEND not set) records a dry_run seen entry.
    monkeypatch.delenv("HERMES_CREDENTIAL_REAUTH_NOTIFIER_SEND", raising=False)
    N._scan_once(tmp_path, seen)
    assert sent == []
    assert seen.get("ou_stt:empty_refresh_token", {}).get("dry_run") is True
    # Now enable live sends: the dry-run entry must NOT dedupe-block the real send.
    monkeypatch.setenv("HERMES_CREDENTIAL_REAUTH_NOTIFIER_SEND", "1")
    N._scan_once(tmp_path, seen)
    assert len(sent) == 1 and sent[0][0] == "ou_stt"


def test_dryrun_self_dedupes_no_relog(sent, tmp_path, monkeypatch):
    # codex round 2: dry-run must still dedupe its OWN entries (no re-log/churn every scan).
    monkeypatch.delenv("HERMES_CREDENTIAL_REAUTH_NOTIFIER_SEND", raising=False)
    now = int(time.time())
    _write_marker(tmp_path, "stt", "ou_stt", ts=now - 60, reason=common.REASON_EMPTY_REFRESH_TOKEN)
    seen = {}
    changed1 = N._scan_once(tmp_path, seen)   # records dry-run entry
    changed2 = N._scan_once(tmp_path, seen)   # same entry within 24h -> deduped, no churn
    assert changed1 is True and changed2 is False
    assert sent == []


def test_real_send_then_dedupes(sent, tmp_path):
    now = int(time.time())
    _write_marker(tmp_path, "stt", "ou_stt", ts=now - 60, reason=common.REASON_EMPTY_REFRESH_TOKEN)
    seen = {}
    N._scan_once(tmp_path, seen)          # real send (SEND=1 via fixture)
    N._scan_once(tmp_path, seen)          # second pass: deduped (real send within 24h)
    assert len(sent) == 1


def test_marker_with_no_ts_is_not_gated(sent, tmp_path):
    # A marker missing ts (ts=0) must not be skipped by the age gate (treat as send-eligible).
    d = tmp_path / "profiles" / "p" / "feishu_uat"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ou_nots.needs_reauth").write_text(
        json.dumps({"reason": common.REASON_EMPTY_REFRESH_TOKEN}),
        encoding="utf-8",
    )
    N._scan_once(tmp_path, {})
    assert len(sent) == 1


def test_authoritative_refresh_rejected_marker_is_sent(sent, tmp_path):
    now = int(time.time())
    _write_marker(
        tmp_path,
        "stt",
        "ou_stt",
        ts=now - 60,
        reason=common.REASON_REFRESH_REJECTED,
        extra={"authoritative": True, "refresh_class": "invalid"},
    )

    N._scan_once(tmp_path, {})

    assert len(sent) == 1
    assert sent[0][0] == "ou_stt"


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


def test_non_authoritative_refresh_rejected_with_valid_uat_is_cleared_not_sent(sent, tmp_path):
    now = int(time.time())
    _write_valid_uat(tmp_path, "stt", "ou_stt")
    _write_marker(tmp_path, "stt", "ou_stt", ts=now - 60, reason=common.REASON_REFRESH_REJECTED)

    N._scan_once(tmp_path, {})

    assert sent == []
    assert not (tmp_path / "profiles" / "stt" / "feishu_uat" / "ou_stt.needs_reauth").exists()
