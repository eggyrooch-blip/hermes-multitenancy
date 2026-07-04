"""Unified connector-failure classifier — the SINGLE terminal-vs-transient decision.

Before this module the "is this a re-auth situation or a transient blip?" decision
was scattered across ``classify_uat_payload``, ``classify_refresh_error`` and
``credential_renewal_worker._record_failure`` (each Feishu-specific), while the
credential-hub readers collapsed every non-zero outcome to a status enum and
threw away the raw downstream signal (http status / exit code / stderr). That made
non-Feishu connectors unclassifiable and let known traps slip through:

  * hades returns HTTP 401 but **exit code 0** — trusting exit_code says "ok".
  * kep-auth reports a locally-valid JWT while the backend 401s — trusting the
    local status says "ok".

The discipline here: **http_status and stderr/body OVERRIDE exit_code**, and each
connector's terminal business codes are explicit. Pure function, no IO, no state —
so the reactive re-auth card, the renewal worker, and any proactive detect tick can
all route the same decision through one place.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .credential_renewal_common import payload_access_expired, payload_refresh_expired

# HTTP statuses that authoritatively mean "the credential is rejected, re-auth".
TERMINAL_HTTP_STATUSES = frozenset({401, 403, 422})

# stderr/body substrings that terminally indicate an expired/rejected credential.
# Deliberately narrow: bare "401"/"403"/"forbidden" are NOT enough (a business
# error like "工作项 401 不存在" or "forbidden by policy" is not a credential
# failure). A numeric status only counts when it sits in an auth/http context
# (HTTP 401 / status: 403 / 401 unauthorized), and the word phrases are unambiguous.
_TERMINAL_STDERR_RE = re.compile(
    r"(?:\b(?:http|status|statuscode|code|错误码|状态码)\b\W{0,4}(?:401|403)\b)"
    r"|(?:\b(?:401|403)\b[ _-]?(?:unauthorized|forbidden))"
    r"|\bunauthorized\b"
    r"|token[ _-]?(?:expired|invalid|无效|过期)"
    r"|invalid[ _-]?(?:grant|token)"
    r"|credential[ _-]?expired"
    r"|needs?[ _-]?re-?auth"
    r"|please[ _-]?re-?login"
    r"|(?:登录|凭证|授权)(?:已)?(?:过期|失效)"
    r"|重新(?:登录|授权)",
    re.IGNORECASE,
)

# Per-connector terminal business codes (non-HTTP app-level codes that mean re-auth).
# Feishu invalid codes are the authoritative source in feishu_uat_auth; mirror the
# well-known ones so a connector surfacing a business code (not an HTTP status) is
# still classified terminal. Transient/retryable Feishu codes stay OUT of this set.
_TERMINAL_BUSINESS_CODES: dict[str, frozenset[int]] = {
    # Feishu (lark-cli): 99991668=token invalid, 20026/20037/20064/20073 invalid grant/token
    "lark-cli": frozenset({99991668, 20026, 20037, 20064, 20073}),
    # kep business: 10101 (parity with WorkBuddy TERMINAL_BUSINESS_CODES) if surfaced
    "kep-cli-online": frozenset({10101}),
    "kep-cli-pre": frozenset({10101}),
    "kep-cli": frozenset({10101}),
}

NEEDS_REAUTH = "needs_reauth"
TRANSIENT = "transient"


def _stderr_is_terminal(stderr: str) -> bool:
    return bool(stderr and _TERMINAL_STDERR_RE.search(stderr))


def _extract_body_code(stderr: str) -> Optional[int]:
    """Best-effort pull an app-level numeric ``code`` out of stderr/body text
    (e.g. ``{"code":99991668,...}`` or ``code: 10101``). Returns None if absent."""
    if not stderr:
        return None
    # Require the key to be exactly "code" (quoted or word-bounded) — NOT a suffix
    # of status_code/retcode/error_code, whose value would otherwise be grabbed.
    m = re.search(r'(?:"code"|\'code\'|(?<![a-zA-Z_])code)\s*[:=]\s*(\d{3,})', stderr)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def classify_connector_failure(
    connector_id: str,
    *,
    http_status: Optional[int] = None,
    exit_code: Optional[int] = None,  # noqa: ARG001 — accepted but DELIBERATELY not trusted
    stderr: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    refresh_class: Optional[str] = None,
) -> dict[str, str]:
    """Classify a connector failure as ``needs_reauth`` (terminal, user must re-auth
    or the token must be refreshed) or ``transient`` (network / server blip → retry).

    Signal precedence (strongest first) — note ``exit_code`` is accepted for call-site
    convenience but is NEVER trusted, because hades returns 401 with exit 0:
      1. payload direct: refresh_token expired → needs_reauth; only access expired
         (refresh still alive) → transient (it is proactively refreshable).
      2. http_status in the terminal set → needs_reauth.
      3. per-connector terminal business code (from stderr/body) → needs_reauth.
      4. stderr/body terminal markers (401/unauthorized/token expired/…) → needs_reauth.
      5. otherwise → transient.
    """
    # 0. Feishu authoritative refresh_class (from classify_refresh_error) — the most
    #    precise signal when the worker already got a curated OAuth verdict. 'invalid'
    #    means the refresh_token itself is rejected → terminal; 'retryable'/'ok' → not.
    if refresh_class is not None:
        if refresh_class == "invalid":
            return {"class": NEEDS_REAUTH, "reason": "refresh_class_invalid"}
        if refresh_class in ("retryable", "ok"):
            return {"class": TRANSIENT, "reason": f"refresh_class_{refresh_class}"}

    # 1. Payload-direct (Feishu-UAT-shaped tokens carry expiry).
    if payload is not None:
        if payload_refresh_expired(payload):
            return {"class": NEEDS_REAUTH, "reason": "refresh_token_expired"}
        if payload_access_expired(payload):
            # access expired but refresh alive → the renewal worker can refresh it.
            return {"class": TRANSIENT, "reason": "access_expired_refreshable"}

    # 2. Authoritative HTTP terminal — overrides exit_code (hades 401/exit0 trap).
    if http_status is not None and int(http_status) in TERMINAL_HTTP_STATUSES:
        return {"class": NEEDS_REAUTH, "reason": f"http_{int(http_status)}"}

    # 3. Per-connector terminal business code surfaced in the body.
    body_code = _extract_body_code(stderr or "")
    if body_code is not None and body_code in _TERMINAL_BUSINESS_CODES.get(connector_id, frozenset()):
        return {"class": NEEDS_REAUTH, "reason": f"business_{body_code}"}

    # 4. stderr/body terminal markers — again overrides a lying exit_code / local status.
    if _stderr_is_terminal(stderr or ""):
        return {"class": NEEDS_REAUTH, "reason": "stderr_terminal"}

    # 5. Everything else (5xx, timeouts, connection refused, unknown non-zero) → transient.
    return {"class": TRANSIENT, "reason": "transient_or_unknown"}


def is_needs_reauth(result: dict[str, str]) -> bool:
    return result.get("class") == NEEDS_REAUTH
