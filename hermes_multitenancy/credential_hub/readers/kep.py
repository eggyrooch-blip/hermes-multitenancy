"""kep-cli (online/pre) credential readers."""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from hermes_multitenancy import credential_hub as _hub

from ...credential_renewal_common import build_status_subprocess_env
from .._io import _normalize_epoch_ms, _now_ms, _safe_account
from ..model import (
    KEP_CLI_ENV_IDS,
    S_AUTHENTICATED,
    S_MISSING,
    S_NEEDS_AUTH,
    S_UNKNOWN,
    CredentialRow,
    _TITLES,
)

logger = logging.getLogger("hermes_multitenancy.credential_hub")

_KEP_IDENTITY_URLS = {
    "online": "https://auth.gotokeep.com/ldap/authjwt",
    "pre": "https://auth.pre.gotokeep.com/ldap/authjwt",
}
_KEP_IDENTITY_TIMEOUT_SECONDS = 3
_KEP_IDENTITY_MAX_BYTES = 64 * 1024


def _kep_auth_bin(shared_home: Path) -> str:
    explicit = os.environ.get("HERMES_KEP_AUTH_BIN", "").strip()
    if explicit:
        return explicit
    return str(Path(shared_home) / "bin" / "kep-auth")


def _decode_jwt_exp_ms(token: str) -> Optional[int]:
    """Decode a JWT's ``exp`` claim to epoch-ms, or None if undecodable.

    kep-cli tokens are HS256 JWTs carrying ``exp`` (48h TTL). ``kep-auth status``
    reports ``state: valid`` from a LOCAL check only — it does not know the real
    server-side expiry (it prints ``expires: 0``). So we decode the token's own
    ``exp`` here to tell a live token from a stale one. Signature is NOT verified
    (the backend owns trust); we only read the unauthenticated expiry hint.
    """
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)  # restore base64url padding
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    return _hub._normalize_epoch_ms(claims.get("exp"))


def _kep_token_exp_ms(
    bin_path: str, *, profile_name: str, env: dict[str, str], cwd: Path, env_name: str = "online"
) -> Optional[int]:
    """Run ``kep-auth token`` and return the token's ``exp`` in epoch-ms.

    Returns None when the token cannot be fetched or decoded — callers treat
    None as "expiry unknown" (conservative: do NOT claim authenticated).
    """
    proc = _hub._run([bin_path, "--profile", profile_name, "--env", env_name, "token"], cwd=cwd, env=env)
    if proc is None or proc.returncode != 0:
        return None
    # Scan for a JWT-shaped line rather than assuming line 0 is the token:
    # tolerates banners/warnings on stdout and whitespace-only output (no
    # IndexError). Returns the first decodable exp.
    for line in (proc.stdout or "").splitlines():
        exp = _hub._decode_jwt_exp_ms(line.strip())
        if exp is not None:
            return exp
    return None


def _kep_token_value(
    bin_path: str, *, profile_name: str, env: dict[str, str], cwd: Path, env_name: str
) -> Optional[str]:
    proc = _hub._run(
        [bin_path, "--profile", profile_name, "--env", env_name, "token"],
        cwd=cwd,
        env=env,
    )
    if proc is None or proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        token = line.strip()
        if token.count(".") == 2:
            return token
    return None


def _probe_kep_identity(token: str, *, profile_name: str, env_name: str) -> dict[str, Any]:
    request = urllib.request.Request(
        _KEP_IDENTITY_URLS[env_name],
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=_KEP_IDENTITY_TIMEOUT_SECONDS) as response:
            raw = response.read(_KEP_IDENTITY_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return {"state": "needs_auth" if 400 <= exc.code < 500 else "unknown"}
    except (OSError, TimeoutError, urllib.error.URLError):
        return {"state": "unknown"}
    if len(raw) > _KEP_IDENTITY_MAX_BYTES:
        return {"state": "unknown"}
    try:
        body = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"state": "unknown"}
    if not isinstance(body, dict):
        return {"state": "unknown"}
    if body.get("errorCode") != 0 or body.get("ok") is not True:
        return {"state": "needs_auth"}
    payload = body.get("data", {}).get("payload") if isinstance(body.get("data"), dict) else None
    if not isinstance(payload, dict):
        return {"state": "unknown"}
    if str(payload.get("name") or "").strip() != profile_name:
        return {"state": "identity_mismatch"}
    expires_at = _hub._normalize_epoch_ms(payload.get("exp"))
    if expires_at is None:
        return {"state": "unknown"}
    if expires_at <= _hub._now_ms():
        return {"state": "needs_auth", "expires_at": expires_at}
    return {
        "state": "authenticated",
        "account_hint": profile_name,
        "expires_at": expires_at,
    }


def _normalize_kep_env_name(value: str) -> str:
    env_name = str(value or "").strip().lower()
    return env_name if env_name in {"online", "pre"} else "online"


def _ordered_kep_envs(target_env: str, report_envs: Optional[tuple[str, ...] | list[str]]) -> list[str]:
    ordered: list[str] = []
    for env_name in (_hub._normalize_kep_env_name(target_env), *(report_envs or ())):
        normalized = _hub._normalize_kep_env_name(env_name)
        if normalized not in ordered:
            ordered.append(normalized)
    return ordered or ["online"]


def _kep_env_token_present(home_dir: Path, *, profile_name: str, env_name: str) -> bool:
    keyring = Path(home_dir) / ".kep-cli" / "keyring-fallback"
    try:
        return keyring.is_dir() and any(
            p.name == f"token-key:{env_name}:{profile_name}"
            for p in keyring.iterdir()
        )
    except OSError:
        return False


def _kep_env_status(
    *,
    bin_path: str,
    profile_dir: Path,
    home_dir: Path,
    profile_name: str,
    env_name: str,
) -> dict[str, Any]:
    has_token = _hub._kep_env_token_present(home_dir, profile_name=profile_name, env_name=env_name)
    live: Optional[str] = None
    account: Optional[str] = None
    expires_at: Optional[int] = None

    if Path(bin_path).exists():
        proc_env = _hub.build_status_subprocess_env({
            "HOME": str(home_dir),
            "HERMES_HOME": str(profile_dir),
            "KEP_PROFILE": str(profile_name),
            "KEP_NO_AUTO_LOGIN": "1",
        })
        proc = _hub._run([bin_path, "--profile", profile_name, "--env", env_name, "status"], cwd=profile_dir, env=proc_env)
        if proc is not None:
            out = f"{proc.stdout}\n{proc.stderr}".lower()
            if re.search(r"not\s*logged\s*in", out):
                live = "not_logged_in"
            elif re.search(r"logged\s*in|state:\s*(valid|logged\s*in)", out):
                live = "logged_in"
                account = _hub._parse_kep_account(f"{proc.stdout}\n{proc.stderr}")
            elif proc.returncode == 3 or re.search(r"unauthorized|401", out):
                live = "not_logged_in"

        if live == "logged_in":
            token = _kep_token_value(
                bin_path,
                profile_name=profile_name,
                env=proc_env,
                cwd=profile_dir,
                env_name=env_name,
            )
            if not token:
                live = "not_logged_in"
            else:
                probe = _probe_kep_identity(
                    token,
                    profile_name=profile_name,
                    env_name=env_name,
                )
                live = str(probe["state"])
                account = probe.get("account_hint")
                expires_at = probe.get("expires_at")

    if live == "authenticated":
        status = S_AUTHENTICATED
        detail = f"kep-auth 已实时验证该 profile 的 {env_name} 登录。"
    elif live == "not_logged_in" or live == "needs_auth":
        status = S_NEEDS_AUTH
        detail = f"kep-cli {env_name} 登录已失效，请重新认证。"
    elif live == "identity_mismatch":
        status = S_UNKNOWN
        detail = f"kep-cli {env_name} 身份校验不匹配，已停止使用该凭证。"
    elif live == "unknown":
        status = S_UNKNOWN
        detail = f"kep-cli {env_name} 暂时无法实时验证，已停止使用该凭证。"
    elif has_token:
        status = S_UNKNOWN
        detail = f"kep-cli {env_name} 凭证存在，但无法实时验证，已停止使用。"
    else:
        status = S_NEEDS_AUTH
        detail = f"kep-cli 需要登录 {env_name}。"

    out: dict[str, Any] = {
        "status": status,
        "detail": detail,
    }
    if account:
        out["account_hint"] = account
    if expires_at is not None:
        out["expires_at"] = expires_at
    return out


def kep_cli_status(
    *,
    profile_dir: Path,
    home_dir: Path,
    profile_name: str,
    shared_home: Path,
    installed: bool = False,
    required_by: Optional[list[str]] = None,
    target_env: str = "online",
    report_envs: Optional[tuple[str, ...] | list[str]] = None,
) -> CredentialRow:
    """kep-cli — keyring presence + live ``kep-auth status`` (guarded)."""
    target_env = _hub._normalize_kep_env_name(target_env)
    row_id = KEP_CLI_ENV_IDS[target_env]
    row = CredentialRow(
        id=row_id, title=_TITLES[row_id], provider="keep",
        installed=installed, status=S_MISSING, required_by=required_by or [],
        action={"kind": "oauth_url", "label": "认证", "env": target_env},
    )
    if not installed:
        row.detail = "该 profile 没有安装依赖 kep-cli 的 skill。"
        return row

    bin_path = _hub._kep_auth_bin(shared_home)
    envs = _hub._ordered_kep_envs(target_env, report_envs)
    row.environments = {
        env_name: _hub._kep_env_status(
            bin_path=bin_path,
            profile_dir=Path(profile_dir),
            home_dir=Path(home_dir),
            profile_name=profile_name,
            env_name=env_name,
        )
        for env_name in envs
    }

    target = row.environments[target_env]
    row.status = str(target["status"])
    row.detail = str(target["detail"])
    row.expires_at = target.get("expires_at")
    row.account_hint = target.get("account_hint")
    if not row.account_hint:
        for env_data in row.environments.values():
            if env_data.get("account_hint"):
                row.account_hint = str(env_data["account_hint"])
                break

    if target_env != "online" and "online" in row.environments:
        online = row.environments["online"]
        online_status = online.get("status")
        if row.status != S_AUTHENTICATED and online_status == S_AUTHENTICATED:
            row.detail = f"{row.detail} online 已登录；当前专家默认需要 {target_env}。"

    row.action["label"] = ("重新认证" if row.status == S_AUTHENTICATED else "认证")
    if target_env != "online":
        row.action["label"] = f"{row.action['label']} {target_env}"
    return row


def kep_auth_state_line(
    *,
    profile_dir: Path,
    home_dir: Path,
    profile_name: str,
    shared_home: Path,
) -> Optional[str]:
    try:
        bin_path = _hub._kep_auth_bin(shared_home)
        statuses = {
            env_name: _hub._kep_env_status(
                bin_path=bin_path,
                profile_dir=Path(profile_dir),
                home_dir=Path(home_dir),
                profile_name=profile_name,
                env_name=env_name,
            )
            for env_name in ("pre", "online")
        }

        def _segment(env_name: str) -> str:
            env_status = statuses[env_name]
            if env_status.get("status") == S_AUTHENTICATED:
                account_hint = str(env_status.get("account_hint") or "").strip()
                return f"已登录: {account_hint}" if account_hint else "已登录"
            return "未登录"

        return (
            f"【系统已核实(勿再自行探活)】kep-cli pre={_segment('pre')}；online={_segment('online')}。"
            "pre 已登录就直接用 ocean-cli --env pre 取数；pre 未登录就只引导用户在连接器认证 "
            "kep-cli pre，不要声称 online 也失败、不要去掉 --profile。"
            "如果 ocean-cli 返回 HTTP 403 或 接口禁止访问，这是已登录账号没有该接口/数据权限；"
            "如实告知无权限，不要要求用户重新登录。只有 HTTP 401/not logged in 才按认证失效处理。"
        )
    except Exception:
        return None


def kep_cli_statuses(
    *,
    profile_dir: Path,
    home_dir: Path,
    profile_name: str,
    shared_home: Path,
    installed: bool = False,
    required_by: Optional[list[str]] = None,
    target_env: str = "online",
) -> list[CredentialRow]:
    """Return separate kep-cli credential rows for online and pre."""
    primary = _hub._normalize_kep_env_name(target_env)
    rows: list[CredentialRow] = []
    for env_name in ("online", "pre"):
        rows.append(
            _hub.kep_cli_status(
                profile_dir=profile_dir,
                home_dir=home_dir,
                profile_name=profile_name,
                shared_home=shared_home,
                installed=installed,
                required_by=(required_by or []) if env_name == primary else [],
                target_env=env_name,
                report_envs=(env_name,),
            )
        )
    return rows


def _parse_kep_account(output: str) -> Optional[str]:
    for key in ("operator", "user"):
        m = re.search(rf"^{key}:\s*(.+)$", output, re.MULTILINE | re.IGNORECASE)
        if m:
            return _hub._safe_account(re.sub(r"\s*<[^>]+>\s*", "", m.group(1)).strip())
    return None
