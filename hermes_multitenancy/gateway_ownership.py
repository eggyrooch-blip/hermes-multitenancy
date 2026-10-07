"""Gateway ownership guards for multitenancy-managed platforms."""
from __future__ import annotations

import functools
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_ROUTER_PROFILE = "multitenancy_router"


def current_profile_name() -> str | None:
    """Return the active Hermes profile name when the process exposes one."""
    for env_name in ("HERMES_PROFILE", "HERMES_PROFILE_NAME"):
        value = os.environ.get(env_name)
        if value and value.strip():
            return value.strip()

    hermes_home = os.environ.get("HERMES_HOME")
    if not hermes_home:
        return None
    path = Path(hermes_home).expanduser()
    if path.name and path.parent.name == "profiles":
        return path.name
    return None


def router_profile_name() -> str:
    return os.environ.get("HERMES_MULTITENANCY_ROUTER_PROFILE", DEFAULT_ROUTER_PROFILE).strip() or DEFAULT_ROUTER_PROFILE


def is_router_profile_runtime() -> bool:
    """Whether this process should own router-only multitenancy runtime.

    Unknown profile keeps legacy behavior for local tests and non-profile
    launches. Production systemd services set HERMES_HOME to a profile path, so
    profile gateways are still constrained fail-closed there.
    """
    profile = current_profile_name()
    return profile is None or profile == router_profile_name()


def _may_own_feishu_runtime() -> bool:
    """Whether this gateway process may own the Feishu websocket.

    ONE rule for BOTH the main router AND expert bots — no masquerade. A gateway
    owns Feishu when it is EITHER the router profile OR a dedicated fixed-expert
    bot instance (its own Feishu app, bound via HERMES_MULTITENANCY_FIXED_EXPERT).
    Per-user profiles are neither, so they stay fail-closed (Feishu stripped).

    This lets an expert bot own its app's websocket WITHOUT setting
    HERMES_MULTITENANCY_ROUTER_PROFILE to masquerade as the router — the
    masquerade also wrongly flipped ``is_router_profile_runtime()`` True on the
    expert bot, enabling router-only cron/broker/credential-renewal subsystems
    that a second instance must not run. Feishu ownership and router-only
    behavior are now decided by two separate predicates.
    """
    if is_router_profile_runtime():
        return True
    try:
        from .expert_bot_route import fixed_expert_id_from_env
    except Exception:
        return False
    return bool(fixed_expert_id_from_env())


def may_own_cron_runtime() -> bool:
    return _may_own_feishu_runtime()


def install_gateway_ownership_guard() -> None:
    """Patch GatewayRunner so non-router profile gateways never create Feishu.

    Never imports ``gateway.run`` here: register runs inside core's plugin
    discovery, which ``gateway.run``'s module body can be waiting on (startup
    deadlock on the production disk, 2026-09-24).  The patches are applied as
    soon as ``gateway.run`` has finished loading, before any importer can build
    a ``GatewayRunner``; a failing patch fails that import (fail-closed).
    """
    from hermes_constants import get_hermes_home

    from .gateway_run_ready import when_gateway_run_loaded

    _patch_gateway_process_identity_home()
    # Resolve the cron scope now, in register's context, as the synchronous
    # install did; the deferred callback may run on another thread.
    profile = current_profile_name()
    profile_home = get_hermes_home()

    def install(gateway_run: Any) -> None:
        GatewayRunner = getattr(gateway_run, "GatewayRunner", None)
        if GatewayRunner is None:
            logger.error("[multitenancy] gateway.run has no GatewayRunner; refusing to start")
            raise RuntimeError("gateway_runner_unavailable")
        _patch_gateway_runner_handle_message(GatewayRunner)
        _patch_gateway_runner_busy_message(GatewayRunner)
        _patch_gateway_runner_obligation_adapter(GatewayRunner)
        _patch_gateway_update_text_reply(GatewayRunner)
        _patch_gateway_text_approval_owner(GatewayRunner)
        _patch_gateway_runner_init(GatewayRunner)
        _patch_gateway_runner_create_adapter(GatewayRunner)
        _patch_gateway_cron_profile_scope(gateway_run, profile=profile, profile_home=profile_home)
        logger.info("[multitenancy] installed gateway ownership guard")

    try:
        when_gateway_run_loaded("gateway_ownership_guard", install)
    except ImportError:
        logger.exception("[multitenancy] failed to install gateway ownership guard")
        raise RuntimeError("gateway_runner_unavailable") from None


def _patch_gateway_process_identity_home() -> None:
    """Keep gateway identity files at launch home while MT cron scans tenants."""
    from gateway import status

    original = status._get_process_hermes_home
    if getattr(original, "_hermes_multitenancy_identity_home_guard", False):
        return
    # Core's process resolver ignores ContextVar overrides but still reads the
    # global environment. MT's legacy cron scan temporarily changes that env;
    # a status path switch resets core's canonical snapshot to starting/empty.
    process_home = Path(original()).expanduser().resolve()

    @functools.wraps(original)
    def identity_home() -> Path:
        return process_home

    identity_home._hermes_multitenancy_identity_home_guard = True
    status._get_process_hermes_home = identity_home


def _patch_gateway_update_text_reply(GatewayRunner: Any) -> None:
    original = getattr(GatewayRunner, "_hm_update_prompt_reply", None)
    if original is None or getattr(original, "_hermes_multitenancy_update_guard", False):
        return

    @functools.wraps(original)
    def update_reply(self: Any, event: Any, *args: Any, **kwargs: Any) -> Any:
        if _platform_name(getattr(getattr(event, "source", None), "platform", None)) == "feishu":
            return None
        return original(self, event, *args, **kwargs)

    update_reply._hermes_multitenancy_update_guard = True
    GatewayRunner._hm_update_prompt_reply = update_reply


_TEXT_APPROVAL_NOT_OWNER = "⛔ 只有发起这条命令的人可以批准或拒绝，你的回复没有生效。"


def _patch_gateway_text_approval_owner(GatewayRunner: Any) -> None:
    """Feishu text /approve and /deny resolve only the sender's own approvals.

    Core resolves by session key; in a shared topic-thread session that lets any
    member answer another member's dangerous-command prompt.
    """
    for name, verb in (("_handle_approve_command", "approve"), ("_handle_deny_command", "deny")):
        original = getattr(GatewayRunner, name, None)
        if original is None or getattr(original, "_hermes_multitenancy_approval_owner", False):
            continue

        def make(original: Any, verb: str) -> Any:
            @functools.wraps(original)
            async def handler(self: Any, event: Any, *args: Any, **kwargs: Any) -> Any:
                if _platform_name(getattr(getattr(event, "source", None), "platform", None)) != "feishu":
                    return await original(self, event, *args, **kwargs)
                from tools.approval import list_gateway_approvals
                from .feishu_ingress_compat import text_approval_allowed

                pending = list_gateway_approvals(self._session_key_for_source(event.source))
                if pending:
                    tokens = event.get_command_args().strip().lower().split()
                    resolve_all = ("all" in tokens) if verb == "approve" else tokens[:1] == ["all"]
                    if not text_approval_allowed(event, pending, resolve_all=resolve_all):
                        logger.warning("[multitenancy] text %s denied reason=owner_mismatch", verb)
                        return _TEXT_APPROVAL_NOT_OWNER
                return await original(self, event, *args, **kwargs)

            handler._hermes_multitenancy_approval_owner = True
            return handler

        setattr(GatewayRunner, name, make(original, verb))


_UNRESOLVED: Any = object()


def _patch_gateway_cron_profile_scope(
    gateway_run: Any = None,
    *,
    profile: Any = _UNRESOLVED,
    profile_home: Any = None,
) -> None:
    if gateway_run is None:
        from gateway import run as gateway_run
    from hermes_constants import get_hermes_home

    original = getattr(gateway_run, "_cron_tick_profile_homes", None)
    if original is None or getattr(original, "_hermes_multitenancy_profile_guard", False):
        return
    # Core 0.21.4 ticks all profiles even with adapter multiplexing off. MT's
    # own worker owns those stores/identities; native cron keeps only this home.
    if profile is _UNRESOLVED:
        profile = current_profile_name()
    if profile_home is None:
        profile_home = get_hermes_home()

    @functools.wraps(original)
    def profile_homes(config: Any) -> list:
        return [(profile, profile_home)]

    profile_homes._hermes_multitenancy_profile_guard = True
    gateway_run._cron_tick_profile_homes = profile_homes


def _patch_gateway_runner_handle_message(GatewayRunner: Any) -> None:
    original = getattr(GatewayRunner, "_handle_message", None)
    if not callable(original):
        raise RuntimeError("gateway_message_handler_unavailable")
    if getattr(original, "_hermes_multitenancy_internal_guard", False) is True:
        return

    @functools.wraps(original)
    async def wrapped_handle_message(self: Any, event: Any, *args: Any, **kwargs: Any) -> Any:
        # Core internal events bypass pre_gateway_dispatch entirely, including startup
        # session resume. No trusted ticket can make that shared-model path tenant-safe.
        if (
            bool(getattr(event, "internal", False))
            and _platform_name(getattr(getattr(event, "source", None), "platform", None)) == "feishu"
        ):
            logger.warning("[multitenancy] blocked internal Feishu event before core dispatch")
            return None
        return await original(self, event, *args, **kwargs)

    wrapped_handle_message._hermes_multitenancy_internal_guard = True
    GatewayRunner._handle_message = wrapped_handle_message


def _patch_gateway_runner_busy_message(GatewayRunner: Any) -> None:
    original = getattr(GatewayRunner, "_handle_active_session_busy_message", None)
    if not callable(original):
        raise RuntimeError("gateway_busy_handler_unavailable")
    if getattr(original, "_hermes_multitenancy_busy_guard", False) is True:
        return

    @functools.wraps(original)
    async def wrapped_busy(self: Any, event: Any, *args: Any, **kwargs: Any) -> bool:
        if _platform_name(getattr(getattr(event, "source", None), "platform", None)) == "feishu":
            if not bool(getattr(event, "internal", False)):
                try:
                    from .plugin_entry import _dispatch_with_worker_init
                    _dispatch_with_worker_init(
                        event=event, gateway=self, session_store=getattr(self, "session_store", None)
                    )
                except Exception:
                    logger.error("[multitenancy] Feishu busy dispatch denied")
            return True
        return await original(self, event, *args, **kwargs)

    wrapped_busy._hermes_multitenancy_busy_guard = True
    GatewayRunner._handle_active_session_busy_message = wrapped_busy


def _patch_gateway_runner_obligation_adapter(GatewayRunner: Any) -> None:
    # Older cores have no delivery ledger; all current replay lanes resolve here.
    original = getattr(GatewayRunner, "_obligation_adapter", None)
    if original is None:
        return
    if not callable(original):
        raise RuntimeError("gateway_obligation_adapter_unavailable")
    if getattr(original, "_hermes_multitenancy_obligation_guard", False) is True:
        return

    @functools.wraps(original)
    async def wrapped_adapter(self: Any, row: dict, *args: Any, **kwargs: Any) -> Any:
        if _platform_name(row.get("platform")) == "feishu":
            logger.warning("[multitenancy] blocked shared Feishu delivery obligation")
            return None
        return await original(self, row, *args, **kwargs)

    wrapped_adapter._hermes_multitenancy_obligation_guard = True
    GatewayRunner._obligation_adapter = wrapped_adapter


def _patch_gateway_runner_init(GatewayRunner: Any) -> None:
    original = getattr(GatewayRunner, "__init__", None)
    if original is None or getattr(original, "_hermes_multitenancy_ownership_patched", False):
        return

    @functools.wraps(original)
    def wrapped_init(self: Any, config: Any = None, *args: Any, **kwargs: Any) -> None:
        # MT owns tenant routing and adapter credentials. Inject the supported
        # embedded-runner config so core's default-on host multiplex preflight
        # cannot discover/start tenant profiles outside MT admission.
        if config is None:
            from gateway.config import load_gateway_config

            config = load_gateway_config()
        config.multiplex_profiles = False
        original(self, config, *args, **kwargs)
        _enforce_feishu_ownership(getattr(self, "config", None))

    setattr(wrapped_init, "_hermes_multitenancy_ownership_patched", True)
    GatewayRunner.__init__ = wrapped_init


def _patch_gateway_runner_create_adapter(GatewayRunner: Any) -> None:
    original = getattr(GatewayRunner, "_create_adapter", None)
    if original is None or getattr(original, "_hermes_multitenancy_ownership_patched", False):
        return

    @functools.wraps(original)
    def wrapped_create_adapter(self: Any, platform: Any, config: Any, *args: Any, **kwargs: Any) -> Any:
        if _should_block_feishu_platform(platform):
            _remove_platform(getattr(self, "config", None), "feishu")
            logger.warning(
                "[multitenancy] blocked Feishu adapter creation for non-router profile %s; "
                "only %s may own the Feishu websocket",
                current_profile_name(),
                router_profile_name(),
            )
            return None
        return original(self, platform, config, *args, **kwargs)

    setattr(wrapped_create_adapter, "_hermes_multitenancy_ownership_patched", True)
    GatewayRunner._create_adapter = wrapped_create_adapter


def _enforce_feishu_ownership(config: Any) -> bool:
    if _may_own_feishu_runtime():
        return False

    removed = _remove_platform(config, "feishu")
    if removed:
        logger.warning(
            "[multitenancy] stripped Feishu platform from profile %s; only the router "
            "or a fixed-expert bot may own the Feishu websocket",
            current_profile_name(),
        )
    return removed


def _should_block_feishu_platform(platform: Any) -> bool:
    return not _may_own_feishu_runtime() and _platform_name(platform) == "feishu"


def _remove_platform(config: Any, platform_name: str) -> bool:
    platforms = getattr(config, "platforms", None)
    if not isinstance(platforms, dict):
        return False

    removed = False
    for platform in list(platforms.keys()):
        if _platform_name(platform) == platform_name:
            del platforms[platform]
            removed = True
    return removed


def _platform_name(platform: Any) -> str:
    value = getattr(platform, "value", platform)
    return str(value).strip().lower()


__all__ = [
    "current_profile_name",
    "install_gateway_ownership_guard",
    "is_router_profile_runtime",
    "may_own_cron_runtime",
    "router_profile_name",
]
