"""Stable post-admission owner for one routed Feishu run."""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, Optional

from .. import router as _m
from ..run_broker import AdmittedRun, RunBroker, RunRejected
from ..run_models import RunResult
from .vision_admission import vision_block_reply


async def execute_admitted_feishu_run(
    admitted_run: AdmittedRun,
    *,
    run_broker: RunBroker,
    event: Any,
    gateway: Any,
    adapter: Any,
    chat_id: str,
    profile_name: str,
    profile_home: Path,
    sender: str,
    sender_alt: Optional[str],
    text: str,
    feishu_full: bool,
) -> RunResult:
    """Own setup, dispatch, persistence, and cleanup after durable admission."""
    enriched_text = admitted_run.request.content
    vision_blocked = vision_block_reply(admitted_run)
    if vision_blocked:
        hist_key = _m._dispatch_session_scope(
            profile_name,
            sender,
            sender_alt,
            chat_id,
            event,
        ).history_key
        user_msg = _m._build_user_message(event, text_override=enriched_text)
        _m._persist_turn(hist_key, user_msg, vision_blocked)
        return await run_broker._run_admitted(
            admitted_run,
            dispatch_agent=lambda _request: "",
        )

    current = asyncio.current_task()
    scope = _m._dispatch_session_scope(
        profile_name,
        sender,
        sender_alt,
        chat_id,
        event,
    )
    inflight_key = scope.inflight_key
    prev = _m._user_inflight_tasks.get(inflight_key)
    if prev is not None and not prev.done() and prev is not current:
        prev_hist_key = _m._user_inflight_history_keys.get(inflight_key)
        if prev_hist_key is not None:
            _m._persist_interruption_marker(prev_hist_key)
        _m._suppress_interruption_marker_tasks.add(prev)
        prev.cancel()
    if current is not None:
        _m._user_inflight_tasks[inflight_key] = current

    outcome_failed = False
    if feishu_full:
        try:
            await adapter.on_processing_start(event)
        except Exception as exc:
            _m.logger.debug("multitenancy: on_processing_start failed: %s", exc)

    hist_key = scope.history_key
    prior = _m._load_history(hist_key)
    contextual_text = _m._append_recent_profile_file_context(
        enriched_text or text,
        profile_name=profile_name,
        chat_id=chat_id,
        profile_home=profile_home,
        prior_messages=prior,
    )
    user_msg = _m._build_user_message(event, text_override=contextual_text)
    conversation = prior + [user_msg]
    _m._persist_user_message(hist_key, user_msg)
    if current is not None and _m._user_inflight_tasks.get(inflight_key) is current:
        _m._user_inflight_history_keys[inflight_key] = hist_key
    agent_event = _m._event_with_text(event, user_msg["content"])

    try:
        if feishu_full:
            async def _dispatch_streaming(request):
                run_event = _m._event_with_run_metadata(
                    agent_event,
                    request.metadata,
                )
                stream_kwargs = {"messages": conversation}
                try:
                    if "gateway" in inspect.signature(_m._stream_into_feishu).parameters:
                        stream_kwargs["gateway"] = gateway
                except (TypeError, ValueError):
                    stream_kwargs["gateway"] = gateway
                stream_response = await _m._stream_into_feishu(
                    adapter,
                    chat_id,
                    profile_name,
                    profile_home,
                    run_event,
                    **stream_kwargs,
                )
                if stream_response:
                    await _m._deliver_media_from_stream_response(
                        gateway,
                        stream_response,
                        run_event,
                        adapter,
                        profile_home,
                    )
                return stream_response

            run_result = await run_broker._run_admitted(
                admitted_run,
                dispatch_agent=_dispatch_streaming,
            )
            response_text = run_result.content
        else:
            if adapter is not None:
                await _m._safe_call(adapter.send_typing, chat_id)

            async def _dispatch_nonstream(request):
                run_event = _m._event_with_run_metadata(
                    agent_event,
                    request.metadata,
                )
                return await _m._get_pool().dispatch(
                    profile_name,
                    profile_home,
                    run_event,
                )

            run_result = await run_broker._run_admitted(
                admitted_run,
                dispatch_agent=_dispatch_nonstream,
            )
            response_text = run_result.content
            if adapter is not None:
                await _m._safe_call(adapter.send, chat_id, response_text)

        if response_text and isinstance(response_text, str):
            _m._persist_assistant_message(hist_key, response_text)
        _m._touch_route(sender, sender_alt)
        return run_result
    except asyncio.CancelledError:
        if current not in _m._suppress_interruption_marker_tasks:
            _m._persist_interruption_marker(hist_key)
        raise
    except RunRejected as exc:
        outcome_failed = True
        retry_message = "当前无法确认员工计费身份，请稍后重试。"
        _m._persist_failure_marker(hist_key)
        _m._persist_assistant_message(hist_key, retry_message)
        _m.logger.warning(
            "multitenancy: prepared routed run rejected profile=%s sender=%s: %s",
            profile_name,
            sender,
            exc,
        )
        if adapter is not None:
            await _m._safe_call(adapter.send, chat_id, retry_message)
        return RunResult(content=retry_message, duplicate=False)
    except Exception as exc:
        outcome_failed = True
        _m._persist_failure_marker(hist_key)
        _m.logger.exception(
            "multitenancy: admitted Feishu execution failed profile=%s sender=%s: %s",
            profile_name,
            sender,
            exc,
        )
        return RunResult(content="", duplicate=False)
    finally:
        if feishu_full:
            try:
                out = _m._processing_outcome(failed=outcome_failed)
                complete_deferred = getattr(
                    adapter,
                    "complete_deferred_processing",
                    None,
                )
                if callable(complete_deferred):
                    await complete_deferred(event, out)
                else:
                    await adapter.on_processing_complete(event, out)
            except Exception as exc:
                _m.logger.debug(
                    "multitenancy: on_processing_complete failed: %s",
                    exc,
                )
        if _m._user_inflight_tasks.get(inflight_key) is current:
            _m._user_inflight_tasks.pop(inflight_key, None)
            _m._user_inflight_history_keys.pop(inflight_key, None)
        if current is not None:
            _m._suppress_interruption_marker_tasks.discard(current)
