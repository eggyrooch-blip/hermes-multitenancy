from __future__ import annotations

from types import SimpleNamespace
from typing import Any
import json
import hashlib

import pytest

from hermes_multitenancy import router


def _event(
    *,
    text: str = "/feishu_auth",
    chat_id: str = "oc_auth",
    message_id: str = "om_real",
    sender_open_id: str = "ou_sender",
    user_id: str = "ou_sender",
    user_id_alt: str = "on_sender",
    message_type: Any = None,
) -> SimpleNamespace:
    event = SimpleNamespace(
        text=text,
        message_id=message_id,
        sender_open_id=sender_open_id,
        source=SimpleNamespace(
            chat_id=chat_id,
            message_id=message_id,
            user_id=user_id,
            user_id_alt=user_id_alt,
            open_id=sender_open_id,
            chat_type="p2p",
            platform=SimpleNamespace(value="feishu"),
        ),
        raw_event={"event": {"message": {"message_id": message_id}}},
    )
    if message_type is not None:
        event.message_type = message_type
    return event


@pytest.fixture(autouse=True)
def _clear_replay_store() -> None:
    router._pending_auth_replay.clear()
    yield
    router._pending_auth_replay.clear()


@pytest.mark.asyncio
async def test_dispatch_synthetic_auth_complete_replays_captured_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[Any, Any]] = []

    async def fake_handle_async(*, event: Any, gateway: Any) -> None:
        captured.append((event, gateway))

    router._capture_pending_auth_replay("alice", "ou_sender", "把上周销售额导出到飞书表格")
    monkeypatch.setattr(router, "handle_async", fake_handle_async)

    dispatched = await router._dispatch_synthetic_auth_complete(
        event=_event(),
        gateway=SimpleNamespace(adapters={}),
        chat_id="oc_auth",
        profile_name="alice",
        open_id="ou_sender",
    )

    assert dispatched is True
    assert len(captured) == 1
    assert captured[0][0].text == "把上周销售额导出到飞书表格"
    assert router._pending_auth_replay_key("alice", "ou_sender") not in router._pending_auth_replay


@pytest.mark.asyncio
async def test_strict_auth_complete_reports_resend_without_replaying_model_or_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    resumed: list[tuple[Any, str]] = []
    model_dispatches: list[Any] = []

    sent: list[tuple[str, str]] = []

    def fake_resume(profile_home, subject, chat_id):
        resumed.append((profile_home, subject, chat_id))
        return {
            "ok": False,
            "error_code": "FEISHU_OPERATION_RESEND_REQUIRED",
            "resend_required": True,
        }

    class Adapter:
        async def send(self, chat_id, text):
            sent.append((chat_id, text))

    async def fake_handle_async(*, event: Any, gateway: Any) -> None:
        model_dispatches.append(event)

    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    monkeypatch.setattr(router, "_profile_name_to_home", lambda _name: tmp_path / "alice")
    monkeypatch.setattr(router, "_resume_pending_lark_cli_step", fake_resume)
    monkeypatch.setattr(router, "handle_async", fake_handle_async)
    router._capture_pending_auth_replay("alice", "ou_sender", "含业务文档内容的原请求")

    dispatched = await router._dispatch_synthetic_auth_complete(
        event=_event(),
        gateway=SimpleNamespace(adapters={"feishu": Adapter()}),
        chat_id="oc_auth",
        profile_name="alice",
        open_id="ou_sender",
    )

    assert dispatched is True
    assert resumed == [(tmp_path / "alice", "ou_sender", "oc_auth")]
    assert model_dispatches == []
    assert sent == [
        (
            "oc_auth",
            "飞书授权已完成。为保护消息内容，原请求未保存也不会自动重放，请重新发送原请求。",
        )
    ]
    assert router._pending_auth_replay == {}


def test_durable_resume_never_reads_transcript_payload_and_keeps_actor_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.operation_checkpoint import OperationCheckpointStore

    profile = tmp_path / "alice"
    (profile / "state").mkdir(parents=True)
    store = OperationCheckpointStore(profile / "state" / "operation-checkpoints.db")
    store.put(
        operation_id="op-1",
        profile_name="alice",
        subject="ou_alice",
        connector="lark-cli",
        intent_key="call:session-1:call-1",
        step="execute",
        state="waiting_auth",
        session_ref="session-1",
        call_ref="call-1",
        tool_scope="feishu:user",
        chat_type="p2p",
        chat_fence=hashlib.sha256(b"oc_auth").hexdigest(),
    )
    store.close()

    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")

    assert agent_real.resume_pending_lark_cli_step(
        profile,
        "ou_alice",
        trusted_chat_id="oc_other",
        session_ref="session-1",
        call_ref="call-1",
    ) is None

    result = agent_real.resume_pending_lark_cli_step(
        profile,
        "ou_alice",
        trusted_chat_id="oc_auth",
        session_ref="session-1",
        call_ref="call-1",
    )
    assert result == {
        "ok": False,
        "recovered": False,
        "failure_subsystem": "permission",
        "error_code": "FEISHU_OPERATION_RESEND_REQUIRED",
        "retryable": False,
        "resend_required": True,
    }
    assert agent_real.resume_pending_lark_cli_step(
        profile, "ou_bob", trusted_chat_id="oc_auth"
    ) is None


@pytest.mark.asyncio
async def test_dispatch_synthetic_auth_complete_falls_back_when_request_was_not_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Any] = []

    async def fake_handle_async(*, event: Any, gateway: Any) -> None:
        captured.append(event)

    router._capture_pending_auth_replay("alice", "ou_sender", "/feishu_auth")
    router._capture_pending_auth_replay("alice", "ou_sender", "")
    monkeypatch.setattr(router, "handle_async", fake_handle_async)

    dispatched = await router._dispatch_synthetic_auth_complete(
        event=_event(),
        gateway=SimpleNamespace(adapters={}),
        chat_id="oc_auth",
        profile_name="alice",
        open_id="ou_sender",
    )

    assert dispatched is True
    assert len(captured) == 1
    assert captured[0].text == router.SYNTHETIC_AUTH_COMPLETE_TEXT
    assert router._pending_auth_replay == {}


@pytest.mark.asyncio
async def test_dispatch_synthetic_auth_complete_skips_stale_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Any] = []

    async def fake_handle_async(*, event: Any, gateway: Any) -> None:
        captured.append(event)

    router._capture_pending_auth_replay("alice", "ou_sender", "这条请求已经过期")
    router._pending_auth_replay[router._pending_auth_replay_key("alice", "ou_sender")] = (
        router.time.time() - (router._PENDING_AUTH_REPLAY_TTL_SECONDS + 5),
        "stale request",
    )
    monkeypatch.setattr(router, "handle_async", fake_handle_async)

    dispatched = await router._dispatch_synthetic_auth_complete(
        event=_event(),
        gateway=SimpleNamespace(adapters={}),
        chat_id="oc_auth",
        profile_name="alice",
        open_id="ou_sender",
    )

    assert dispatched is True
    assert len(captured) == 1
    assert captured[0].text == router.SYNTHETIC_AUTH_COMPLETE_TEXT


@pytest.mark.asyncio
async def test_dispatch_synthetic_auth_complete_keeps_requests_isolated_by_profile_and_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Any] = []

    async def fake_handle_async(*, event: Any, gateway: Any) -> None:
        captured.append(event)

    router._capture_pending_auth_replay("profileA", "uidA", "只属于 A 的请求")
    monkeypatch.setattr(router, "handle_async", fake_handle_async)

    dispatched = await router._dispatch_synthetic_auth_complete(
        event=_event(sender_open_id="uidB", user_id="uidB", user_id_alt="on_uid_b"),
        gateway=SimpleNamespace(adapters={}),
        chat_id="oc_auth",
        profile_name="profileB",
        open_id="uidB",
    )

    assert dispatched is True
    assert len(captured) == 1
    assert captured[0].text == router.SYNTHETIC_AUTH_COMPLETE_TEXT
    assert router._pending_auth_replay[router._pending_auth_replay_key("profileA", "uidA")][1] == "只属于 A 的请求"


@pytest.mark.asyncio
@pytest.mark.parametrize("message_type", ["interactive", "card"])
async def test_handle_async_does_not_capture_interactive_or_card_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    message_type: str,
) -> None:
    captured_calls: list[tuple[str, str, str]] = []

    def fake_capture(profile_name: str, open_id: str, text: str) -> None:
        captured_calls.append((profile_name, open_id, text))

    class FakeRunBroker:
        async def admit(self, request: Any) -> Any:
            return SimpleNamespace(duplicate=True)

    async def fake_enrich(event: Any, gateway: Any, *, profile_home) -> str:
        del event, gateway, profile_home
        return ""

    monkeypatch.setattr(router, "_capture_pending_auth_replay", fake_capture)
    monkeypatch.setattr(router, "_resolve_or_auto_provision_route", lambda sender, alt_id=None: ("alice", tmp_path))
    monkeypatch.setattr(router, "_get_feishu_adapter", lambda gateway: None)
    monkeypatch.setattr(router, "_materialize_inbound_media_for_profile", lambda event, profile_home: None)
    monkeypatch.setattr(router, "_call_enrich_via_hermes_pipeline", fake_enrich)
    monkeypatch.setattr(router, "_image_vision_unavailable_response", lambda event, enriched_text: None)
    monkeypatch.setattr(router, "_make_routed_run_broker", lambda dispatch_agent=None: FakeRunBroker())

    await router.handle_async(
        event=_event(text="这是一条按钮回调", message_type=message_type),
        gateway=SimpleNamespace(adapters={}),
    )

    assert captured_calls == []
