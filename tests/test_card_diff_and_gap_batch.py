from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_multitenancy.card.flush_controller import FlushController
from hermes_multitenancy.card.state import _new_state
from hermes_multitenancy.card import streaming_controller as streaming_mod


@pytest.fixture(autouse=True)
def _disable_card_content_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_CARD_CONTENT_THROTTLE_S", "0")


def _stream_state(content: str) -> dict[str, Any]:
    state = _new_state()
    state["card_id"] = "ck-1"
    state["content"] = content
    return state


def test_flush_state_skips_duplicate_cardkit_content_push(monkeypatch: pytest.MonkeyPatch) -> None:
    pushes: list[dict[str, Any]] = []

    async def fake_stream_cardkit_content(
        adapter: Any, card_id: str, content: str, sequence: int
    ) -> None:
        pushes.append(
            {
                "adapter": adapter,
                "card_id": card_id,
                "content": content,
                "sequence": sequence,
            }
        )

    monkeypatch.setattr(streaming_mod, "_stream_cardkit_content", fake_stream_cardkit_content)

    async def driver() -> None:
        adapter = SimpleNamespace()
        state = _stream_state("same text")
        first = await streaming_mod._flush_state(adapter, "msg-1", state)
        second = await streaming_mod._flush_state(adapter, "msg-1", state)
        assert first.success is True
        assert second.success is True

    asyncio.run(driver())

    assert len(pushes) == 1
    assert pushes[0]["content"] == "same text"


def test_flush_state_pushes_again_when_rendered_text_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    pushes: list[dict[str, Any]] = []

    async def fake_stream_cardkit_content(
        adapter: Any, card_id: str, content: str, sequence: int
    ) -> None:
        pushes.append({"card_id": card_id, "content": content, "sequence": sequence})

    monkeypatch.setattr(streaming_mod, "_stream_cardkit_content", fake_stream_cardkit_content)

    async def driver() -> None:
        adapter = SimpleNamespace()
        state = _stream_state("first text")
        await streaming_mod._flush_state(adapter, "msg-1", state)
        state["content"] = "second text"
        await streaming_mod._flush_state(adapter, "msg-1", state)

    asyncio.run(driver())

    assert [push["content"] for push in pushes] == ["first text", "second text"]


def test_flush_state_skip_does_not_consume_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    pushes: list[dict[str, Any]] = []

    async def fake_stream_cardkit_content(
        adapter: Any, card_id: str, content: str, sequence: int
    ) -> None:
        pushes.append({"content": content, "sequence": sequence})

    monkeypatch.setattr(streaming_mod, "_stream_cardkit_content", fake_stream_cardkit_content)

    async def driver() -> None:
        adapter = SimpleNamespace()
        state = _stream_state("alpha")
        await streaming_mod._flush_state(adapter, "msg-1", state)
        await streaming_mod._flush_state(adapter, "msg-1", state)
        state["content"] = "beta"
        await streaming_mod._flush_state(adapter, "msg-1", state)

    asyncio.run(driver())

    assert [push["content"] for push in pushes] == ["alpha", "beta"]
    assert pushes[1]["sequence"] == pushes[0]["sequence"] + 1


def test_flush_controller_batches_after_long_gap() -> None:
    async def driver() -> None:
        controller = FlushController()
        rendered = {"text": "seed"}
        calls: list[str] = []

        async def flush() -> None:
            calls.append(rendered["text"])

        await controller.throttled_update(
            0.01,
            flush,
            long_gap_s=0.05,
            batch_after_gap_s=0.02,
        )
        assert calls == ["seed"]

        await asyncio.sleep(0.06)

        rendered["text"] = "H"
        await controller.throttled_update(
            0.01,
            flush,
            long_gap_s=0.05,
            batch_after_gap_s=0.02,
        )
        assert calls == ["seed"]

        rendered["text"] = "Hello after batching"
        await controller.throttled_update(
            0.01,
            flush,
            long_gap_s=0.05,
            batch_after_gap_s=0.02,
        )
        assert calls == ["seed"]

        await asyncio.sleep(0.03)

        assert calls == ["seed", "Hello after batching"]
        controller.cancel()

    asyncio.run(driver())
