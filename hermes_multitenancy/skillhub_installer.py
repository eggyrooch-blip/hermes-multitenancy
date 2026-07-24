"""Install-side worker for queued AiDock SkillHub events.

The webhook receiver only validates/deduplicates/persists inbound events. This
module consumes queued rows and performs the additive install onto the target
profiles.
"""
from __future__ import annotations

import hashlib
import ipaddress
import socket
import io
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import threading
from typing import Any, Callable

import yaml

from . import plugin_ingest
from .skill_registry import (
    MANAGED_SKILL_MANIFEST,
    _install_personal_skill,
    _read_manifest,
    _safe_skill_relative_path,
    _write_manifest,
)
from .skillhub_events import SkillhubEventStore, get_event_store

_DOWNLOAD_TIMEOUT_SECONDS = 30
# Size caps for skill/plugin package download + extraction. The broker serving
# ~1259 employees buffers the download in memory and extracts to shared disk, so
# an unbounded response.read()/zip is an OOM / disk-fill DoS. MED-3, audit
# 2026-07-03.
_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024        # 64 MiB compressed download cap
_MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024   # 256 MiB total extracted cap
_MAX_ZIP_ENTRIES = 5000                        # entry-count cap
_ZIP_COPY_CHUNK = 1024 * 1024
_LOCK_FILENAME = Path(".keephub") / "lock.json"
_SKILL_DISTRIBUTION_FILENAME = "skill-distribution.yaml"
_SKILL_DISTRIBUTION_ORIGIN = "aidock-skillhub"
_PLUGIN_DISTRIBUTION_ORIGIN = "aidock-skillhub-plugin"

#: Event types that carry the FULL authorized-user snapshot (not an increment).
#: For these, audience-shrink triggers a reconcile that uninstalls dropped users.
#: v2 confirmed ``skill.auth_type_changed`` is a full snapshot too (previously
#: mis-assumed incremental), so it joins install_approved / status_changed here.
_FULL_SNAPSHOT_EVENT_TYPES = frozenset(
    {
        "skill.install_approved",
        "skill.status_changed",
        "skill.auth_type_changed",
    }
)

logger = logging.getLogger(__name__)

# ponytail: one process-wide serial lane is enough for the observed 293-event
# burst; shard by plugin only if measured throughput later requires it.
_DRAIN_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="skillhub-drain")
_DRAIN_LOCK = threading.Lock()
_DRAIN_STATES: dict[str, dict[str, Any]] = {}


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


def _skillhub_allowed_hosts() -> frozenset[str]:
    """Optional host allowlist for skill/plugin download URLs (comma-separated
    in ``HERMES_SKILLHUB_ALLOWED_HOSTS``). Empty → host is not restricted, but
    https is still enforced."""
    raw = os.environ.get("HERMES_SKILLHUB_ALLOWED_HOSTS", "")
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def _ip_is_blocked(ip: Any) -> bool:
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def _host_literal_blocked_ip(host: str) -> bool:
    """True if ``host`` is an IP literal in a private/reserved range — INCLUDING
    the decimal/hex/octal/short IPv4 forms the C resolver (``inet_aton``) accepts
    but ``ipaddress`` rejects. e.g. decimal / hex / octal / short forms like
    ``2130706433`` / ``0x7f000001`` / ``127.1`` all resolve to loopback, and a
    short ``10.1`` resolves to a private 10.x host. Canonical IPv4/IPv6 literals
    go through ``ipaddress``."""
    candidates: list[Any] = []
    try:
        candidates.append(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:  # permissive IPv4 forms the real connect() would accept
        candidates.append(ipaddress.ip_address(socket.inet_aton(host)))
    except (OSError, ValueError):
        pass
    return any(_ip_is_blocked(ip) for ip in candidates)


def _assert_download_url_allowed(url: str) -> None:
    """Reject non-https and non-allowlisted download hosts BEFORE fetching.

    The bytes fetched here are extracted and land in every profile's agent
    instruction layer, so an ``http://`` MITM, a ``file://`` local read, or an
    SSRF to an internal host must never be fetched. HIGH-3, audit 2026-07-03.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:  # e.g. malformed IPv6 literal
        raise SkillhubInstallError(
            f"malformed skill download URL: {exc}", error_code="PACKAGE_INVALID"
        ) from exc
    if parsed.scheme != "https":
        raise SkillhubInstallError(
            f"refusing non-https skill download URL (scheme={parsed.scheme or 'none'!r})",
            error_code="PACKAGE_INVALID",
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise SkillhubInstallError(
            "skill download URL has no host", error_code="PACKAGE_INVALID"
        )
    # Block private/reserved IP hosts by default (SSRF to internal services +
    # cloud metadata e.g. 169.254.169.254), including obfuscated numeric IPv4
    # forms. A public hostname that RESOLVES to a private IP (DNS-rebinding) is
    # only caught by HERMES_SKILLHUB_ALLOWED_HOSTS — set it for strict pinning.
    if _host_literal_blocked_ip(host):
        raise SkillhubInstallError(
            f"refusing skill download from non-public IP host {host!r}",
            error_code="PACKAGE_INVALID",
        )
    allowed = _skillhub_allowed_hosts()
    if allowed and host not in allowed:
        raise SkillhubInstallError(
            f"skill download host {host!r} not in HERMES_SKILLHUB_ALLOWED_HOSTS",
            error_code="PACKAGE_INVALID",
        )


def _default_downloader(url: str) -> bytes:
    _assert_download_url_allowed(url)
    with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:  # noqa: S310 — scheme asserted https above
        # Read at most cap+1 so an over-large body is detected without buffering
        # the whole thing (a compromised artifact host could return multi-GB).
        data = response.read(_MAX_DOWNLOAD_BYTES + 1)
    if len(data) > _MAX_DOWNLOAD_BYTES:
        raise SkillhubInstallError(
            f"skill package exceeds the {_MAX_DOWNLOAD_BYTES}-byte download cap",
            error_code="PACKAGE_INVALID",
        )
    return data


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


def _skillhub_require_checksum() -> bool:
    """Whether a downloaded package MUST carry a checksum_sha256.

    Default OFF keeps behavior byte-identical (AiDock does not always send a
    checksum today — enabling this unconditionally would reject real installs).
    Flip ``HERMES_SKILLHUB_REQUIRE_CHECKSUM=1`` once the sender is confirmed to
    include checksums; then an unverified package is refused. HIGH-3."""
    return os.environ.get("HERMES_SKILLHUB_REQUIRE_CHECKSUM", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _verify_checksum(package_bytes: bytes, expected: str | None) -> None:
    # A provided checksum is ALWAYS verified. A MISSING checksum is refused only
    # when HERMES_SKILLHUB_REQUIRE_CHECKSUM is on — the integrity check is the
    # only thing between a compromised/MITM'd artifact host and arbitrary skill
    # code landing in every profile's agent instruction layer. HIGH-3.
    if not expected:
        if _skillhub_require_checksum():
            raise SkillhubInstallError(
                "downloaded package has no checksum_sha256 and "
                "HERMES_SKILLHUB_REQUIRE_CHECKSUM is on — refusing unverified bytes",
                error_code="PACKAGE_INVALID",
            )
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


def _copy_capped(source: Any, sink: Any, *, max_bytes: int) -> int:
    """Copy ``source`` → ``sink`` streaming, raising if more than ``max_bytes``
    are written. Guards against a zip entry whose declared ``file_size``
    under-reports the real uncompressed size (a lying header that beats the
    up-front declared-total check). Returns the number of bytes written."""
    written = 0
    while True:
        chunk = source.read(_ZIP_COPY_CHUNK)
        if not chunk:
            return written
        written += len(chunk)
        if written > max_bytes:
            raise SkillhubInstallError(
                f"package extraction exceeds the {_MAX_UNCOMPRESSED_BYTES}-byte cap",
                error_code="PACKAGE_INVALID",
            )
        sink.write(chunk)


def _extract_zip_safely(package_bytes: bytes, destination: Path) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(package_bytes))
    except zipfile.BadZipFile as exc:
        raise SkillhubInstallError("package is not a valid zip archive", error_code="PACKAGE_INVALID") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > _MAX_ZIP_ENTRIES:
            raise SkillhubInstallError(
                f"package has too many entries (> {_MAX_ZIP_ENTRIES})",
                error_code="PACKAGE_INVALID",
            )
        # Reject on the declared uncompressed total up front (cheap zip-bomb check)...
        if sum(int(i.file_size) for i in infos) > _MAX_UNCOMPRESSED_BYTES:
            raise SkillhubInstallError(
                f"package uncompressed size exceeds the {_MAX_UNCOMPRESSED_BYTES}-byte cap",
                error_code="PACKAGE_INVALID",
            )
        # ...and enforce the same cap DURING copy (cumulative across files) so a
        # lying file_size header can't beat the up-front declared-total check.
        remaining = _MAX_UNCOMPRESSED_BYTES
        for info in infos:
            rel_path = _validated_zip_relpath(info.filename)
            target = destination / rel_path
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("wb") as sink:
                remaining -= _copy_capped(source, sink, max_bytes=remaining)


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
    return shared_home / "_managed" / _SKILL_DISTRIBUTION_ORIGIN / safe_skill / safe_version


def _canonical_plugin_repo_dir(shared_home: Path, plugin_id: str, version: str) -> Path:
    safe_plugin = _safe_skill_relative_path(plugin_id)
    safe_version = _safe_skill_relative_path(version)
    return shared_home / "_managed" / _PLUGIN_DISTRIBUTION_ORIGIN / safe_plugin / safe_version


def _lint_downloaded_skill_advisory(skill_root: Path, *, skill_code: str) -> None:
    """Run the shared prompt-injection lint over a DOWNLOADED skill's text and log
    an advisory on hits — closing the asymmetry where CLI-embedded skills were
    linted (update_center) but SkillHub downloads were not.

    Advisory by DEFAULT (content is already checksum-verified; blocking would
    false-positive on legit skill docs, per the update-center first-party
    decision). Set HERMES_SKILLHUB_QUARANTINE_ON_LINT=1 to hard-reject on a hit.
    Never breaks an install on its own error path. MED-5, audit 2026-07-03.
    """
    try:
        from .update_center import _lint_skill_files_for_injection
    except Exception:
        return
    files: list[dict[str, str]] = []
    for path in sorted(skill_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".md", ".mdx", ".txt"}:
            continue
        try:
            files.append(
                {"path": str(path.relative_to(skill_root)),
                 "content": path.read_text(encoding="utf-8", errors="replace")}
            )
        except OSError:
            continue
    try:
        hits = _lint_skill_files_for_injection(files)
    except Exception:
        return
    if not hits:
        return
    logger.warning(
        "[skillhub] downloaded skill %s tripped injection-lint (advisory): %s",
        skill_code, hits[:5],
    )
    if os.environ.get("HERMES_SKILLHUB_QUARANTINE_ON_LINT", "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        raise SkillhubInstallError(
            f"downloaded skill {skill_code} tripped injection-lint and "
            "HERMES_SKILLHUB_QUARANTINE_ON_LINT is on",
            error_code="PACKAGE_INVALID",
        )


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
        # Lint on the cache hit too — a package cached once under advisory mode
        # must still be re-checked (and rejected under strict mode) on every
        # serve, else the cache is a lint bypass. MED-5.
        _lint_downloaded_skill_advisory(existing, skill_code=skill_code)
        return existing

    release_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=release_dir.parent, prefix=f".{release_dir.name}-") as temp_dir:
        staging = Path(temp_dir) / "payload"
        staging.mkdir(parents=True, exist_ok=True)
        _extract_zip_safely(package_bytes, staging)
        resolved = _resolve_skill_root(staging)
        if resolved is None:
            raise SkillhubInstallError("package missing top-level SKILL.md", error_code="PACKAGE_INVALID")
        # Lint BEFORE moving into the canonical cache: a strict-mode reject must
        # leave NO residue on disk. Raising here unwinds the TemporaryDirectory,
        # so the poisoned payload is discarded and cannot be served from the
        # cache-hit early-return on a later call. MED-5.
        _lint_downloaded_skill_advisory(resolved, skill_code=skill_code)
        if release_dir.exists():
            shutil.rmtree(release_dir)
        shutil.move(str(staging), str(release_dir))
    installed = _resolve_skill_root(release_dir)
    if installed is None:
        raise SkillhubInstallError("package missing installed SKILL.md", error_code="PACKAGE_INVALID")
    return installed


def _resolve_plugin_repo_root(base_dir: Path) -> Path | None:
    if (base_dir / plugin_ingest.PLUGIN_MANIFEST_REL).is_file():
        return base_dir
    try:
        children = sorted(path for path in base_dir.iterdir() if path.is_dir())
    except FileNotFoundError:
        return None
    for child in children:
        if child.name == "__MACOSX":
            continue
        if (child / plugin_ingest.PLUGIN_MANIFEST_REL).is_file():
            return child
    return None


def _plugin_release_key(event: dict[str, Any]) -> str | None:
    """Upstream release identity used as the plugin repo cache key.

    The zip's inner plugin.json version is self-declared: upstream has shipped a
    new release whose payload changed but whose inner version stayed "0.1.0"
    (release 168, 2026-07-03), which poisoned the version-keyed cache and served
    stale content. The SkillHub release row (version + release_id) is the only
    trustworthy identity, so key on it; the inner version is a fallback for
    events that predate these fields.
    """
    version = _first_str(event.get("version"))
    release_id = _first_str(event.get("release_id"))
    if version and release_id:
        return f"{version}-r{release_id}"
    return version or release_id or None


def _materialize_plugin_repo(
    shared_home: Path, plugin_id: str, package_bytes: bytes, *, release_key: str | None = None
) -> tuple[Path, bool]:
    """Return ``(release_dir, fresh)``; ``fresh`` is False on a cache hit.

    Callers thread ``fresh`` into ``plugin_ingest.ingest(force=...)`` — a newly
    extracted release must overwrite the name-keyed shared-skill copies, else the
    repo pointer moves but every profile keeps serving the previous release's files.
    """
    try:
        with tempfile.TemporaryDirectory(dir=shared_home, prefix=".plugin-staging-") as temp_dir:
            staging = Path(temp_dir)
            _extract_zip_safely(package_bytes, staging)
            repo_root = _resolve_plugin_repo_root(staging)
            if repo_root is None:
                raise SkillhubInstallError("package missing plugin manifest", error_code="PACKAGE_INVALID")
            try:
                manifest = plugin_ingest.load_plugin_manifest(repo_root)
            except plugin_ingest.PluginIngestError as exc:
                raise SkillhubInstallError(str(exc), error_code="PACKAGE_INVALID") from exc
            manifest_id = _first_str(manifest.get("id"))
            if manifest_id != plugin_id:
                raise SkillhubInstallError("plugin manifest id mismatch", error_code="PACKAGE_INVALID")
            version = _first_str(manifest.get("version"))
            if not version:
                raise SkillhubInstallError("plugin manifest missing version", error_code="PACKAGE_INVALID")
            release_dir = _canonical_plugin_repo_dir(shared_home, plugin_id, release_key or version)
            if (release_dir / plugin_ingest.PLUGIN_MANIFEST_REL).is_file():
                return release_dir, False
            release_dir.parent.mkdir(parents=True, exist_ok=True)
            if release_dir.exists():
                shutil.rmtree(release_dir)
            with tempfile.TemporaryDirectory(dir=release_dir.parent, prefix=f".{release_dir.name}-") as move_dir:
                move_root = Path(move_dir)
                payload_dir = move_root / "payload"
                payload_dir.mkdir(parents=True, exist_ok=True)
                _extract_zip_safely(package_bytes, payload_dir)
                resolved = _resolve_plugin_repo_root(payload_dir)
                if resolved is None:
                    raise SkillhubInstallError("package missing plugin manifest", error_code="PACKAGE_INVALID")
                shutil.move(str(resolved), str(release_dir))
    except plugin_ingest.PluginIngestError as exc:
        raise SkillhubInstallError(str(exc), error_code="PACKAGE_INVALID") from exc
    if not (release_dir / plugin_ingest.PLUGIN_MANIFEST_REL).is_file():
        raise SkillhubInstallError("package missing installed plugin manifest", error_code="PACKAGE_INVALID")
    return release_dir, True


def _plugin_managed_audience_profiles(shared_home: Path, plugin_id: str) -> set[str]:
    path = plugin_ingest._managed_path(shared_home, plugin_id)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return set()
    audience = manifest.get("audience") if isinstance(manifest, dict) else {}
    profiles = audience.get("profiles") if isinstance(audience, dict) else []
    if not isinstance(profiles, list):
        return set()
    return {
        profile
        for profile in (_first_str(value) for value in profiles)
        if profile is not None
    }


def _plugin_managed_audience_mode(shared_home: Path, plugin_id: str) -> str | None:
    path = plugin_ingest._managed_path(shared_home, plugin_id)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    audience = manifest.get("audience") if isinstance(manifest, dict) else {}
    if not isinstance(audience, dict):
        return None
    return _first_str(audience.get("mode"))


def _plugin_sticky_profiles(shared_home: Path, plugin_id: str) -> set[str]:
    """Operator-pinned profiles that must NEVER be auto-uninstalled by any event.

    Read from ``.hermes-plugin-managed/<plugin_id>.sticky.json`` (``{"profiles": [...]}``).
    These are manually-added whitelist entries that are NOT in AiDock's authoritative
    audience, so a full-snapshot reconcile (or inactive) would otherwise remove them
    (incident 2026-07-02). reconcile/inactive exempt them; they always stay in the
    ingest target. Missing/broken file -> empty set (fail-safe, never raises).
    """
    path = plugin_ingest._managed_path(shared_home, plugin_id).parent / f"{plugin_id}.sticky.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return set()
    raw = data.get("profiles") if isinstance(data, dict) else []
    if not isinstance(raw, list):
        return set()
    return {p for p in (_first_str(v) for v in raw) if p is not None}


def _default_plugin_profiles(shared_home: Path, profiles_root: Path) -> set[str]:
    try:
        resolved = _resolve_profile_name(shared_home, "sunke")
    except sqlite3.Error as exc:
        logger.warning("[skillhub] default profile routing unavailable")
        raise SkillhubInstallError(
            "default profile could not be proven", error_code="DEFAULT_PROFILE_UNRESOLVED"
        ) from exc
    if resolved != "sunke" or not (profiles_root / "sunke").is_dir():
        logger.warning("[skillhub] default profile could not be proven")
        raise SkillhubInstallError(
            "default profile could not be proven", error_code="DEFAULT_PROFILE_UNRESOLVED"
        )
    return {"sunke"}


def _plugin_managed_status(shared_home: Path, plugin_id: str) -> str | None:
    path = plugin_ingest._managed_path(shared_home, plugin_id)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    status = manifest.get("status") if isinstance(manifest, dict) else None
    return str(status) if status in {"active", "inactive"} else None


def _set_plugin_status(
    shared_home: Path, plugin_id: str, status: str, *, _lock_held: bool = False
) -> bool:
    if _lock_held:
        return _set_plugin_status_locked(shared_home, plugin_id, status)
    with plugin_ingest._plugin_ingest_lock(shared_home, plugin_id):
        return _set_plugin_status_locked(shared_home, plugin_id, status)


def _set_plugin_status_locked(shared_home: Path, plugin_id: str, status: str) -> bool:
    path = plugin_ingest._managed_path(shared_home, plugin_id)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return False
    if not isinstance(manifest, dict):
        return False
    manifest["status"] = status
    plugin_ingest._write_managed_manifest(shared_home, manifest, dry_run=False)
    return True


def _ingest_plugin_candidate(
    repo_root: Path,
    *,
    audience: str,
    shared: Path,
    profiles: Path,
    force: bool = False,
    activate: bool = False,
    allow_create_distribution: bool = False,
    health_required: bool = False,
    release_version: str | None = None,
    release_id: str | None = None,
) -> dict[str, Any]:
    plugin_id = plugin_ingest.load_plugin_manifest(repo_root)["id"]
    managed_path = plugin_ingest._managed_path(shared, plugin_id)
    try:
        previous = json.loads(managed_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        previous = None
    previous_active = (
        isinstance(previous, dict)
        and previous.get("plugin_id") == plugin_id
        and previous.get("status") in {None, "", "active"}
    )
    try:
        report = plugin_ingest.ingest(
            repo_root,
            audience=audience,
            shared_home=shared,
            profiles_root=profiles,
            force=force,
            activate=activate,
            allow_create_distribution=allow_create_distribution,
            _lock_held=True,
        )
        if previous_active or health_required:
            _assert_plugin_candidate_health(shared, profiles, plugin_id)
        if release_version:
            manifest = json.loads(managed_path.read_text(encoding="utf-8"))
            if (
                not isinstance(manifest, dict)
                or manifest.get("plugin_id") != plugin_id
                or manifest.get("status") != "active"
            ):
                raise plugin_ingest.PluginIngestError(
                    "plugin release metadata found no active candidate"
                )
            same_release = (
                manifest.get("release_version") == release_version
                and manifest.get("release_id") == release_id
                and isinstance(manifest.get("release_installed_at"), int)
            )
            manifest["release_version"] = release_version
            if release_id:
                manifest["release_id"] = release_id
            else:
                manifest.pop("release_id", None)
            if not same_release:
                manifest["release_installed_at"] = int(time.time())
            plugin_ingest._write_managed_manifest(shared, manifest, dry_run=False)
        return report
    except Exception as exc:
        if previous_active:
            plugin_ingest._write_managed_manifest(shared, previous, dry_run=False)
        if isinstance(exc, plugin_ingest.PluginIngestError):
            raise
        raise plugin_ingest.PluginIngestError(
            f"plugin candidate failed: {type(exc).__name__}"
        ) from exc


@contextmanager
def _plugin_update_transaction(
    repo_root: Path,
    *,
    audience: str,
    shared: Path,
    profiles: Path,
):
    plugin = plugin_ingest.load_plugin_manifest(repo_root)
    resolved = plugin_ingest.resolve_audience(audience, profiles_root=profiles)
    was_active = _plugin_managed_status(shared, plugin["id"]) == "active"
    try:
        with plugin_ingest._active_plugin_state_transaction(
            plugin,
            resolved,
            shared_home=shared,
            profiles_root=profiles,
        ):
            yield was_active
    except Exception as exc:
        if isinstance(exc, plugin_ingest.PluginIngestError):
            raise
        raise plugin_ingest.PluginIngestError(
            f"plugin update transaction failed: {type(exc).__name__}"
        ) from exc


def _assert_plugin_candidate_health(
    shared: Path,
    profiles: Path,
    plugin_id: str,
) -> None:
    managed_path = plugin_ingest._managed_path(shared, plugin_id)
    try:
        manifest = json.loads(managed_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        raise plugin_ingest.PluginIngestError("plugin health check could not read active manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("status") != "active":
        raise plugin_ingest.PluginIngestError("plugin health check found no active candidate")
    expected = {
        str(row.get("id"))
        for row in (manifest.get("experts") or [])
        if isinstance(row, dict) and row.get("id")
    }
    if not expected:
        return
    skill_names = {
        str(name)
        for name in (manifest.get("skills") or [])
        if str(name).strip()
    }
    audience = manifest.get("audience") if isinstance(manifest.get("audience"), dict) else {}
    if audience.get("mode") == "profile":
        targets = [profiles / str(name) for name in (audience.get("profiles") or [])]
    elif audience.get("mode") == "all":
        try:
            targets = [
                path
                for path in profiles.iterdir()
                if path.is_dir() and not path.is_symlink()
            ]
        except OSError as exc:
            raise plugin_ingest.PluginIngestError(
                "plugin health check could not enumerate intended profiles"
            ) from exc
    else:
        return
    from . import expert_overlay

    missing = 0
    for profile_home in targets:
        visible = {str(row.get("id")) for row in expert_overlay.list_experts(profile_home)}
        skills_ready = audience.get("mode") != "profile"
        if not skills_ready:
            skills_ready = True
            for name in skill_names:
                source = shared / "skills" / name
                target = profile_home / "skills" / name
                if target.is_symlink():
                    matches = target.resolve() == source.resolve()
                else:
                    matches = (
                        source.is_dir()
                        and target.is_dir()
                        and plugin_ingest._skill_tree_digest(target)
                        == plugin_ingest._skill_tree_digest(source)
                    )
                if not matches:
                    skills_ready = False
                    break
        if not expected.issubset(visible) or not skills_ready:
            missing += 1
    if missing:
        raise plugin_ingest.PluginIngestError(
            f"plugin health check failed for {missing} intended profile(s)"
        )


def _best_effort_plugin_uninstall(
    plugin_id: str,
    *,
    shared: Path,
    profiles: Path,
    profile_subset: list[str] | None = None,
) -> dict[str, Any] | None:
    try:
        return plugin_ingest.uninstall(
            plugin_id,
            shared_home=shared,
            profiles_root=profiles,
            profiles=profile_subset,
            _lock_held=True,
        )
    except plugin_ingest.PluginIngestError as exc:
        if "no managed manifest" in str(exc):
            return None
        raise SkillhubInstallError(str(exc), error_code="PLUGIN_INGEST_FAILED") from exc


def _reconcile_plugin_full_snapshot(
    plugin_id: str,
    *,
    shared: Path,
    profiles: Path,
    new_profiles: set[str],
) -> None:
    """Remove dropped profile installs; strip prior fan-out before re-scoping to profiles.

    STICKY (incident 2026-07-02): operator-pinned profiles are exempt from removal —
    a full-snapshot event carrying AiDock's short list must never wipe the manually-added
    whitelist. The caller also unions sticky into the ingest target so they stay installed.
    """
    sticky = _plugin_sticky_profiles(shared, plugin_id)
    prev_mode = _plugin_managed_audience_mode(shared, plugin_id)
    if prev_mode in (None, "profile"):
        dropped = _plugin_managed_audience_profiles(shared, plugin_id) - set(new_profiles) - sticky
        if dropped:
            _best_effort_plugin_uninstall(
                plugin_id,
                shared=shared,
                profiles=profiles,
                profile_subset=sorted(dropped),
            )
    else:
        # all-mode -> subset: strip the global fan-out; sticky are re-pinned by the
        # caller's ingest(target ∪ sticky), so no sticky loss across the transition.
        _best_effort_plugin_uninstall(plugin_id, shared=shared, profiles=profiles)


def _routing_db_path(shared_home: Path) -> Path:
    return shared_home / "multitenancy.db"


def _resolve_auth_type(event: dict[str, Any]) -> str:
    audience = event.get("audience") if isinstance(event.get("audience"), dict) else None
    nested = _first_str(audience.get("auth_type")) if isinstance(audience, dict) else None
    return nested or _first_str(event.get("auth_type")) or "auth"


def _distribution_config_path(shared_home: Path) -> Path:
    return shared_home / _SKILL_DISTRIBUTION_FILENAME


def _require_distribution_config(
    shared_home: Path,
    *,
    allow_create_distribution: bool,
) -> Path:
    config_path = _distribution_config_path(shared_home)
    if config_path.exists() or allow_create_distribution:
        return config_path
    raise SkillhubInstallError(
        f"no {_SKILL_DISTRIBUTION_FILENAME} at {shared_home} — refusing to create it",
        error_code="DISTRIBUTION_FILE_MISSING",
    )


def _symlink_target(path: Path) -> Path | None:
    try:
        raw_target = os.readlink(path)
    except OSError:
        return None
    target = Path(raw_target)
    if not target.is_absolute():
        target = path.parent / target
    return target.resolve(strict=False)


def _is_owned_shared_skill_target(
    target: Path,
    *,
    shared_home: Path,
    skill_code: str,
) -> bool:
    owned_root = shared_home / "_managed" / _SKILL_DISTRIBUTION_ORIGIN / _safe_skill_relative_path(skill_code)
    try:
        target.relative_to(owned_root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _register_shared_skill_symlink_source(
    *,
    shared_home: Path,
    skill_code: str,
    canonical_skill_root: Path,
) -> dict[str, Any]:
    rel_path = _safe_skill_relative_path(skill_code)
    source_path = shared_home / "skills" / rel_path
    canonical_root = canonical_skill_root.resolve(strict=True)
    if source_path.is_symlink():
        current_target = _symlink_target(source_path)
        if current_target == canonical_root:
            return {"path": str(rel_path), "source_path": str(source_path), "action": "present"}
        if current_target is not None and _is_owned_shared_skill_target(
            current_target,
            shared_home=shared_home,
            skill_code=skill_code,
        ):
            source_path.unlink()
            source_path.symlink_to(canonical_skill_root)
            return {"path": str(rel_path), "source_path": str(source_path), "action": "repointed"}
        return {"path": str(rel_path), "source_path": str(source_path), "action": "skipped-foreign"}
    if source_path.exists():
        return {"path": str(rel_path), "source_path": str(source_path), "action": "skipped-foreign"}
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.symlink_to(canonical_skill_root)
    return {"path": str(rel_path), "source_path": str(source_path), "action": "registered"}


def _register_all_audience_distribution(
    *,
    shared_home: Path,
    skill_code: str,
    canonical_skill_root: Path,
    allow_create_distribution: bool = False,
) -> dict[str, Any]:
    config_path = _require_distribution_config(
        shared_home,
        allow_create_distribution=allow_create_distribution,
    )
    source = _register_shared_skill_symlink_source(
        shared_home=shared_home,
        skill_code=skill_code,
        canonical_skill_root=canonical_skill_root,
    )
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    raw = loaded if isinstance(loaded, dict) else {}
    skills_list = raw.get("skills")
    if not isinstance(skills_list, list):
        skills_list = []
    path_text = str(_safe_skill_relative_path(skill_code))
    entry = {
        "path": path_text,
        "audience": "all",
        "install_mode": "symlink",
        "origin": _SKILL_DISTRIBUTION_ORIGIN,
    }
    raw["skills"] = [
        item
        for item in skills_list
        if not (
            isinstance(item, dict)
            and item.get("path") == path_text
            and item.get("origin") == _SKILL_DISTRIBUTION_ORIGIN
        )
    ]
    raw["skills"].append(entry)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return {
        "path": path_text,
        "audience": "all",
        "install_mode": "symlink",
        "config_path": str(config_path),
        "source": str(source["action"]),
        "entry_written": True,
    }


def _unregister_all_audience_distribution(
    *,
    shared_home: Path,
    skill_code: str,
) -> dict[str, Any]:
    rel_path = _safe_skill_relative_path(skill_code)
    path_text = str(rel_path)
    config_path = _distribution_config_path(shared_home)
    source_path = shared_home / "skills" / rel_path
    if not config_path.exists():
        return {"status": "absent", "entry_removed": False, "source": "absent"}

    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw = loaded if isinstance(loaded, dict) else {}
    skills_list = raw.get("skills")
    if not isinstance(skills_list, list):
        skills_list = []
    kept = [
        item
        for item in skills_list
        if not (
            isinstance(item, dict)
            and item.get("path") == path_text
            and item.get("origin") == _SKILL_DISTRIBUTION_ORIGIN
        )
    ]
    entry_removed = len(kept) != len(skills_list)
    raw["skills"] = kept
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    source = "absent"
    if source_path.is_symlink():
        target = _symlink_target(source_path)
        if target is not None and _is_owned_shared_skill_target(
            target,
            shared_home=shared_home,
            skill_code=skill_code,
        ):
            source_path.unlink()
            source = "removed"
        else:
            source = "skipped-foreign"
    elif source_path.exists():
        source = "skipped-foreign"

    return {
        "status": "removed" if entry_removed or source == "removed" else "absent",
        "entry_removed": entry_removed,
        "source": source,
    }


def _has_all_audience_distribution(
    *,
    shared_home: Path,
    skill_code: str,
) -> bool:
    config_path = _distribution_config_path(shared_home)
    if not config_path.exists():
        return False
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw = loaded if isinstance(loaded, dict) else {}
    skills_list = raw.get("skills")
    if not isinstance(skills_list, list):
        return False
    path_text = str(_safe_skill_relative_path(skill_code))
    return any(
        isinstance(item, dict)
        and item.get("path") == path_text
        and item.get("origin") == _SKILL_DISTRIBUTION_ORIGIN
        and item.get("audience") == "all"
        for item in skills_list
    )


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


def _remove_keephub_lock_entry(
    *,
    profile_home: Path,
    skill_code: str,
) -> None:
    rel_path = _safe_skill_relative_path(skill_code)
    path = profile_home / "skills" / _LOCK_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return
    installed = raw.get("installed") if isinstance(raw, dict) else None
    merged = dict(installed) if isinstance(installed, dict) else {}
    merged.pop(str(rel_path), None)
    path.write_text(
        json.dumps({"installed": merged}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _uninstall_from_profile(
    *,
    profile_home: Path,
    skill_code: str,
) -> dict[str, Any]:
    profile = Path(profile_home).expanduser()
    rel_path = _safe_skill_relative_path(skill_code)
    key = str(rel_path)
    managed = _read_manifest(profile, MANAGED_SKILL_MANIFEST)
    entry = managed.get(key) if isinstance(managed, dict) else None
    if not isinstance(entry, dict):
        return {"status": "absent", "profile": profile.name}
    if entry.get("origin") != _SKILL_DISTRIBUTION_ORIGIN:
        return {"status": "skipped-foreign", "profile": profile.name}

    skill_dir = profile / "skills" / rel_path
    if skill_dir.is_symlink():
        skill_dir.unlink()
    elif skill_dir.is_dir():
        shutil.rmtree(skill_dir)
    elif skill_dir.exists():
        skill_dir.unlink()

    managed.pop(key, None)
    _write_manifest(profile, MANAGED_SKILL_MANIFEST, managed)
    _remove_keephub_lock_entry(profile_home=profile, skill_code=skill_code)
    return {"status": "removed", "profile": profile.name}


def _profiles_with_skill(
    *,
    profiles_root: Path,
    skill_code: str,
) -> list[Path]:
    # ponytail: O(profiles) manifest scan; build an index if throughput matters.
    rel_path = _safe_skill_relative_path(skill_code)
    key = str(rel_path)
    try:
        profiles = sorted(path for path in profiles_root.iterdir() if path.is_dir())
    except FileNotFoundError:
        return []
    matches: list[Path] = []
    for profile_home in profiles:
        entry = _read_manifest(profile_home, MANAGED_SKILL_MANIFEST).get(key)
        if isinstance(entry, dict) and entry.get("origin") == _SKILL_DISTRIBUTION_ORIGIN:
            matches.append(profile_home)
    return matches


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


def _resolve_installed_release(
    *,
    profiles_root: Path,
    skill_code: str,
) -> dict[str, Any] | None:
    """Recover an already-materialized canonical release for ``skill_code``.

    ``permission_approved`` (and similar add-only grants) frequently omit
    release_id/version/download_url — the skill is already live and we only need
    to grant it to new users. Read the managed-manifest entry of any profile that
    already has it to recover the canonical skill root, version, and release_id.
    Returns ``None`` when no profile has a usable installed copy.
    """
    rel_path = _safe_skill_relative_path(skill_code)
    key = str(rel_path)
    for profile_home in _profiles_with_skill(profiles_root=profiles_root, skill_code=skill_code):
        entry = _read_manifest(profile_home, MANAGED_SKILL_MANIFEST).get(key)
        if not isinstance(entry, dict):
            continue
        canonical_raw = _first_str(entry.get("target"), entry.get("source"))
        version = _first_str(entry.get("version"))
        if not canonical_raw or not version:
            continue
        canonical_root = Path(canonical_raw)
        if _resolve_skill_root(canonical_root) is None:
            continue
        return {
            "canonical_skill_root": canonical_root,
            "version": version,
            "release_id": _first_str(entry.get("release_id")),
        }
    return None


def _install_from_existing_release(
    *,
    event: dict[str, Any],
    skill_code: str,
    shared: Path,
    profiles: Path,
) -> dict[str, Any]:
    """Grant an already-installed skill to new users without a download.

    Add-only (incremental): installs the canonical release into each new audience
    profile; never reconciles/removes. Raises ``PACKAGE_INVALID`` only when the
    skill is not installed anywhere (nothing to copy) — a genuinely-unknown skill
    with no package.
    """
    resolved = _resolve_installed_release(profiles_root=profiles, skill_code=skill_code)
    if resolved is None:
        raise SkillhubInstallError("event missing package fields", error_code="PACKAGE_INVALID")
    canonical_skill_root = resolved["canonical_skill_root"]
    # Grant-to-new-profile from cache must also lint (see cache-hit note). MED-5.
    _lint_downloaded_skill_advisory(Path(canonical_skill_root), skill_code=skill_code)
    version = resolved["version"]
    release_id = _first_str(event.get("release_id")) or resolved["release_id"]
    audience = event.get("audience") if isinstance(event.get("audience"), dict) else {}
    users = audience.get("users") if isinstance(audience, dict) else []
    results: dict[str, Any] = {
        "action": "install",
        "mode": "from_existing",
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


def _process_plugin_event(
    event: dict[str, Any],
    *,
    shared: Path,
    profiles: Path,
    downloader: Callable[[str], bytes] | None,
    allow_create_distribution: bool = False,
) -> dict[str, Any]:
    plugin_id = _first_str(event.get("skill_code"))
    if not plugin_id:
        raise SkillhubInstallError("plugin event missing skill_code", error_code="PACKAGE_INVALID")
    with plugin_ingest._plugin_ingest_lock(shared, plugin_id):
        return _process_plugin_event_locked(
            event,
            shared=shared,
            profiles=profiles,
            downloader=downloader,
            allow_create_distribution=allow_create_distribution,
        )


def _process_plugin_event_locked(
    event: dict[str, Any],
    *,
    shared: Path,
    profiles: Path,
    downloader: Callable[[str], bytes] | None,
    allow_create_distribution: bool = False,
) -> dict[str, Any]:
    plugin_id = _first_str(event.get("skill_code"))
    if not plugin_id:
        raise SkillhubInstallError("plugin event missing skill_code", error_code="PACKAGE_INVALID")

    explicit_skill_status = bool(event.get("skill_status_explicit", "skill_status" in event))
    skill_status = _first_str(event.get("skill_status")) or "active"
    activate = explicit_skill_status and skill_status == "active"
    if skill_status == "pending":
        return {"action": "skipped_pending", "plugin_id": plugin_id, "item_type": "plugin"}
    if skill_status == "inactive":
        try:
            report = plugin_ingest.deactivate(
                plugin_id,
                shared_home=shared,
                profiles_root=profiles,
                _lock_held=True,
            )
        except plugin_ingest.PluginIngestError as exc:
            raise SkillhubInstallError(
                str(exc), error_code="PLUGIN_INGEST_FAILED"
            ) from exc
        return {
            "action": "plugin_disable",
            "plugin_id": plugin_id,
            "status": "inactive",
            "uninstall": report,
        }
    if not explicit_skill_status and _plugin_managed_status(shared, plugin_id) == "inactive":
        return {
            "action": "plugin_skipped_inactive",
            "plugin_id": plugin_id,
            "status": "inactive",
        }

    auth_type = _resolve_auth_type(event)
    download_url = _first_str(event.get("download_url"))
    if auth_type.strip().lower() == "all":
        # Resolve the repo BEFORE any uninstall: the no-download path reads the cached repo
        # pointer from the managed manifest, and uninstall unlinks that manifest. If we can't
        # get a repo, bail with plugin_no_package WITHOUT having destroyed the existing install.
        if download_url:
            package_bytes = _download_package(download_url, downloader)
            _verify_checksum(package_bytes, _first_str(event.get("checksum_sha256")))
            repo_root, fresh_repo = _materialize_plugin_repo(
                shared, plugin_id, package_bytes, release_key=_plugin_release_key(event)
            )
        else:
            fresh_repo = False
            managed_path = plugin_ingest._managed_path(shared, plugin_id)
            try:
                managed = json.loads(managed_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
                managed = None
            repo_raw = _first_str(managed.get("repo")) if isinstance(managed, dict) else None
            repo_root = Path(repo_raw).expanduser() if repo_raw else None
            if repo_root is None or not repo_root.exists() or not (repo_root / plugin_ingest.PLUGIN_MANIFEST_REL).is_file():
                return {
                    "action": "plugin_no_package",
                    "plugin_id": plugin_id,
                    "reason": "no download_url and no cached plugin repo",
                }
        try:
            with _plugin_update_transaction(
                repo_root,
                audience="all",
                shared=shared,
                profiles=profiles,
            ) as was_active:
                # Repo is secured before uninstall. The outer transaction keeps the
                # previous manifest, fanout, sources and profile trees recoverable.
                _best_effort_plugin_uninstall(
                    plugin_id,
                    shared=shared,
                    profiles=profiles,
                )
                report = _ingest_plugin_candidate(
                    repo_root,
                    audience="all",
                    shared=shared,
                    profiles=profiles,
                    # Thread the caller's opt-in instead of hardcoding True. Creating
                    # skill-distribution.yaml where none exists overrides every
                    # profile's default-skill source; the skill path already gates
                    # this behind allow_create_distribution (default False), and the
                    # plugin path must match. MED-4, audit 2026-07-03.
                    allow_create_distribution=allow_create_distribution,
                    force=fresh_repo,
                    activate=activate,
                    health_required=was_active,
                    release_version=_first_str(event.get("version")),
                    release_id=_first_str(event.get("release_id")),
                )
        except plugin_ingest.PluginIngestError as exc:
            raise SkillhubInstallError(str(exc), error_code="PLUGIN_INGEST_FAILED") from exc
        return {"action": "plugin_install", "mode": "all", "plugin_id": plugin_id, "ingest": report}

    audience = event.get("audience") if isinstance(event.get("audience"), dict) else {}
    users = audience.get("users") if isinstance(audience, dict) else []
    event_type = _first_str(event.get("event_type")) or ""
    full_snapshot = event_type in _FULL_SNAPSHOT_EVENT_TYPES
    incremental = event_type == "skill.permission_approved"
    retained = (
        _plugin_managed_audience_profiles(shared, plugin_id)
        if full_snapshot
        and skill_status == "active"
        and _plugin_managed_status(shared, plugin_id) == "inactive"
        else set()
    )
    if not isinstance(users, list):
        users = []

    user_report: dict[str, dict[str, Any]] = {}
    resolved_names: list[str] = []
    for entry in users:
        if not isinstance(entry, dict):
            continue
        ldap = _first_str(entry.get("profile_id"), entry.get("employee_id"), entry.get("open_id"))
        if not ldap:
            continue
        profile_name = _resolve_profile_name(shared, ldap)
        if not profile_name:
            user_report[ldap] = {"status": "PROFILE_NOT_FOUND", "profile": None}
            continue
        profiles.joinpath(profile_name).mkdir(parents=True, exist_ok=True)
        user_report[ldap] = {"status": "resolved", "profile": profile_name}
        if profile_name not in resolved_names:
            resolved_names.append(profile_name)

    if (
        incremental
        and _plugin_managed_audience_mode(shared, plugin_id) == "all"
        and (not activate or _plugin_managed_status(shared, plugin_id) == "active")
    ):
        # Plugin is already all-audience (org-sync covers everyone) → an incremental grant
        # is redundant. Keep the all-mode manifest intact; a per-profile install here would
        # downgrade the manifest to profile-mode and orphan the all-distribution on a later
        # inactive uninstall.
        return {"action": "plugin_noop_already_all", "plugin_id": plugin_id, "users": user_report}

    if download_url:
        package_bytes = _download_package(download_url, downloader)
        _verify_checksum(package_bytes, _first_str(event.get("checksum_sha256")))
        repo_root, fresh_repo = _materialize_plugin_repo(
            shared, plugin_id, package_bytes, release_key=_plugin_release_key(event)
        )
        defaults = _default_plugin_profiles(shared, profiles)
        target = set(resolved_names) | defaults | retained
        if incremental:
            target |= _plugin_managed_audience_profiles(shared, plugin_id)
        if full_snapshot:
            # sticky whitelist always stays installed + in the manifest audience, so a
            # short upstream list can never shrink it away (reconcile also exempts sticky).
            target |= _plugin_sticky_profiles(shared, plugin_id)
        if not target:
            return {
                "action": "plugin_install",
                "plugin_id": plugin_id,
                "users": user_report,
                "ingest": None,
            }
        target_audience = ",".join(sorted(target))
        try:
            with _plugin_update_transaction(
                repo_root,
                audience=target_audience,
                shared=shared,
                profiles=profiles,
            ) as was_active:
                if full_snapshot:
                    _reconcile_plugin_full_snapshot(
                        plugin_id,
                        shared=shared,
                        profiles=profiles,
                        new_profiles=set(resolved_names) | defaults | retained,
                    )
                report = _ingest_plugin_candidate(
                    repo_root,
                    audience=target_audience,
                    shared=shared,
                    profiles=profiles,
                    force=fresh_repo,
                    activate=activate,
                    health_required=was_active,
                    release_version=_first_str(event.get("version")),
                    release_id=_first_str(event.get("release_id")),
                )
        except plugin_ingest.PluginIngestError as exc:
            raise SkillhubInstallError(str(exc), error_code="PLUGIN_INGEST_FAILED") from exc
        return {
            "action": "plugin_install",
            "plugin_id": plugin_id,
            "users": user_report,
            "ingest": report,
        }

    managed_path = plugin_ingest._managed_path(shared, plugin_id)
    try:
        managed = json.loads(managed_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        managed = None
    repo_raw = _first_str(managed.get("repo")) if isinstance(managed, dict) else None
    repo_root = Path(repo_raw).expanduser() if repo_raw else None
    if repo_root is None or not repo_root.exists() or not (repo_root / plugin_ingest.PLUGIN_MANIFEST_REL).is_file():
        return {
            "action": "plugin_no_package",
            "plugin_id": plugin_id,
            "reason": "no download_url and no cached plugin repo",
        }

    defaults = _default_plugin_profiles(shared, profiles)
    target = set(resolved_names) | defaults | retained
    if incremental:
        target |= _plugin_managed_audience_profiles(shared, plugin_id)
    if full_snapshot:
        # sticky whitelist stays in the manifest audience on this cached-repo path too,
        # else the ingest below would rewrite the manifest without sticky (codex 2026-07-02).
        target |= _plugin_sticky_profiles(shared, plugin_id)
    if not target:
        return {
            "action": "plugin_install",
            "plugin_id": plugin_id,
            "mode": "from_existing",
            "users": user_report,
            "ingest": None,
        }
    target_audience = ",".join(sorted(target))
    try:
        with _plugin_update_transaction(
            repo_root,
            audience=target_audience,
            shared=shared,
            profiles=profiles,
        ) as was_active:
            if full_snapshot:
                _reconcile_plugin_full_snapshot(
                    plugin_id,
                    shared=shared,
                    profiles=profiles,
                    new_profiles=set(resolved_names) | defaults | retained,
                )
            report = _ingest_plugin_candidate(
                repo_root,
                audience=target_audience,
                shared=shared,
                profiles=profiles,
                activate=activate,
                health_required=was_active,
                release_version=_first_str(event.get("version")),
                release_id=_first_str(event.get("release_id")),
            )
    except plugin_ingest.PluginIngestError as exc:
        raise SkillhubInstallError(str(exc), error_code="PLUGIN_INGEST_FAILED") from exc
    return {
        "action": "plugin_install",
        "mode": "from_existing",
        "plugin_id": plugin_id,
        "users": user_report,
        "ingest": report,
    }


def process_event(
    event: dict[str, Any],
    *,
    shared_home: Path,
    profiles_root: Path | None = None,
    downloader: Callable[[str], bytes] | None = None,
    allow_create_distribution: bool = False,
) -> dict[str, Any]:
    """Materialize one queued skillhub event.

    Per-user resolution misses are reported in the results payload. Whole-event
    failures raise ``SkillhubInstallError`` with a stable error code.
    """
    event_type = _first_str(event.get("event_type")) or ""
    skill_status = _first_str(event.get("skill_status")) or "active"
    skill_code = _first_str(event.get("skill_code"))
    item_type = (_first_str(event.get("item_type")) or "skill").strip().lower()
    shared = Path(shared_home).expanduser()
    profiles = (profiles_root or shared / "profiles").expanduser()
    if item_type == "plugin":
        result = _process_plugin_event(
            event,
            shared=shared,
            profiles=profiles,
            downloader=downloader,
            allow_create_distribution=allow_create_distribution,
        )
        return result
    if skill_status == "pending":
        # Skill not yet live — never download or install. Idempotent no-op.
        return {"action": "skipped_pending", "skill_code": skill_code}
    if event_type == "skill.status_changed" and skill_status == "inactive":
        if not skill_code:
            return {"action": "uninstall_inactive", "skill_code": None, "removed": []}
        removed = [
            _uninstall_from_profile(profile_home=profile_home, skill_code=skill_code)
            for profile_home in _profiles_with_skill(
                profiles_root=profiles,
                skill_code=skill_code,
            )
        ]
        distribution = _unregister_all_audience_distribution(
            shared_home=shared,
            skill_code=skill_code,
        )
        return {
            "action": "uninstall_inactive",
            "skill_code": skill_code,
            "removed": removed,
            "distribution": distribution,
        }

    download_url = _first_str(event.get("download_url"))
    version = _first_str(event.get("version"))
    if not skill_code:
        raise SkillhubInstallError("event missing package fields", error_code="PACKAGE_INVALID")
    if not download_url or not version:
        # No package to fetch (common for permission_approved: grant an existing
        # skill to new users). Install the already-materialized release if one
        # exists; a never-installed skill with no package is PACKAGE_INVALID.
        return _install_from_existing_release(
            event=event,
            skill_code=skill_code,
            shared=shared,
            profiles=profiles,
        )

    release_id = _first_str(event.get("release_id"))
    auth_type = _resolve_auth_type(event)
    auth_type_normalized = auth_type.strip().lower()
    if auth_type.strip().lower() == "all":
        _require_distribution_config(
            shared,
            allow_create_distribution=allow_create_distribution,
        )
    package_bytes = _download_package(download_url, downloader)
    _verify_checksum(package_bytes, _first_str(event.get("checksum_sha256")))
    canonical_skill_root = _materialize_canonical_skill(
        shared_home=shared,
        skill_code=skill_code,
        version=version,
        package_bytes=package_bytes,
    )
    if auth_type_normalized == "all":
        distribution = _register_all_audience_distribution(
            shared_home=shared,
            skill_code=skill_code,
            canonical_skill_root=canonical_skill_root,
            allow_create_distribution=allow_create_distribution,
        )
        return {
            "action": "install",
            "mode": "all",
            "skill_code": skill_code,
            "version": version,
            "release_id": release_id,
            "distribution": distribution,
        }

    audience = event.get("audience") if isinstance(event.get("audience"), dict) else {}
    users = audience.get("users") if isinstance(audience, dict) else []
    results: dict[str, Any] = {
        "action": "install",
        "skill_code": skill_code,
        "version": version,
        "release_id": release_id,
        "users": {},
    }
    full_snapshot = event_type in _FULL_SNAPSHOT_EVENT_TYPES
    distribution = None
    if full_snapshot and _has_all_audience_distribution(shared_home=shared, skill_code=skill_code):
        distribution = _unregister_all_audience_distribution(
            shared_home=shared,
            skill_code=skill_code,
        )

    install_targets: list[tuple[str, Path]] = []
    keep_profiles: set[Path] = set()
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
        install_targets.append((ldap, profile_home))
        if full_snapshot:
            keep_profiles.add(profile_home)

    if full_snapshot:
        removed = [
            _uninstall_from_profile(profile_home=profile_home, skill_code=skill_code)
            for profile_home in _profiles_with_skill(
                profiles_root=profiles,
                skill_code=skill_code,
            )
            if profile_home not in keep_profiles
        ]
        if removed:
            results["removed"] = removed
    if distribution is not None:
        results["distribution"] = distribution

    for ldap, profile_home in install_targets:
        profile_home.mkdir(parents=True, exist_ok=True)
        results["users"][ldap] = _install_into_profile(
            profile_home=profile_home,
            skill_code=skill_code,
            version=version,
            release_id=release_id,
            canonical_skill_root=canonical_skill_root,
        )
    return results


def _item_type_from_raw_payload(raw_payload: Any) -> str | None:
    """Recover item_type (skill|plugin) from the stored raw body.

    item_type has no dedicated column; the raw_payload column already holds the
    full inbound body, so parse it from there rather than migrating the schema.
    """
    if not raw_payload:
        return None
    try:
        payload = json.loads(raw_payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    skill_block = payload.get("skill") if isinstance(payload.get("skill"), dict) else {}
    return _first_str(payload.get("item_type"), skill_block.get("item_type"))


def _event_from_row(row: dict[str, Any]) -> dict[str, Any]:
    audience = json.loads(row["audience_json"]) if row.get("audience_json") else {}
    raw_payload = row.get("raw_payload")
    explicit_skill_status = False
    try:
        raw = json.loads(raw_payload) if raw_payload else {}
        skill = raw.get("skill") if isinstance(raw, dict) and isinstance(raw.get("skill"), dict) else {}
        explicit_skill_status = bool(
            isinstance(raw, dict)
            and ("skill_status" in raw or "status" in skill)
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return {
        "event_type": row.get("event_type"),
        "skill_code": row.get("skill_code"),
        "release_id": row.get("release_id"),
        "version": row.get("version"),
        "download_url": row.get("download_url"),
        "checksum_sha256": row.get("checksum_sha256"),
        "skill_status": row.get("skill_status"),
        "skill_status_explicit": explicit_skill_status,
        "item_type": _item_type_from_raw_payload(row.get("raw_payload")),
        "audience": audience,
    }


def _process_row(
    row: dict[str, Any],
    *,
    shared: Path,
    profiles: Path,
    event_store: SkillhubEventStore,
    downloader: Callable[[str], bytes] | None,
) -> dict[str, Any]:
    try:
        results = process_event(
            _event_from_row(row),
            shared_home=shared,
            profiles_root=profiles,
            downloader=downloader,
        )
        if event_store.mark_installed(str(row["event_id"]), results):
            if results.get("action") in {
                "skipped_pending",
                "skipped_inactive",
                "plugin_no_audience",
                "plugin_no_package",
            }:
                return {"status": "skipped", "results": results}
            return {"status": "installed", "results": results}
    except SkillhubInstallError as exc:
        if event_store.mark_failed(str(row["event_id"]), exc.error_code, str(exc)):
            return {"status": "failed"}
    except Exception as exc:
        if event_store.mark_failed(str(row["event_id"]), "INTERNAL_ERROR", str(exc)):
            return {"status": "failed"}
    return {"status": "skipped"}


def process_one(
    *,
    event_id: str,
    shared_home: Path | None = None,
    profiles_root: Path | None = None,
    store: SkillhubEventStore | None = None,
    downloader: Callable[[str], bytes] | None = None,
) -> dict[str, Any]:
    shared = (shared_home or _default_shared_home()).expanduser()
    profiles = (profiles_root or shared / "profiles").expanduser()
    event_store = store or get_event_store()
    row = event_store.get(str(event_id))
    if row is None or row.get("status") not in {"queued", "queued_unknown_type"}:
        return {"processed": 0}
    result = _process_row(
        row,
        shared=shared,
        profiles=profiles,
        event_store=event_store,
        downloader=downloader,
    )
    return {"processed": 1, "status": result["status"]}


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

    rows = event_store.list_queued(limit=limit)
    index = 0
    while index < len(rows):
        row = rows[index]
        event = _event_from_row(row)
        batch = [row]
        if event.get("item_type") == "plugin" and event.get("event_type") == "skill.permission_approved":
            key = tuple(row.get(name) for name in (
                "skill_code", "release_id", "version", "download_url",
                "checksum_sha256", "skill_status", "auth_type",
            ))
            while index + len(batch) < len(rows):
                candidate = rows[index + len(batch)]
                candidate_event = _event_from_row(candidate)
                candidate_key = tuple(candidate.get(name) for name in (
                    "skill_code", "release_id", "version", "download_url",
                    "checksum_sha256", "skill_status", "auth_type",
                ))
                if (
                    candidate_event.get("item_type") != "plugin"
                    or candidate_event.get("event_type") != "skill.permission_approved"
                    or candidate_key != key
                ):
                    break
                batch.append(candidate)

        if len(batch) == 1:
            results = [_process_row(
                row,
                shared=shared,
                profiles=profiles,
                event_store=event_store,
                downloader=downloader,
            )]
        else:
            merged = event
            users: list[dict[str, Any]] = []
            seen_users: set[str] = set()
            for batch_row in batch:
                batch_event = _event_from_row(batch_row)
                audience = batch_event.get("audience") or {}
                for user in audience.get("users") or []:
                    marker = json.dumps(user, sort_keys=True, ensure_ascii=False)
                    if marker not in seen_users:
                        seen_users.add(marker)
                        users.append(user)
            merged["audience"] = {**(merged.get("audience") or {}), "users": users}
            try:
                batch_result = process_event(
                    merged,
                    shared_home=shared,
                    profiles_root=profiles,
                    downloader=downloader,
                )
                batch_result = {**batch_result, "batched_events": len(batch)}
                status = "skipped" if batch_result.get("action") in {
                    "skipped_pending", "skipped_inactive", "plugin_no_audience", "plugin_no_package"
                } else "installed"
                results = []
                for batch_row in batch:
                    event_store.mark_installed(str(batch_row["event_id"]), batch_result)
                    results.append({"status": status, "results": batch_result})
            except SkillhubInstallError as exc:
                results = []
                for batch_row in batch:
                    event_store.mark_failed(str(batch_row["event_id"]), exc.error_code, str(exc))
                    results.append({"status": "failed"})
            except Exception as exc:
                results = []
                for batch_row in batch:
                    event_store.mark_failed(str(batch_row["event_id"]), "INTERNAL_ERROR", str(exc))
                    results.append({"status": "failed"})

        for result in results:
            summary["processed"] += 1
            status = result.get("status")
            if status in {"installed", "failed", "skipped"}:
                summary[status] += 1
        index += len(batch)
    return summary


def _drain_queued(shared_home: Path, key: str) -> None:
    try:
        while True:
            while run_worker(shared_home=shared_home, limit=500)["processed"]:
                pass
            with _DRAIN_LOCK:
                state = _DRAIN_STATES.get(key)
                if state is not None and state["requested"]:
                    state["requested"] = False
                    continue
                _DRAIN_STATES.pop(key, None)
                return
    except Exception:
        logger.exception("[skillhub] queued-event drain failed")
        with _DRAIN_LOCK:
            _DRAIN_STATES.pop(key, None)


def schedule_drain(shared_home: Path | None = None) -> Future[Any]:
    """Wake the isolated serial SkillHub lane; repeated wakes collapse safely."""
    shared = (shared_home or _default_shared_home()).expanduser()
    key = str(shared.resolve())
    with _DRAIN_LOCK:
        state = _DRAIN_STATES.get(key)
        if state is not None:
            state["requested"] = True
            return state["future"]
        state = {"requested": False, "future": None}
        _DRAIN_STATES[key] = state
        future = _DRAIN_EXECUTOR.submit(_drain_queued, shared, key)
        state["future"] = future
        return future
