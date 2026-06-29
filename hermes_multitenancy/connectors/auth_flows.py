"""Auth-flow wrappers — thin delegation to ``credential_hub_auth``.

Phase 1 does NOT reimplement any auth flow. keep-record QR, kep-cli OAuth and
the kep callback rewrite/complete all keep living in ``credential_hub_auth``;
these wrappers exist so the registry has a stable, flow-typed surface to call
and so a later phase can move the implementation here without touching callers.

Every function preserves the original ``HubAuthError`` (status code intact) so
the router / broker error handling is unchanged.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


# --- qr_poll (keep-record) --------------------------------------------------


def start_qr(profile_dir: Path) -> dict[str, Any]:
    from .. import credential_hub_auth as cha

    return cha.start_keep_record_qr(profile_dir)


def poll_qr(profile_dir: Path, qrcode_id: str, *, timeout_ms: int = 15000) -> dict[str, Any]:
    from .. import credential_hub_auth as cha

    return cha.poll_keep_record_once(profile_dir, qrcode_id, timeout_ms=timeout_ms)


def fetch_qr_image_key(shared_home: Path, qrcode_url: str) -> str:
    from .. import credential_hub_auth as cha

    return cha.fetch_qr_image_key(shared_home, qrcode_url)


# --- oauth_callback (kep-cli) ----------------------------------------------


def start_oauth(
    profile_dir: Path,
    profile_name: str,
    shared_home: Path,
    *,
    public_origin: Optional[str] = None,
    env_name: str = "online",
) -> dict[str, Any]:
    from .. import credential_hub_auth as cha

    return cha.start_kep_cli_login(
        profile_dir,
        profile_name,
        shared_home,
        public_origin=public_origin,
        env_name=env_name,
    )


def poll_oauth(profile_dir: Path, profile_name: str, shared_home: Path, *, env_name: str = "online") -> bool:
    from .. import credential_hub_auth as cha

    return cha.kep_cli_logged_in(profile_dir, profile_name, shared_home, env_name=env_name)


def complete_oauth_callback(session_id: str, query: str) -> str:
    """Forward an OAuth callback to the stored localhost kep-auth server.

    The session store lives in ``credential_hub_auth`` (Phase 1). The WebUI/
    broker public route is a thin forward to this; the registry never owns the
    callback HTML.
    """
    from .. import credential_hub_auth as cha

    return cha.complete_kep_callback(session_id, query)
