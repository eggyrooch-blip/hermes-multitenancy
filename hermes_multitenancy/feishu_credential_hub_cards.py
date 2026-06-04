"""Feishu interactive card for the ``/auth`` credential hub.

Renders ONE OpenClaw v2 card listing every credential (lark-cli, keep-record,
kep-cli) with a status badge, expiry, and — when the tool's auth flow is wired
and a credential is needed — an inline "前往授权" URL button. This mirrors the
WebUI ``CredentialsView.vue`` collection so Feishu-only users can self-serve.

Style intentionally matches ``feishu_auth_cards`` (schema 2.0, blue header,
``column_set`` right-aligned buttons) so ``/feishu_auth`` reads as one row of
this hub. Transport/update helpers are reused from ``feishu_auth_cards``.
"""
from __future__ import annotations

from typing import Any, Optional

from .credential_hub import CredentialRow, human_expiry

_LOCALES = ["zh_cn", "en_us"]

# status → (emoji, zh label, en label, color)
_STATUS_BADGE = {
    "authenticated": ("✅", "已认证", "Authenticated", "green"),
    "needs_auth": ("⚠️", "未认证", "Not authenticated", "orange"),
    "expired": ("⏰", "已过期", "Expired", "red"),
    "missing": ("⚪", "未安装", "Not installed", "grey"),
    "unknown": ("🔍", "待验证", "Unverified", "grey"),
}


def _i18n(zh: str, en: str) -> dict[str, str]:
    return {"zh_cn": zh, "en_us": en}


def _plain_i18n(zh: str, en: str) -> dict[str, Any]:
    return {"tag": "plain_text", "content": en, "i18n_content": _i18n(zh, en)}


def _badge(status: str) -> tuple[str, str, str, str]:
    return _STATUS_BADGE.get(status, _STATUS_BADGE["unknown"])


def _row_markdown(row: CredentialRow) -> dict[str, Any]:
    emoji, zh_label, en_label, color = _badge(row.status)
    expiry = human_expiry(row.expires_at)
    hint = f" · {row.account_hint}" if row.account_hint else ""

    zh_parts = [f"**{row.title}**  {emoji} <font color='{color}'>{zh_label}</font>{hint}"]
    en_parts = [f"**{row.title}**  {emoji} <font color='{color}'>{en_label}</font>{hint}"]
    if expiry:
        zh_parts.append(f"<font color='grey'>{expiry}</font>")
        en_parts.append(f"<font color='grey'>{expiry}</font>")
    if row.detail:
        zh_parts.append(f"<font color='grey'>{row.detail}</font>")
        en_parts.append(f"<font color='grey'>{row.detail}</font>")
    return {
        "tag": "markdown",
        "content": "\n".join(en_parts),
        "i18n_content": _i18n("\n".join(zh_parts), "\n".join(en_parts)),
        "text_size": "normal",
    }


def _auth_button(url: str, *, label_zh: str = "前往授权", label_en: str = "Authorize") -> dict[str, Any]:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "horizontal_align": "right",
        "columns": [
            {
                "tag": "column",
                "width": "auto",
                "elements": [
                    {
                        "tag": "button",
                        "text": _plain_i18n(label_zh, label_en),
                        "type": "primary",
                        "size": "small",
                        "multi_url": {
                            "url": url,
                            "pc_url": url,
                            "android_url": url,
                            "ios_url": url,
                        },
                    }
                ],
            }
        ],
    }


def build_hub_card(
    *,
    rows: list[CredentialRow],
    auth_urls: Optional[dict[str, str]] = None,
    pending_note: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Build the credential-hub card.

    ``auth_urls`` maps credential id → a verification/authorize URL. A row gets
    a button only when (a) it is not already authenticated AND (b) an URL is
    provided. ``pending_note`` maps credential id → a short zh note shown when a
    not-yet-wired tool needs auth (e.g. "请在 WebUI 完成").
    """
    auth_urls = auth_urls or {}
    pending_note = pending_note or {}
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": "Authenticate the tools below one by one.",
            "i18n_content": _i18n(
                "在下方逐个完成各工具的认证。已认证的会显示有效期。",
                "Authenticate the tools below one by one.",
            ),
            "text_size": "normal",
        },
        {"tag": "hr"},
    ]

    for idx, row in enumerate(rows):
        elements.append(_row_markdown(row))
        url = auth_urls.get(row.id)
        if not row.authenticated and url:
            elements.append(_auth_button(url))
        elif not row.authenticated and pending_note.get(row.id):
            note = pending_note[row.id]
            elements.append(
                {
                    "tag": "markdown",
                    "content": note,
                    "i18n_content": _i18n(note, note),
                    "text_size": "notation",
                }
            )
        if idx != len(rows) - 1:
            elements.append({"tag": "hr"})

    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": False,
            "update_multi": True,
            "locales": _LOCALES,
        },
        "header": {
            "title": _plain_i18n("凭证中心", "Credential hub"),
            "subtitle": {"tag": "plain_text", "content": ""},
            "template": "blue",
            "padding": "12px 12px 12px 12px",
            "icon": {"tag": "standard_icon", "token": "lock-chat_filled"},
        },
        "body": {"elements": elements},
    }
