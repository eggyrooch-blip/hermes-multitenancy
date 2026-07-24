from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_AUDIT_PATH = Path("/var/log/hermes/conversation-audit.jsonl")
_SHANGHAI_TZ = timezone(timedelta(hours=8))
_ENSURED_PARENT_DIRS: set[Path] = set()
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "rejected"})
_FAILURE_SUBSYSTEMS = frozenset({
    "expert_resolution",
    "credential",
    "identity",
    "permission",
    "lark_api",
    "transport",
    "tool",
    "model",
    "runtime",
    "output",
})
_REGISTERED_ERROR_CODES = frozenset({
    "EXPERT_UNAVAILABLE",
    "RUN_CANCELLED",
    "MODEL_FAILED",
    "TOOL_FAILED",
    "OUTPUT_INCOMPLETE",
    "UNKNOWN",
    "FEISHU_AUTH_REAUTH_REQUIRED",
    "FEISHU_IDENTITY_UNBOUND",
    "FEISHU_IDENTITY_MISMATCH",
    "FEISHU_PERMISSION_DENIED",
    "FEISHU_RATE_LIMITED",
    "FEISHU_DEPENDENCY_TIMEOUT",
    "FEISHU_DEPENDENCY_UNAVAILABLE",
    "FEISHU_REQUEST_INVALID",
    "FEISHU_BUSINESS_ERROR",
    "FEISHU_UNKNOWN",
})


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def conversation_audit_enabled() -> bool:
    return _truthy(os.getenv("HERMES_CONVERSATION_AUDIT_ENABLED"))


def conversation_audit_path() -> Path:
    raw = os.getenv("HERMES_CONVERSATION_AUDIT_PATH")
    if raw and raw.strip():
        return Path(raw).expanduser()
    return DEFAULT_AUDIT_PATH


def _timestamp_iso(timestamp: float | int | None) -> str:
    value = float(timestamp) if timestamp is not None else datetime.now().timestamp()
    return datetime.fromtimestamp(value, tz=_SHANGHAI_TZ).isoformat(timespec="seconds")


def build_conversation_audit_context(event: Any, profile_home: Path) -> dict[str, str]:
    source = getattr(event, "source", None)
    platform_obj = getattr(source, "platform", "") if source is not None else ""
    platform = getattr(platform_obj, "value", platform_obj) or ""
    return {
        "profile_name": Path(profile_home).name,
        "platform": str(platform),
        "chat_type": str(getattr(source, "chat_type", "") or ""),
    }


def _append_jsonl(event: dict[str, Any]) -> None:
    try:
        path = conversation_audit_path()
        parent = path.parent
        if parent not in _ENSURED_PARENT_DIRS:
            parent.mkdir(parents=True, exist_ok=True)
            _ENSURED_PARENT_DIRS.add(parent)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            fh.write("\n")
    except Exception:
        logger.exception("[multitenancy] conversation audit append failed")


def append_conversation_audit_event(
    *,
    profile_name: str,
    platform: str,
    chat_type: str,
    session_id: str,
    message_id: int | str | None,
    role: str,
    content: str | None,
    timestamp: float | int | None,
    tool_name: str | None = None,
    tool_calls: str | None = None,
    finish_reason: str | None = None,
    source: str = "state_db_mirror",
) -> None:
    if not conversation_audit_enabled():
        return

    # Same leak class as the security audit: prod profile names embed
    # chat_id/open_id (feishu_group_<chat_id> / feishu_ou_<open_id>). Scrub the
    # embedded id from the profile field before write. (content stays raw — it
    # is the conversation body this audit exists to capture.)
    from .security_audit import _redact_embedded_ids

    event = {
        "@timestamp": _timestamp_iso(timestamp),
        "event_type": "conversation_message",
        "profile": _redact_embedded_ids(str(profile_name or "")),
        "platform": platform,
        "chat_type": chat_type,
        "session_id": session_id,
        "message_id": message_id,
        "role": role,
        "content": content,
        "tool_name": tool_name,
        "tool_calls": tool_calls,
        "finish_reason": finish_reason,
        "source": source,
    }

    _append_jsonl(event)


def append_run_terminal_event(
    *,
    terminal_event_id: str,
    profile_name: str,
    platform: str,
    chat_type: str,
    expert_requested: bool,
    expert_id: str | None,
    expert_resolution: str,
    terminal_status: str,
    error_code: str | None,
    failure_subsystem: str | None,
    retryable: bool,
    retried: bool,
    answer_completed: bool,
    duration_ms: int,
    timestamp: float | int | None = None,
    source: str = "run_broker",
) -> None:
    """Append one content-free Run Broker terminal event."""
    if not conversation_audit_enabled():
        return
    if terminal_status not in _TERMINAL_STATUSES:
        raise ValueError("invalid terminal_status")
    if failure_subsystem is not None and failure_subsystem not in _FAILURE_SUBSYSTEMS:
        raise ValueError("invalid failure_subsystem")
    if error_code is not None and error_code not in _REGISTERED_ERROR_CODES:
        raise ValueError("unregistered error_code")
    if terminal_status == "completed":
        if error_code is not None or failure_subsystem is not None or not answer_completed:
            raise ValueError("completed terminal field combination is invalid")
    elif answer_completed:
        raise ValueError("non-completed terminal cannot have answer_completed=true")

    from .security_audit import _redact_embedded_ids

    _append_jsonl({
        "@timestamp": _timestamp_iso(timestamp),
        "event_type": "run_terminal",
        "schema_version": 1,
        "terminal_event_id": str(terminal_event_id),
        "profile": _redact_embedded_ids(str(profile_name or "")),
        "platform": str(platform or ""),
        "chat_type": str(chat_type or ""),
        "source": str(source or "run_broker"),
        "expert_requested": bool(expert_requested),
        "expert_id": str(expert_id or "") or None,
        "expert_resolution": str(expert_resolution or ""),
        "terminal_status": terminal_status,
        "error_code": error_code,
        "failure_subsystem": failure_subsystem,
        "retryable": bool(retryable),
        "retried": bool(retried),
        "answer_completed": bool(answer_completed),
        "duration_ms": max(0, int(duration_ms)),
    })
