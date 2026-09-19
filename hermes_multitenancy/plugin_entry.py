"""Plugin registration + gateway-dispatch wiring for hermes-multitenancy.

This lives OUT of ``hermes_multitenancy/__init__.py`` on purpose. Python runs a
package's ``__init__`` before ANY ``import hermes_multitenancy.<sub>``, so every
import this file needs would otherwise land in the import closure of every test
in the repo — 187 of the package's 225 files, which makes affected-test
selection useless. ``__init__`` reaches this module lazily by name (PEP 562
``__getattr__``), so importing e.g. ``hermes_multitenancy.routing`` no longer
drags the whole plugin surface along.

Nothing here is imported at ``hermes_multitenancy`` import time; it is loaded on
first access to ``register`` / ``_dispatch_with_worker_init``.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

_PACKAGE = __name__.rsplit(".", 1)[0]
logger = logging.getLogger(_PACKAGE)


def _mt():
    """The ``hermes_multitenancy`` package object.

    Tests monkeypatch names ON the package (``monkeypatch.setattr(
    hermes_multitenancy, "may_own_cron_runtime", ...)``), and the package
    resolves those lazily through PEP 562. Reading them off the package object
    honors both; a module-local ``from .x import y`` would shadow the patch.
    """
    # Not a hardcoded name: `hermes plugins install owner/repo` loads the repo
    # root under a synthetic package, so this module's real package is whatever
    # `__name__` says it is.
    return sys.modules[_PACKAGE]


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


def _register(ctx) -> None:
    """Hermes plugin entry point — wires the multitenancy router to pre_gateway_dispatch.

    Called by Hermes plugin loader once at startup. ``ctx`` is a PluginContext
    instance (hermes_cli.plugins.PluginContext). It exposes ``register_hook``.

    Side effect: installs a RuntimePool whose factory uses ``real_run_agent``
    (live LLM thin client) instead of the unit-test echo stub. Without this,
    Bot replies would be the echo string, not real model output.
    """
    mt = _mt()
    from . import webui_broker_server
    from .agent_real import real_run_agent
    from .credential_tool import register_credential_status_tool
    from .lark_cli_tool import (
        post_lark_cli_operation,
        transform_lark_cli_operation_result,
    )
    from .router import override_pool
    from .runtime import ProfileRuntime

    def _real_factory(profile_name, profile_home):
        return ProfileRuntime(profile_home=profile_home, run_agent_fn=real_run_agent)

    override_pool(_build_runtime_pool(_real_factory))
    mt.install_gateway_ownership_guard()
    from .trusted_feishu_ingress import install_trusted_feishu_ingress_admission
    install_trusted_feishu_ingress_admission()
    # Make core skill resolution honor the CURRENT HERMES_HOME (not the frozen
    # import-time value). Unconditional: in the router gateway it fixes
    # cross-profile "skill not found" for cron jobs; in profile-native runtimes
    # dynamic == static, so it is harmless. See skills_dir_dynamic for why.
    from .skills_dir_dynamic import install_dynamic_skills_dir_patch
    install_dynamic_skills_dir_patch()
    mt.install_profile_native_cron_guard()
    if mt.may_own_cron_runtime():
        mt.install_cron_runtime_patches()
        mt.install_gateway_startup_watcher()
    if mt.is_router_profile_runtime():
        from .feishu_group_topic_session import install_feishu_group_topic_session_patch
        install_feishu_group_topic_session_patch()
        from .group_inviter_hook import install_feishu_bot_added_hook
        install_feishu_bot_added_hook()
        from .feishu_media_retry import install_feishu_media_retry_patch
        install_feishu_media_retry_patch()
        # ONE card-action dispatcher owns the live callback; the feature
        # installers below only register/reuse their business handlers.
        from .feishu_card_action_dispatcher import install_feishu_card_action_dispatcher
        install_feishu_card_action_dispatcher()
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
        mt._start_credential_renewal_subsystem()

    register_credential_status_tool(ctx)
    _register_optional_vod_image_gen_provider(ctx)
    ctx.register_hook("post_tool_call", post_lark_cli_operation)
    ctx.register_hook("transform_tool_result", transform_lark_cli_operation_result)
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
    mt = _mt()
    from .feishu_uat_auth import resolve_shared_home

    try:
        shared_home = resolve_shared_home()
    except Exception:
        logger.exception("[credential_renewal] failed to resolve shared_home; subsystem disabled")
        return

    try:
        mt.run_startup_audit(shared_home)
    except Exception:
        logger.exception("[credential_renewal] L5 startup audit failed")

    try:
        mt.ensure_renewal_worker_started(shared_home)
    except Exception:
        logger.exception("[credential_renewal] L2 renewal worker failed to start")


def _dispatch_with_worker_init(**kwargs: Any) -> dict:
    """Wrap on_pre_gateway_dispatch: lazy-start the multi-profile cron worker."""
    mt = _mt()
    try:
        from . import webui_broker_server
        from .trusted_feishu_ingress import validate_admitted_feishu_event
        if not validate_admitted_feishu_event(kwargs.get("event"), kwargs.get("gateway")):
            logger.warning("[multitenancy] trusted Feishu ingress denied before dispatch")
            return {"action": "skip", "reason": "trusted Feishu ingress denied"}
        if mt.is_router_profile_runtime():
            webui_broker_server.ensure_run_broker_server_started()
            if not webui_broker_server.run_broker_server_ready():
                logger.error("[multitenancy] run broker is not ready; dropping routed gateway event")
                return {"action": "skip", "reason": "multitenancy run broker unavailable"}
        gateway = kwargs.get("gateway")
        if gateway is not None and mt.may_own_cron_runtime():
            try:
                mt.ensure_cron_worker_started(gateway)
            except Exception:
                logger.exception("[multitenancy] cron worker lazy-start failed")
        return mt.on_pre_gateway_dispatch(**kwargs)
    except Exception:
        # Known-gotcha: Hermes treats a hook exception as no decision and falls
        # through to the core agent, so this ownership boundary must return skip.
        logger.exception("[multitenancy] pre_gateway_dispatch boundary failed; dropping event")
        return {"action": "skip", "reason": "multitenancy router hook failed closed"}
