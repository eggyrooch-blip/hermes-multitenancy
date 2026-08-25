"""Tick file-lock primitives + isolated subprocess job execution.

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
from ..expert_bot_route import READONLY_ENV

# Keep the historical logger name so log records are attributed to
# ``hermes_multitenancy.cron_worker`` exactly as before the split.
logger = logging.getLogger("hermes_multitenancy.cron_worker")



class _CronTickFileLock:
    def __init__(self, lock_fd: Any) -> None:
        self._lock_fd = lock_fd
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            if fcntl is not None:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows-only
                msvcrt.locking(self._lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
        except (OSError, IOError):
            pass
        self._lock_fd.close()


class _ProfileTickClaim:
    def __init__(self, tick_lock: Any) -> None:
        self._tick_lock = tick_lock
        self._lock = threading.Lock()
        self._remaining = 0
        self._submitting = True
        self._released = False

    def add_future(self) -> None:
        with self._lock:
            self._remaining += 1

    def future_done(self) -> None:
        with self._lock:
            if self._remaining > 0:
                self._remaining -= 1
            self._release_if_ready_locked()

    def close_submissions(self) -> None:
        with self._lock:
            self._submitting = False
            self._release_if_ready_locked()

    def _release_if_ready_locked(self) -> None:
        if self._released or self._submitting or self._remaining > 0:
            return
        self._released = True
        self._tick_lock.release()


def _acquire_cron_tick_file_lock(
    cron_jobs: Any,
    cron_scheduler: Any,
) -> Optional[_cw._CronTickFileLock]:
    get_lock_paths = getattr(cron_scheduler, "_get_lock_paths", None)
    if callable(get_lock_paths):
        lock_dir, lock_file = get_lock_paths()
    else:
        lock_dir = Path(getattr(cron_scheduler, "_LOCK_DIR", None) or cron_jobs.CRON_DIR)
        lock_file = Path(getattr(cron_scheduler, "_LOCK_FILE", None) or (lock_dir / ".tick.lock"))
    lock_dir.mkdir(parents=True, exist_ok=True)

    lock_fd = None
    try:
        lock_fd = open(lock_file, "w", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:  # pragma: no cover - Windows-only
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
    except (OSError, IOError):
        if lock_fd is not None:
            lock_fd.close()
        return None
    return _cw._CronTickFileLock(lock_fd)


@contextlib.contextmanager
def _cron_tick_file_lock(cron_jobs: Any, cron_scheduler: Any) -> Any:
    tick_lock = _cw._acquire_cron_tick_file_lock(cron_jobs, cron_scheduler)
    try:
        yield tick_lock is not None
    finally:
        if tick_lock is not None:
            tick_lock.release()


def _run_cron_job_body(job: dict, cron_scheduler: Any) -> tuple[bool, str, str, Optional[str]]:
    deferred = _cw._l4_check_needs_reauth_and_defer(job)
    if deferred is not None:
        return deferred
    if _cw._cron_run_broker_enabled():
        return _cw._run_job_through_broker(job, cron_scheduler)
    if str(job.get("expert_id") or "").strip():
        return False, "", "", "scheduled expert execution requires RunBroker"
    return cron_scheduler.run_job(job)


def _sweep_cron_mcp_orphans() -> None:
    try:
        from tools.mcp_tool import _kill_orphaned_mcp_children

        _kill_orphaned_mcp_children()
    except Exception as exc:
        logger.debug("[multitenancy] cron MCP orphan cleanup failed: %s", exc)


def _run_cron_job_body_with_cleanup(
    job: dict,
    cron_scheduler: Any,
) -> tuple[bool, str, str, Optional[str]]:
    try:
        return _cw._run_cron_job_body(job, cron_scheduler)
    finally:
        _cw._sweep_cron_mcp_orphans()


def _install_cron_subprocess_runtime_pool() -> None:
    """Install the real agent runner without gateway-only plugin side effects."""
    from .. import _build_runtime_pool
    from ..agent_real import real_run_agent
    from ..router import override_pool
    from ..runtime import ProfileRuntime

    def _real_factory(_profile_name: str, profile_home: Path) -> ProfileRuntime:
        return ProfileRuntime(profile_home=profile_home, run_agent_fn=real_run_agent)

    override_pool(_build_runtime_pool(_real_factory))


def _writable_authorized(job: dict) -> bool:
    """Strict read of the create-time write-authorization flag from (possibly
    dirty / hand-edited) persisted job data. Only a real bool True or a canonical
    truthy string authorizes writes — "0"/"false"/""/0/None do NOT (Python
    truthiness would wrongly treat the strings "0"/"false" as authorized)."""
    value = job.get("writable_authorized")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _run_job_for_profile_current_process(profile_home: Path, job: dict) -> dict[str, Any]:
    profile_home = Path(profile_home).expanduser().resolve()
    previous_home = os.environ.get("HERMES_HOME")
    previous_readonly = os.environ.get(READONLY_ENV)
    cron_jobs = None
    cron_scheduler = None
    saved_cron_state: Optional[tuple[Any, ...]] = None
    source_app = str(job.get("source_app") or "").strip()
    os.environ["HERMES_HOME"] = str(profile_home)
    # Writable scheduling is an EXPERT-only privilege: drop the readonly gate only
    # for a job that is BOTH expert-routed (source_app — set only by the create
    # hook, never by backfill) AND create-time authorized. A dirty/untagged job can
    # never opt into writable execution, independent of the upstream partition.
    if source_app and _writable_authorized(job):
        os.environ.pop(READONLY_ENV, None)
    try:
        _cw._install_cron_subprocess_runtime_pool()

        import cron.jobs as cron_jobs_module
        import cron.scheduler as cron_scheduler_module

        cron_jobs = cron_jobs_module
        cron_scheduler = cron_scheduler_module
        saved_cron_state = (
            getattr(cron_jobs, "HERMES_DIR", None),
            getattr(cron_jobs, "CRON_DIR", None),
            getattr(cron_jobs, "JOBS_FILE", None),
            getattr(cron_jobs, "OUTPUT_DIR", None),
            getattr(cron_scheduler, "_hermes_home", None),
            getattr(cron_scheduler, "_LOCK_DIR", None),
            getattr(cron_scheduler, "_LOCK_FILE", None),
        )

        cron_jobs.HERMES_DIR = profile_home
        cron_jobs.CRON_DIR = cron_jobs.HERMES_DIR / "cron"
        cron_jobs.JOBS_FILE = cron_jobs.CRON_DIR / "jobs.json"
        cron_jobs.OUTPUT_DIR = cron_jobs.CRON_DIR / "output"
        cron_scheduler._hermes_home = profile_home
        cron_scheduler._LOCK_DIR = cron_jobs.CRON_DIR
        cron_scheduler._LOCK_FILE = cron_jobs.CRON_DIR / ".tick.lock"

        audit_ok = True
        try:
            from ..security_audit import append_security_event

            # An expert-routed scheduled run is the no-approval writable path; its
            # audit IS the compensating control (SPEC guardrail), so force it to
            # land even under a default-off audit gate. Non-expert user cron keeps
            # the existing gate (byte-identical).
            audit_ok = append_security_event(
                event_type="cron_scheduled_execution",
                point="cron.scheduled",
                open_id=job.get("owner_open_id"),
                profile=job.get("owner_profile"),
                run_id=job.get("id"),
                expert_id=_cw._expert_id_for_cron_job(job, profile_home=profile_home),
                decision="writable" if _writable_authorized(job) else "readonly",
                force=bool(source_app),
            )
        except Exception:
            audit_ok = False
            logger.warning(
                "[multitenancy] cron scheduled execution audit raised for job=%s",
                job.get("id"),
                exc_info=True,
            )
        # Fail closed for expert (source_app) jobs: if the compensating-control
        # audit could not be recorded, refuse to run the writable no-approval body
        # — "可写不上 approval" is only acceptable because every run is audited.
        # Normal user cron (no source_app) is unaffected.
        if source_app and not audit_ok:
            logger.error(
                "[multitenancy] cron scheduled execution BLOCKED for expert job=%s: "
                "compensating audit could not be recorded (fail-closed)",
                job.get("id"),
            )
            return {
                "success": False,
                "output": "",
                "final_response": "",
                "error": "scheduled execution blocked: security audit (compensating control) could not be recorded",
            }
        success, output, final_response, error = _cw._run_cron_job_body_with_cleanup(job, cron_scheduler)
        return {
            "success": bool(success),
            "output": "" if output is None else str(output),
            "final_response": "" if final_response is None else str(final_response),
            "error": None if error is None else str(error),
        }
    except BaseException as exc:
        job_id = str(job.get("id") or "")
        job_name = str(job.get("name") or job_id or "scheduled task")
        error = str(exc)
        return {
            "success": False,
            "output": (
                f"# Cron Job: {job_name}\n\n"
                f"**Job ID:** {job_id}\n"
                f"**Run Path:** isolated subprocess\n\n"
                f"Error: {error}"
            ),
            "final_response": "",
            "error": error,
        }
    finally:
        if cron_jobs is not None and cron_scheduler is not None and saved_cron_state is not None:
            (
                cron_jobs.HERMES_DIR,
                cron_jobs.CRON_DIR,
                cron_jobs.JOBS_FILE,
                cron_jobs.OUTPUT_DIR,
                cron_scheduler._hermes_home,
                cron_scheduler._LOCK_DIR,
                cron_scheduler._LOCK_FILE,
            ) = saved_cron_state
        if previous_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous_home
        if previous_readonly is None:
            os.environ.pop(READONLY_ENV, None)
        else:
            os.environ[READONLY_ENV] = previous_readonly


_CRON_JOB_TIMEOUT_DEFAULT_SECONDS = 1800  # 30min — bounds a hung job; long agent runs still fit
_CRON_JOB_TIMEOUT_MAX_SECONDS = 7200       # 2h hard ceiling — a fat-fingered/huge env can't disable the watchdog


def _cron_job_timeout_seconds() -> float:
    """Wall-clock bound for one isolated cron-job subprocess.

    Without it a single hung job (LLM/network stall, MCP tool deadlock, an
    infinite loop) blocks its worker thread and holds the profile tick-lock
    forever; a handful of such hangs exhaust the ThreadPoolExecutor and halt
    cron delivery for every profile (CRIT-2, audit 2026-07-03). Override via
    HERMES_CRON_JOB_TIMEOUT (seconds).
    """
    raw = os.environ.get("HERMES_CRON_JOB_TIMEOUT", "").strip()
    if raw:
        try:
            val = float(raw)
        except ValueError:
            val = 0.0
        if val > 0 and math.isfinite(val):
            return min(val, float(_CRON_JOB_TIMEOUT_MAX_SECONDS))
    return float(_CRON_JOB_TIMEOUT_DEFAULT_SECONDS)


def _kill_cron_job_process_group(proc: "subprocess.Popen") -> None:
    """Kill the whole job tree (wrapper + any tool/agent grandchildren), not just
    the direct child. On timeout, a hung descendant that inherited the captured
    stdout/stderr pipes would otherwise keep them open and block the worker even
    after the wrapper dies. Falls back to killing the direct process if the group
    is already gone. CRIT-2."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _run_job_for_profile_subprocess(profile_home: Path, job: dict) -> dict[str, Any]:
    profile_home = Path(profile_home).expanduser().resolve()
    payload = {"profile_home": str(profile_home), "job": job}
    child_code = r'''
import json
import os
import sys
from pathlib import Path

payload = json.loads(sys.stdin.read())
profile_home = Path(payload["profile_home"]).expanduser().resolve()
os.environ["HERMES_HOME"] = str(profile_home)
job = payload.get("job") or {}

try:
    from hermes_multitenancy.cron_worker import _run_job_for_profile_current_process

    result = _run_job_for_profile_current_process(profile_home, job)
except BaseException as exc:
    job_id = str(job.get("id") or "")
    job_name = str(job.get("name") or job_id or "scheduled task")
    error = str(exc)
    result = {
        "success": False,
        "output": (
            f"# Cron Job: {job_name}\n\n"
            f"**Job ID:** {job_id}\n"
            f"**Run Path:** isolated subprocess\n\n"
            f"Error: {error}"
        ),
        "final_response": "",
        "error": error,
    }

print(json.dumps(result, ensure_ascii=False))
'''
    env = os.environ.copy()
    env["HERMES_HOME"] = str(profile_home)
    # 每个 cron job 都跑在这个独立子进程里，所以这里的标记 = 「本进程内的每个回合都是定时
    # 任务触发的」，是唯一无歧义的判别点。token 台账读它把 platform 记成 "cron"：在此之前
    # cron 的合成 event 没有 platform，被 _resolve_platform_value 的 "feishu" 默认值吞掉，
    # 台账里的 cron 回合全部戴着飞书的帽子（生产近 7 天：3632 行 chat_type 为空的"飞书"行
    # 其实是 cron，真实飞书 DM 只有 486 行），成本按平台归因因此完全失真。
    env["HERMES_CRON_JOB"] = "1"
    timeout_s = _cw._cron_job_timeout_seconds()
    # start_new_session=True → the child leads its own process group so a timeout
    # can kill the ENTIRE job tree. A cron job runs an agent that may spawn
    # CLI/tool subprocesses; bounding only the direct wrapper (subprocess.run's
    # default) leaves runaway grandchildren that hold the captured pipes open and
    # keep the worker — then the pool, then all cron delivery — blocked. CRIT-2.
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        out_text, err_text = proc.communicate(
            input=json.dumps(payload, ensure_ascii=False), timeout=timeout_s
        )
    except subprocess.TimeoutExpired:
        # Kill the whole group, then reap. Return a failure result so the future
        # completes → the done-callback finalizes → the profile tick-lock
        # releases, instead of hanging the worker. CRIT-2.
        _cw._kill_cron_job_process_group(proc)
        try:
            out_text, err_text = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            _cw._kill_cron_job_process_group(proc)
            out_text, err_text = "", ""
        error = f"cron subprocess exceeded {timeout_s:.0f}s timeout — job tree killed"
        logger.warning(
            "[multitenancy] cron job timed out after %ss profile=%s job=%s",
            timeout_s, profile_home.name, job.get("id", "?"),
        )
        return {
            "success": False,
            "output": (
                f"# Cron Job: {job.get('name') or job.get('id') or 'scheduled task'}\n\n"
                f"**Job ID:** {job.get('id') or ''}\n"
                f"**Run Path:** isolated subprocess\n\n"
                f"Error: {error}"
            ),
            "final_response": "",
            "error": error,
        }
    parsed: Optional[dict[str, Any]] = None
    for line in reversed([line.strip() for line in (out_text or "").splitlines() if line.strip()]):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            parsed = candidate
            break
    if parsed is not None:
        if proc.returncode == 0:
            return parsed
        stderr = (err_text or "").strip()
        parsed["success"] = False
        parsed["error"] = parsed.get("error") or stderr or f"cron subprocess exited {proc.returncode}"
        return parsed
    stderr = (err_text or "").strip()
    error = stderr or f"cron subprocess exited {proc.returncode} without JSON result"
    return {
        "success": False,
        "output": (
            f"# Cron Job: {job.get('name') or job.get('id') or 'scheduled task'}\n\n"
            f"**Job ID:** {job.get('id') or ''}\n"
            f"**Run Path:** isolated subprocess\n\n"
            f"Error: {error}"
        ),
        "final_response": "",
        "error": error,
    }
