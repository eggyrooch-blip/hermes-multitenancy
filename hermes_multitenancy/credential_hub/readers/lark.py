"""lark-cli / feishu UAT credential reader."""
from __future__ import annotations
from hermes_multitenancy import credential_hub as _hub  # route patchable helpers via package namespace

import json
import logging
from pathlib import Path
from typing import Optional

from .._io import _now_ms, _read_small_text
from ..model import (
    LARK_CLI,
    S_AUTHENTICATED,
    S_NEEDS_AUTH,
    S_UNKNOWN,
    CredentialRow,
    _TITLES,
)

logger = logging.getLogger("hermes_multitenancy.credential_hub")


def _local_feishu_uat(profile_dir: Path) -> tuple[bool, Optional[int]]:
    """Mirror the WebUI localFeishuUatStatus: read ``<profile_dir>/feishu_uat/*.json``.

    Returns (connected, latest_expires_at_ms). Connected iff some cache file has a
    non-empty access_token AND (no expiry OR expiry > now + 60s).
    """
    d = Path(profile_dir) / "feishu_uat"
    connected = False
    latest_exp: Optional[int] = None
    try:
        names = [p for p in d.iterdir() if p.name.endswith(".json")]
    except OSError:
        return False, None
    for path in names:
        raw = _hub._read_small_text(path)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        token = parsed.get("access_token")
        exp = parsed.get("expires_at")
        try:
            exp_i = int(exp) if exp else 0
        except (ValueError, TypeError):
            exp_i = 0
        if exp_i and (latest_exp is None or exp_i > latest_exp):
            latest_exp = exp_i
        if isinstance(token, str) and token and (not exp_i or exp_i > _hub._now_ms() + 60_000):
            connected = True
    return connected, latest_exp


def lark_cli_status(
    *,
    profile_name: str,
    open_id: str,
    shared_home: Path,
    profile_dir: Optional[Path] = None,
    required_by: Optional[list[str]] = None,
) -> CredentialRow:
    """lark-cli/feishu UAT — mirrors the WebUI larkCliStatus 3-way OR.

    authenticated iff DB status 'valid' OR local feishu_uat/*.json file-cache is
    connected OR lark_cli.default_identity == 'user' (parity with skill-credentials.ts).
    """
    from ... import feishu_uat_auth

    row = CredentialRow(
        id=LARK_CLI, title=_TITLES[LARK_CLI], provider="lark", installed=True,
        status=S_UNKNOWN, required_by=required_by or [],
        action={"kind": "feishu_device_flow", "label": "授权"},
    )
    try:
        raw = feishu_uat_auth.credential_status(
            profile_name=profile_name, open_id=open_id, shared_home=shared_home
        )
    except Exception as exc:
        logger.debug("credential_hub: lark-cli status read failed (%s)", exc)
        raw = {"status": "", "lark_cli": {}}
        status_read_failed = True
    else:
        status_read_failed = False

    raw_status = str(raw.get("status") or "")
    raw_runtime_available = raw.get("runtime_available")
    lark = raw.get("lark_cli") or {}
    default_identity = lark.get("default_identity")
    # Display the REFRESH window, not the ~1h access token: the access token is
    # auto-renewed via the refresh token (refresh_uat_if_needed), so the user
    # only needs to re-auth when the refresh token expires (~30 days). Showing
    # the 1h access expiry alarms users into thinking it's about to break.
    access_exp = raw.get("expires_at")
    refresh_exp = raw.get("refresh_expires_at")
    row.expires_at = int(refresh_exp) if refresh_exp else (int(access_exp) if access_exp else None)

    p_dir = Path(profile_dir) if profile_dir else (Path(shared_home) / "profiles" / profile_name)
    local_connected, local_exp = _hub._local_feishu_uat(p_dir)
    if local_exp and not row.expires_at:
        row.expires_at = local_exp

    # Status vocab matches the WebUI SkillCredentialState (no 'expired'):
    connected = (
        (raw_status == "valid" and raw_runtime_available is not False)
        or local_connected
        or default_identity == "user"
    )
    if connected:
        row.status = S_AUTHENTICATED
        row.default_identity = default_identity or ("user" if local_connected else None)
        row.detail = "Lark-cli 已完成用户授权（访问令牌自动续期，到期前无需重新授权）。"
        row.action["label"] = "重新授权"
    elif raw_status in ("missing", "scope_missing", "expired"):
        row.status, row.detail = S_NEEDS_AUTH, "Lark-cli 需要用户授权后才能访问私有飞书资源。"
    elif status_read_failed:
        row.status, row.detail = S_UNKNOWN, "Lark-cli 状态读取失败"
    else:
        row.status, row.detail = S_UNKNOWN, "Lark-cli 状态待验证。"
    return row
