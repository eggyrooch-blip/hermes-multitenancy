from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

_IDENTITY_URLS = {
    "online": "https://auth.gotokeep.com/ldap/authjwt",
    "pre": "https://auth.pre.gotokeep.com/ldap/authjwt",
}
_TIMEOUT_SECONDS = 3
_MAX_BYTES = 64 * 1024
_AUTH_HTTP_CODES = {400, 401, 403}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _urlopen(request: urllib.request.Request, *, timeout: int):
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def _epoch_ms(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number * 1000 if number < 1_000_000_000_000 else number


def probe_kep_identity(
    token: str,
    *,
    profile_name: str,
    env_name: str,
    identity_urls: dict[str, str] | None = None,
) -> dict[str, Any]:
    urls = _IDENTITY_URLS if identity_urls is None else identity_urls
    request = urllib.request.Request(
        urls[env_name],
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with _urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            raw = response.read(_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return {"state": "needs_auth" if exc.code in _AUTH_HTTP_CODES else "unknown"}
    except (OSError, TimeoutError, urllib.error.URLError):
        return {"state": "unknown"}
    if len(raw) > _MAX_BYTES:
        return {"state": "unknown"}
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        return {"state": "unknown"}
    if not isinstance(body, dict):
        return {"state": "unknown"}
    if body.get("errorCode") != 0 or body.get("ok") is not True:
        return {"state": "needs_auth"}
    data = body.get("data")
    payload = data.get("payload") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return {"state": "unknown"}
    if str(payload.get("name") or "").strip() != profile_name:
        return {"state": "identity_mismatch"}
    expires_at = _epoch_ms(payload.get("exp"))
    if expires_at is None:
        return {"state": "unknown"}
    if expires_at <= int(time.time() * 1000):
        return {"state": "needs_auth", "expires_at": expires_at}
    return {
        "state": "authenticated",
        "account_hint": profile_name,
        "expires_at": expires_at,
    }
