"""Synthetic auth-complete dispatch after successful Feishu auth."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from hermes_multitenancy import router


def _event(chat_id: str = "oc_auth", message_id: str = "om_real") -> SimpleNamespace:
    return SimpleNamespace(
        text="/feishu_auth",
        message_id=message_id,
        sender_open_id="ou_sender",
        source=SimpleNamespace(
            chat_id=chat_id,
            message_id=message_id,
            user_id="ou_sender",
            user_id_alt="on_sender",
            open_id="ou_sender",
            chat_type="p2p",
            platform=SimpleNamespace(value="feishu"),
        ),
        raw_event={"event": {"message": {"message_id": message_id}}},
    )


@pytest.mark.asyncio
async def test_dispatch_synthetic_auth_complete_builds_event_and_calls_handle_async(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[Any, Any]] = []

    async def fake_handle_async(*, event: Any, gateway: Any) -> None:
        captured.append((event, gateway))

    monkeypatch.setattr(router, "handle_async", fake_handle_async)
    gateway = SimpleNamespace(adapters={})
    original = _event()

    dispatched = await router._dispatch_synthetic_auth_complete(
        event=original,
        gateway=gateway,
        chat_id="oc_auth",
        profile_name="alice",
        open_id="ou_sender",
    )

    assert dispatched is True
    assert len(captured) == 1
    synthetic, captured_gateway = captured[0]
    assert captured_gateway is gateway
    assert synthetic.text == router.SYNTHETIC_AUTH_COMPLETE_TEXT
    assert synthetic.source.chat_id == "oc_auth"
    assert router._event_message_id(synthetic) != router._event_message_id(original)
    assert router._event_message_id(synthetic).endswith(":auth-complete")
    assert synthetic.sender_open_id == "ou_sender"
    assert synthetic.source.open_id == "ou_sender"


@pytest.mark.asyncio
async def test_dispatch_synthetic_auth_complete_fails_open_when_dispatch_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_handle_async(*, event: Any, gateway: Any) -> None:
        raise RuntimeError("dispatch failed")

    monkeypatch.setattr(router, "handle_async", fake_handle_async)

    dispatched = await router._dispatch_synthetic_auth_complete(
        event=_event(),
        gateway=SimpleNamespace(adapters={}),
        chat_id="oc_auth",
        profile_name="alice",
        open_id="ou_sender",
    )

    assert dispatched is False


@pytest.mark.parametrize(
    ("chat_id", "profile_name"),
    [
        ("", "alice"),
        ("oc_auth", ""),
    ],
)
@pytest.mark.asyncio
async def test_dispatch_synthetic_auth_complete_skips_without_ai_context(
    monkeypatch: pytest.MonkeyPatch,
    chat_id: str,
    profile_name: str,
) -> None:
    called = False

    async def fake_handle_async(*, event: Any, gateway: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(router, "handle_async", fake_handle_async)

    dispatched = await router._dispatch_synthetic_auth_complete(
        event=_event(chat_id=chat_id),
        gateway=SimpleNamespace(adapters={}),
        chat_id=chat_id,
        profile_name=profile_name,
        open_id="ou_sender",
    )

    assert dispatched is False
    assert called is False


@pytest.mark.asyncio
async def test_auth_poll_success_branch_dispatches_synthetic_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_multitenancy import feishu_auth_cards
    from hermes_multitenancy import feishu_uat_auth

    event = _event()
    dispatches: list[dict[str, Any]] = []
    card_updates: list[dict[str, Any]] = []

    async def fake_sleep(delay: float) -> None:
        return None

    async def fake_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    async def fake_update_auth_card(*, adapter: Any, auth_card: Any, card: dict[str, Any]) -> bool:
        card_updates.append(card)
        return True

    async def fake_dispatch_synthetic_auth_complete(**kwargs: Any) -> bool:
        dispatches.append(kwargs)
        return True

    monkeypatch.setattr(router.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(router.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(router, "_get_feishu_adapter", lambda gateway: SimpleNamespace())
    monkeypatch.setattr(feishu_uat_auth, "poll_session", lambda **kwargs: {"status": "success"})
    monkeypatch.setattr(feishu_auth_cards, "update_auth_card", fake_update_auth_card)
    monkeypatch.setattr(router, "_dispatch_synthetic_auth_complete", fake_dispatch_synthetic_auth_complete, raising=False)

    await router._poll_feishu_auth_session_until_done(
        session_id="sess-1",
        profile_name="alice",
        open_id="ou_sender",
        chat_id="oc_auth",
        gateway=SimpleNamespace(adapters={}),
        event=event,
        interval=0,
        auth_card={"message_id": "om_card"},
    )

    assert card_updates
    assert len(dispatches) == 1
    assert dispatches[0]["event"] is event
    assert dispatches[0]["gateway"].adapters == {}
    assert dispatches[0]["chat_id"] == "oc_auth"
    assert dispatches[0]["profile_name"] == "alice"
    assert dispatches[0]["open_id"] == "ou_sender"
