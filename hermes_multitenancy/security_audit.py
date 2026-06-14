from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_AUDIT_PATH = Path("/var/log/hermes/multitenancy-security.jsonl")
_SHANGHAI_TZ = timezone(timedelta(hours=8))
_ENSURED_PARENT_DIRS: set[Path] = set()
_SAFE_FIELD_NAMES = frozenset(
    {
        "profile",
        "command_name",
        "reason",
        "path_hash",
        "path_kind",
        "command_hash",
        "command_kind",
        "chat_id_hash",
        "lease_kind",
        "decision",
        "run_id",
        "ttl_seconds",
        "point",
    }
)


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def security_audit_enabled() -> bool:
    """Whether security-audit JSONL is written.

    DEFAULT OFF so a default-off deploy is byte-identical to pre-merge prod (no
    new /var/log/hermes/multitenancy-security.jsonl side-effect). Turned on by
    an explicit HERMES_MT_SECURITY_AUDIT_ENABLED, OR automatically under strict
    context (strict = the hardened security posture, where audit belongs). An
    explicit HERMES_MT_SECURITY_AUDIT_ENABLED=0 wins even under strict (operator
    override).
    """
    raw = os.getenv("HERMES_MT_SECURITY_AUDIT_ENABLED")
    if raw is not None:
        return _truthy(raw)
    # No explicit flag: follow the strict-context posture (default off).
    try:
        from .runtime import strict_context_enabled

        return strict_context_enabled()
    except Exception:  # pragma: no cover - defensive: never break callers
        return False


def security_audit_path() -> Path:
    raw = os.getenv("HERMES_MT_SECURITY_AUDIT_PATH")
    if raw and raw.strip():
        return Path(raw).expanduser()
    return DEFAULT_AUDIT_PATH


def _timestamp_iso(timestamp: float | int | None = None) -> str:
    value = float(timestamp) if timestamp is not None else datetime.now().timestamp()
    return datetime.fromtimestamp(value, tz=_SHANGHAI_TZ).isoformat(timespec="seconds")


def _redacted_path_fields(candidate: Any) -> dict[str, str]:
    raw = str(candidate or "").strip()
    if not raw:
        return {
            "path_hash": hashlib.sha256(b"").hexdigest()[:12],
            "path_kind": "<empty>",
        }
    suffix = Path(raw).suffix.lower()
    return {
        "path_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12],
        "path_kind": suffix if suffix else "<no-ext>",
    }


def _hash_id(candidate: Any) -> str:
    return hashlib.sha256(str(candidate).encode("utf-8")).hexdigest()[:12]


def append_security_event(*, event_type: str, **fields: Any) -> None:
    if not security_audit_enabled():
        return

    event: dict[str, str] = {
        "@timestamp": _timestamp_iso(),
        "event_type": str(event_type),
    }
    path_fields = _redacted_path_fields(fields.get("path")) if "path" in fields else {}
    fields = {**fields, **path_fields}
    for name in _SAFE_FIELD_NAMES:
        value = str(fields.get(name) or "").strip()
        if value:
            event[name] = value
    open_id = str(fields.get("open_id") or "").strip()
    if open_id:
        event["open_id_hash"] = _hash_id(open_id)
    chat_id = str(fields.get("chat_id") or "").strip()
    if chat_id:
        event["chat_id_hash"] = _hash_id(chat_id)

    try:
        path = security_audit_path()
        parent = path.parent
        if parent not in _ENSURED_PARENT_DIRS:
            parent.mkdir(parents=True, exist_ok=True)
            _ENSURED_PARENT_DIRS.add(parent)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            fh.write("\n")
    except Exception:
        logger.exception("[multitenancy] security audit append failed")
