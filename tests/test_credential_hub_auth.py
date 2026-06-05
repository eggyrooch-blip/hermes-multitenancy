"""Auth-START flows (keep-record QR, kep-cli web) — credential_hub_auth.

Subprocess + Feishu image upload are mocked so no real binary/network is needed;
the live end-to-end (real QR scan, real kep-auth login) is the user's acceptance
step. These pin the parsing, the verification-marker write, and error mapping.
"""
from __future__ import annotations

import hashlib
import json

import pytest


def _mk_skill(profile_dir):
    skill = profile_dir / "skills" / "Keep" / "keep-record" / "scripts"
    skill.mkdir(parents=True)
    for s in ("mcp-call.js", "login-wait.js", "persist_auth.js"):
        (skill / s).write_text("// stub", encoding="utf-8")
    return profile_dir / "skills" / "Keep" / "keep-record"


def test_start_keep_record_qr_parses_envelope(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub_auth as cha

    _mk_skill(tmp_path)
    monkeypatch.setattr(cha, "_run_keep_node",
                        lambda *a, **k: {"ok": True, "data": {"qrcodeId": "q1", "qrcodeUrl": "https://x/img", "redirectUrl": "https://x/r"}})
    out = cha.start_keep_record_qr(tmp_path)
    assert out["qrcode_id"] == "q1"
    assert out["qrcode_url"] == "https://x/img"
    assert out["redirect_url"] == "https://x/r"


def test_start_keep_record_qr_missing_skill_raises_404(tmp_path):
    from hermes_multitenancy import credential_hub_auth as cha

    with pytest.raises(cha.HubAuthError) as ei:
        cha.start_keep_record_qr(tmp_path)
    assert ei.value.status == 404


def test_poll_keep_record_authorized_writes_marker(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub_auth as cha

    _mk_skill(tmp_path)
    calls = []
    def fake_run(profile_dir, args, **k):
        calls.append(args)
        if "login-wait.js" in args[0]:
            return {"ok": True, "data": {"status": "authorized", "token": "TKN", "user": {"username": "owner"}}}
        return {"ok": True, "data": {}}
    monkeypatch.setattr(cha, "_run_keep_node", fake_run)
    res = cha.poll_keep_record_once(tmp_path, "q1")
    assert res["status"] == "authorized"
    assert res["username"] == "owner"
    # verification marker written with the token hash → status reader will see authenticated
    marker = tmp_path / "home" / ".keepai" / "webui-auth-verified.json"
    assert marker.is_file()
    data = json.loads(marker.read_text())
    assert data["token_sha256"] == hashlib.sha256(b"TKN").hexdigest()
    # persist_auth.js was invoked
    assert any("persist_auth.js" in a[0] for a in calls)


def test_poll_keep_record_pending(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub_auth as cha

    _mk_skill(tmp_path)
    monkeypatch.setattr(cha, "_run_keep_node", lambda *a, **k: {"ok": True, "data": {"status": "pending"}})
    res = cha.poll_keep_record_once(tmp_path, "q1")
    assert res["status"] == "pending"


def test_run_keep_node_maps_missing_sdk_to_424(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub_auth as cha

    _mk_skill(tmp_path)

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "Error: Cannot find module '@keepclaw/skill-sdk/mcp-cli'"

    monkeypatch.setattr(cha.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(cha.HubAuthError) as ei:
        cha._run_keep_node(tmp_path, ["x.js"])
    assert ei.value.status == 424


def test_kep_cli_logged_in_parses_status(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub_auth as cha

    bin_path = tmp_path / "bin" / "kep-auth"
    bin_path.parent.mkdir(parents=True)
    bin_path.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HERMES_KEP_AUTH_BIN", str(bin_path))

    class _Logged:
        stdout = "state: valid\noperator: owner"
        stderr = ""

    class _Not:
        stdout = "state: not logged in"
        stderr = ""

    monkeypatch.setattr(cha.subprocess, "run", lambda *a, **k: _Logged())
    assert cha.kep_cli_logged_in(tmp_path, "p", tmp_path) is True
    monkeypatch.setattr(cha.subprocess, "run", lambda *a, **k: _Not())
    assert cha.kep_cli_logged_in(tmp_path, "p", tmp_path) is False


def test_kep_cli_logged_in_no_binary_false(tmp_path, monkeypatch):
    from hermes_multitenancy import credential_hub_auth as cha

    monkeypatch.setenv("HERMES_KEP_AUTH_BIN", str(tmp_path / "nope" / "kep-auth"))
    assert cha.kep_cli_logged_in(tmp_path, "p", tmp_path) is False


# -- card: QR image element --------------------------------------------------


def test_hub_card_keep_record_callback_button():
    """keep-record on the hub card is a CALLBACK button (QR is sent after click)."""
    from hermes_multitenancy.credential_hub import CredentialRow
    from hermes_multitenancy.feishu_credential_hub_cards import build_hub_card

    rows = [CredentialRow(id="keep-record", title="Keep-record", provider="keep",
                          installed=True, status="needs_auth")]
    blob = json.dumps(build_hub_card(rows=rows), ensure_ascii=False)
    assert '"callback"' in blob
    assert '"cred": "keep-record"' in blob
    assert '"tag": "img"' not in blob  # no embedded QR in the hub card; sent on click


def test_hub_card_keep_record_reauth_callback_when_authenticated():
    from hermes_multitenancy.credential_hub import CredentialRow
    from hermes_multitenancy.feishu_credential_hub_cards import build_hub_card

    rows = [CredentialRow(id="keep-record", title="Keep-record", provider="keep",
                          installed=True, status="authenticated")]
    blob = json.dumps(build_hub_card(rows=rows), ensure_ascii=False)
    assert '"cred": "keep-record"' in blob  # re-auth callback present when authenticated
