"""``POST /api/run-broker/push`` — the pure send-bypass seam tests.

Covers the SPEC push-custom-message acceptance + rejection paths: fail-closed
Bearer auth (401, no token echo), text/interactive dispatch, chat_id
auto-detection, app_id-not-registered / bad msg_type / oversize content / bad
env → 400, env→message_id mapping + echo, and Feishu-side error passthrough
(never a false 200). Every Feishu call is mocked via the module send seam.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from hermes_multitenancy import push_env_map
from hermes_multitenancy import push_message_routes as pmr


@pytest.fixture(autouse=True)
def _isolate_push_state():
    """Fresh in-memory env-map per test; restore default seams after."""
    push_env_map.override_env_map_store(":memory:")
    yield
    push_env_map.override_env_map_store(None)
    pmr.override_feishu_sender(None)
    pmr.override_bot_credentials_resolver(None)


def _app(monkeypatch, *, ingest_key="testkey", keys_file=None):
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    if keys_file is not None:
        monkeypatch.delenv("HERMES_INGEST_KEY", raising=False)
        monkeypatch.setenv("HERMES_INGEST_KEYS_FILE", str(keys_file))
    else:
        monkeypatch.delenv("HERMES_INGEST_KEYS_FILE", raising=False)
        if ingest_key is None:
            monkeypatch.delenv("HERMES_INGEST_KEY", raising=False)
        else:
            monkeypatch.setenv("HERMES_INGEST_KEY", ingest_key)
    return create_run_broker_app(
        dispatch_agent=lambda _req: "",
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )


def _recording_sender(calls, *, message_id="om_test123"):
    def _send(**kwargs):
        calls.append(kwargs)
        return message_id
    return _send


def _dummy_resolver(app_id):
    return ("cli_main", "app_secret")


def _post(app, body, *, headers=None):
    from aiohttp.test_utils import TestClient, TestServer

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post(
                "/api/run-broker/push", json=body, headers=headers or {}
            )
            return response.status, await response.text()
        finally:
            await client.close()

    return asyncio.run(runner())


_TEXT_BODY = {"target": "on_user1", "msg_type": "text", "content": {"text": "hi"}}
_AUTH = {"Authorization": "Bearer testkey"}


# ── auth ─────────────────────────────────────────────────────────────────

def test_push_missing_token_is_401(monkeypatch):
    app = _app(monkeypatch)
    status, text = _post(app, _TEXT_BODY)
    assert status == 401
    assert json.loads(text)["ok"] is False


def test_push_wrong_token_is_401_and_not_echoed(monkeypatch):
    app = _app(monkeypatch)
    wrong = "hm-ingest-super-secret-wrong-token"
    status, text = _post(app, _TEXT_BODY, headers={"Authorization": f"Bearer {wrong}"})
    assert status == 401
    assert wrong not in text  # LEAK: response must never echo the presented token


# ── success paths ──────────────────────────────────────────────────────────

def test_push_text_success(monkeypatch):
    calls = []
    monkeypatch.setattr(pmr, "_feishu_sender", _recording_sender(calls))
    monkeypatch.setattr(pmr, "_bot_credentials_resolver", _dummy_resolver)

    app = _app(monkeypatch)
    status, text = _post(app, _TEXT_BODY, headers=_AUTH)
    data = json.loads(text)
    assert status == 200
    assert data["ok"] is True
    assert data["message_id"] == "om_test123"
    assert data["env"] == "pre"  # default
    assert len(calls) == 1
    sent = calls[0]
    assert sent["msg_type"] == "text"
    assert sent["receive_id"] == "on_user1"
    assert sent["receive_id_type"] == "user_id"  # not oc_ → default user_id
    assert sent["content_str"] == json.dumps({"text": "hi"}, ensure_ascii=False)


def test_push_interactive_success(monkeypatch):
    calls = []
    monkeypatch.setattr(pmr, "_feishu_sender", _recording_sender(calls))
    monkeypatch.setattr(pmr, "_bot_credentials_resolver", _dummy_resolver)

    card = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "x"}]}}
    app = _app(monkeypatch)
    status, text = _post(
        app,
        {"target": "oc_group1", "msg_type": "interactive", "content": card},
        headers=_AUTH,
    )
    data = json.loads(text)
    assert status == 200
    assert data["ok"] is True
    sent = calls[0]
    assert sent["msg_type"] == "interactive"
    # Card JSON passed through verbatim (server only json.dumps the object).
    assert sent["content_str"] == json.dumps(card, ensure_ascii=False)


def test_push_chat_id_autodetected_from_oc_prefix(monkeypatch):
    calls = []
    monkeypatch.setattr(pmr, "_feishu_sender", _recording_sender(calls))
    monkeypatch.setattr(pmr, "_bot_credentials_resolver", _dummy_resolver)

    app = _app(monkeypatch)
    status, _ = _post(
        app,
        {"target": "oc_abc", "msg_type": "text", "content": {"text": "hi"}},
        headers=_AUTH,
    )
    assert status == 200
    assert calls[0]["receive_id_type"] == "chat_id"


def test_push_explicit_receive_id_type_wins(monkeypatch):
    calls = []
    monkeypatch.setattr(pmr, "_feishu_sender", _recording_sender(calls))
    monkeypatch.setattr(pmr, "_bot_credentials_resolver", _dummy_resolver)

    app = _app(monkeypatch)
    status, _ = _post(
        app,
        {"target": "ou_x", "receive_id_type": "open_id", "msg_type": "text",
         "content": {"text": "hi"}},
        headers=_AUTH,
    )
    assert status == 200
    assert calls[0]["receive_id_type"] == "open_id"


def test_push_uuid_is_passed_through(monkeypatch):
    calls = []
    monkeypatch.setattr(pmr, "_feishu_sender", _recording_sender(calls))
    monkeypatch.setattr(pmr, "_bot_credentials_resolver", _dummy_resolver)

    app = _app(monkeypatch)
    status, _ = _post(app, {**_TEXT_BODY, "uuid": "idem-1"}, headers=_AUTH)
    assert status == 200
    assert calls[0]["uuid"] == "idem-1"


# ── rejection paths (fail-closed 400) ──────────────────────────────────────

def test_push_invalid_msg_type_is_400(monkeypatch):
    monkeypatch.setattr(pmr, "_bot_credentials_resolver", _dummy_resolver)
    called = []
    monkeypatch.setattr(pmr, "_feishu_sender", _recording_sender(called))

    app = _app(monkeypatch)
    status, text = _post(
        app,
        {"target": "on_u", "msg_type": "post", "content": {"text": "hi"}},
        headers=_AUTH,
    )
    assert status == 400
    assert json.loads(text)["ok"] is False
    assert called == []  # never reaches the sender


def test_push_content_not_object_is_400(monkeypatch):
    monkeypatch.setattr(pmr, "_bot_credentials_resolver", _dummy_resolver)
    app = _app(monkeypatch)
    status, text = _post(
        app,
        {"target": "on_u", "msg_type": "text", "content": "just a string"},
        headers=_AUTH,
    )
    assert status == 400
    assert "content" in json.loads(text)["error"]


def test_push_content_over_30kb_is_400(monkeypatch):
    called = []
    monkeypatch.setattr(pmr, "_feishu_sender", _recording_sender(called))
    monkeypatch.setattr(pmr, "_bot_credentials_resolver", _dummy_resolver)

    app = _app(monkeypatch)
    big = {"text": "x" * (30 * 1024 + 100)}
    status, text = _post(
        app, {"target": "on_u", "msg_type": "text", "content": big}, headers=_AUTH
    )
    assert status == 400
    assert json.loads(text)["ok"] is False
    assert called == []


def test_push_invalid_env_is_400(monkeypatch):
    monkeypatch.setattr(pmr, "_bot_credentials_resolver", _dummy_resolver)
    app = _app(monkeypatch)
    status, text = _post(app, {**_TEXT_BODY, "env": "prod"}, headers=_AUTH)
    assert status == 400
    assert "env" in json.loads(text)["error"]


def test_push_app_id_unregistered_is_400(monkeypatch):
    def resolver(app_id):
        raise pmr.PushBotNotRegistered(app_id)

    called = []
    monkeypatch.setattr(pmr, "_bot_credentials_resolver", resolver)
    monkeypatch.setattr(pmr, "_feishu_sender", _recording_sender(called))

    app = _app(monkeypatch)
    status, text = _post(
        app, {**_TEXT_BODY, "app_id": "cli_unknown"}, headers=_AUTH
    )
    assert status == 400
    assert "app_id" in json.loads(text)["error"]
    assert called == []


# ── env mapping ────────────────────────────────────────────────────────────

def test_push_env_pre_records_mapping_and_echoes(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(pmr, "_feishu_sender", _recording_sender(calls, message_id="om_map1"))
    monkeypatch.setattr(pmr, "_bot_credentials_resolver", _dummy_resolver)

    keys_file = tmp_path / "ingest-keys.json"
    keys_file.write_text(
        json.dumps({"keys": [{"token": "testkey", "owner": "wangkejie", "name": "wangkejie"}]}),
        encoding="utf-8",
    )
    app = _app(monkeypatch, keys_file=keys_file)
    status, text = _post(app, {**_TEXT_BODY, "env": "pre"}, headers=_AUTH)
    data = json.loads(text)
    assert status == 200
    assert data["env"] == "pre"

    row = push_env_map.get_env_map_store().get("om_map1")
    assert row is not None
    assert row["env"] == "pre"
    assert row["key_owner"] == "wangkejie"


def test_push_env_online_recorded(monkeypatch):
    calls = []
    monkeypatch.setattr(pmr, "_feishu_sender", _recording_sender(calls, message_id="om_on1"))
    monkeypatch.setattr(pmr, "_bot_credentials_resolver", _dummy_resolver)

    app = _app(monkeypatch)
    status, text = _post(app, {**_TEXT_BODY, "env": "online"}, headers=_AUTH)
    assert status == 200
    assert json.loads(text)["env"] == "online"
    assert push_env_map.get_env_map_store().get("om_on1")["env"] == "online"


# ── Feishu-side failure never becomes a false 200 ──────────────────────────

def test_push_feishu_error_is_passed_through_non_200(monkeypatch):
    def failing_send(**_kwargs):
        raise pmr.PushFeishuError(code=230002, msg="bot is not in the chat")

    monkeypatch.setattr(pmr, "_feishu_sender", failing_send)
    monkeypatch.setattr(pmr, "_bot_credentials_resolver", _dummy_resolver)

    app = _app(monkeypatch)
    status, text = _post(app, _TEXT_BODY, headers=_AUTH)
    data = json.loads(text)
    assert status == 502
    assert data["ok"] is False
    assert data["feishu_code"] == 230002
    assert "not in the chat" in data["feishu_msg"]
    # no mapping row written on a failed send
    assert push_env_map.get_env_map_store().get("om_test123") is None


def test_push_feishu_missing_message_id_is_not_200(monkeypatch):
    def send_no_id(**_kwargs):
        raise pmr.PushFeishuError(code=0, msg="feishu returned no message_id")

    monkeypatch.setattr(pmr, "_feishu_sender", send_no_id)
    monkeypatch.setattr(pmr, "_bot_credentials_resolver", _dummy_resolver)

    app = _app(monkeypatch)
    status, _ = _post(app, _TEXT_BODY, headers=_AUTH)
    assert status == 502


def test_push_text_content_missing_text_key_is_400(monkeypatch):
    calls = []
    monkeypatch.setattr(pmr, "_feishu_sender", _recording_sender(calls))
    monkeypatch.setattr(pmr, "_bot_credentials_resolver", _dummy_resolver)

    app = _app(monkeypatch)
    status, text = _post(
        app,
        {"target": "on_u", "msg_type": "text", "content": {"foo": "bar"}},
        headers=_AUTH,
    )
    assert status == 400
    assert not calls  # rejected before any Feishu call
