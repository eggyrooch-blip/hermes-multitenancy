"""Feishu CardKit streaming compatibility for clean Hermes agents.

The implementation mirrors openclaw-lark's safe card lifecycle:
CardKit card.create -> send IM interactive card_id -> cardElement.content
streaming -> card.settings(streaming_mode=false) -> card.update final card.
If CardKit is unavailable, it falls back to ordinary interactive-card PATCH
without taking over UAT, lark-cli, routing, or profile execution.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from types import MethodType, SimpleNamespace
from typing import Any, Optional

logger = logging.getLogger(__name__)

_INSTALLED_ATTR = "_hermes_mt_cardkit_compat_installed"
_STATE_ATTR = "_hermes_mt_streaming_card_state"
_STREAMING_ELEMENT_ID = "streaming_content"
_REASONING_TAG_RE = re.compile(
    r"<(think|thinking|thought|antthinking)\b[^>]*>([\s\S]*?)</\1>",
    re.IGNORECASE,
)
_UNCLOSED_REASONING_TAG_RE = re.compile(
    r"<(think|thinking|thought|antthinking)\b[^>]*>([\s\S]*)$",
    re.IGNORECASE,
)
_TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>\s*([\s\S]*?)\s*</tool_call>", re.IGNORECASE)
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")

try:  # pragma: no cover - import shape depends on the installed Hermes agent.
    from gateway.platforms.base import SendResult  # type: ignore
except Exception:  # pragma: no cover
    SendResult = None  # type: ignore

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


def ensure_feishu_cardkit_streaming(adapter: Any) -> Any:
    """Install streaming-card methods on a Feishu adapter when they are absent."""
    if adapter is None:
        return None
    if _has_native_streaming_surface(adapter) or getattr(adapter, _INSTALLED_ATTR, False):
        return adapter
    if not _can_install_compat(adapter):
        return adapter

    setattr(adapter, _STATE_ATTR, {})
    adapter.supports_streaming_card = MethodType(_supports_streaming_card, adapter)
    adapter.start_streaming_card = MethodType(_start_streaming_card, adapter)
    adapter.update_streaming_card = MethodType(_update_streaming_card, adapter)
    adapter.abort_streaming_card = MethodType(_abort_streaming_card, adapter)
    adapter.update_streaming_card_reasoning = MethodType(_update_streaming_card_reasoning, adapter)
    adapter.update_streaming_card_status = MethodType(_update_streaming_card_status, adapter)
    adapter.update_streaming_card_tool_started = MethodType(_update_streaming_card_tool_started, adapter)
    adapter.update_streaming_card_tool_completed = MethodType(_update_streaming_card_tool_completed, adapter)
    setattr(adapter, _INSTALLED_ATTR, True)
    logger.info("multitenancy: installed Feishu CardKit compat streaming surface")
    return adapter


def _has_native_streaming_surface(adapter: Any) -> bool:
    starter = getattr(adapter, "start_streaming_card", None)
    updater = getattr(adapter, "update_streaming_card", None)
    if not callable(starter) or not callable(updater):
        return False
    supports = getattr(adapter, "supports_streaming_card", None)
    if callable(supports):
        try:
            return bool(supports())
        except Exception:
            return False
    return bool(getattr(adapter, "SUPPORTS_STREAMING_CARD", False))


def _can_install_compat(adapter: Any) -> bool:
    return callable(getattr(adapter, "_feishu_send_with_retry", None)) and (
        _can_use_cardkit(adapter)
        or callable(getattr(adapter, "_patch_auth_card", None))
        or _can_patch_interactive_message(adapter)
        or _can_update_interactive_message(adapter)
    )


def _supports_streaming_card(self: Any) -> bool:
    return _can_install_compat(self)


async def _start_streaming_card(
    self: Any,
    *,
    chat_id: str,
    reply_to: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> Any:
    if not _supports_streaming_card(self):
        return _result(False, error="streaming card compat unavailable")

    state = _new_state()
    if _can_use_cardkit(self):
        try:
            card_id = await _create_cardkit_card(self, _render_cardkit_initial_card())
            if card_id:
                state["card_id"] = card_id
                state["original_card_id"] = card_id
                state["sequence"] = 1
                response = await self._feishu_send_with_retry(
                    chat_id=chat_id,
                    msg_type="interactive",
                    payload=json.dumps({"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False),
                    reply_to=reply_to,
                    metadata=metadata,
                )
                result = _finalize(self, response, "streaming CardKit send failed")
                message_id = getattr(result, "message_id", None)
                if getattr(result, "success", False) and message_id:
                    _states(self)[str(message_id)] = state
                    logger.info(
                        "multitenancy: Feishu CardKit compat card sent message_id=%s card_id=%s",
                        message_id,
                        card_id,
                    )
                    return result
                return result
        except Exception as exc:
            logger.warning("multitenancy: Feishu CardKit compat flow failed, using IM fallback: %s", exc)
            state["card_id"] = None
            state["original_card_id"] = None
            state["sequence"] = 0

    try:
        response = await self._feishu_send_with_retry(
            chat_id=chat_id,
            msg_type="interactive",
            payload=json.dumps(_render_message_card(state), ensure_ascii=False),
            reply_to=reply_to,
            metadata=metadata,
        )
        result = _finalize(self, response, "streaming card compat send failed")
        message_id = getattr(result, "message_id", None)
        if getattr(result, "success", False) and message_id:
            _states(self)[str(message_id)] = state
        return result
    except Exception as exc:
        logger.warning("multitenancy: Feishu CardKit compat start failed: %s", exc)
        return _result(False, error=str(exc))


async def _update_streaming_card(
    self: Any,
    *,
    chat_id: str,
    message_id: str,
    content: str,
    finalize: bool = False,
) -> Any:
    del chat_id
    state = _state_for(self, message_id)
    formatted = _format(self, content)
    raw_tool_intents, visible_text = _extract_raw_tool_call_intents(formatted)
    _merge_raw_tool_intents(state, raw_tool_intents)
    answer_text, reasoning_text = _split_reasoning_text(visible_text)
    if reasoning_text:
        state["reasoning"] = reasoning_text
    if state.get("reasoning_started_at") and not state.get("reasoning_elapsed"):
        state["reasoning_elapsed"] = max(0.0, time.monotonic() - float(state["reasoning_started_at"]))
    state["content"] = answer_text
    state["finalized"] = bool(finalize)
    if finalize:
        state["status"] = ""
    return await _flush_state(self, message_id, state, final=finalize, pop=finalize)


async def _abort_streaming_card(
    self: Any,
    *,
    chat_id: str,
    message_id: str,
    content: Optional[str] = None,
) -> Any:
    del chat_id
    state = _state_for(self, message_id)
    if content:
        state["content"] = _format(self, content)
    state["status"] = "Aborted."
    state["aborted"] = True
    state["finalized"] = True
    return await _flush_state(self, message_id, state, final=True, pop=True)


async def _update_streaming_card_reasoning(
    self: Any,
    *,
    chat_id: str,
    message_id: str,
    content: str,
) -> Any:
    del chat_id
    state = _state_for(self, message_id)
    if not state.get("reasoning_started_at"):
        state["reasoning_started_at"] = time.monotonic()
    answer_text, reasoning_text = _split_reasoning_text(_format(self, content))
    state["reasoning"] = reasoning_text or answer_text
    return await _flush_state(self, message_id, state)


async def _update_streaming_card_status(
    self: Any,
    *,
    chat_id: str,
    message_id: str,
    content: str,
) -> Any:
    del chat_id
    state = _state_for(self, message_id)
    state["status"] = _format(self, content)
    return await _flush_state(self, message_id, state)


async def _update_streaming_card_tool_started(
    self: Any,
    *,
    chat_id: str,
    message_id: str,
    tool_name: str,
    preview: Optional[str] = None,
    args: Optional[dict[str, Any]] = None,
) -> Any:
    del chat_id
    state = _state_for(self, message_id)
    state["tools"].append(
        {
            "name": str(tool_name or "tool"),
            "status": "running",
            "preview": str(preview or "")[:240],
            "args": args,
        }
    )
    return await _flush_state(self, message_id, state)


async def _update_streaming_card_tool_completed(
    self: Any,
    *,
    chat_id: str,
    message_id: str,
    tool_name: str,
    duration: Optional[float] = None,
    is_error: bool = False,
) -> Any:
    del chat_id
    state = _state_for(self, message_id)
    name = str(tool_name or "tool")
    for tool in reversed(state["tools"]):
        if tool.get("name") == name and tool.get("status") == "running":
            tool["status"] = "error" if is_error else "done"
            tool["duration"] = duration
            break
    else:
        state["tools"].append(
            {
                "name": name,
                "status": "error" if is_error else "done",
                "duration": duration,
            }
        )
    return await _flush_state(self, message_id, state)


async def _flush_state(
    adapter: Any,
    message_id: str,
    state: dict[str, Any],
    *,
    final: bool = False,
    pop: bool = False,
) -> Any:
    if not message_id:
        return _result(False, error="missing message_id")
    try:
        card_id = str(state.get("card_id") or "")
        original_card_id = str(state.get("original_card_id") or "")
        if final and original_card_id:
            await _set_card_streaming_mode(adapter, original_card_id, False, _next_sequence(state))
            await _update_cardkit_card(adapter, original_card_id, _to_cardkit2(_render_message_card(state)), _next_sequence(state))
            if pop:
                _states(adapter).pop(str(message_id), None)
            return _result(True, message_id=str(message_id))

        if card_id:
            try:
                await _stream_cardkit_content(adapter, card_id, _render_stream_text(state), _next_sequence(state))
                return _result(True, message_id=str(message_id))
            except Exception as exc:
                logger.warning(
                    "multitenancy: Feishu CardKit content update failed, final card will use card.update: %s",
                    exc,
                )
                state["card_id"] = None
                return _result(True, message_id=str(message_id))

        if original_card_id:
            return _result(True, message_id=str(message_id))

        success = await _patch_interactive_state(adapter, str(message_id), state)
        if success and pop:
            _states(adapter).pop(str(message_id), None)
        return _result(bool(success), message_id=str(message_id), error=None if success else "card patch failed")
    except Exception as exc:
        logger.warning("multitenancy: Feishu CardKit compat patch failed: %s", exc)
        return _result(False, message_id=str(message_id), error=str(exc))


async def _patch_interactive_state(adapter: Any, message_id: str, state: dict[str, Any]) -> bool:
    card = _render_message_card(state)
    patch_auth_card = getattr(adapter, "_patch_auth_card", None)
    if callable(patch_auth_card):
        return bool(await patch_auth_card(message_id, card))
    if _can_patch_interactive_message(adapter):
        return await _patch_interactive_message(adapter, message_id, card)
    if _can_update_interactive_message(adapter):
        return await _update_interactive_message(adapter, message_id, card)
    return False


def _can_use_cardkit(adapter: Any) -> bool:
    client = getattr(adapter, "_client", None)
    cardkit = getattr(getattr(client, "cardkit", None), "v1", None)
    card = getattr(cardkit, "card", None)
    card_element = getattr(cardkit, "card_element", None)
    return bool(
        client
        and callable(getattr(adapter, "_feishu_send_with_retry", None))
        and callable(getattr(card, "create", None))
        and callable(getattr(card, "settings", None))
        and callable(getattr(card, "update", None))
        and callable(getattr(card_element, "content", None))
    )


def _can_patch_interactive_message(adapter: Any) -> bool:
    client = getattr(adapter, "_client", None)
    message = getattr(getattr(getattr(client, "im", None), "v1", None), "message", None)
    return bool(client and callable(getattr(message, "patch", None)))


def _can_update_interactive_message(adapter: Any) -> bool:
    client = getattr(adapter, "_client", None)
    message = getattr(getattr(getattr(client, "im", None), "v1", None), "message", None)
    return bool(
        client
        and callable(getattr(message, "update", None))
        and callable(getattr(adapter, "_build_update_message_body", None))
        and callable(getattr(adapter, "_build_update_message_request", None))
    )


async def _create_cardkit_card(adapter: Any, card: dict[str, Any]) -> Optional[str]:
    request = _build_create_card_request(card)
    response = await asyncio.to_thread(adapter._client.cardkit.v1.card.create, request)
    _raise_on_lark_error(response, "card.create")
    return _extract_response_field(response, "card_id")


async def _stream_cardkit_content(adapter: Any, card_id: str, content: str, sequence: int) -> None:
    request = _build_content_card_element_request(card_id, _STREAMING_ELEMENT_ID, content, sequence)
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
    settings = json.dumps({"streaming_mode": streaming_mode})
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


def _new_state() -> dict[str, Any]:
    return {
        "status": "Hermes is preparing a response...",
        "reasoning": "",
        "reasoning_started_at": None,
        "reasoning_elapsed": None,
        "content": "",
        "tools": [],
        "started_at": time.monotonic(),
        "finalized": False,
        "aborted": False,
        "card_id": None,
        "original_card_id": None,
        "sequence": 0,
    }


def _states(adapter: Any) -> dict[str, dict[str, Any]]:
    state = getattr(adapter, _STATE_ATTR, None)
    if not isinstance(state, dict):
        state = {}
        setattr(adapter, _STATE_ATTR, state)
    return state


def _state_for(adapter: Any, message_id: str) -> dict[str, Any]:
    return _states(adapter).setdefault(str(message_id), _new_state())


def _next_sequence(state: dict[str, Any]) -> int:
    state["sequence"] = int(state.get("sequence") or 0) + 1
    return int(state["sequence"])


def _format(adapter: Any, content: str) -> str:
    formatter = getattr(adapter, "format_message", None)
    if callable(formatter):
        try:
            return str(formatter(content or ""))
        except Exception:
            pass
    return str(content or "")


def _render_cardkit_initial_card() -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {
            "streaming_mode": True,
            "locales": ["zh_cn", "en_us"],
            "summary": {
                "content": "Thinking...",
                "i18n_content": {"zh_cn": "思考中...", "en_us": "Thinking..."},
            },
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "",
                    "text_align": "left",
                    "text_size": "normal_v2",
                    "margin": "0px 0px 0px 0px",
                    "element_id": _STREAMING_ELEMENT_ID,
                },
                {
                    "tag": "markdown",
                    "content": " ",
                    "element_id": "loading_icon",
                },
            ]
        },
    }


def _render_stream_text(state: dict[str, Any]) -> str:
    content = _optimize_markdown_style(_strip_tool_call_blocks(_strip_reasoning_tags(str(state.get("content") or ""))).strip())
    reasoning = _clean_reasoning_prefix(str(state.get("reasoning") or "")).strip()
    status = str(state.get("status") or "").strip()
    tools = list(state.get("tools") or [])

    parts: list[str] = []
    tool_section = _render_tool_calls_section(tools)
    if tool_section:
        parts.append(tool_section)
    if content:
        parts.append(content)
    elif reasoning:
        parts.append(f"💭 **Thinking...**\n\n{_clip(reasoning, 1200)}")
    elif status:
        parts.append(status)
    else:
        parts.append("Thinking...")
    return "\n\n".join(part for part in parts if part).strip() or "Thinking..."


def _render_message_card(state: dict[str, Any]) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    status = str(state.get("status") or "").strip()
    content, inline_reasoning = _split_reasoning_text(_strip_tool_call_blocks(str(state.get("content") or "")))
    reasoning = _clean_reasoning_prefix(str(state.get("reasoning") or inline_reasoning or "")).strip()
    content = _strip_reasoning_tags(content).strip()
    tools = list(state.get("tools") or [])

    tool_section = _render_tool_calls_section(tools)
    if tool_section:
        elements.append({"tag": "markdown", "content": tool_section})
    if status and not state.get("finalized"):
        elements.append({"tag": "markdown", "content": status})
    if reasoning:
        elements.append(_render_reasoning_panel(reasoning, state))
    if content:
        elements.append({"tag": "markdown", "content": _optimize_markdown_style(content)})
    else:
        elements.append({"tag": "markdown", "content": "..."})
    if state.get("finalized") or state.get("aborted"):
        elements.append(
            {
                "tag": "markdown",
                "content": _render_done_footer(state),
                "text_size": "notation",
            }
        )

    return {
        "config": {
            "wide_screen_mode": True,
            "update_multi": True,
            "locales": ["zh_cn", "en_us"],
            "summary": {"content": _plain_summary(content or status or "Hermes")},
        },
        "elements": elements,
    }


def _to_cardkit2(card: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "2.0",
        "config": card.get("config", {}),
        "body": {"elements": card.get("elements", [])},
    }
    if card.get("header"):
        result["header"] = card["header"]
    return result


def _render_reasoning_panel(reasoning: str, state: dict[str, Any]) -> dict[str, Any]:
    zh_label, en_label = _format_reasoning_label(state)
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {
                "tag": "markdown",
                "content": f"💭 {en_label}",
                "i18n_content": {"zh_cn": f"💭 {zh_label}", "en_us": f"💭 {en_label}"},
            },
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
            },
            "icon_position": "follow_text",
            "icon_expanded_angle": -180,
        },
        "border": {"color": "grey", "corner_radius": "5px"},
        "vertical_spacing": "8px",
        "padding": "8px 8px 8px 8px",
        "elements": [{"tag": "markdown", "content": _clip(reasoning, 1200), "text_size": "notation"}],
    }


def _render_tool_calls_section(tools: list[Any]) -> str:
    normalized = [tool for tool in tools[-5:] if isinstance(tool, dict)]
    if not normalized:
        return ""
    lines = ["**Tool calls:**"]
    lines.extend(_render_tool_call_line(tool) for tool in normalized)
    return "\n".join(lines)


def _render_tool_call_line(tool: dict[str, Any]) -> str:
    name = str(tool.get("name") or "tool")
    status = str(tool.get("status") or "running")
    extra = " running" if status == "running" else " failed" if status == "error" else ""
    if tool.get("duration") is not None:
        extra = f" ({_format_tool_duration(tool['duration'])})"
    elif tool.get("preview"):
        extra = f": {_clip(str(tool['preview']), 160)}"
    return f"- `{name}`{extra}"


def _render_done_footer(state: dict[str, Any]) -> str:
    label = "Aborted" if state.get("aborted") else "Done"
    return f"{label} ({_format_elapsed_since_start(state)})"


def _format_elapsed_since_start(state: dict[str, Any]) -> str:
    started_at = state.get("started_at")
    if isinstance(started_at, (int, float)) and started_at > 0:
        elapsed = max(0.0, time.monotonic() - float(started_at))
        return f"{elapsed:.1f}s"
    return "0.0s"


def _format_tool_duration(value: Any) -> str:
    try:
        seconds = max(0.0, float(value))
    except (TypeError, ValueError):
        return str(value)
    return f"{int(round(seconds * 1000))} ms"


def _extract_raw_tool_call_intents(text: str) -> tuple[list[dict[str, Any]], str]:
    raw = str(text or "")
    intents: list[dict[str, Any]] = []
    for match in _TOOL_CALL_BLOCK_RE.finditer(raw):
        payload = match.group(1).strip()
        try:
            parsed = json.loads(payload)
        except Exception:
            parsed = {}
        name = str(parsed.get("name") or "").strip() if isinstance(parsed, dict) else ""
        if name:
            intents.append({"name": name, "status": "error"})
    return intents, _strip_tool_call_blocks(raw)


def _merge_raw_tool_intents(state: dict[str, Any], intents: list[dict[str, Any]]) -> None:
    if not intents:
        return
    tools = state.setdefault("tools", [])
    if not isinstance(tools, list):
        state["tools"] = tools = []
    existing_names = {str(tool.get("name") or "") for tool in tools if isinstance(tool, dict)}
    for intent in intents:
        name = str(intent.get("name") or "")
        if name and name not in existing_names:
            tools.append(intent)
            existing_names.add(name)


def _strip_tool_call_blocks(text: str) -> str:
    return _TOOL_CALL_BLOCK_RE.sub("", str(text or ""))


def _format_reasoning_label(state: dict[str, Any]) -> tuple[str, str]:
    elapsed = state.get("reasoning_elapsed")
    if not isinstance(elapsed, (int, float)) and state.get("reasoning_started_at"):
        try:
            elapsed = max(0.0, time.monotonic() - float(state["reasoning_started_at"]))
        except (TypeError, ValueError):
            elapsed = None
    if isinstance(elapsed, (int, float)):
        duration = _format_elapsed(float(elapsed))
        return f"思考了 {duration}", f"Thought for {duration}"
    return "思考", "Thought"


def _format_elapsed(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {round(seconds % 60)}s"


def _split_reasoning_text(text: str) -> tuple[str, str]:
    raw = str(text or "")
    reasoning_parts = [_clean_reasoning_prefix(match.group(2)) for match in _REASONING_TAG_RE.finditer(raw)]
    unclosed = _UNCLOSED_REASONING_TAG_RE.search(raw)
    if unclosed and not _REASONING_TAG_RE.search(raw[unclosed.start() :]):
        reasoning_parts.append(_clean_reasoning_prefix(unclosed.group(2)))
    answer = _strip_reasoning_tags(raw).strip()

    prefix = re.match(r"^Reasoning:\s*([\s\S]*)$", answer, flags=re.IGNORECASE)
    if prefix:
        body = prefix.group(1).strip()
        if "\n\n" in body:
            reasoning, remaining = body.split("\n\n", 1)
            reasoning_parts.insert(0, _clean_reasoning_prefix(reasoning))
            answer = remaining.strip()
        else:
            reasoning_parts.insert(0, _clean_reasoning_prefix(body))
            answer = ""

    reasoning = "\n\n".join(part for part in reasoning_parts if part).strip()
    return answer, reasoning


def _strip_reasoning_tags(text: str) -> str:
    without_closed = _REASONING_TAG_RE.sub("", str(text or ""))
    without_unclosed = _UNCLOSED_REASONING_TAG_RE.sub("", without_closed)
    return without_unclosed


def _clean_reasoning_prefix(text: str) -> str:
    cleaned = re.sub(r"^Reasoning:\s*", "", str(text or ""), flags=re.IGNORECASE)
    cleaned = "\n".join(re.sub(r"^_(.+)_$", r"\1", line) for line in cleaned.splitlines())
    return cleaned.strip()


def _optimize_markdown_style(text: str, card_version: int = 2) -> str:
    try:
        return _strip_invalid_image_keys(_optimize_markdown_style_inner(text, card_version))
    except Exception:
        return str(text or "")


def _optimize_markdown_style_inner(text: str, card_version: int = 2) -> str:
    original = str(text or "")
    code_blocks: list[str] = []

    def stash_code_block(match: re.Match[str]) -> str:
        code_blocks.append(match.group(0))
        return f"___CB_{len(code_blocks) - 1}___"

    result = re.sub(r"```[\s\S]*?```", stash_code_block, original)
    if re.search(r"^#{1,3} ", original, flags=re.MULTILINE):
        result = re.sub(r"^#{2,6} (.+)$", r"##### \1", result, flags=re.MULTILINE)
        result = re.sub(r"^# (.+)$", r"#### \1", result, flags=re.MULTILINE)

    if card_version >= 2:
        result = re.sub(r"^(#{4,5} .+)\n{1,2}(#{4,5} )", r"\1\n<br>\n\2", result, flags=re.MULTILINE)
        result = re.sub(r"^([^|\n].*)\n(\|.+\|)", r"\1\n\n\2", result, flags=re.MULTILINE)
        result = re.sub(r"\n\n((?:\|.+\|[^\S\n]*\n?)+)", r"\n\n<br>\n\n\1", result)
        result = re.sub(r"((?:^\|.+\|[^\S\n]*\n?)+)", r"\1\n<br>\n", result, flags=re.MULTILINE)
        result = re.sub(r"^((?!#{4,5} )(?!\*\*).+)\n\n(<br>)\n\n(\|)", r"\1\n\2\n\3", result, flags=re.MULTILINE)
        result = re.sub(r"^(\*\*.+)\n\n(<br>)\n\n(\|)", r"\1\n\2\n\n\3", result, flags=re.MULTILINE)
        result = re.sub(r"(\|[^\n]*\n)\n(<br>\n)((?!#{4,5} )(?!\*\*))", r"\1\2\3", result, flags=re.MULTILINE)
        for index, block in enumerate(code_blocks):
            result = result.replace(f"___CB_{index}___", f"\n<br>\n{block}\n<br>\n")
    else:
        for index, block in enumerate(code_blocks):
            result = result.replace(f"___CB_{index}___", block)

    return re.sub(r"\n{3,}", "\n\n", result)


def _strip_invalid_image_keys(text: str) -> str:
    if "![" not in text:
        return text

    def replace(match: re.Match[str]) -> str:
        return match.group(0) if match.group(2).startswith("img_") else ""

    return _IMAGE_RE.sub(replace, text)


def _plain_summary(text: str) -> str:
    summary = re.sub(r"[*_`#>\[\]()~]", "", str(text or "")).strip()
    return summary[:120] if summary else "Hermes"


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "\n...[truncated]"


def _raise_on_lark_error(response: Any, api: str) -> None:
    code = getattr(response, "code", None)
    if code is not None and code != 0:
        msg = getattr(response, "msg", "")
        raise RuntimeError(f"{api} failed: code={code}, msg={msg}")


def _response_succeeded(adapter: Any, response: Any, default_message: str) -> bool:
    finalizer = getattr(adapter, "_finalize_send_result", None)
    if callable(finalizer):
        try:
            return bool(getattr(finalizer(response, default_message), "success", False))
        except Exception:
            pass
    succeeded = getattr(adapter, "_response_succeeded", None)
    if callable(succeeded):
        return bool(succeeded(response))
    code = getattr(response, "code", None)
    return code is None or code == 0


def _finalize(adapter: Any, response: Any, default_message: str) -> Any:
    finalizer = getattr(adapter, "_finalize_send_result", None)
    if callable(finalizer):
        return finalizer(response, default_message)
    message_id = _extract_response_field(response, "message_id")
    if message_id:
        return _result(True, message_id=message_id, raw_response=response)
    return _result(False, error=default_message, raw_response=response)


def _extract_response_field(response: Any, name: str) -> Optional[str]:
    for source in (
        response,
        getattr(response, "data", None),
        getattr(response, "message", None),
    ):
        if isinstance(source, dict):
            value = source.get(name)
        else:
            value = getattr(source, name, None)
        if value:
            return str(value)
    return None


def _result(
    success: bool,
    *,
    message_id: Optional[str] = None,
    error: Optional[str] = None,
    raw_response: Any = None,
) -> Any:
    if SendResult is not None:
        return SendResult(success=success, message_id=message_id, error=error, raw_response=raw_response)
    return SimpleNamespace(success=success, message_id=message_id, error=error, raw_response=raw_response)
