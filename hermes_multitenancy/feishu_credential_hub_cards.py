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


#: Feishu form control names — read back off ``action.form_value`` on submit.
GITLAB_FORM = "gitlab_token_form"
GITLAB_TOKEN_FIELD = "gitlab_token"
GITLAB_EXPIRY_FIELD = "gitlab_expiry"
GITLAB_TIER_FIELD = "gitlab_tier"
GITLAB_SUBMIT_ACTION = "gitlab_token"


def build_gitlab_token_form_card(*, notice: str = "") -> dict[str, Any]:
    """The employee's own-GitLab-token form.

    A form, not a chat prompt, on purpose: what the user types into a form
    control is submitted as ``form_value`` and never becomes a chat message, so
    the token does not end up sitting in the conversation history.

    Follows the same four rules the clarify/fill-form cards were hardened on:
    ONE form container (Feishu only returns ``form_value`` for controls inside a
    single form), the submit button MUST carry a ``value`` (a value-less submit
    is silently dropped with error 200340), the action name is mirrored in both
    ``value`` and ``behaviors`` so a double-tap collapses to one key, and the
    card is re-rendered in place so the DM-allowlist message id is preserved.
    """
    elements: list[dict[str, Any]] = []
    if notice:
        elements.append({
            "tag": "markdown",
            "content": notice,
            "text_size": "notation",
        })
    elements.append({
        "tag": "markdown",
        "content": (
            "填你自己的 GitLab token，hermes 之后就用**你本人的权限**操作仓库。\n\n"
            "在 GitLab 建 token 时：\n"
            "1. **名字必须填 `hermes`** —— 我们靠这个名字找到它、核对你给的权限\n"
            "2. **填一个到期日**（GitLab 允许不填，但我们不接受永久有效的）\n"
            "3. 按你选的档位勾 scope：\n"
            "   · **只读** —— 看 MR、issue、流水线、拉代码：勾 `read_api` + `read_repository`\n"
            "   · **可写** —— 上面全部再加改动和推代码：勾 `api` + `write_repository`\n\n"
            "两档都必须带一个 API scope，只勾 repository 的话 hermes 调不动 GitLab 接口。\n"
            "过期后会自动停用，需要你回这里换一个新的。\n\n"
            "**说明**：我们靠 token 的名字去核对权限，这能帮你发现填错档位，"
            "但**没法严格保证**你粘贴的就是那个 token。请你自己确认交出的权限就是你想给的——"
            "hermes 会拿着它以你的身份操作仓库。"
        ),
        "text_size": "notation",
    })
    elements.append({
        "tag": "form",
        "name": GITLAB_FORM,
        "elements": [
            {
                "tag": "select_static",
                "name": GITLAB_TIER_FIELD,
                "required": True,
                "label": _plain_i18n("授权档位", "Access tier"),
                "placeholder": _plain_i18n("选一档", "Pick one"),
                "options": [
                    {"text": _plain_i18n("只读（read_api + read_repository）",
                                         "Read-only"), "value": "read"},
                    {"text": _plain_i18n("可写（api + write_repository）",
                                         "Read-write"), "value": "write"},
                ],
            },
            {
                "tag": "input",
                "name": GITLAB_TOKEN_FIELD,
                "required": True,
                "label": _plain_i18n("GitLab token", "GitLab token"),
                "placeholder": _plain_i18n("粘贴你的 token", "Paste your token"),
            },
            {
                "tag": "input",
                "name": GITLAB_EXPIRY_FIELD,
                "required": True,
                "label": _plain_i18n("到期日", "Expires on"),
                "placeholder": _plain_i18n("YYYY-MM-DD", "YYYY-MM-DD"),
            },
            {
                "tag": "button",
                "name": "gitlab_token_submit",
                "text": _plain_i18n("提交", "Submit"),
                "type": "primary",
                "size": "small",
                "form_action_type": "submit",
                "value": {"hermes_action": GITLAB_SUBMIT_ACTION},
                "behaviors": [
                    {"type": "callback", "value": {"hermes_action": GITLAB_SUBMIT_ACTION}}
                ],
            },
        ],
    })
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": False, "update_multi": True, "locales": _LOCALES},
        "header": {
            "title": _plain_i18n("GitLab — 使用我自己的权限", "GitLab — use my own access"),
            "subtitle": {"tag": "plain_text", "content": ""},
            "template": "orange", "padding": "12px 12px 12px 12px",
            "icon": {"tag": "standard_icon", "token": "lock-chat_filled"},
        },
        "body": {"elements": elements},
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
