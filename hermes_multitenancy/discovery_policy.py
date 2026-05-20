"""Policy gate for upstream capability discovery surfaces.

Skills Hub community sources and xAI ``x_search`` are useful, but in a
multitenant deployment they must be audience-scoped before becoming visible to
profiles.  This module produces a secret-free plan and optional audit record;
it does not fetch, install, or call any upstream discovery service.
"""
from __future__ import annotations

import json
import inspect
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONFIG_NAME = "discovery-policy.yaml"
AUDIT_PATH = "audit/discovery-policy.jsonl"
BUILTIN_SOURCES = frozenset({"official", "hermes-index"})
EXTERNAL_SOURCES = frozenset({
    "browse-sh",
    "skills-sh",
    "skills.sh",
    "github",
    "url",
    "well-known",
    "clawhub",
    "lobehub",
    "claude-marketplace",
})
COMMUNITY_SOURCES = EXTERNAL_SOURCES - {"github", "well-known", "url"}
POLICY_TOOLSETS = frozenset({"x_search"})
_SECRET_KEYS = ("secret", "token", "api_key", "key", "bearer", "credential")
_SAFE_SECRET_NAMED_KEYS = {
    "secret_free",
    "install_requires_secret_guard",
    "credential_available",
    "credential_source",
    "user_key",
}


@dataclass(frozen=True)
class Subject:
    profile_name: str
    user_key: str | None = None
    departments: tuple[str, ...] = ()


def plan_profile_discovery(
    *,
    shared_home: str | Path,
    profile_name: str,
    user_key: str | None = None,
    departments: list[str] | tuple[str, ...] | None = None,
    requested_sources: list[str] | tuple[str, ...] | None = None,
    requested_toolsets: list[str] | tuple[str, ...] | None = None,
    xai_credentials: dict[str, Any] | None = None,
    audit: bool = False,
) -> dict[str, Any]:
    """Return a redacted discovery policy plan for one profile."""

    shared = Path(shared_home).expanduser().resolve()
    config = _load_config(shared)
    subject = Subject(
        profile_name=str(profile_name),
        user_key=str(user_key).strip() or None if user_key is not None else None,
        departments=tuple(str(item).strip() for item in departments or () if str(item).strip()),
    )
    sources = list(requested_sources or ("official", "browse-sh", "skills-sh", "github", "url", "well-known"))
    toolsets = list(requested_toolsets or ("x_search",))

    source_plan = {
        source: _source_decision(source, config, subject)
        for source in sources
    }
    toolset_plan = {
        toolset: _toolset_decision(toolset, config, subject, xai_credentials or {})
        for toolset in toolsets
    }
    plan: dict[str, Any] = {
        "enabled": bool(config.get("enabled")),
        "profile_name": subject.profile_name,
        "user_key": subject.user_key,
        "departments": list(subject.departments),
        "sources": source_plan,
        "toolsets": toolset_plan,
        "audit": {
            "enabled": bool(_get(config, "audit", "enabled")),
            "written": False,
            "path": str(shared / AUDIT_PATH),
        },
        "secret_free": True,
    }
    if audit and plan["audit"]["enabled"]:
        append_discovery_audit(shared_home=shared, plan=plan)
        plan["audit"]["written"] = True
    return _redact(plan)


def append_discovery_audit(*, shared_home: str | Path, plan: dict[str, Any]) -> Path:
    """Append one redacted JSONL audit event and return the audit path."""

    shared = Path(shared_home).expanduser().resolve()
    path = shared / AUDIT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    sources = plan.get("sources", {})
    toolsets = plan.get("toolsets", {})
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "profile_name": plan.get("profile_name"),
        "user_key": plan.get("user_key"),
        "sources": {
            "allowed": sorted(name for name, item in sources.items() if item.get("allowed")),
            "blocked": sorted(name for name, item in sources.items() if not item.get("allowed")),
        },
        "toolsets": {
            "allowed": sorted(name for name, item in toolsets.items() if item.get("allowed")),
            "blocked": sorted(name for name, item in toolsets.items() if not item.get("allowed")),
        },
        "secret_free": True,
    }
    path.write_text(
        (path.read_text(encoding="utf-8") if path.exists() else "")
        + json.dumps(_redact(record), ensure_ascii=False, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return path


def apply_toolset_policy(
    enabled_toolsets: list[str] | tuple[str, ...] | None,
    *,
    shared_home: str | Path,
    profile_name: str,
    user_key: str | None = None,
    departments: list[str] | tuple[str, ...] | None = None,
    xai_credentials: dict[str, Any] | None = None,
) -> list[str] | None:
    """Filter upstream auto-enabled discovery toolsets through policy."""

    if enabled_toolsets is None:
        return None
    toolsets = [str(item) for item in enabled_toolsets]
    if "x_search" not in toolsets:
        return toolsets
    plan = plan_profile_discovery(
        shared_home=shared_home,
        profile_name=profile_name,
        user_key=user_key,
        departments=departments,
        requested_sources=[],
        requested_toolsets=["x_search"],
        xai_credentials=xai_credentials or {"available": True, "source": "upstream_toolset"},
    )
    if plan["toolsets"]["x_search"]["allowed"]:
        return toolsets
    return [item for item in toolsets if item != "x_search"]


def _source_decision(source: str, config: dict[str, Any], subject: Subject) -> dict[str, Any]:
    normalized = _normalize_source(source)
    if normalized in BUILTIN_SOURCES:
        return {
            "allowed": True,
            "mode": "read_only",
            "reason": "builtin source is allowed for read-only discovery",
            "requires_audit": False,
            "install_requires_secret_guard": False,
        }
    rule = _get(config, "sources", normalized) or _get(config, "sources", source)
    if not config.get("enabled") or not isinstance(rule, dict):
        return _blocked("blocked by multitenancy discovery policy")
    if str(rule.get("action") or "deny").strip().lower() != "allow":
        return _blocked("source action is not allow")
    if not _audience_matches(rule.get("audience", "all"), subject):
        return _blocked("profile does not match source audience")
    return {
        "allowed": True,
        "mode": str(rule.get("mode") or "read_only"),
        "reason": "allowed by multitenancy discovery policy",
        "requires_audit": True,
        "install_requires_secret_guard": normalized in EXTERNAL_SOURCES,
        "trust_level": "community" if normalized in COMMUNITY_SOURCES else "external",
    }


def _toolset_decision(
    toolset: str,
    config: dict[str, Any],
    subject: Subject,
    credentials: dict[str, Any],
) -> dict[str, Any]:
    if toolset not in POLICY_TOOLSETS:
        return {
            "allowed": True,
            "reason": "toolset is outside discovery policy",
            "requires_audit": False,
            "secret_free": True,
        }

    rule = _get(config, "toolsets", toolset)
    available = bool(credentials.get("available"))
    base = {
        "credential_available": available,
        "credential_source": "present" if available else "missing",
        "requires_audit": True,
        "secret_free": True,
    }
    if not config.get("enabled") or not isinstance(rule, dict):
        return {**base, **_blocked("blocked by multitenancy discovery policy")}
    if str(rule.get("action") or "deny").strip().lower() != "allow":
        return {**base, **_blocked("toolset action is not allow")}
    if not _audience_matches(rule.get("audience", "all"), subject):
        return {**base, **_blocked("profile does not match toolset audience")}
    if not available:
        return {**base, **_blocked("xAI credential is not available")}
    return {
        **base,
        "allowed": True,
        "reason": "allowed by multitenancy discovery policy",
    }


def _blocked(reason: str) -> dict[str, Any]:
    return {
        "allowed": False,
        "mode": "blocked",
        "reason": reason,
        "requires_audit": True,
        "install_requires_secret_guard": True,
    }


def _audience_matches(audience: Any, subject: Subject) -> bool:
    if audience is None:
        return False
    if isinstance(audience, str):
        return audience.strip().lower() in {"all", "*", "everyone", "__all__"}
    if isinstance(audience, list):
        return subject.profile_name in {str(item).strip() for item in audience}
    if not isinstance(audience, dict):
        return False

    profiles = _as_set(audience.get("profiles") or audience.get("profile"))
    users = _as_set(audience.get("users") or audience.get("user_ids") or audience.get("user_id"))
    departments = _as_set(audience.get("departments") or audience.get("department_ids") or audience.get("dept_ids"))
    if profiles and subject.profile_name in profiles:
        return True
    if users and subject.user_key and subject.user_key in users:
        return True
    if departments and departments.intersection(subject.departments):
        return True
    return not profiles and not users and not departments


def _load_config(shared_home: Path) -> dict[str, Any]:
    path = shared_home / CONFIG_NAME
    if not path.exists():
        return {"enabled": False}
    data = _load_yaml_like(path)
    if not isinstance(data, dict):
        return {"enabled": False}
    data.setdefault("enabled", False)
    return data


def _load_yaml_like(path: Path) -> Any:
    text = inspect.cleandoc(path.read_text(encoding="utf-8"))
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except Exception:
        return _parse_simple_yaml(text)


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Small fallback parser for the simple policy YAML used by this adapter."""
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        lines.append((len(raw) - len(raw.lstrip(" ")), raw.strip()))

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(lines):
            return {}, index
        is_list = lines[index][0] == indent and lines[index][1].startswith("- ")
        if is_list:
            items: list[Any] = []
            while index < len(lines):
                current_indent, line = lines[index]
                if current_indent != indent or not line.startswith("- "):
                    break
                value = line[2:].strip()
                if value:
                    items.append(_scalar(value))
                    index += 1
                else:
                    child, index = parse_block(index + 1, _next_indent(index, indent))
                    items.append(child)
            return items, index

        mapping: dict[str, Any] = {}
        while index < len(lines):
            current_indent, line = lines[index]
            if current_indent != indent or line.startswith("- "):
                break
            if ":" not in line:
                index += 1
                continue
            key, raw_value = line.split(":", 1)
            key = key.strip()
            value = raw_value.strip()
            if value:
                mapping[key] = _scalar(value)
                index += 1
            else:
                child, index = parse_block(index + 1, _next_indent(index, indent))
                mapping[key] = child
        return mapping, index

    def _next_indent(index: int, fallback: int) -> int:
        return lines[index + 1][0] if index + 1 < len(lines) else fallback + 2

    parsed, _ = parse_block(0, lines[0][0] if lines else 0)
    return parsed if isinstance(parsed, dict) else {}


def _scalar(value: str) -> Any:
    value = value.strip().strip("\"'")
    lower = value.lower()
    if lower in {"true", "yes", "on"}:
        return True
    if lower in {"false", "no", "off"}:
        return False
    if lower in {"null", "none", "~"}:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def _get(mapping: dict[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _as_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    return {str(value).strip()} if str(value).strip() else set()


def _normalize_source(source: str) -> str:
    return "skills-sh" if source == "skills.sh" else str(source).strip()


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in _SAFE_SECRET_NAMED_KEYS:
                redacted[key_text] = _redact(item)
            elif any(part in key_text.lower() for part in _SECRET_KEYS):
                redacted[key_text] = "<redacted>"
            else:
                redacted[key_text] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value
