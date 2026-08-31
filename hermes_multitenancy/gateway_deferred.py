"""Deadlock-free installation of patches that depend on ``GatewayRunner``."""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_pending: dict[str, tuple[Callable[[Any], None], bool]] = {}
_installed: set[str] = set()
_inflight: set[str] = set()
_failed: set[str] = set()
_worker: threading.Thread | None = None


def _gateway_runner() -> Any | None:
    module = sys.modules.get("gateway.run")
    return getattr(module, "GatewayRunner", None) if module is not None else None


def _apply_ready() -> None:
    runner = _gateway_runner()
    if runner is None:
        return
    with _lock:
        callbacks = list(_pending.items())
        for name, _callback in callbacks:
            _pending.pop(name, None)
            _inflight.add(name)
    for name, (callback, required) in callbacks:
        try:
            callback(runner)
        except Exception:
            logger.exception("[multitenancy] deferred gateway patch %s failed", name)
            with _lock:
                _inflight.discard(name)
                _failed.add(name)
            if required:
                logger.critical(
                    "[multitenancy] required deferred gateway patch %s failed; "
                    "terminating startup",
                    name,
                )
                _abort_gateway_startup()
        else:
            with _lock:
                _inflight.discard(name)
                _installed.add(name)


def _wait_for_gateway_runner() -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        _apply_ready()
        with _lock:
            if not _pending:
                return
        time.sleep(0.01)
    with _lock:
        names = sorted(_pending)
    if names:
        logger.error(
            "[multitenancy] GatewayRunner never became ready; deferred patches remain: %s",
            ", ".join(names),
        )


def install_when_gateway_runner_ready(
    name: str,
    callback: Callable[[Any], None],
    *,
    repeat_when_ready: bool = False,
    required: bool = False,
) -> bool:
    """Install now when safe, otherwise poll ``sys.modules`` without importing.

    Importing ``gateway.run`` here is unsafe: Hermes may call plugin registration
    while that module is still initializing on another loader thread.
    """
    global _worker
    with _lock:
        already_installed = name in _installed
        previously_failed = name in _failed
        if previously_failed and not repeat_when_ready:
            return True
    if (already_installed or previously_failed) and repeat_when_ready:
        callback(_gateway_runner())
        return True
    if already_installed:
        return True
    with _lock:
        if name not in _inflight:
            _pending.setdefault(name, (callback, required))

    _apply_ready()
    with _lock:
        if name in _installed:
            return True
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(
                target=_wait_for_gateway_runner,
                name="multitenancy-gateway-patches",
                daemon=True,
            )
            _worker.start()
    return False


def _abort_gateway_startup() -> None:
    """Terminate the host process when a required late boundary cannot install."""
    os.kill(os.getpid(), signal.SIGTERM)
