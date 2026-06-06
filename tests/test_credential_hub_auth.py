"""Auth-START flows (keep-record QR, kep-cli web) — credential_hub_auth.

Subprocess + Feishu image upload are mocked so no real binary/network is needed;
the live end-to-end (real QR scan, real kep-auth login) is the user's acceptance
step. These pin the parsing, the verification-marker write, and error mapping.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

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


def test_hub_card_keep_record_inline_qr():
    from hermes_multitenancy.credential_hub import CredentialRow
    from hermes_multitenancy.feishu_credential_hub_cards import build_hub_card

    rows = [CredentialRow(id="keep-record", title="Keep-record", provider="keep",
                          installed=True, status="needs_auth")]
    blob = json.dumps(build_hub_card(rows=rows, qr_image_keys={"keep-record": "img_k"}), ensure_ascii=False)
    assert '"tag": "img"' in blob
    assert "img_k" in blob
    assert '"callback"' not in blob


def test_hub_card_keep_record_authenticated_has_no_entry():
    from hermes_multitenancy.credential_hub import CredentialRow
    from hermes_multitenancy.feishu_credential_hub_cards import build_hub_card

    rows = [CredentialRow(id="keep-record", title="Keep-record", provider="keep",
                          installed=True, status="authenticated")]
    blob = json.dumps(build_hub_card(rows=rows), ensure_ascii=False)
    assert "已认证" in blob
    assert '"tag": "img"' not in blob
    assert '"callback"' not in blob


@pytest.mark.asyncio
async def test_poll_hub_flows_rerenders_after_lark_success(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub, feishu_auth_cards, feishu_uat_auth
    from hermes_multitenancy import feishu_credential_hub_cards as hub_cards
    from hermes_multitenancy import router as router_mod

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(router_mod.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(router_mod, "_get_feishu_adapter", lambda _gateway: object())
    monkeypatch.setattr(feishu_uat_auth, "poll_session", lambda **_kwargs: {"status": "success"})

    rerenders = []
    success_cards = []
    updates = []

    def fake_collect_credential_statuses(*, profile_name, open_id, home_dir):
        assert profile_name == "owner"
        assert open_id == "ou_owner"
        assert home_dir == tmp_path / "home"
        return [
            credential_hub.CredentialRow(
                id=credential_hub.LARK_CLI,
                title="Lark-cli",
                provider="lark",
                installed=True,
                status="authenticated",
                expires_at=1_800_000_000_000,
            )
        ]

    def fake_build_hub_card(*, rows, auth_urls=None, pending_note=None, qr_image_keys=None):
        rerenders.append(
            {
                "rows": rows,
                "auth_urls": dict(auth_urls or {}),
                "pending_note": dict(pending_note or {}),
                "qr_image_keys": dict(qr_image_keys or {}),
            }
        )
        return {"schema": "2.0", "body": {"elements": []}}

    async def fake_update_auth_card(*, adapter, auth_card, card):
        updates.append({"adapter": adapter, "auth_card": auth_card, "card": card})
        return True

    async def fake_send_auth_card(*, adapter, chat_id, card, metadata=None):
        success_cards.append({"adapter": adapter, "chat_id": chat_id, "card": card, "metadata": metadata})
        return {"message_id": "om_success"}

    monkeypatch.setattr(credential_hub, "collect_credential_statuses", fake_collect_credential_statuses)
    monkeypatch.setattr(hub_cards, "build_hub_card", fake_build_hub_card)
    monkeypatch.setattr(feishu_auth_cards, "update_auth_card", fake_update_auth_card)
    monkeypatch.setattr(feishu_auth_cards, "send_auth_card", fake_send_auth_card)

    await router_mod._poll_hub_flows(
        profile_name="owner",
        open_id="ou_owner",
        profile_dir=Path(tmp_path),
        shared_home=Path(tmp_path / "shared"),
        chat_id="oc_chat",
        gateway=object(),
        hub_card={"message_id": "om_hub"},
        flows={credential_hub.LARK_CLI: {"kind": "lark", "session_id": "sess_1"}},
        auth_urls={credential_hub.LARK_CLI: "https://auth.example.com/lark"},
        qr_image_keys={},
    )

    assert len(updates) == 1
    assert len(rerenders) == 1
    assert rerenders[0]["auth_urls"] == {}
    assert rerenders[0]["qr_image_keys"] == {}
    assert rerenders[0]["pending_note"] == {}
    assert [row.id for row in rerenders[0]["rows"]] == [credential_hub.LARK_CLI]
    assert len(success_cards) == 1
    assert success_cards[0]["card"]["header"]["template"] == "green"
    success_blob = json.dumps(success_cards[0]["card"], ensure_ascii=False)
    assert "认证成功" in success_blob
    assert "Lark-cli" in success_blob
