"""Recognize Feishu OAPI permission error codes in FAILED lark_cli results and
append one generic, actionable hint line.

Two codes are split because their remediation differs completely:

* 99991672 — the APP is not granted the scope on the open platform. Only an
  admin can grant it; a user ``/auth`` does nothing (the death-loop ticket).
* 99991679 — the app has the scope but the USER has not authorized. The user
  just runs ``/auth``.

STRUCTURED FIELDS ONLY — no text scanning, by decision (sunke, 2026-07-10).
Four review rounds proved that scraping codes/scopes out of free text
(stdout / stderr / json string fields) has no fixed point: every tightening
missed real errors, every loosening misread coincidental digits, and each
review round could construct a counterexample against whichever side the
design picked. So classification trusts exactly one signal: an int-typed (or
digit-string) ``code`` field at the top level of the parsed JSON dict, falling
back to ``error``/``data`` nesting at most 2 levels deep, with the top level
always winning. A permission error reported only as prose gets NO hint — that
is the status quo for every other error today, and strictly better than a
wrong hint.

Other guarantees (each maps to a confirmed prior regression):

* **Success is never touched.** Only ``ok is False`` results are inspected.
* **Number conversion never raises — type allowlist, not exception
  enumeration.** A JSON ``code`` is only ever an int or a string; floats are
  rejected outright (``int(float("inf"))`` raises OverflowError and
  ``int(99991672.9)`` would truncate into a false match), and str conversion
  failures of any kind (Unicode lookalikes, over-long digit strings) are
  caught.
* **app_id is allowlist-validated** before it lands in a URL path; dirty
  values degrade to the generic console link.

Domain follows the repo-wide ``FEISHU_OPEN_BASE_URL`` convention
(``feishu_uat_auth.py``) so larksuite / self-hosted tenants get the right host.
"""

from __future__ import annotations

import os
import re


DEFAULT_OPEN_DOMAIN = "https://open.feishu.cn"
LARK_PERMISSION_CODES: dict[int, str] = {
    99991672: "app_scope_missing",        # APP lacks the scope; only an admin can grant it.
    99991679: "user_scope_insufficient",  # APP has the scope; the user must /auth.
}

# Real Feishu app ids are ``cli_`` + ~16 alnum (~20 chars); 64 is a generous
# ceiling. Bounded so a pathological/oversized env value degrades to the generic
# link instead of bloating the user-visible hint (codex concern 2026-07-10).
_APP_ID_SAFE = re.compile(r"[A-Za-z0-9_-]{1,64}")


_DOMAIN_SAFE = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?")


def _open_domain() -> str:
    """Same env convention as feishu_uat_auth.py, resolved per call.

    Shape-validated: the value lands verbatim in a user-visible hint AFTER the
    stderr redaction pipeline has already run, so anything that is not a bare
    ``https://host[:port]`` (query strings, userinfo, fragments, http://) falls
    back to the default rather than reaching the user. NOTE: in production the
    agent subprocess env is allowlist-scrubbed and does not currently forward
    FEISHU_OPEN_BASE_URL, so prod effectively always uses the default —
    correct for this feishu.cn deployment; plumb the env through
    agent_real's subprocess allowlist if a larksuite tenant ever appears."""
    raw = os.environ.get("FEISHU_OPEN_BASE_URL", DEFAULT_OPEN_DOMAIN).rstrip("/")
    return raw if _DOMAIN_SAFE.fullmatch(raw) else DEFAULT_OPEN_DOMAIN


def _as_int_code(value: object) -> int | None:
    """Exact integer code from an int or an ASCII-digit string; anything else
    is None. The explicit ASCII-digit gate keeps the contract exact — bare
    ``int()`` would also accept ``"+99991672"``, ``"99_991_672"``, full-width
    and Arabic-Indic digits, which are not code shapes the Feishu API emits.
    ``int()`` stays inside try/except for the CPython digit-length limit."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isascii() and text.isdigit():
            try:
                return int(text)
            except ValueError:
                return None
        return None
    return None


def _label_for_dict(node: object, depth: int = 0) -> str | None:
    """Permission label from a dict's structured ``code`` field.

    The top-level ``code`` is authoritative: a permission code classifies, and
    any OTHER definitive code (e.g. rate-limit 230020) ends classification —
    nested ``error``/``data`` codes must never override it. We descend (at most
    2 levels) only when the level has NO code at all, or a non-definitive
    ``code: 0`` success-shell wrapping a nested error object.
    """
    if not isinstance(node, dict):
        return None
    code = _as_int_code(node.get("code"))
    if code in LARK_PERMISSION_CODES:
        return LARK_PERMISSION_CODES[code]
    if code is not None and code != 0:
        return None  # definitive non-permission code — never overridden
    if depth >= 2:
        return None
    for key in ("error", "data"):
        label = _label_for_dict(node.get(key), depth + 1)
        if label is not None:
            return label
    return None


def classify_lark_error(parsed: object) -> str | None:
    """Permission label from the structured ``code`` field of a dict payload,
    or None. Non-dict payloads (None / list / scalar) are never classified —
    structured fields are the only trusted signal (see module docstring)."""
    return _label_for_dict(parsed)


def build_permission_url(app_id: str | None, *, open_domain: str | None = None) -> str:
    """Admin permission page. With a well-formed app_id → that app\'s auth page;
    otherwise → the generic app console.

    app_id is env-derived (LARKSUITE_CLI_APP_ID) and interpolated into a URL
    path, so it is allowlist-validated — a dirty value (``cli_x?redirect=...``,
    slashes, whitespace, Unicode) degrades to the generic link rather than
    emitting a corrupted/misleading URL."""
    domain = open_domain or _open_domain()
    if app_id and _APP_ID_SAFE.fullmatch(app_id):
        return f"{domain}/app/{app_id}/auth?token_type=user"
    return f"{domain}/app"


def annotate_permission_error(result: dict, app_id: str | None = None, *, open_domain: str | None = None) -> dict:
    """Append one permission-hint line to ``stderr_redacted`` for a genuine
    permission failure. Pure: never mutates ``result``; returns the same object
    unchanged otherwise.

    Gate 1: only a FAILED command (``ok is False``) is ever inspected.
    Gate 2: only a structured ``code`` field classifies (no text scanning).
    """
    if result.get("ok") is not False:
        return result

    label = classify_lark_error(result.get("json"))
    if label is None:
        return result

    if label == "app_scope_missing":
        url = build_permission_url(app_id, open_domain=open_domain)
        hint = f"⚠️ 应用未开通所需权限，只有管理员能在开放平台开通：{url}"
    else:
        # Copy aligned with the tool's pre-flight auth-gate message: name both
        # remediation surfaces (Feishu /auth hub, WebUI 凭证 page) so the same
        # failure never shows two divergent instructions depending on layer.
        hint = "⚠️ 需要重新授权：请在飞书私聊 Hermes 发送 /auth（或在 WebUI 打开「凭证」页）完成授权。"

    stderr = result.get("stderr_redacted") or ""
    annotated = dict(result)
    annotated["stderr_redacted"] = f"{stderr}\n{hint}" if stderr else hint
    return annotated


if __name__ == "__main__":
    # Gate 1: a successful result is never touched, even with a coincidental code.
    ok = {"ok": True, "json": {"code": 0, "data": {"chat_id": "99991672"}}, "stdout": "", "stderr_redacted": "keep"}
    assert annotate_permission_error(ok) is ok and ok["stderr_redacted"] == "keep"

    # Failure + top-level app-scope code → admin hint + generic (no-scope) link.
    app = {"ok": False, "json": {"code": 99991672}, "stdout": "", "stderr_redacted": "boom"}
    out = annotate_permission_error(app, app_id="cli_x")
    assert out is not app and app["stderr_redacted"] == "boom"
    assert "只有管理员" in out["stderr_redacted"]
    assert "/app/cli_x/auth?token_type=user" in out["stderr_redacted"]

    # Top-level code beats nested; nested only when top absent/non-matching.
    assert classify_lark_error({"code": 99991679, "data": {"code": 99991672}}) == "user_scope_insufficient"
    assert classify_lark_error({"code": 0, "data": {"code": 99991672}}) == "app_scope_missing"

    # Gate 2: no text scanning — prose-only codes do NOT classify (by decision).
    assert classify_lark_error(None) is None
    assert classify_lark_error([{"code": 99991672}]) is None
    assert classify_lark_error({"ok": False, "error": "permission denied (99991672)"}) is None
    # ...and a legitimate non-permission code is never overridden by anything.
    assert classify_lark_error({"code": 230020}) is None

    # Numbers never crash and floats never match.
    assert _as_int_code("9" * 5000) is None
    assert _as_int_code("9999167²") is None
    assert _as_int_code(float("inf")) is None
    assert _as_int_code(99991672.9) is None

    # Dirty app_id degrades to the generic link.
    assert build_permission_url("cli_x?redirect=evil").endswith("/app")

    print("feishu_permission_errors self-check OK")
