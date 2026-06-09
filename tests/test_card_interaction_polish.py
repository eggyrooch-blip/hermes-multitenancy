from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from hermes_multitenancy.card import ensure_feishu_cardkit_streaming
from hermes_multitenancy.card import tool_use_config, tool_use_display


def _tool(index: int) -> dict[str, object]:
    return {
        "name": f"tool_{index}",
        "status": "done",
        "duration": 0.1 + index / 1000,
    }


def _card_text(card_or_elements):
    elements = (
        card_or_elements.get("elements", card_or_elements)
        if isinstance(card_or_elements, dict)
        else card_or_elements
    )
    parts = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        content = element.get("content")
        if content:
            parts.append(str(content))
        nested = element.get("elements")
        if nested:
            parts.append(_card_text(nested))
    return "\n".join(part for part in parts if part)


def test_tool_row_cap_is_configurable_and_tail_preserving(monkeypatch):
    tools = [_tool(index) for index in range(12)]

    assert tool_use_config._TOOL_ROW_MAX > 5
    assert tool_use_display._TOOL_ROW_MAX == tool_use_config._TOOL_ROW_MAX

    rendered_default = tool_use_display._render_tool_calls_section(tools)
    default_lines = rendered_default.splitlines()
    assert len(default_lines) == 12
    assert "`tool_0`" in rendered_default
    assert "`tool_11`" in rendered_default

    monkeypatch.setattr(tool_use_display, "_TOOL_ROW_MAX", 3)
    rendered_last_three = tool_use_display._render_tool_calls_section(tools)
    assert rendered_last_three.splitlines() == [
        "- `tool_9` (109 ms)",
        "- `tool_10` (110 ms)",
        "- `tool_11` (111 ms)",
    ]

    monkeypatch.setattr(tool_use_display, "_TOOL_ROW_MAX", 0)
    rendered_unlimited = tool_use_display._render_tool_calls_section(tools)
    assert len(rendered_unlimited.splitlines()) == 12
    assert "`tool_0`" in rendered_unlimited
    assert "`tool_11`" in rendered_unlimited


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Authorization: Bearer sk-secret123", "Authorization: Bearer [redacted]"),
        ("token=abcdef123", "token=[redacted]"),
        ("--api-key SECRETVAL", "--api-key [redacted]"),
        ("--api-key=SECRETVAL", "--api-key=[redacted]"),
        ('password="hunter2"', "password=[redacted]"),
        ("-H 'X-Api-Key: leak'", "-H 'X-Api-Key: [redacted]'"),
        ("--header X-Api-Key: leak", "--header X-Api-Key: [redacted]"),
    ],
)
def test_redact_inline_secrets_covers_sensitive_cases(raw: str, expected: str):
    redacted = tool_use_display.redact_inline_secrets(raw)
    assert redacted == expected
    assert "[redacted]" in redacted


def test_redact_inline_secrets_preserves_nonsensitive_content():
    assert tool_use_display.redact_inline_secrets("--limit 3") == "--limit 3"
    assert tool_use_display.redact_inline_secrets("query=北京") == "query=北京"
    assert (
        tool_use_display.redact_inline_secrets("-H 'Authorization: opaque'")
        == "-H 'Authorization: opaque'"
    )


def test_rendered_tool_rows_apply_redaction_without_printing_arguments():
    rendered = tool_use_display._render_tool_calls_section(
        [
            {
                "name": "token=SECRETVAL",
                "status": "done",
                "duration": 0.2,
                "args": {"token": "should-not-appear"},
            }
        ]
    )

    assert "`token=[redacted]` (200 ms)" in rendered
    assert "SECRETVAL" not in rendered
    assert "should-not-appear" not in rendered


class _DelayedCardKitAdapter:
    """In-memory CardKit-capable adapter whose first ``card_element.content``
    call parks on an :class:`asyncio.Event` (cooperatively, since the real
    wrapper dispatches the SDK call via ``asyncio.to_thread``).

    The park lets a *second* ``update_streaming_card`` arrive while the first
    flush is still in flight, which is exactly the long-interval-batch race
    the ``FlushController`` reflush is designed to survive.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._patch_auth_card = None
        self._loop = loop
        self.card_sends = []
        self.created_cards = []
        self.content_updates = []
        self.settings_updates = []
        self.card_updates = []
        # Set from the worker thread when the first content() lands; awaited
        # from the loop thread so the loop is never blocked.
        self.first_content_entered = asyncio.Event()
        # Released from the loop thread to let the parked first content() return.
        self.release_first_content = asyncio.Event()

        class CardApi:
            def __init__(api_self, outer):
                api_self.outer = outer

            def create(api_self, request):
                api_self.outer.created_cards.append(request)
                return SimpleNamespace(code=0, msg="success", data={"card_id": "ck-1"})

            def settings(api_self, request):
                api_self.outer.settings_updates.append(request)
                return SimpleNamespace(code=0, msg="success")

            def update(api_self, request):
                api_self.outer.card_updates.append(request)
                return SimpleNamespace(code=0, msg="success")

        class CardElementApi:
            def __init__(api_self, outer):
                api_self.outer = outer

            def content(api_self, request):
                outer = api_self.outer
                outer.content_updates.append(request)
                if len(outer.content_updates) == 1:
                    # Runs on the to_thread worker; signal + park via the loop.
                    outer._loop.call_soon_threadsafe(outer.first_content_entered.set)
                    future = asyncio.run_coroutine_threadsafe(
                        outer.release_first_content.wait(), outer._loop
                    )
                    future.result(timeout=2)
                return SimpleNamespace(code=0, msg="success")

        self._client = SimpleNamespace(
            cardkit=SimpleNamespace(
                v1=SimpleNamespace(
                    card=CardApi(self),
                    card_element=CardElementApi(self),
                )
            )
        )

    async def _feishu_send_with_retry(
        self, *, chat_id, msg_type, payload, reply_to, metadata
    ):
        self.card_sends.append(
            {
                "chat_id": chat_id,
                "msg_type": msg_type,
                "payload": payload,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SimpleNamespace(code=0, data={"message_id": "compat-card-1"})

    def _finalize_send_result(self, response, default_message):
        message_id = getattr(response, "data", {}).get("message_id")
        return SimpleNamespace(
            success=bool(message_id),
            message_id=message_id,
            error=None if message_id else default_message,
            raw_response=response,
        )


async def test_flush_controller_reflush_preserves_latest_streamed_content():
    from hermes_multitenancy.card.streaming_controller import _FLUSH_CONTROLLERS_ATTR

    loop = asyncio.get_running_loop()
    adapter = ensure_feishu_cardkit_streaming(_DelayedCardKitAdapter(loop))
    started = await adapter.start_streaming_card(chat_id="chat-1")

    # First flush parks inside the first content() call (in-flight).
    first_flush = asyncio.create_task(
        adapter.update_streaming_card(
            chat_id="chat-1",
            message_id=started.message_id,
            content="first token",
        )
    )
    await asyncio.wait_for(adapter.first_content_entered.wait(), timeout=2)

    # Second event arrives WHILE the first flush is in flight. The controller
    # must coalesce this (needs_reflush) rather than run a second flush now.
    second_flush = asyncio.create_task(
        adapter.update_streaming_card(
            chat_id="chat-1",
            message_id=started.message_id,
            content="last token",
        )
    )
    # Give the coalesced call a turn to register needs_reflush.
    await asyncio.sleep(0)

    # Release the parked first flush; the controller schedules the reflush.
    adapter.release_first_content.set()
    await first_flush
    await second_flush
    # Drain the scheduled reflush follow-up task.
    for _ in range(20):
        await asyncio.sleep(0)

    # Reflush guarantees the LAST token reached the card, not just the first.
    assert len(adapter.content_updates) >= 2
    streamed = [
        update.request_body.content
        for update in adapter.content_updates
        if "last token" in str(getattr(update.request_body, "content", ""))
    ]
    assert streamed, "reflush must re-stream the latest batched token"

    await adapter.update_streaming_card(
        chat_id="chat-1",
        message_id=started.message_id,
        content="last token",
        finalize=True,
    )

    final_card = json.loads(adapter.card_updates[-1].request_body.card["data"])
    assert "last token" in _card_text(final_card["body"]["elements"])
    # Final flush must clean up the per-message controller (no unbounded growth).
    assert started.message_id not in getattr(adapter, _FLUSH_CONTROLLERS_ATTR)


async def test_flush_controller_wiring_fails_open_and_logs_warning(caplog):
    from hermes_multitenancy.card.state import _state_for
    from hermes_multitenancy.card.streaming_controller import (
        _FLUSH_CONTROLLERS_ATTR,
        _flush_state,
    )

    loop = asyncio.get_running_loop()
    adapter = ensure_feishu_cardkit_streaming(_DelayedCardKitAdapter(loop))
    started = await adapter.start_streaming_card(chat_id="chat-1")
    state = _state_for(adapter, started.message_id)
    controllers = getattr(adapter, _FLUSH_CONTROLLERS_ATTR)

    class _BoomController:
        async def flush(self, flush_callable):
            raise RuntimeError("flush boom")

    controllers[started.message_id] = _BoomController()

    caplog.set_level("WARNING", logger="hermes_multitenancy.feishu_cardkit_compat")
    # No park is triggered here: state already has a card_id so the direct
    # fallback streams content immediately. C2: wiring failure must fail open.
    adapter.release_first_content.set()
    result = await _flush_state(adapter, started.message_id, state)

    assert result.success is True
    assert adapter.content_updates
    assert "flush boom" in caplog.text
