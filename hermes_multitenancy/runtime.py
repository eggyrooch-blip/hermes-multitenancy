"""ProfileRuntime — runs an agent inside a profile-specific HERMES_HOME context.

Phase 2 Step 0 — concurrency-safe by design:

  * ``contextvars.ContextVar`` is the **authoritative** carrier of the active
    profile_home. Each asyncio task gets its own context copy, so concurrent
    dispatches cannot observe each other's profile.
  * ``os.environ['HERMES_HOME']`` mutation is **kept** for compatibility with
    Hermes upstream code that reads ``hermes_constants.get_hermes_home()``,
    but is serialized behind an ``asyncio.Lock`` to prevent the original-value
    capture race that would otherwise leave the env var stuck on a tenant's
    profile after dispatch finishes.

Caveat that remains for Phase 2 (real AIAgent integration):
  Modules that cache ``_hermes_home`` at import time (e.g. ``run.py:93``) do
  NOT see the env switch. Either reload them or add a contextvars-based
  upstream PR so Hermes reads the same ContextVar this module sets.

The actual AIAgent invocation is left abstract (via ``run_agent_fn``) so unit
tests can inject mocks without spinning up real LLMs.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

HERMES_HOME_ENV = "HERMES_HOME"

# Authoritative per-task profile context.
# Phase 2 PR target: have hermes_constants.get_hermes_home() consult this
# ContextVar before falling back to os.environ.
_PROFILE_HOME_VAR: contextvars.ContextVar[Optional[Path]] = contextvars.ContextVar(
    "hermes_multitenancy_profile_home", default=None
)


def get_current_profile_home() -> Optional[Path]:
    """Return the active profile_home for the current asyncio task, or None."""
    return _PROFILE_HOME_VAR.get()


def strict_context_enabled() -> bool:
    """True when HERMES_MULTITENANCY_STRICT_CONTEXT enables the broker boundary."""
    return os.environ.get("HERMES_MULTITENANCY_STRICT_CONTEXT", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# Serializes os.environ HERMES_HOME mutations across concurrent dispatches.
# We key locks by event loop because asyncio.Lock binds to the loop on first
# acquire — pytest spawns a fresh loop per test so a module-level lock would
# raise "bound to a different event loop" on the second test. Production
# runs a single loop so this dict stays at 1 entry.
# Phase 2: when upstream PR lands, this lock can go away (contextvars alone
# is enough because Hermes will read the ContextVar directly).
_ENV_LOCKS: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}


def _get_env_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _ENV_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _ENV_LOCKS[loop] = lock
    return lock


# Type for an agent-runner: takes the event + profile_home, returns a response string
AgentRunner = Callable[[Any, Path], Awaitable[str]]


# Spike-only routing table — Phase 2 replaces with SQLite
_SPIKE_ROUTING: dict[str, Path] = {}


def add_spike_route(open_id: str, profile_home: Path) -> None:
    """Register a hard-coded route. Spike-only helper for tests/demos."""
    _SPIKE_ROUTING[open_id] = profile_home


def clear_spike_routes() -> None:
    """Clear all spike routes (used by tests)."""
    _SPIKE_ROUTING.clear()


def resolve_profile_home(open_id: str) -> Optional[Path]:
    """Resolve open_id to a profile home directory using the spike routing table."""
    return _SPIKE_ROUTING.get(open_id)


def _consume_cancelled_runner(task: asyncio.Task) -> None:
    """Observe a detached cancelled runner so asyncio does not log it as lost."""
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.debug("multitenancy: detached runner finished after cancel: %s", exc)


class ProfileRuntime:
    """Runs an agent against a specific profile home directory.

    Use as ``await ProfileRuntime(profile_home).dispatch(event)``.

    Concurrency model:
      * The ContextVar is set BEFORE acquiring the env lock so the runner sees
        its profile_home immediately, even if env mutation is queued behind
        another dispatch.
      * The env lock is held for the entire body of the runner so any code
        inside the runner that reads ``os.environ['HERMES_HOME']`` (e.g. a
        legacy hermes module) sees the right value.

    Trade-off: env-lock serializes runners. This is acceptable for Spike
    because (a) tests use mock runners with sub-millisecond duration, and
    (b) Phase 2 PR removes the env mutation entirely in favor of contextvars,
    making lock unnecessary.
    """

    def __init__(
        self,
        profile_home: Path,
        run_agent_fn: Optional[AgentRunner] = None,
    ) -> None:
        self.profile_home = Path(profile_home)
        self._run_agent_fn = run_agent_fn or _default_run_agent

    async def dispatch(self, event: Any) -> str:
        """Set per-task profile context, run the agent under env lock."""
        token = _PROFILE_HOME_VAR.set(self.profile_home)
        try:
            async with _get_env_lock():
                original = os.environ.get(HERMES_HOME_ENV)
                os.environ[HERMES_HOME_ENV] = str(self.profile_home)
                try:
                    self._verify_switch()
                    runner_task = asyncio.create_task(
                        self._run_agent_fn(event, self.profile_home)
                    )
                    try:
                        return await asyncio.shield(runner_task)
                    except asyncio.CancelledError:
                        runner_task.cancel()
                        runner_task.add_done_callback(_consume_cancelled_runner)
                        raise
                finally:
                    if original is None:
                        os.environ.pop(HERMES_HOME_ENV, None)
                    else:
                        os.environ[HERMES_HOME_ENV] = original
        finally:
            _PROFILE_HOME_VAR.reset(token)

    def _verify_switch(self) -> None:
        """Sanity-check that the switch reached both ContextVar and hermes_constants."""
        ctx = get_current_profile_home()
        if ctx != self.profile_home:
            logger.warning(
                "multitenancy: ContextVar mismatch (want=%s got=%s)",
                self.profile_home,
                ctx,
            )

        try:
            from hermes_constants import get_hermes_home  # type: ignore
        except ImportError:
            logger.debug("hermes_constants not importable (ok in unit tests)")
            return
        actual = get_hermes_home()
        if actual != self.profile_home:
            logger.warning(
                "multitenancy: HERMES_HOME env mismatch (want=%s got=%s)",
                self.profile_home,
                actual,
            )


async def _default_run_agent(event: Any, profile_home: Path) -> str:
    """Default agent runner — Spike stub.

    Phase 2 will replace this with a real AIAgent instantiation. For Spike,
    we only need to demonstrate the dispatch path works; tests inject mocks.
    """
    text = getattr(event, "text", "") or ""
    return f"[profile={profile_home.name}] echo: {text}"
