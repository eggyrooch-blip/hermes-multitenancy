"""Stable post-admission owner for one routed Feishu run."""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, Optional

from .. import router as _m
from .. import turn_tool_context
from ..run_broker import AdmittedRun, RunBroker, RunRejected
from ..run_models import RunResult
from ..trusted_feishu_ingress import TrustedFeishuAdmission
from .feishu_completion import begin_deferred_completion
from .vision_admission import vision_block_reply


_FEISHU_COMPLETION_TIMEOUT_SECONDS = 5.0
# Unit separator — same delimiter ``session_scope`` uses; cannot appear in a
# chat_id or an open_id.
_CARRY_SEP = "\x1f"


def _carry_admission(event: Any, profile_name: str) -> Optional[TrustedFeishuAdmission]:
    """The sealed DM admission this run may carry tool context under, else None.

    Same boundary the credential materializer enforces
    (``agent_real/_core.py`` ``_resolve_runtime_credentials``): only a sealed,
    employee-actor, ``feishu:user``-scoped admission whose profile agrees with
    the routed one. Group runs execute under ``feishu:bot`` and therefore bind
    nothing — no carry, no capture.
    """
    admission = getattr(event, "trusted_feishu_ingress_admission", None)
    if (
        isinstance(admission, TrustedFeishuAdmission)
        and admission.is_authentic()
        and getattr(admission, "actor_kind", "user") == "user"
        and getattr(admission, "tool_scope", "") == "feishu:user"
        and getattr(admission, "profile_name", "") == profile_name
    ):
        return admission
    return None


def _carry_session_id(hist_key: tuple, admission: Any) -> str:
    """Session dimension of the carryover key: history key + chat.

    With STRICT_CONTEXT off ``SessionScope.history_key`` is only
    ``(profile, user_key)``, so the chat_id suffix is the load-bearing part
    that keeps a DM's carried tool output from reaching a group turn.
    """
    return f"{hist_key[1]}{_CARRY_SEP}{getattr(admission, 'chat_id', '') or ''}"


async def _complete_feishu_processing(adapter: Any, event: Any, *, failed: bool) -> None:
    """Close the gateway-owned Feishu lifecycle exactly once for this run."""
    async def _complete() -> None:
        # No await is allowed between this snapshot and entering the adapter
        # coroutine: its deferred-id discard therefore covers exactly these
        # registered hook generations, not a later redelivery.
        begin_deferred_completion(adapter, event)
        outcome = _m._processing_outcome(failed=failed)
        complete_deferred = getattr(
            adapter,
            "complete_deferred_processing",
            None,
        )
        if callable(complete_deferred):
            await complete_deferred(event, outcome)
        else:
            await adapter.on_processing_complete(event, outcome)

    try:
        async with asyncio.timeout(_FEISHU_COMPLETION_TIMEOUT_SECONDS):
            await _complete()
    except asyncio.TimeoutError:
        _m.logger.warning(
            "multitenancy: Feishu processing completion timed out message_id=%s",
            getattr(event, "message_id", "") or "",
        )
    except Exception as exc:
        _m.logger.debug(
            "multitenancy: on_processing_complete failed: %s",
            exc,
        )


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
    completion_failed: Optional[asyncio.Event] = None,
) -> RunResult:
    """Own setup, dispatch, persistence, and cleanup after durable admission."""
    enriched_text = admitted_run.request.content
    vision_blocked = vision_block_reply(admitted_run)
    if vision_blocked:
        try:
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
        except asyncio.CancelledError:
            raise
        except Exception:
            if completion_failed is not None:
                completion_failed.set()
            raise

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
    # Bind BEFORE the event is cloned: ``_event_with_text`` /
    # ``_event_with_run_metadata`` are ``copy.copy`` shallow copies, so the
    # clones the child streams through share this one RunCarry object and
    # mark_done/record_transcript land on the instance ``commit_turn`` reads.
    carry_admission = _carry_admission(event, profile_name)
    if carry_admission is not None:
        turn_tool_context.bind(
            event,
            channel="feishu",
            profile_name=profile_name,
            user_key=carry_admission.actor_subject,
            session_id=_carry_session_id(hist_key, carry_admission),
            user_text=user_msg["content"],
            # The FULL conversation, exactly what WebUI hands it
            # (``periphery`` passes its own ``messages``): ``align`` drops
            # ``history[:-1]`` as the in-flight user message. Passing ``prior``
            # would eat the last PRIOR row instead — invisible when it is an
            # assistant reply, fatal after a media-only turn that persisted
            # none, whose user row would never count as answered.
            messages=conversation,
        )
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
        # Outside the ``response_text`` guard on purpose: a media-only or empty
        # answer still ran tools, and every failure path (cancel / RunRejected /
        # exception / concurrent replacement) returns before this line. The
        # terminal-done gate lives inside commit_turn, which logs its verdict.
        turn_tool_context.commit_turn(event)
        _m._touch_route(sender, sender_alt)
        return run_result
    except asyncio.CancelledError:
        if current not in _m._suppress_interruption_marker_tasks:
            _m._persist_interruption_marker(hist_key)
        raise
    except RunRejected as exc:
        if completion_failed is not None:
            completion_failed.set()
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
        if completion_failed is not None:
            completion_failed.set()
        _m._persist_failure_marker(hist_key)
        _m.logger.exception(
            "multitenancy: admitted Feishu execution failed profile=%s sender=%s: %s",
            profile_name,
            sender,
            exc,
        )
        return RunResult(content="", duplicate=False)
    finally:
        if _m._user_inflight_tasks.get(inflight_key) is current:
            _m._user_inflight_tasks.pop(inflight_key, None)
            _m._user_inflight_history_keys.pop(inflight_key, None)
        if current is not None:
            _m._suppress_interruption_marker_tasks.discard(current)
