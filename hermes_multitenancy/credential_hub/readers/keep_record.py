"""keep-record credential reader."""
from __future__ import annotations
from hermes_multitenancy import credential_hub as _hub  # route patchable helpers via package namespace

import hashlib
import json
from pathlib import Path
from typing import Optional

from .._io import _normalize_epoch_ms, _parse_env_file, _read_small_text, _safe_account
from ..model import (
    KEEP_RECORD,
    S_AUTHENTICATED,
    S_MISSING,
    S_NEEDS_AUTH,
    S_UNKNOWN,
    CredentialRow,
    _TITLES,
)


def _keep_record_verified(home_dir: Path, token: str) -> tuple[bool, Optional[str]]:
    marker = _hub._read_small_text(Path(home_dir) / ".keepai" / "webui-auth-verified.json")
    if not marker:
        return False, None
    try:
        parsed = json.loads(marker)
    except (json.JSONDecodeError, TypeError):
        return False, None
    expected = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return parsed.get("token_sha256") == expected, _hub._safe_account(parsed.get("account_hint"))


def keep_record_status(*, home_dir: Path, installed: bool = True) -> CredentialRow:
    """keep-record — reads ``<home>/.keepai/.env`` + webui verification marker."""
    row = CredentialRow(
        id=KEEP_RECORD, title=_TITLES[KEEP_RECORD], provider="keep",
        installed=installed, status=S_MISSING,
        action={"kind": "skill_flow", "label": "扫码认证", "command": "/keep-record auth"},
    )
    if not installed:
        row.status = S_MISSING
        row.detail = "Keep-record skill 未在该 profile 安装。"
        return row

    env = _hub._parse_env_file(Path(home_dir) / ".keepai" / ".env")
    token = env.get("keep_auth_token") or ""
    if not token:
        row.status = S_NEEDS_AUTH
        row.detail = "Keep-record 需要扫码授权。"
        return row

    row.action["label"] = "重新扫码"
    row.account_hint = env.get("keep_username") or None
    row.expires_at = _hub._normalize_epoch_ms(env.get("keep_auth_token_expired"))

    # Status vocab matches the WebUI exactly: the webui-auth-verified marker is
    # the source of truth (the WebUI ignores token expiry for keep-record). The
    # expires_at field above is additive info for the Feishu card only.
    verified, marker_account = _hub._keep_record_verified(home_dir, token)
    if marker_account and not row.account_hint:
        row.account_hint = marker_account
    if verified:
        row.status = S_AUTHENTICATED
        row.detail = "Keep-record 已通过 WebUI 扫码验证。"
    else:
        row.status = S_UNKNOWN
        row.detail = "Keep-record 本地凭证存在，但未经 WebUI 验证，建议重新扫码确认。"
    return row
