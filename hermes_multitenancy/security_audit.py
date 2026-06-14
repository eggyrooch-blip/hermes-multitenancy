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
_SAFE_FIELD_NAMES = frozenset({"profile", "command_name", "reason"})


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def security_audit_enabled() -> bool:
    raw = os.getenv("HERMES_MT_SECURITY_AUDIT_ENABLED")
    if raw is None:
        return True
    return _truthy(raw)


def security_audit_path() -> Path:
    raw = os.getenv("HERMES_MT_SECURITY_AUDIT_PATH")
    if raw and raw.strip():
        return Path(raw).expanduser()
    return DEFAULT_AUDIT_PATH


def _timestamp_iso(timestamp: float | int | None = None) -> str:
    value = float(timestamp) if timestamp is not None else datetime.now().timestamp()
    return datetime.fromtimestamp(value, tz=_SHANGHAI_TZ).isoformat(timespec="seconds")


def append_security_event(*, event_type: str, **fields: Any) -> None:
    if not security_audit_enabled():
        return

    event: dict[str, str] = {
        "@timestamp": _timestamp_iso(),
        "event_type": str(event_type),
    }
    for name in _SAFE_FIELD_NAMES:
        value = str(fields.get(name) or "").strip()
        if value:
            event[name] = value
    open_id = str(fields.get("open_id") or "").strip()
    if open_id:
        event["open_id_hash"] = hashlib.sha256(open_id.encode("utf-8")).hexdigest()[:12]

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
