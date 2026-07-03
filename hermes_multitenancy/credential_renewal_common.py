"""Shared helpers for credential renewal hardening (L1/L2/L3/L4/L5).

Single source of truth for:
  * ``.needs_reauth`` marker filenames, reasons, and atomic writers
  * UAT validation predicates (offline_access scope, non-empty refresh_token)
  * UAT path enumeration (both legacy ``<shared>/feishu_uat/`` and per-profile
    ``<shared>/profiles/<name>/feishu_uat/``)
  * Fixture path filtering (never touch ``feishu_uat.fixtures.bak``)
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

logger = logging.getLogger(__name__)

REASON_EMPTY_REFRESH_TOKEN = "empty_refresh_token"
REASON_SCOPE_STRIPPED_BY_FEISHU = "scope_stripped_by_feishu"
REASON_ACCESS_TOKEN_EXPIRED = "access_token_expired"
REASON_REFRESH_TOKEN_EXPIRED = "refresh_token_expired"
REASON_REFRESH_REJECTED = "refresh_rejected"
REASON_REFRESH_DIAGNOSTIC = "refresh_diagnostic"

SCOPE_STRIPPED_REASONS = frozenset({REASON_SCOPE_STRIPPED_BY_FEISHU})

FIXTURE_DIRNAMES = frozenset({"feishu_uat.fixtures.bak"})


# Benign, non-secret env vars a third-party credential-status/login CLI
# (npx @lark-project/meegle, kep-auth, node keep-record) legitimately needs.
# The vault master decryption key (HERMES_MULTITENANCY_CREDENTIAL_KEY /
# HERMES_CREDENTIAL_KEY), FEISHU_APP_SECRET, and any other secret promoted into
# os.environ are deliberately absent — a positive allowlist keeps them out of
# third-party subprocess environments (CWE-200). Everything a CLI needs beyond
# these benign vars is passed explicitly by the caller via ``overrides``.
_STATUS_SUBPROCESS_ENV_ALLOWLIST = frozenset({
    # process basics / locale
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TZ", "TMPDIR",
    "LANG", "LC_ALL", "LC_CTYPE", "LANGUAGE",
    # outbound network — proxies + TLS trust (CLIs reach IdP / npm registry)
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    # node / npm package resolution + cache (npx resolves under HOME)
    "NODE_PATH", "NPM_CONFIG_CACHE", "npm_config_cache", "NPM_CONFIG_PREFIX",
})


def build_status_subprocess_env(overrides: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Minimal env for third-party credential-status/login subprocesses.

    Starts from an allowlisted subset of ``os.environ`` (benign vars only) and
    layers the caller's explicit ``overrides`` on top. Replaces the historical
    ``{**os.environ, ...}`` pattern so secrets promoted into the parent process
    env (vault master key, FEISHU_APP_SECRET) can never leak into npx / node /
    kep-auth child environments. See CRIT-1, audit 2026-07-03.
    """
    env = {k: v for k, v in os.environ.items() if k in _STATUS_SUBPROCESS_ENV_ALLOWLIST}
    if overrides:
        env.update({k: str(v) for k, v in overrides.items()})
    return env


@dataclass(frozen=True)
class UatLocation:
    """One UAT file on disk plus the profile_name it's bound to (legacy → '')."""

    path: Path
    profile_name: str
    open_id: str
    legacy: bool


def is_fixture_path(path: Path) -> bool:
    """Return True if a UAT path lives inside a fixture or archived test directory."""
    parts = path.parts
    return any(p in FIXTURE_DIRNAMES for p in parts)


def parse_scopes(raw: str | Iterable[str] | None) -> list[str]:
    """Split a Feishu scope string into a deduped sorted list (whitespace-safe)."""
    if raw is None:
        return []
    if isinstance(raw, str):
        values = raw.replace(",", " ").split()
    else:
        values = [str(item) for item in raw]
    return sorted({value.strip() for value in values if value.strip()})


def payload_has_offline_access(payload: dict[str, Any]) -> bool:
    return "offline_access" in parse_scopes(payload.get("scope"))


def payload_has_refresh_token(payload: dict[str, Any]) -> bool:
    return bool(str(payload.get("refresh_token") or "").strip())


def payload_has_access_token(payload: dict[str, Any]) -> bool:
    return bool(str(payload.get("access_token") or "").strip())


def _now_ms() -> int:
    return int(time.time() * 1000)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def payload_access_expired(payload: dict[str, Any], *, headroom_seconds: int = 0) -> bool:
    expires_at = _as_int(
        payload.get("expires_at") or payload.get("expire_at") or payload.get("access_token_expires_at")
    )
    if not expires_at:
        return False
    return _now_ms() >= expires_at - int(headroom_seconds) * 1000


def payload_refresh_expired(payload: dict[str, Any]) -> bool:
    refresh_expires_at = _as_int(payload.get("refresh_expires_at"))
    if not refresh_expires_at:
        return False
    return refresh_expires_at <= _now_ms()


def marker_path_for(uat_path: Path) -> Path:
    """`<uat>.json` → `<uat>.json.needs_reauth` (sidecar in same dir)."""
    return uat_path.with_name(uat_path.name + ".needs_reauth") if uat_path.suffix == ".json" else (
        uat_path.with_name(uat_path.stem + ".needs_reauth")
    )


def marker_path_for_open_id(parent_dir: Path, open_id: str) -> Path:
    """Compute the canonical marker path used when only ``parent_dir`` + ``open_id`` are known."""
    return parent_dir / f"{open_id}.needs_reauth"


def refresh_diagnostic_path_for_open_id(parent_dir: Path, open_id: str) -> Path:
    """Compute the non-user-facing refresh diagnostic sidecar path."""
    return parent_dir / f"{open_id}.refresh_diagnostic"


def refresh_diagnostic_path_for_reauth_marker(marker_path: Path) -> Path:
    """Compute the diagnostic sidecar path corresponding to a reauth marker."""
    name = marker_path.name
    if name.endswith(".json.needs_reauth"):
        open_id = name[: -len(".json.needs_reauth")]
    elif name.endswith(".needs_reauth"):
        open_id = name[: -len(".needs_reauth")]
    else:
        open_id = marker_path.stem
    return refresh_diagnostic_path_for_open_id(marker_path.parent, open_id)


def write_needs_reauth_marker(
    marker_path: Path,
    *,
    reason: str,
    detail: str = "",
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Atomic write of a `.needs_reauth` marker, mode 0600, idempotent."""
    payload: dict[str, Any] = {
        "reason": reason,
        "ts": int(time.time()),
    }
    if detail:
        payload["detail"] = detail
    if extra:
        payload.update({k: v for k, v in extra.items() if k not in payload})
    marker_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = marker_path.with_name(f".{marker_path.name}.{secrets.token_hex(6)}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, marker_path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def write_refresh_diagnostic_marker(
    marker_path: Path,
    *,
    detail: str,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Atomic write of non-user-facing refresh diagnostic state."""
    payload: dict[str, Any] = {
        "reason": REASON_REFRESH_DIAGNOSTIC,
        "ts": int(time.time()),
        "detail": detail,
        "actionable": False,
    }
    if extra:
        payload.update({k: v for k, v in extra.items() if k not in payload})
    marker_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = marker_path.with_name(f".{marker_path.name}.{secrets.token_hex(6)}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, marker_path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def preserve_reauth_marker_as_refresh_diagnostic(
    marker_path: Path,
    marker_body: dict[str, Any],
    *,
    source: str,
) -> None:
    """Preserve a cleared non-actionable reauth marker as diagnostic state."""
    source_reason = str(marker_body.get("reason") or "unknown")
    detail = str(marker_body.get("detail") or f"cleared non-actionable reauth marker: {source_reason}")
    extra: dict[str, Any] = {
        "source": source,
        "source_reason": source_reason,
    }
    for key in ("ts", "profile", "layer", "authoritative", "refresh_class"):
        if key in marker_body:
            extra[f"source_{key}" if key == "ts" else key] = marker_body[key]
    write_refresh_diagnostic_marker(
        refresh_diagnostic_path_for_reauth_marker(marker_path),
        detail=detail,
        extra=extra,
    )


def read_needs_reauth_marker(marker_path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_refresh_diagnostic_marker(marker_path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def clear_needs_reauth_marker(marker_path: Path) -> bool:
    """Remove a marker if present. Returns True when the marker is now gone
    (removed, or already absent), False when removal FAILED (root-owned marker,
    read-only FS, …).

    Callers that loop until markers are cleared MUST check this: treating a
    failed unlink as "cleared" gives no forward progress and spins the loop
    forever at 100% CPU (HIGH-2, audit 2026-07-03)."""
    try:
        marker_path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        logger.warning("[credential] failed to remove reauth marker %s", marker_path)
        return False


def iter_uat_locations(shared_home: Path) -> Iterator[UatLocation]:
    """Yield every on-disk UAT JSON we care about — legacy + per-profile.

    Excludes ``feishu_uat.fixtures.bak/`` so unit tests can leave fixtures in
    place without tripping the audit.
    """
    legacy_dir = shared_home / "feishu_uat"
    if legacy_dir.is_dir():
        for entry in sorted(legacy_dir.iterdir()):
            if entry.suffix != ".json":
                continue
            if is_fixture_path(entry):
                continue
            yield UatLocation(path=entry, profile_name="", open_id=entry.stem, legacy=True)

    profiles_dir = shared_home / "profiles"
    if profiles_dir.is_dir():
        for profile_dir in sorted(profiles_dir.iterdir()):
            if not profile_dir.is_dir():
                continue
            if is_fixture_path(profile_dir):
                continue
            uat_dir = profile_dir / "feishu_uat"
            if not uat_dir.is_dir():
                continue
            for entry in sorted(uat_dir.iterdir()):
                if entry.suffix != ".json":
                    continue
                if is_fixture_path(entry):
                    continue
                yield UatLocation(
                    path=entry,
                    profile_name=profile_dir.name,
                    open_id=entry.stem,
                    legacy=False,
                )


def classify_uat_payload(payload: dict[str, Any]) -> Optional[str]:
    """Return a `.needs_reauth` reason if the payload is unusable; else None.

    Order matters — most actionable reason first for the passive task gate
    (scope_stripped_by_feishu requires owner/admin action).
    """
    if not payload_has_offline_access(payload):
        return REASON_SCOPE_STRIPPED_BY_FEISHU
    if not payload_has_refresh_token(payload):
        return REASON_EMPTY_REFRESH_TOKEN
    if payload_refresh_expired(payload):
        return REASON_REFRESH_TOKEN_EXPIRED
    if payload_access_expired(payload) and not payload_has_refresh_token(payload):
        return REASON_ACCESS_TOKEN_EXPIRED
    return None


def payload_is_currently_usable(payload: dict[str, Any]) -> bool:
    """True when local UAT material is structurally valid and refreshable."""
    if classify_uat_payload(payload) is not None:
        return False
    return True


def current_valid_uat_exists(shared_home: Path, open_id: str) -> bool:
    """Return True if any current on-disk UAT for ``open_id`` is usable now."""
    for loc in iter_uat_locations(shared_home):
        if loc.open_id != open_id:
            continue
        try:
            payload = json.loads(loc.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload_is_currently_usable(payload):
            return True
    return False


def clear_reauth_markers_if_uat_recovered(
    shared_home: Path,
    open_id: str,
    marker_path: Path,
) -> bool:
    """Clear reauth markers when a newer valid UAT proves recovery."""
    try:
        marker_mtime = marker_path.stat().st_mtime
    except OSError:
        return False

    recovered_mtime: float | None = None
    for loc in iter_uat_locations(shared_home):
        if loc.open_id != open_id:
            continue
        try:
            uat_mtime = loc.path.stat().st_mtime
            if uat_mtime <= marker_mtime:
                continue
            payload = json.loads(loc.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload_is_currently_usable(payload):
            recovered_mtime = max(recovered_mtime or 0.0, uat_mtime)

    if recovered_mtime is None:
        return False

    for stale_marker in _iter_reauth_markers_for_open_id(shared_home, open_id):
        try:
            if stale_marker.stat().st_mtime <= recovered_mtime:
                clear_needs_reauth_marker(stale_marker)
        except OSError:
            continue
    return True


def marker_requires_reauth(marker_body: dict[str, Any]) -> bool:
    """Whether a marker is authoritative enough to defer a real task as reauth.

    ``refresh_rejected`` is only user-actionable when the refresh layer has
    parsed a Feishu invalid/revoked refresh-token response. Legacy catch-all
    markers and local infra failures must not become user-facing reauth prompts.
    """
    reason = str(marker_body.get("reason") or "")
    if reason != REASON_REFRESH_REJECTED:
        return True
    return bool(marker_body.get("authoritative")) and str(marker_body.get("refresh_class") or "") == "invalid"


def find_marker_for_open_id(shared_home: Path, open_id: str) -> Optional[Path]:
    """Return the most-recent marker file for ``open_id`` (legacy or per-profile)."""
    candidates: list[Path] = []
    legacy_marker = shared_home / "feishu_uat" / f"{open_id}.needs_reauth"
    if legacy_marker.is_file():
        candidates.append(legacy_marker)
    profiles_dir = shared_home / "profiles"
    if profiles_dir.is_dir():
        for profile_dir in profiles_dir.iterdir():
            if not profile_dir.is_dir() or is_fixture_path(profile_dir):
                continue
            marker = profile_dir / "feishu_uat" / f"{open_id}.needs_reauth"
            if marker.is_file():
                candidates.append(marker)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _iter_reauth_markers_for_open_id(shared_home: Path, open_id: str) -> list[Path]:
    markers = [shared_home / "feishu_uat" / f"{open_id}.needs_reauth"]
    profiles_dir = shared_home / "profiles"
    try:
        profile_dirs = list(profiles_dir.iterdir())
    except OSError:
        profile_dirs = []
    for profile_dir in profile_dirs:
        if profile_dir.is_dir():
            markers.append(profile_dir / "feishu_uat" / f"{open_id}.needs_reauth")
    return markers
