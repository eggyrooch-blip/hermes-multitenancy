"""Tenant-aware run broker skeleton.

This module is intentionally channel-neutral. Feishu, WebUI, and cron should
submit ``RunRequest`` objects here; channel adapters remain responsible for
rendering ``RunEvent`` objects back to their clients.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import inspect
import os
import threading
from typing import Awaitable, Callable, Optional

from .run_models import RunEvent, RunRequest, RunResult
from .skill_slash import rewrite_skill_slash_text


class RunRejected(RuntimeError):
    """Raised when a run violates broker execution policy."""


DispatchAgent = Callable[[RunRequest], Awaitable[str] | str]
EmitEvent = Callable[[RunEvent], Awaitable[None] | None]
MarkSeen = Callable[[RunRequest], bool]
IsSeen = Callable[[RunRequest], bool]
SandboxAvailable = Callable[[], bool]
PrepareRequest = Callable[[RunRequest], Awaitable[RunRequest] | RunRequest]
TransformRequest = Callable[["PreparedRun"], Awaitable[RunRequest] | RunRequest]


_PREPARED_RUN_PROOF = object()
_ADMITTED_RUN_PROOF = object()
_PREPARE_INFLIGHT_LOCK = threading.Lock()
_PREPARE_INFLIGHT: dict[
    tuple[int, int, str, str, str, str], asyncio.Task["PreparedRun"]
] = {}
_ADMISSION_INFLIGHT: dict[
    tuple[int, int, int, str, str, str, str], "_AdmissionInflight"
] = {}


def _request_identity(request: RunRequest) -> tuple[str, str, str, str]:
    return (
        request.channel,
        request.profile_name,
        request.user_key,
        request.effective_idempotency_key,
    )


class _PreparedAdmissionState:
    __slots__ = ("lock", "spent")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.spent = False


class PreparedRun:
    """Opaque proof that a broker successfully prepared one request.

    Instances can only be issued by :meth:`RunBroker.prepare`.  Keeping the
    proof separate from ``RunRequest`` prevents public callers from claiming a
    raw request was already prepared and bypassing billing/identity work.
    """

    __slots__ = (
        "_request",
        "_admission_request",
        "_identity",
        "_authority",
        "_admission_state",
        "_proof",
    )

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("PreparedRun values are issued by RunBroker.prepare()")

    def __setattr__(self, _name, _value) -> None:
        raise AttributeError("PreparedRun is immutable")

    @property
    def request(self) -> RunRequest:
        _require_prepared(self)
        return self._request

    def with_request(self, request: RunRequest) -> "PreparedRun":
        """Attach enriched dispatch data without changing canonical admission data."""
        _require_prepared(self)
        if not isinstance(request, RunRequest):
            raise TypeError("prepared request must be a RunRequest")
        if _request_identity(request)[:3] != self._identity[:3]:
            raise ValueError("prepared request identity cannot change")
        return _issue_prepared(
            request,
            admission_request=self._admission_request,
            identity=self._identity,
            authority=self._authority,
            admission_state=self._admission_state,
        )


def _issue_prepared(
    request: RunRequest,
    *,
    admission_request: Optional[RunRequest] = None,
    identity: Optional[tuple[str, str, str, str]] = None,
    authority: object,
    admission_state: Optional[_PreparedAdmissionState] = None,
) -> PreparedRun:
    prepared = object.__new__(PreparedRun)
    object.__setattr__(prepared, "_request", request)
    canonical_request = admission_request or request
    object.__setattr__(prepared, "_admission_request", canonical_request)
    object.__setattr__(prepared, "_identity", identity or _request_identity(canonical_request))
    object.__setattr__(prepared, "_authority", authority)
    object.__setattr__(
        prepared,
        "_admission_state",
        admission_state or _PreparedAdmissionState(),
    )
    object.__setattr__(prepared, "_proof", _PREPARED_RUN_PROOF)
    return prepared


def _require_prepared(value: object) -> PreparedRun:
    if (
        not isinstance(value, PreparedRun)
        or getattr(value, "_proof", None) is not _PREPARED_RUN_PROOF
    ):
        raise TypeError("a RunBroker-issued PreparedRun is required")
    return value


class AdmittedRun:
    """One-shot authority to dispatch a successfully admitted request."""

    __slots__ = ("_request", "_authority", "_claim_lock", "_claimed", "_proof")

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("AdmittedRun values are issued by RunBroker admission")

    def __setattr__(self, _name, _value) -> None:
        raise AttributeError("AdmittedRun is immutable")

    @property
    def request(self) -> RunRequest:
        _require_admitted(self)
        return self._request

    @property
    def duplicate(self) -> bool:
        return False


def _issue_admitted(request: RunRequest, *, authority: object) -> AdmittedRun:
    admitted = object.__new__(AdmittedRun)
    object.__setattr__(admitted, "_request", request)
    object.__setattr__(admitted, "_authority", authority)
    object.__setattr__(admitted, "_claim_lock", threading.Lock())
    object.__setattr__(admitted, "_claimed", False)
    object.__setattr__(admitted, "_proof", _ADMITTED_RUN_PROOF)
    return admitted


def _require_admitted(value: object) -> AdmittedRun:
    if (
        not isinstance(value, AdmittedRun)
        or getattr(value, "_proof", None) is not _ADMITTED_RUN_PROOF
    ):
        raise TypeError("a RunBroker-issued AdmittedRun is required")
    return value


class _AdmissionInflight:
    __slots__ = ("task", "delivered", "waiters")

    def __init__(
        self,
        task: asyncio.Task[tuple[Optional[PreparedRun], RunResult]],
    ) -> None:
        self.task = task
        self.delivered = False
        self.waiters = 0


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
        is_seen: Optional[IsSeen] = None,
        sandbox_available: Optional[SandboxAvailable] = None,
        prepare_request: Optional[PrepareRequest] = None,
        require_sandbox_for_host_tools: bool = True,
    ) -> None:
        self._dispatch_agent = dispatch_agent
        self._emit_event = emit_event
        self._mark_seen = mark_seen
        self._is_seen = is_seen
        self._sandbox_available = sandbox_available or _default_sandbox_available
        self._prepare_request = prepare_request
        self._preparation_authority = prepare_request if prepare_request is not None else self
        self._require_sandbox_for_host_tools = require_sandbox_for_host_tools

    async def run(self, request: RunRequest) -> RunResult:
        """Prepare, admit, and execute one request through every boundary."""
        admitted, admission = await self.prepare_and_admit(request)
        if admission.duplicate:
            await self._emit(RunEvent(kind="done"))
            return admission
        assert admitted is not None
        return await self.run_admitted(admitted)

    async def run_admitted(
        self,
        admitted: AdmittedRun,
        *,
        dispatch_agent: Optional[DispatchAgent] = None,
        emit_event: Optional[EmitEvent] = None,
    ) -> RunResult:
        """Atomically claim and dispatch this broker's admitted capability once."""
        admitted = _require_admitted(admitted)
        if admitted._authority is not self:
            raise TypeError("AdmittedRun was issued by a different broker authority")
        with admitted._claim_lock:
            if admitted._claimed:
                raise TypeError("AdmittedRun was already claimed")
            object.__setattr__(admitted, "_claimed", True)
        request = admitted.request
        self._assert_policy(request)
        response = await _maybe_await((dispatch_agent or self._dispatch_agent)(request))
        content = str(response or "")
        if content:
            await self._emit_to(RunEvent(kind="content", text=content), emit_event)
        await self._emit_to(RunEvent(kind="done"), emit_event)
        return RunResult(content=content, duplicate=False)

    async def run_prepared(
        self,
        admitted: AdmittedRun,
        *,
        dispatch_agent: Optional[DispatchAgent] = None,
        emit_event: Optional[EmitEvent] = None,
    ) -> RunResult:
        """Compatibility name; execution still requires an ``AdmittedRun``."""
        return await self.run_admitted(
            admitted,
            dispatch_agent=dispatch_agent,
            emit_event=emit_event,
        )

    async def admit(self, request: RunRequest) -> RunResult:
        """Prepare and consume idempotency without dispatching the agent."""
        _prepared, admission = await self.prepare_and_admit(request)
        return admission

    async def prepare_and_admit(
        self,
        request: RunRequest,
        *,
        transform_request: Optional[TransformRequest] = None,
    ) -> tuple[Optional[AdmittedRun], RunResult]:
        """Coalesce canonical prepare/transform/admission and issue one executor."""
        request = self.check_policy(request)
        loop = asyncio.get_running_loop()
        identity = _request_identity(request)
        key = (
            id(loop),
            id(self._preparation_authority),
            id(self._mark_seen),
            *identity,
        )
        with _PREPARE_INFLIGHT_LOCK:
            entry = _ADMISSION_INFLIGHT.get(key)
            if entry is None:
                task = loop.create_task(
                    self._prepare_transform_once(request, transform_request)
                )
                entry = _AdmissionInflight(task)
                _ADMISSION_INFLIGHT[key] = entry
            entry.waiters += 1

        released = False
        try:
            prepared, admission = await asyncio.shield(entry.task)
            if admission.duplicate or prepared is None:
                self._drop_admission_inflight(key, entry)
                return None, admission
            with _PREPARE_INFLIGHT_LOCK:
                if entry.delivered:
                    return None, RunResult(content="", duplicate=True)
                entry.delivered = True
            try:
                if not self._claim_prepared_admission(prepared):
                    return None, RunResult(content="", duplicate=True)
                admission = self._consume_idempotency(prepared._admission_request)
            finally:
                self._drop_admission_inflight(key, entry)
            if admission.duplicate:
                return None, admission
            return _issue_admitted(prepared.request, authority=self), admission
        except asyncio.CancelledError:
            self._release_admission_waiter(key, entry, cancel_if_last=True)
            released = True
            raise
        except BaseException:
            self._drop_admission_inflight(key, entry)
            raise
        finally:
            if not released:
                self._release_admission_waiter(key, entry, cancel_if_last=False)

    def check_policy(self, request: RunRequest) -> RunRequest:
        """Rewrite and validate a request without preparation or idempotency."""
        request = _rewrite_skill_slash_request(request)
        self._assert_policy(request)
        return request

    def is_duplicate(self, request: RunRequest) -> bool:
        """Return persistent duplicate state without mutating admission state."""
        request = self.check_policy(request)
        return self._known_duplicate(request)

    async def prepare_if_fresh(self, request: RunRequest) -> Optional[PreparedRun]:
        """Return a prepared capability, or ``None`` for a known duplicate."""
        request = self.check_policy(request)
        if self._known_duplicate(request):
            return None
        return await self._prepare_checked(request)

    async def prepare(self, request: RunRequest) -> PreparedRun:
        """Validate and issue an opaque capability before idempotency mutation."""
        request = self.check_policy(request)
        return await self._prepare_checked(request)

    async def admit_prepared(self, prepared: PreparedRun) -> AdmittedRun | RunResult:
        """Consume canonical idempotency and issue a one-shot dispatch capability."""
        prepared = _require_prepared(prepared)
        if prepared._authority is not self._preparation_authority:
            raise TypeError("PreparedRun was issued by a different preparation boundary")
        if not self._claim_prepared_admission(prepared):
            return RunResult(content="", duplicate=True)
        self._assert_policy(prepared.request)
        admission = self._consume_idempotency(prepared._admission_request)
        if admission.duplicate:
            return admission
        return _issue_admitted(prepared.request, authority=self)

    async def _prepare_transform_once(
        self,
        request: RunRequest,
        transform_request: Optional[TransformRequest],
    ) -> tuple[Optional[PreparedRun], RunResult]:
        if self._known_duplicate(request):
            return None, RunResult(content="", duplicate=True)
        if self._prepare_request is None:
            prepared = _issue_prepared(
                request,
                authority=self._preparation_authority,
            )
        else:
            prepared = await self._prepare_once(request)
        if transform_request is not None:
            dispatch_request = await _maybe_await(transform_request(prepared))
            if not isinstance(dispatch_request, RunRequest):
                raise TypeError("transform_request must return a RunRequest")
            prepared = prepared.with_request(self.check_policy(dispatch_request))
        return prepared, RunResult(content="", duplicate=False)

    @staticmethod
    def _drop_admission_inflight(
        key: tuple[int, int, int, str, str, str, str],
        entry: _AdmissionInflight,
    ) -> None:
        with _PREPARE_INFLIGHT_LOCK:
            if _ADMISSION_INFLIGHT.get(key) is entry:
                _ADMISSION_INFLIGHT.pop(key, None)

    @staticmethod
    def _release_admission_waiter(
        key: tuple[int, int, int, str, str, str, str],
        entry: _AdmissionInflight,
        *,
        cancel_if_last: bool,
    ) -> None:
        with _PREPARE_INFLIGHT_LOCK:
            entry.waiters -= 1
            if cancel_if_last and entry.waiters == 0:
                if not entry.task.done():
                    entry.task.cancel()
                if _ADMISSION_INFLIGHT.get(key) is entry:
                    _ADMISSION_INFLIGHT.pop(key, None)

    def _claim_prepared_admission(self, prepared: PreparedRun) -> bool:
        if prepared._authority is not self._preparation_authority:
            raise TypeError("PreparedRun was issued by a different preparation boundary")
        with prepared._admission_state.lock:
            if prepared._admission_state.spent:
                return False
            prepared._admission_state.spent = True
            return True

    async def _prepare_checked(self, request: RunRequest) -> PreparedRun:
        if self._prepare_request is None:
            return _issue_prepared(request, authority=self._preparation_authority)

        loop = asyncio.get_running_loop()
        key = (
            id(loop),
            id(self._prepare_request),
            request.channel,
            request.profile_name,
            request.user_key,
            request.effective_idempotency_key,
        )
        with _PREPARE_INFLIGHT_LOCK:
            task = _PREPARE_INFLIGHT.get(key)
            if task is None:
                task = loop.create_task(self._prepare_once(request))
                _PREPARE_INFLIGHT[key] = task

                def _cleanup(done_task: asyncio.Task[PreparedRun]) -> None:
                    with _PREPARE_INFLIGHT_LOCK:
                        if _PREPARE_INFLIGHT.get(key) is done_task:
                            _PREPARE_INFLIGHT.pop(key, None)

                task.add_done_callback(_cleanup)
        return await asyncio.shield(task)

    async def _prepare_once(self, request: RunRequest) -> PreparedRun:
        prepared_request = await _maybe_await(self._prepare_request(request))
        if not isinstance(prepared_request, RunRequest):
            raise TypeError("prepare_request must return a RunRequest")
        if _request_identity(prepared_request) != _request_identity(request):
            raise ValueError("prepare_request cannot change request identity")
        self._assert_policy(prepared_request)
        return _issue_prepared(
            prepared_request,
            admission_request=request,
            identity=_request_identity(request),
            authority=self._preparation_authority,
        )

    def _assert_policy(self, request: RunRequest) -> None:
        if (
            request.requires_host_tools
            and self._require_sandbox_for_host_tools
            and not self._sandbox_available()
        ):
            raise RunRejected("sandbox is required for host-tool-capable runs")

    def _consume_idempotency(self, request: RunRequest) -> RunResult:
        if self._mark_seen is not None and not self._mark_seen(request):
            return RunResult(content="", duplicate=True)

        return RunResult(content="", duplicate=False)

    def _known_duplicate(self, request: RunRequest) -> bool:
        return self._is_seen is not None and bool(self._is_seen(request))

    async def _emit(self, event: RunEvent) -> None:
        await self._emit_to(event, None)

    async def _emit_to(
        self,
        event: RunEvent,
        emit_event: Optional[EmitEvent],
    ) -> None:
        emitter = emit_event or self._emit_event
        if emitter is None:
            return
        await _maybe_await(emitter(event))


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
        # Verify BOTH required mutations actually happened by inspecting the states it
        # returned — the SKILLS_DIR redirect AND the command-cache reset. If either was
        # skipped, the rewrite could still resolve against the previous profile's state.
        # (Inspecting states avoids hard-importing tools.skills_tool here, which would make
        # every rewrite depend on that import even where the loader itself handles it.)
        mutated = {attr for (_m, attr, _old, _had) in states}
        scoped = "SKILLS_DIR" in mutated and "_skill_commands" in mutated
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
