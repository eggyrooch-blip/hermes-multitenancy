"""Auth-START flows for the ``/auth`` credential hub (keep-record QR, kep-cli web).

Read-only status lives in ``credential_hub.py``. This module STARTS the
interactive auth flows the Feishu card offers and polls them to completion,
running the per-profile CLIs inside the profile's HOME sandbox (so tokens land
in the right profile). It mirrors the WebUI's start/complete logic so the two
surfaces behave identically.

- keep-record: ``get_qrcode`` → QR image (uploaded to Feishu as an image_key for
  the card) → background ``login-wait`` → ``persist_auth`` + verification marker.
- kep-cli: ``kep-auth login`` → capture the OAuth verification URL for a card
  button → poll ``kep-auth status`` until logged in.

Subprocess execution is guarded; missing binaries/SDKs raise typed errors the
router turns into a graceful pending-note instead of a dead button.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_KEEP_TIMEOUT = 30
_URL_RE = re.compile(r"https?://[^\s'\"]+")


class HubAuthError(Exception):
    def __init__(self, message: str, *, status: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


# --- keep-record (QR) -------------------------------------------------------


def _profile_home(profile_dir: Path) -> Path:
    return Path(profile_dir) / "home"


def keep_record_skill_dir(profile_dir: Path) -> Path:
    return Path(profile_dir) / "skills" / "Keep" / "keep-record"


def _keep_node_path(profile_dir: Path, skill_dir: Path) -> str:
    """Mirror the WebUI keepRecordNodeModulePaths resolution (skill + shared)."""
    shared_home = Path(profile_dir).parent.parent  # <shared>/profiles/<p> → <shared>
    candidates = [
        skill_dir / "node_modules",
        shared_home / "skills" / "Keep" / "keep-record" / "node_modules",
    ]
    paths = [str(p) for p in candidates if p.exists()]
    if os.environ.get("NODE_PATH"):
        paths.append(os.environ["NODE_PATH"])
    return os.pathsep.join(paths)


def _run_keep_node(profile_dir: Path, args: list[str], *, timeout: int = _KEEP_TIMEOUT) -> dict[str, Any]:
    skill = keep_record_skill_dir(profile_dir)
    env = {**os.environ, "HOME": str(_profile_home(profile_dir))}
    node_path = _keep_node_path(profile_dir, skill)
    if node_path:
        env["NODE_PATH"] = node_path
    try:
        proc = subprocess.run(
            ["node", *args], cwd=str(skill), env=env,
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise HubAuthError("node runtime not found", status=502) from exc
    except subprocess.TimeoutExpired as exc:
        raise HubAuthError("keep-record script timed out", status=504) from exc
    out = (proc.stdout or "").strip()
    if "@keepclaw/skill-sdk" in (proc.stderr or "") and "Cannot find module" in (proc.stderr or ""):
        raise HubAuthError("keep-record dependencies not installed for this profile", status=424)
    if not out:
        raise HubAuthError(f"keep-record returned no output (rc={proc.returncode})", status=502)
    try:
        env_obj = json.loads(out)
    except json.JSONDecodeError as exc:
        raise HubAuthError("keep-record returned invalid JSON", status=502) from exc
    if env_obj.get("ok") is False:
        msg = ((env_obj.get("error") or {}).get("message")) or "keep-record call failed"
        raise HubAuthError(str(msg), status=502)
    return env_obj


def start_keep_record_qr(profile_dir: Path) -> dict[str, Any]:
    """Run get_qrcode under the profile → {qrcode_id, qrcode_url, redirect_url}."""
    skill = keep_record_skill_dir(profile_dir)
    script = skill / "scripts" / "mcp-call.js"
    if not script.is_file():
        raise HubAuthError("keep-record skill is not installed for this profile", status=404)
    env_obj = _run_keep_node(profile_dir, [str(script), "get_qrcode", json.dumps({"authType": "openclaw"})])
    data = env_obj.get("data") or {}
    qid = str(data.get("qrcodeId") or data.get("qrcode_id") or "").strip()
    qurl = str(data.get("qrcodeUrl") or data.get("qrcode_url") or "").strip()
    if not qid or not qurl:
        raise HubAuthError("keep-record did not return a QR code", status=502)
    return {"qrcode_id": qid, "qrcode_url": qurl, "redirect_url": str(data.get("redirectUrl") or "").strip() or None}


def poll_keep_record_once(profile_dir: Path, qrcode_id: str, *, timeout_ms: int = 15000) -> dict[str, Any]:
    """One login-wait cycle. On authorized → persist + write verification marker."""
    skill = keep_record_skill_dir(profile_dir)
    wait = skill / "scripts" / "login-wait.js"
    persist = skill / "scripts" / "persist_auth.js"
    if not wait.is_file() or not persist.is_file():
        raise HubAuthError("keep-record auth scripts are not installed", status=404)
    env_obj = _run_keep_node(profile_dir, [str(wait), qrcode_id, f"--timeout={timeout_ms}"],
                             timeout=int(timeout_ms / 1000) + 6)
    data = env_obj.get("data") or {}
    if data.get("status") == "authorized" and data.get("token"):
        token = str(data["token"])
        username = (data.get("user") or {}).get("username") or data.get("username")
        args = [str(persist), f"--token={token}"]
        if username:
            args.append(f"--username={username}")
        _run_keep_node(profile_dir, args)
        _write_keep_verification(profile_dir, token, username)
        return {"status": "authorized", "username": username}
    return {"status": str(data.get("status") or "pending")}


def _write_keep_verification(profile_dir: Path, token: str, account: Optional[str]) -> None:
    """Write the webui-auth-verified.json marker (parity with the WebUI)."""
    marker = _profile_home(profile_dir) / ".keepai" / "webui-auth-verified.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "account_hint": account or None,
    }, ensure_ascii=False), encoding="utf-8")


# --- Feishu image upload (for the QR in the card) ---------------------------


def upload_feishu_image(shared_home: Path, image_bytes: bytes) -> str:
    """Upload image bytes to Feishu im/v1/images (image_type=message) → image_key."""
    from . import feishu_uat_auth as fa

    client_id, client_secret = fa._feishu_app_credentials(shared_home)
    token = fa._mint_tenant_access_token(client_id, client_secret)
    boundary = "----hermesCredHubQR"
    parts: list[bytes] = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"image_type\"\r\n\r\nmessage\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"qr.png\"\r\n"
        f"Content-Type: image/png\r\n\r\n".encode()
    )
    parts.append(image_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        f"{fa.FEISHU_OPEN_BASE_URL}/open-apis/im/v1/images",
        data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        out = json.loads(resp.read())
    if out.get("code") != 0:
        raise HubAuthError(f"Feishu image upload failed: {out.get('msg')}", status=502)
    image_key = str((out.get("data") or {}).get("image_key") or "").strip()
    if not image_key:
        raise HubAuthError("Feishu image upload returned no image_key", status=502)
    return image_key


def fetch_qr_image_key(shared_home: Path, qrcode_url: str) -> str:
    """Download the keep QR image then upload to Feishu → image_key for the card."""
    try:
        with urllib.request.urlopen(qrcode_url, timeout=15) as r:
            image_bytes = r.read()
    except Exception as exc:
        raise HubAuthError(f"could not fetch QR image: {exc}", status=502) from exc
    return upload_feishu_image(shared_home, image_bytes)


# --- kep-cli (web) ----------------------------------------------------------


def kep_auth_bin(shared_home: Path) -> str:
    explicit = os.environ.get("HERMES_KEP_AUTH_BIN", "").strip()
    if explicit:
        return explicit
    return str(Path(shared_home) / "bin" / "kep-auth")


def _kep_env(profile_dir: Path, profile_name: str) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(_profile_home(profile_dir)),
        "HERMES_HOME": str(profile_dir),
        "KEP_PROFILE": str(profile_name),
    }


def start_kep_cli_login(profile_dir: Path, profile_name: str, shared_home: Path) -> dict[str, Any]:
    """Spawn ``kep-auth login`` and capture the OAuth verification URL from output.

    The login process is left running (it waits for the OAuth callback); the
    caller stores the Popen handle so it survives until the user authorizes.
    Returns {verification_uri, _proc}.
    """
    bin_path = kep_auth_bin(shared_home)
    if not Path(bin_path).exists():
        raise HubAuthError("kep-auth binary is not installed", status=404)
    try:
        proc = subprocess.Popen(
            [bin_path, "--profile", profile_name, "--env", "online", "login"],
            cwd=str(profile_dir), env=_kep_env(profile_dir, profile_name),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
    except FileNotFoundError as exc:
        raise HubAuthError("kep-auth binary not found", status=404) from exc

    # Read output lines until a URL appears (bounded).
    url = ""
    assert proc.stdout is not None
    import time as _t
    deadline = None  # Date.now banned in workflow ctx; here we're in plugin runtime so time is fine
    start = _t.time()
    while _t.time() - start < 12:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        m = _URL_RE.search(line)
        if m:
            url = m.group(0)
            break
    if not url:
        try:
            proc.kill()
        except Exception:
            pass
        raise HubAuthError("kep-auth login did not return an authorization URL", status=502)
    return {"verification_uri": url, "_proc": proc}


def kep_cli_logged_in(profile_dir: Path, profile_name: str, shared_home: Path) -> bool:
    """Poll: run ``kep-auth status`` → True iff logged in."""
    bin_path = kep_auth_bin(shared_home)
    if not Path(bin_path).exists():
        return False
    env = {**_kep_env(profile_dir, profile_name), "KEP_NO_AUTO_LOGIN": "1"}
    try:
        proc = subprocess.run(
            [bin_path, "--profile", profile_name, "--env", "online", "status"],
            cwd=str(profile_dir), env=env, capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    out = f"{proc.stdout}\n{proc.stderr}".lower()
    if re.search(r"not\s*logged\s*in", out):
        return False
    return bool(re.search(r"logged\s*in|state:\s*(valid|logged\s*in)", out))
