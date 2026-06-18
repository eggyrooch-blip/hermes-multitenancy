from __future__ import annotations

import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any


KEY_FIELDS = ("token", "owner", "profile", "agent", "name")


class IngestKeyError(ValueError):
    """Raised for operator-fixable ingest key file problems."""


def generate_token() -> str:
    return "hm-ingest-" + secrets.token_urlsafe(32)


def mask_token(token: str) -> str:
    token = str(token or "")
    if len(token) <= 8:
        return "****" if token else ""
    return f"{token[:4]}...{token[-4:]}"


def _clean_entry(entry: dict[str, Any]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for field in KEY_FIELDS:
        value = str(entry.get(field) or "").strip()
        if value:
            cleaned[field] = value
    if "agent" not in cleaned:
        value = str(entry.get("agent_id") or "").strip()
        if value:
            cleaned["agent"] = value
    if "name" not in cleaned:
        value = str(entry.get("display_label") or "").strip()
        if value:
            cleaned["name"] = value
    return cleaned


def load_key_file(path: Path) -> list[dict[str, str]]:
    path = path.expanduser()
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("keys", payload.get("bindings", [])) if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise IngestKeyError("ingest key file must contain a list or a {'keys': [...]} object")
    keys: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cleaned = _clean_entry(entry)
        if cleaned.get("token") and (cleaned.get("owner") or cleaned.get("profile")):
            keys.append(cleaned)
    return keys


def save_key_file(path: Path, keys: list[dict[str, str]]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    payload = {"keys": [{field: entry[field] for field in KEY_FIELDS if entry.get(field)} for entry in keys]}
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
        path.chmod(0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def public_entry(entry: dict[str, str], *, index: int) -> dict[str, Any]:
    return {
        "token": mask_token(entry.get("token", "")),
        "owner": entry.get("owner", ""),
        "profile": entry.get("profile", ""),
        "agent": entry.get("agent", ""),
        "name": entry.get("name", ""),
        "source": entry.get("source", "file") or "file",
        "index": index,
    }


def add_binding(
    keys: list[dict[str, str]],
    *,
    token: str,
    owner: str = "",
    profile: str = "",
    agent: str = "",
    name: str = "",
) -> dict[str, str]:
    token = token.strip()
    owner = owner.strip()
    profile = profile.strip()
    agent = agent.strip()
    name = name.strip()
    if not token:
        raise IngestKeyError("token is required")
    if not owner and not profile:
        raise IngestKeyError("owner or profile is required")
    for entry in keys:
        if hmac.compare_digest(entry.get("token", ""), token):
            raise IngestKeyError("token already exists")
    entry = {"token": token}
    if owner:
        entry["owner"] = owner
    if profile:
        entry["profile"] = profile
    if agent:
        entry["agent"] = agent
    if name:
        entry["name"] = name
    keys.append(entry)
    return entry


def select_indices(
    keys: list[dict[str, str]],
    *,
    index: int | None = None,
    owner: str = "",
    profile: str = "",
    agent: str = "",
    name: str = "",
) -> list[int]:
    if index is not None:
        return [index] if 0 <= index < len(keys) else []
    selectors = {
        "owner": owner.strip(),
        "profile": profile.strip(),
        "agent": agent.strip(),
        "name": name.strip(),
    }
    if not any(selectors.values()):
        raise IngestKeyError("provide --index or at least one selector")
    matches: list[int] = []
    for idx, entry in enumerate(keys):
        if all(not value or entry.get(field, "") == value for field, value in selectors.items()):
            matches.append(idx)
    return matches


def require_single_match(keys: list[dict[str, str]], **selectors: Any) -> int:
    matches = select_indices(keys, **selectors)
    if not matches:
        raise IngestKeyError("no matching ingest key binding")
    if len(matches) > 1:
        raise IngestKeyError(f"selector matched {len(matches)} bindings; add --index or more selectors")
    return matches[0]


def rotate_binding(keys: list[dict[str, str]], *, token: str, **selectors: Any) -> dict[str, str]:
    idx = require_single_match(keys, **selectors)
    token = token.strip()
    if not token:
        raise IngestKeyError("token is required")
    for other_idx, entry in enumerate(keys):
        if other_idx != idx and hmac.compare_digest(entry.get("token", ""), token):
            raise IngestKeyError("token already exists")
    keys[idx]["token"] = token
    return keys[idx]


def revoke_binding(keys: list[dict[str, str]], **selectors: Any) -> dict[str, str]:
    idx = require_single_match(keys, **selectors)
    return keys.pop(idx)
