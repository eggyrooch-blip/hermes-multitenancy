"""Multi-profile cron worker for multitenancy.

Hermes-agent's built-in cron ticker scans a single ``<HERMES_HOME>/cron/jobs.json``
file. In multitenancy mode each user has their own profile directory under
``<root>/profiles/<name>/``, and ``_configure_cron_home`` lets each AIAgent
subprocess write reminders to its own profile-default cron path
(``<target_profile>/cron/jobs.json``).

This module starts a single background thread inside the gateway process that
periodically scans every profile under ``<root>/profiles/*/cron/jobs.json``
and reuses hermes-agent's native ``cron.scheduler.tick()`` to dispatch due
jobs. The worker is lazy-started on the first ``pre_gateway_dispatch`` hook
call (when the gateway runner and platform adapters are ready).

Race safety: the worker mutates module-level constants in ``cron.jobs`` to
point at each profile in turn, holds an internal lock for the
patch-then-tick window, and restores the originals in a ``finally`` block.
``cron.scheduler.tick`` already uses a file lock keyed by jobs path, so even
if the gateway's own ticker races on the patched constant the worst case is
a duplicate-tick attempt that resolves into a no-op.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_worker_lock = threading.Lock()
_worker_started = False
_worker_thread: Optional[threading.Thread] = None
_worker_stop: Optional[threading.Event] = None


def ensure_cron_worker_started(gateway: Any) -> None:
    """Start the multi-profile cron worker once, on first hook dispatch.

    No-op when gateway adapters are not yet ready, when no asyncio loop is
    running, or when HERMES_HOME does not look like a multitenancy nested
    layout (``<root>/profiles/<name>``).
    """
    global _worker_started, _worker_thread, _worker_stop
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
        profiles_root = _resolve_profiles_root()
        if profiles_root is None:
            return
        _worker_stop = threading.Event()
        _worker_thread = threading.Thread(
            target=_multiprofile_cron_worker,
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


def _resolve_profiles_root() -> Optional[Path]:
    hermes_home = os.environ.get("HERMES_HOME")
    if not hermes_home:
        logger.info("[multitenancy] cron worker: HERMES_HOME unset; skipping")
        return None
    profile_home = Path(hermes_home).expanduser()
    if profile_home.parent.name != "profiles":
        logger.info(
            "[multitenancy] cron worker: HERMES_HOME=%s not in profiles/<name> layout; skipping",
            profile_home,
        )
        return None
    return profile_home.parent


def _multiprofile_cron_worker(
    profiles_root: Path,
    adapters: Any,
    loop: asyncio.AbstractEventLoop,
    stop_event: threading.Event,
    interval: int = 60,
) -> None:
    try:
        import cron.jobs as cron_jobs
        from cron.scheduler import tick as cron_tick
    except Exception:
        logger.exception("[multitenancy] cron modules not importable; worker aborted")
        return

    patch_lock = threading.Lock()
    while not stop_event.is_set():
        try:
            for profile_dir in sorted(profiles_root.iterdir()):
                if not profile_dir.is_dir():
                    continue
                jobs_file = profile_dir / "cron" / "jobs.json"
                if not jobs_file.exists():
                    continue
                _tick_one_profile(
                    cron_jobs,
                    cron_tick,
                    profile_dir,
                    jobs_file,
                    adapters,
                    loop,
                    patch_lock,
                )
        except Exception:
            logger.exception("[multitenancy] cron worker scan error")
        stop_event.wait(timeout=interval)
    logger.info("[multitenancy] cron worker stopped")


def _tick_one_profile(
    cron_jobs: Any,
    cron_tick: Any,
    profile_dir: Path,
    jobs_file: Path,
    adapters: Any,
    loop: asyncio.AbstractEventLoop,
    patch_lock: threading.Lock,
) -> None:
    with patch_lock:
        saved = (
            cron_jobs.HERMES_DIR,
            cron_jobs.CRON_DIR,
            cron_jobs.JOBS_FILE,
            cron_jobs.OUTPUT_DIR,
        )
        try:
            cron_jobs.HERMES_DIR = profile_dir.resolve()
            cron_jobs.CRON_DIR = cron_jobs.HERMES_DIR / "cron"
            cron_jobs.JOBS_FILE = jobs_file
            cron_jobs.OUTPUT_DIR = cron_jobs.CRON_DIR / "output"
            cron_tick(verbose=False, adapters=adapters, loop=loop)
        except Exception:
            logger.exception(
                "[multitenancy] cron tick failed for profile %s",
                profile_dir.name,
            )
        finally:
            (
                cron_jobs.HERMES_DIR,
                cron_jobs.CRON_DIR,
                cron_jobs.JOBS_FILE,
                cron_jobs.OUTPUT_DIR,
            ) = saved
