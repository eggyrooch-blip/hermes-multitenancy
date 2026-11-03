"""Profile-scoped adapter for Hermes curator metadata.

Multitenancy owns managed/shared skill distribution.  This adapter only reuses
Hermes curator sidecar data for secret-free audit and future dry-run surfaces.
It deliberately does not execute curator or mark managed skills eligible.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_USAGE_FIELDS = (
    "use_count",
    "view_count",
    "patch_count",
    "last_used_at",
    "last_viewed_at",
    "last_patched_at",
)
_ACTIVITY_FIELDS = ("last_used_at", "last_viewed_at", "last_patched_at")


def build_curator_dry_run_plan(*, profile_home: Path) -> dict[str, Any]:
    """Return the command/env needed for a profile-scoped curator dry-run.

    The caller can display or explicitly execute this later.  Returning
    ``executes=False`` is part of the contract: multitenancy never runs curator
    implicitly.
    """
    profile = Path(profile_home).expanduser()
    return {
        "command": ["hermes", "curator", "run", "--dry-run"],
        "env": {"HERMES_HOME": str(profile)},
        "executes": False,
    }


def curator_metadata_for_skill(
    *,
    profile_home: Path,
    skill_path: str,
    skill_md: Path,
    source: str,
    usage: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build secret-free curator policy/usage metadata for one installed skill."""
    skill_name = read_skill_name(skill_md=skill_md, fallback=Path(skill_path).name)
    usage_map = usage if usage is not None else load_usage(profile_home=profile_home)
    record = usage_map.get(skill_name)
    usage = _safe_usage(record)
    source_name = str(source or "unknown")

    eligible = False
    if source_name == "managed":
        reason = "managed_by_multitenancy"
    elif source_name in {"org", "shared"}:
        reason = f"{source_name}_skill_not_eligible"
    elif source_name == "personal" and _is_agent_created(record):
        eligible = True
        reason = "agent_created_personal_skill"
    elif source_name == "personal":
        reason = "personal_not_agent_created"
    else:
        reason = "unknown_not_agent_created"

    return {
        "eligible": eligible,
        "reason": reason,
        "skill_name": skill_name,
        "state": _record_value(record, "state", default="active"),
        "pinned": bool(_record_value(record, "pinned", default=False)),
        "usage": usage,
    }


def load_usage(*, profile_home: Path) -> dict[str, dict[str, Any]]:
    path = Path(profile_home).expanduser() / "skills" / ".usage.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def read_skill_name(*, skill_md: Path, fallback: str) -> str:
    try:
        text = Path(skill_md).read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return fallback
    in_frontmatter = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "---":
            if in_frontmatter:
                break
            in_frontmatter = True
            continue
        if in_frontmatter and stripped.startswith("name:"):
            value = stripped.split(":", 1)[1].strip().strip("\"'")
            if value:
                return value
    return fallback


def _safe_usage(record: Any) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key in _USAGE_FIELDS:
        value = _record_value(record, key, default=0 if key.endswith("_count") else None)
        if key.endswith("_count"):
            value = _as_int(value)
        safe[key] = value
    safe["latest_activity_at"] = _latest_activity_at(record)
    safe["activity_count"] = sum(_as_int(safe[key]) for key in ("use_count", "view_count", "patch_count"))
    return safe


def _latest_activity_at(record: Any) -> str | None:
    if not isinstance(record, dict):
        return None
    values = [str(record[key]) for key in _ACTIVITY_FIELDS if record.get(key)]
    return max(values) if values else None


def _is_agent_created(record: Any) -> bool:
    return isinstance(record, dict) and (
        record.get("created_by") == "agent" or record.get("agent_created") is True
    )


def _record_value(record: Any, key: str, *, default: Any) -> Any:
    if not isinstance(record, dict):
        return default
    return record.get(key, default)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
