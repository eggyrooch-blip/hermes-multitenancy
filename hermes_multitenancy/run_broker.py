"""Tenant-aware run broker skeleton.

This module is intentionally channel-neutral. Feishu, WebUI, and cron should
submit ``RunRequest`` objects here; channel adapters remain responsible for
rendering ``RunEvent`` objects back to their clients.
"""
from __future__ import annotations

from dataclasses import replace
import inspect
import os
from typing import Awaitable, Callable, Optional

from .run_models import RunEvent, RunRequest, RunResult
from .skill_slash import rewrite_skill_slash_text


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
        request = _rewrite_skill_slash_request(request)
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
        request = _rewrite_skill_slash_request(request)
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


def _rewrite_skill_slash_request(request: RunRequest) -> RunRequest:
    task_id = (
        request.session_id
        or f"multitenancy:{request.channel}:{request.profile_name}:"
        f"{request.chat_id or 'run-broker'}:{request.user_key}"
    )
    platform = request.channel if request.channel == "feishu" else None
    # Scope the core skill-command loader to THIS request's profile so the slash rewrite
    # resolves against the profile's own installed skills (incl. their slash_aliases),
    # not a stale process-global cache left by whichever profile ran last. The Feishu
    # path already scopes before its rewrite; the webui/cron broker entry did not, which
    # leaked one profile's skill/alias commands into another's resolution.
    #
    # This whole block is SYNCHRONOUS (no await between scope-set and restore), so in
    # single-threaded asyncio there is no interleaving point: no lock is needed, there is
    # no race, and we never touch the non-reentrant env lock (which the Feishu path /
    # _profile_home_context already hold — re-acquiring it here would deadlock). Reusing a
    # profile we're already scoped to (Feishu re-entry) is a harmless idempotent no-op.
    states: list = []
    scoped = False
    router = None
    try:
        from . import router  # lazy import: router imports run_broker (avoid cycle)

        profile_home = router._profile_name_to_home(request.profile_name)
        states = router._scope_profile_skill_loader(profile_home)
        # `_scope_profile_skill_loader` is best-effort and silently skips a failed import,
        # so a non-empty `states` does NOT prove the loader is pointed at this profile.
        # Verify the ACTUAL post-conditions: both the skills dir and the command cache must
        # be scoped, or resolution would still run against the previous profile's state.
        import tools.skills_tool as _st  # type: ignore
        import agent.skill_commands as _sc  # type: ignore

        scoped = (
            getattr(_st, "SKILLS_DIR", None) == profile_home / "skills"
            and getattr(_sc, "_skill_commands", None) == {}
        )
    except Exception:
        scoped = False
    if not scoped:
        if states and router is not None:  # restore any partial scoping before bailing
            try:
                router._restore_profile_skill_loader(states)
            except Exception:
                pass
        # FAIL CLOSED: if we cannot scope the skill loader to THIS profile, we must not
        # run the rewrite against a stale process-global cache — that would resolve the
        # command against whatever profile ran last (the exact cross-profile leak this
        # fixes). Leave the text untouched; a real command just passes through unrewritten.
        return request
    try:
        rewritten = rewrite_skill_slash_text(request.content, task_id=task_id, platform=platform)
    finally:
        try:
            router._restore_profile_skill_loader(states)
        except Exception:
            pass
    if not rewritten or rewritten == request.content:
        return request
    return replace(request, content=rewritten)
