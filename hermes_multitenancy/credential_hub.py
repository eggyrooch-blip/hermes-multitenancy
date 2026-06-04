"""Credential hub status aggregation for the ``/auth`` command.

The ``/auth`` Feishu command renders one interactive card that mirrors the
WebUI credential collection (``CredentialsView.vue``) so that users who only
reach Hermes through Feishu can authenticate the per-profile CLI tools
themselves.  This module is the read-only status layer: given a profile +
open_id it reports, per credential, a normalized status and (when known) an
expiry so the card can show "已认证 · 30天后过期" style rows.

Status vocabulary (mirrors the WebUI ``SkillCredentialEntry['status']``):

    authenticated  — a valid credential is present
    needs_auth     — no credential yet, or scopes missing → user should auth
    expired        — credential present but past its expiry
    missing        — the tool/credential store is not installed for the profile
    unknown        — a credential file exists but validity cannot be read here

Only the read path lives here.  Starting an auth flow (device flow / QR /
OAuth) stays in the per-tool modules and is wired by the router.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Credential ids in display order. lark-cli is wired end-to-end (device flow);
# keep-record / kep-cli report status here and get their auth-start wired in a
# follow-up slice. Keep ids identical to the WebUI so the two surfaces agree.
LARK_CLI = "lark-cli"
KEEP_RECORD = "keep-record"
KEP_CLI = "kep-cli"

CREDENTIAL_ORDER = (LARK_CLI, KEEP_RECORD, KEP_CLI)

_TITLES = {
    LARK_CLI: "Lark-cli",
    KEEP_RECORD: "Keep-record",
    KEP_CLI: "Kep-cli",
}

_STATUS_AUTHENTICATED = "authenticated"
_STATUS_NEEDS_AUTH = "needs_auth"
_STATUS_EXPIRED = "expired"
_STATUS_MISSING = "missing"
_STATUS_UNKNOWN = "unknown"


@dataclass
class CredentialRow:
    """One credential's status for the hub card (no secrets)."""

    id: str
    title: str
    status: str
    expires_at: Optional[int] = None  # epoch milliseconds
    account_hint: Optional[str] = None
    detail: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def authenticated(self) -> bool:
        return self.status == _STATUS_AUTHENTICATED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "expires_at": self.expires_at,
            "account_hint": self.account_hint,
            "detail": self.detail,
            **({"extra": self.extra} if self.extra else {}),
        }


def _now_ms() -> int:
    return int(time.time() * 1000)


def profile_root(shared_home: Path, profile_name: str) -> Path:
    """``<shared_home>/profiles/<profile_name>`` — the profile root dir."""
    return Path(shared_home) / "profiles" / str(profile_name)


def profile_home_dir(shared_home: Path, profile_name: str) -> Path:
    """The HOME-redirect dir where per-profile tools drop their dotfiles."""
    return profile_root(shared_home, profile_name) / "home"


# --- per-tool readers -------------------------------------------------------


def lark_cli_status(*, profile_name: str, open_id: str, shared_home: Path) -> CredentialRow:
    """Status for lark-cli/feishu UAT — reuses the canonical feishu reader."""
    from . import feishu_uat_auth

    row = CredentialRow(id=LARK_CLI, title=_TITLES[LARK_CLI], status=_STATUS_UNKNOWN)
    try:
        raw = feishu_uat_auth.credential_status(
            profile_name=profile_name,
            open_id=open_id,
            shared_home=shared_home,
        )
    except Exception as exc:  # network/route/db errors must not break the card
        logger.debug("credential_hub: lark-cli status read failed (%s)", exc)
        row.status = _STATUS_UNKNOWN
        row.detail = "状态读取失败"
        return row

    raw_status = str(raw.get("status") or "")
    expires_at = raw.get("expires_at")
    row.expires_at = int(expires_at) if expires_at else None
    if raw_status == "valid":
        row.status = _STATUS_AUTHENTICATED
    elif raw_status == "expired":
        row.status = _STATUS_EXPIRED
    elif raw_status in ("missing", "scope_missing"):
        row.status = _STATUS_NEEDS_AUTH
    else:
        row.status = _STATUS_UNKNOWN
    return row


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def keep_record_status(*, home_dir: Path) -> CredentialRow:
    """Status for keep-record — reads ``<home>/.keepai/.env``.

    Fields written by ``persist_auth.js``: ``keep_auth_token``,
    ``keep_auth_token_expired`` (epoch ms), ``keep_username``.
    """
    row = CredentialRow(id=KEEP_RECORD, title=_TITLES[KEEP_RECORD], status=_STATUS_MISSING)
    env_path = Path(home_dir) / ".keepai" / ".env"
    if not env_path.is_file():
        row.status = _STATUS_NEEDS_AUTH if (Path(home_dir) / ".keepai").exists() else _STATUS_MISSING
        return row
    env = _parse_env_file(env_path)
    token = env.get("keep_auth_token") or ""
    if not token:
        row.status = _STATUS_NEEDS_AUTH
        return row
    row.account_hint = env.get("keep_username") or None
    expired_raw = env.get("keep_auth_token_expired") or ""
    try:
        expired_ms = int(float(expired_raw)) if expired_raw else 0
    except ValueError:
        expired_ms = 0
    if expired_ms:
        row.expires_at = expired_ms
        row.status = _STATUS_EXPIRED if expired_ms <= _now_ms() else _STATUS_AUTHENTICATED
    else:
        # token present but no expiry recorded — can't confirm validity here
        row.status = _STATUS_UNKNOWN
    return row


def kep_cli_status(*, home_dir: Path) -> CredentialRow:
    """Status for kep-cli — checks ``<home>/.kep-cli/tokens/*.enc`` presence.

    The token blob is encrypted; expiry is only readable via ``kep-auth``,
    which this read-only path does not invoke. Presence ⇒ unknown (待验证),
    absence ⇒ needs_auth.
    """
    row = CredentialRow(id=KEP_CLI, title=_TITLES[KEP_CLI], status=_STATUS_NEEDS_AUTH)
    tokens_dir = Path(home_dir) / ".kep-cli" / "tokens"
    try:
        has_token = tokens_dir.is_dir() and any(tokens_dir.glob("*.enc"))
    except OSError:
        has_token = False
    if has_token:
        row.status = _STATUS_UNKNOWN
        row.detail = "已有 token，有效期需在使用时校验"
    return row


def collect_credential_statuses(
    *,
    profile_name: str,
    open_id: str,
    shared_home: Optional[Path] = None,
    home_dir: Optional[Path] = None,
) -> list[CredentialRow]:
    """Aggregate per-credential status for one profile/open_id, in display order.

    ``home_dir`` is the profile's HOME-redirect dir (where per-profile tools
    drop dotfiles). Callers that already know it (the router has the
    authoritative ``profile_home``) should pass it; otherwise it is derived from
    ``shared_home`` — which honors ``HERMES_SHARED_HOME``/``HERMES_HOME``.
    """
    from . import feishu_uat_auth

    shared = Path(shared_home) if shared_home else feishu_uat_auth.resolve_shared_home()
    home = Path(home_dir) if home_dir else profile_home_dir(shared, profile_name)

    rows = {
        LARK_CLI: lark_cli_status(profile_name=profile_name, open_id=open_id, shared_home=shared),
        KEEP_RECORD: keep_record_status(home_dir=home),
        KEP_CLI: kep_cli_status(home_dir=home),
    }
    return [rows[cid] for cid in CREDENTIAL_ORDER]


# --- display helpers --------------------------------------------------------


def human_expiry(expires_at: Optional[int], *, now_ms: Optional[int] = None) -> str:
    """Render an expiry timestamp (ms) as a short zh phrase, or '' if unknown."""
    if not expires_at:
        return ""
    now = now_ms if now_ms is not None else _now_ms()
    delta_ms = int(expires_at) - now
    if delta_ms <= 0:
        return "已过期"
    days = delta_ms // (24 * 3600 * 1000)
    if days >= 1:
        return f"{days}天后过期"
    hours = delta_ms // (3600 * 1000)
    if hours >= 1:
        return f"{hours}小时后过期"
    minutes = max(1, delta_ms // (60 * 1000))
    return f"{minutes}分钟后过期"
