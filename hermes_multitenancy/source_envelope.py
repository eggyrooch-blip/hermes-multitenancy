"""Minimal, profile-bound source contract for final run events."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit


MAX_SOURCE_REFS = 20
_MAX_ID = 128
_MAX_LABEL = 256
_MAX_LOCATOR = 1024
_MAX_URI = 2048
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_OPAQUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_SECRET_RE = re.compile(
    r"(?:authorization|cookie|password|session[_-]?token|access[_-]?token|refresh[_-]?token|api[_-]?key|open[_-]?id|token)\s*[:=]",
    re.IGNORECASE,
)
_RAW_CONTEXT_ID_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:ou|oc|om|on)_[A-Za-z0-9_-]+",
    re.IGNORECASE,
)
_CREDENTIAL_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{6,}|eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_=-]{4,}){1,2})",
    re.IGNORECASE,
)


def _contains_sensitive_value(value: str) -> bool:
    decoded = value
    while True:
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    lowered = decoded.lower()
    return bool(
        _SECRET_RE.search(decoded)
        or _RAW_CONTEXT_ID_RE.search(decoded)
        or _CREDENTIAL_VALUE_RE.search(decoded)
        or lowered.startswith(("bearer ", "/home/", "/users/"))
        or "/profiles/" in lowered
    )


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > limit or "\x00" in value:
        return None
    if _contains_sensitive_value(value):
        return None
    return value


def _web_uri(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    uri = value.strip()
    if not uri or len(uri) > _MAX_URI or "\x00" in uri:
        return None
    try:
        parsed = urlsplit(uri)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        safe_uri = urlunsplit(("https", parsed.netloc, parsed.path or "", "", ""))
        return safe_uri if _text(safe_uri, _MAX_URI) is not None else None
    except ValueError:
        return None


def _workspace_locator(value: Any, profile_home: Path) -> str | None:
    locator = _text(value, _MAX_LOCATOR)
    if locator is None or "\\" in locator:
        return None
    relative = PurePosixPath(locator)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    if relative.as_posix() != locator:
        return None
    try:
        workspace = (profile_home / "workspace").resolve(strict=True)
        target = (workspace / relative.as_posix()).resolve(strict=True)
        return target.relative_to(workspace).as_posix() if target.is_file() else None
    except (OSError, ValueError):
        return None


def _opaque_locator(value: Any) -> str | None:
    locator = _text(value, _MAX_LOCATOR)
    return locator if locator is not None and _OPAQUE_RE.fullmatch(locator) else None


def _structured_result(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str) or not value.lstrip().startswith("{"):
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def normalize_tool_source_refs(tool_result: Any, profile_home: Path) -> list[dict[str, str]]:
    """Read only an explicit structured ``source_refs`` member from a tool result."""
    structured = _structured_result(tool_result)
    raw_refs = structured.get("source_refs") if structured is not None else None
    if not isinstance(raw_refs, list):
        return []

    normalized: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for raw in raw_refs:
        if len(normalized) >= MAX_SOURCE_REFS or not isinstance(raw, Mapping):
            continue
        ref_id = _text(raw.get("id"), _MAX_ID)
        ref_type = _text(raw.get("type"), 32)
        label = _text(raw.get("label"), _MAX_LABEL)
        if (
            ref_id is None
            or not _ID_RE.fullmatch(ref_id)
            or ref_id in seen_ids
            or ref_type not in {"web", "workspace", "lark_doc", "other"}
            or label is None
        ):
            continue
        if ref_type == "web":
            uri = _web_uri(raw.get("uri"))
            if uri is None:
                continue
            ref = {"id": ref_id, "type": ref_type, "label": label, "uri": uri}
        elif ref_type == "workspace":
            locator = _workspace_locator(raw.get("locator"), profile_home)
            if locator is None:
                continue
            ref = {"id": ref_id, "type": ref_type, "label": label, "locator": locator}
        else:
            locator = _opaque_locator(raw.get("locator"))
            if locator is None:
                continue
            ref = {"id": ref_id, "type": ref_type, "label": label, "locator": locator}
        normalized.append(ref)
        seen_ids.add(ref_id)
    return normalized
