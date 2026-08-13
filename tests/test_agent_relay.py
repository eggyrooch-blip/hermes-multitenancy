"""Local-agent relay tests through its public HTTP seam."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import stat
import sys
import threading
import time
import types
from urllib.parse import parse_qs, urlparse

import pytest


class FakeOAuth:
    def __init__(self) -> None:
        self.exchanges = 0

    def authorize_url(self, *, state: str) -> str:
        return f"https://oauth.example/authorize?state={state}"

    async def exchange(self, code: str) -> dict[str, str]:
        self.exchanges += 1
        assert code == "oauth-code-alice"
        return {"actor_id": "ou_alice", "display_name": "Alice"}


class FakeFeishu:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.cards: list[dict] = []
        self.card_updates: list[dict] = []
        self.ambiguity_notices: list[str] = []

    async def send_message(self, **request) -> dict[str, str]:
        self.messages.append(request)
        return {
            "message_id": f"om_{len(self.messages)}",
            "conversation_id": "oc_self",
        }

    async def send_card(self, **request) -> dict[str, str]:
        self.cards.append(request)
        return {
            "message_id": f"om_card_{len(self.cards)}",
            "conversation_id": "oc_self",
        }

    async def update_card(self, **request) -> None:
        self.card_updates.append(request)

    async def send_ambiguity_notice(self, actor_id: str) -> None:
        self.ambiguity_notices.append(actor_id)


class MultiOAuth:
    def authorize_url(self, *, state: str) -> str:
        return f"https://oauth.example/authorize?state={state}"

    async def exchange(self, code: str) -> dict[str, str]:
        actor = code.removeprefix("oauth-code-")
        return {"actor_id": f"ou_{actor}", "display_name": actor.title()}


class FakeTimeoutAfterAccept:
    def __init__(self) -> None:
        self.by_uuid: dict[str, dict[str, str]] = {}
        self.calls = 0
        self.card_by_uuid: dict[str, dict[str, str]] = {}
        self.card_calls = 0

    async def send_message(self, **request) -> dict[str, str]:
        self.calls += 1
        result = self.by_uuid.setdefault(
            request["uuid"],
            {"message_id": "om_once", "conversation_id": "oc_alice"},
        )
        if self.calls == 1:
            raise TimeoutError
        return result

    async def send_card(self, **request) -> dict[str, str]:
        self.card_calls += 1
        result = self.card_by_uuid.setdefault(
            request["uuid"],
            {"message_id": "om_card_once", "conversation_id": "oc_alice"},
        )
        if self.card_calls == 1:
            raise TimeoutError
        return result


def _install_fake_lark(monkeypatch, client_type, sdk_ws_client):
    class Dispatcher:
        def __init__(self):
            self.handlers = {}

        @classmethod
        def builder(cls, *_args):
            return cls()

        def __getattr__(self, name):
            if name.startswith("register_"):
                def register(handler):
                    self.handlers[name] = handler
                    return self

                return register
            raise AttributeError(name)

        def build(self):
            return self

    lark = types.ModuleType("lark_oapi")
    lark.__path__ = []
    lark.JSON = types.SimpleNamespace(marshal=lambda value: value)
    lark.LogLevel = types.SimpleNamespace(ERROR="ERROR")
    lark_ws = types.ModuleType("lark_oapi.ws")
    lark_ws.__path__ = []
    lark_ws.Client = client_type
    lark_ws.client = sdk_ws_client
    lark.ws = lark_ws
    lark_event = types.ModuleType("lark_oapi.event")
    lark_event.__path__ = []
    dispatcher = types.ModuleType("lark_oapi.event.dispatcher_handler")
    dispatcher.EventDispatcherHandler = Dispatcher
    monkeypatch.setitem(sys.modules, "lark_oapi", lark)
    monkeypatch.setitem(sys.modules, "lark_oapi.ws", lark_ws)
    monkeypatch.setitem(sys.modules, "lark_oapi.ws.client", sdk_ws_client)
    monkeypatch.setitem(sys.modules, "lark_oapi.event", lark_event)
    monkeypatch.setitem(sys.modules, "lark_oapi.event.dispatcher_handler", dispatcher)


def test_event_stream_uses_a_thread_private_event_loop(monkeypatch):
    """The SDK's module-global loop must not reuse aiohttp's running loop."""

    connected = threading.Event()
    drained = threading.Event()
    finished = threading.Event()
    failures: list[str] = []
    sdk_ws_client = types.ModuleType("lark_oapi.ws.client")

    class Client:
        def __init__(self, *_args, **_kwargs):
            self._auto_reconnect = True
            self._conn = None

        async def _connect(self):
            self._conn = object()

        async def _disconnect(self):
            self._conn = None

        def start(self):
            async def stay_connected():
                try:
                    await asyncio.Event().wait()
                finally:
                    drained.set()

            try:
                sdk_ws_client.loop.run_until_complete(self._connect())
                connected.set()
                sdk_ws_client.loop.run_until_complete(stay_connected())
            except RuntimeError as exc:
                if str(exc) != "Event loop stopped before Future completed.":
                    failures.append(str(exc))
            finally:
                finished.set()

    _install_fake_lark(monkeypatch, Client, sdk_ws_client)

    from hermes_multitenancy.agent_relay_feishu import FeishuRelayClient

    async def runner():
        running_loop = asyncio.get_running_loop()
        sdk_ws_client.loop = running_loop
        relay = FeishuRelayClient("cli_test", "secret", "https://relay/callback")
        stop = relay.start_event_stream(object(), running_loop)
        assert await asyncio.to_thread(connected.wait, 2)
        assert failures == []
        assert sdk_ws_client.loop is not running_loop
        assert callable(stop)
        stop()
        assert await asyncio.to_thread(finished.wait, 2)
        assert drained.is_set()

    asyncio.run(runner())


def test_event_stream_startup_failure_is_raised(monkeypatch):
    sdk_ws_client = types.ModuleType("lark_oapi.ws.client")

    class Client:
        def __init__(self, *_args, **_kwargs):
            self._auto_reconnect = False
            self._conn = None

        async def _connect(self):
            raise RuntimeError("ws connect failed")

        async def _disconnect(self):
            return None

        def start(self):
            sdk_ws_client.loop.run_until_complete(self._connect())

    _install_fake_lark(monkeypatch, Client, sdk_ws_client)

    from hermes_multitenancy.agent_relay_feishu import FeishuRelayClient

    async def runner():
        running_loop = asyncio.get_running_loop()
        sdk_ws_client.loop = running_loop
        relay = FeishuRelayClient("cli_test", "secret", "https://relay/callback")
        with pytest.raises(RuntimeError, match="event stream failed to start"):
            relay.start_event_stream(object(), running_loop)

    asyncio.run(runner())


def test_event_stream_logs_background_message_failure(monkeypatch, caplog):
    sdk_ws_client = types.ModuleType("lark_oapi.ws.client")

    class Client:
        def __init__(self, *_args, **kwargs):
            self._auto_reconnect = True
            self._conn = None
            self.event_handler = kwargs["event_handler"]

        async def _connect(self):
            self._conn = object()

        async def _disconnect(self):
            self._conn = None

        def start(self):
            sdk_ws_client.loop.run_until_complete(self._connect())
            self.event_handler.handlers["register_p2_im_message_receive_v1"](
                json.dumps(
                    {
                        "header": {"event_id": "event-canary"},
                        "event": {
                            "sender": {"sender_id": {"open_id": "ou_actor_canary"}},
                            "message": {
                                "chat_type": "p2p",
                                "message_type": "text",
                                "content": '{"text":"MESSAGE-CANARY-DO-NOT-LOG"}',
                                "create_time": "1",
                            },
                        },
                    }
                )
            )
            sdk_ws_client.loop.run_forever()

    _install_fake_lark(monkeypatch, Client, sdk_ws_client)

    from hermes_multitenancy.agent_relay import RelayEvents
    from hermes_multitenancy.agent_relay_feishu import FeishuRelayClient

    class Store:
        def assign_reply(self, **_event):
            return False, True

    class FailingFeishu:
        async def send_ambiguity_notice(self, _actor_id):
            raise RuntimeError("TEXT-CANARY-DO-NOT-LOG")

    async def runner():
        relay = FeishuRelayClient("cli_test", "secret", "https://relay/callback")
        stop = relay.start_event_stream(
            RelayEvents(Store(), FailingFeishu()), asyncio.get_running_loop()
        )
        try:
            for _ in range(20):
                if "event=message status=failed" in caplog.text:
                    break
                await asyncio.sleep(0.01)
        finally:
            stop()

    with caplog.at_level(logging.ERROR):
        asyncio.run(runner())
    assert caplog.text.count("event=message status=failed") == 1
    assert "TEXT-CANARY-DO-NOT-LOG" not in caplog.text
    assert "event-canary" not in caplog.text


def test_text_ingress_logs_every_redacted_outcome(caplog):
    class Store:
        outcomes = iter([(False, False), (False, True), (True, False)])

        def assign_reply(self, **_event):
            return next(self.outcomes)

    from hermes_multitenancy.agent_relay import RelayEvents

    async def runner():
        events = RelayEvents(Store(), FakeFeishu())
        assert not await events.ingest_text(
            event_id="", actor_id="ou_actor_canary", text="MESSAGE-CANARY", create_time=1
        )
        for event_id in ("zero-window", "ambiguous", "assigned"):
            await events.ingest_text(
                event_id=event_id,
                actor_id="ou_actor_canary",
                text="MESSAGE-CANARY",
                create_time=1,
            )

    with caplog.at_level(logging.INFO):
        asyncio.run(runner())
    for status in ("invalid", "unmatched", "ambiguous", "assigned"):
        assert caplog.text.count(f"event=reply status={status}") == 1
    assert "ou_actor_canary" not in caplog.text
    assert "MESSAGE-CANARY" not in caplog.text


class FakeFailures(FakeFeishu):
    async def send_message(self, **request) -> dict[str, str]:
        from hermes_multitenancy.agent_relay import FeishuApiError

        raise FeishuApiError(429, 7, code=99991400, message="MESSAGE-UPSTREAM-CANARY")

    async def send_card(self, **request) -> dict[str, str]:
        from hermes_multitenancy.agent_relay import FeishuApiError

        self.cards.append(request)
        raise FeishuApiError(400, code=230099, message="CARD-UPSTREAM-CANARY")


async def enroll(client) -> tuple[str, str]:
    started = await client.post("/v1/enroll/sessions")
    body = await started.json()
    state = parse_qs(urlparse(body["authorize_url"]).query)["state"][0]
    callback = await client.get(
        "/v1/enroll/callback", params={"state": state, "code": "oauth-code-alice"}
    )
    assert callback.status == 200
    claimed = await client.get(f"/v1/enroll/sessions/{body['enroll_id']}")
    result = await claimed.json()
    return result["token"], result["token_id"]


def test_enrollment_binds_message_delivery_to_self_and_rejects_target_fields(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.agent_relay import create_agent_relay_app

    async def runner() -> None:
        feishu = FakeFeishu()
        oauth = FakeOAuth()
        app = create_agent_relay_app(
            db_path=tmp_path / "relay.db",
            encryption_key="test-encryption-key",
            oauth=oauth,
            feishu=feishu,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            for suffix in ("", "-wal", "-shm"):
                path = tmp_path / f"relay.db{suffix}"
                if path.exists():
                    assert stat.S_IMODE(path.stat().st_mode) == 0o600
            started = await client.post("/v1/enroll/sessions")
            started_body = await started.json()
            assert started.status == 201
            assert started_body["status"] == "pending"
            state = parse_qs(urlparse(started_body["authorize_url"]).query)["state"][0]

            callback = await client.get(
                "/v1/enroll/callback",
                params={"state": state, "code": "oauth-code-alice"},
            )
            assert callback.status == 200
            replayed_callback = await client.get(
                "/v1/enroll/callback",
                params={"state": state, "code": "oauth-code-alice"},
            )
            assert replayed_callback.status == 400
            assert oauth.exchanges == 1

            claimed = await client.get(
                f"/v1/enroll/sessions/{started_body['enroll_id']}"
            )
            claimed_body = await claimed.json()
            assert claimed.status == 200
            assert claimed_body["status"] == "completed"
            token = claimed_body["token"]
            token_id = claimed_body["token_id"]
            assert token.startswith("hm-relay-")
            assert claimed_body["user_name"] == "Alice"

            claimed_again = await client.get(
                f"/v1/enroll/sessions/{started_body['enroll_id']}"
            )
            assert await claimed_again.json() == {"status": "claimed"}

            auth = {"Authorization": f"Bearer {token}"}
            whoami = await client.get("/v1/whoami", headers=auth)
            whoami_body = await whoami.json()
            assert whoami.status == 200
            assert whoami_body == {
                "token_id": token_id,
                "user_name": "Alice",
                "identity_fingerprint": whoami_body["identity_fingerprint"],
                "issued_at": whoami_body["issued_at"],
                "status": "active",
            }
            assert len(whoami_body["identity_fingerprint"]) == 12
            assert "ou_alice" not in str(whoami_body)

            forbidden = await client.post(
                "/v1/messages",
                headers=auth,
                json={
                    "type": "text",
                    "content": {"text": "progress"},
                    "idempotency_key": "progress-1",
                    "target": "actor-mallory",
                },
            )
            assert forbidden.status == 400
            assert feishu.messages == []

            sent = await client.post(
                "/v1/messages",
                headers=auth,
                json={
                    "type": "text",
                    "content": {"text": "progress"},
                    "idempotency_key": "progress-1",
                },
            )
            assert sent.status == 201
            assert await sent.json() == {
                "message_id": "om_1",
                "conversation_id": "oc_self",
            }
            assert len(feishu.messages) == 1
            assert feishu.messages[0]["actor_id"] == "ou_alice"
            assert len(feishu.messages[0]["uuid"]) <= 50
            assert "actor_id" not in (await sent.json())
            cross_kind = await client.post(
                "/v1/cards",
                headers=auth,
                json={
                    "content": {"elements": []},
                    "actions": [{"id": "ok", "label": "OK"}],
                    "expires_in": 600,
                    "idempotency_key": "progress-1",
                },
            )
            assert cross_kind.status == 409
        finally:
            await client.close()

    asyncio.run(runner())


def test_replies_reactions_and_cards_are_isolated_and_deterministic(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.agent_relay import (
        FeishuRelayClient,
        RELAY_EVENTS_KEY,
        RELAY_STORE_KEY,
        create_agent_relay_app,
    )

    async def enroll_as(client, name: str) -> tuple[str, str]:
        started = await client.post("/v1/enroll/sessions")
        body = await started.json()
        state = parse_qs(urlparse(body["authorize_url"]).query)["state"][0]
        callback = await client.get(
            "/v1/enroll/callback",
            params={"state": state, "code": f"oauth-code-{name}"},
        )
        assert callback.status == 200
        claimed = await client.get(f"/v1/enroll/sessions/{body['enroll_id']}")
        result = await claimed.json()
        return result["token"], result["token_id"]

    async def runner() -> None:
        feishu = FakeFeishu()
        app = create_agent_relay_app(
            db_path=tmp_path / "relay.db",
            encryption_key="test-encryption-key",
            oauth=MultiOAuth(),
            feishu=feishu,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            alice_token, _ = await enroll_as(client, "alice")
            alice_second_device, _ = await enroll_as(client, "alice")
            bob_token, _ = await enroll_as(client, "bob")
            alice = {"Authorization": f"Bearer {alice_token}"}
            alice_other_device = {
                "Authorization": f"Bearer {alice_second_device}"
            }
            bob = {"Authorization": f"Bearer {bob_token}"}

            anchor = await client.post(
                "/v1/messages",
                headers=alice,
                json={
                    "type": "text",
                    "content": {"text": "continue?"},
                    "idempotency_key": "alice-anchor-1",
                    "reply_window_seconds": 600,
                },
            )
            anchor_id = (await anchor.json())["message_id"]
            bob_anchor = await client.post(
                "/v1/messages",
                headers=bob,
                json={
                    "type": "text",
                    "content": {"text": "bob continue?"},
                    "idempotency_key": "bob-anchor-1",
                    "reply_window_seconds": 600,
                },
            )
            assert bob_anchor.status == 201
            events = app[RELAY_EVENTS_KEY]
            transport = FeishuRelayClient("cli_test", "secret", "https://relay/callback")
            assert not await transport.ingest_event_payload(
                events,
                "message",
                {
                    "header": {"event_id": "evt-group"},
                    "event": {
                        "sender": {"sender_id": {"open_id": "ou_alice"}},
                        "message": {
                            "chat_type": "group",
                            "message_type": "text",
                            "content": '{"text":"ignored"}',
                            "create_time": "99",
                        },
                    },
                },
            )
            assert not await transport.ingest_event_payload(
                events,
                "message",
                {
                    "header": {"event_id": "evt-noncanonical"},
                    "event": {
                        "sender": {"sender_id": {"open_id": "actor-alice"}},
                        "message": {
                            "chat_type": "p2p",
                            "message_type": "text",
                            "content": '{"text":"ignored"}',
                            "create_time": "99",
                        },
                    },
                },
            )
            assert await transport.ingest_event_payload(
                events,
                "message",
                {
                    "header": {"event_id": "evt-a1"},
                    "event": {
                        "sender": {"sender_id": {"open_id": "ou_alice"}},
                        "message": {
                            "chat_type": "p2p",
                            "message_type": "text",
                            "content": '{"text":"continue"}',
                            "create_time": "100",
                        },
                    },
                },
            )
            assert not await events.ingest_text(
                event_id="evt-bad",
                actor_id="ou_bob",
                text="steal",
                create_time=101,
                parent_message_id=anchor_id,
            )
            replies = await client.get(
                f"/v1/messages/{anchor_id}/replies", headers=alice
            )
            assert await replies.json() == {
                "replies": [{"id": "evt-a1", "text": "continue", "create_time": 100}]
            }
            hidden = await client.get(
                f"/v1/messages/{anchor_id}/replies", headers=bob
            )
            assert hidden.status == 404
            same_user_other_device = await client.get(
                f"/v1/messages/{anchor_id}/replies", headers=alice_other_device
            )
            assert same_user_other_device.status == 404

            second = await client.post(
                "/v1/messages",
                headers=alice,
                json={
                    "type": "text",
                    "content": {"text": "another?"},
                    "idempotency_key": "alice-anchor-2",
                    "reply_window_seconds": 600,
                },
            )
            second_id = (await second.json())["message_id"]
            assert not await events.ingest_text(
                event_id="evt-ambiguous",
                actor_id="ou_alice",
                text="yes",
                create_time=102,
            )
            assert feishu.ambiguity_notices == ["ou_alice"]
            assert await events.ingest_text(
                event_id="evt-exact",
                actor_id="ou_alice",
                text="second",
                create_time=103,
                parent_message_id=second_id,
            )

            assert await transport.ingest_event_payload(
                events,
                "reaction_created",
                {
                    "header": {"event_id": "reaction-1"},
                    "event": {
                        "user_id": {"open_id": "ou_alice"},
                        "message_id": anchor_id,
                        "reaction_type": {"emoji_type": "THUMBSUP"},
                    },
                },
            )
            assert not await events.ingest_reaction(
                event_id="reaction-bad",
                actor_id="ou_bob",
                message_id=anchor_id,
                emoji_type="EYES",
                operation="create",
            )
            reactions = await client.get(
                f"/v1/messages/{anchor_id}/reactions", headers=alice
            )
            assert await reactions.json() == {"reactions": ["THUMBSUP"]}
            assert await events.ingest_reaction(
                event_id="reaction-1",
                actor_id="ou_alice",
                message_id=anchor_id,
                emoji_type="THUMBSUP",
                operation="delete",
            )
            reactions = await client.get(
                f"/v1/messages/{anchor_id}/reactions", headers=alice
            )
            assert await reactions.json() == {"reactions": []}
            assert await events.ingest_reaction(
                event_id="reaction-2",
                actor_id="ou_alice",
                message_id=anchor_id,
                emoji_type="EYES",
                operation="create",
            )

            card = await client.post(
                "/v1/cards",
                headers=alice,
                json={
                    "content": {
                        "header": {"title": {"tag": "plain_text", "content": "Approve?"}},
                        "elements": [{"tag": "markdown", "content": "Dangerous command"}],
                    },
                    "actions": [
                        {"id": "approve", "label": "Approve"},
                        {"id": "deny", "label": "Deny"},
                    ],
                    "expires_in": 600,
                    "idempotency_key": "card-1",
                },
            )
            assert card.status == 201
            card_body = await card.json()
            card_id = card_body["card_id"]
            sent_card = feishu.cards[0]
            assert len(sent_card["uuid"]) <= 50
            assert "nonce" not in str(card_body)
            assert not await events.ingest_card_action(
                actor_id="ou_bob",
                card_id=card_id,
                nonce=sent_card["nonce"],
                message_id=card_body["message_id"],
                action_id="approve",
            )
            assert await transport.ingest_event_payload(
                events,
                "card_action",
                {
                    "event": {
                        "operator": {"open_id": "ou_alice"},
                        "context": {"open_message_id": card_body["message_id"]},
                        "action": {
                            "value": {
                                "relay_action": True,
                                "card_id": card_id,
                                "nonce": sent_card["nonce"],
                                "action_id": "approve",
                            }
                        },
                    }
                },
            )
            assert not await events.ingest_card_action(
                actor_id="ou_alice",
                card_id=card_id,
                nonce=sent_card["nonce"],
                message_id=card_body["message_id"],
                action_id="deny",
            )
            card_state = await client.get(f"/v1/cards/{card_id}", headers=alice)
            assert (await card_state.json())["status"] == "actioned"
            assert (await client.get(f"/v1/cards/{card_id}", headers=bob)).status == 404
            raced = await client.patch(
                f"/v1/cards/{card_id}",
                headers=alice,
                json={"status": "closed", "reason": "local_resumed"},
            )
            assert raced.status == 409
            assert (await raced.json())["status"] == "actioned"
            # 点击不再由服务端重绘：调用方拿着原文做局部替换，服务端拼不出那个内容。
            # 状态保护不依赖重绘 —— 第二次点击 409 且飞书弹 Already resolved。
            assert feishu.card_updates == []

            closeable = await client.post(
                "/v1/cards",
                headers=alice,
                json={
                    "content": {"elements": []},
                    "actions": [{"id": "ok", "label": "OK"}],
                    "expires_in": 600,
                    "idempotency_key": "card-close",
                },
            )
            closeable_id = (await closeable.json())["card_id"]
            closed = await client.patch(
                f"/v1/cards/{closeable_id}",
                headers=alice,
                json={"status": "closed", "reason": "local_resumed"},
            )
            assert closed.status == 200
            assert (await closed.json())["status"] == "closed"
            assert len(feishu.card_updates) == 1

            await client.close()
            app = create_agent_relay_app(
                db_path=tmp_path / "relay.db",
                encryption_key="test-encryption-key",
                oauth=MultiOAuth(),
                feishu=feishu,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            assert (await client.get(f"/v1/cards/{card_id}", headers=alice)).status == 200
            replies_after_restart = await client.get(
                f"/v1/messages/{second_id}/replies", headers=alice
            )
            assert (await replies_after_restart.json())["replies"][0]["text"] == "second"
            reactions_after_restart = await client.get(
                f"/v1/messages/{anchor_id}/reactions", headers=alice
            )
            assert await reactions_after_restart.json() == {"reactions": ["EYES"]}

            assert app[RELAY_STORE_KEY].prune(now_ms=10**15) == 2
            purged = await client.get(
                f"/v1/messages/{anchor_id}/replies", headers=alice
            )
            assert await purged.json() == {"replies": []}
            with sqlite3.connect(tmp_path / "relay.db") as audit_db:
                audit = audit_db.execute(
                    "SELECT COUNT(*), COUNT(purged_at) FROM relay_replies"
                ).fetchone()
            assert audit == (2, 2)
        finally:
            await client.close()

    asyncio.run(runner())


def test_idempotency_survives_timeout_and_restart_then_token_can_revoke_itself(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.agent_relay import create_agent_relay_app

    async def runner() -> None:
        db_path = tmp_path / "relay.db"
        feishu = FakeTimeoutAfterAccept()
        app = create_agent_relay_app(
            db_path=db_path,
            encryption_key="test-encryption-key",
            oauth=FakeOAuth(),
            feishu=feishu,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        token, token_id = await enroll(client)
        second_token, _ = await enroll(client)
        auth = {"Authorization": f"Bearer {token}"}
        request = {
            "type": "text",
            "content": {"text": "exactly once"},
            "idempotency_key": "once-1",
        }
        first = await client.post("/v1/messages", headers=auth, json=request)
        assert first.status == 504
        retried = await client.post("/v1/messages", headers=auth, json=request)
        assert retried.status == 200
        assert await retried.json() == {
            "message_id": "om_once",
            "conversation_id": "oc_alice",
        }
        repeated = await client.post("/v1/messages", headers=auth, json=request)
        assert repeated.status == 200
        assert feishu.calls == 2
        conflict = await client.post(
            "/v1/messages",
            headers=auth,
            json={**request, "content": {"text": "different"}},
        )
        assert conflict.status == 409
        card_request = {
            "content": {"elements": []},
            "actions": [{"id": "approve", "label": "Approve"}],
            "expires_in": 600,
            "idempotency_key": "card-timeout-1",
        }
        card_timeout = await client.post("/v1/cards", headers=auth, json=card_request)
        assert card_timeout.status == 504
        card_retry = await client.post("/v1/cards", headers=auth, json=card_request)
        assert card_retry.status == 200
        assert (await card_retry.json())["status"] == "pending"
        assert feishu.card_calls == 2
        assert len(feishu.card_by_uuid) == 1
        await client.close()

        restarted = create_agent_relay_app(
            db_path=db_path,
            encryption_key="test-encryption-key",
            oauth=FakeOAuth(),
            feishu=feishu,
        )
        client = TestClient(TestServer(restarted))
        await client.start_server()
        try:
            repeated_after_restart = await client.post(
                "/v1/messages", headers=auth, json=request
            )
            assert repeated_after_restart.status == 200
            assert feishu.calls == 2
            revoked = await client.post(
                f"/v1/tokens/{token_id}/revoke", headers=auth
            )
            assert revoked.status == 200
            assert await revoked.json() == {"token_id": token_id, "status": "revoked"}
            denied = await client.get("/v1/whoami", headers=auth)
            assert denied.status == 401
            second_device = await client.get(
                "/v1/whoami",
                headers={"Authorization": f"Bearer {second_token}"},
            )
            assert second_device.status == 200
        finally:
            await client.close()

    asyncio.run(runner())


def test_rate_limit_failed_card_and_logs_do_not_leak_payloads(tmp_path, caplog, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.agent_relay import FeishuRelayClient, create_agent_relay_app

    def oauth_response(url: str, **_request):
        if url.endswith("/oauth/token"):
            return {"access_token": "user-token-canary"}
        return {"code": 0, "data": {"open_id": "ou_verified", "name": "Verified"}}

    monkeypatch.setattr(FeishuRelayClient, "_request_json", staticmethod(oauth_response))
    identity = FeishuRelayClient("cli_test", "secret", "https://relay/callback")._exchange("code")
    assert identity == {"actor_id": "ou_verified", "display_name": "Verified"}

    async def runner() -> None:
        feishu = FakeFailures()
        app = create_agent_relay_app(
            db_path=tmp_path / "relay.db",
            encryption_key="test-encryption-key",
            oauth=FakeOAuth(),
            feishu=feishu,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            token, _ = await enroll(client)
            auth = {"Authorization": f"Bearer {token}"}
            limited = await client.post(
                "/v1/messages",
                headers=auth,
                json={
                    "type": "text",
                    "content": {"text": "BODY-CANARY-DO-NOT-LOG"},
                    "idempotency_key": "limited-1",
                },
            )
            assert limited.status == 429
            assert limited.headers["Retry-After"] == "7"
            limited_error = (await limited.json())["error"]
            assert limited_error["retry_after"] == 7
            assert "code=99991400" in limited_error["message"]
            assert "MESSAGE-UPSTREAM-CANARY" in limited_error["message"]

            request = {
                "content": {"elements": []},
                "actions": [{"id": "approve", "label": "Approve"}],
                "expires_in": 600,
                "idempotency_key": "failed-card-1",
            }
            failed = await client.post("/v1/cards", headers=auth, json=request)
            assert failed.status == 400
            failed_error = (await failed.json())["error"]
            assert "code=230099" in failed_error["message"]
            assert "CARD-UPSTREAM-CANARY" in failed_error["message"]
            repeated = await client.post("/v1/cards", headers=auth, json=request)
            assert repeated.status == 200
            assert (await repeated.json())["status"] == "failed"
            assert len(feishu.cards) == 1
            assert "BODY-CANARY-DO-NOT-LOG" not in caplog.text
            assert "MESSAGE-UPSTREAM-CANARY" not in caplog.text
            assert "CARD-UPSTREAM-CANARY" not in caplog.text
            assert token not in caplog.text
            assert "ou_alice" not in caplog.text
        finally:
            await client.close()

    asyncio.run(runner())


def test_card_expiry_alias_normalizes_idempotency_and_validation_is_specific(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.agent_relay import create_agent_relay_app

    async def runner() -> None:
        feishu = FakeFeishu()
        app = create_agent_relay_app(
            db_path=tmp_path / "relay.db",
            encryption_key="test-encryption-key",
            oauth=FakeOAuth(),
            feishu=feishu,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            token, _ = await enroll(client)
            auth = {"Authorization": f"Bearer {token}"}
            base = {
                "content": {"elements": []},
                "actions": [{"id": "approve", "label": "Approve"}],
                "idempotency_key": "expiry-alias-1",
            }

            invalid_actions = await client.post(
                "/v1/cards", headers=auth, json={**base, "actions": ["approve"], "expiry": 600}
            )
            assert invalid_actions.status == 400
            assert (await invalid_actions.json())["error"]["message"] == (
                "actions must be 1..5 objects with exactly string id and label fields of 1..64 characters"
            )

            invalid_expiry = await client.post(
                "/v1/cards", headers=auth, json={**base, "expiry": "600"}
            )
            assert invalid_expiry.status == 400
            assert (await invalid_expiry.json())["error"]["message"] == (
                "expires_in or expiry must be integer TTL seconds from 300 to 1800"
            )

            duplicate_expiry = await client.post(
                "/v1/cards", headers=auth, json={**base, "expires_in": 600, "expiry": 600}
            )
            assert duplicate_expiry.status == 400
            assert (await duplicate_expiry.json())["error"]["message"] == (
                "use only one of expires_in or expiry"
            )

            alias = await client.post("/v1/cards", headers=auth, json={**base, "expiry": 600})
            assert alias.status == 201
            canonical = await client.post(
                "/v1/cards", headers=auth, json={**base, "expires_in": 600}
            )
            assert canonical.status == 200
            assert len(feishu.cards) == 1
        finally:
            await client.close()

    asyncio.run(runner())


def test_feishu_transport_preserves_application_error_details(monkeypatch):
    import io
    import urllib.error
    import urllib.request

    from hermes_multitenancy.agent_relay_feishu import FeishuApiError, FeishuRelayClient

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"code":230099,"msg":"bad card shape"}'

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    with pytest.raises(FeishuApiError) as raised:
        FeishuRelayClient._request_json("https://open.feishu.cn/open-apis/im/v1/messages")
    assert raised.value.code == 230099
    assert raised.value.message == "bad card shape"

    def fail_with(error):
        def raise_error(*_args, **_kwargs):
            raise error

        monkeypatch.setattr(urllib.request, "urlopen", raise_error)
        with pytest.raises(FeishuApiError) as caught:
            FeishuRelayClient._request_json("https://open.feishu.cn/open-apis/im/v1/messages")
        return caught.value

    http_error = urllib.error.HTTPError(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        429,
        "rate limited",
        {"Retry-After": "7"},
        io.BytesIO(b'{"code":99991400,"msg":"slow down"}'),
    )
    caught = fail_with(http_error)
    assert (caught.status, caught.retry_after, caught.code, caught.message) == (
        429,
        7,
        99991400,
        "slow down",
    )

    class BrokenBody:
        def read(self):
            raise OSError

        def close(self):
            return None

    for body in (io.BytesIO(b"not-json"), BrokenBody()):
        caught = fail_with(
            urllib.error.HTTPError("https://open.feishu.cn", 503, "unavailable", {}, body)
        )
        assert (caught.status, caught.code, caught.message) == (503, None, "")


def test_card_message_content_update_is_verbatim_and_owner_bound(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.agent_relay import create_agent_relay_app

    async def enroll_as(client, name: str) -> str:
        started = await client.post("/v1/enroll/sessions")
        body = await started.json()
        state = parse_qs(urlparse(body["authorize_url"]).query)["state"][0]
        await client.get(
            "/v1/enroll/callback",
            params={"state": state, "code": f"oauth-code-{name}"},
        )
        claimed = await client.get(f"/v1/enroll/sessions/{body['enroll_id']}")
        return (await claimed.json())["token"]

    async def runner() -> None:
        feishu = FakeFeishu()
        app = create_agent_relay_app(
            db_path=tmp_path / "relay.db",
            encryption_key="test-encryption-key",
            oauth=MultiOAuth(),
            feishu=feishu,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            alice = {"Authorization": f"Bearer {await enroll_as(client, 'alice')}"}
            bob = {"Authorization": f"Bearer {await enroll_as(client, 'bob')}"}

            schema_two = {
                "schema": "2.0",
                "body": {"elements": [{"tag": "markdown", "content": "step 1"}]},
            }
            posted = await client.post(
                "/v1/messages",
                headers=alice,
                json={
                    "type": "card",
                    "content": schema_two,
                    "idempotency_key": "alice-progress",
                },
            )
            assert posted.status == 201
            message_id = (await posted.json())["message_id"]

            updated_content = {
                "schema": "2.0",
                "body": {"elements": [{"tag": "markdown", "content": "step 2 of 9"}]},
            }
            updated = await client.patch(
                f"/v1/messages/{message_id}",
                headers=alice,
                json={"content": updated_content},
            )
            assert updated.status == 200
            assert feishu.card_updates[-1]["content"] == updated_content
            assert "elements" not in feishu.card_updates[-1]["content"]

            stranger = await client.patch(
                f"/v1/messages/{message_id}",
                headers=bob,
                json={"content": updated_content},
            )
            assert stranger.status == 404

            texted = await client.post(
                "/v1/messages",
                headers=alice,
                json={
                    "type": "text",
                    "content": {"text": "hi"},
                    "idempotency_key": "alice-text",
                },
            )
            text_id = (await texted.json())["message_id"]
            refused = await client.patch(
                f"/v1/messages/{text_id}",
                headers=alice,
                json={"content": {"elements": []}},
            )
            assert refused.status == 400
            assert (await refused.json())["error"]["code"] == "invalid_message"

            identity = await client.patch(
                f"/v1/messages/{message_id}",
                headers=alice,
                json={"content": {"elements": [{"open_id": "ou_bob"}]}},
            )
            assert identity.status == 400
            assert (await identity.json())["error"]["code"] == "identity_field_forbidden"

            oversized = await client.patch(
                f"/v1/messages/{message_id}",
                headers=alice,
                json={"content": {"elements": [{"tag": "markdown", "content": "x" * 32000}]}},
            )
            assert oversized.status == 400
            assert (await oversized.json())["error"]["code"] == "content_too_large"

            unauth = await client.patch(
                f"/v1/messages/{message_id}", json={"content": updated_content}
            )
            assert unauth.status == 401
        finally:
            await client.close()

    asyncio.run(runner())


def test_reply_window_can_be_closed_on_arrival(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.agent_relay import (
        RELAY_EVENTS_KEY,
        RELAY_STORE_KEY,
        create_agent_relay_app,
    )

    async def enroll_as(client, name: str) -> str:
        started = await client.post("/v1/enroll/sessions")
        body = await started.json()
        state = parse_qs(urlparse(body["authorize_url"]).query)["state"][0]
        await client.get(
            "/v1/enroll/callback",
            params={"state": state, "code": f"oauth-code-{name}"},
        )
        claimed = await client.get(f"/v1/enroll/sessions/{body['enroll_id']}")
        return (await claimed.json())["token"]

    async def runner() -> None:
        feishu = FakeFeishu()
        app = create_agent_relay_app(
            db_path=tmp_path / "relay.db",
            encryption_key="test-encryption-key",
            oauth=MultiOAuth(),
            feishu=feishu,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        events = app[RELAY_EVENTS_KEY]
        store = app[RELAY_STORE_KEY]
        try:
            alice = {"Authorization": f"Bearer {await enroll_as(client, 'alice')}"}
            first = await client.post(
                "/v1/messages",
                headers=alice,
                json={
                    "type": "text",
                    "content": {"text": "round 1?"},
                    "idempotency_key": "round-1",
                    "reply_window_seconds": 300,
                },
            )
            first_id = (await first.json())["message_id"]

            closed = await client.patch(
                f"/v1/messages/{first_id}",
                headers=alice,
                json={"reply_window_seconds": 0},
            )
            assert closed.status == 200

            # the window is shut: a plain reply no longer lands anywhere
            assert not await events.ingest_text(
                actor_id="ou_alice",
                event_id="evt-late",
                text="too late",
                create_time=int(time.time() * 1000),
            )
            replies = await client.get(f"/v1/messages/{first_id}/replies", headers=alice)
            assert (await replies.json())["replies"] == []

            # retention still works: the stamp is a real timestamp, never NULL
            with store._lock:
                stamp = store._conn.execute(
                    "SELECT reply_expires_at FROM relay_messages WHERE message_id=?",
                    (first_id,),
                ).fetchone()["reply_expires_at"]
            assert stamp is not None and stamp <= int(time.time() * 1000)

            # closing again is a 409, not a silent success
            again = await client.patch(
                f"/v1/messages/{first_id}",
                headers=alice,
                json={"reply_window_seconds": 0},
            )
            assert again.status == 409

            # round 2 opens a fresh window and is unambiguous again
            second = await client.post(
                "/v1/messages",
                headers=alice,
                json={
                    "type": "text",
                    "content": {"text": "round 2?"},
                    "idempotency_key": "round-2",
                    "reply_window_seconds": 300,
                },
            )
            second_id = (await second.json())["message_id"]
            assert await events.ingest_text(
                actor_id="ou_alice",
                event_id="evt-round-2",
                text="批准",
                create_time=int(time.time() * 1000),
            )
            assert feishu.ambiguity_notices == []
            landed = await client.get(f"/v1/messages/{second_id}/replies", headers=alice)
            assert [r["text"] for r in (await landed.json())["replies"]] == ["批准"]
        finally:
            await client.close()

    asyncio.run(runner())


def test_button_card_gets_a_text_reply_fallback(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.agent_relay import (
        RELAY_EVENTS_KEY,
        create_agent_relay_app,
    )

    async def enroll_as(client, name: str) -> str:
        started = await client.post("/v1/enroll/sessions")
        body = await started.json()
        state = parse_qs(urlparse(body["authorize_url"]).query)["state"][0]
        await client.get(
            "/v1/enroll/callback",
            params={"state": state, "code": f"oauth-code-{name}"},
        )
        claimed = await client.get(f"/v1/enroll/sessions/{body['enroll_id']}")
        return (await claimed.json())["token"]

    async def runner() -> None:
        feishu = FakeFeishu()
        app = create_agent_relay_app(
            db_path=tmp_path / "relay.db",
            encryption_key="test-encryption-key",
            oauth=MultiOAuth(),
            feishu=feishu,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        events = app[RELAY_EVENTS_KEY]
        try:
            alice = {"Authorization": f"Bearer {await enroll_as(client, 'alice')}"}
            posted = await client.post(
                "/v1/cards",
                headers=alice,
                json={
                    "content": {"elements": []},
                    "actions": [{"id": "ok", "label": "OK"}],
                    "expires_in": 600,
                    "idempotency_key": "card-fallback",
                },
            )
            assert posted.status == 201
            body = await posted.json()
            assert set(body) == {"card_id", "status", "message_id", "expires_at"}
            card_message_id = body["message_id"]

            assert await events.ingest_text(
                actor_id="ou_alice",
                event_id="evt-card-reply",
                text="批准",
                create_time=int(time.time() * 1000),
                parent_message_id=card_message_id,
            )
            replies = await client.get(
                f"/v1/messages/{card_message_id}/replies", headers=alice
            )
            assert replies.status == 200
            assert [r["text"] for r in (await replies.json())["replies"]] == ["批准"]
        finally:
            await client.close()

    asyncio.run(runner())


def test_card_fallback_row_carries_the_card_ttl_and_never_breaks_the_card(tmp_path, caplog):
    """The registered row must expire WITH the card, and its failure must stay invisible."""
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.agent_relay import (
        RELAY_EVENTS_KEY,
        RELAY_STORE_KEY,
        create_agent_relay_app,
    )

    async def enroll_as(client, name: str) -> str:
        started = await client.post("/v1/enroll/sessions")
        body = await started.json()
        state = parse_qs(urlparse(body["authorize_url"]).query)["state"][0]
        await client.get(
            "/v1/enroll/callback",
            params={"state": state, "code": f"oauth-code-{name}"},
        )
        claimed = await client.get(f"/v1/enroll/sessions/{body['enroll_id']}")
        return (await claimed.json())["token"]

    async def runner() -> None:
        feishu = FakeFeishu()
        app = create_agent_relay_app(
            db_path=tmp_path / "relay.db",
            encryption_key="test-encryption-key",
            oauth=MultiOAuth(),
            feishu=feishu,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        store = app[RELAY_STORE_KEY]
        try:
            alice = {"Authorization": f"Bearer {await enroll_as(client, 'alice')}"}
            made = await client.post(
                "/v1/cards",
                headers=alice,
                json={
                    "content": {"elements": []},
                    "actions": [{"id": "ok", "label": "OK"}],
                    "expires_in": 600,
                    "idempotency_key": "ttl-card",
                },
            )
            body = await made.json()
            with store._lock:
                row = store._conn.execute(
                    "SELECT reply_expires_at, idempotency_key FROM relay_messages WHERE message_id=?",
                    (body["message_id"],),
                ).fetchone()
            # window ends exactly WITH the card, not a fresh clock read after the send
            assert row["reply_expires_at"] == body["expires_at"]
            # raw key: a later POST /v1/messages reusing it must 409, never 500
            reused = await client.post(
                "/v1/messages",
                headers=alice,
                json={
                    "type": "text",
                    "content": {"text": "hi"},
                    "idempotency_key": "ttl-card",
                },
            )
            assert reused.status == 409
            assert (await reused.json())["error"]["code"] == "idempotency_conflict"

            # a live card + a live message = two windows → unreferenced replies are ambiguous
            await client.post(
                "/v1/messages",
                headers=alice,
                json={
                    "type": "text",
                    "content": {"text": "and?"},
                    "idempotency_key": "second-window",
                    "reply_window_seconds": 300,
                },
            )
            events = app[RELAY_EVENTS_KEY]
            assert not await events.ingest_text(
                actor_id="ou_alice",
                event_id="evt-ambiguous",
                text="哪个?",
                create_time=int(time.time() * 1000),
            )
            assert feishu.ambiguity_notices == ["ou_alice"]

            # registration failure must not touch the card's own response
            store.register_card_message = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))
            broken = await client.post(
                "/v1/cards",
                headers=alice,
                json={
                    "content": {"elements": []},
                    "actions": [{"id": "ok", "label": "OK"}],
                    "expires_in": 600,
                    "idempotency_key": "card-register-broken",
                },
            )
            assert broken.status == 201
            assert (await broken.json())["status"] == "pending"
        finally:
            await client.close()

    with caplog.at_level("WARNING"):
        asyncio.run(runner())
    assert "fallback_register_failed" in caplog.text


def test_card_content_patch_keeps_the_guards_the_other_routes_have(tmp_path):
    """The content route must not be a softer door than POST /v1/cards."""
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.agent_relay import create_agent_relay_app

    async def enroll_as(client, name: str) -> str:
        started = await client.post("/v1/enroll/sessions")
        body = await started.json()
        state = parse_qs(urlparse(body["authorize_url"]).query)["state"][0]
        await client.get(
            "/v1/enroll/callback",
            params={"state": state, "code": f"oauth-code-{name}"},
        )
        claimed = await client.get(f"/v1/enroll/sessions/{body['enroll_id']}")
        return (await claimed.json())["token"]

    async def runner() -> None:
        feishu = FakeFeishu()
        app = create_agent_relay_app(
            db_path=tmp_path / "relay.db",
            encryption_key="test-encryption-key",
            oauth=MultiOAuth(),
            feishu=feishu,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            alice = {"Authorization": f"Bearer {await enroll_as(client, 'alice')}"}
            made = await client.post(
                "/v1/cards",
                headers=alice,
                json={
                    "content": {"elements": []},
                    "actions": [{"id": "ok", "label": "OK"}],
                    "expires_in": 600,
                    "idempotency_key": "guard-card",
                },
            )
            card_id = (await made.json())["card_id"]
            before = len(feishu.card_updates)

            # identity keys are rejected at ANY depth, exactly like POST /v1/cards
            identity = await client.patch(
                f"/v1/cards/{card_id}",
                headers=alice,
                json={"content": {"elements": [{"tag": "markdown", "open_id": "ou_bob"}]}},
            )
            assert identity.status == 400
            assert (await identity.json())["error"]["code"] == "identity_field_forbidden"

            oversized = await client.patch(
                f"/v1/cards/{card_id}",
                headers=alice,
                json={"content": {"elements": [{"tag": "markdown", "content": "x" * 32000}]}},
            )
            assert oversized.status == 400
            assert (await oversized.json())["error"]["code"] == "content_too_large"

            # neither rejection may reach Feishu
            assert len(feishu.card_updates) == before

            # p1: the empty body must keep its historical error code
            empty = await client.patch(f"/v1/cards/{card_id}", headers=alice, json={})
            assert empty.status == 400
            assert (await empty.json())["error"]["code"] == "invalid_close"

            # p1: Feishu transport failures map like the sibling routes do
            real_update = feishu.update_card

            async def hang(**kw):
                raise TimeoutError

            feishu.update_card = hang
            timed_out = await client.patch(
                f"/v1/cards/{card_id}",
                headers=alice,
                json={"content": {"elements": []}},
            )
            assert timed_out.status == 504
            assert (await timed_out.json())["error"]["code"] == "upstream_timeout"

            from hermes_multitenancy.agent_relay import FeishuApiError

            async def rejected(**kw):
                raise FeishuApiError(400, None, code=230001, message="bad card")

            feishu.update_card = rejected
            refused = await client.patch(
                f"/v1/cards/{card_id}",
                headers=alice,
                json={"content": {"elements": []}},
            )
            assert refused.status == 400
            assert (await refused.json())["error"]["code"] == "invalid_card"
            feishu.update_card = real_update

            # a close with NO content still drives the card to its terminal look —
            # this is the shape every pre-existing client sends
            closed = await client.patch(
                f"/v1/cards/{card_id}",
                headers=alice,
                json={"status": "closed", "reason": "client_timeout"},
            )
            assert closed.status == 200
            assert (await closed.json())["status"] == "closed"
            assert len(feishu.card_updates) == before + 1
            assert feishu.card_updates[-1].get("status") == "closed"
        finally:
            await client.close()

    asyncio.run(runner())


def test_update_card_ships_the_composed_card_not_the_raw_argument():
    """Transport-level: FakeFeishu only records kwargs, so the HTTP body needs its own test."""
    from hermes_multitenancy.agent_relay_feishu import FeishuRelayClient

    client = FeishuRelayClient.__new__(FeishuRelayClient)
    sent: list[dict] = []
    client._tenant_access = lambda: "t-token"
    client._request_json = lambda url, **kw: sent.append({"url": url, **kw}) or {}

    # no caller content → the server-composed terminal card must go out, never "null"
    client._update_card(message_id="om_x", status="closed")
    body = json.loads(sent[-1]["body"]["content"])
    assert body == {"elements": [{"tag": "markdown", "content": "Status: closed"}]}

    client._update_card(message_id="om_x", status="actioned", action_id="approve")
    assert json.loads(sent[-1]["body"]["content"])["elements"][0]["content"] == (
        "Status: actioned (approve)"
    )

    # caller content → verbatim, not re-wrapped
    caller = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "done"}]}}
    client._update_card(message_id="om_x", content=caller)
    assert json.loads(sent[-1]["body"]["content"]) == caller
