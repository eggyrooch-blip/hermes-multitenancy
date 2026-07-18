"""Lark CardKit + IM SDK wrappers.

Thin async wrappers over ``cardkit.v1.card.create / settings / update``,
``cardkit.v1.card_element.content``, ``im.v1.message.patch`` and ``.update``.
Falls back to ``SimpleNamespace`` request payloads when the lark-oapi model
modules are absent (test fakes).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Optional

from .card_error import (
    _extract_response_field,
    _raise_on_lark_error,
    _response_succeeded,
)

try:  # pragma: no cover - exercised in live Hermes, faked in unit tests.
    from lark_oapi.api.cardkit.v1.model.create_card_request import CreateCardRequest
    from lark_oapi.api.cardkit.v1.model.create_card_request_body import CreateCardRequestBody
    from lark_oapi.api.cardkit.v1.model.content_card_element_request import ContentCardElementRequest
    from lark_oapi.api.cardkit.v1.model.content_card_element_request_body import (
        ContentCardElementRequestBody,
    )
    from lark_oapi.api.cardkit.v1.model.settings_card_request import SettingsCardRequest
    from lark_oapi.api.cardkit.v1.model.settings_card_request_body import SettingsCardRequestBody
    from lark_oapi.api.cardkit.v1.model.update_card_request import UpdateCardRequest
    from lark_oapi.api.cardkit.v1.model.update_card_request_body import UpdateCardRequestBody
    from lark_oapi.api.im.v1.model.patch_message_request import PatchMessageRequest
    from lark_oapi.api.im.v1.model.patch_message_request_body import PatchMessageRequestBody
except Exception:  # pragma: no cover
    CreateCardRequest = CreateCardRequestBody = None  # type: ignore
    ContentCardElementRequest = ContentCardElementRequestBody = None  # type: ignore
    SettingsCardRequest = SettingsCardRequestBody = None  # type: ignore
    UpdateCardRequest = UpdateCardRequestBody = None  # type: ignore
    PatchMessageRequest = PatchMessageRequestBody = None  # type: ignore

_STREAMING_ELEMENT_ID = "streaming_content"


async def _create_cardkit_card(adapter: Any, card: dict[str, Any]) -> Optional[str]:
    request = _build_create_card_request(card)
    response = await asyncio.to_thread(adapter._client.cardkit.v1.card.create, request)
    _raise_on_lark_error(response, "card.create")
    return _extract_response_field(response, "card_id")


async def _stream_cardkit_content(adapter: Any, card_id: str, content: str, sequence: int) -> None:
    await _stream_cardkit_element(adapter, card_id, _STREAMING_ELEMENT_ID, content, sequence)


async def _stream_cardkit_element(adapter: Any, card_id: str, element_id: str, content: str, sequence: int) -> None:
    request = _build_content_card_element_request(card_id, element_id, content, sequence)
    response = await asyncio.to_thread(adapter._client.cardkit.v1.card_element.content, request)
    _raise_on_lark_error(response, "cardElement.content")


async def _set_card_streaming_mode(adapter: Any, card_id: str, streaming_mode: bool, sequence: int) -> None:
    request = _build_settings_card_request(card_id, streaming_mode, sequence)
    response = await asyncio.to_thread(adapter._client.cardkit.v1.card.settings, request)
    _raise_on_lark_error(response, "card.settings")


async def _update_cardkit_card(adapter: Any, card_id: str, card: dict[str, Any], sequence: int) -> None:
    request = _build_update_card_request(card_id, card, sequence)
    response = await asyncio.to_thread(adapter._client.cardkit.v1.card.update, request)
    _raise_on_lark_error(response, "card.update")


async def _patch_interactive_message(adapter: Any, message_id: str, card: dict[str, Any]) -> bool:
    request = _build_patch_message_request(message_id, card)
    response = await asyncio.to_thread(adapter._client.im.v1.message.patch, request)
    return _response_succeeded(adapter, response, "card patch failed")


async def _update_interactive_message(adapter: Any, message_id: str, card: dict[str, Any]) -> bool:
    body = adapter._build_update_message_body(
        msg_type="interactive",
        content=json.dumps(card, ensure_ascii=False),
    )
    request = adapter._build_update_message_request(message_id=message_id, request_body=body)
    response = await asyncio.to_thread(adapter._client.im.v1.message.update, request)
    return _response_succeeded(adapter, response, "card update failed")


def _build_create_card_request(card: dict[str, Any]) -> Any:
    if CreateCardRequest is not None and CreateCardRequestBody is not None:
        body = (
            CreateCardRequestBody.builder()
            .type("card_json")
            .data(json.dumps(card, ensure_ascii=False))
            .build()
        )
        return CreateCardRequest.builder().request_body(body).build()
    return SimpleNamespace(request_body=SimpleNamespace(type="card_json", data=json.dumps(card, ensure_ascii=False)))


def _build_content_card_element_request(card_id: str, element_id: str, content: str, sequence: int) -> Any:
    if ContentCardElementRequest is not None and ContentCardElementRequestBody is not None:
        body = ContentCardElementRequestBody.builder().content(content).sequence(sequence).build()
        return (
            ContentCardElementRequest.builder()
            .card_id(card_id)
            .element_id(element_id)
            .request_body(body)
            .build()
        )
    return SimpleNamespace(
        card_id=card_id,
        element_id=element_id,
        request_body=SimpleNamespace(content=content, sequence=sequence),
    )


def _build_settings_card_request(card_id: str, streaming_mode: bool, sequence: int) -> Any:
    settings = json.dumps(
        {"config": {"streaming_mode": streaming_mode}},
        separators=(",", ":"),
    )
    if SettingsCardRequest is not None and SettingsCardRequestBody is not None:
        body = SettingsCardRequestBody.builder().settings(settings).sequence(sequence).build()
        return SettingsCardRequest.builder().card_id(card_id).request_body(body).build()
    return SimpleNamespace(card_id=card_id, request_body=SimpleNamespace(settings=settings, sequence=sequence))


def _build_update_card_request(card_id: str, card: dict[str, Any], sequence: int) -> Any:
    card_payload = {"type": "card_json", "data": json.dumps(card, ensure_ascii=False)}
    if UpdateCardRequest is not None and UpdateCardRequestBody is not None:
        body = UpdateCardRequestBody.builder().card(card_payload).sequence(sequence).build()
        return UpdateCardRequest.builder().card_id(card_id).request_body(body).build()
    return SimpleNamespace(card_id=card_id, request_body=SimpleNamespace(card=card_payload, sequence=sequence))


def _build_patch_message_request(message_id: str, card: dict[str, Any]) -> Any:
    if PatchMessageRequest is not None and PatchMessageRequestBody is not None:
        body = PatchMessageRequestBody.builder().content(json.dumps(card, ensure_ascii=False)).build()
        return PatchMessageRequest.builder().message_id(message_id).request_body(body).build()
    return SimpleNamespace(
        message_id=message_id,
        request_body=SimpleNamespace(content=json.dumps(card, ensure_ascii=False)),
    )
