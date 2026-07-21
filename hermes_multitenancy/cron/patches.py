"""Cron worker lifecycle + monkeypatch installers.

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
from ..feishu_inbound_richtext import install_feishu_inbound_richtext_patch
from ..feishu_adapter_compat import load_feishu_adapter, log_feishu_adapter_load_error
from .. import gateway_ownership

# Keep the historical logger name so log records are attributed to
# ``hermes_multitenancy.cron_worker`` exactly as before the split.
logger = logging.getLogger("hermes_multitenancy.cron_worker")



_worker_lock = threading.Lock()
_worker_started = False
_worker_thread: Optional[threading.Thread] = None
_worker_stop: Optional[threading.Event] = None
# NB: _runtime_patches_installed is owned by the cron_worker shim (its sole
# reader/writer is install_cron_runtime_patches there), so it is defined in the
# shim — not here — to keep single-owner semantics for the reassigned flag.
_gateway_watcher_installed = False
_watcher_attr = "_hermes_multitenancy_cron_watch_scheduled"


def ensure_cron_worker_started(gateway: Any) -> None:
    """Start the multi-profile cron worker once, on first hook dispatch.

    No-op when gateway adapters are not yet ready, when no asyncio loop is
    running, or when HERMES_HOME does not look like a multitenancy nested
    layout (``<root>/profiles/<name>``).
    """
    global _worker_started, _worker_thread, _worker_stop
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        adapters = getattr(gateway, "adapters", None)
        if not adapters:
            logger.info("[multitenancy] cron worker delayed: adapters not ready")
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.info("[multitenancy] cron worker delayed: no running loop")
            return
        profiles_root = _cw._resolve_profiles_root()
        if profiles_root is None:
            return
        _worker_stop = threading.Event()
        _worker_thread = threading.Thread(
            target=_cw._multiprofile_cron_worker,
            args=(profiles_root, adapters, loop, _worker_stop),
            daemon=True,
            name="multitenancy-cron-worker",
        )
        _worker_thread.start()
        _worker_started = True
        logger.info(
            "[multitenancy] multi-profile cron worker started (scanning %s)",
            profiles_root,
        )


def install_profile_native_cron_guard() -> None:
    """Prevent non-router profile gateways from executing profile cron jobs.

    The router-owned multi-profile worker scans all profile cron stores with
    live Feishu adapters and RunBroker credentials. A non-router profile gateway
    may still import upstream ``cron.scheduler``; if it ticks the same profile
    store it can bypass RunBroker and then fail delivery. In multitenancy
    profile layouts, make that native tick yield to the router unless explicitly
    re-enabled for emergency diagnostics.
    """
    try:
        import cron.scheduler as scheduler
    except Exception:
        logger.exception("[multitenancy] failed to patch profile native cron guard")
        return

    original = getattr(scheduler, "tick", None)
    if original is None or getattr(original, "_hermes_multitenancy_profile_guard", False):
        return

    @functools.wraps(original)
    def tick(*args: Any, **kwargs: Any) -> int:
        profile_home = _cw._current_profile_home()
        if profile_home.parent.name == "profiles" and profile_home.name != "multitenancy_router":
            if not _cw._profile_native_cron_enabled():
                logger.info(
                    "[multitenancy] profile native cron tick skipped for %s; router worker owns cron execution",
                    profile_home.name,
                )
                return 0
            logger.warning(
                "[multitenancy] profile native cron tick escape enabled for %s; "
                "this bypasses router-owned RunBroker cron execution",
                profile_home.name,
            )
        return original(*args, **kwargs)

    setattr(tick, "_hermes_multitenancy_profile_guard", True)
    setattr(tick, "_hermes_multitenancy_original", original)
    scheduler.tick = tick
    logger.info("[multitenancy] patched profile native cron tick guard")


def install_gateway_startup_watcher() -> None:
    """Start the cron worker after GatewayRunner connects adapters.

    Hermes core has no generic "gateway started" plugin hook in the upstream
    code we run against. Rather than patching core files, the plugin wraps
    ``GatewayRunner._create_adapter`` early in startup and schedules a small
    async watcher on the same event loop. Once the gateway's adapter map is
    populated, the normal worker start path receives the live gateway object.
    """
    global _gateway_watcher_installed
    if _gateway_watcher_installed:
        return
    try:
        from gateway.run import GatewayRunner
    except Exception:
        logger.exception("[multitenancy] failed to install gateway cron startup watcher")
        return

    original = getattr(GatewayRunner, "_create_adapter", None)
    if original is None or getattr(original, "_hermes_multitenancy_patched", False):
        _gateway_watcher_installed = True
        return

    @functools.wraps(original)
    def wrapped_create_adapter(self: Any, *args: Any, **kwargs: Any) -> Any:
        adapter = original(self, *args, **kwargs)
        _cw._schedule_startup_watch(self)
        return adapter

    setattr(wrapped_create_adapter, "_hermes_multitenancy_patched", True)
    GatewayRunner._create_adapter = wrapped_create_adapter
    _gateway_watcher_installed = True
    logger.info("[multitenancy] installed gateway cron startup watcher")


def _schedule_startup_watch(gateway: Any) -> None:
    if gateway_ownership.is_router_profile_runtime():
        try:
            from ..webui_broker_server import ensure_run_broker_server_started

            ensure_run_broker_server_started()
        except Exception:
            logger.exception("[multitenancy] failed to schedule WebUI run broker sidecar")
    if getattr(gateway, _watcher_attr, False):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.info("[multitenancy] cron startup watcher delayed: no running loop")
        return
    setattr(gateway, _watcher_attr, True)
    task = loop.create_task(_cw._start_worker_when_adapters_ready(gateway))
    task.add_done_callback(_cw._log_startup_watch_failure)


async def _start_worker_when_adapters_ready(gateway: Any, attempts: int = 90) -> None:
    for _ in range(attempts):
        if getattr(gateway, "adapters", None):
            _cw.ensure_cron_worker_started(gateway)
            return
        await asyncio.sleep(1)
    logger.warning("[multitenancy] cron worker not started: gateway adapters stayed empty")


def _log_startup_watch_failure(task: "asyncio.Task[Any]") -> None:
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except Exception as err:
        logger.error("[multitenancy] cron startup watcher result unavailable: %s", err)
        return
    if exc is not None:
        logger.error(
            "[multitenancy] cron startup watcher failed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def _patch_scheduler_owner_open_id_delivery() -> None:
    try:
        import cron.scheduler as scheduler
    except Exception:
        logger.exception("[multitenancy] failed to patch cron owner delivery")
        return

    original = getattr(scheduler, "_resolve_single_delivery_target", None)
    if original is None or getattr(original, "_hermes_multitenancy_patched", False):
        return

    @functools.wraps(original)
    def resolve_single_delivery_target(job: dict, deliver_value: str) -> Optional[dict]:
        target = original(job, deliver_value)
        if target is not None:
            return target
        if str(deliver_value).strip().lower() != "feishu":
            return None
        owner_open_id = str(job.get("owner_open_id") or "").strip()
        if not owner_open_id.startswith("ou_"):
            return None
        return {
            "platform": "feishu",
            "chat_id": owner_open_id,
            "thread_id": None,
        }

    setattr(resolve_single_delivery_target, "_hermes_multitenancy_patched", True)
    scheduler._resolve_single_delivery_target = resolve_single_delivery_target
    logger.info("[multitenancy] patched cron delivery fallback to owner open_id")


def _cron_run_broker_enabled() -> bool:
    value = os.environ.get("HERMES_MULTITENANCY_CRON_RUN_BROKER", "").strip().lower()
    if not value:
        return True
    return value in {"1", "true", "yes", "on"}


def _profile_native_cron_enabled() -> bool:
    value = os.environ.get("HERMES_MULTITENANCY_PROFILE_NATIVE_CRON", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _patch_cron_run_broker() -> None:
    try:
        import cron.scheduler as scheduler
    except Exception:
        logger.exception("[multitenancy] failed to patch cron run broker")
        return

    original = getattr(scheduler, "run_job", None)
    if original is None or getattr(original, "_hermes_multitenancy_patched", False):
        return

    @functools.wraps(original)
    def run_job(job: dict, *args: Any, **kwargs: Any) -> tuple[bool, str, str, Optional[str]]:
        # Forward every argument the core scheduler passes (e.g. the parallel
        # pool's keyword-only `defer_agent_teardown` list). Hard-coding the
        # signature here silently breaks every cron tick the moment the core
        # run_job grows a parameter — see tests/test_cron_runjob_signature_passthrough.py.
        deferred = _cw._l4_check_needs_reauth_and_defer(job)
        if deferred is not None:
            return deferred
        if not _cw._cron_run_broker_enabled():
            return original(job, *args, **kwargs)
        return _cw._run_job_through_broker(job, scheduler)

    setattr(run_job, "_hermes_multitenancy_patched", True)
    setattr(run_job, "_hermes_multitenancy_original", original)
    scheduler.run_job = run_job
    logger.info("[multitenancy] patched cron run_job with opt-in RunBroker path")


def _patch_cron_delivery_mirror() -> None:
    try:
        import cron.scheduler as scheduler
    except Exception:
        logger.exception("[multitenancy] failed to patch cron delivery mirror")
        return

    original = getattr(scheduler, "_deliver_result", None)
    if original is None or getattr(original, "_hermes_multitenancy_patched", False):
        return

    @functools.wraps(original)
    def deliver_result(job: dict, content: str, adapters: Any = None, loop: Any = None) -> Optional[str]:
        # Deliver Feishu cron output as a STREAMING CardKit card (same UX as a
        # normal agent reply: streaming print + rendered markdown + Done footer)
        # before core falls back to flattened plain text. Only when every target
        # is Feishu, a streaming-capable live adapter is present, and there is no
        # media. Any failure returns None so we fall through to core delivery —
        # a cron delivery is never dropped.
        if _cw._cron_card_response_enabled():
            try:
                streamed = _cw._try_deliver_cron_feishu_streaming_card(
                    scheduler, job, content, adapters=adapters, loop=loop
                )
            except Exception:
                logger.warning(
                    "[multitenancy] cron streaming card path raised; using core delivery",
                    exc_info=True,
                )
                streamed = None
            if streamed is True:
                _cw._mirror_cron_delivery_to_owner(job, content)
                return None

        error = original(job, content, adapters=adapters, loop=loop)
        if error is not None and _cw._is_feishu_platform_config_error(error):
            live_error = _cw._deliver_cron_feishu_via_live_adapter(
                scheduler,
                job,
                content,
                adapters=adapters,
                loop=loop,
            )
            if live_error is None:
                _cw._mirror_cron_delivery_to_owner(job, content)
                return None
            logger.warning(
                "[multitenancy] cron live Feishu delivery fallback failed job=%s: %s",
                job.get("id", "?"),
                live_error,
            )
            return f"{error}; live Feishu fallback failed: {live_error}"
        if error is None:
            _cw._mirror_cron_delivery_to_owner(job, content)
        return error

    setattr(deliver_result, "_hermes_multitenancy_patched", True)
    scheduler._deliver_result = deliver_result
    logger.info("[multitenancy] patched cron delivery mirror to owner session")


def _patch_feishu_open_id_send() -> None:
    try:
        FeishuAdapter = load_feishu_adapter()
    except Exception as exc:
        log_feishu_adapter_load_error(
            logger,
            "[multitenancy] FeishuAdapter not importable yet; open_id delivery patch deferred",
            exc,
        )
        return

    original = getattr(FeishuAdapter, "_send_raw_message", None)
    if original is None or getattr(original, "_hermes_multitenancy_patched", False):
        return

    @functools.wraps(original)
    async def send_raw_message(
        self: Any,
        *,
        chat_id: str,
        msg_type: str,
        payload: str,
        reply_to: Optional[str],
        metadata: Optional[dict],
    ) -> Any:
        if reply_to or not str(chat_id).startswith("ou_"):
            return await original(
                self,
                chat_id=chat_id,
                msg_type=msg_type,
                payload=payload,
                reply_to=reply_to,
                metadata=metadata,
            )

        body = self._build_create_message_body(
            receive_id=chat_id,
            msg_type=msg_type,
            content=payload,
            uuid_value=str(uuid.uuid4()),
        )
        request = self._build_create_message_request("open_id", body)
        response = await asyncio.to_thread(self._client.im.v1.message.create, request)
        logger.info(
            "[multitenancy] delivered Feishu open_id message chat_id=%s message_id=%s",
            chat_id,
            _cw._feishu_response_message_id(response) or "unknown",
        )
        return response

    setattr(send_raw_message, "_hermes_multitenancy_patched", True)
    FeishuAdapter._send_raw_message = send_raw_message
    logger.info("[multitenancy] patched Feishu delivery for user open_id targets")


def _patch_feishu_outbound_link_render() -> None:
    """Make markdown links clickable in Feishu plain-text (table) messages.

    Wraps ``FeishuAdapter._build_outbound_payload`` without modifying Hermes
    source. When the core builder returns a ``"text"`` payload (which it does
    for any markdown-table content), rewrite ``[label](url)`` to ``label (url)``
    so the bare URL is auto-linkified by Feishu. ``"post"`` payloads already
    render markdown links and are left untouched.
    """
    try:
        FeishuAdapter = load_feishu_adapter()
    except Exception as exc:
        log_feishu_adapter_load_error(
            logger,
            "[multitenancy] FeishuAdapter not importable yet; outbound link render patch deferred",
            exc,
        )
        return

    original = getattr(FeishuAdapter, "_build_outbound_payload", None)
    if original is None or getattr(original, "_hermes_multitenancy_patched", False):
        return

    @functools.wraps(original)
    def build_outbound_payload(self: Any, content: str) -> tuple[str, str]:
        msg_type, payload = original(self, content)
        if msg_type != "text":
            return msg_type, payload
        try:
            data = json.loads(payload)
        except Exception:
            return msg_type, payload
        text = data.get("text") if isinstance(data, dict) else None
        if isinstance(text, str) and _cw._MD_LINK_RE.search(text):
            data["text"] = _cw._linkify_markdown_links_in_text(text)
            payload = json.dumps(data, ensure_ascii=False)
        return msg_type, payload

    setattr(build_outbound_payload, "_hermes_multitenancy_patched", True)
    setattr(build_outbound_payload, "_hermes_multitenancy_original", original)
    FeishuAdapter._build_outbound_payload = build_outbound_payload
    logger.info("[multitenancy] patched Feishu outbound payload to linkify markdown links in text mode")
