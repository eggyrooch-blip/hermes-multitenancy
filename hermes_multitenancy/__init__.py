"""hermes-multitenancy — Spike Phase 1.

Verifies 3 things end-to-end via mocks:
  1. pre_gateway_dispatch hook registration + fire-and-forget asyncio.create_task
  2. ProfileRuntime monkey-patches HERMES_HOME to spawn a mini-AIAgent
  3. Full loop: hook -> ProfileRuntime -> send_typing -> AIAgent.run -> adapter.send

Plugin contract (what `register(ctx)` does):
  - Registers ONE pre_gateway_dispatch callback (sync def, returns dict)
  - Callback uses asyncio.create_task to dispatch async work
  - Callback returns {"action": "skip"} so gateway main flow does not handle the message
"""
from __future__ import annotations

from .router import on_pre_gateway_dispatch


def register(ctx) -> None:
    """Hermes plugin entry point — wires the multitenancy router to pre_gateway_dispatch.

    Called by Hermes plugin loader once at startup. ``ctx`` is a PluginContext
    instance (hermes_cli.plugins.PluginContext). It exposes ``register_hook``.

    Side effect: installs a RuntimePool whose factory uses ``real_run_agent``
    (live LLM thin client) instead of the unit-test echo stub. Without this,
    Bot replies would be the echo string, not real model output.
    """
    from .agent_real import real_run_agent
    from .pool import RuntimePool
    from .router import override_pool
    from .runtime import ProfileRuntime

    def _real_factory(profile_name, profile_home):
        return ProfileRuntime(profile_home=profile_home, run_agent_fn=real_run_agent)

    override_pool(RuntimePool(runtime_factory=_real_factory))

    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)


__all__ = ["register", "on_pre_gateway_dispatch"]
