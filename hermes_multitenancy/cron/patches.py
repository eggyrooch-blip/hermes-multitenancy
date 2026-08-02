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
import weakref
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
from ..feishu_adapter_compat import (
    is_executor_shutdown_error,
    load_feishu_adapter,
    load_feishu_module,
    log_feishu_adapter_load_error,
)
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
# Weak handle on the live GatewayRunner, remembered on the paths that already
# receive it, so shutdown-sensitive code can read core's own ``_draining`` flag
# instead of tracking teardown state itself. Reassigned → proxied live through
# the cron_worker shim (_LIVE_STATE_OWNERS), never snapshot-copied.
_gateway_ref: "Optional[weakref.ref[Any]]" = None


def _remember_gateway(gateway: Any) -> None:
    global _gateway_ref
    try:
        _gateway_ref = weakref.ref(gateway)
    except TypeError:  # pragma: no cover - non-weakrefable double
        _gateway_ref = None


def _note_live_gateway(gateway: Any) -> None:
    """A live gateway exists → remember it AND re-land the send-retry patch.

    register()-time class patching lands on the WRONG class object. The core
    plugin loader (``hermes_cli/plugins.py:_load_directory_module``) re-execs the
    feishu platform source under the synthetic name
    ``hermes_plugins.feishu_platform.adapter``, producing a class distinct from
    ``plugins.platforms.feishu.adapter.FeishuAdapter`` — and at register() time
    the synthetic module does not exist yet, so ``load_feishu_module()`` can only
    return the fallback clone. prod v0190 boot 2026-07-30 18:04:19 shows exactly
    that split: register-time hooks logged against
    ``plugins.platforms.feishu.adapter.FeishuAdapter`` while the running adapter
    logs under ``hermes_plugins.feishu_platform.adapter``.

    A gateway instance only exists after the platforms are built, so re-running
    the installer here re-resolves through ``load_feishu_module()`` — which now
    prefers the synthetic module — and lands the patch on the class the gateway
    actually runs (including a fresh ``adapter_module`` closure for the tunables).
    The register()-time install stays, covering loaders with other orderings; the
    marker makes both passes idempotent per class.
    """
    _remember_gateway(gateway)
    _patch_feishu_send_retry_shutdown_fatal()


def gateway_is_shutting_down() -> bool:
    """True once the gateway entered teardown (``stop()`` sets ``_draining``).

    Core flips ``_draining`` at the top of ``_stop_impl`` — before the drain,
    adapter disconnect and executor teardown — and back to False only after
    "Gateway stopped". Unknown gateway → False (fail-open: never suppress a
    delivery just because we lost the handle).
    """
    ref = _gateway_ref
    gateway = ref() if ref is not None else None
    if gateway is None:
        return False
    return bool(getattr(gateway, "_draining", False))


def ensure_cron_worker_started(gateway: Any) -> None:
    """Start the multi-profile cron worker once, on first hook dispatch.

    No-op when gateway adapters are not yet ready, when no asyncio loop is
    running, or when HERMES_HOME does not look like a multitenancy nested
    layout (``<root>/profiles/<name>``).
    """
    global _worker_started, _worker_thread, _worker_stop
    _note_live_gateway(gateway)
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
    _note_live_gateway(gateway)
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
        owner_open_id = str(job.get("owner_open_id") or "").strip()
        if str(deliver_value).strip().lower() == "feishu" and owner_open_id.startswith("ou_"):
            return {
                "platform": "feishu",
                "chat_id": owner_open_id,
                "thread_id": None,
            }
        return None

    setattr(resolve_single_delivery_target, "_hermes_multitenancy_patched", True)
    scheduler._resolve_single_delivery_target = resolve_single_delivery_target
    logger.info("[multitenancy] patched cron delivery fallback to owner open_id")


def _cron_run_broker_enabled() -> bool:
    value = os.environ.get("HERMES_MULTITENANCY_CRON_RUN_BROKER", "").strip().lower()
    if not value:
        return True
    return value in {"1", "true", "yes", "on"}


def _cron_delivery_identity_is_bound(job: dict, target: dict) -> bool:
    """Prove one active profile route owns the Feishu delivery target."""
    owner = str(job.get("owner_open_id") or "").strip()
    profile = str(job.get("owner_profile") or "").strip()
    if not owner.startswith("ou_") or not profile or profile == "multitenancy_router":
        return False
    try:
        db_path = _cw._resolve_shared_home() / "multitenancy.db"
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
            rows = conn.execute(
                "SELECT open_id, owner_open_id, chat_id "
                "FROM multitenancy_routing WHERE profile_name = ? AND active = 1",
                (profile,),
            ).fetchall()
    except Exception:
        return False
    if len(rows) != 1 or str(rows[0][1] or rows[0][0] or "").strip() != owner:
        return False

    chat_id = str(target.get("chat_id") or "").strip()
    return bool(
        chat_id
        and (
            chat_id == owner
            or chat_id == str(rows[0][2] or "").strip()
        )
    )


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
        # Once the gateway is draining, every Feishu send is doomed (the SDK
        # executor is going away) and each attempt still pays the retry tax —
        # that is how prod 2026-07-30 logged "delivery error: Feishu send
        # failed: cannot schedule new futures after interpreter shutdown" while
        # the process hung for 4 minutes. Skip the send and report a delivery
        # error so mark_job_run records the miss instead of silently claiming
        # the result was delivered.
        if _cw.gateway_is_shutting_down():
            logger.info(
                "[multitenancy] cron delivery skipped during gateway shutdown job=%s",
                job.get("id", "?"),
            )
            return "cron delivery skipped: gateway is shutting down"

        # A cron delivery has no interactive caller watching a stream. Send its
        # single Feishu target once and require the resulting message receipt;
        # core and the old multi-stage stream both accept weaker acknowledgments.
        deliver = str(job.get("deliver") or "").strip().lower()
        origin = job.get("origin") if isinstance(job.get("origin"), dict) else {}
        requests_feishu = any(
            part.strip().split(":", 1)[0] == "feishu" for part in deliver.split(",")
        ) or (
            deliver == "origin"
            and str(origin.get("platform") or "").strip().lower() == "feishu"
        )
        try:
            targets = scheduler._resolve_delivery_targets(job)
        except Exception:
            if requests_feishu:
                return "failed to resolve cron delivery target"
            return original(job, content, adapters=adapters, loop=loop)
        if requests_feishu and (
            not targets
            or (
                len(targets) == 1
                and str(targets[0].get("platform") or "").strip().lower() != "feishu"
            )
        ):
            return "failed to resolve cron delivery target"
        if len(targets) == 1 and str(targets[0].get("platform") or "").strip().lower() == "feishu":
            target = targets[0]
            if not _cw._cron_delivery_identity_is_bound(job, target):
                return "cron delivery identity is unavailable or ambiguous"
            try:
                _, media_files = _cw._cron_delivery_payload_for_adapter(job, content)
            except Exception:
                media_files = ["unknown"]
            if not media_files:
                live_error = _cw._deliver_cron_feishu_via_live_adapter(
                    scheduler,
                    job,
                    content,
                    adapters=adapters,
                    loop=loop,
                    require_receipt=True,
                    targets_override=[target],
                )
                if live_error is None:
                    _cw._mirror_cron_delivery_to_owner(job, content)
                    return None
                if live_error == "feishu live adapter unavailable":
                    return live_error
                reason = "other"
                for marker, code in (
                    ("no payload", "no_payload"),
                    ("missing message_id", "missing_receipt"),
                    ("timed out", "timeout"),
                    ("loop unavailable", "loop_unavailable"),
                    ("schedule", "loop_unavailable"),
                    ("send failed", "rejected"),
                    ("send raised", "exception"),
                    ("delivery to", "exception"),
                    ("serialize", "serialize"),
                ):
                    if marker in live_error:
                        reason = code
                        break
                logger.warning(
                    "[multitenancy] cron Feishu delivery unconfirmed job=%s reason=%s",
                    job.get("id", "?"),
                    reason,
                )
                return "feishu live adapter delivery unconfirmed"

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


_SEND_RETRY_KWARGS = frozenset({"chat_id", "msg_type", "payload", "reply_to", "metadata"})


def _patch_feishu_send_retry_shutdown_fatal() -> None:
    """Stop the Feishu send-retry loop from retrying process-teardown errors.

    Core's ``_feishu_send_with_retry`` treats every exception as transient and
    sleeps 1s then 2s before giving up on attempt 3. Once the interpreter / SDK
    executor is shut down, every send raises ``RuntimeError: cannot schedule new
    futures after …shutdown`` — fatal and unrecoverable — so the loop burned ~3s
    per chat and, serialized over the chats with live sessions, held the process
    open for 4 minutes until systemd SIGKILLed it (prod 2026-07-30
    18:00:11→18:04:11, teardown itself took 0.91s).

    This REPLACES the loop instead of wrapping it: the retry decision lives
    inside core's ``except`` block and there is no seam to hook from outside.
    Everything else mirrors core, including the two escape branches
    (post-content-invalid, withdrawn-reply fallback) and the exact
    "[Feishu] Send attempt …" wording ops greps for. The tunables are read from
    the runtime adapter module on every call so core retuning still applies.

    ponytail: a fork of ~30 core lines is the ceiling here. If core grows a
    parameter or an ``_is_retryable`` seam, this delegates to the original and
    warns (see the call-shape check) rather than mirroring stale logic — upgrade
    path is to drop this patch and use the core seam.
    """
    try:
        # ONE resolution for both, so the patched class and the module we read
        # the tunables from can never come from different copies of the adapter
        # source (the double-import trap: the plugin loader's synthetic module vs
        # ``plugins.platforms.feishu.adapter``).
        adapter_module = load_feishu_module()
        FeishuAdapter = getattr(adapter_module, "FeishuAdapter")
    except Exception as exc:
        log_feishu_adapter_load_error(
            logger,
            "[multitenancy] FeishuAdapter not importable yet; send retry shutdown patch deferred",
            exc,
        )
        return

    original = getattr(FeishuAdapter, "_feishu_send_with_retry", None)
    if original is None or getattr(original, "_hermes_multitenancy_send_retry_fatal_patched", False):
        return

    @functools.wraps(original)
    async def feishu_send_with_retry(self: Any, *args: Any, **kwargs: Any) -> Any:
        # Never assume the core signature: anything we don't recognise goes
        # straight to the original (2026-07-21 cron run_job taught us what a
        # hard-coded signature costs).
        # EXACTLY core's five keyword-only required params — a subset would let a
        # missing-arg call through and hand ``_send_raw_message`` a None where
        # core would have raised TypeError immediately.
        if args or set(kwargs) != _SEND_RETRY_KWARGS:
            logger.warning(
                "[multitenancy] unexpected _feishu_send_with_retry call shape %r; "
                "using core retry loop (shutdown fast-fail inactive)",
                sorted(kwargs),
            )
            return await original(self, *args, **kwargs)

        attempts = max(int(getattr(adapter_module, "_FEISHU_SEND_ATTEMPTS", 3) or 1), 1)
        fallback_codes = getattr(adapter_module, "_FEISHU_REPLY_FALLBACK_CODES", frozenset())
        post_invalid_re = getattr(adapter_module, "_POST_CONTENT_INVALID_RE", None)

        chat_id = kwargs.get("chat_id")
        msg_type = kwargs.get("msg_type")
        payload = kwargs.get("payload")
        metadata = kwargs.get("metadata")
        active_reply_to = kwargs.get("reply_to")

        last_error: Optional[BaseException] = None
        for attempt in range(attempts):
            try:
                response = await self._send_raw_message(
                    chat_id=chat_id,
                    msg_type=msg_type,
                    payload=payload,
                    reply_to=active_reply_to,
                    metadata=metadata,
                )
                # Replying to a withdrawn/missing message: post a new message
                # to the chat instead (core behaviour, preserved verbatim).
                if active_reply_to and not self._response_succeeded(response):
                    code = getattr(response, "code", None)
                    if code in fallback_codes:
                        # A thread reply that fails must NOT fall back to a
                        # top-level message — that would open a new topic in the
                        # group (core v0190 guard, kept verbatim).
                        if (metadata or {}).get("thread_id"):
                            logger.warning(
                                "[Feishu] Reply to %s failed in thread %s (code %s — message withdrawn/missing); "
                                "skipping top-level fallback to avoid creating a new topic",
                                active_reply_to,
                                (metadata or {}).get("thread_id"),
                                code,
                            )
                            return response
                        logger.warning(
                            "[Feishu] Reply to %s failed (code %s — message withdrawn/missing); "
                            "falling back to new message in chat %s",
                            active_reply_to,
                            code,
                            chat_id,
                        )
                        active_reply_to = None
                        response = await self._send_raw_message(
                            chat_id=chat_id,
                            msg_type=msg_type,
                            payload=payload,
                            reply_to=None,
                            metadata=metadata,
                        )
                return response
            except Exception as exc:
                last_error = exc
                if is_executor_shutdown_error(exc):
                    logger.warning(
                        "[multitenancy] Feishu send aborted for chat %s — "
                        "process is shutting down, not retrying: %s",
                        chat_id,
                        exc,
                    )
                    raise
                if msg_type == "post" and post_invalid_re is not None and post_invalid_re.search(str(exc)):
                    raise
                if attempt >= attempts - 1:
                    raise
                wait_seconds = 2 ** attempt
                logger.warning(
                    "[Feishu] Send attempt %d/%d failed for chat %s; retrying in %ds: %s",
                    attempt + 1,
                    attempts,
                    chat_id,
                    wait_seconds,
                    exc,
                )
                await asyncio.sleep(wait_seconds)
        raise last_error or RuntimeError("Feishu send failed")

    setattr(feishu_send_with_retry, "_hermes_multitenancy_send_retry_fatal_patched", True)
    setattr(feishu_send_with_retry, "_hermes_multitenancy_original", original)
    FeishuAdapter._feishu_send_with_retry = feishu_send_with_retry
    logger.info(
        "[multitenancy] patched Feishu send retry to fail fast on executor-shutdown errors (%s)",
        getattr(adapter_module, "__name__", "?"),
    )


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

    # Core owns this signature; v0.19 added the keyword-only ``prefer_post``.
    @functools.wraps(original)
    def build_outbound_payload(self: Any, content: str, *args: Any, **kwargs: Any) -> tuple[str, str]:
        msg_type, payload = original(self, content, *args, **kwargs)
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
