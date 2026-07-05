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


def test_kep_cli_logged_in_checks_requested_env(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub_auth as cha

    bin_path = tmp_path / "bin" / "kep-auth"
    bin_path.parent.mkdir(parents=True)
    bin_path.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HERMES_KEP_AUTH_BIN", str(bin_path))

    calls = []

    class _Logged:
        stdout = "state: valid\noperator: owner"
        stderr = ""

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return _Logged()

    monkeypatch.setattr(cha.subprocess, "run", fake_run)
    assert cha.kep_cli_logged_in(tmp_path, "p", tmp_path, env_name="pre") is True
    assert calls[0] == [str(bin_path), "--profile", "p", "--env", "pre", "status"]


def test_start_kep_cli_login_uses_requested_env(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub_auth as cha

    bin_path = tmp_path / "bin" / "kep-auth"
    bin_path.parent.mkdir(parents=True)
    bin_path.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HERMES_KEP_AUTH_BIN", str(bin_path))

    calls = []

    class _Stdout:
        def __init__(self):
            self._lines = iter(["https://auth.example.com/start?response_url=http%3A%2F%2Flocalhost%3A1234%2Fcb\n"])

        def readline(self):
            return next(self._lines, "")

    class _Proc:
        stdout = _Stdout()

        def poll(self):
            return None

        def kill(self):
            return None

    def fake_popen(cmd, *a, **k):
        calls.append(cmd)
        return _Proc()

    monkeypatch.setattr(cha.subprocess, "Popen", fake_popen)
    out = cha.start_kep_cli_login(tmp_path, "p", tmp_path, env_name="pre")

    assert out["verification_uri"].startswith("https://auth.example.com/start")
    assert calls[0] == [str(bin_path), "--profile", "p", "--env", "pre", "login"]


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

    def fake_build_hub_card(*, rows, auth_urls=None, pending_note=None, qr_image_keys=None, ctx=None):
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


@pytest.mark.asyncio
async def _auth_command_card(monkeypatch, tmp_path, rows):
    """Run /auth with a mocked adapter/send and return the sent card JSON blob.

    The unified hub sends INSTANTLY with per-credential callback buttons and no
    eager minting, so this only needs the status rows + a send capture."""
    import json
    from hermes_multitenancy import credential_hub, feishu_auth_cards, feishu_uat_auth
    from hermes_multitenancy import router as router_mod

    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(router_mod, "_get_feishu_adapter", lambda _g: object())
    monkeypatch.setattr(feishu_uat_auth, "resolve_shared_home", lambda: tmp_path)
    monkeypatch.setattr(credential_hub, "collect_credential_statuses",
                        lambda *, profile_name, open_id, home_dir: rows)
    sent: dict = {}

    async def fake_send_auth_card(*, adapter, chat_id, card, metadata=None):
        sent["card"] = card
        return {"message_id": "om_hub"}

    monkeypatch.setattr(feishu_auth_cards, "send_auth_card", fake_send_auth_card)
    await router_mod._handle_auth_command(
        args="", sender="ou_owner", sender_alt=None, profile_name="owner",
        profile_home=tmp_path, chat_id="oc_chat", gateway=object(), event=object(),
    )
    return json.dumps(sent.get("card", {}), ensure_ascii=False)


async def test_auth_command_offers_lark_reauth_when_status_authenticated(monkeypatch, tmp_path):
    """/auth must offer a re-auth control for the lark row even when its status
    reads 'authenticated' (weak default_identity==user signal). In the unified
    hub that control is a cred_auth callback button labelled 重新认证; the actual
    device-flow session is minted lazily on click (see _mint_one_cred test)."""
    from hermes_multitenancy import credential_hub
    blob = await _auth_command_card(monkeypatch, tmp_path, [
        credential_hub.CredentialRow(
            id=credential_hub.LARK_CLI, title="Lark-cli", provider="lark",
            installed=True, status="authenticated", default_identity="user",
        )
    ])
    assert '"hermes_action": "cred_auth"' in blob and '"cred": "lark-cli"' in blob, \
        "lark row must offer a cred_auth callback button even when authenticated"
    assert "重新认证" in blob


@pytest.mark.asyncio
async def test_auth_command_offers_keep_record_callback_button(monkeypatch, tmp_path):
    """Every credential (incl. an authenticated keep-record) gets a unified
    callback button; nothing is pre-minted at /auth time."""
    from hermes_multitenancy import credential_hub, credential_hub_auth as cha
    calls = {"n": 0}
    monkeypatch.setattr(cha, "start_keep_record_qr", lambda _p: calls.__setitem__("n", calls["n"] + 1) or {})
    blob = await _auth_command_card(monkeypatch, tmp_path, [
        credential_hub.CredentialRow(id=credential_hub.KEEP_RECORD, title="Keep-record",
                                     provider="keep", installed=True, status="authenticated")
    ])
    assert '"cred": "keep-record"' in blob and '"hermes_action": "cred_auth"' in blob
    assert calls["n"] == 0, "/auth must NOT eagerly mint the keep QR (lazy on click)"


@pytest.mark.asyncio
async def test_mint_one_cred_lark_returns_verify_url(monkeypatch, tmp_path):
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy import feishu_auth_hub_actions as actions
    monkeypatch.setattr(feishu_uat_auth, "find_active_session", lambda **k: None)
    monkeypatch.setattr(feishu_uat_auth, "start_session",
                        lambda **k: {"session_id": "s1", "verification_uri": "https://accounts.feishu.cn/lark-verify"})
    auth_urls, qr, note, flows = actions._mint_one_cred(
        "lark-cli", profile_name="owner", open_id="ou", pdir=tmp_path, shared=tmp_path)
    assert auth_urls["lark-cli"] == "https://accounts.feishu.cn/lark-verify"
    assert flows["lark-cli"]["kind"] == "lark" and flows["lark-cli"]["session_id"] == "s1"


@pytest.mark.asyncio
async def test_mint_one_cred_keep_returns_qr(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub_auth as cha
    from hermes_multitenancy import feishu_auth_hub_actions as actions
    monkeypatch.setattr(cha, "start_keep_record_qr",
                        lambda _p: {"qrcode_id": "q1", "qrcode_url": "https://x/img"})
    monkeypatch.setattr(cha, "fetch_qr_image_key", lambda _s, _u: "img_reauth")
    auth_urls, qr, note, flows = actions._mint_one_cred(
        "keep-record", profile_name="owner", open_id="ou", pdir=tmp_path, shared=tmp_path)
    assert qr["keep-record"] == "img_reauth"
    assert flows["keep-record"]["kind"] == "keep" and flows["keep-record"]["qrcode_id"] == "q1"


@pytest.mark.asyncio
async def test_mint_one_cred_kep_online_with_origin(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub_auth as cha, webui_broker_server
    from hermes_multitenancy import feishu_auth_hub_actions as actions
    monkeypatch.setenv("HERMES_PUBLIC_CALLBACK_ORIGIN", "https://hermes.example.com")
    monkeypatch.setattr(webui_broker_server, "ensure_run_broker_server_started", lambda: None)
    monkeypatch.setattr(cha, "start_kep_cli_login",
                        lambda _p, _prof, _sh, public_origin=None, env_name="online": {
                            "verification_uri": "https://kep.example.com/reauth", "_proc": object()})
    auth_urls, qr, note, flows = actions._mint_one_cred(
        "kep-cli-online", profile_name="owner", open_id="ou", pdir=tmp_path, shared=tmp_path)
    assert auth_urls["kep-cli-online"] == "https://kep.example.com/reauth"
    assert flows["kep-cli-online"]["env"] == "online"


@pytest.mark.asyncio
async def test_mint_one_cred_kep_pre_targets_pre_env(monkeypatch, tmp_path):
    """kep-cli-pre must start login with env_name='pre', not online."""
    from hermes_multitenancy import credential_hub_auth as cha, webui_broker_server
    from hermes_multitenancy import feishu_auth_hub_actions as actions
    monkeypatch.setenv("HERMES_PUBLIC_CALLBACK_ORIGIN", "https://hermes.example.com")
    monkeypatch.setattr(webui_broker_server, "ensure_run_broker_server_started", lambda: None)
    calls = []
    monkeypatch.setattr(cha, "start_kep_cli_login",
                        lambda _p, _prof, _sh, public_origin=None, env_name="online": (
                            calls.append(env_name) or {"verification_uri": "https://kep.example.com/pre", "_proc": object()}))
    actions._mint_one_cred("kep-cli-pre", profile_name="owner", open_id="ou", pdir=tmp_path, shared=tmp_path)
    assert calls == ["pre"]


@pytest.mark.asyncio
async def test_mint_one_cred_kep_no_origin_shows_webui_note(monkeypatch, tmp_path):
    """kep-cli locally (no public callback) offers a WebUI note, not a dead click."""
    from hermes_multitenancy import feishu_auth_hub_actions as actions
    monkeypatch.delenv("HERMES_PUBLIC_CALLBACK_ORIGIN", raising=False)
    auth_urls, qr, note, flows = actions._mint_one_cred(
        "kep-cli-online", profile_name="owner", open_id="ou", pdir=tmp_path, shared=tmp_path)
    assert not auth_urls and not flows
    assert "WebUI" in note["kep-cli-online"]


def _cred_auth_event(operator_open_id, payload):
    import types
    return types.SimpleNamespace(
        operator=types.SimpleNamespace(open_id=operator_open_id),
        context=types.SimpleNamespace(open_chat_id="oc", open_message_id="om"),
        action=types.SimpleNamespace(value=payload),
    )


def test_cred_auth_action_uses_signed_operator_ignores_payload_identity(monkeypatch, tmp_path):
    """SECURITY: identity + profile come from the Feishu-signed event operator,
    never the (group-clickable, unsigned) callback payload. A forged
    profile_name/open_id in the value must be ignored — a clicker only ever
    authenticates their OWN resolved profile."""
    import types
    from hermes_multitenancy import feishu_auth_hub_actions as actions
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy import router as router_mod

    class _Row:
        profile_name = "profile_B"

    class _Table:
        def resolve_owner_root(self, oid):
            return _Row() if oid == "ou_B" else None
        def lookup_by_open_id(self, oid):
            return None

    monkeypatch.setattr(router_mod, "_get_routing_table", lambda: _Table())
    (tmp_path / "profiles" / "profile_B").mkdir(parents=True)
    monkeypatch.setattr(feishu_uat_auth, "resolve_shared_home", lambda: tmp_path)

    used = {}

    def fake_mint(cred, *, profile_name, open_id, pdir, shared):
        used.update(profile_name=profile_name, open_id=open_id)
        return ({cred: "https://v"}, {}, {}, {})

    monkeypatch.setattr(actions, "_mint_one_cred", fake_mint)
    monkeypatch.setattr(actions, "_collect_rows", lambda **k: [])

    forged = {"hermes_action": "cred_auth", "cred": "lark-cli",
              "profile_name": "profile_A", "open_id": "ou_ATTACKER"}
    resp = actions._handle_cred_auth_action(
        types.SimpleNamespace(_loop=None), _cred_auth_event("ou_B", forged), forged)
    assert resp is not None
    assert used == {"profile_name": "profile_B", "open_id": "ou_B"}, \
        "must mint for the signed operator's profile, not the forged payload"


def test_cred_auth_action_rejects_unbound_operator(monkeypatch, tmp_path):
    """An operator with no bound profile is rejected cleanly — no mis-targeted mint."""
    import types
    from hermes_multitenancy import feishu_auth_hub_actions as actions
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy import router as router_mod

    class _Table:
        def resolve_owner_root(self, oid):
            return None
        def lookup_by_open_id(self, oid):
            return None

    monkeypatch.setattr(router_mod, "_get_routing_table", lambda: _Table())
    monkeypatch.setattr(feishu_uat_auth, "resolve_shared_home", lambda: tmp_path)

    minted = {"n": 0}
    monkeypatch.setattr(actions, "_mint_one_cred",
                        lambda *a, **k: minted.__setitem__("n", minted["n"] + 1) or ({}, {}, {}, {}))

    resp = actions._handle_cred_auth_action(
        types.SimpleNamespace(_loop=None),
        _cred_auth_event("ou_unbound", {"cred": "lark-cli"}),
        {"hermes_action": "cred_auth", "cred": "lark-cli"})
    assert resp is not None
    assert minted["n"] == 0, "unbound operator must not trigger any mint"


def test_cred_auth_action_resolves_schema2_union_id_operator(monkeypatch, tmp_path):
    """Feishu card-callback Schema 2 may deliver an operator with only union_id /
    user_id and NO open_id. The handler must still resolve the clicker's profile
    (via lookup_by_union_id) and use the routing row's authoritative open_id,
    else a legitimate click is wrongly denied — the exact bug fixed earlier for
    group card actions."""
    import types
    from hermes_multitenancy import feishu_auth_hub_actions as actions
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy import router as router_mod

    class _Row:
        profile_name = "profile_B"
        open_id = "ou_B_synced"

    class _Table:
        def resolve_owner_root(self, oid):
            return None
        def lookup_by_open_id(self, oid):
            return None
        def lookup_by_union_id(self, uid):
            return _Row() if uid == "on_UNION" else None
        def lookup_by_user_id(self, u):
            return None

    monkeypatch.setattr(router_mod, "_get_routing_table", lambda: _Table())
    (tmp_path / "profiles" / "profile_B").mkdir(parents=True)
    monkeypatch.setattr(feishu_uat_auth, "resolve_shared_home", lambda: tmp_path)

    used = {}
    monkeypatch.setattr(actions, "_mint_one_cred",
                        lambda cred, *, profile_name, open_id, pdir, shared: (
                            used.update(profile_name=profile_name, open_id=open_id) or ({cred: "u"}, {}, {}, {})))
    monkeypatch.setattr(actions, "_collect_rows", lambda **k: [])

    # Operator carries ONLY union_id (Schema 2) — no open_id.
    event = types.SimpleNamespace(
        operator=types.SimpleNamespace(union_id="on_UNION"),
        context=types.SimpleNamespace(open_chat_id="oc", open_message_id="om"),
        action=types.SimpleNamespace(value={"cred": "lark-cli"}),
    )
    resp = actions._handle_cred_auth_action(
        types.SimpleNamespace(_loop=None), event, {"cred": "lark-cli"})
    assert resp is not None
    assert used == {"profile_name": "profile_B", "open_id": "ou_B_synced"}, \
        "Schema 2 union_id operator must resolve via lookup_by_union_id + routing open_id"


def test_poll_hub_flows_checks_kep_cli_pre_when_flow_targets_pre(monkeypatch, tmp_path):
    """A pre login flow must be confirmed by polling pre, not online."""
    import asyncio
    from hermes_multitenancy import credential_hub, credential_hub_auth as cha
    from hermes_multitenancy import feishu_auth_cards
    from hermes_multitenancy import feishu_credential_hub_cards as hub_cards
    from hermes_multitenancy import router as router_mod

    class DoneProc:
        def poll(self):
            return 0

    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(router_mod, "_get_feishu_adapter", lambda _gateway: object())

    calls = []

    def fake_logged_in(_profile_dir, _profile_name, _shared_home, env_name="online"):
        calls.append(env_name)
        return True

    def fake_collect(*, profile_name, open_id, home_dir):
        return [
            credential_hub.CredentialRow(
                id=credential_hub.KEP_CLI_PRE,
                title="kep-cli pre",
                provider="keep",
                installed=True,
                status="authenticated",
            )
        ]

    async def fake_send_auth_card(*, adapter, chat_id, card, metadata=None):
        return {"message_id": "om_success"}

    async def fake_update_auth_card(*, adapter, auth_card, card):
        return True

    monkeypatch.setattr(cha, "kep_cli_logged_in", fake_logged_in)
    monkeypatch.setattr(credential_hub, "collect_credential_statuses", fake_collect)
    monkeypatch.setattr(feishu_auth_cards, "send_auth_card", fake_send_auth_card)
    monkeypatch.setattr(feishu_auth_cards, "update_auth_card", fake_update_auth_card)
    monkeypatch.setattr(hub_cards, "build_hub_card", lambda **_kwargs: {"schema": "2.0"})

    asyncio.run(
        router_mod._poll_hub_flows(
            profile_name="owner",
            open_id="ou_owner",
            profile_dir=Path(tmp_path),
            shared_home=Path(tmp_path / "shared"),
            chat_id="oc_chat",
            gateway=object(),
            hub_card={"message_id": "om_hub"},
            flows={credential_hub.KEP_CLI_PRE: {"kind": "kep", "proc": DoneProc(), "env": "pre"}},
            auth_urls={credential_hub.KEP_CLI_PRE: "https://kep.example.com/pre"},
            qr_image_keys={},
        )
    )

    assert calls == ["pre"]
