"""Employee-facing UX boundary for a mapped-Codex ``ExecutorUnavailable`` (PLAN.md W1 t04).

``executor_map.ExecutorUnavailable`` carries a raw English ``reason`` meant for
logs/audits only — never for an employee's screen. It can embed the wrapped
exception's own text: a git-clone failure's reason includes verbatim git
stderr (``_core.py`` docstring, "auth, ref, DNS... not our paraphrase of it"),
and the binary-missing reason names the run PATH.
``codex_session_bridge.CodexSessionBridgeRejected`` (t01's thread-binding and
event-shape fail-closed checks) is the other real "Codex unavailable" raise
site — a distinct exception type, not an ``ExecutorUnavailable`` subclass —
carrying a bare stable token (e.g. ``"binding_stale"`` for a stale thread
binding, ``"workflow_id_invalid"`` for a malformed event whose
``metadata.workflow_id`` isn't a clean opaque id) as its message.
``is_unavailable()`` recognizes both; this module is the ONE place that turns
either into:

* a stable internal code (``CODEX_*``), classified from the exception's own
  ``.reason`` (or an explicit ``.code`` a future raiser sets) — NEVER from
  event/request content, so a crafted event field cannot pick its own error
  text or code;
* a structured audit record (``append_security_event``, the same seam
  ``_core._codex_unavailable`` already uses) carrying the code, the run id,
  and a hash of the reason — never the raw reason itself;
* a fixed Chinese action message, looked up BY CODE from a table — never
  derived from the reason string, so nothing internal can leak through by
  construction, no matter how ugly the internal reason is.

Wired at the two places (stream + non-stream, commit 7ceadc8) in ``_core.py``
where a caught ``ExecutorUnavailable`` is about to leave the ``agent_real``
package boundary toward WebUI/Feishu — everything upstream of that boundary
may still log/audit the raw reason; nothing downstream ever sees it again.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from .codex_gate_resume import GateResumeRejected
from .codex_session_bridge import CodexSessionBridgeRejected
from .executor_map import ExecutorUnavailable
from ..security_audit import append_security_event

logger = logging.getLogger(__name__)

CODEX_BINARY_MISSING = "CODEX_BINARY_MISSING"
CODEX_THREAD_STALE = "CODEX_THREAD_STALE"
CODEX_GATE_DENIED = "CODEX_GATE_DENIED"
CODEX_EVENT_MALFORMED = "CODEX_EVENT_MALFORMED"
CODEX_RUNTIME_ERROR = "CODEX_RUNTIME_ERROR"  # generic fallback — no more specific bucket fits

AUDIT_EVENT_TYPE = "codex_unavailable_employee_ux"

_EMPLOYEE_MESSAGE: dict[str, str] = {
    CODEX_BINARY_MISSING: "Codex 运行环境暂不可用，请稍后重试；如持续失败请联系管理员。",
    CODEX_THREAD_STALE: "本次会话状态已过期，请重新发起一轮对话。",
    CODEX_GATE_DENIED: "本次操作未获批准，已停止执行。",
    CODEX_EVENT_MALFORMED: "本次请求格式异常，已终止执行，请重新发起。",
    CODEX_RUNTIME_ERROR: "Codex 运行出现问题，已终止本次执行，请稍后重试或联系管理员。",
}

# ponytail: keyword match on the trusted exception's own reason text. Gate
# (t03) doesn't exist in this worktree yet, so there is no explicit
# code-carrying constructor to key off of for CODEX_GATE_DENIED — this
# heuristic is the bridge until t03 lands and raises with an explicit
# `.code`, which `classify()` already prefers outright over this keyword
# match. Thread-binding and event-shape validation (t01) DO exist now
# (codex_session_bridge.CodexSessionBridgeRejected) and raise bare stable
# tokens, not prose — "binding_stale" and the "*_invalid" tokens below are
# those exact wire values (from `_opaque()`/`_validate_principal()`/
# `_clock()`), not sentence fragments.
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        CODEX_BINARY_MISSING,
        ("codex' binary", "binary is on the run path", "binary missing", "codex binary"),
    ),
    (
        CODEX_THREAD_STALE,
        (
            "thread stale", "thread is stale", "thread binding stale",
            "stale thread", "thread binding is stale",
            "binding_stale",  # codex_session_bridge.CodexSessionBridgeRejected's real token
        ),
    ),
    (
        CODEX_GATE_DENIED,
        ("gate denied", "gate rejected", "capability denied", "waiting_gate", "denied:"),
    ),
    (
        CODEX_EVENT_MALFORMED,
        (
            "malformed event", "event is malformed", "malformed request",
            # codex_session_bridge's real opaque-id / principal / clock
            # validation tokens — the actual shape a malformed event takes
            # once it reaches plan_codex_thread (e.g. metadata.workflow_id
            # not a clean string).
            "workflow_id_invalid", "profile_name_invalid", "executor_invalid",
            "thread_id_invalid", "principal_invalid", "clock_invalid",
            "plan_invalid",
        ),
    ),
)


_UNAVAILABLE_TYPES: tuple[type[Exception], ...] = (
    ExecutorUnavailable,
    CodexSessionBridgeRejected,
    # t03: raises with an explicit `.code = CODEX_GATE_DENIED` already, so
    # classify()'s explicit-code path handles it -- no keyword match needed.
    GateResumeRejected,
)


def is_unavailable(exc: Exception) -> bool:
    """True for any caught exception this module knows how to turn into an
    employee-safe unavailable notice: t02's mapped-Codex
    ``executor_map.ExecutorUnavailable`` or t01's thread-binding
    ``codex_session_bridge.CodexSessionBridgeRejected`` — whichever raise
    site produced it. Single place both ``_core.py`` boundary hooks call, so
    a third raise type (t03's gate denial) only needs adding here once."""
    return isinstance(exc, _UNAVAILABLE_TYPES)


def classify(exc: Exception) -> str:
    """Stable internal code for a caught ``ExecutorUnavailable``.

    Reads ONLY ``exc.code`` / ``exc.reason`` (falling back to ``str(exc)``) —
    never the inbound event or request. An explicit ``exc.code`` wins outright
    over the keyword heuristic.
    """
    explicit = getattr(exc, "code", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    reason = str(getattr(exc, "reason", None) or exc).lower()
    for code, needles in _KEYWORDS:
        if any(needle in reason for needle in needles):
            return code
    return CODEX_RUNTIME_ERROR


def employee_message(code: str) -> str:
    """Fixed Chinese action text for a code — unknown codes get the generic one."""
    return _EMPLOYEE_MESSAGE.get(code, _EMPLOYEE_MESSAGE[CODEX_RUNTIME_ERROR])


class ExecutorUnavailableForEmployee(ExecutorUnavailable):
    """Same wire contract as ``ExecutorUnavailable`` — message is Chinese-only.

    Subclasses ``ExecutorUnavailable`` (not just ``ExpertUnavailableError``) so
    every existing ``except executor_map.ExecutorUnavailable:`` re-raise point
    keeps matching. Deliberately does NOT call ``ExecutorUnavailable.__init__``:
    that prefixes the message with the literal string "EXECUTOR_UNAVAILABLE:",
    itself an internal token the leak test forbids.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.reason = message
        RuntimeError.__init__(self, message)


def _run_id_for_event(event: Any) -> str:
    if event is None:
        return ""
    try:
        from . import run_workspace

        return run_workspace.workflow_id_for(event)
    except Exception:
        return ""


def _audit(*, code: str, run_id: str, reason: str) -> None:
    fingerprint = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:16]
    try:
        append_security_event(
            force=True,
            event_type=AUDIT_EVENT_TYPE,
            # `reason` is the only free-text field on the security-audit
            # allowlist; carry the stable code + a reason hash in it rather
            # than adding a new allowlisted field name for one caller.
            reason=f"code={code} reason_fp={fingerprint}",
            run_id=run_id,
            decision="rejected",
        )
    except Exception:  # pragma: no cover - audit must never mask the real error
        logger.debug("[multitenancy] unavailable-ux audit failed", exc_info=True)


def render_unavailable(exc: Exception, *, event: Any = None) -> ExecutorUnavailableForEmployee:
    """Classify + audit a caught ``ExecutorUnavailable``; return the employee-safe reraise."""
    code = classify(exc)
    reason = str(getattr(exc, "reason", None) or exc)
    _audit(code=code, run_id=_run_id_for_event(event), reason=reason)
    return ExecutorUnavailableForEmployee(code, employee_message(code))


__all__ = [
    "CODEX_BINARY_MISSING",
    "CODEX_THREAD_STALE",
    "CODEX_GATE_DENIED",
    "CODEX_EVENT_MALFORMED",
    "CODEX_RUNTIME_ERROR",
    "AUDIT_EVENT_TYPE",
    "ExecutorUnavailableForEmployee",
    "is_unavailable",
    "classify",
    "employee_message",
    "render_unavailable",
]
