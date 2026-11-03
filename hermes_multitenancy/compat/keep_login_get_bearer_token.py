#!/usr/bin/env python3
"""Hermes compatibility wrapper for legacy keep-login-skill.

Legacy Keep skills call a desktop-oriented get_bearer_token.py from one of the
usual agent home directories. In Hermes multitenancy, the credential source of
truth is profile-scoped kep-auth. This wrapper preserves the legacy CLI shape
while keeping auth profile/env scoped.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_=-]{4,}){1,2}$")


def _detect_env_from_url(url: str) -> str:
    text = (url or "").strip().lower()
    if "proxy.cms.pre.example.com" in text or "auth.pre.example.com" in text:
        return "pre"
    return "online"


def _kep_auth_candidates() -> list[str]:
    candidates = []
    explicit = os.environ.get("HERMES_KEP_AUTH_BIN", "").strip()
    if explicit:
        candidates.append(explicit)
    candidates.extend(
        [
            "kep-auth",
            str(Path(os.environ.get("HERMES_SHARED_HOME", "")) / "bin" / "kep-auth")
            if os.environ.get("HERMES_SHARED_HOME")
            else "",
            str(Path.home() / ".hermes" / "bin" / "kep-auth"),
            str(Path.home() / ".local" / "bin" / "kep-auth"),
            "/home/hermes/.hermes/bin/kep-auth",
            "/home/hermes/.local/bin/kep-auth",
        ]
    )
    return [candidate for candidate in candidates if candidate]


def _find_kep_auth() -> Optional[str]:
    for candidate in _kep_auth_candidates():
        if os.path.sep not in candidate:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
            continue
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def _profile() -> str:
    return (os.environ.get("KEP_PROFILE") or os.environ.get("HERMES_PROFILE") or "").strip()


def _run_kep_auth(kep_auth: str, *, env_name: str, command: str) -> subprocess.CompletedProcess[str]:
    cmd = [kep_auth]
    profile = _profile()
    if profile:
        cmd.extend(["--profile", profile])
    cmd.extend(["--env", env_name, command])
    child_env = os.environ.copy()
    child_env["KEP_NO_AUTO_LOGIN"] = "1"
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        env=child_env,
    )


def _extract_jwt(output: str) -> Optional[str]:
    for line in (output or "").splitlines():
        token = line.strip()
        if JWT_RE.match(token):
            return token
    return None


def _jwt_exp_ms(token: str) -> Optional[int]:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    if exp is None:
        return None
    try:
        value = float(exp)
    except (TypeError, ValueError):
        return None
    if value < 10_000_000_000:
        value *= 1000
    return int(value)


def _status_is_valid(output: str, returncode: int) -> bool:
    text = (output or "").lower()
    if "not logged in" in text or "unauthorized" in text or "401" in text:
        return False
    return returncode == 0 and (
        re.search(r"state:\s*valid", text) is not None
        or re.search(r"\blogged\s*in\b", text) is not None
    )


def _auth_error(env_name: str) -> int:
    profile = _profile() or "<profile>"
    print(
        f"当前 Hermes profile 的 kep-cli {env_name} 未授权或 token 无效；"
        f"请通过 Hermes 连接器认证 kep-cli {env_name}，或运行: "
        f"kep-auth --profile {profile} --env {env_name} login",
        file=sys.stderr,
    )
    return 77


def _missing_profile_error() -> int:
    print(
        "当前 Hermes runtime 缺少 KEP_PROFILE/HERMES_PROFILE，拒绝无 profile 调用 kep-auth；"
        "请在 Hermes profile sandbox 中运行，或显式传 --profile <profile>。",
        file=sys.stderr,
    )
    return 77


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes keep-login-skill compatibility wrapper")
    parser.add_argument("--env", choices=["online", "pre"], default="online")
    parser.add_argument("--url")
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--profile")
    args = parser.parse_args(argv)

    if args.profile and not os.environ.get("KEP_PROFILE"):
        os.environ["KEP_PROFILE"] = args.profile
    env_name = _detect_env_from_url(args.url) if args.url else args.env

    if args.clear:
        print(
            f"Hermes runtime 不在 legacy keep-login-skill 中清除凭证；"
            f"请通过 Hermes 连接器重新认证 kep-cli {env_name}，或运行 kep-auth --env {env_name} logout。",
            file=sys.stderr,
        )
        return 2

    if not _profile():
        return _missing_profile_error()

    kep_auth = _find_kep_auth()
    if not kep_auth:
        print("未找到 kep-auth；请先安装 Hermes kep-cli 连接器运行时。", file=sys.stderr)
        return 127

    # kep-auth status is currently human-readable; require both success exit and
    # an explicit valid marker so missing credentials cannot look authenticated.
    status = _run_kep_auth(kep_auth, env_name=env_name, command="status")
    if not _status_is_valid(status.stdout, status.returncode):
        return _auth_error(env_name)

    token_proc = _run_kep_auth(kep_auth, env_name=env_name, command="token")
    if token_proc.returncode != 0:
        return _auth_error(env_name)
    token = _extract_jwt(token_proc.stdout)
    if not token:
        return _auth_error(env_name)
    exp_ms = _jwt_exp_ms(token)
    if exp_ms is None or exp_ms <= int(time.time() * 1000):
        return _auth_error(env_name)

    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
