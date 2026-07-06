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
    """Patch GatewayRunner so non-router profile gateways never create Feishu."""
    try:
        from gateway.run import GatewayRunner
    except Exception:
        logger.exception("[multitenancy] failed to install gateway ownership guard")
        return

    _patch_gateway_runner_init(GatewayRunner)
    _patch_gateway_runner_create_adapter(GatewayRunner)
    logger.info("[multitenancy] installed gateway ownership guard")


def _patch_gateway_runner_init(GatewayRunner: Any) -> None:
    original = getattr(GatewayRunner, "__init__", None)
    if original is None or getattr(original, "_hermes_multitenancy_ownership_patched", False):
        return

    @functools.wraps(original)
    def wrapped_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
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
