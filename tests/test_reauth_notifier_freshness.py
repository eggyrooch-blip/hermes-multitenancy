from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_multitenancy import credential_reauth_notifier as N


def _write_marker(shared: Path, profile: str, open_id: str, *, ts: int, reason: str = "refresh_rejected"):
    d = shared / "profiles" / profile / "feishu_uat"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{open_id}.needs_reauth").write_text(
        json.dumps({"reason": reason, "ts": ts, "detail": "x", "profile": profile}),
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
    _write_marker(tmp_path, "olduser", "ou_old", ts=now - 3 * 24 * 3600)  # 3 days old (06-09 style)
    N._scan_once(tmp_path, {})
    assert sent == []  # gated by default 24h freshness window


def test_fresh_marker_is_sent(sent, tmp_path):
    now = int(time.time())
    _write_marker(tmp_path, "stt", "ou_stt", ts=now - 60)  # just rejected
    N._scan_once(tmp_path, {})
    assert len(sent) == 1
    recipient, text = sent[0]
    assert recipient == "ou_stt"
    assert "/feishu_auth" in text  # actionable re-auth prompt


def test_gate_disabled_with_zero_sends_stale(sent, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CREDENTIAL_REAUTH_NOTIFIER_MAX_AGE_SECONDS", "0")
    now = int(time.time())
    _write_marker(tmp_path, "olduser", "ou_old", ts=now - 30 * 24 * 3600)
    N._scan_once(tmp_path, {})
    assert len(sent) == 1  # gate off -> even ancient markers send


def test_resolve_max_marker_age_env(monkeypatch):
    monkeypatch.delenv("HERMES_CREDENTIAL_REAUTH_NOTIFIER_MAX_AGE_SECONDS", raising=False)
    assert N._resolve_max_marker_age() == N._DEFAULT_MAX_MARKER_AGE_SECONDS
    monkeypatch.setenv("HERMES_CREDENTIAL_REAUTH_NOTIFIER_MAX_AGE_SECONDS", "3600")
    assert N._resolve_max_marker_age() == 3600
    monkeypatch.setenv("HERMES_CREDENTIAL_REAUTH_NOTIFIER_MAX_AGE_SECONDS", "garbage")
    assert N._resolve_max_marker_age() == N._DEFAULT_MAX_MARKER_AGE_SECONDS


def test_marker_with_no_ts_is_not_gated(sent, tmp_path):
    # A marker missing ts (ts=0) must not be skipped by the age gate (treat as send-eligible).
    d = tmp_path / "profiles" / "p" / "feishu_uat"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ou_nots.needs_reauth").write_text(json.dumps({"reason": "refresh_rejected"}), encoding="utf-8")
    N._scan_once(tmp_path, {})
    assert len(sent) == 1
