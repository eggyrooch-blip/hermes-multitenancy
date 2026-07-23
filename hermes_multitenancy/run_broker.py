"""Tenant-aware run broker skeleton.

This module is intentionally channel-neutral. Feishu, WebUI, and cron should
submit ``RunRequest`` objects here; channel adapters remain responsible for
rendering ``RunEvent`` objects back to their clients.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import inspect
import logging
import os
import threading
from typing import Awaitable, Callable, Optional

from .run_models import RunEvent, RunRequest, RunResult
from .skill_slash import rewrite_skill_slash_text


logger = logging.getLogger(__name__)


class RunRejected(RuntimeError):
    """Raised when a run violates broker execution policy."""


DispatchAgent = Callable[[RunRequest], Awaitable[str] | str]
EmitEvent = Callable[[RunEvent], Awaitable[None] | None]
MarkSeen = Callable[[RunRequest], bool]
IsSeen = Callable[[RunRequest], bool]
SandboxAvailable = Callable[[], bool]
PrepareRequest = Callable[[RunRequest], Awaitable[RunRequest] | RunRequest]


_PREPARED_RUN_PROOF = object()
_ADMITTED_RUN_PROOF = object()
_PREPARE_INFLIGHT_LOCK = threading.Lock()
_PREPARE_INFLIGHT: dict[
    tuple[int, int, str, str, str, str], asyncio.Task["PreparedRun"]
] = {}
_EXECUTION_INFLIGHT: dict[
    tuple[int, int, int, str, str, str, str], "_ExecutionInflight"
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
        "_internal_metadata",
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

    @property
    def internal_metadata(self) -> dict[str, object]:
        _require_prepared(self)
        return dict(self._internal_metadata)

    def with_request(
        self,
        request: RunRequest,
        *,
        internal_metadata: Optional[dict[str, object]] = None,
    ) -> "PreparedRun":
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
            internal_metadata=(
                self._internal_metadata
                if internal_metadata is None
                else internal_metadata
            ),
        )


def _issue_prepared(
    request: RunRequest,
    *,
    admission_request: Optional[RunRequest] = None,
    identity: Optional[tuple[str, str, str, str]] = None,
    authority: object,
    admission_state: Optional[_PreparedAdmissionState] = None,
    internal_metadata: Optional[dict[str, object]] = None,
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
    object.__setattr__(prepared, "_internal_metadata", dict(internal_metadata or {}))
    object.__setattr__(prepared, "_proof", _PREPARED_RUN_PROOF)
    return prepared


def _require_prepared(value: object) -> PreparedRun:
    if (
        not isinstance(value, PreparedRun)
        or getattr(value, "_proof", None) is not _PREPARED_RUN_PROOF
    ):
        raise TypeError("a RunBroker-issued PreparedRun is required")
    return value


TransformRequest = Callable[
    [PreparedRun],
    Awaitable[RunRequest | PreparedRun] | RunRequest | PreparedRun,
]
BeforeAdmit = Callable[[PreparedRun], Awaitable[None] | None]
OnAbandon = Callable[[], None]
OnExecutionDone = Callable[[], None]
OnEntryDone = Callable[[bool], Awaitable[None] | None]


class AdmittedRun:
    """One-shot authority to dispatch a successfully admitted request."""

    __slots__ = (
        "_request",
        "_authority",
        "_claim_lock",
        "_claimed",
        "_internal_metadata",
        "_proof",
    )

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("AdmittedRun values are issued by RunBroker admission")

    def __setattr__(self, _name, _value) -> None:
        raise AttributeError("AdmittedRun is immutable")

    @property
    def request(self) -> RunRequest:
        _require_admitted(self)
        return self._request

    @property
    def internal_metadata(self) -> dict[str, object]:
        _require_admitted(self)
        return dict(self._internal_metadata)

    @property
    def duplicate(self) -> bool:
        return False


def _issue_admitted(
    request: RunRequest,
    *,
    authority: object,
    internal_metadata: Optional[dict[str, object]] = None,
) -> AdmittedRun:
    admitted = object.__new__(AdmittedRun)
    object.__setattr__(admitted, "_request", request)
    object.__setattr__(admitted, "_authority", authority)
    object.__setattr__(admitted, "_claim_lock", threading.Lock())
    object.__setattr__(admitted, "_claimed", False)
    object.__setattr__(admitted, "_internal_metadata", dict(internal_metadata or {}))
    object.__setattr__(admitted, "_proof", _ADMITTED_RUN_PROOF)
    return admitted


def _require_admitted(value: object) -> AdmittedRun:
    if (
        not isinstance(value, AdmittedRun)
        or getattr(value, "_proof", None) is not _ADMITTED_RUN_PROOF
    ):
        raise TypeError("a RunBroker-issued AdmittedRun is required")
    return value


ExecuteAdmitted = Callable[[AdmittedRun], Awaitable[RunResult] | RunResult]


class _ExecutionInflight:
    __slots__ = (
        "task",
        "committed",
        "delivered",
        "waiters",
        "abandoned",
        "entry_done",
    )

    def __init__(self) -> None:
        self.task: Optional[asyncio.Task[RunResult]] = None
        self.committed = False
        self.delivered = False
        self.waiters = 0
        self.abandoned = False
        self.entry_done: Optional[_EntryDoneFinalizer] = None


class _EntryDoneFinalizer:
    """Run one async shared-entry finalizer even on pre-first-step cancel."""

    __slots__ = ("_callback", "_gate", "_lock", "_started", "_task")

    def __init__(self, callback: OnEntryDone) -> None:
        self._callback = callback
        self._lock = threading.Lock()
        self._started = False
        loop = asyncio.get_running_loop()
        self._gate: asyncio.Future[bool] = loop.create_future()
        completion_coro = self._run_entry_done()
        try:
            task = loop.create_task(completion_coro)
        except BaseException:
            completion_coro.close()
            self._gate.cancel()
            raise
        task.add_done_callback(_consume_task_exception)
        cancelling = getattr(task, "cancelling", None)
        if task.done() or (callable(cancelling) and cancelling()):
            if not self._gate.done():
                self._gate.cancel()
            if not task.done():
                task.cancel()
            raise RuntimeError("shared entry finalizer task unavailable")
        self._task = task

    def ensure(self, *, failed: bool) -> asyncio.Task[None]:
        with self._lock:
            if not self._gate.done():
                self._gate.set_result(failed)
            return self._task

    def cancel_unowned(self) -> None:
        with self._lock:
            if not self._gate.done():
                self._gate.cancel()
            if not self._task.done():
                self._task.cancel()

    def subscribe_started(self, started: Optional[asyncio.Event]) -> None:
        if started is None:
            return
        with self._lock:
            if self._started:
                started.set()

    async def _run_entry_done(self) -> None:
        failed = await self._gate
        with self._lock:
            self._started = True
        try:
            await _maybe_await(self._callback(failed))
        except Exception:
            logger.exception("RunBroker shared-entry completion failed")


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


async def _run_execute_admitted(
    execute: ExecuteAdmitted,
    admitted: AdmittedRun,
) -> RunResult:
    return await _maybe_await(execute(admitted))


async def _run_after_admission(
    admission_gate: asyncio.Future[AdmittedRun],
    execute: ExecuteAdmitted,
) -> RunResult:
    """Keep a stable task parked until durable admission has succeeded."""
    admitted = await admission_gate
    return await _run_execute_admitted(execute, admitted)


def _consume_task_exception(done_task: asyncio.Task) -> None:
    try:
        done_task.exception()
    except asyncio.CancelledError:
        pass


def _cancel_unadmitted_execution(
    admission_gate: asyncio.Future[AdmittedRun],
    execution_task: asyncio.Task[RunResult],
) -> None:
    if not admission_gate.done():
        admission_gate.cancel()
    if not execution_task.done():
        execution_task.cancel()


def _create_gated_execution_task(
    execute: ExecuteAdmitted,
) -> tuple[asyncio.Future[AdmittedRun], asyncio.Task[RunResult]]:
    """Create and validate the stable owner before idempotency is consumed."""
    loop = asyncio.get_running_loop()
    admission_gate: asyncio.Future[AdmittedRun] = loop.create_future()
    execution_coro = _run_after_admission(admission_gate, execute)
    try:
        execution_task = asyncio.create_task(execution_coro)
    except BaseException:
        execution_coro.close()
        admission_gate.cancel()
        raise
    execution_task.add_done_callback(_consume_task_exception)
    cancelling = getattr(execution_task, "cancelling", None)
    if execution_task.done() or (callable(cancelling) and cancelling()):
        _cancel_unadmitted_execution(admission_gate, execution_task)
        raise RuntimeError("stable execution task unavailable before admission")
    return admission_gate, execution_task


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
        result = await self.prepare_and_execute(
            request,
            execute=lambda admitted: self._run_admitted(admitted),
        )
        if result.duplicate:
            await self._emit(RunEvent(kind="done"))
        return result

    async def _run_admitted(
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
        response = await _maybe_await((dispatch_agent or self._dispatch_agent)(request))
        content = str(response or "")
        if content:
            await self._emit_to(RunEvent(kind="content", text=content), emit_event)
        await self._emit_to(RunEvent(kind="done"), emit_event)
        return RunResult(content=content, duplicate=False)

    async def prepare_and_execute(
        self,
        request: RunRequest,
        *,
        execute: ExecuteAdmitted,
        transform_request: Optional[TransformRequest] = None,
        before_admit: Optional[BeforeAdmit] = None,
        shared_entry_owned: Optional[asyncio.Event] = None,
        execution_owned: Optional[asyncio.Event] = None,
        on_abandon: Optional[OnAbandon] = None,
        on_execution_done: Optional[OnExecutionDone] = None,
        entry_completion_owned: Optional[asyncio.Event] = None,
        entry_completion_started: Optional[asyncio.Event] = None,
        on_entry_done: Optional[OnEntryDone] = None,
    ) -> RunResult:
        """Share preparation, durable admission, and one stable execution owner.

        ``execution_owned`` is set only after durable admission and successful
        creation of the stable execution task.  Callers that stage resources
        before admission can use it to decide whether cancellation cleanup
        still belongs to the request task or has transferred to execution.

        ``on_abandon`` is owned by the shared leader and runs exactly once when
        preparation/admission ends without a stable execution task.  It lets a
        shared transform stage resources without relying on a request waiter
        remaining alive to clean them up.

        ``shared_entry_owned`` is set synchronously for the caller whose
        callbacks own a newly-created shared entry. Callers with transient
        resources can defer that request's release to ``on_abandon`` or
        ``on_execution_done`` when the outer waiter exits before the shared
        task itself settles.

        ``on_execution_done`` is attached to the stable task itself. It runs
        after success, failure, or cancellation even when the coroutine is
        cancelled before its first step, where a coroutine-body ``finally``
        would never execute.

        ``on_entry_done`` belongs to the shared entry, not an HTTP/channel
        waiter. It is invoked once with ``failed=True`` when preparation,
        admission, or execution raises/cancels, and with ``failed=False`` for
        success or a durable duplicate. ``entry_completion_owned`` tells every
        waiter covered by that shared finalizer not to finish the same channel
        lifecycle independently.
        """
        request = self.check_policy(request)
        loop = asyncio.get_running_loop()
        key = (
            id(loop),
            id(self._preparation_authority),
            id(self._mark_seen),
            *_request_identity(request),
        )
        with _PREPARE_INFLIGHT_LOCK:
            entry = _EXECUTION_INFLIGHT.get(key)
            if entry is not None and entry.committed and entry.waiters == 0:
                # Durable execution is already owned by the stable child, but
                # every original waiter has gone away (for example a bounded
                # synchronous HTTP request timed out).  A retry must not attach
                # to that unobserved task and inherit an unbounded wait.
                if entry_completion_owned is not None and entry.entry_done is not None:
                    entry.entry_done.subscribe_started(entry_completion_started)
                    entry_completion_owned.set()
                return RunResult(content="", duplicate=True)
            if entry is None:
                entry = _ExecutionInflight()
                if on_entry_done is not None:
                    entry.entry_done = _EntryDoneFinalizer(on_entry_done)
                try:
                    entry.task = loop.create_task(
                        self._prepare_admit_execute_once(
                            entry,
                            request,
                            execute=execute,
                            transform_request=transform_request,
                            before_admit=before_admit,
                            execution_owned=execution_owned,
                            on_abandon=on_abandon,
                            on_execution_done=on_execution_done,
                            entry_done=entry.entry_done,
                        )
                    )
                except BaseException:
                    if entry.entry_done is not None:
                        entry.entry_done.cancel_unowned()
                    raise
                _EXECUTION_INFLIGHT[key] = entry

                def _cleanup_execution(done_task: asyncio.Task[RunResult]) -> None:
                    failed = done_task.cancelled()
                    try:
                        failed = done_task.exception() is not None
                    except asyncio.CancelledError:
                        failed = True
                    if entry.entry_done is not None:
                        entry.entry_done.ensure(failed=failed)
                    self._run_on_abandon_once(entry, on_abandon)
                    with _PREPARE_INFLIGHT_LOCK:
                        if _EXECUTION_INFLIGHT.get(key) is entry:
                            _EXECUTION_INFLIGHT.pop(key, None)

                entry.task.add_done_callback(_cleanup_execution)
                if shared_entry_owned is not None:
                    shared_entry_owned.set()
            if (
                entry_completion_owned is not None
                and entry.entry_done is not None
            ):
                entry.entry_done.subscribe_started(entry_completion_started)
                entry_completion_owned.set()
            entry.waiters += 1

        released = False
        try:
            assert entry.task is not None
            result = await asyncio.shield(entry.task)
            self._drop_execution_inflight(key, entry)
            if result.duplicate:
                return result
            with _PREPARE_INFLIGHT_LOCK:
                if entry.delivered:
                    return RunResult(content="", duplicate=True)
                entry.delivered = True
            return result
        except asyncio.CancelledError:
            self._release_execution_waiter(key, entry, cancel_if_last=True)
            released = True
            raise
        except BaseException:
            self._drop_execution_inflight(key, entry)
            raise
        finally:
            if not released:
                self._release_execution_waiter(key, entry, cancel_if_last=False)

    async def run_prepared(
        self,
        prepared: PreparedRun,
        *,
        dispatch_agent: Optional[DispatchAgent] = None,
        emit_event: Optional[EmitEvent] = None,
    ) -> RunResult:
        """Consume one prepared capability and give dispatch a stable task owner."""
        prepared = _require_prepared(prepared)
        if prepared._authority is not self._preparation_authority:
            raise TypeError("PreparedRun was issued by a different preparation boundary")
        if not self._claim_prepared_admission(prepared):
            return RunResult(content="", duplicate=True)
        self._assert_policy(prepared.request)
        admitted = _issue_admitted(
            prepared.request,
            authority=self,
            internal_metadata=prepared._internal_metadata,
        )
        admission_gate, execution_task = _create_gated_execution_task(
            lambda value: self._run_admitted(
                value,
                dispatch_agent=dispatch_agent,
                emit_event=emit_event,
            )
        )
        try:
            admission = self._consume_idempotency(prepared._admission_request)
        except BaseException:
            _cancel_unadmitted_execution(admission_gate, execution_task)
            raise
        if admission.duplicate:
            _cancel_unadmitted_execution(admission_gate, execution_task)
            return admission
        admission_gate.set_result(admitted)
        return await asyncio.shield(execution_task)

    async def admit(self, request: RunRequest) -> RunResult:
        """Prepare and consume idempotency without dispatching the agent."""
        prepared = await self.prepare_if_fresh(request)
        if prepared is None:
            return RunResult(content="", duplicate=True)
        return await self.admit_prepared(prepared)

    def check_policy(self, request: RunRequest) -> RunRequest:
        """Rewrite and validate a request without preparation or idempotency."""
        from . import router
        from .plugin_state import (
            PluginStateError,
            assert_no_stale_inactive_skills,
        )

        try:
            profile_home = router._profile_name_to_home(request.profile_name)
            if profile_home is not None:
                assert_no_stale_inactive_skills(profile_home)
        except PluginStateError as exc:
            raise RunRejected("profile plugin state is unavailable") from exc
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

    async def admit_prepared(self, prepared: PreparedRun) -> RunResult:
        """Consume canonical idempotency without dispatching the agent."""
        prepared = _require_prepared(prepared)
        if prepared._authority is not self._preparation_authority:
            raise TypeError("PreparedRun was issued by a different preparation boundary")
        if not self._claim_prepared_admission(prepared):
            return RunResult(content="", duplicate=True)
        self._assert_policy(prepared.request)
        admission = self._consume_idempotency(prepared._admission_request)
        return admission

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
            transformed = await _maybe_await(transform_request(prepared))
            if isinstance(transformed, PreparedRun):
                transformed = _require_prepared(transformed)
                if transformed._admission_state is not prepared._admission_state:
                    raise ValueError("transform_request cannot replace prepared authority")
                prepared = transformed.with_request(
                    self.check_policy(transformed.request)
                )
            else:
                if not isinstance(transformed, RunRequest):
                    raise TypeError("transform_request must return a RunRequest or PreparedRun")
                prepared = prepared.with_request(self.check_policy(transformed))
        return prepared, RunResult(content="", duplicate=False)

    async def _prepare_before_admit_once(
        self,
        request: RunRequest,
        transform_request: Optional[TransformRequest],
        before_admit: Optional[BeforeAdmit],
    ) -> tuple[Optional[PreparedRun], RunResult]:
        prepared, admission = await self._prepare_transform_once(
            request,
            transform_request,
        )
        if prepared is not None and not admission.duplicate and before_admit is not None:
            await _maybe_await(before_admit(prepared))
        return prepared, admission

    async def _prepare_admit_execute_once(
        self,
        entry: _ExecutionInflight,
        request: RunRequest,
        *,
        execute: ExecuteAdmitted,
        transform_request: Optional[TransformRequest],
        before_admit: Optional[BeforeAdmit],
        execution_owned: Optional[asyncio.Event],
        on_abandon: Optional[OnAbandon],
        on_execution_done: Optional[OnExecutionDone],
        entry_done: Optional[_EntryDoneFinalizer],
    ) -> RunResult:
        failed = True
        try:
            prepared, admission = await self._prepare_before_admit_once(
                request,
                transform_request,
                before_admit,
            )
            if prepared is None or admission.duplicate:
                failed = False
                return admission
            if not self._claim_prepared_admission(prepared):
                failed = False
                return RunResult(content="", duplicate=True)
            admitted = _issue_admitted(
                prepared.request,
                authority=self,
                internal_metadata=prepared._internal_metadata,
            )
            # Park and fully instrument the stable child before durable mark.
            # Opening its gate after mark is synchronous, so a successful
            # admission can never exist without an execution owner.
            admission_gate, execution_task = _create_gated_execution_task(
                execute,
            )
            if on_execution_done is not None:
                def _finalize_execution(_done_task: asyncio.Task[RunResult]) -> None:
                    with _PREPARE_INFLIGHT_LOCK:
                        committed = entry.committed
                    if not committed:
                        return
                    try:
                        on_execution_done()
                    except Exception:
                        logger.exception("RunBroker execution cleanup failed")

                execution_task.add_done_callback(_finalize_execution)
            try:
                admission = self._consume_idempotency(prepared._admission_request)
            except BaseException:
                _cancel_unadmitted_execution(admission_gate, execution_task)
                raise
            if admission.duplicate:
                _cancel_unadmitted_execution(admission_gate, execution_task)
                failed = False
                return admission
            with _PREPARE_INFLIGHT_LOCK:
                entry.committed = True
            if execution_owned is not None:
                execution_owned.set()
            admission_gate.set_result(admitted)
            try:
                result = await asyncio.shield(execution_task)
            except asyncio.CancelledError:
                if execution_task.cancelled():
                    raise
                # The entry is never cancelled by waiter release after commit.
                # A cancellation requested synchronously by a custom mark
                # callback is delayed until the stable owner settles.
                result = await execution_task
            if not isinstance(result, RunResult):
                raise TypeError("execute must return a RunResult")
            failed = False
            return result
        finally:
            self._run_on_abandon_once(entry, on_abandon)
            if entry_done is not None:
                await asyncio.shield(entry_done.ensure(failed=failed))

    @staticmethod
    def _run_on_abandon_once(
        entry: _ExecutionInflight,
        on_abandon: Optional[OnAbandon],
    ) -> None:
        if on_abandon is None:
            return
        with _PREPARE_INFLIGHT_LOCK:
            if entry.committed or entry.abandoned:
                return
            entry.abandoned = True
        try:
            on_abandon()
        except Exception:
            logger.exception("RunBroker on_abandon cleanup failed")

    @staticmethod
    def _drop_execution_inflight(
        key: tuple[int, int, int, str, str, str, str],
        entry: _ExecutionInflight,
    ) -> None:
        with _PREPARE_INFLIGHT_LOCK:
            if _EXECUTION_INFLIGHT.get(key) is entry:
                _EXECUTION_INFLIGHT.pop(key, None)

    @staticmethod
    def _release_execution_waiter(
        key: tuple[int, int, int, str, str, str, str],
        entry: _ExecutionInflight,
        *,
        cancel_if_last: bool,
    ) -> None:
        with _PREPARE_INFLIGHT_LOCK:
            entry.waiters -= 1
            if cancel_if_last and entry.waiters == 0 and not entry.committed:
                if entry.task is not None and not entry.task.done():
                    entry.task.cancel()
                if _EXECUTION_INFLIGHT.get(key) is entry:
                    _EXECUTION_INFLIGHT.pop(key, None)

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
