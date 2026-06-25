"""Install-side worker for queued AiDock SkillHub events.

The webhook receiver only validates/deduplicates/persists inbound events. This
module consumes queued rows and performs the additive install onto the target
profiles.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .skill_registry import (
    MANAGED_SKILL_MANIFEST,
    _install_personal_skill,
    _read_manifest,
    _safe_skill_relative_path,
    _write_manifest,
)
from .skillhub_events import SkillhubEventStore, get_event_store

_DOWNLOAD_TIMEOUT_SECONDS = 30
_LOCK_FILENAME = Path(".keephub") / "lock.json"


class SkillhubInstallError(Exception):
    """Whole-event install failure with a stable error code."""

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = str(error_code)


def _default_shared_home() -> Path:
    """Resolve the shared Hermes home without importing heavy runtime modules."""
    return Path(
        os.environ.get("HERMES_SHARED_HOME")
        or os.environ.get("HERMES_HOME")
        or "~/.hermes"
    ).expanduser()


def _default_downloader(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
        return response.read()


def _first_str(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _looks_like_expired_body(data: bytes) -> bool:
    lowered = data.lower()
    return b"accessdenied" in lowered or b"request has expired" in lowered


def _download_error(exc: Exception) -> SkillhubInstallError:
    if isinstance(exc, urllib.error.HTTPError):
        if 400 <= int(exc.code) < 500:
            return SkillhubInstallError(
                f"download failed with HTTP {exc.code}",
                error_code="DOWNLOAD_EXPIRED",
            )
    return SkillhubInstallError(str(exc) or "download failed", error_code="DOWNLOAD_EXPIRED")


def _download_package(url: str, downloader: Callable[[str], bytes] | None) -> bytes:
    fetch = downloader or _default_downloader
    try:
        payload = fetch(url)
    except SkillhubInstallError:
        raise
    except Exception as exc:
        raise _download_error(exc) from exc
    if _looks_like_expired_body(payload):
        raise SkillhubInstallError("download URL expired", error_code="DOWNLOAD_EXPIRED")
    return payload


def _verify_checksum(package_bytes: bytes, expected: str | None) -> None:
    if not expected:
        return
    actual = hashlib.sha256(package_bytes).hexdigest()
    if actual.lower() != str(expected).strip().lower():
        raise SkillhubInstallError("package checksum mismatch", error_code="PACKAGE_INVALID")


def _validated_zip_relpath(name: str) -> Path:
    normalized = name.replace("\\", "/")
    rel = PurePosixPath(normalized)
    if not normalized or rel.is_absolute() or ".." in rel.parts:
        raise SkillhubInstallError("zip contains unsafe path", error_code="PACKAGE_INVALID")
    return Path(*rel.parts)


def _extract_zip_safely(package_bytes: bytes, destination: Path) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(package_bytes))
    except zipfile.BadZipFile as exc:
        raise SkillhubInstallError("package is not a valid zip archive", error_code="PACKAGE_INVALID") from exc
    with archive:
        for info in archive.infolist():
            rel_path = _validated_zip_relpath(info.filename)
            target = destination / rel_path
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink)


def _resolve_skill_root(base_dir: Path) -> Path | None:
    if (base_dir / "SKILL.md").is_file():
        return base_dir
    try:
        children = list(base_dir.iterdir())
    except FileNotFoundError:
        return None
    dirs = [child for child in children if child.is_dir()]
    if len(dirs) != 1:
        return None
    nested = dirs[0]
    return nested if (nested / "SKILL.md").is_file() else None


def _canonical_release_dir(shared_home: Path, skill_code: str, version: str) -> Path:
    safe_skill = _safe_skill_relative_path(skill_code)
    safe_version = _safe_skill_relative_path(version)
    return shared_home / "_managed" / "aidock-skillhub" / safe_skill / safe_version


def _materialize_canonical_skill(
    *,
    shared_home: Path,
    skill_code: str,
    version: str,
    package_bytes: bytes,
) -> Path:
    release_dir = _canonical_release_dir(shared_home, skill_code, version)
    existing = _resolve_skill_root(release_dir)
    if existing is not None:
        return existing

    release_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=release_dir.parent, prefix=f".{release_dir.name}-") as temp_dir:
        staging = Path(temp_dir) / "payload"
        staging.mkdir(parents=True, exist_ok=True)
        _extract_zip_safely(package_bytes, staging)
        resolved = _resolve_skill_root(staging)
        if resolved is None:
            raise SkillhubInstallError("package missing top-level SKILL.md", error_code="PACKAGE_INVALID")
        if release_dir.exists():
            shutil.rmtree(release_dir)
        shutil.move(str(staging), str(release_dir))
    installed = _resolve_skill_root(release_dir)
    if installed is None:
        raise SkillhubInstallError("package missing installed SKILL.md", error_code="PACKAGE_INVALID")
    return installed


def _routing_db_path(shared_home: Path) -> Path:
    return shared_home / "multitenancy.db"


def _resolve_profile_name(shared_home: Path, ldap: str) -> str | None:
    db_path = _routing_db_path(shared_home)
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
        row = conn.execute(
            "SELECT profile_name FROM multitenancy_routing WHERE user_id = ? AND active = 1 LIMIT 1",
            (ldap,),
        ).fetchone()
    if row is None:
        return None
    return _first_str(row[0])


def _profile_skill_matches(
    *,
    profile_home: Path,
    rel_path: Path,
    manifest_entry: dict[str, Any] | None,
    canonical_skill_root: Path,
    version: str,
) -> bool:
    if not isinstance(manifest_entry, dict):
        return False
    if str(manifest_entry.get("version") or "") != version:
        return False
    skill_dir = profile_home / "skills" / rel_path
    if skill_dir.is_symlink():
        try:
            return skill_dir.resolve(strict=True) == canonical_skill_root.resolve(strict=True)
        except FileNotFoundError:
            return False
    expected = str(canonical_skill_root)
    source = str(manifest_entry.get("source") or "")
    target = str(manifest_entry.get("target") or "")
    return (skill_dir / "SKILL.md").is_file() and expected in {source, target}


def _write_keephub_lock(
    *,
    profile_home: Path,
    skill_code: str,
    version: str,
    release_id: str | None,
) -> None:
    path = profile_home / "skills" / _LOCK_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        raw = {}
    installed = raw.get("installed") if isinstance(raw, dict) else None
    merged = dict(installed) if isinstance(installed, dict) else {}
    merged[skill_code] = {"version": version, "release_id": release_id}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"installed": merged}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _install_into_profile(
    *,
    profile_home: Path,
    skill_code: str,
    version: str,
    release_id: str | None,
    canonical_skill_root: Path,
) -> dict[str, Any]:
    rel_path = _safe_skill_relative_path(skill_code)
    managed = _read_manifest(profile_home, MANAGED_SKILL_MANIFEST)
    existing_entry = managed.get(str(rel_path)) if isinstance(managed, dict) else None
    if _profile_skill_matches(
        profile_home=profile_home,
        rel_path=rel_path,
        manifest_entry=existing_entry,
        canonical_skill_root=canonical_skill_root,
        version=version,
    ):
        return {"status": "skipped", "profile": profile_home.name}

    skill_dir = profile_home / "skills" / rel_path
    install_meta = _install_personal_skill(canonical_skill_root, skill_dir)
    managed[str(rel_path)] = {
        "source": str(canonical_skill_root),
        "target": str(canonical_skill_root),
        "version": version,
        "credential": "kep-cli",
        "origin": "aidock-skillhub",
        "release_id": release_id,
        **install_meta,
    }
    _write_manifest(profile_home, MANAGED_SKILL_MANIFEST, managed)
    _write_keephub_lock(
        profile_home=profile_home,
        skill_code=str(rel_path),
        version=version,
        release_id=release_id,
    )
    status = "installed" if existing_entry is None else "repointed"
    return {"status": status, "profile": profile_home.name}


def process_event(
    event: dict[str, Any],
    *,
    shared_home: Path,
    profiles_root: Path | None = None,
    downloader: Callable[[str], bytes] | None = None,
) -> dict[str, Any]:
    """Materialize one queued skillhub event.

    Per-user resolution misses are reported in the results payload. Whole-event
    failures raise ``SkillhubInstallError`` with a stable error code.
    """
    event_type = _first_str(event.get("event_type")) or ""
    skill_status = _first_str(event.get("skill_status")) or "active"
    if event_type == "skill.status_changed" and skill_status == "inactive":
        return {"action": "skipped_inactive", "users": {}}

    download_url = _first_str(event.get("download_url"))
    skill_code = _first_str(event.get("skill_code"))
    version = _first_str(event.get("version"))
    if not download_url or not skill_code or not version:
        raise SkillhubInstallError("event missing package fields", error_code="PACKAGE_INVALID")

    release_id = _first_str(event.get("release_id"))
    shared = Path(shared_home).expanduser()
    profiles = (profiles_root or shared / "profiles").expanduser()
    package_bytes = _download_package(download_url, downloader)
    _verify_checksum(package_bytes, _first_str(event.get("checksum_sha256")))
    canonical_skill_root = _materialize_canonical_skill(
        shared_home=shared,
        skill_code=skill_code,
        version=version,
        package_bytes=package_bytes,
    )

    audience = event.get("audience") if isinstance(event.get("audience"), dict) else {}
    users = audience.get("users") if isinstance(audience, dict) else []
    results: dict[str, Any] = {
        "action": "install",
        "skill_code": skill_code,
        "version": version,
        "release_id": release_id,
        "users": {},
    }
    for entry in users if isinstance(users, list) else []:
        if not isinstance(entry, dict):
            continue
        ldap = _first_str(entry.get("profile_id"), entry.get("employee_id"), entry.get("open_id"))
        if not ldap:
            continue
        profile_name = _resolve_profile_name(shared, ldap)
        if not profile_name:
            results["users"][ldap] = {"status": "PROFILE_NOT_FOUND", "profile": None}
            continue
        profile_home = profiles / profile_name
        profile_home.mkdir(parents=True, exist_ok=True)
        results["users"][ldap] = _install_into_profile(
            profile_home=profile_home,
            skill_code=skill_code,
            version=version,
            release_id=release_id,
            canonical_skill_root=canonical_skill_root,
        )
    return results


def run_worker(
    *,
    store: SkillhubEventStore | None = None,
    shared_home: Path | None = None,
    profiles_root: Path | None = None,
    limit: int = 50,
    downloader: Callable[[str], bytes] | None = None,
) -> dict[str, Any]:
    """Process queued events and persist installed/failed results."""
    shared = (shared_home or _default_shared_home()).expanduser()
    profiles = (profiles_root or shared / "profiles").expanduser()
    event_store = store or get_event_store()
    summary = {"processed": 0, "installed": 0, "failed": 0, "skipped": 0}

    for row in event_store.list_queued(limit=limit):
        summary["processed"] += 1
        try:
            audience = json.loads(row["audience_json"]) if row.get("audience_json") else {}
            event = {
                "event_type": row.get("event_type"),
                "skill_code": row.get("skill_code"),
                "release_id": row.get("release_id"),
                "version": row.get("version"),
                "download_url": row.get("download_url"),
                "checksum_sha256": row.get("checksum_sha256"),
                "skill_status": row.get("skill_status"),
                "audience": audience,
            }
            results = process_event(
                event,
                shared_home=shared,
                profiles_root=profiles,
                downloader=downloader,
            )
            if event_store.mark_installed(str(row["event_id"]), results):
                if results.get("action") == "skipped_inactive":
                    summary["skipped"] += 1
                else:
                    summary["installed"] += 1
        except SkillhubInstallError as exc:
            if event_store.mark_failed(str(row["event_id"]), exc.error_code, str(exc)):
                summary["failed"] += 1
        except Exception as exc:
            if event_store.mark_failed(str(row["event_id"]), "INTERNAL_ERROR", str(exc)):
                summary["failed"] += 1
    return summary
