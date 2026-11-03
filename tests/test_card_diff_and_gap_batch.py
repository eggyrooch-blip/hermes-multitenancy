from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_multitenancy.card.flush_controller import FlushController
from hermes_multitenancy.card.card_error import StreamingClosedError
from hermes_multitenancy.card.state import _new_state, _states
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


@pytest.mark.parametrize("code", [200850, 300309])
def test_flush_state_reopens_closed_stream_once(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    operations: list[tuple[str, int]] = []
    attempts = 0

    async def fake_stream(
        adapter: Any, card_id: str, content: str, sequence: int
    ) -> None:
        nonlocal attempts
        attempts += 1
        operations.append(("stream", sequence))
        if attempts == 1:
            raise StreamingClosedError("cardElement.content", code, "closed")

    async def fake_settings(
        adapter: Any, card_id: str, streaming_mode: bool, sequence: int
    ) -> None:
        assert streaming_mode is True
        operations.append(("settings", sequence))

    monkeypatch.setattr(streaming_mod, "_stream_cardkit_content", fake_stream)
    monkeypatch.setattr(streaming_mod, "_set_card_streaming_mode", fake_settings)

    async def driver() -> None:
        state = _stream_state("continue")
        result = await streaming_mod._flush_state(SimpleNamespace(), "msg-1", state)
        assert result.success is True
        assert state["sequence"] == 3

    asyncio.run(driver())
    assert operations == [("stream", 1), ("settings", 2), ("stream", 3)]


@pytest.mark.parametrize(
    ("failure_stage", "expected_operations"),
    [
        ("settings", [("stream", 1), ("settings", 2)]),
        ("retry", [("stream", 1), ("settings", 2), ("stream", 3)]),
    ],
)
def test_closed_stream_recovery_failure_does_not_loop(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    expected_operations: list[tuple[str, int]],
) -> None:
    operations: list[tuple[str, int]] = []
    attempts = 0

    async def fake_stream(
        adapter: Any, card_id: str, content: str, sequence: int
    ) -> None:
        nonlocal attempts
        attempts += 1
        operations.append(("stream", sequence))
        if attempts == 1:
            raise StreamingClosedError("cardElement.content", 300309, "closed")
        if failure_stage == "retry":
            raise RuntimeError("retry failed")

    async def fake_settings(
        adapter: Any, card_id: str, streaming_mode: bool, sequence: int
    ) -> None:
        operations.append(("settings", sequence))
        if failure_stage == "settings":
            raise RuntimeError("settings failed")

    monkeypatch.setattr(streaming_mod, "_stream_cardkit_content", fake_stream)
    monkeypatch.setattr(streaming_mod, "_set_card_streaming_mode", fake_settings)

    async def driver() -> None:
        state = _stream_state("continue")
        result = await streaming_mod._flush_state(SimpleNamespace(), "msg-1", state)
        assert result.success is True
        assert state["card_id"] is None

    asyncio.run(driver())
    assert operations == expected_operations


def test_closed_stream_recovery_serializes_concurrent_tool_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations: list[tuple[str, int]] = []
    settings_started = asyncio.Event()
    release_settings = asyncio.Event()
    stream_attempts = 0

    async def fake_stream(
        adapter: Any, card_id: str, content: str, sequence: int
    ) -> None:
        nonlocal stream_attempts
        stream_attempts += 1
        operations.append(("stream", sequence))
        if stream_attempts == 1:
            raise StreamingClosedError("cardElement.content", 300309, "closed")

    async def fake_settings(
        adapter: Any, card_id: str, streaming_mode: bool, sequence: int
    ) -> None:
        operations.append(("settings", sequence))
        settings_started.set()
        await release_settings.wait()

    async def fake_element(
        adapter: Any,
        card_id: str,
        element_id: str,
        content: str,
        sequence: int,
    ) -> None:
        operations.append(("element", sequence))

    monkeypatch.setattr(streaming_mod, "_stream_cardkit_content", fake_stream)
    monkeypatch.setattr(streaming_mod, "_set_card_streaming_mode", fake_settings)
    monkeypatch.setattr(streaming_mod, "_stream_cardkit_element", fake_element)

    async def driver() -> None:
        adapter = SimpleNamespace()
        state = _stream_state("continue")
        _states(adapter)["msg-1"] = state
        status_task = asyncio.create_task(
            streaming_mod._update_streaming_card_status(
                adapter,
                chat_id="chat-1",
                message_id="msg-1",
                content="working",
            )
        )
        await settings_started.wait()
        tool_task = asyncio.create_task(
            streaming_mod._update_streaming_card_tool_started(
                adapter,
                chat_id="chat-1",
                message_id="msg-1",
                tool_name="execute_code",
            )
        )
        await asyncio.sleep(0)
        assert operations == [("stream", 1), ("settings", 2)]
        release_settings.set()
        status_result, tool_result = await asyncio.gather(status_task, tool_task)
        assert status_result.success is True
        assert tool_result.success is True

    asyncio.run(driver())
    assert operations == [
        ("stream", 1),
        ("settings", 2),
        ("stream", 3),
        ("element", 4),
    ]


def test_error_terminal_blocks_queued_status_frame(
) -> None:
    async def driver() -> None:
        patches: list[dict[str, Any]] = []

        def patch(message_id: str, card: dict[str, Any]) -> bool:
            patches.append({"message_id": message_id, "card": card})
            return True

        adapter = SimpleNamespace(_patch_auth_card=patch)
        state = _stream_state("partial")
        state["card_id"] = None
        _states(adapter)["msg-1"] = state
        fail_result = await streaming_mod._fail_streaming_card(
            adapter,
            chat_id="chat-1",
            message_id="msg-1",
            content="partial\n\nfailed",
        )
        assert "msg-1" not in _states(adapter)
        status_result = await streaming_mod._update_streaming_card_status(
            adapter,
            chat_id="chat-1",
            message_id="msg-1",
            content="late",
        )
        tool_result = await streaming_mod._update_streaming_card_tool_started(
            adapter,
            chat_id="chat-1",
            message_id="msg-1",
            tool_name="late-tool",
        )
        assert fail_result.success is True
        assert status_result.success is True
        assert tool_result.success is True
        assert state["status"] == ""
        assert len(patches) == 1

    asyncio.run(driver())


def test_flush_controller_batches_after_long_gap() -> None:
    async def driver() -> None:
        # Long-gap knobs live on the instance (back-compat: throttled_update's
        # public signature stays (throttle_s, flush_callable)).
        controller = FlushController(long_gap_s=0.05, batch_after_gap_s=0.02)
        rendered = {"text": "seed"}
        calls: list[str] = []

        async def flush() -> None:
            calls.append(rendered["text"])

        await controller.throttled_update(0.01, flush)
        assert calls == ["seed"]

        await asyncio.sleep(0.06)

        rendered["text"] = "H"
        await controller.throttled_update(0.01, flush)
        assert calls == ["seed"]

        rendered["text"] = "Hello after batching"
        await controller.throttled_update(0.01, flush)
        assert calls == ["seed"]

        await asyncio.sleep(0.03)

        assert calls == ["seed", "Hello after batching"]
        controller.cancel()

    asyncio.run(driver())
