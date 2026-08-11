"""feishu-project credential reader (via the meegle CLI)."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional

from hermes_multitenancy import credential_hub as _hub

from ...credential_renewal_common import build_status_subprocess_env
from .._io import _safe_account
from ..model import (
    FEISHU_PROJECT,
    S_AUTHENTICATED,
    S_ERROR,
    S_MISSING,
    S_NEEDS_AUTH,
    CredentialRow,
    _TITLES,
)

_MEEGLE_DEFAULT_HOST = "project.feishu.cn"


def _meegle_allow_npx_status() -> bool:
    """Mirror shouldAllowNpxForMeegleStatus — gate npx execution for status reads."""
    override = os.environ.get("HERMES_MEEGLE_STATUS_ALLOW_NPX", "").strip().lower()
    if override:
        return override not in ("0", "false", "no", "off")
    return True  # WebUI default (NODE_ENV !== 'test')


def _meegle_search_path(*extra: str) -> str:
    """PATH for meegle/npx/node lookup, mirroring the WebUI ``meeglePathDirs``.

    The broker may run under a narrow launchd/systemd PATH that lacks node/npx; the
    TS reader this registry replaces augments lookup with common node install dirs +
    ``HERMES_MEEGLE_EXTRA_PATHS``. We mirror that so flipping the WebUI to the broker
    does not regress feishu-project to a false "未安装" on a deploy where the TS
    reader worked. ``extra`` lets callers prepend the resolved command's own dir so a
    found ``npx`` can locate the sibling ``node`` (handles non-standard installs like
    a version-manager bin that is itself on PATH but whose node isn't globally linked).
    """
    extra_env = os.environ.get("HERMES_MEEGLE_EXTRA_PATHS", "")
    candidates = [
        *os.environ.get("PATH", "").split(os.pathsep),
        *(p for p in extra_env.split(os.pathsep) if p),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        *extra,
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for d in candidates:
        if d and d not in seen:
            seen.add(d)
            ordered.append(d)
    return os.pathsep.join(ordered)


def _which_meegle(name: str) -> Optional[str]:
    """``shutil.which`` against the augmented meegle search path (not the bare PATH)."""
    return shutil.which(name, path=_hub._meegle_search_path())


def _meegle_invocation(*, allow_npx: bool = True) -> Optional[list[str]]:
    explicit = os.environ.get("HERMES_MEEGLE_BIN", "").strip()
    if explicit:
        return [explicit]
    direct = _hub._which_meegle("meegle")
    if direct:
        return [direct]
    if not allow_npx:
        return None
    npx = _hub._which_meegle("npx")
    if npx:
        return [npx, "-y", "@lark-project/meegle"]
    return None


def _meegle_profile(profile_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(profile_name or "default")).strip("_") or "default"
    return f"hermes_{safe}"


def feishu_project_status(
    *, profile_dir: Path, profile_name: str, required_by: Optional[list[str]] = None
) -> CredentialRow:
    """feishu-project via meegle CLI `auth status --format json` (guarded)."""
    row = CredentialRow(
        id=FEISHU_PROJECT, title=_TITLES[FEISHU_PROJECT], provider="feishu-project",
        installed=False, status=S_MISSING, required_by=required_by or [],
        action={"kind": "oauth_url", "label": "授权"},
    )
    inv = _hub._meegle_invocation(allow_npx=_hub._meegle_allow_npx_status())
    if inv is None:
        # npx-only but npx execution is disabled for status (or no CLI at all).
        # Mirror the WebUI: available iff npx is present, without running it.
        if _hub._which_meegle("npx"):
            row.installed = True
            row.status = S_NEEDS_AUTH
            row.detail = "飞书项目需要授权后才能查询和更新工作项。"
        else:
            row.detail = "飞书项目 CLI 未安装，无法授权或调用飞书项目能力。"
        return row

    host = os.environ.get("HERMES_MEEGLE_HOST", _MEEGLE_DEFAULT_HOST).strip() or _MEEGLE_DEFAULT_HOST
    # Child PATH includes the resolved command's own dir so a found npx can locate
    # the sibling node binary (parity with the TS meegleEnv child PATH).
    env = _hub.build_status_subprocess_env({
        "HOME": str(Path(profile_dir) / "home"),
        "MEEGLE_HOST": host,
        "PATH": _hub._meegle_search_path(os.path.dirname(inv[0])),
    })
    cmd = [*inv, "--profile", _hub._meegle_profile(profile_name), "auth", "status", "--format", "json"]
    proc = _hub._run(cmd, cwd=profile_dir, env=env)
    try:
        parsed = json.loads((getattr(proc, "stdout", "") or "{}"))
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    if proc is None or proc.returncode not in (0, None):
        # Binary present but status errored. Classify instead of collapsing EVERY
        # non-zero to needs_auth: a transient npx/node/network error must NOT tell
        # the user to re-authorize. Terminal (token expired / 401) → needs_auth;
        # transient → error (retry). exit_code is passed but not trusted; the
        # decision rides on stderr markers.
        from ...connector_failure_classifier import classify_connector_failure, is_needs_reauth

        rc = None if proc is None else proc.returncode
        stderr_tail = (getattr(proc, "stderr", "") or "")[-500:] if proc is not None else ""
        verdict = classify_connector_failure("feishu-project", exit_code=rc, stderr=stderr_tail)
        row.installed = True
        if parsed.get("reason") == "no local token" or is_needs_reauth(verdict):
            row.status = S_NEEDS_AUTH
            row.detail = "飞书项目需要授权后才能查询和更新工作项。"
        else:
            row.status = S_ERROR
            row.detail = "飞书项目状态暂时不可用（临时错误，可稍后重试）。"
        return row
    row.installed = True
    if parsed.get("authenticated") is True:
        row.status = S_AUTHENTICATED
        row.account_hint = _hub._safe_account(parsed.get("account") or (parsed.get("user") or {}).get("name"))
        row.detail = "飞书项目 CLI 已在当前 profile 完成授权。"
        row.action["label"] = "重新授权"
    else:
        row.status = S_NEEDS_AUTH
        row.detail = "飞书项目需要授权后才能查询和更新工作项。"
    return row
