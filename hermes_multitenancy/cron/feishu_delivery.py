"""Live Feishu adapter cron delivery (direct + streaming).

Split out of the historical ``cron_worker`` god-node. Pure move + re-export:
``cron_worker`` re-exports every symbol so import paths and ``monkeypatch.setattr(
cron_worker, ...)`` targets are unchanged. Cross-module and monkeypatched helpers
are reached through the ``cron_worker`` shim (``_cw``) so patches applied to
``cron_worker.<name>`` are honoured by callers that live in a sibling module.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
from concurrent.futures import TimeoutError as FuturesTimeout
import functools
import json
import math
import signal
import logging
import os
import re
import subprocess
import sqlite3
import sys
import threading
import time
import uuid
import copy
from urllib import error as urllib_error
from urllib import request as urllib_request
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None

from .. import cron_worker as _cw
from ..feishu_adapter_compat import load_feishu_adapter, log_feishu_adapter_load_error

# Keep the historical logger name so log records are attributed to
# ``hermes_multitenancy.cron_worker`` exactly as before the split.
logger = logging.getLogger("hermes_multitenancy.cron_worker")



def _is_feishu_platform_config_error(error: object) -> bool:
    text = str(error or "").lower()
    return "platform 'feishu' not configured/enabled" in text


def _deliver_cron_feishu_via_live_adapter(
    scheduler: Any,
    job: dict,
    content: str,
    *,
    adapters: Any = None,
    loop: Any = None,
    require_receipt: bool = False,
    targets_override: Optional[list[dict]] = None,
) -> Optional[str]:
    """Fallback for multitenancy gateways where Feishu exists only as a live adapter."""
    if adapters is None or loop is None or not getattr(loop, "is_running", lambda: False)():
        return "feishu live adapter unavailable"
    if targets_override is None:
        try:
            targets = scheduler._resolve_delivery_targets(job)
        except Exception as exc:
            return f"failed to resolve feishu delivery target: {exc}"
    else:
        targets = targets_override

    feishu_targets = [
        target for target in targets
        if str(target.get("platform") or "").strip().lower() == "feishu"
    ]
    if not feishu_targets:
        return "no feishu delivery target resolved"

    adapter = _cw._adapter_for_platform(adapters, "feishu")
    if adapter is None:
        return "feishu live adapter unavailable"

    text_to_send, media_files = _cw._cron_delivery_payload_for_adapter(job, content)
    if require_receipt and not text_to_send and not media_files:
        return "feishu live adapter delivery has no payload"

    # Cron deliveries historically go out as plain text via ``adapter.send``,
    # which flattens markdown (bullets/bold/links) — unlike normal replies that
    # stream as interactive CardKit cards. When enabled, render a simple
    # interactive card so scheduled-task output looks like a normal reply.
    card: Optional[dict] = None
    if (
        text_to_send
        and _cw._cron_card_response_enabled()
        and callable(getattr(adapter, "_feishu_send_with_retry", None))
    ):
        try:
            card, card_media = _cw._build_cron_card(job, content)
            if card is not None:
                # Card and text paths extract media from the same content.
                media_files = card_media
        except Exception:
            logger.warning("[multitenancy] cron card build failed; using text", exc_info=True)
            card = None

    errors: list[str] = []
    for target in feishu_targets:
        chat_id = str(target.get("chat_id") or "").strip()
        if not chat_id:
            errors.append("missing feishu chat_id")
            continue
        thread_id = target.get("thread_id")
        metadata = _cw._feishu_delivery_metadata(chat_id, thread_id)
        try:
            sent_payload = False
            if card is not None:
                card_error = _cw._send_cron_card_via_live_adapter(
                    adapter, chat_id, card, metadata, loop,
                    require_receipt=require_receipt,
                )
                if card_error is None:
                    sent_payload = True
                    logger.info(
                        "[multitenancy] cron delivered card to Feishu job=%s",
                        job.get("id", "?"),
                    )
                else:
                    if require_receipt:
                        errors.append(card_error)
                        continue
                    logger.warning(
                        "[multitenancy] cron card send failed for %s (%s); "
                        "falling back to plain text",
                        chat_id,
                        card_error,
                    )
            if text_to_send and not sent_payload:
                future, schedule_error = _cw._schedule_on_gateway_loop(
                    adapter.send(chat_id, text_to_send, metadata=metadata),
                    loop,
                )
                if schedule_error:
                    errors.append(schedule_error)
                    continue
                if future is None:
                    errors.append(f"feishu live adapter loop unavailable for {chat_id}")
                    continue
                try:
                    result = future.result(timeout=15)
                except FuturesTimeout:
                    future.cancel()
                    raise
                if require_receipt and (not result or not getattr(result, "success", False)):
                    errors.append(
                        f"feishu live adapter send failed for {chat_id}: "
                        f"{getattr(result, 'error', 'unknown')}"
                    )
                    continue
                if not require_receipt and result and not getattr(result, "success", True):
                    errors.append(
                        f"feishu live adapter send failed for {chat_id}: "
                        f"{getattr(result, 'error', 'unknown')}"
                    )
                    continue
                if require_receipt and not str(getattr(result, "message_id", "") or "").strip():
                    errors.append("feishu live adapter send missing message_id")
                    continue
            if media_files:
                media_error = _cw._send_media_files_via_live_adapter(
                    adapter,
                    chat_id,
                    media_files,
                    metadata,
                    loop,
                    job,
                )
                if media_error:
                    errors.append(media_error)
                    continue
            logger.info(
                "[multitenancy] cron delivered to Feishu via live adapter job=%s",
                job.get("id", "?"),
            )
        except Exception as exc:
            errors.append(f"feishu live adapter delivery to {chat_id} failed: {exc}")
    return "; ".join(errors) if errors else None


def _schedule_on_gateway_loop(coro: Any, loop: Any) -> tuple[Any, Optional[str]]:
    try:
        from agent.async_utils import safe_schedule_threadsafe
    except Exception as exc:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return None, f"safe_schedule_threadsafe unavailable: {exc}"
    try:
        return safe_schedule_threadsafe(coro, loop), None
    except Exception as exc:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return None, f"failed to schedule Feishu live adapter send: {exc}"


def _feishu_delivery_metadata(chat_id: str, thread_id: Any) -> Optional[dict]:
    if not thread_id:
        return None
    # Feishu DMs (`ou_...`) should not receive topic metadata; stale thread_id
    # can create visible topic replies in one-to-one chats.
    if str(chat_id).startswith("ou_"):
        return None
    return {"thread_id": thread_id}


def _adapter_for_platform(adapters: Any, platform_name: str) -> Any:
    if not adapters:
        return None
    target = platform_name.lower()
    try:
        from gateway.config import Platform

        platform = Platform(target)
        adapter = adapters.get(platform)
        if adapter is not None:
            return adapter
    except Exception:
        pass
    for key, adapter in adapters.items():
        candidates = {
            str(key).lower(),
            str(getattr(key, "value", "")).lower(),
            str(getattr(key, "name", "")).lower(),
        }
        if target in candidates or any(candidate.endswith(f".{target}") for candidate in candidates):
            return adapter
    return None


def _send_cron_card_via_live_adapter(
    adapter: Any,
    chat_id: str,
    card: dict[str, Any],
    metadata: Optional[dict],
    loop: Any,
    *,
    require_receipt: bool = False,
) -> Optional[str]:
    """Send one interactive card on the gateway loop. Returns error str or None.

    This MUST NEVER raise: any failure (serialize, synchronous adapter raise,
    scheduling, timeout, coroutine exception, non-success response) is returned
    as an error string so the caller can fall back to the plain-text path. An
    escaped exception here would skip delivery bookkeeping and falsely mark the
    cron run as delivered.
    """
    try:
        payload = json.dumps(card, ensure_ascii=False)
    except Exception as exc:
        return f"card serialize failed: {exc}"

    try:
        # ``_feishu_send_with_retry`` may raise synchronously while building the
        # coroutine; keep it inside the guard.
        coro = adapter._feishu_send_with_retry(
            chat_id=chat_id,
            msg_type="interactive",
            payload=payload,
            reply_to=None,
            metadata=metadata,
        )
        future, schedule_error = _cw._schedule_on_gateway_loop(coro, loop)
        if schedule_error:
            return schedule_error
        if future is None:
            return f"feishu live adapter loop unavailable for {chat_id}"
        try:
            response = future.result(timeout=15)
        except FuturesTimeout:
            future.cancel()
            return f"feishu card send timed out for {chat_id}"
        # Normalize the raw adapter response the same way the streaming-card
        # paths do; a bare response has no ``.success`` attribute, so the naive
        # ``getattr(result, "success", True)`` would treat a failed send (code
        # != 0, no message_id) as success and skip the text fallback. If
        # ``_finalize`` is unavailable or raises, the outer ``except`` below
        # returns an error string so the caller still falls back to text.
        from ..card.card_error import _finalize

        result = _finalize(adapter, response, "cron card send failed")
        if require_receipt and (result is None or not getattr(result, "success", False)):
            return (
                f"feishu card send failed for {chat_id}: "
                f"{getattr(result, 'error', 'unknown')}"
            )
        if not require_receipt and result is not None and not getattr(result, "success", True):
            return (
                f"feishu card send failed for {chat_id}: "
                f"{getattr(result, 'error', 'unknown')}"
            )
        if require_receipt and not str(getattr(result, "message_id", "") or "").strip():
            return "feishu card send missing message_id"
        return None
    except Exception as exc:
        return f"feishu card send raised for {chat_id}: {exc}"


def _send_media_files_via_live_adapter(
    adapter: Any,
    chat_id: str,
    media_files: list,
    metadata: Optional[dict],
    loop: Any,
    job: dict,
) -> Optional[str]:
    try:
        import cron.scheduler as scheduler
        from gateway.config import Platform

        scheduler._send_media_via_adapter(
            adapter,
            chat_id,
            media_files,
            metadata,
            loop,
            job,
            platform=Platform("feishu"),
        )
        return None
    except Exception:
        logger.warning("[multitenancy] cron media delivery via live Feishu adapter failed", exc_info=True)
        return f"cron media delivery via live Feishu adapter failed job={job.get('id', '?')}"


def _mirror_cron_delivery_to_owner(job: dict, content: str) -> None:
    owner_open_id = str(job.get("owner_open_id") or "").strip()
    if not owner_open_id.startswith("ou_"):
        return

    owner_profile = str(job.get("owner_profile") or "").strip()
    if not owner_profile:
        try:
            from .. import router

            resolved_profile, profile_home = router._resolve_route(owner_open_id)
            if profile_home is not None:
                owner_profile = resolved_profile
        except Exception:
            logger.debug("[multitenancy] cron mirror route lookup failed", exc_info=True)
    if not owner_profile:
        return

    job_name = str(job.get("name") or job.get("id") or "scheduled task")
    job_id = str(job.get("id") or "")
    mirrored_content = (
        f"[Scheduled task delivery]\n"
        f"Task: {job_name}\n"
        f"Job ID: {job_id}\n\n"
        f"{content}"
    )

    try:
        from .. import router

        # Use the typed SessionScope construction point (channel="cron") rather
        # than a raw (profile, open_id) tuple, so cron history goes through the
        # single key authority. Default mode → byte-identical (owner_profile,
        # owner_open_id); strict mode → an isolated cron-channel key so cron
        # deliveries don't cross into the owner's DM/webui sessions.
        key = router._history_key(owner_profile, owner_open_id, None, channel="cron")
        existing = router._session_history.get(key, [])
        router._session_history[key] = router._trim_history(
            existing + [{"role": "assistant", "content": mirrored_content}]
        )
        store = router._get_session_store()
        if store is not None:
            store.append(key[0], key[1], "assistant", mirrored_content)
        logger.info(
            "[multitenancy] mirrored cron delivery to profile session profile=%s job=%s",
            owner_profile,
            job_id,
        )
    except Exception:
        logger.exception("[multitenancy] failed to mirror cron delivery to owner session")


def _stream_cron_card_on_loop(
    adapter: Any,
    chat_id: str,
    body: str,
    metadata: Optional[dict],
    loop: Any,
) -> Optional[str]:
    """Drive start -> stream -> finalize of a streaming card. Returns err or None.

    Reuses the same CardKit streaming surface a normal reply uses, so cron output
    streams in with the print effect and finalizes with the Done footer. Never
    raises: any failure is returned as a string so the caller falls back to core
    plain-text delivery (a delivery is never dropped).
    """
    try:
        future, sched_err = _cw._schedule_on_gateway_loop(
            adapter.start_streaming_card(chat_id=chat_id, metadata=metadata), loop
        )
        if sched_err:
            return sched_err
        if future is None:
            return f"streaming loop unavailable for {chat_id}"
        try:
            start_res = future.result(timeout=15)
        except FuturesTimeout:
            future.cancel()
            return f"streaming card start timed out for {chat_id}"
        message_id = getattr(start_res, "message_id", None)
        if not getattr(start_res, "success", False) or not message_id:
            return f"streaming card start failed: {getattr(start_res, 'error', 'unknown')}"

        # First push streams the content (print effect), second finalizes (footer).
        # Non-final failure => the body was never shown => return an error so the
        # caller falls back to plain text. Final-only failure => the body is
        # already on screen => tolerate (return None) to avoid a double-send.
        for finalize in (False, True):
            future, sched_err = _cw._schedule_on_gateway_loop(
                adapter.update_streaming_card(
                    chat_id=chat_id,
                    message_id=message_id,
                    content=body,
                    finalize=finalize,
                ),
                loop,
            )
            if sched_err:
                return None if finalize else sched_err
            if future is None:
                return None if finalize else f"streaming loop unavailable for {chat_id}"
            try:
                res = future.result(timeout=15)
            except Exception:
                future.cancel()
                return None if finalize else f"streaming card update failed for {chat_id}"
            if res is not None and not getattr(res, "success", True):
                if finalize:
                    return None
                return (
                    f"streaming card update failed for {chat_id}: "
                    f"{getattr(res, 'error', 'unknown')}"
                )
        return None
    except Exception as exc:
        return f"streaming card raised for {chat_id}: {exc}"


def _try_deliver_cron_feishu_streaming_card(
    scheduler: Any,
    job: dict,
    content: str,
    *,
    adapters: Any = None,
    loop: Any = None,
) -> Optional[bool]:
    """Deliver an all-Feishu cron job as a streaming card.

    Returns ``True`` when every target was delivered as a streaming card, or
    ``None`` when the streaming path is not applicable / failed (caller then
    falls through to core plain-text delivery). Only handles the case where every
    resolved target is Feishu, a streaming-capable live adapter exists, the loop
    is running, and the content carries no media attachments.
    """
    if adapters is None or loop is None or not getattr(loop, "is_running", lambda: False)():
        return None
    try:
        targets = scheduler._resolve_delivery_targets(job)
    except Exception:
        return None
    if not targets:
        return None
    if any(str(t.get("platform") or "").strip().lower() != "feishu" for t in targets):
        return None
    # Single target only: a partial failure across multiple targets would make us
    # return None and let core re-deliver ALL targets, double-sending the one we
    # already streamed. Multi-target cron is rare — let core handle it as text.
    if len(targets) != 1:
        return None

    adapter = _cw._adapter_for_platform(adapters, "feishu")
    if adapter is not None:
        # The CardKit streaming surface is installed lazily on the gateway's
        # Feishu adapter when it handles a normal reply. The cron adapter
        # instance may not have it yet (no inbound message triggered install),
        # so install it on demand — the same surface normal replies use.
        try:
            from ..card import ensure_feishu_cardkit_streaming

            ensure_feishu_cardkit_streaming(adapter)
        except Exception:
            logger.warning(
                "[multitenancy] ensure_feishu_cardkit_streaming failed", exc_info=True
            )
    if adapter is None or not callable(getattr(adapter, "start_streaming_card", None)):
        logger.debug(
            "[multitenancy] cron stream: adapter=%s lacks start_streaming_card=%s",
            type(adapter).__name__ if adapter is not None else None,
            callable(getattr(adapter, "start_streaming_card", None)),
        )
        return None
    supports = getattr(adapter, "supports_streaming_card", None)
    if not callable(supports) or not supports():
        logger.debug(
            "[multitenancy] cron stream: supports callable=%s value=%s",
            callable(supports),
            (supports() if callable(supports) else None),
        )
        return None

    # Media deliveries are handled by core's native path (cards don't carry files).
    try:
        from gateway.platforms.base import BasePlatformAdapter

        media_files, cleaned = BasePlatformAdapter.extract_media(content)
        media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
    except Exception:
        # Fail closed: if we can't tell whether there's media, let core deliver
        # (it handles attachments) rather than risk streaming text and dropping files.
        return None
    if media_files:
        return None
    if not cleaned.strip():
        return None

    body = _cw._build_cron_card_body(job, cleaned)
    for target in targets:
        chat_id = str(target.get("chat_id") or "").strip()
        if not chat_id:
            return None
        metadata = _cw._feishu_delivery_metadata(chat_id, target.get("thread_id"))
        err = _cw._stream_cron_card_on_loop(adapter, chat_id, body, metadata, loop)
        if err is not None:
            logger.warning(
                "[multitenancy] cron streaming card failed for %s job=%s: %s",
                chat_id,
                job.get("id", "?"),
                err,
            )
            return None
        logger.info(
            "[multitenancy] cron delivered streaming card to feishu:%s job=%s",
            chat_id,
            job.get("id", "?"),
        )
    return True


def _feishu_response_message_id(response: Any) -> Optional[str]:
    data = getattr(response, "data", None)
    for source in (data, response):
        if source is None:
            continue
        value = getattr(source, "message_id", None)
        if value:
            return str(value)
        if isinstance(source, dict):
            value = source.get("message_id") or source.get("message", {}).get("message_id")
            if value:
                return str(value)
    return None
