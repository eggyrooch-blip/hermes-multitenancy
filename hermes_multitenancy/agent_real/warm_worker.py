from __future__ import annotations

import sys as _sys
_pkg = _sys.modules[__package__]

import json
import logging
import os
import sys
import time
import hashlib
import tempfile
import uuid
import re
import secrets
import importlib
import threading
from contextlib import closing, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional


_AIAGENT_WARM_WORKERS: dict[tuple[str, Any], "_AiagentWarmWorker"] = {}
_AIAGENT_WARM_PROFILE_LOCKS: dict[str, threading.Lock] = {}
_AIAGENT_WARM_WORKERS_GUARD = threading.RLock()
_AIAGENT_WARM_WORKER_BASE_ENV_DROP: frozenset[str] = frozenset({
    # Pre-existing leak (predates credential delegation): subprocess_env writes
    # this from the ambient sender ContextVar, so the FIRST run's initiator got
    # baked into the long-lived warm base env and stayed readable by every later
    # user's children via /proc/<pid>/environ. Delegation now makes identity a
    # credential decision input, so it must be per-run only.
    "HERMES_FEISHU_USER_OPEN_ID",
    "HERMES_MULTITENANCY_APPROVAL_DIR",
    "HERMES_MULTITENANCY_AUTHORIZATION_DIR",
    "HERMES_MULTITENANCY_CRED_BROKER_TOKEN",
    "HERMES_MULTITENANCY_CRED_LEASE",
    "HERMES_MULTITENANCY_RUN_ID",
    "HERMES_MULTITENANCY_SESSION_SEARCH_TOKEN",
    "HERMES_MULTITENANCY_SESSION_SEARCH_URL",
    "HERMES_LITELLM_RUNTIME_API_KEY",
    "HERMES_LITELLM_RUNTIME_BASE_URL",
    "HERMES_LITELLM_RUNTIME_EMPLOYEE_ID",
    "LARKSUITE_CLI_AUTH_PROXY",
    "LARKSUITE_CLI_PROXY_KEY",
})


def _aiagent_warm_worker_enabled() -> bool:
    return os.getenv("HERMES_AIAGENT_WARM_WORKER") == "1"


_AIAGENT_WARM_SLOT_WAIT_TIMEOUT_DEFAULT_S = 120.0


def _aiagent_warm_slot_wait_timeout_s() -> float:
    """Bounded wait for the per-profile slot before the one-shot fallback runs.

    Unbounded was the 2026-09-11 incident: a queued run waited forever behind a
    leaked lock with no log line and no error. 120s comfortably covers the
    normal "previous turn still finishing" case without parking a user's
    message for the whole of a multi-minute tool run.
    """
    raw = os.getenv("HERMES_AIAGENT_WARM_SLOT_WAIT_TIMEOUT", "")
    try:
        value = float(raw) if raw.strip() else _AIAGENT_WARM_SLOT_WAIT_TIMEOUT_DEFAULT_S
    except ValueError:
        value = _AIAGENT_WARM_SLOT_WAIT_TIMEOUT_DEFAULT_S
    return _clamp_slot_wait_timeout(value)


def _clamp_slot_wait_timeout(value: float) -> float:
    """Finite, positive, and no larger than the platform lock timeout ceiling."""
    import math

    if not math.isfinite(value) or value <= 0:
        return _AIAGENT_WARM_SLOT_WAIT_TIMEOUT_DEFAULT_S
    return min(value, float(threading.TIMEOUT_MAX))


# How often a parked run re-checks the slot. The run is idle while parked, so
# this is pure latency (≤ one interval after release), not CPU.
_AIAGENT_WARM_SLOT_POLL_S = 0.05


def _aiagent_warm_profile_key(profile_home: Path) -> str:
    return str(profile_home.expanduser().resolve())


def _aiagent_warm_worker_key(profile_home: Path) -> tuple[str, Any]:
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    return (_aiagent_warm_profile_key(profile_home), loop)


def _get_aiagent_warm_profile_lock(profile_key: str) -> threading.Lock:
    lock = _AIAGENT_WARM_PROFILE_LOCKS.get(profile_key)
    if lock is None:
        lock = threading.Lock()
        _AIAGENT_WARM_PROFILE_LOCKS[profile_key] = lock
    return lock


def _build_aiagent_warm_worker_base_env(profile_home: Path) -> dict[str, str]:
    approval_dir = profile_home / "tmp" / "aiagent-warm-worker-base-approval"
    approval_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # delegation_enabled=False: this env outlives every individual run and is
    # visible to every later run's children via /proc/<pid>/environ. No user's
    # borrowed credential may be resolved into it — the per-run env built by
    # _aiagent_subprocess_env_scope is the only place delegation belongs.
    env = _build_subprocess_env(
        profile_home,
        approval_dir=approval_dir,
        event_stream=True,
        delegation_enabled=False,
    )
    for key in _AIAGENT_WARM_WORKER_BASE_ENV_DROP:
        env.pop(key, None)
    env["HERMES_AIAGENT_WARM_WORKER_CHILD"] = "1"
    return env


class _AiagentWarmRun:
    def __init__(self, worker: "_AiagentWarmWorker", lock: Any) -> None:
        self.worker = worker
        self.lock = lock
        self.closed = False
        self.done = False

    async def readline(self) -> bytes:
        if self.done:
            return b""
        while True:
            proc = self.worker.proc
            if proc is None or proc.stdout is None:
                await self.close()
                raise RuntimeError("AIAgent warm worker is not running")
            line = await proc.stdout.readline()
            if not line:
                await self.worker.close()
                await self.close()
                raise RuntimeError("AIAgent warm worker stream ended without done event")
            try:
                data = json.loads(line.decode("utf-8", errors="replace").strip())
            except json.JSONDecodeError:
                return line
            if data.get("event") == "ready":
                continue
            if data.get("event") == "done":
                self.done = True
            return line

    async def start(self, payload: bytes, env: dict[str, str], timeout_s: float) -> None:
        try:
            await self.worker.start_locked_run(payload, env, timeout_s)
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.lock.release()


class _AiagentWarmWorker:
    def __init__(self, profile_home: Path, profile_lock: threading.Lock | None = None) -> None:
        self.profile_home = profile_home
        self.proc: Any = None
        self._lock = profile_lock or threading.Lock()
        self._slot_waiters_count = 0
        self._slot_waiters_guard = threading.Lock()

    def _slot_waiters(self) -> int:
        """Runs currently parked in ``acquire_run`` (diagnostics/tests)."""
        with self._slot_waiters_guard:
            return self._slot_waiters_count

    async def acquire_run(self, wait_timeout_s: float | None = None) -> _AiagentWarmRun:
        """Take the per-profile slot; cancel-safe and time-bounded.

        The slot is a ``threading.Lock`` shared across event loops (see
        ``test_aiagent_warm_worker_slot_serializes_across_event_loops``), so it
        cannot simply become an ``asyncio.Lock``. The previous implementation
        parked a worker thread in ``lock.acquire()`` with no timeout; when the
        awaiting coroutine was cancelled (the router aborts the previous
        dispatch when the user's next message arrives) the thread still took
        the lock later and nothing released it — the profile was wedged until
        the gateway restarted, silently, right after the ``turn_tool_context``
        log line (incident 2026-09-11..15, profile ``zhengshi``).

        Now the wait is a non-blocking ``acquire(False)`` poll from the event
        loop itself:

        * no thread ever holds the lock on our behalf, so a cancel (which can
          only land at the ``await asyncio.sleep``) has nothing to compensate;
        * the deadline is monotonic and covers the whole wait — there is no
          executor queue that could stretch it (review B1);
        * on timeout we raise so the caller's existing
          "slot unavailable → one-shot subprocess" fallback runs instead of an
          unbounded silent park.
        """
        import asyncio

        timeout_s = (
            _aiagent_warm_slot_wait_timeout_s()
            if wait_timeout_s is None
            else _clamp_slot_wait_timeout(float(wait_timeout_s))
        )
        lock = self._lock
        if lock.acquire(False):
            return _AiagentWarmRun(self, lock)

        logger.info(
            "[multitenancy] AIAgent warm worker slot busy; waiting profile_home=%s timeout=%.0fs",
            self.profile_home,
            timeout_s,
        )
        started = time.monotonic()
        deadline = started + timeout_s
        with self._slot_waiters_guard:
            self._slot_waiters_count += 1
        try:
            while True:
                # Cancellation lands here. We hold nothing, so nothing leaks.
                await asyncio.sleep(_AIAGENT_WARM_SLOT_POLL_S)
                if lock.acquire(False):
                    logger.info(
                        "[multitenancy] AIAgent warm worker slot acquired after wait profile_home=%s waited=%.1fs",
                        self.profile_home,
                        time.monotonic() - started,
                    )
                    return _AiagentWarmRun(self, lock)
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"AIAgent warm worker slot still busy after {timeout_s:.0f}s "
                        f"profile_home={self.profile_home}"
                    )
        finally:
            with self._slot_waiters_guard:
                self._slot_waiters_count -= 1

    async def _ensure_started(self, timeout_s: float) -> None:
        import asyncio

        if self.proc is not None and self.proc.returncode is None:
            logger.info(
                "[multitenancy] AIAgent warm worker hit profile_home=%s pid=%s",
                self.profile_home,
                self.proc.pid,
            )
            return
        self.proc = None
        child_script = Path(__file__).parent.with_name("aiagent_subprocess.py").resolve()
        env = _build_aiagent_warm_worker_base_env(self.profile_home)
        # Off the event loop: the desktop backend may create/start a container.
        cmd, spawn_env = await asyncio.to_thread(
            _sandbox_spawn,
            [sys.executable, str(child_script), "--worker"], self.profile_home, env=env,
        )
        logger.info("[multitenancy] AIAgent warm worker spawning profile_home=%s", self.profile_home)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=spawn_env,
            cwd=_aiagent_subprocess_cwd(self.profile_home),
            limit=_AIAGENT_STREAM_LIMIT,
        )
        self.proc = proc
        assert proc.stdout is not None
        ready_timeout_s = min(
            float(os.getenv("HERMES_AIAGENT_WARM_WORKER_READY_TIMEOUT", "30")),
            max(timeout_s, 0.001),
        )
        deadline = time.monotonic() + ready_timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await self.close()
                raise RuntimeError(
                    f"AIAgent warm worker did not become ready after {ready_timeout_s:g}s"
                )
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            if not line:
                await self.close()
                raise RuntimeError("AIAgent warm worker exited before ready")
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                logger.debug("[multitenancy] ignoring non-json warm worker startup line len=%s", len(text))
                continue
            if data.get("event") == "ready":
                logger.info(
                    "[multitenancy] AIAgent warm worker ready profile_home=%s pid=%s",
                    self.profile_home,
                    proc.pid,
                )
                return
            logger.debug("[multitenancy] ignoring warm worker startup event: %s", data.get("event"))

    async def start_locked_run(self, payload: bytes, env: dict[str, str], timeout_s: float) -> None:
        await self._ensure_started(timeout_s)
        proc = self.proc
        assert proc is not None
        assert proc.stdin is not None
        request = {
            "type": "run",
            "payload": json.loads(payload.decode("utf-8")),
            "env": env,
        }
        proc.stdin.write(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
        await proc.stdin.drain()

    async def start_run(self, payload: bytes, env: dict[str, str], timeout_s: float) -> _AiagentWarmRun:
        run = await self.acquire_run()
        await run.start(payload, env, timeout_s)
        return run

    async def close(self) -> None:
        import asyncio

        proc = self.proc
        self.proc = None
        if proc is None:
            return
        if proc.returncode is not None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.write(b'{"type":"shutdown"}\n')
                await proc.stdin.drain()
        except Exception:
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            return
        except Exception:
            logger.debug("[multitenancy] failed to kill AIAgent warm worker", exc_info=True)
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass


def _get_aiagent_warm_worker(profile_home: Path) -> "_AiagentWarmWorker":
    profile_key = _aiagent_warm_profile_key(profile_home)
    key = _aiagent_warm_worker_key(profile_home)
    with _AIAGENT_WARM_WORKERS_GUARD:
        worker = _AIAGENT_WARM_WORKERS.get(key)
        if worker is None:
            worker = _AiagentWarmWorker(
                profile_home,
                profile_lock=_get_aiagent_warm_profile_lock(profile_key),
            )
            _AIAGENT_WARM_WORKERS[key] = worker
        return worker


async def _discard_aiagent_warm_worker(profile_home: Path) -> None:
    key = _aiagent_warm_worker_key(profile_home)
    with _AIAGENT_WARM_WORKERS_GUARD:
        worker = _AIAGENT_WARM_WORKERS.pop(key, None)
    if worker is not None:
        await worker.close()


async def _reset_aiagent_warm_workers_for_tests() -> None:
    with _AIAGENT_WARM_WORKERS_GUARD:
        workers = list(_AIAGENT_WARM_WORKERS.values())
        _AIAGENT_WARM_WORKERS.clear()
        _AIAGENT_WARM_PROFILE_LOCKS.clear()
    for worker in workers:
        await worker.close()
