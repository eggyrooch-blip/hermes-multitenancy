"""Multi-profile scan/submit/finalize cron orchestration.

Split out of the historical ``cron_worker`` god-node. Pure move + re-export:
``cron_worker`` re-exports every symbol so import paths and ``monkeypatch.setattr(
cron_worker, ...)`` targets are unchanged. Cross-module and monkeypatched helpers
are reached through the ``cron_worker`` shim (``_cw``) so patches applied to
``cron_worker.<name>`` are honoured by callers that live in a sibling module.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
from concurrent.futures import TimeoutError as FuturesTimeout
import functools
import json
import math
import signal
import logging
import os
import re
import subprocess
import sqlite3
import sys
import threading
import time
import uuid
import copy
from urllib import error as urllib_error
from urllib import request as urllib_request
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None

from .. import cron_worker as _cw

# Keep the historical logger name so log records are attributed to
# ``hermes_multitenancy.cron_worker`` exactly as before the split.
logger = logging.getLogger("hermes_multitenancy.cron_worker")



_cron_module_patch_lock = threading.Lock()
_cron_in_flight_lock = threading.Lock()


def _job_belongs_to_this_gateway(job: dict) -> bool:
    source_app = str(job.get("source_app") or "").strip()

    from ..expert_bot_route import fixed_expert_id_from_env, fixed_expert_app_id_from_env

    if not fixed_expert_id_from_env():
        # Router / per-user gateway: owns only untagged jobs.
        return not source_app
    # Expert gateway: match the app id from the PROCESS env (deploy contract).
    # Never from a profile file — the scan rebinds HERMES_HOME to each scanned
    # USER profile, so a profile-file lookup would read the wrong app. Missing
    # app-id env → matches nothing (fail-closed misconfig, not misroute).
    owned_app = fixed_expert_app_id_from_env()
    return bool(source_app) and bool(owned_app) and source_app == owned_app


def _release_cron_run_claim(cron_jobs: Any, cron_scheduler: Any, job: dict) -> bool:
    """Clear the one-shot ``run_claim`` core stamped on a job we then rejected.

    Core ``get_due_jobs()`` stamps a ``run_claim`` on every due ``once`` job
    BEFORE returning it, so the ownership filter always runs on an
    already-claimed job. A claim we abandon is not re-scanned by core until it
    ages past ``ONESHOT_RUN_CLAIM_TTL_SECONDS`` (1800s), which is longer than
    the one-shot's whole life: the gateway that DOES own the job never sees it
    as due and it silently never fires. Releasing in the same tick hands it
    back immediately.

    Compare-and-clear under the jobs lock (same shape as core's
    ``heartbeat_run_claim``) so a claim another process has since taken over is
    left alone. Never raises — a failed release costs one TTL, an exception
    here would cost the rest of the profile's scan.
    """
    claim = job.get("run_claim")
    if not isinstance(claim, dict) or job.get("schedule", {}).get("kind") != "once":
        return False  # only one-shots are ever claimed by the due scan
    job_id = str(job.get("id") or "").strip()
    try:
        load_jobs = _cw._cron_scheduler_function(cron_jobs, cron_scheduler, "load_jobs")
        save_jobs = _cw._cron_scheduler_function(cron_jobs, cron_scheduler, "save_jobs")
        # save_jobs() takes the lock itself (re-entrant); holding it across the
        # whole load→modify→save is what keeps the read-modify-write atomic
        # against a concurrent tick. Cores without the private lock helper
        # degrade to save_jobs()' own locking.
        jobs_lock = getattr(cron_jobs, "_jobs_lock", None)
        with jobs_lock() if callable(jobs_lock) else contextlib.nullcontext():
            jobs = load_jobs()
            for stored in jobs:
                if str(stored.get("id") or "").strip() != job_id:
                    continue
                if stored.get("run_claim") != claim:
                    return False  # re-claimed or already cleared elsewhere
                stored["run_claim"] = None
                save_jobs(jobs)
                return True
    except Exception:
        logger.exception(
            "[multitenancy] cron run_claim release failed job=%s", job_id
        )
    return False


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


def _active_cron_profiles(profiles_root: Path) -> Optional[set[str]]:
    """Return active routed profile names, or None when no routing DB exists."""
    db_path = profiles_root.parent / "multitenancy.db"
    if not db_path.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
            rows = conn.execute(
                "SELECT DISTINCT profile_name FROM multitenancy_routing "
                "WHERE active = 1 AND profile_name IS NOT NULL AND profile_name != ''"
            ).fetchall()
    except Exception:
        logger.exception("[multitenancy] cron worker failed to read active routing profiles")
        return None
    return {str(row[0]) for row in rows if row and row[0]}


def _multitenancy_cron_worker_count() -> int:
    raw = os.environ.get("HERMES_MULTITENANCY_CRON_WORKERS", "").strip()
    if not raw:
        return 4
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        logger.warning(
            "[multitenancy] invalid HERMES_MULTITENANCY_CRON_WORKERS=%r; using 4",
            raw,
        )
        return 4


@contextlib.contextmanager
def _cron_profile_context(
    cron_jobs: Any,
    cron_scheduler: Any,
    profile_dir: Path,
    jobs_file: Path,
    patch_lock: threading.Lock,
) -> Any:
    with patch_lock:
        saved = (
            cron_jobs.HERMES_DIR,
            cron_jobs.CRON_DIR,
            cron_jobs.JOBS_FILE,
            cron_jobs.OUTPUT_DIR,
            getattr(cron_scheduler, "_hermes_home", None),
            getattr(cron_scheduler, "_LOCK_DIR", None),
            getattr(cron_scheduler, "_LOCK_FILE", None),
            os.environ.get("HERMES_HOME"),
        )
        try:
            profile_home = profile_dir.resolve()
            os.environ["HERMES_HOME"] = str(profile_home)
            cron_jobs.HERMES_DIR = profile_home
            cron_jobs.CRON_DIR = cron_jobs.HERMES_DIR / "cron"
            cron_jobs.JOBS_FILE = jobs_file
            cron_jobs.OUTPUT_DIR = cron_jobs.CRON_DIR / "output"
            cron_scheduler._hermes_home = profile_home
            cron_scheduler._LOCK_DIR = cron_jobs.CRON_DIR
            cron_scheduler._LOCK_FILE = cron_jobs.CRON_DIR / ".tick.lock"
            yield profile_home
        finally:
            (
                cron_jobs.HERMES_DIR,
                cron_jobs.CRON_DIR,
                cron_jobs.JOBS_FILE,
                cron_jobs.OUTPUT_DIR,
                cron_scheduler._hermes_home,
                cron_scheduler._LOCK_DIR,
                cron_scheduler._LOCK_FILE,
                previous_home,
            ) = saved
            if previous_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = previous_home


def _cron_scheduler_function(
    cron_jobs: Any,
    cron_scheduler: Any,
    name: str,
) -> Any:
    func = getattr(cron_scheduler, name, None)
    if callable(func):
        return func
    func = getattr(cron_jobs, name, None)
    if callable(func):
        return func
    raise AttributeError(f"cron scheduler/jobs missing callable {name}")


def _result_field(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


def _finalize_claimed_cron_job_current_context(
    cron_jobs: Any,
    cron_scheduler: Any,
    job: dict,
    result: Any,
    adapters: Any,
    loop: asyncio.AbstractEventLoop,
    *,
    verbose: bool = False,
) -> bool:
    mark_job_run = _cw._cron_scheduler_function(cron_jobs, cron_scheduler, "mark_job_run")
    try:
        save_job_output = _cw._cron_scheduler_function(cron_jobs, cron_scheduler, "save_job_output")
        deliver_result = getattr(cron_scheduler, "_deliver_result")
        summarize_failure = getattr(
            cron_scheduler,
            "_summarize_cron_failure_for_delivery",
            lambda _job, error: f"⚠️ Cron '{_job.get('name') or _job.get('id')}' failed: {error}",
        )

        success = bool(_cw._result_field(result, "success", False))
        output = str(_cw._result_field(result, "output", "") or "")
        final_response = str(_cw._result_field(result, "final_response", "") or "")
        raw_error = _cw._result_field(result, "error", None)
        error = None if raw_error is None else str(raw_error)

        output_file = save_job_output(job["id"], output)
        if verbose:
            logger.info("[multitenancy] cron output saved to: %s", output_file)

        deliver_content = final_response if success else summarize_failure(job, error)
        should_deliver = bool(str(deliver_content or "").strip())
        silent_marker = str(getattr(cron_scheduler, "SILENT_MARKER", "[SILENT]")).upper()
        if should_deliver and success and silent_marker in str(deliver_content).strip().upper():
            logger.info(
                "[multitenancy] cron job '%s': agent returned %s — skipping delivery",
                job["id"],
                silent_marker,
            )
            should_deliver = False

        delivery_error = None
        if should_deliver:
            try:
                delivery_error = deliver_result(job, deliver_content, adapters=adapters, loop=loop)
            except Exception as de:
                delivery_error = str(de)
                logger.error("[multitenancy] cron delivery failed for job %s: %s", job["id"], de)

        if success and not final_response.strip():
            success = False
            error = "Agent completed but produced empty response (model error, timeout, or misconfiguration)"

        mark_job_run(job["id"], success, error, delivery_error=delivery_error)
        return True
    except Exception as exc:
        logger.error("[multitenancy] error finalizing cron job %s: %s", job.get("id"), exc)
        try:
            mark_job_run(job["id"], False, str(exc))
        except Exception:
            logger.exception("[multitenancy] failed to mark cron job %s after finalize error", job.get("id"))
        return False


def _finalize_claimed_cron_job(
    cron_jobs: Any,
    cron_scheduler: Any,
    profile_dir: Path,
    jobs_file: Path,
    job: dict,
    result: Any,
    adapters: Any,
    loop: asyncio.AbstractEventLoop,
    patch_lock: threading.Lock,
    *,
    verbose: bool = False,
) -> bool:
    with _cw._cron_profile_context(cron_jobs, cron_scheduler, profile_dir, jobs_file, patch_lock):
        return _cw._finalize_claimed_cron_job_current_context(
            cron_jobs,
            cron_scheduler,
            job,
            result,
            adapters,
            loop,
            verbose=verbose,
        )


def _complete_claimed_cron_job(
    future: concurrent.futures.Future,
    *,
    cron_jobs: Any,
    cron_scheduler: Any,
    profile_dir: Path,
    jobs_file: Path,
    job: dict,
    adapters: Any,
    loop: asyncio.AbstractEventLoop,
    patch_lock: threading.Lock,
    in_flight: set[tuple[str, str]],
    key: tuple[str, str],
    in_flight_lock: threading.Lock,
    profile_claim: Optional[_cw._ProfileTickClaim] = None,
) -> None:
    try:
        try:
            result = future.result()
        except Exception as exc:
            logger.exception(
                "[multitenancy] cron isolated runner failed profile=%s job=%s",
                profile_dir.name,
                job.get("id", "?"),
            )
            result = {
                "success": False,
                "output": (
                    f"# Cron Job: {job.get('name') or job.get('id') or 'scheduled task'}\n\n"
                    f"**Job ID:** {job.get('id') or ''}\n"
                    f"**Run Path:** isolated subprocess\n\n"
                    f"Error: {exc}"
                ),
                "final_response": "",
                "error": str(exc),
            }
        _cw._finalize_claimed_cron_job(
            cron_jobs,
            cron_scheduler,
            profile_dir,
            jobs_file,
            job,
            result,
            adapters,
            loop,
            patch_lock,
        )
    finally:
        with in_flight_lock:
            in_flight.discard(key)
        if profile_claim is not None:
            profile_claim.future_done()


def _scan_and_submit_due_profile_jobs(
    cron_jobs: Any,
    cron_scheduler: Any,
    profiles_root: Path,
    active_profiles: Optional[set[str]],
    *,
    adapters: Any,
    loop: asyncio.AbstractEventLoop,
    patch_lock: threading.Lock,
    executor: concurrent.futures.Executor,
    in_flight: set[tuple[str, str]],
    runner: Any = None,
    in_flight_lock: Optional[threading.Lock] = None,
) -> int:
    runner = runner or _cw._run_job_for_profile_subprocess
    in_flight_lock = in_flight_lock or _cron_in_flight_lock
    submitted = 0
    get_due_jobs = _cw._cron_scheduler_function(cron_jobs, cron_scheduler, "get_due_jobs")
    advance_next_run = _cw._cron_scheduler_function(cron_jobs, cron_scheduler, "advance_next_run")

    for profile_dir in sorted(profiles_root.iterdir()):
        if not profile_dir.is_dir():
            continue
        if active_profiles is not None and profile_dir.name not in active_profiles:
            continue
        jobs_file = profile_dir / "cron" / "jobs.json"
        if not jobs_file.exists():
            continue
        profile_claim: Optional[_cw._ProfileTickClaim] = None
        callback_specs: list[
            tuple[
                concurrent.futures.Future,
                Path,
                Path,
                dict,
                tuple[str, str],
                _cw._ProfileTickClaim,
            ]
        ] = []
        try:
            _cw.backfill_cron_owner_context_for_profile(profile_dir)
            with _cw._cron_profile_context(cron_jobs, cron_scheduler, profile_dir, jobs_file, patch_lock):
                tick_lock = _cw._acquire_cron_tick_file_lock(cron_jobs, cron_scheduler)
                if tick_lock is None:
                    logger.debug(
                        "[multitenancy] cron tick skipped for profile %s; lock held",
                        profile_dir.name,
                    )
                    continue
                profile_claim = _cw._ProfileTickClaim(tick_lock)
                due_jobs = list(get_due_jobs())
                for job in due_jobs:
                    job_id = str(job.get("id") or "").strip()
                    if not job_id:
                        logger.warning(
                            "[multitenancy] cron due job without id skipped profile=%s",
                            profile_dir.name,
                        )
                        continue
                    # Owning gateway both executes and later finalizes/delivers
                    # through its live Feishu adapter, so non-owners must skip
                    # before advance_next_run/in_flight to avoid double-run.
                    # ponytail: shared profile tick locks are enough today; add
                    # per-app sub-locks only if mixed router/expert scans ever
                    # show starvation under real load.
                    if not _cw._job_belongs_to_this_gateway(job):
                        # get_due_jobs() already claimed this one-shot for us;
                        # hand it straight back or the owning gateway is locked
                        # out of it for the whole claim TTL.
                        _cw._release_cron_run_claim(cron_jobs, cron_scheduler, job)
                        logger.debug(
                            "[multitenancy] cron due job owned by another gateway profile=%s job=%s source_app=%s",
                            profile_dir.name,
                            job_id,
                            str(job.get("source_app") or "").strip(),
                        )
                        continue
                    key = (profile_dir.name, job_id)
                    with in_flight_lock:
                        if key in in_flight:
                            logger.info(
                                "[multitenancy] cron job already running — skipping profile=%s job=%s",
                                profile_dir.name,
                                job.get("name") or job_id,
                            )
                            continue
                        in_flight.add(key)
                    claimed_job = copy.deepcopy(job)
                    try:
                        advance_next_run(job_id)
                        future = executor.submit(runner, profile_dir.resolve(), copy.deepcopy(claimed_job))
                    except Exception:
                        with in_flight_lock:
                            in_flight.discard(key)
                        error = "cron submit failed: executor down"
                        exc = sys.exc_info()[1]
                        if exc is not None:
                            error = f"cron submit failed: {exc}"
                        _cw._finalize_claimed_cron_job_current_context(
                            cron_jobs,
                            cron_scheduler,
                            claimed_job,
                            {
                                "success": False,
                                "output": (
                                    f"# Cron Job: {claimed_job.get('name') or job_id}\n\n"
                                    f"**Job ID:** {job_id}\n"
                                    f"**Run Path:** isolated subprocess\n\n"
                                    f"Error: {error}"
                                ),
                                "final_response": "",
                                "error": error,
                            },
                            adapters,
                            loop,
                        )
                        logger.exception(
                            "[multitenancy] cron submit failed profile=%s job=%s",
                            profile_dir.name,
                            job_id,
                        )
                        continue
                    profile_claim.add_future()
                    callback_specs.append(
                        (
                            future,
                            profile_dir,
                            jobs_file,
                            claimed_job,
                            key,
                            profile_claim,
                        )
                    )
                    submitted += 1
        except Exception:
            logger.exception(
                "[multitenancy] cron claim failed for profile %s",
                profile_dir.name,
            )
        finally:
            try:
                for future, callback_profile_dir, callback_jobs_file, callback_job, key, claim in callback_specs:
                    future.add_done_callback(
                        lambda fut,
                        profile_dir=callback_profile_dir,
                        jobs_file=callback_jobs_file,
                        job=callback_job,
                        key=key,
                        profile_claim=claim: _cw._complete_claimed_cron_job(
                            fut,
                            cron_jobs=cron_jobs,
                            cron_scheduler=cron_scheduler,
                            profile_dir=profile_dir,
                            jobs_file=jobs_file,
                            job=job,
                            adapters=adapters,
                            loop=loop,
                            patch_lock=patch_lock,
                            in_flight=in_flight,
                            key=key,
                            in_flight_lock=in_flight_lock,
                            profile_claim=profile_claim,
                        )
                    )
            finally:
                if profile_claim is not None:
                    profile_claim.close_submissions()
    return submitted


def _multiprofile_cron_worker(
    profiles_root: Path,
    adapters: Any,
    loop: asyncio.AbstractEventLoop,
    stop_event: threading.Event,
    interval: int = 60,
) -> None:
    try:
        import cron.jobs as cron_jobs
        import cron.scheduler as cron_scheduler
    except Exception:
        logger.exception("[multitenancy] cron modules not importable; worker aborted")
        return

    worker_count = _cw._multitenancy_cron_worker_count()
    in_flight: set[tuple[str, str]] = set()
    logger.info(
        "[multitenancy] cron cross-profile dispatcher started workers=%d",
        worker_count,
    )
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="multitenancy-cron",
    ) as executor:
        while not stop_event.is_set():
            try:
                active_profiles = _cw._active_cron_profiles(profiles_root)
                _cw._scan_and_submit_due_profile_jobs(
                    cron_jobs,
                    cron_scheduler,
                    profiles_root,
                    active_profiles,
                    adapters=adapters,
                    loop=loop,
                    patch_lock=_cron_module_patch_lock,
                    executor=executor,
                    in_flight=in_flight,
                    in_flight_lock=_cron_in_flight_lock,
                )
            except Exception:
                logger.exception("[multitenancy] cron worker scan error")
            stop_event.wait(timeout=interval)
    logger.info("[multitenancy] cron worker stopped")


def _tick_one_profile(
    cron_jobs: Any,
    cron_scheduler: Any,
    profile_dir: Path,
    jobs_file: Path,
    adapters: Any,
    loop: asyncio.AbstractEventLoop,
    patch_lock: threading.Lock,
) -> None:
    try:
        with _cw._cron_profile_context(cron_jobs, cron_scheduler, profile_dir, jobs_file, patch_lock):
            cron_scheduler.tick(verbose=False, adapters=adapters, loop=loop)
    except Exception:
        logger.exception(
            "[multitenancy] cron tick failed for profile %s",
            profile_dir.name,
        )
