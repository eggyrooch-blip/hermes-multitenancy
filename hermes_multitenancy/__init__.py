"""hermes-multitenancy — Spike Phase 1.

Verifies 3 things end-to-end via mocks:
  1. pre_gateway_dispatch hook registration + fire-and-forget asyncio.create_task
  2. ProfileRuntime monkey-patches HERMES_HOME to spawn a mini-AIAgent
  3. Full loop: hook -> ProfileRuntime -> send_typing -> AIAgent.run -> adapter.send

Plugin contract (what `register(ctx)` does):
  - Registers ONE pre_gateway_dispatch callback (sync def, returns dict)
  - Callback uses asyncio.create_task to dispatch async work
  - Callback returns {"action": "skip"} so gateway main flow does not handle the message
  - Lazy-starts a per-profile cron worker on first dispatch (multi-profile cron support)
"""
from __future__ import annotations

import logging
import os
from typing import Any

from .cron_worker import (
    ensure_cron_worker_started,
    install_cron_runtime_patches,
    install_gateway_startup_watcher,
    install_profile_native_cron_guard,
)
from .credential_audit import run_startup_audit
from .credential_renewal_worker import ensure_renewal_worker_started
from .router import on_pre_gateway_dispatch
from . import webui_broker_server
from .gateway_ownership import (
    install_gateway_ownership_guard,
    is_router_profile_runtime,
    may_own_cron_runtime,
)

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _build_runtime_pool(runtime_factory):
    from .pool import (
        DEFAULT_COLD_START_CONCURRENCY,
        DEFAULT_IDLE_EVICT,
        DEFAULT_INFLIGHT_TIMEOUT,
        DEFAULT_MAX_LOADED,
        RuntimePool,
    )

    return RuntimePool(
        max_loaded_runtimes=_env_int("HERMES_MULTITENANCY_MAX_LOADED_RUNTIMES", DEFAULT_MAX_LOADED),
        idle_evict_seconds=_env_float("HERMES_MULTITENANCY_IDLE_EVICT_SECONDS", DEFAULT_IDLE_EVICT),
        cold_start_concurrency=_env_int("HERMES_MULTITENANCY_COLD_START_CONCURRENCY", DEFAULT_COLD_START_CONCURRENCY),
        inflight_timeout_seconds=_env_float("HERMES_MULTITENANCY_INFLIGHT_TIMEOUT_SECONDS", DEFAULT_INFLIGHT_TIMEOUT),
        runtime_factory=runtime_factory,
    )


def register(ctx) -> None:
    """Hermes plugin entry point — wires the multitenancy router to pre_gateway_dispatch.

    Called by Hermes plugin loader once at startup. ``ctx`` is a PluginContext
    instance (hermes_cli.plugins.PluginContext). It exposes ``register_hook``.

    Side effect: installs a RuntimePool whose factory uses ``real_run_agent``
    (live LLM thin client) instead of the unit-test echo stub. Without this,
    Bot replies would be the echo string, not real model output.
    """
    from .agent_real import real_run_agent
    from .credential_tool import register_credential_status_tool
    from .router import override_pool
    from .runtime import ProfileRuntime

    def _real_factory(profile_name, profile_home):
        return ProfileRuntime(profile_home=profile_home, run_agent_fn=real_run_agent)

    override_pool(_build_runtime_pool(_real_factory))
    install_gateway_ownership_guard()
    # Make core skill resolution honor the CURRENT HERMES_HOME (not the frozen
    # import-time value). Unconditional: in the router gateway it fixes
    # cross-profile "skill not found" for cron jobs; in profile-native runtimes
    # dynamic == static, so it is harmless. See skills_dir_dynamic for why.
    from .skills_dir_dynamic import install_dynamic_skills_dir_patch
    install_dynamic_skills_dir_patch()
    install_profile_native_cron_guard()
    # ponytail: temp live-diagnostic seam (env-gated, no-op unless HERMES_PUSH_CARD_DEBUG
    # set). Surfaces push-card INFO/DEBUG to stderr because the gateway filters
    # hermes_multitenancy.* below WARNING. Remove before ship.
    if __import__("os").environ.get("HERMES_PUSH_CARD_DEBUG"):
        import logging as _lg
        _pc = _lg.getLogger("hermes_multitenancy")
        _pc.setLevel(_lg.DEBUG)
        _h = _lg.StreamHandler()
        _h.setLevel(_lg.DEBUG)
        _h.setFormatter(_lg.Formatter("PCDEBUG %(name)s %(levelname)s %(message)s"))
        _pc.addHandler(_h)
        _pc.warning("PCDEBUG push-card debug logging enabled; is_router=%s", is_router_profile_runtime())
    if may_own_cron_runtime():
        install_cron_runtime_patches()
        install_gateway_startup_watcher()
    if is_router_profile_runtime():
        from .group_inviter_hook import install_feishu_bot_added_hook
        install_feishu_bot_added_hook()
        from .feishu_helpdesk_events import install_feishu_helpdesk_events_patch
        install_feishu_helpdesk_events_patch()
        from .feishu_media_retry import install_feishu_media_retry_patch
        install_feishu_media_retry_patch()
        from .feishu_clarify_cards import install_feishu_clarify_card_action_patch
        install_feishu_clarify_card_action_patch()
        try:
            from .push_card_matcher import install_feishu_push_card_matcher_patch
            install_feishu_push_card_matcher_patch()
        except Exception:
            # Matcher wiring is fail-open by design; a deferred/failed install
            # must never block the broker/credential subsystems started below.
            logger.exception("[push_card] matcher install failed; continuing without inbound matching")
        try:
            from .push_card_confirm import install_feishu_push_card_confirm_patch
            install_feishu_push_card_confirm_patch()
        except Exception:
            # 5th card.action.trigger patch; undefined放行 delegates to the other
            # four. Fail-open like its siblings — never block the broker below.
            logger.exception("[push_card] confirm patch install failed; continuing without confirm handling")
        try:
            # Bind the proactive card sender (notify-card dispatch has no inbound
            # event to carry the adapter) and start the sweep worker — without
            # these the live sender never fires and the expiry/re-drive/reconcile
            # sweeps are dead code.
            from .push_send_queue import (
                install_gateway_push_card_adapter_capture,
                install_live_push_card_sender,
            )
            install_live_push_card_sender()
            # Capture the live Feishu adapter at gateway startup so the FIRST
            # proactive push works without the user messaging the bot first
            # (cold-start; the inbound stash is only a fallback).
            install_gateway_push_card_adapter_capture()
            from .push_card_workers import ensure_push_card_sweeps_started
            ensure_push_card_sweeps_started()
        except Exception:
            logger.exception("[push_card] send-sender / sweep wiring failed; continuing")
        try:
            from .feishu_message_trace import install_message_trace_filter
            install_message_trace_filter()
        except Exception:
            # Diagnostic sugar must not block the broker / credential subsystems
            # that this same block starts right after (sibling installs above
            # swallow too, for the same reason).
            logger.exception("[message_trace] install failed; continuing without per-message log tracing")
        webui_broker_server.ensure_run_broker_server_started()
        _start_credential_renewal_subsystem()

    register_credential_status_tool(ctx)
    _register_optional_vod_image_gen_provider(ctx)
    ctx.register_hook("pre_gateway_dispatch", _dispatch_with_worker_init)


def _register_optional_vod_image_gen_provider(ctx) -> bool:
    register = getattr(ctx, "register_image_gen_provider", None)
    if not callable(register):
        logger.debug("[tencent-vod] image_gen provider registration unavailable on ctx")
        return False
    try:
        from .tencent_vod_image_gen import register_vod_image_gen_provider
    except ModuleNotFoundError as exc:
        missing = str(getattr(exc, "name", "") or "")
        if missing == "agent" or missing.startswith("agent."):
            logger.info("[tencent-vod] optional image_gen provider API unavailable; skipping registration")
            return False
        raise
    return bool(register_vod_image_gen_provider(ctx))


def _start_credential_renewal_subsystem() -> None:
    """Wire L5 (startup audit) + L2 (renewal worker).

    Router-only — these subsystems own the credential lifecycle for the whole
    Mac. Running per-profile would race on the same marker files.
    """
    from .feishu_uat_auth import resolve_shared_home

    try:
        shared_home = resolve_shared_home()
    except Exception:
        logger.exception("[credential_renewal] failed to resolve shared_home; subsystem disabled")
        return

    try:
        run_startup_audit(shared_home)
    except Exception:
        logger.exception("[credential_renewal] L5 startup audit failed")

    try:
        ensure_renewal_worker_started(shared_home)
    except Exception:
        logger.exception("[credential_renewal] L2 renewal worker failed to start")


def _dispatch_with_worker_init(**kwargs: Any) -> dict:
    """Wrap on_pre_gateway_dispatch: lazy-start the multi-profile cron worker."""
    if is_router_profile_runtime():
        webui_broker_server.ensure_run_broker_server_started()
    gateway = kwargs.get("gateway")
    if gateway is not None and may_own_cron_runtime():
        try:
            ensure_cron_worker_started(gateway)
        except Exception:
            logger.exception("[multitenancy] cron worker lazy-start failed")
    return on_pre_gateway_dispatch(**kwargs)


__all__ = ["register", "on_pre_gateway_dispatch", "_build_runtime_pool"]
