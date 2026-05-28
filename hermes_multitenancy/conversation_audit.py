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

    event = {
        "@timestamp": _timestamp_iso(timestamp),
        "event_type": "conversation_message",
        "profile": profile_name,
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
