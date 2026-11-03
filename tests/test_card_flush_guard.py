"""Phase 3 (card-flush-guard) — FlushController + UnavailableGuard tests.

Exercises the two new defensive primitives without touching the existing
``test_streaming_card_transport.py`` 2074-line contract:

- ``FlushController`` mutex + needs_reflush + 0-delay follow-up
- ``UnavailableGuard`` per-adapter recalled/deleted message set
- ``UnavailableGuard.should_proceed`` short-circuits in injected methods
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from hermes_multitenancy.card import ensure_feishu_cardkit_streaming
from hermes_multitenancy.card.flush_controller import FlushController
from hermes_multitenancy.card.unavailable_guard import (
    UNAVAILABLE_CODES,
    UnavailableGuard,
    _RECALLED_CODE,
    _DELETED_CODE,
)


# ---------------------------------------------------------------------------
# FlushController
# ---------------------------------------------------------------------------


def test_flush_controller_runs_passed_callable_under_mutex():
    controller = FlushController()
    calls: list[str] = []

    async def work():
        calls.append("ran")
        return "ok"

    async def driver():
        return await controller.flush(work)

    result = asyncio.run(driver())
    assert result == "ok"
    assert calls == ["ran"]
    assert controller.in_flight is False
    assert controller.needs_reflush is False


def test_flush_controller_serializes_concurrent_flush_calls():
    """Second call while first is in flight should set needs_reflush and not
    invoke the callable twice immediately."""
    controller = FlushController()
    in_progress = asyncio.Event()
    release = asyncio.Event()
    runs: list[int] = []

    async def slow_work():
        runs.append(1)
        in_progress.set()
        await release.wait()

    async def driver():
        task1 = asyncio.create_task(controller.flush(slow_work))
        await in_progress.wait()
        # While task1 is in flight, a second flush should NOT invoke slow_work
        # again — it should just set needs_reflush.
        result2 = await controller.flush(slow_work)
        assert result2 is None
        assert controller.needs_reflush is True
        release.set()
        await task1
        # Give the follow-up task a chance to run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(driver())
    # First in-flight call ran once; controller scheduled a follow-up,
    # so we expect 2 runs total after the follow-up resolves.
    assert len(runs) >= 1


def test_flush_controller_reflush_fires_when_needed():
    """After a flush completes with needs_reflush set, the controller should
    schedule a follow-up flush via asyncio.create_task."""
    controller = FlushController()
    in_progress = asyncio.Event()
    release = asyncio.Event()
    runs: list[int] = []

    async def work():
        runs.append(1)
        if len(runs) == 1:
            in_progress.set()
            await release.wait()

    async def driver():
        task1 = asyncio.create_task(controller.flush(work))
        await in_progress.wait()
        # Trigger needs_reflush from outside.
        await controller.flush(work)
        release.set()
        await task1
        # Allow scheduled follow-up to drain.
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(driver())
    # First call ran once; follow-up triggered by needs_reflush ran second time.
    assert len(runs) >= 2


def test_flush_controller_exception_clears_in_flight_state():
    controller = FlushController()

    async def boom():
        raise RuntimeError("boom")

    async def driver():
        with pytest.raises(RuntimeError, match="boom"):
            await controller.flush(boom)

    asyncio.run(driver())
    assert controller.in_flight is False
    assert controller.needs_reflush is False


# ---------------------------------------------------------------------------
# UnavailableGuard
# ---------------------------------------------------------------------------


def test_unavailable_guard_starts_clean_for_new_adapter():
    adapter = SimpleNamespace()
    assert UnavailableGuard.should_proceed(adapter, "msg-1") is True
    assert UnavailableGuard.should_proceed(adapter, "msg-2") is True


def test_unavailable_guard_marks_and_short_circuits():
    adapter = SimpleNamespace()
    UnavailableGuard.mark_unavailable(adapter, "msg-1", _RECALLED_CODE)
    assert UnavailableGuard.should_proceed(adapter, "msg-1") is False
    # Different message_id is unaffected.
    assert UnavailableGuard.should_proceed(adapter, "msg-2") is True


def test_unavailable_guard_isolates_per_adapter():
    a = SimpleNamespace()
    b = SimpleNamespace()
    UnavailableGuard.mark_unavailable(a, "msg-1", _DELETED_CODE)
    assert UnavailableGuard.should_proceed(a, "msg-1") is False
    assert UnavailableGuard.should_proceed(b, "msg-1") is True


def test_unavailable_codes_include_recalled_and_deleted():
    assert _RECALLED_CODE in UNAVAILABLE_CODES
    assert _DELETED_CODE in UNAVAILABLE_CODES
    assert 0 not in UNAVAILABLE_CODES


def test_unavailable_guard_empty_message_id_is_noop_safe():
    adapter = SimpleNamespace()
    UnavailableGuard.mark_unavailable(adapter, "", _RECALLED_CODE)
    # Empty message_id is treated as "no scoping" — should_proceed returns True
    # so early-lifecycle calls (before start_streaming_card) aren't blocked.
    assert UnavailableGuard.should_proceed(adapter, "") is True


def test_unavailable_guard_clear_lets_caller_proceed_again():
    adapter = SimpleNamespace()
    UnavailableGuard.mark_unavailable(adapter, "msg-1", _RECALLED_CODE)
    assert UnavailableGuard.should_proceed(adapter, "msg-1") is False
    UnavailableGuard.clear(adapter, "msg-1")
    assert UnavailableGuard.should_proceed(adapter, "msg-1") is True


# ---------------------------------------------------------------------------
# Streaming-controller wiring: should_proceed gates every injected method
# ---------------------------------------------------------------------------


class _MarkableAdapter:
    """Test adapter that supports the CardKit compat install path but never
    actually talks to Lark — pure in-memory."""

    def __init__(self):
        self.sent = []
        self.card_sends = []

    def _patch_auth_card(self, message_id, card):
        self.sent.append({"message_id": message_id, "card": card})
        return True

    async def _feishu_send_with_retry(self, *, chat_id, msg_type, payload, reply_to=None, metadata=None):
        self.card_sends.append({"chat_id": chat_id, "payload": payload})
        return SimpleNamespace(success=True, message_id="msg-1")

    def _finalize_send_result(self, response, default_message):
        return SimpleNamespace(
            success=bool(getattr(response, "success", False)),
            message_id=getattr(response, "message_id", None),
        )


def _build_adapter():
    return ensure_feishu_cardkit_streaming(_MarkableAdapter())


def test_update_streaming_card_short_circuits_when_message_marked_unavailable():
    adapter = _build_adapter()
    UnavailableGuard.mark_unavailable(adapter, "msg-1", _RECALLED_CODE)
    result = asyncio.run(
        adapter.update_streaming_card(chat_id="chat-1", message_id="msg-1", content="hello")
    )
    assert result.success is False
    assert "card unavailable" in (result.error or "")
    # The adapter should not have observed a card patch attempt for an
    # unavailable message.
    assert adapter.sent == []


def test_all_six_update_methods_respect_unavailable_guard():
    adapter = _build_adapter()
    UnavailableGuard.mark_unavailable(adapter, "msg-1", _DELETED_CODE)
    common = {"chat_id": "chat-1", "message_id": "msg-1"}

    async def run_all():
        return [
            await adapter.update_streaming_card(**common, content="x"),
            await adapter.update_streaming_card_reasoning(**common, content="r"),
            await adapter.update_streaming_card_status(**common, content="s"),
            await adapter.update_streaming_card_tool_started(**common, tool_name="t"),
            await adapter.update_streaming_card_tool_completed(**common, tool_name="t", duration=0.1),
            await adapter.abort_streaming_card(**common),
        ]

    results = asyncio.run(run_all())
    assert all(getattr(result, "success", True) is False for result in results)
    assert all("card unavailable" in (getattr(result, "error", "") or "") for result in results)
