"""Unified /auth credential-hub card actions (lazy per-credential auth on click).

The ``/auth`` hub now sends INSTANTLY: one unified 认证/重新认证 callback button
per credential, nothing pre-generated (see router.commands._handle_auth_command
+ feishu_credential_hub_cards.build_hub_card). This module handles the click:

  click 认证 → mint ONLY that credential's auth entry (device-flow session / QR /
  kep login) → expand that row in place (QR/URL shown, others stay collapsed
  buttons) → background-poll that one flow → on success flip its row to ✅.

Same class-level ``_on_card_action_trigger`` monkeypatch mechanism as
``feishu_group_valve`` (chained: non-``cred_auth`` actions delegate to the
original, which the core routes to its own handlers). Installed at plugin
register time via ``__init__``. The patch lands on the module the gateway
actually runs because ``load_feishu_adapter`` resolves the plugin-loader's
synthetic module (see feishu_adapter_compat)."""
from __future__ import annotations

import asyncio
import functools
import logging
from pathlib import Path
from typing import Any, Optional

from .feishu_adapter_compat import load_feishu_adapter, log_feishu_adapter_load_error

logger = logging.getLogger(__name__)

_HOOK_INSTALLED = False
_CARD_ACTION_FLAG = "_hermes_multitenancy_cred_auth_card_action_patched"


def install_feishu_auth_hub_actions() -> None:
    """Idempotently install the cred_auth card-action handler on FeishuAdapter."""
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    try:
        FeishuAdapter = load_feishu_adapter()
    except Exception as exc:
        log_feishu_adapter_load_error(
            logger,
            "[multitenancy] FeishuAdapter not importable yet; cred_auth card-action hook deferred",
            exc,
        )
        return
    _patch_card_action(FeishuAdapter)
    _HOOK_INSTALLED = True


def _patch_card_action(FeishuAdapter: Any) -> None:
    original = getattr(FeishuAdapter, "_on_card_action_trigger", None)
    if original is None or getattr(original, _CARD_ACTION_FLAG, False):
        return

    @functools.wraps(original)
    def wrapped(self: Any, data: Any) -> Any:
        try:
            event = _read_value(data, "event")
            action = _read_value(event, "action")
            action_value = _read_value(action, "value") or {}
            if not isinstance(action_value, dict):
                action_value = {}
            if action_value.get("hermes_action") != "cred_auth":
                return original(self, data)
            return _handle_cred_auth_action(self, event, action_value)
        except Exception:
            logger.debug(
                "[multitenancy] cred_auth card action failed; delegating to original",
                exc_info=True,
            )
            return original(self, data)

    setattr(wrapped, _CARD_ACTION_FLAG, True)
    FeishuAdapter._on_card_action_trigger = wrapped
    logger.info("[multitenancy] installed cred_auth card-action hook on %s.FeishuAdapter", FeishuAdapter.__module__)


def _handle_cred_auth_action(adapter: Any, event: Any, action_value: dict[str, Any]) -> Any:
    """Mint the clicked credential's auth entry, expand its row in place, and
    background-poll it to ✅. Runs on the SDK callback thread (blocking network
    calls are fine here); the poll is scheduled on the adapter loop."""
    from . import feishu_uat_auth
    from .feishu_credential_hub_cards import build_hub_card

    cred = str(action_value.get("cred") or "").strip()
    chat_id = _ctx_chat_id(event, action_value)
    message_id = _ctx_message_id(event)
    if not cred:
        return _toast_response("认证信息不完整，请重新发送 /auth")

    # SECURITY: identity and profile are resolved from the Feishu-SIGNED event
    # operator (who actually clicked), NEVER from the unsigned callback payload.
    # The card can be clicked by anyone who sees it (e.g. any group member), so
    # trusting a profile/open_id baked into the button value would be a
    # cross-user / cross-profile write path. Deriving both from the signed
    # operator means a clicker can only ever authenticate their OWN resolved
    # profile, and profile_home fidelity comes from the authoritative routing.
    # Resolves from the operator's FULL id set (open_id / union_id / user_id) so
    # a Schema-2 callback that omits open_id still matches (mirrors group_valve).
    shared = feishu_uat_auth.resolve_shared_home()
    profile_name, open_id, pdir = _resolve_operator_profile(event, shared)
    if not profile_name or not open_id or pdir is None:
        return _toast_response("无法确认你的 Hermes profile，请先私聊我发送 /auth")
    ctx = {"chat_id": chat_id}

    # Mint ONLY this credential's entry (blocking network — Feishu shows the
    # button's native loading spinner meanwhile).
    auth_urls, qr_image_keys, pending_note, flows = _mint_one_cred(
        cred, profile_name=profile_name, open_id=open_id, pdir=pdir, shared=shared
    )

    rows = _collect_rows(profile_name=profile_name, open_id=open_id, pdir=pdir)
    card = build_hub_card(
        rows=rows, ctx=ctx,
        auth_urls=auth_urls, qr_image_keys=qr_image_keys, pending_note=pending_note,
    )

    # Schedule the success-poll on the adapter loop so the row flips to ✅ once
    # the user completes the scan/login. Reuses the hardened _poll_hub_flows.
    loop = getattr(adapter, "_loop", None)
    if flows and message_id and loop is not None and not loop.is_closed():
        try:
            from .router import _poll_hub_flows
            hub_card = {"message_id": message_id}
            asyncio.run_coroutine_threadsafe(
                _poll_hub_flows(
                    profile_name=profile_name, open_id=open_id,
                    profile_dir=pdir, shared_home=shared, chat_id=chat_id,
                    gateway=None, adapter=adapter, hub_card=hub_card, flows=flows,
                    auth_urls=auth_urls, qr_image_keys=qr_image_keys, ctx=ctx,
                ),
                loop,
            )
        except Exception:
            logger.debug("[multitenancy] cred_auth poll schedule failed", exc_info=True)

    return _updated_card_response(card)


def _mint_one_cred(
    cred: str, *, profile_name: str, open_id: str, pdir: Path, shared: Path
) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, dict[str, Any]]]:
    """Mint a single credential's inline auth entry. Returns
    (auth_urls, qr_image_keys, pending_note, flows) each keyed by ``cred``."""
    import os as _os

    from . import credential_hub, credential_hub_auth as cha, feishu_uat_auth

    auth_urls: dict[str, str] = {}
    qr_image_keys: dict[str, str] = {}
    pending_note: dict[str, str] = {}
    flows: dict[str, dict[str, Any]] = {}
    try:
        if cred == credential_hub.LARK_CLI:
            session = feishu_uat_auth.find_active_session(profile_name=profile_name, open_id=open_id)
            if not session:
                session = feishu_uat_auth.start_session(profile_name=profile_name, open_id=open_id)
            uri = str(session.get("verification_uri") or "")
            if uri:
                auth_urls[cred] = uri
                flows[cred] = {"kind": "lark", "session_id": str(session.get("session_id") or "")}
        elif cred == credential_hub.KEEP_RECORD:
            qr = cha.start_keep_record_qr(pdir)
            image_key = cha.fetch_qr_image_key(shared, qr["qrcode_url"])
            qr_image_keys[cred] = image_key
            flows[cred] = {"kind": "keep", "qrcode_id": qr["qrcode_id"]}
        elif cred in credential_hub.KEP_CLI_IDS:
            origin = _os.environ.get("HERMES_PUBLIC_CALLBACK_ORIGIN", "").strip() or None
            if not origin:
                pending_note[cred] = "kep-cli 飞书内认证需公网回调（已在 prod 支持），本机请用 WebUI。"
            else:
                env_name = "pre" if cred == credential_hub.KEP_CLI_PRE else "online"
                try:
                    from . import webui_broker_server as _wb
                    _wb.ensure_run_broker_server_started()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("[multitenancy] run-broker lazy start failed (%s)", exc)
                login = cha.start_kep_cli_login(
                    pdir, profile_name, shared, public_origin=origin, env_name=env_name
                )
                proc = login.get("_proc")
                try:
                    from .router import _track_kep_login_proc
                    _track_kep_login_proc(proc)
                except Exception:
                    pass
                auth_urls[cred] = login["verification_uri"]
                flows[cred] = {"kind": "kep", "proc": proc, "env": env_name}
        else:
            pending_note[cred] = "该凭证暂不支持在飞书内认证，请用 WebUI。"
    except cha.HubAuthError as exc:
        pending_note[cred] = f"暂时无法开始认证：{exc.message}"
    except Exception as exc:
        logger.debug("[multitenancy] cred_auth mint %s failed (%s)", cred, exc, exc_info=True)
        pending_note[cred] = "暂时无法开始认证，请稍后重试。"
    return auth_urls, qr_image_keys, pending_note, flows


def _collect_rows(*, profile_name: str, open_id: str, pdir: Path) -> list:
    from . import credential_hub
    from .router import _filter_hub_rows_for_auth
    try:
        rows = credential_hub.collect_credential_statuses(
            profile_name=profile_name, open_id=open_id, home_dir=pdir / "home",
        )
        return _filter_hub_rows_for_auth(rows)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[multitenancy] cred_auth status refresh failed (%s)", exc)
        return []


# --- event field readers + SDK response builders (mirror feishu_group_valve) ---

def _read_value(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _ctx_chat_id(event: Any, action_value: dict[str, Any]) -> str:
    chat_id = str(action_value.get("chat_id") or "").strip()
    if chat_id:
        return chat_id
    context = _read_value(event, "context")
    return str(_read_value(context, "open_chat_id") or "").strip()


def _ctx_message_id(event: Any) -> str:
    context = _read_value(event, "context")
    for name in ("open_message_id", "message_id"):
        value = _read_value(context, name)
        if value:
            return str(value)
    return str(_read_value(event, "open_message_id") or "").strip()


def _operator_typed_ids(event: Any) -> dict[str, str]:
    """The SIGNED operator's typed ids. Under Feishu card-callback Schema 2 the
    operator may omit ``open_id`` and carry only ``union_id`` / ``user_id``, so
    all three are extracted and resolved (mirrors feishu_group_valve)."""
    operator = _read_value(event, "operator") or _read_value(event, "operator_id")

    def pick(*names: str) -> str:
        for name in names:
            value = _read_value(operator, name)
            if value:
                return str(value).strip()
        return ""

    return {
        "open_id": pick("open_id", "operator_open_id"),
        "union_id": pick("union_id", "operator_union_id"),
        "user_id": pick("user_id", "operator_user_id"),
    }


def _resolve_operator_profile(event: Any, shared: Path) -> tuple[str, str, Optional[Path]]:
    """Authoritatively map the SIGNED operator → (profile_name, open_id, profile_dir)
    via the routing table, trying every typed id (open_id → resolve_owner_root /
    lookup_by_open_id, union_id → lookup_by_union_id, user_id → lookup_by_user_id).
    Returns ("", "", None) when the clicker has no bound profile, so an
    unrecognized operator gets a clean rejection rather than a mis-targeted auth.
    The resolved ``open_id`` is the routing row's authoritative open_id (needed by
    the auth flow) even when the Schema-2 event only carried a union_id/user_id."""
    ids = _operator_typed_ids(event)
    try:
        from .router import _get_routing_table
        table = _get_routing_table()
        if table is None:
            return "", "", None
        owner = None
        if ids["open_id"]:
            owner = table.resolve_owner_root(ids["open_id"]) or table.lookup_by_open_id(ids["open_id"])
        if owner is None and ids["union_id"]:
            owner = table.lookup_by_union_id(ids["union_id"])
        if owner is None and ids["user_id"]:
            owner = table.lookup_by_user_id(ids["user_id"])
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[multitenancy] cred_auth operator resolve failed (%s)", exc)
        return "", "", None
    if owner is None:
        return "", "", None
    profile_name = str(getattr(owner, "profile_name", "") or "").strip()
    open_id = str(getattr(owner, "open_id", "") or "").strip() or ids["open_id"]
    if not profile_name or not open_id:
        return "", "", None
    pdir = shared / "profiles" / profile_name
    if not pdir.exists():
        return "", "", None
    return profile_name, open_id, pdir


def _toast_response(content: str, *, level: str = "info") -> Any:
    try:
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )
    except Exception:
        return {"kind": "toast", "toast": {"type": level, "content": content}}
    response = P2CardActionTriggerResponse()
    response.toast = {"type": level, "content": content}
    return response


def _updated_card_response(card: dict[str, Any]) -> Any:
    try:
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            CallBackCard,
            P2CardActionTriggerResponse,
        )
    except Exception:
        return {"kind": "card", "card": {"type": "raw", "data": card}}
    response = P2CardActionTriggerResponse()
    callback_card = CallBackCard()
    callback_card.type = "raw"
    callback_card.data = card
    response.card = callback_card
    return response
