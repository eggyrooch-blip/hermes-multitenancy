"""Tenant-aware run broker skeleton.

This module is intentionally channel-neutral. Feishu, WebUI, and cron should
submit ``RunRequest`` objects here; channel adapters remain responsible for
rendering ``RunEvent`` objects back to their clients.
"""
from __future__ import annotations

import inspect
import os
from typing import Awaitable, Callable, Optional

from .run_models import RunEvent, RunRequest, RunResult


class RunRejected(RuntimeError):
    """Raised when a run violates broker execution policy."""


DispatchAgent = Callable[[RunRequest], Awaitable[str] | str]
EmitEvent = Callable[[RunEvent], Awaitable[None] | None]
MarkSeen = Callable[[RunRequest], bool]
SandboxAvailable = Callable[[], bool]


def _default_sandbox_available() -> bool:
    return os.environ.get("HERMES_USE_SANDBOX", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


class RunBroker:
    """Single execution boundary for tenant-scoped agent runs."""

    def __init__(
        self,
        *,
        dispatch_agent: DispatchAgent,
        emit_event: Optional[EmitEvent] = None,
        mark_seen: Optional[MarkSeen] = None,
        sandbox_available: Optional[SandboxAvailable] = None,
        require_sandbox_for_host_tools: bool = True,
    ) -> None:
        self._dispatch_agent = dispatch_agent
        self._emit_event = emit_event
        self._mark_seen = mark_seen
        self._sandbox_available = sandbox_available or _default_sandbox_available
        self._require_sandbox_for_host_tools = require_sandbox_for_host_tools

    async def run(self, request: RunRequest, *, admitted: bool = False) -> RunResult:
        """Execute a request after policy and idempotency checks."""
        if not admitted:
            admission = await self.admit(request)
            if admission.duplicate:
                await self._emit(RunEvent(kind="done"))
                return admission

        response = await _maybe_await(self._dispatch_agent(request))
        content = str(response or "")
        if content:
            await self._emit(RunEvent(kind="content", text=content))
        await self._emit(RunEvent(kind="done"))
        return RunResult(content=content, duplicate=False)

    async def admit(self, request: RunRequest) -> RunResult:
        """Run policy/idempotency checks without dispatching the agent."""
        if (
            request.requires_host_tools
            and self._require_sandbox_for_host_tools
            and not self._sandbox_available()
        ):
            raise RunRejected("sandbox is required for host-tool-capable runs")

        if self._mark_seen is not None and not self._mark_seen(request):
            return RunResult(content="", duplicate=True)

        return RunResult(content="", duplicate=False)

    async def _emit(self, event: RunEvent) -> None:
        if self._emit_event is None:
            return
        await _maybe_await(self._emit_event(event))
