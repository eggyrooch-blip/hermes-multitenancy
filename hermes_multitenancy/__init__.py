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

import os
from typing import Any

from .cron_worker import ensure_cron_worker_started
from .router import on_pre_gateway_dispatch


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
    from .router import override_pool
    from .runtime import ProfileRuntime

    def _real_factory(profile_name, profile_home):
        return ProfileRuntime(profile_home=profile_home, run_agent_fn=real_run_agent)

    override_pool(_build_runtime_pool(_real_factory))

    ctx.register_hook("gateway_startup", _startup_with_worker_init)
    ctx.register_hook("pre_gateway_dispatch", _dispatch_with_worker_init)


def _startup_with_worker_init(**kwargs: Any) -> None:
    """Start the multi-profile cron worker as soon as the router gateway is ready."""
    gateway = kwargs.get("gateway")
    if gateway is not None:
        ensure_cron_worker_started(gateway)


def _dispatch_with_worker_init(**kwargs: Any) -> dict:
    """Wrap on_pre_gateway_dispatch: lazy-start the multi-profile cron worker."""
    gateway = kwargs.get("gateway")
    if gateway is not None:
        ensure_cron_worker_started(gateway)
    return on_pre_gateway_dispatch(**kwargs)


__all__ = ["register", "on_pre_gateway_dispatch", "_build_runtime_pool"]
