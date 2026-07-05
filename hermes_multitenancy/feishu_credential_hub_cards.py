"""Feishu interactive card for the ``/auth`` credential hub.

Renders ONE OpenClaw v2 card listing every credential row with a status badge,
expiry, and any pre-generated auth entry embedded directly in the hub card
(static authorize URL button or inline scannable QR). This mirrors the WebUI
``CredentialsView.vue`` collection so Feishu-only users can self-serve.

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
    "configured": ("✅", "Token 可读", "Configured", "green"),
    "needs_auth": ("⚠️", "未认证", "Not authenticated", "orange"),
    "expired": ("⏰", "已过期", "Expired", "red"),
    "missing": ("⚪", "未安装", "Not installed", "grey"),
    "unknown": ("🔍", "待验证", "Unverified", "grey"),
    "error": ("❗", "检测失败", "Error", "red"),
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


def _auth_callback_button(
    cred_id: str,
    ctx: dict[str, Any],
    *,
    label_zh: str,
    label_en: str,
    disabled: bool = False,
) -> dict[str, Any]:
    """A right-aligned callback button. Clicking fires ``_on_card_action_trigger``
    with ``value = {hermes_action: 'cred_auth', cred: <id>, **ctx}`` so the hub
    mints THIS credential's auth entry lazily (no eager pre-generation), then
    expands the card in place. Unified across every credential — the single
    interaction the user asked for, replacing the scattered inline-QR / URL mix."""
    value: dict[str, Any] = {"hermes_action": "cred_auth", "cred": cred_id}
    value.update(ctx)
    button: dict[str, Any] = {
        "tag": "button",
        "text": _plain_i18n(label_zh, label_en),
        "type": "primary",
        "size": "small",
        "value": value,
    }
    if disabled:
        button["disabled"] = True
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "horizontal_align": "right",
        "columns": [{"tag": "column", "width": "auto", "elements": [button]}],
    }


def _qr_image(img_key: str, *, label_zh: str = "请使用对应 App 扫码认证", label_en: str = "Scan to authenticate") -> list[dict[str, Any]]:
    return [
        {"tag": "img", "img_key": img_key, "alt": _plain_i18n(label_zh, label_en), "mode": "fit_horizontal", "preview": True},
        {"tag": "markdown", "content": label_en, "i18n_content": _i18n(label_zh, label_en), "text_size": "notation"},
    ]


def build_qr_card(title: str, image_key: str, *, hint_zh: str = "请用对应 App 扫码完成认证") -> dict[str, Any]:
    """A card showing a scannable QR image (sent after the user clicks 认证)."""
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": False, "update_multi": True, "locales": _LOCALES},
        "header": {
            "title": _plain_i18n(f"{title} 扫码认证", f"{title} — scan to authenticate"),
            "subtitle": {"tag": "plain_text", "content": ""},
            "template": "blue", "padding": "12px 12px 12px 12px",
            "icon": {"tag": "standard_icon", "token": "lock-chat_filled"},
        },
        "body": {"elements": [
            {"tag": "img", "img_key": image_key, "alt": _plain_i18n(hint_zh, "Scan to authenticate"),
             "mode": "fit_horizontal", "preview": True},
            {"tag": "markdown", "content": hint_zh, "i18n_content": _i18n(hint_zh, "Scan to authenticate"),
             "text_size": "notation"},
        ]},
    }


def build_url_card(title: str, url: str, *, label_zh: str = "前往认证", label_en: str = "Authorize") -> dict[str, Any]:
    """A card with a 前往认证 URL button (sent after the user clicks 认证)."""
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": False, "update_multi": True, "locales": _LOCALES},
        "header": {
            "title": _plain_i18n(f"{title} 认证", f"{title} authentication"),
            "subtitle": {"tag": "plain_text", "content": ""},
            "template": "blue", "padding": "12px 12px 12px 12px",
            "icon": {"tag": "standard_icon", "token": "lock-chat_filled"},
        },
        "body": {"elements": [
            {"tag": "markdown",
             "content": "Open the page below to authorize, then come back.",
             "i18n_content": _i18n("点开下方链接完成授权，完成后返回飞书查看结果。",
                                   "Open the page below to authorize, then come back."),
             "text_size": "normal"},
            _auth_button(url, label_zh=label_zh, label_en=label_en),
        ]},
    }


def build_success_card(title: str, *, expiry_zh: str = "") -> dict[str, Any]:
    """A small green '✅ <title> 认证成功' card pushed when an auth flow completes."""
    body_zh = f"**{title}** 认证成功 ✅"
    body_en = f"**{title}** authenticated ✅"
    if expiry_zh:
        body_zh += f"\n\n<font color='grey'>{expiry_zh}</font>"
        body_en += f"\n\n<font color='grey'>{expiry_zh}</font>"
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": False, "update_multi": True, "locales": _LOCALES},
        "header": {
            "title": _plain_i18n("认证成功", "Authenticated"),
            "subtitle": {"tag": "plain_text", "content": ""},
            "template": "green",
            "padding": "12px 12px 12px 12px",
            "icon": {"tag": "standard_icon", "token": "yes_filled"},
        },
        "body": {"elements": [{"tag": "markdown", "content": body_en, "i18n_content": _i18n(body_zh, body_en)}]},
    }


def build_hub_card(
    *,
    rows: list[CredentialRow],
    auth_urls: Optional[dict[str, str]] = None,
    pending_note: Optional[dict[str, str]] = None,
    qr_image_keys: Optional[dict[str, str]] = None,
    ctx: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build the credential-hub card.

    Two rendering modes, mixable per row:

    * **Collapsed (default, fast)** — when ``ctx`` is provided, every credential
      renders a unified *认证 / 重新认证* CALLBACK button carrying
      ``{hermes_action: 'cred_auth', cred: <id>, **ctx}``. Nothing is
      pre-generated, so the card sends instantly and every credential (incl.
      expired ones) gets a re-auth control.
    * **Expanded (after a click / on poll re-render)** — ``auth_urls`` maps
      credential id → a verification URL (renders a 前往认证 button) and
      ``qr_image_keys`` maps credential id → a Feishu image_key (renders an
      inline scannable QR). A row present in either shows its entry inline
      instead of the collapsed button. ``pending_note`` maps id → a short note
      (e.g. kep-cli needs a public callback → use WebUI locally).
    """
    auth_urls = auth_urls or {}
    pending_note = pending_note or {}
    qr_image_keys = qr_image_keys or {}
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
        if row.id in qr_image_keys:
            # Render the scannable QR whether or not the row is authenticated so the
            # user can re-verify on demand (sunke 2026-06-26). The hub handler only
            # mints a QR for a row it can actually (re-)start.
            if row.authenticated:
                elements.extend(_qr_image(qr_image_keys[row.id], label_zh="重新认证：请使用对应 App 扫码", label_en="Re-authenticate — scan with the app"))
            else:
                elements.extend(_qr_image(qr_image_keys[row.id]))
        elif row.id in auth_urls:
            # A minted URL is only present when the hub handler chose to offer
            # (re-)authorization for this row. Render it even when the row reads
            # authenticated so the user can re-verify on demand — lark always, and
            # kep-cli when a public callback origin is set (issue:
            # auth-hub-lark-reauth-button, broadened 2026-06-26). A row with no
            # minted URL/QR shows only its status (no dead control).
            if row.authenticated:
                elements.append(_auth_button(auth_urls[row.id], label_zh="重新授权", label_en="Re-authorize"))
            else:
                elements.append(_auth_button(auth_urls[row.id]))
        elif pending_note.get(row.id):
            # Expanded-state note for a credential that can't mint an inline
            # entry here (e.g. kep-cli locally needs a public callback → WebUI).
            note = pending_note[row.id]
            elements.append({"tag": "markdown", "content": note,
                             "i18n_content": _i18n(note, note), "text_size": "notation"})
        elif ctx is not None:
            # Collapsed default: one unified callback button per credential. The
            # click handler mints this row's entry lazily and re-renders it into
            # the expanded state above. Authenticated rows still offer re-auth.
            if row.authenticated:
                elements.append(_auth_callback_button(row.id, ctx, label_zh="重新认证", label_en="Re-authenticate"))
            else:
                elements.append(_auth_callback_button(row.id, ctx, label_zh="认证", label_en="Authenticate"))
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
