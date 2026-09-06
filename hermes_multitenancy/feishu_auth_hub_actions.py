"""Unified /auth credential-hub card actions (lazy per-credential auth on click).

The ``/auth`` hub now sends INSTANTLY: one unified 认证/重新认证 callback button
per credential, nothing pre-generated (see router.commands._handle_auth_command
+ feishu_credential_hub_cards.build_hub_card). This module handles the click:

  click 认证 → mint ONLY that credential's auth entry (device-flow session / QR /
  kep login) → expand that row in place (QR/URL shown, others stay collapsed
  buttons) → background-poll that one flow → on success flip its row to ✅.

``cred_auth`` / ``gitlab_token`` are BUILT-INS of the one card-action dispatcher
(``feishu_card_action_dispatcher``), which owns the live
``_on_card_action_trigger`` and calls ``handle_auth_hub_card_action`` below. This
module no longer installs a wrapper of its own — the retired ``_patch_card_action``
entry point just installs the dispatcher, so any install order converges. The
dispatcher lands on the module the gateway actually runs because
``load_feishu_adapter`` resolves the plugin-loader's synthetic module (see
feishu_adapter_compat)."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

from .feishu_adapter_compat import load_feishu_adapter, log_feishu_adapter_load_error

logger = logging.getLogger(__name__)

_HOOK_INSTALLED = False
_CARD_ACTION_FLAG = "_hermes_multitenancy_cred_auth_card_action_patched"

# --- DM-provenance allowlist ---------------------------------------------------
# Card-action callbacks carry NO chat_type, and user routing rows store no p2p
# chat_id, so a click alone can't prove it happened in a 1:1 DM. But we know one
# thing for certain: /auth is hard-blocked in groups at the command gate (message
# events DO carry a reliable chat_type), so every hub card we actually SEND lands
# in a private chat. We remember those cards' signed message ids here; a card
# FORWARDED into a group becomes a NEW message with a different id, so its clicks
# miss the allowlist and are rejected. This is the positive DM signal — without
# it an unprovisioned-group forward would fall open (codex round-6 CRITICAL).
#
# ponytail: process-local bounded LRU. On gateway restart the set is empty, so a
# stale card's click fails CLOSED ("re-send /auth") — safe and self-healing.
# Make it durable only if restart-mid-auth becomes a real complaint.
_DM_AUTH_CARD_IDS_MAX = 4096
_dm_auth_card_ids: "OrderedDict[str, None]" = OrderedDict()
_dm_auth_card_lock = threading.Lock()


def record_dm_auth_card(message_id: Optional[str]) -> None:
    """Remember the MESSAGE id of an /auth hub card sent into a DM.

    Called from the (group-blocked) /auth command send site, so every id here
    provably belongs to a 1:1 chat. Message id ONLY — never a CardKit card_id,
    which is the card entity id and survives a forward (see the round-7 note in
    _handle_cred_auth_action).
    """
    mid = str(message_id or "").strip()
    if not mid:
        return
    with _dm_auth_card_lock:
        _dm_auth_card_ids.pop(mid, None)
        _dm_auth_card_ids[mid] = None
        while len(_dm_auth_card_ids) > _DM_AUTH_CARD_IDS_MAX:
            _dm_auth_card_ids.popitem(last=False)


def _is_known_dm_auth_card(message_id: Optional[str]) -> bool:
    """True iff the message id matches a hub card we sent into a DM (see above)."""
    mid = str(message_id or "").strip()
    if not mid:
        return False
    with _dm_auth_card_lock:
        if mid in _dm_auth_card_ids:
            _dm_auth_card_ids.move_to_end(mid)
            return True
    return False


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


def handle_auth_hub_card_action(adapter: Any, cb: Any) -> Any:
    """Built-in auth/credential handler, owned by the one card-action dispatcher.

    A raise here is CONSUMED by the dispatcher as a generic error — it is never
    handed back to the core generic handler, which would turn a failed
    credential click into a synthetic ``/card`` model turn."""
    if cb.kind == "gitlab_token":
        return _handle_gitlab_token_submit(adapter, cb.event, cb.action)
    return _handle_cred_auth_action(adapter, cb.event, cb.value)


def _patch_card_action(FeishuAdapter: Any) -> None:
    """Retired wrapper entry point — installs THE dispatcher instead."""
    from .feishu_card_action_dispatcher import install_feishu_card_action_dispatcher

    install_feishu_card_action_dispatcher(FeishuAdapter)


def _handle_cred_auth_action(adapter: Any, event: Any, action_value: dict[str, Any]) -> Any:
    """Mint the clicked credential's auth entry, expand its row in place, and
    background-poll it to ✅. Runs on the SDK callback thread (blocking network
    calls are fine here); the poll is scheduled on the adapter loop."""
    from . import feishu_uat_auth
    from .feishu_credential_hub_cards import build_hub_card

    cred = str(action_value.get("cred") or "").strip()
    chat_id = _signed_chat_id(event)
    message_id = _ctx_message_id(event)
    if not cred:
        return _toast_response("认证信息不完整，请重新发送 /auth")

    # SCOPING (fail-closed): the credential hub is strictly personal. Two guards,
    # both must pass, so a member can never render/mutate their personal hub in a
    # shared chat:
    #  1) cheap reject if the signed event/routing says this is a group chat;
    #  2) AUTHORITATIVE positive gate — proceed only if this click lands on a hub
    #     card WE sent into a DM (allowlist by signed MESSAGE id). Forwarding a card
    #     into a group produces a NEW message with a message_id we never recorded,
    #     so it is rejected even though callbacks carry no chat_type (round-6 fix).
    # NOTE: match on message_id ONLY, never the card_id — a CardKit card_id is the
    # card ENTITY id and is STABLE across a forward, so trusting it would re-open
    # the exact fall-open this gate closes (codex round-7).
    if _chat_is_group(event, chat_id):
        return _toast_response("认证只能在私聊里进行，请私聊我发送 /auth")
    if not _is_known_dm_auth_card(message_id):
        return _toast_response("认证卡片已过期或不在私聊里，请重新私聊我发送 /auth")

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
    # Empty ctx: the re-rendered card's remaining callback buttons carry ONLY
    # the credential id (never chat_id/identity), identical to the initial /auth
    # card. chat_id flows to the poll separately (below), taken from the signed
    # event — nothing sensitive ever rides in the button payload.
    ctx: dict[str, Any] = {}

    # gitlab has no interactive auth flow to mint — the employee supplies the
    # token themselves, so this row opens a form instead. Rendered in place so
    # the DM-allowlist message id (checked again on submit) is preserved.
    from . import credential_hub as _ch

    if cred == _ch.GITLAB:
        from .feishu_credential_hub_cards import build_gitlab_token_form_card

        return _updated_card_response(build_gitlab_token_form_card())

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


def _gitlab_form_value(action: Any) -> tuple[str, str]:
    """Pull (token, tier) out of the submitted form. Untrusted input.

    No expiry field any more: the expiry is read off the token's own GitLab row
    during intake, never re-typed by the employee.
    """
    from .feishu_credential_hub_cards import (
        GITLAB_TIER_FIELD,
        GITLAB_TOKEN_FIELD,
    )

    raw = _read_value(action, "form_value")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None
    if not isinstance(raw, dict):
        return "", ""
    return (
        str(raw.get(GITLAB_TOKEN_FIELD) or "").strip(),
        str(raw.get(GITLAB_TIER_FIELD) or "").strip(),
    )


def _handle_gitlab_token_submit(adapter: Any, event: Any, action: Any) -> Any:
    """Vault an employee's own GitLab token, submitted from the hub form card.

    This is a credential WRITE path, so it repeats the cred_auth guards verbatim
    rather than trusting that the user reached the form legitimately:
    group chats are refused, the click must land on a hub card WE sent into a
    DM (message-id allowlist, which survives because the form is rendered in
    place), and the target profile is derived from the Feishu-SIGNED operator —
    never from the callback payload or the form body, either of which an
    attacker controls.

    The token value itself is never logged and never echoed back into the card.
    """
    from . import feishu_uat_auth
    from .feishu_credential_hub_cards import build_gitlab_token_form_card, build_hub_card
    from .gitlab_token_intake import TokenRejected, submit_personal_token

    chat_id = _signed_chat_id(event)
    message_id = _ctx_message_id(event)
    if _chat_is_group(event, chat_id):
        return _toast_response("认证只能在私聊里进行，请私聊我发送 /auth")
    if not _is_known_dm_auth_card(message_id):
        return _toast_response("认证卡片已过期或不在私聊里，请重新私聊我发送 /auth")

    shared = feishu_uat_auth.resolve_shared_home()
    profile_name, open_id, pdir = _resolve_operator_profile(event, shared)
    if not profile_name or not open_id or pdir is None:
        return _toast_response("无法确认你的 Hermes profile，请先私聊我发送 /auth")

    token, tier = _gitlab_form_value(action)
    try:
        submit_personal_token(
            profile_name=profile_name,
            token=token,
            tier=tier,
            shared_home=shared,
            credential_subject=open_id,
        )
    except TokenRejected as exc:
        # Re-render the form with the reason so the user can correct it without
        # starting over. Never include the submitted token in the notice.
        return _updated_card_response(
            build_gitlab_token_form_card(notice=f"**没有保存：**{exc.reason}")
        )
    except Exception:
        logger.debug("[multitenancy] gitlab token submit failed", exc_info=True)
        return _toast_response("保存失败，请稍后重试", level="error")

    rows = _collect_rows(profile_name=profile_name, open_id=open_id, pdir=pdir)
    return _updated_card_response(build_hub_card(rows=rows, ctx={}))


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


def _signed_chat_id(event: Any) -> str:
    """The chat where the card actually lives, from the Feishu-SIGNED event
    context ONLY. Never the button payload: a forwarded/stale card could carry a
    spoofed (e.g. original DM) chat_id and slip a personal hub past the
    group-scope guard. This is the authoritative chat for both the group check
    and delivery."""
    context = _read_value(event, "context")
    return str(_read_value(context, "open_chat_id") or "").strip()


def _ctx_message_id(event: Any) -> str:
    context = _read_value(event, "context")
    for name in ("open_message_id", "message_id"):
        value = _read_value(context, name)
        if value:
            return str(value)
    return str(_read_value(event, "open_message_id") or "").strip()


def _chat_is_group(event: Any, chat_id: str) -> bool:
    """True when the click happened in a group/topic chat. Checks the SIGNED
    event chat_type first (catches an UNPROVISIONED group not yet in the routing
    table) and the routing table's group row (catches provisioned groups). Fails
    CLOSED on any error so a personal hub can never leak into a group."""
    try:
        from .router import _extract_chat_type, _is_group_chat_type
        chat_type = _extract_chat_type(event)
        if not chat_type:
            context = _read_value(event, "context")
            chat_type = str(_read_value(context, "chat_type") or "").strip().lower()
        if chat_type and _is_group_chat_type(chat_type):
            return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[multitenancy] cred_auth chat_type check failed (%s)", exc)
        return True
    if chat_id:
        try:
            from .router import _get_routing_table
            table = _get_routing_table()
            if table is not None and table.lookup_by_chat_id(chat_id) is not None:
                return True
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[multitenancy] cred_auth group-row check failed for %s (%s)", chat_id, exc)
            return True
    return False


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
