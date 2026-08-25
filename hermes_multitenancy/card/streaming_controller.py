"""Streaming-card orchestrator + entry point.

``ensure_feishu_cardkit_streaming(adapter)`` installs the 8-method streaming
surface (``supports_streaming_card``, ``start_streaming_card``,
``update_streaming_card``, ``abort_streaming_card``,
``update_streaming_card_reasoning`` / ``_status`` /
``_tool_started`` / ``_tool_completed``) on a vanilla Feishu adapter, and
``_flush_state`` drives every per-event render+send loop with the three-tier
CardKit / IM-patch / IM-update fallback.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import os
import time
from types import MethodType
from typing import Any, Awaitable, Callable, Optional

from .builder import (
    _TOOLS_ELEMENT_ID,
    _render_cardkit_initial_card,
    _render_message_card,
    _render_stream_text,
    _to_cardkit2,
)
from .card_error import RateLimitError, StreamingClosedError, TableLimitError, UnavailableError, _finalize, _result
from .cardkit_client import (
    _create_cardkit_card,
    _patch_interactive_message,
    _set_card_streaming_mode,
    _stream_cardkit_content,
    _stream_cardkit_element,
    _update_cardkit_card,
    _update_interactive_message,
)
from .flush_controller import FlushController
from .markdown_style import _optimize_markdown_style
from .reasoning import _split_reasoning_text
from .reply_dispatcher import (
    _can_patch_interactive_message,
    _can_update_interactive_message,
    _can_use_cardkit,
)
from .sanitization import _format, _plain_summary, _unescape_display_whitespace
from .state import (
    _INSTALLED_ATTR,
    _STATE_ATTR,
    PHASE_ABORTED,
    PHASE_COMPLETED,
    PHASE_CREATING,
    PHASE_CREATION_FAILED,
    PHASE_ERROR,
    PHASE_STREAMING,
    _acquire_epoch,
    _new_state,
    _next_sequence,
    _mark_terminal_message,
    _state_for,
    _states,
    _transition,
)
from .tool_use_display import (
    _extract_raw_tool_call_intents,
    _format_todo_progress,
    _merge_raw_tool_intents,
    _render_tool_calls_section,
)
from .unavailable_guard import UnavailableGuard

# Preserve the legacy logger name so existing log filters / alerts / collectors
# that match on logger name (hermes_multitenancy.feishu_cardkit_compat) keep
# firing. The implementation moved under hermes_multitenancy.card.*, but the
# observable logger name must not change with the modularization.
logger = logging.getLogger("hermes_multitenancy.feishu_cardkit_compat")
_FLUSH_CONTROLLERS_ATTR = "_hermes_mt_streaming_flush_controllers"
_NATIVE_RECOVERY_WRAPPED_ATTR = "_hermes_mt_native_streaming_recovery_wrapped"


def _is_terminal_state(state: dict[str, Any]) -> bool:
    return bool(
        state.get("finalized")
        or state.get("aborted")
        or state.get("errored")
        or state.get("phase") in {PHASE_ABORTED, PHASE_COMPLETED, PHASE_ERROR}
    )


def _card_write_lock(state: dict[str, Any]) -> asyncio.Lock:
    lock = state.get("_write_lock")
    if isinstance(lock, asyncio.Lock):
        return lock
    lock = asyncio.Lock()
    state["_write_lock"] = lock
    return lock


def _card_content_throttle_s() -> float:
    """Min seconds between streamed content/reasoning network pushes (issue #4).

    Coalesces rapid token frames so the card updates as a smooth typewriter
    instead of one blocking round-trip per token. Env-tunable for real-machine
    tuning without a redeploy; 0 disables throttling (revert to per-event push).
    """
    raw = os.getenv("HERMES_CARD_CONTENT_THROTTLE_S")
    if raw is None:
        # Just under the consumer's 0.1s content gate so it stays transparent
        # for content (no double-coarsening) while still coalescing faster
        # reasoning frames. openclaw CardKit throttle is 100ms.
        return 0.08
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.08


def _card_long_gap_threshold_s() -> float:
    """Idle-gap threshold before the next flush batches briefly."""
    raw = os.getenv("HERMES_CARD_LONG_GAP_THRESHOLD_S")
    if raw is None:
        return 2.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 2.0


def _card_batch_after_gap_s() -> float:
    """Post-gap batch window so the next visible frame is meaningful."""
    raw = os.getenv("HERMES_CARD_BATCH_AFTER_GAP_S")
    if raw is None:
        return 0.3
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.3


def ensure_feishu_cardkit_streaming(adapter: Any) -> Any:
    """Install streaming-card methods on a Feishu adapter when they are absent."""
    if adapter is None:
        return None
    if _has_native_streaming_surface(adapter):
        _wrap_native_streaming_recovery(adapter)
        return adapter
    if getattr(adapter, _INSTALLED_ATTR, False):
        return adapter
    if not _can_install_compat(adapter):
        return adapter

    setattr(adapter, _STATE_ATTR, {})
    setattr(adapter, _FLUSH_CONTROLLERS_ATTR, {})
    adapter.supports_streaming_card = MethodType(_supports_streaming_card, adapter)
    adapter.start_streaming_card = MethodType(_start_streaming_card, adapter)
    adapter.update_streaming_card = MethodType(_update_streaming_card, adapter)
    adapter.abort_streaming_card = MethodType(_abort_streaming_card, adapter)
    adapter.fail_streaming_card = MethodType(_fail_streaming_card, adapter)
    adapter.update_streaming_card_reasoning = MethodType(_update_streaming_card_reasoning, adapter)
    adapter.update_streaming_card_status = MethodType(_update_streaming_card_status, adapter)
    adapter.update_streaming_card_tool_started = MethodType(_update_streaming_card_tool_started, adapter)
    adapter.update_streaming_card_tool_completed = MethodType(_update_streaming_card_tool_completed, adapter)
    adapter.update_streaming_card_metrics = MethodType(_update_streaming_card_metrics, adapter)
    setattr(adapter, _INSTALLED_ATTR, True)
    logger.info("multitenancy: installed Feishu CardKit compat streaming surface")
    return adapter


def supports_streaming_closed_recovery(adapter: Any) -> bool:
    """Whether the selected streaming surface has bounded close-code recovery."""
    return bool(
        getattr(adapter, _INSTALLED_ATTR, False)
        or getattr(adapter, _NATIVE_RECOVERY_WRAPPED_ATTR, False)
    )


def _wrap_native_streaming_recovery(adapter: Any) -> bool:
    """Wrap an explicit native reopen contract with one retry per write.

    Native adapters own their CardKit sequence counter, so the required
    ``reopen_streaming_card`` method must re-enable streaming with its next
    sequence. Adapters without that contract are left untouched; the router
    selects edit transport before opening a card.
    """
    if getattr(adapter, _NATIVE_RECOVERY_WRAPPED_ATTR, False):
        return True
    reopener = getattr(adapter, "reopen_streaming_card", None)
    if not callable(reopener):
        return False

    method_names = (
        "update_streaming_card",
        "update_streaming_card_reasoning",
        "update_streaming_card_status",
        "update_streaming_card_tool_started",
        "update_streaming_card_tool_completed",
        "fail_streaming_card",
    )
    for method_name in method_names:
        original = getattr(adapter, method_name, None)
        if not callable(original):
            continue

        def _make_wrapper(method: Callable[..., Any]) -> Callable[..., Awaitable[Any]]:
            @functools.wraps(method)
            async def _wrapped(*args: Any, **kwargs: Any) -> Any:
                try:
                    result = method(*args, **kwargs)
                    return await result if inspect.isawaitable(result) else result
                except StreamingClosedError:
                    reopened = reopener(
                        chat_id=str(kwargs.get("chat_id") or ""),
                        message_id=str(kwargs.get("message_id") or ""),
                    )
                    if inspect.isawaitable(reopened):
                        await reopened
                    result = method(*args, **kwargs)
                    return await result if inspect.isawaitable(result) else result

            return _wrapped

        setattr(adapter, method_name, _make_wrapper(original))

    setattr(adapter, _NATIVE_RECOVERY_WRAPPED_ATTR, True)
    logger.info("multitenancy: wrapped native Feishu CardKit close-code recovery")
    return True


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
    state["epoch"] = _acquire_epoch(self)
    _transition(state, PHASE_CREATING)
    if _can_use_cardkit(self):
        try:
            card_id = await _create_cardkit_card(self, _render_cardkit_initial_card())
            if card_id:
                state["card_id"] = card_id
                state["original_card_id"] = card_id
                state["sequence"] = 1
                state["chat_id"] = str(chat_id or "")
                state["reply_to"] = reply_to
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
                    _transition(state, PHASE_STREAMING)
                    _states(self)[str(message_id)] = state
                    logger.info(
                        "multitenancy: Feishu CardKit compat card sent message_id=%s card_id=%s",
                        message_id,
                        card_id,
                    )
                    return result
                _transition(state, PHASE_CREATION_FAILED)
                return result
        except Exception as exc:
            logger.warning("multitenancy: Feishu CardKit compat flow failed, using IM fallback: %s", exc)
            state["card_id"] = None
            state["original_card_id"] = None
            state["sequence"] = 0

    try:
        state["chat_id"] = str(chat_id or "")
        state["reply_to"] = reply_to
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
            _transition(state, PHASE_STREAMING)
            _states(self)[str(message_id)] = state
        else:
            _transition(state, PHASE_CREATION_FAILED)
        return result
    except Exception as exc:
        logger.warning("multitenancy: Feishu CardKit compat start failed: %s", exc)
        _transition(state, PHASE_CREATION_FAILED)
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
    if not UnavailableGuard.should_proceed(self, message_id):
        return _result(False, message_id=str(message_id), error="card unavailable")
    formatted = _format(self, content)
    state = _state_for(self, message_id)
    async with _card_write_lock(state):
        if _is_terminal_state(state):
            return _result(True, message_id=str(message_id))
        raw_tool_intents, visible_text = _extract_raw_tool_call_intents(formatted)
        _merge_raw_tool_intents(state, raw_tool_intents)
        answer_text, reasoning_text = _split_reasoning_text(visible_text)
        if reasoning_text:
            state["reasoning"] = _unescape_display_whitespace(reasoning_text)
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
    if not UnavailableGuard.should_proceed(self, message_id):
        return _result(False, message_id=str(message_id), error="card unavailable")
    state = _state_for(self, message_id)
    async with _card_write_lock(state):
        if _is_terminal_state(state):
            return _result(True, message_id=str(message_id))
        if content:
            state["content"] = _format(self, content)
        state["status"] = "Aborted."
        state["aborted"] = True
        state["finalized"] = True
        _transition(state, PHASE_ABORTED)
    return await _flush_state(self, message_id, state, final=True, pop=True)


async def _fail_streaming_card(
    self: Any,
    *,
    chat_id: str,
    message_id: str,
    content: Optional[str] = None,
) -> Any:
    del chat_id
    if not UnavailableGuard.should_proceed(self, message_id):
        return _result(False, message_id=str(message_id), error="card unavailable")
    state = _state_for(self, message_id)
    async with _card_write_lock(state):
        if _is_terminal_state(state):
            return _result(True, message_id=str(message_id))
        if content:
            state["content"] = _format(self, content)
        state["status"] = ""
        state["reasoning"] = ""
        state["errored"] = True
        state["finalized"] = True
        _transition(state, PHASE_ERROR)
    return await _flush_state(self, message_id, state, final=True, pop=True)


def _update_streaming_card_metrics(
    self: Any,
    *,
    message_id: str,
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    cache_hit_pct: Optional[float] = None,
    context_pct: Optional[float] = None,
    model_name: Optional[str] = None,
) -> Any:
    """Merge LLM observability metrics into the per-message state.

    No API call is made — metrics are rendered onto the final card by
    ``_render_done_footer`` at finalize time when ``FOOTER_SHOW_METRICS``
    env flag is set. Synchronous: router.py can call without await.
    Unknown ``message_id`` is a no-op (defensive against stale callers).
    """
    if not message_id:
        return _result(False, error="missing message_id")
    if not UnavailableGuard.should_proceed(self, message_id):
        return _result(False, message_id=str(message_id), error="card unavailable")
    states = _states(self)
    state = states.get(str(message_id))
    if state is None:
        return _result(False, message_id=str(message_id), error="unknown message_id")
    if _is_terminal_state(state):
        return _result(True, message_id=str(message_id))
    if tokens_in is not None:
        state["tokens_in"] = tokens_in
    if tokens_out is not None:
        state["tokens_out"] = tokens_out
    if cache_hit_pct is not None:
        state["cache_hit_pct"] = cache_hit_pct
    if context_pct is not None:
        state["context_pct"] = context_pct
    if model_name is not None:
        state["model_name"] = model_name
    return _result(True, message_id=str(message_id))


async def _update_streaming_card_reasoning(
    self: Any,
    *,
    chat_id: str,
    message_id: str,
    content: str,
) -> Any:
    del chat_id
    if not UnavailableGuard.should_proceed(self, message_id):
        return _result(False, message_id=str(message_id), error="card unavailable")
    state = _state_for(self, message_id)
    async with _card_write_lock(state):
        if _is_terminal_state(state):
            return _result(True, message_id=str(message_id))
        if not state.get("reasoning_started_at"):
            state["reasoning_started_at"] = time.monotonic()
        answer_text, reasoning_text = _split_reasoning_text(_format(self, content))
        # ``or answer_text``: a structured ``reasoning_content`` payload carries no
        # <think> tags, so the whole thing IS the reasoning. Unescape before it
        # reaches the panel — raw model text escapes its own newlines (2026-08-20).
        state["reasoning"] = _unescape_display_whitespace(reasoning_text or answer_text)
    # issue #4: route reasoning through the throttled _flush_state (it pushes the
    # same _render_stream_text content) so rapid thinking tokens coalesce into a
    # smooth typewriter instead of one blocking round-trip per token.
    return await _flush_state(self, message_id, state)


async def _update_streaming_card_status(
    self: Any,
    *,
    chat_id: str,
    message_id: str,
    content: str,
) -> Any:
    del chat_id
    if not UnavailableGuard.should_proceed(self, message_id):
        return _result(False, message_id=str(message_id), error="card unavailable")
    formatted = _format(self, content)
    if _is_invisible_card_status(formatted):
        return _result(True, message_id=str(message_id))
    state = _state_for(self, message_id)
    async with _card_write_lock(state):
        if _is_terminal_state(state):
            return _result(True, message_id=str(message_id))
        state["status"] = formatted
    if state.get("card_id"):
        attempted, exc = await _write_cardkit_frame(
            self,
            state,
            lambda card_id, sequence: _stream_cardkit_content(
                self, card_id, str(state.get("status") or " "), sequence
            ),
        )
        if attempted and exc is None:
            return _result(True, message_id=str(message_id))
        if exc is not None:
            handled = _handle_cardkit_exc(self, message_id, state, exc)
            if handled is True:
                return _result(True, message_id=str(message_id))
            if handled is False:
                return _result(False, message_id=str(message_id), error=str(exc))
    return await _flush_state(self, message_id, state)


def _is_invisible_card_status(text: str) -> bool:
    stripped = str(text or "").strip()
    return stripped in {"", "\u200b", "\u200c", "\u200d", "\ufeff"}


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
    if not UnavailableGuard.should_proceed(self, message_id):
        return _result(False, message_id=str(message_id), error="card unavailable")
    state = _state_for(self, message_id)
    async with _card_write_lock(state):
        if _is_terminal_state(state):
            return _result(True, message_id=str(message_id))
        name = str(tool_name or "tool")
        row = {
            "name": name,
            "status": "running",
            "preview": str(preview or "")[:240],
            "args": args,
        }
        if name == "todo":
            progress = _format_todo_progress(args)
            existing = next(
                (tool for tool in state["tools"] if tool.get("name") == "todo"),
                None,
            )
            if existing is not None:
                is_read = args is None or (
                    isinstance(args, dict)
                    and (args.get("merge") or "todos" not in args)
                )
                if not progress and is_read:
                    progress = str(existing.get("todo_progress") or "")
                existing.clear()
                existing.update(row)
                if progress:
                    existing["todo_progress"] = progress
            else:
                if progress:
                    row["todo_progress"] = progress
                state["tools"].append(row)
        else:
            state["tools"].append(row)
    if state.get("card_id"):
        attempted, exc = await _write_cardkit_frame(
            self,
            state,
            lambda card_id, sequence: _stream_cardkit_element(
                self,
                card_id,
                _TOOLS_ELEMENT_ID,
                _render_tool_calls_section(list(state.get("tools") or [])) or " ",
                sequence,
            ),
        )
        if attempted and exc is None:
            return _result(True, message_id=str(message_id))
        if exc is not None:
            handled = _handle_cardkit_exc(self, message_id, state, exc)
            if handled is True:
                return _result(True, message_id=str(message_id))
            if handled is False:
                return _result(False, message_id=str(message_id), error=str(exc))
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
    if not UnavailableGuard.should_proceed(self, message_id):
        return _result(False, message_id=str(message_id), error="card unavailable")
    state = _state_for(self, message_id)
    async with _card_write_lock(state):
        if _is_terminal_state(state):
            return _result(True, message_id=str(message_id))
        name = str(tool_name or "tool")
        for tool in reversed(state["tools"]):
            if tool.get("name") == name and (
                name == "todo" or tool.get("status") == "running"
            ):
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
    if state.get("card_id"):
        attempted, exc = await _write_cardkit_frame(
            self,
            state,
            lambda card_id, sequence: _stream_cardkit_element(
                self,
                card_id,
                _TOOLS_ELEMENT_ID,
                _render_tool_calls_section(list(state.get("tools") or [])) or " ",
                sequence,
            ),
        )
        if attempted and exc is None:
            return _result(True, message_id=str(message_id))
        if exc is not None:
            handled = _handle_cardkit_exc(self, message_id, state, exc)
            if handled is True:
                return _result(True, message_id=str(message_id))
            if handled is False:
                return _result(False, message_id=str(message_id), error=str(exc))
    return await _flush_state(self, message_id, state)


async def _recover_closed_stream(
    adapter: Any,
    state: dict[str, Any],
    card_id: str,
    exc: Exception,
    retry: Callable[[int], Awaitable[Any]],
) -> Optional[Exception]:
    """Re-enable an auto-closed CardKit stream and retry the frame once."""
    if not isinstance(exc, StreamingClosedError):
        return exc
    try:
        await _set_card_streaming_mode(adapter, card_id, True, _next_sequence(state))
        await retry(_next_sequence(state))
    except Exception as recovery_exc:
        logger.warning(
            "multitenancy: Feishu CardKit stream recovery failed after code=%s: %s",
            exc.code,
            recovery_exc,
        )
        return recovery_exc
    logger.info("multitenancy: Feishu CardKit stream reopened after code=%s", exc.code)
    return None


async def _write_cardkit_frame(
    adapter: Any,
    state: dict[str, Any],
    write: Callable[[str, int], Awaitable[Any]],
) -> tuple[bool, Optional[Exception]]:
    """Serialize one card write and its optional reopen/retry pair."""
    async with _card_write_lock(state):
        if _is_terminal_state(state):
            return True, None
        card_id = str(state.get("card_id") or "")
        if not card_id:
            return False, None
        try:
            await write(card_id, _next_sequence(state))
        except Exception as exc:
            exc = await _recover_closed_stream(
                adapter,
                state,
                card_id,
                exc,
                lambda sequence: write(card_id, sequence),
            )
            return True, exc
        return True, None


def _handle_cardkit_exc(
    adapter: Any,
    message_id: str,
    state: dict[str, Any],
    exc: Exception,
) -> Optional[bool]:
    if isinstance(exc, UnavailableError):
        UnavailableGuard.mark_unavailable(adapter, str(message_id), exc.code)
        return False
    if isinstance(exc, RateLimitError):
        logger.debug("multitenancy: Feishu CardKit rate limited (230020), skipping frame")
        return True
    if isinstance(exc, TableLimitError):
        logger.warning("multitenancy: Feishu CardKit content exceeded table limit, falling back to final card: %s", exc)
        state["card_id"] = None
        return None
    logger.warning("multitenancy: Feishu CardKit update failed, falling back to final card: %s", exc)
    state["card_id"] = None
    return None


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

    async def _flush_body() -> Any:
        try:
            if not final and _is_terminal_state(state):
                return _result(True, message_id=str(message_id))
            card_id = str(state.get("card_id") or "")
            original_card_id = str(state.get("original_card_id") or "")
            if final:
                async with _card_write_lock(state):
                    return await _deliver_final_card(adapter, message_id, state, pop=pop)

            if card_id:
                rendered = _render_stream_text(state)
                missing = object()
                last_flushed = state.get("last_flushed_text", missing)
                if last_flushed is not missing and rendered == last_flushed:
                    return _result(True, message_id=str(message_id))
                attempted, exc = await _write_cardkit_frame(
                    adapter,
                    state,
                    lambda current_card_id, sequence: _stream_cardkit_content(
                        adapter, current_card_id, rendered, sequence
                    ),
                )
                if attempted and exc is None:
                    state["last_flushed_text"] = rendered
                    return _result(True, message_id=str(message_id))
                if exc is not None:
                    handled = _handle_cardkit_exc(adapter, message_id, state, exc)
                    if handled is True:
                        return _result(True, message_id=str(message_id))
                    if handled is False:
                        return _result(False, message_id=str(message_id), error=str(exc))
                return _result(True, message_id=str(message_id))

            if original_card_id:
                return _result(True, message_id=str(message_id))

            async with _card_write_lock(state):
                if _is_terminal_state(state):
                    return _result(True, message_id=str(message_id))
                success = await _patch_interactive_state(adapter, str(message_id), state)
            return _result(bool(success), message_id=str(message_id), error=None if success else "card patch failed")
        except Exception as exc:
            if isinstance(exc, UnavailableError):
                UnavailableGuard.mark_unavailable(adapter, str(message_id), exc.code)
            logger.warning("multitenancy: Feishu CardKit compat patch failed: %s", exc)
            return _result(False, message_id=str(message_id), error=str(exc))

    if final:
        # issue #4: cancel any armed throttle timer so a stale frame can't fire
        # after the card is finalized (no post-completion ghost update).
        controllers = _flush_controllers(adapter)
        existing = controllers.get(str(message_id))
        if existing is not None:
            existing.cancel()
        try:
            return await _flush_body()
        finally:
            controllers.pop(str(message_id), None)

    try:
        controller = _flush_controllers(adapter).setdefault(
            str(message_id),
            FlushController(
                long_gap_s=_card_long_gap_threshold_s(),
                batch_after_gap_s=_card_batch_after_gap_s(),
            ),
        )
        # Call signature stays (throttle_s, flush_callable) so duck-typed /
        # fork controllers (e.g. a core that supplies its own) keep working;
        # the long-gap batching knobs live on our FlushController instance.
        result = await controller.throttled_update(
            _card_content_throttle_s(),
            _flush_body,
        )
        if result is None:
            return _result(True, message_id=str(message_id))
        return result
    except Exception as exc:
        logger.warning(
            "multitenancy: FlushController failed for message_id=%s, falling back to direct flush: %s",
            message_id,
            exc,
        )
        _flush_controllers(adapter).pop(str(message_id), None)
        return await _flush_body()


async def _patch_interactive_state(adapter: Any, message_id: str, state: dict[str, Any]) -> bool:
    card = _render_message_card(state)
    patch_auth_card = getattr(adapter, "_patch_auth_card", None)
    if callable(patch_auth_card):
        result = patch_auth_card(message_id, card)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)
    if _can_patch_interactive_message(adapter):
        return await _patch_interactive_message(adapter, message_id, card)
    if _can_update_interactive_message(adapter):
        return await _update_interactive_message(adapter, message_id, card)
    return False


async def _deliver_final_card(
    adapter: Any,
    message_id: str,
    state: dict[str, Any],
    *,
    pop: bool,
) -> Any:
    card_id = str(state.get("card_id") or "")
    original_card_id = str(state.get("original_card_id") or "")

    if card_id:
        try:
            await _stream_cardkit_content(
                adapter,
                card_id,
                _render_stream_text(state),
                _next_sequence(state),
            )
        except Exception as exc:
            exc = await _recover_closed_stream(
                adapter,
                state,
                card_id,
                exc,
                lambda sequence: _stream_cardkit_content(
                    adapter,
                    card_id,
                    _render_stream_text(state),
                    sequence,
                ),
            )
            if exc is None:
                pass
            elif isinstance(exc, UnavailableError):
                UnavailableGuard.mark_unavailable(adapter, str(message_id), exc.code)
            elif isinstance(exc, RateLimitError):
                logger.debug("multitenancy: Feishu CardKit rate limited (230020) during final stream, skipping frame")
            else:
                logger.warning(
                    "multitenancy: Feishu CardKit final stream failed, falling back to full-card delivery: %s",
                    exc,
                )
                state["card_id"] = None

    if original_card_id:
        try:
            await _set_card_streaming_mode(adapter, original_card_id, False, _next_sequence(state))
        except Exception as exc:
            if isinstance(exc, UnavailableError):
                UnavailableGuard.mark_unavailable(adapter, str(message_id), exc.code)
            logger.warning(
                "multitenancy: Feishu CardKit failed to close streaming mode, falling back to IM delivery: %s",
                exc,
            )
        else:
            try:
                await _update_cardkit_card(
                    adapter,
                    original_card_id,
                    _to_cardkit2(_render_message_card(state)),
                    _next_sequence(state),
                )
            except Exception as exc:
                if isinstance(exc, UnavailableError):
                    UnavailableGuard.mark_unavailable(adapter, str(message_id), exc.code)
                logger.warning(
                    "multitenancy: Feishu CardKit final card update failed, falling back to IM delivery: %s",
                    exc,
                )
            else:
                if not state.get("aborted") and not state.get("errored"):
                    _transition(state, PHASE_COMPLETED)
                if pop:
                    _mark_terminal_message(adapter, message_id)
                    _states(adapter).pop(str(message_id), None)
                return _result(True, message_id=str(message_id))

    patch_success = await _patch_interactive_state(adapter, str(message_id), state)
    if patch_success:
        if not state.get("aborted") and not state.get("errored"):
            _transition(state, PHASE_COMPLETED)
        if pop:
            _mark_terminal_message(adapter, message_id)
            _states(adapter).pop(str(message_id), None)
        return _result(True, message_id=str(message_id))

    text_success = await _send_plaintext_last_resort(adapter, state)
    if text_success:
        if pop:
            _mark_terminal_message(adapter, message_id)
            _states(adapter).pop(str(message_id), None)
        return _result(True, message_id=str(message_id))
    return _result(False, message_id=str(message_id), error="final delivery failed")


async def _send_plaintext_last_resort(adapter: Any, state: dict[str, Any]) -> bool:
    chat_id = str(state.get("chat_id") or "")
    if not chat_id:
        return False
    content = _render_plaintext_last_resort(state)
    reply_to = state.get("reply_to")
    sender = getattr(adapter, "send", None)
    if callable(sender):
        response = await sender(chat_id, content, reply_to=reply_to, metadata=None)
        return bool(getattr(response, "success", False))
    retry_sender = getattr(adapter, "_feishu_send_with_retry", None)
    if callable(retry_sender):
        response = await retry_sender(
            chat_id=chat_id,
            msg_type="text",
            payload=content,
            reply_to=reply_to,
            metadata=None,
        )
        return bool(getattr(_finalize(adapter, response, "text send failed"), "success", False))
    return False


def _render_plaintext_last_resort(state: dict[str, Any]) -> str:
    content = _optimize_markdown_style(str(state.get("content") or ""), card_version=2).strip()
    if content:
        return content
    return _plain_summary(str(state.get("status") or "") or "Hermes")


def _flush_controllers(adapter: Any) -> dict[str, FlushController]:
    controllers = getattr(adapter, _FLUSH_CONTROLLERS_ATTR, None)
    if not isinstance(controllers, dict):
        controllers = {}
        setattr(adapter, _FLUSH_CONTROLLERS_ATTR, controllers)
    return controllers
