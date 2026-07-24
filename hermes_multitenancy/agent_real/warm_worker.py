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
    "HERMES_MULTITENANCY_APPROVAL_DIR",
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
    env = _build_subprocess_env(profile_home, approval_dir=approval_dir, event_stream=True)
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

    async def acquire_run(self) -> _AiagentWarmRun:
        import asyncio

        await asyncio.to_thread(self._lock.acquire)
        return _AiagentWarmRun(self, self._lock)

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
        cmd = _wrap_with_sandbox([sys.executable, str(child_script), "--worker"], self.profile_home)
        env = _build_aiagent_warm_worker_base_env(self.profile_home)
        logger.info("[multitenancy] AIAgent warm worker spawning profile_home=%s", self.profile_home)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
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
