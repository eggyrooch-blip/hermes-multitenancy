from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_multitenancy.card import ensure_feishu_cardkit_streaming


@pytest.fixture(autouse=True)
def _disable_card_content_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_CARD_CONTENT_THROTTLE_S", "0")


def _ok(**kwargs: Any) -> SimpleNamespace:
    payload = {"code": 0, "msg": "success"}
    payload.update(kwargs)
    return SimpleNamespace(**payload)


def _error(code: int, msg: str, *, sub_code: int | None = None) -> SimpleNamespace:
    payload: dict[str, Any] = {"code": code, "msg": msg}
    if sub_code is not None:
        payload["sub_code"] = sub_code
    return SimpleNamespace(**payload)


def _card_text(card_or_elements: Any) -> str:
    elements = card_or_elements.get("elements", card_or_elements) if isinstance(card_or_elements, dict) else card_or_elements
    parts: list[str] = []
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


def _patch_card(request: Any) -> dict[str, Any]:
    return json.loads(request.request_body.content)


def _update_card(request: Any) -> dict[str, Any]:
    return json.loads(request.request_body.card["data"])


class _ProgrammableCardKitAdapter:
    def __init__(self, planned: dict[str, list[SimpleNamespace]] | None = None) -> None:
        self._planned = {name: list(values) for name, values in (planned or {}).items()}
        self.created_cards: list[Any] = []
        self.content_updates: list[Any] = []
        self.settings_updates: list[Any] = []
        self.card_updates: list[Any] = []
        self.patch_requests: list[Any] = []
        self.card_sends: list[dict[str, Any]] = []
        self.text_sends: list[dict[str, Any]] = []

        class CardApi:
            def __init__(api_self, outer: "_ProgrammableCardKitAdapter") -> None:
                api_self.outer = outer

            def create(api_self, request: Any) -> SimpleNamespace:
                api_self.outer.created_cards.append(request)
                return api_self.outer._take(
                    "card.create",
                    _ok(data={"card_id": "ck-1"}),
                )

            def settings(api_self, request: Any) -> SimpleNamespace:
                api_self.outer.settings_updates.append(request)
                return api_self.outer._take("card.settings", _ok())

            def update(api_self, request: Any) -> SimpleNamespace:
                api_self.outer.card_updates.append(request)
                return api_self.outer._take("card.update", _ok())

        class CardElementApi:
            def __init__(api_self, outer: "_ProgrammableCardKitAdapter") -> None:
                api_self.outer = outer

            def content(api_self, request: Any) -> SimpleNamespace:
                api_self.outer.content_updates.append(request)
                return api_self.outer._take("cardElement.content", _ok())

        class MessageApi:
            def __init__(api_self, outer: "_ProgrammableCardKitAdapter") -> None:
                api_self.outer = outer

            def patch(api_self, request: Any) -> SimpleNamespace:
                api_self.outer.patch_requests.append(request)
                return api_self.outer._take("message.patch", _ok())

        self._client = SimpleNamespace(
            cardkit=SimpleNamespace(
                v1=SimpleNamespace(
                    card=CardApi(self),
                    card_element=CardElementApi(self),
                )
            ),
            im=SimpleNamespace(v1=SimpleNamespace(message=MessageApi(self))),
        )

    def _take(self, name: str, default: SimpleNamespace) -> SimpleNamespace:
        planned = self._planned.get(name) or []
        if planned:
            return planned.pop(0)
        return default

    async def _feishu_send_with_retry(
        self,
        *,
        chat_id: str,
        msg_type: str,
        payload: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        self.card_sends.append(
            {
                "chat_id": chat_id,
                "msg_type": msg_type,
                "payload": payload,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return self._take("message.send", _ok(data={"message_id": "msg-1"}))

    def _finalize_send_result(self, response: Any, default_message: str) -> SimpleNamespace:
        code = getattr(response, "code", 0)
        data = getattr(response, "data", None) or {}
        message_id = data.get("message_id") if isinstance(data, dict) else getattr(data, "message_id", None)
        success = code == 0 or bool(getattr(response, "success", False))
        return SimpleNamespace(
            success=success,
            message_id=message_id,
            error=None if success else default_message,
            raw_response=response,
        )

    def format_message(self, content: Any) -> str:
        return str(content or "")

    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        self.text_sends.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SimpleNamespace(success=True, message_id="text-1")


def _state(adapter: Any, message_id: str) -> dict[str, Any]:
    return adapter._hermes_mt_streaming_card_state[str(message_id)]


def test_rate_limit_midstream_skips_frames_but_keeps_card_alive():
    asyncio.run(_run_rate_limit_midstream_skips_frames_but_keeps_card_alive())


async def _run_rate_limit_midstream_skips_frames_but_keeps_card_alive() -> None:
    adapter = ensure_feishu_cardkit_streaming(
        _ProgrammableCardKitAdapter(
            {
                "cardElement.content": [
                    _error(230020, "rate 1"),
                    _error(230020, "rate 2"),
                    _ok(),
                ]
            }
        )
    )

    started = await adapter.start_streaming_card(chat_id="chat-1", reply_to="src-1")
    state = _state(adapter, started.message_id)
    assert state["sequence"] == 1

    first = await adapter.update_streaming_card(chat_id="chat-1", message_id=started.message_id, content="hello 1")
    assert first.success is True
    assert state["card_id"] == "ck-1"
    assert state["sequence"] == 2

    second = await adapter.update_streaming_card(chat_id="chat-1", message_id=started.message_id, content="hello 2")
    assert second.success is True
    assert state["card_id"] == "ck-1"
    assert state["sequence"] == 3

    final = await adapter.update_streaming_card(
        chat_id="chat-1",
        message_id=started.message_id,
        content="final full answer",
        finalize=True,
    )

    assert final.success is True
    assert len(adapter.content_updates) == 3
    assert adapter.card_updates[-1].card_id == "ck-1"
    assert "final full answer" in _card_text(_update_card(adapter.card_updates[-1])["body"]["elements"])


def test_table_limit_midstream_drops_streaming_id_but_finalizes_via_original_card():
    asyncio.run(_run_table_limit_midstream_drops_streaming_id_but_finalizes_via_original_card())


async def _run_table_limit_midstream_drops_streaming_id_but_finalizes_via_original_card() -> None:
    adapter = ensure_feishu_cardkit_streaming(
        _ProgrammableCardKitAdapter(
            {
                "cardElement.content": [
                    _error(230099, "table limit", sub_code=11310),
                ]
            }
        )
    )

    started = await adapter.start_streaming_card(chat_id="chat-1")
    state = _state(adapter, started.message_id)
    table_text = "| a | b |\n| --- | --- |\n| 1 | 2 |"

    result = await adapter.update_streaming_card(
        chat_id="chat-1",
        message_id=started.message_id,
        content=table_text,
    )
    assert result.success is True
    assert state["card_id"] is None
    assert state["original_card_id"] == "ck-1"

    final = await adapter.update_streaming_card(
        chat_id="chat-1",
        message_id=started.message_id,
        content=f"{table_text}\n\nfull answer",
        finalize=True,
    )

    assert final.success is True
    assert adapter.card_updates[-1].card_id == "ck-1"
    assert "full answer" in _card_text(_update_card(adapter.card_updates[-1])["body"]["elements"])


def test_final_card_update_error_falls_back_to_im_patch():
    asyncio.run(_run_final_card_update_error_falls_back_to_im_patch())


async def _run_final_card_update_error_falls_back_to_im_patch() -> None:
    adapter = ensure_feishu_cardkit_streaming(
        _ProgrammableCardKitAdapter({"card.update": [_error(500, "boom")]})
    )

    started = await adapter.start_streaming_card(chat_id="chat-1")
    final = await adapter.update_streaming_card(
        chat_id="chat-1",
        message_id=started.message_id,
        content="full answer via patch",
        finalize=True,
    )

    assert final.success is True
    assert adapter.patch_requests
    patched = _patch_card(adapter.patch_requests[-1])
    assert "full answer via patch" in _card_text(patched["elements"])
    assert adapter.text_sends == []
    assert started.message_id not in adapter._hermes_mt_streaming_card_state


def test_plain_text_last_resort_fires_when_final_card_and_patch_both_fail():
    asyncio.run(_run_plain_text_last_resort_fires_when_final_card_and_patch_both_fail())


async def _run_plain_text_last_resort_fires_when_final_card_and_patch_both_fail() -> None:
    adapter = ensure_feishu_cardkit_streaming(
        _ProgrammableCardKitAdapter(
            {
                "card.update": [_error(500, "boom")],
                "message.patch": [_error(500, "patch boom")],
            }
        )
    )

    started = await adapter.start_streaming_card(chat_id="chat-1", reply_to="src-1")
    state = _state(adapter, started.message_id)

    final = await adapter.update_streaming_card(
        chat_id="chat-1",
        message_id=started.message_id,
        content="full answer via plaintext",
        finalize=True,
    )

    assert final.success is True
    assert adapter.patch_requests
    assert adapter.text_sends == [
        {
            "chat_id": "chat-1",
            "content": "full answer via plaintext",
            "reply_to": "src-1",
            "metadata": None,
        }
    ]
    assert adapter.settings_updates[-1].request_body.settings == json.dumps({"streaming_mode": False})
    assert state["phase"] != "completed"
    assert started.message_id not in adapter._hermes_mt_streaming_card_state


def test_stream_pushes_are_capped_but_final_card_keeps_full_content():
    asyncio.run(_run_stream_pushes_are_capped_but_final_card_keeps_full_content())


async def _run_stream_pushes_are_capped_but_final_card_keeps_full_content() -> None:
    adapter = ensure_feishu_cardkit_streaming(_ProgrammableCardKitAdapter())
    started = await adapter.start_streaming_card(chat_id="chat-1")
    long_text = "A" * 50_000

    streamed = await adapter.update_streaming_card(
        chat_id="chat-1",
        message_id=started.message_id,
        content=long_text,
    )
    assert streamed.success is True
    assert len(adapter.content_updates[-1].request_body.content) <= 8000

    final = await adapter.update_streaming_card(
        chat_id="chat-1",
        message_id=started.message_id,
        content=long_text,
        finalize=True,
    )
    assert final.success is True

    final_card = _update_card(adapter.card_updates[-1])
    answer_lengths = [
        len(str(element.get("content", "")))
        for element in final_card["body"]["elements"]
        if element.get("tag") == "markdown"
    ]
    assert max(answer_lengths) >= len(long_text)
