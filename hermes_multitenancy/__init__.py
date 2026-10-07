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

import importlib
import logging
import sys

logger = logging.getLogger(__name__)

# Importing ANY hermes_multitenancy submodule executes this package __init__
# first, so an eager import here drags 187 of the package's 225 files into
# every test's import closure and makes affected-test selection useless.
# Everything package-internal is therefore resolved lazily; audit in
# .ftask/mt-lazy-plugin-imports/SPEC.md confirms none of these modules has an
# import-time side effect (no thread start, no global registry write).
_LAZY_ATTRS = {
    "_register": ".plugin_entry",
    "_register_ctx_surface": ".plugin_entry",
    "_register_optional_vod_image_gen_provider": ".plugin_entry",
    "_start_credential_renewal_subsystem": ".plugin_entry",
    "_dispatch_with_worker_init": ".plugin_entry",
    "_build_runtime_pool": ".plugin_entry",
    "_env_int": ".plugin_entry",
    "_env_float": ".plugin_entry",
    "on_pre_gateway_dispatch": ".router",
    "install_gateway_ownership_guard": ".gateway_ownership",
    "is_router_profile_runtime": ".gateway_ownership",
    "may_own_cron_runtime": ".gateway_ownership",
    "ensure_cron_worker_started": ".cron_worker",
    "install_cron_runtime_patches": ".cron_worker",
    "install_gateway_startup_watcher": ".cron_worker",
    "install_profile_native_cron_guard": ".cron_worker",
    "run_startup_audit": ".credential_audit",
    "ensure_renewal_worker_started": ".credential_renewal_worker",
}


# The submodules that were package ATTRIBUTES before this file went lazy (a
# top-level `from .cron_worker import x` binds `cron_worker` on the package as
# a side effect). Allowlisted, not open-ended: auto-importing any name that
# happens to match a file would let a `hasattr` probe pull in an optional
# module like tencent_vod_image_gen and raise ModuleNotFoundError where the
# eager version raised AttributeError.
_LAZY_SUBMODULES = frozenset(
    {
        "cron_worker",
        "credential_audit",
        "credential_renewal_worker",
        "gateway_ownership",
        "router",
        "webui_broker_server",
    }
)


def __getattr__(name: str):
    """PEP 562 — resolve re-exports and submodules on first attribute access."""
    module = _LAZY_ATTRS.get(name)
    if module is not None:
        return getattr(importlib.import_module(module, __name__), name)
    if name in _LAZY_SUBMODULES:
        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _self():
    """This module as an object.

    ``__getattr__`` above only fires for ``pkg.attr`` access, never for a bare
    global-name lookup inside a function — and tests monkeypatch names ON this
    package (``monkeypatch.setattr(hermes_multitenancy, "may_own_cron_runtime",
    ...)``). Going through the module object keeps both working; a function-local
    ``from .x import y`` would shadow the patch.
    """
    return sys.modules[__name__]


# Process-wide owner of the MT patches, kept on ``sys`` so every copy of this
# package sees it.  core 0.21.x builds one PluginManager per HERMES_HOME; while
# MT has HERMES_HOME pointed at a tenant, a discover loads a second copy of MT
# through the entry point (top-level ``hermes_multitenancy`` instead of the
# directory plugin name).  That copy must not re-patch process-global Feishu /
# gateway classes: its admitter would replace the live one and every inbound
# would fail the first copy's seal check.
_REGISTERED_MODULE_ATTR = "_hermes_multitenancy_registered_module"
# The PluginManager that ran the full registration.  With the entry point as the
# only loader, a tenant home's manager imports the SAME module name, so the
# module name alone cannot tell "force re-discover of the owner" from "another
# home's manager"; the manager object can.  Managers, not homes, own the hook
# lists, are cached for the life of the process, and need no path resolution.
_REGISTERED_MANAGER_ATTR = "_hermes_multitenancy_registered_manager"


def register(ctx) -> None:
    """Register the production isolation boundary or terminate startup."""
    owner = getattr(sys, _REGISTERED_MODULE_ATTR, None)
    manager = getattr(ctx, "_manager", None)
    if owner is not None and owner != __name__:
        _register_duplicate_copy(ctx, owner)
        return
    if owner is not None:
        first_manager = getattr(sys, _REGISTERED_MANAGER_ATTR, None)
        if manager is not None and first_manager is not None and manager is not first_manager:
            _register_duplicate_copy(ctx, owner)
            return
    setattr(sys, _REGISTERED_MODULE_ATTR, __name__)
    setattr(sys, _REGISTERED_MANAGER_ATTR, manager)
    try:
        _self()._register(ctx)
    except Exception as exc:
        if getattr(sys, _REGISTERED_MODULE_ATTR, None) == __name__:
            delattr(sys, _REGISTERED_MODULE_ATTR)
            if hasattr(sys, _REGISTERED_MANAGER_ATTR):
                delattr(sys, _REGISTERED_MANAGER_ATTR)
        # The host treats plugin registration errors as optional.  Converting a
        # multitenancy failure to SystemExit keeps production fail-closed
        # without changing hermes-agent.
        logger.critical("[multitenancy] required plugin registration failed: %s", type(exc).__name__)
        raise SystemExit(1) from None


def _register_duplicate_copy(ctx, owner: str) -> None:
    """Give another home's manager the owner's hooks, without patching anything.

    Hooks are looked up on the manager of the *current* home, so a manager
    without MT's ``pre_gateway_dispatch`` would let an inbound handled while
    HERMES_HOME points at a tenant skip MT routing.  Delegating to the owner
    copy keeps one set of patches and one set of callbacks.
    """
    owner_module = sys.modules.get(owner)
    if owner_module is None:
        logger.warning(
            "[multitenancy] duplicate load as %s but owner %s is not loaded; nothing registered",
            __name__, owner,
        )
        return
    logger.warning(
        "[multitenancy] already registered as %s; duplicate load as %s only shares its hooks",
        owner, __name__,
    )
    owner_module._register_ctx_surface(ctx)


__all__ = ["register", "on_pre_gateway_dispatch", "_build_runtime_pool"]
