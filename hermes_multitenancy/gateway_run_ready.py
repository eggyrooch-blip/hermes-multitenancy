"""Apply MT's ``gateway.run`` patches once core has finished loading that module.

Why this exists (core 0.21.4, production spinning disk, 2026-09-24): core runs
plugin discovery on a background thread that holds ``PluginManager._discovery_lock``
while it calls MT ``register``.  The gateway main thread is meanwhile importing
``gateway.run``, whose module body reaches plugin discovery and waits for that
lock while holding the ``gateway.run`` module import lock.  A synchronous
``from gateway.run import GatewayRunner`` inside ``register`` then waits for the
module lock: two threads wait on each other forever.  Python's import deadlock
detection only sees module locks, not the discovery RLock, so it never fires.

So register never imports ``gateway.run``.  Callbacks run:

* immediately, when ``gateway.run`` is already fully loaded (legacy timing);
* otherwise (not imported yet, or still initializing) right after its module body
  finishes, in the importing thread, before the importer receives the module.

The completion signal is the import system binding the submodule on its parent:
``_find_and_load_unlocked`` runs ``setattr(sys.modules["gateway"], "run", module)``
after ``_load_unlocked`` returns and before the import statement completes
(CPython 3.11-3.13).  The ``gateway`` package's class is swapped for a subclass
whose ``__setattr__`` runs the pending callbacks at that moment, so every
``GatewayRunner(...)`` sees the patches.  While they run, ``gateway.run`` is
marked initializing again so concurrent importers wait on its module lock
instead of taking the unpatched module.  A failing callback makes the
``gateway.run`` import fail and drops the unpatched module from ``sys.modules``
(fail-closed; ``AttributeError`` is re-raised as ``RuntimeError`` because the
import system swallows ``AttributeError`` from that ``setattr``).
"""
from __future__ import annotations

import importlib.util
import logging
import sys
import threading
import types
from typing import Callable

logger = logging.getLogger(__name__)

GATEWAY_PACKAGE = "gateway"
GATEWAY_RUN = "gateway.run"

Callback = Callable[[types.ModuleType], None]

_lock = threading.RLock()
_pending: list[tuple[str, Callback]] = []
_hook_class: type | None = None
_original_class: type | None = None


class GatewayRunPatchError(RuntimeError):
    """A deferred ``gateway.run`` patch failed; the gateway must not start."""


def _is_initializing(module: types.ModuleType) -> bool:
    return bool(getattr(getattr(module, "__spec__", None), "_initializing", False))


def gateway_run_state() -> str:
    """``"loaded"``, ``"initializing"`` or ``"absent"`` for ``gateway.run``."""
    if GATEWAY_RUN not in sys.modules:
        return "absent"
    module = sys.modules[GATEWAY_RUN]
    if module is None:
        raise ModuleNotFoundError(f"import of {GATEWAY_RUN} halted; None in sys.modules", name=GATEWAY_RUN)
    return "initializing" if _is_initializing(module) else "loaded"


def when_gateway_run_loaded(key: str, callback: Callback) -> bool:
    """Run ``callback(gateway.run)`` now or as soon as ``gateway.run`` finishes loading.

    Returns ``True`` when the callback ran synchronously (its exceptions propagate
    to the caller, as the old synchronous import did), ``False`` when deferred.
    Raises ``ModuleNotFoundError`` when ``gateway.run`` cannot be imported at all.
    A deferred ``key`` that is already pending is not queued twice.
    """
    state = gateway_run_state()
    if state == "loaded":
        callback(sys.modules[GATEWAY_RUN])
        return True
    if state == "absent" and importlib.util.find_spec(GATEWAY_RUN) is None:
        raise ModuleNotFoundError(f"No module named {GATEWAY_RUN!r}", name=GATEWAY_RUN)
    with _lock:
        if all(existing != key for existing, _ in _pending):
            _pending.append((key, callback))
        _arm_package_hook()
        # gateway.run may have finished between the state check and arming; the
        # bind already happened then, so nothing else will run the queue.
        if gateway_run_state() == "loaded":
            _run_pending(sys.modules[GATEWAY_RUN])
            return True
    logger.info("[multitenancy] deferred %s until %s finishes loading (state=%s)", key, GATEWAY_RUN, state)
    return False


def pending_keys() -> list[str]:
    with _lock:
        return [key for key, _ in _pending]


def _run_pending(module: types.ModuleType) -> None:
    """Run every queued callback in registration order (caller holds ``_lock``).

    The queue is cleared only after the whole batch succeeded.  On a failure the
    full batch stays queued (earlier successes included) and the unpatched module
    is dropped, so a retried import re-executes ``gateway.run`` and re-applies
    every patch to the fresh objects, or keeps failing closed.
    """
    for key, callback in list(_pending):
        try:
            callback(module)
        except BaseException as exc:
            logger.critical("[multitenancy] deferred gateway.run patch %s failed: %s", key, type(exc).__name__)
            if sys.modules.get(GATEWAY_RUN) is module:
                del sys.modules[GATEWAY_RUN]
            error = f"gateway_run_patch_failed:{key}"
            _poison(module, error)
            raise GatewayRunPatchError(error) from exc
    _pending.clear()
    _disarm_package_hook()


def _poison(module: types.ModuleType, error: str) -> None:
    """Make the half-patched module unusable for anyone already holding it.

    An importer that waited on the module lock (C fast path) keeps the object it
    looked up before the failure instead of re-importing, so dropping it from
    ``sys.modules`` alone is not enough.
    """
    base = type(module)

    def __getattribute__(self, name):  # noqa: N807 - module dunder hook
        if name.startswith("__") and name.endswith("__"):
            return base.__getattribute__(self, name)
        raise GatewayRunPatchError(error)

    try:
        module.__class__ = type(
            "_UnpatchedGatewayRun", (base,), {"__getattribute__": __getattribute__, "__module__": __name__}
        )
    except TypeError:
        logger.critical("[multitenancy] could not poison unpatched gateway.run")


def _run_pending_unpublished(module: types.ModuleType) -> None:
    """Run the queue while other importers still treat ``gateway.run`` as loading.

    The parent bind runs inside ``_find_and_load`` with the ``gateway.run``
    module lock held, but ``_load_unlocked`` has already cleared
    ``__spec__._initializing``, so a concurrent ``from gateway.run import ...``
    would take the fast path and get the unpatched class.  Re-marking the spec
    as initializing sends those importers to the module lock, which this
    thread releases only after the patches ran (or after a failure dropped the
    module, in which case they re-import and re-run the queue).
    """
    spec = getattr(module, "__spec__", None)
    if spec is not None:
        spec._initializing = True
    try:
        _run_pending(module)
    finally:
        if spec is not None:
            spec._initializing = False


def _arm_package_hook() -> None:
    """Swap the ``gateway`` package class for one that sees ``gateway.run`` bind."""
    global _hook_class, _original_class
    package = sys.modules.get(GATEWAY_PACKAGE)
    if package is None:
        raise ModuleNotFoundError(f"No module named {GATEWAY_PACKAGE!r}", name=GATEWAY_PACKAGE)
    if _hook_class is not None and isinstance(package, _hook_class):
        return
    base = type(package)

    def __setattr__(self, name, value):  # noqa: N807 - module dunder hook
        if (
            name == "run"
            and isinstance(value, types.ModuleType)
            and getattr(value, "__name__", None) == GATEWAY_RUN
            and not _is_initializing(value)
        ):
            with _lock:
                if _pending:
                    _run_pending_unpublished(value)
        base.__setattr__(self, name, value)

    hook = type("_MultitenancyGatewayPackage", (base,), {"__setattr__": __setattr__, "__module__": __name__})
    package.__class__ = hook
    _hook_class, _original_class = hook, base


def _disarm_package_hook() -> None:
    global _hook_class, _original_class
    package = sys.modules.get(GATEWAY_PACKAGE)
    if package is not None and _hook_class is not None and type(package) is _hook_class:
        package.__class__ = _original_class
    _hook_class, _original_class = None, None
