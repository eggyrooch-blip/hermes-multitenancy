"""Materialize vault credentials into profile-local compatibility files.

This is the bridge for existing token-bearing skills that expect files like
``/workspace/credentials/gitlab.token``.  The credential payload stays in the
multitenancy vault; this module writes scoped compatibility files only for the
profiles named by an operator-managed audience list.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterable

import yaml

from .credentials import CredentialStore


DEFAULT_CONFIG_NAMES = (
    "credential-materialization.yaml",
    "credential-materialization.yml",
)
DEFAULT_SHARED_PROFILE = "__shared__"
_DEFAULT_PAYLOAD_KEYS = ("token", "content", "value")
_FORBIDDEN_TARGET_NAMES = {".env", "auth.json", "feishu_uat.json"}
_ALL_ACTIVE_PROFILE_MARKERS = {"*", "all", "all_active", "__all_active__"}


def materialize_credentials(
    *,
    shared_home: Path | None = None,
    profiles_root: Path | None = None,
    db_path: Path | None = None,
    config_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write configured credential payloads to profile-local target files.

    If no config exists, this is a no-op so it can safely run after org sync.
    """
    shared = (shared_home or _current_hermes_home()).expanduser()
    profiles = (profiles_root or shared / "profiles").expanduser()
    db = (db_path or shared / "multitenancy.db").expanduser()
    config = _resolve_config_path(shared, config_path)

    stats: dict[str, Any] = {
        "config_path": str(config) if config else None,
        "entries": 0,
        "profiles_targeted": 0,
        "written": 0,
        "would_write": 0,
        "missing_credentials": 0,
        "skipped_profiles": 0,
    }
    if config is None:
        return stats

    raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    entries = raw.get("credentials") or []
    if not isinstance(entries, list):
        raise ValueError("credential materialization config must contain credentials: []")

    store = CredentialStore(db)
    try:
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("credential materialization entry must be a mapping")
            stats["entries"] += 1
            entry_stats = _materialize_entry(
                store,
                entry,
                shared_home=shared,
                profiles_root=profiles,
                dry_run=dry_run,
            )
            for key in ("profiles_targeted", "written", "would_write", "missing_credentials", "skipped_profiles"):
                stats[key] += entry_stats[key]
    finally:
        store.close()
    return stats


def _materialize_entry(
    store: CredentialStore,
    entry: dict[str, Any],
    *,
    shared_home: Path,
    profiles_root: Path,
    dry_run: bool,
) -> dict[str, int]:
    stats = {
        "profiles_targeted": 0,
        "written": 0,
        "would_write": 0,
        "missing_credentials": 0,
        "skipped_profiles": 0,
    }
    target_rel = _safe_target(entry.get("target") or "workspace/credentials/token")
    target_profiles = _target_profiles(entry, shared_home=shared_home)
    if not target_profiles:
        return stats

    try:
        payload = store.get_secret_for_runtime(
            profile_name=str(entry.get("vault_profile") or DEFAULT_SHARED_PROFILE),
            subject_id=str(entry["subject_id"]),
            provider=str(entry["provider"]),
            secret_kind=str(entry.get("secret_kind") or "token"),
        )
    except PermissionError:
        stats["missing_credentials"] += len(target_profiles)
        return stats

    content = _payload_content(payload, entry.get("payload_key"))
    for profile_name in target_profiles:
        stats["profiles_targeted"] += 1
        profile_home = profiles_root / profile_name
        if not profile_home.is_dir():
            stats["skipped_profiles"] += 1
            continue
        target = profile_home / target_rel
        if dry_run:
            stats["would_write"] += 1
            continue
        _atomic_write_secret(target, content)
        stats["written"] += 1
    return stats


def _resolve_config_path(shared_home: Path, config_path: Path | None) -> Path | None:
    if config_path is not None:
        path = config_path.expanduser()
        if not path.exists():
            raise FileNotFoundError(str(path))
        return path
    for name in DEFAULT_CONFIG_NAMES:
        path = shared_home / name
        if path.exists():
            return path
    return None


def _target_profiles(entry: dict[str, Any], *, shared_home: Path) -> list[str]:
    profiles: list[str] = []
    raw_profiles = entry.get("profiles") or []
    if isinstance(raw_profiles, str):
        raw_profiles = [raw_profiles]
    for item in raw_profiles:
        if str(item or "").strip().lower() in _ALL_ACTIVE_PROFILE_MARKERS:
            profiles.extend(_active_route_profiles(shared_home))
            continue
        cleaned = _safe_profile_name(item)
        if cleaned:
            profiles.append(cleaned)

    profile_file = entry.get("profile_file")
    if profile_file:
        path = Path(str(profile_file)).expanduser()
        if not path.is_absolute():
            path = shared_home / path
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            cleaned = _safe_profile_name(line)
            if cleaned:
                profiles.append(cleaned)
    return sorted(set(profiles))


def _active_route_profiles(shared_home: Path) -> list[str]:
    db_path = shared_home / "multitenancy.db"
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT DISTINCT profile_name FROM multitenancy_routing "
            "WHERE active = 1 AND kind = 'user' AND profile_name IS NOT NULL AND profile_name != ''"
        )
        return [
            cleaned
            for row in cur.fetchall()
            if (cleaned := _safe_profile_name(row[0]))
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _payload_content(payload: dict[str, Any], payload_key: Any) -> str:
    keys: Iterable[str]
    if payload_key:
        keys = (str(payload_key),)
    else:
        keys = _DEFAULT_PAYLOAD_KEYS
    for key in keys:
        if key in payload and payload[key] is not None:
            return str(payload[key]).rstrip("\n") + "\n"
    raise ValueError(f"credential payload lacks one of {', '.join(keys)}")


def _safe_profile_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name or "/" in name or "\\" in name or "\0" in name or ".." in name:
        return ""
    return name


def _safe_target(value: Any) -> Path:
    raw = str(value or "").strip()
    path = Path(raw)
    if path.is_absolute() or not raw:
        raise ValueError("credential materialization target must be a relative profile path")
    parts = path.parts
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("credential materialization target must not escape the profile")
    if parts[0] not in {"workspace", "home", "tokens"}:
        raise ValueError("credential materialization target must live under workspace/, home/, or tokens/")
    if any(part in _FORBIDDEN_TARGET_NAMES for part in parts):
        raise ValueError("credential materialization target cannot overwrite profile secret control files")
    return path


def _atomic_write_secret(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        delete=False,
    ) as fh:
        tmp = Path(fh.name)
        fh.write(content)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _current_hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-multitenancy-credentials")
    parser.add_argument("--home", type=Path, default=None, help="Shared Hermes home (default: HERMES_HOME or ~/.hermes)")
    parser.add_argument("--profiles-root", type=Path, default=None, help="Profiles root (default: <home>/profiles)")
    parser.add_argument("--db", type=Path, default=None, help="Credential DB path (default: <home>/multitenancy.db)")
    parser.add_argument("--config", type=Path, default=None, help="Materialization config path")
    parser.add_argument("--dry-run", action="store_true", help="Plan writes without touching profile files")
    args = parser.parse_args(argv)

    stats = materialize_credentials(
        shared_home=args.home,
        profiles_root=args.profiles_root,
        db_path=args.db,
        config_path=args.config,
        dry_run=args.dry_run,
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
