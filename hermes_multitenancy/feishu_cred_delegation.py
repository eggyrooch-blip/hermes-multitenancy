"""GitLab credential delegation card flow (group profile → initiator DM).

Trigger: a GROUP profile run emits ``("auth_required", {"provider": "gitlab"})``
(marker written by ``credential_tool`` inside the child, surfaced by
``agent_real.streaming``). The router hands it here instead of the Feishu UAT
device-flow. We:

  1. resolve the INITIATOR (message sender) and their personal profile;
  2. if they hold no personal GitLab token → DM them a bind-first notice
     (no delegation card — nothing to delegate);
  3. if a standing (chat-scope) lease already exists → replay immediately;
  4. otherwise DM a delegation card (允许一次 / 允许本群(可撤销) / 拒绝).

The card click is handled by ``handle_delegation_card_action`` (a BUILT-IN of
the one card-action dispatcher, mirroring ``cred_auth``): the clicking operator
MUST be the initiator (anti-spoof, from the Feishu-signed operator id — never
the button payload), an approval writes a lease row and replays the original
group request through the existing synthetic-continue seam, a denial/timeout
posts a friendly notice into the group.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import credential_delegation as _leases

logger = logging.getLogger(__name__)

PENDING_TTL_SECONDS = 10 * 60

ACTION_KIND = "cred_delegation"
CHOICE_ALLOW_ONCE = "allow_once"
CHOICE_ALLOW_CHAT = "allow_chat"
CHOICE_DENY = "deny"

SYNTHETIC_DELEGATION_COMPLETE_TEXT = "我已完成 GitLab 凭证授权，请继续执行之前的操作。"


@dataclass
class _Pending:
    delegation_id: str
    owner_open_id: str
    owner_profile: str
    borrower_profile: str
    group_chat_id: str
    replay_text: str
    gateway: Any
    event: Any
    adapter: Any
    created_at: float = field(default_factory=time.time)
    card_state: Optional[dict[str, Any]] = None


_pending: dict[str, _Pending] = {}
_pending_lock = threading.Lock()


def _reset_pending_for_tests() -> None:
    with _pending_lock:
        _pending.clear()


def _register_pending(entry: _Pending) -> None:
    now = time.time()
    with _pending_lock:
        _sweep_expired_locked(now)
        _pending[entry.delegation_id] = entry


def _sweep_expired_locked(now: float) -> None:
    for key in [
        k for k, v in _pending.items() if now - v.created_at > PENDING_TTL_SECONDS
    ]:
        _pending.pop(key, None)


def _reserve_pending(entry: _Pending) -> bool:
    """Claim the (initiator, group) slot BEFORE the card is sent.

    Checking "is one already pending?" and then awaiting the DM let two
    concurrent runs both pass the check and both DM a card. The check and the
    insert therefore happen under one lock hold; the loser reserves nothing and
    the winner releases its reservation if the DM fails.
    """
    now = time.time()
    with _pending_lock:
        _sweep_expired_locked(now)
        if any(
            other.owner_open_id == entry.owner_open_id
            and other.borrower_profile == entry.borrower_profile
            for other in _pending.values()
        ):
            return False
        _pending[entry.delegation_id] = entry
        return True


def _peek_pending(delegation_id: str) -> Optional[_Pending]:
    with _pending_lock:
        entry = _pending.get(str(delegation_id or ""))
    if entry is None:
        return None
    if time.time() - entry.created_at > PENDING_TTL_SECONDS:
        with _pending_lock:
            _pending.pop(entry.delegation_id, None)
        return None
    return entry


def _pop_pending(delegation_id: str) -> Optional[_Pending]:
    with _pending_lock:
        return _pending.pop(str(delegation_id or ""), None)


def _has_pending_for(owner_open_id: str, borrower_profile: str) -> bool:
    now = time.time()
    with _pending_lock:
        return any(
            entry.owner_open_id == owner_open_id
            and entry.borrower_profile == borrower_profile
            and now - entry.created_at <= PENDING_TTL_SECONDS
            for entry in _pending.values()
        )


# --- cards ----------------------------------------------------------------------

def build_delegation_card(
    *,
    delegation_id: str,
    group_label: str,
) -> dict[str, Any]:
    """委托授权卡：三个 callback 按钮，值里只带 delegation_id + choice。

    身份绝不进按钮 payload——点卡人身份取 Feishu 签名的 operator。
    """
    from .feishu_auth_cards import _i18n, _plain_i18n

    body_zh = (
        f"群「{group_label}」里的 Hermes 需要用 **你本人的 GitLab 权限** 来完成你刚才发起的操作。\n\n"
        "- **允许一次**：仅本次操作借用，用完即失效\n"
        "- **允许本群**：本群后续操作免确认（可随时撤销）\n\n"
        "<font color='grey'>凭证只在单次运行的进程环境里使用，不会写入群配置或任何共享文件；每次借用都有审计记录。</font>"
    )
    body_en = (
        f"Hermes in group '{group_label}' needs YOUR GitLab access to finish the operation you started.\n\n"
        "- Allow once: this operation only\n"
        "- Allow this group: no more prompts here (revocable)\n\n"
        "<font color='grey'>The credential lives only in the run's process env; every borrow is audited.</font>"
    )

    def _button(text_zh: str, text_en: str, choice: str, btn_type: str) -> dict[str, Any]:
        value = {
            "action": ACTION_KIND,
            "choice": choice,
            "delegation_id": delegation_id,
        }
        return {
            "tag": "button",
            "text": _plain_i18n(text_zh, text_en),
            "type": btn_type,
            "size": "medium",
            "value": value,
            "behaviors": [{"type": "callback", "value": value}],
        }

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": False, "update_multi": True, "locales": ["zh_cn", "en_us"]},
        "header": {
            "title": _plain_i18n("GitLab 凭证借用请求", "GitLab credential delegation"),
            "subtitle": {"tag": "plain_text", "content": ""},
            "template": "blue",
            "padding": "12px 12px 12px 12px",
            "icon": {"tag": "standard_icon", "token": "lock-chat_filled"},
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": body_en,
                    "i18n_content": _i18n(body_zh, body_en),
                    "text_size": "normal",
                },
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "horizontal_align": "right",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [
                                _button("允许一次", "Allow once", CHOICE_ALLOW_ONCE, "primary"),
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [
                                _button("允许本群（可撤销）", "Allow this group", CHOICE_ALLOW_CHAT, "default"),
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [
                                _button("拒绝", "Deny", CHOICE_DENY, "danger"),
                            ],
                        },
                    ],
                },
            ]
        },
    }


def _result_card(*, granted: bool, scope: str = "", reason: str = "") -> dict[str, Any]:
    from .feishu_auth_cards import _status_card

    if granted:
        scope_zh = "本次操作" if scope == _leases.SCOPE_ONCE else "本群后续操作"
        return _status_card(
            title_zh="已授权",
            title_en="Delegated",
            body_zh=(
                f"已允许该群借用你的 GitLab 凭证（范围：{scope_zh}）。原操作将自动继续。\n\n"
                "<font color='grey'>每次借用都会记录审计；「允许本群」可联系管理员随时撤销。</font>"
            ),
            body_en="Delegation granted; the original operation will continue automatically.",
            template="green",
            icon_token="yes_filled",
        )
    return _status_card(
        title_zh="已拒绝",
        title_en="Denied",
        body_zh=reason or "已拒绝本次凭证借用。群里的操作不会使用你的 GitLab 权限。",
        body_en="Delegation denied. The group operation will not use your GitLab access.",
        template="yellow",
        icon_token="warning_filled",
    )


def _expired_card() -> dict[str, Any]:
    from .feishu_auth_cards import _status_card

    return _status_card(
        title_zh="授权请求已过期",
        title_en="Request expired",
        body_zh="这次凭证借用请求已过期。需要时在群里重新发起原操作即可。",
        body_en="This delegation request expired. Re-run the operation in the group to retry.",
        template="grey",
        icon_token="time_filled",
    )


def _bind_first_notice(group_label: str) -> str:
    return (
        f"群「{group_label}」里的 Hermes 想借用你的 GitLab 权限完成你发起的操作，"
        "但你还没有绑定自己的 GitLab token。请先私聊我发送 /auth，在凭证中心提交你的 "
        "GitLab token，然后回到群里重新发起操作。"
    )


# --- trigger (router side, async) -----------------------------------------------

async def handle_gitlab_delegation_required(
    *,
    gateway: Any,
    adapter: Any,
    chat_id: str,
    profile_name: str,
    event: Any,
    payload: Any = None,
) -> None:
    """Group run lacked a GitLab credential → DM the initiator a delegation card."""
    del payload
    from . import router as _m
    from .feishu_auth_cards import send_auth_card

    if adapter is None or not profile_name:
        return
    open_id = (
        _m._normalize_feishu_open_id(getattr(event, "sender_open_id", None))
        or _m._normalize_feishu_open_id(
            getattr(getattr(event, "source", None), "user_id", None)
        )
        or ""
    )
    if not _m._is_feishu_open_id(open_id):
        return

    shared_home = _resolve_shared_home()
    db_path = shared_home / "multitenancy.db"
    owner_profile = _leases.owner_profile_for_open_id(db_path, open_id)
    if not owner_profile:
        logger.info("[cred_delegation] no personal profile for initiator %s", open_id)
        return

    group_label = str(chat_id or profile_name)

    # No personal token → bind-first guidance, never a delegation card.
    if not _leases.owner_has_personal_gitlab_token(shared_home, owner_profile):
        await _m._safe_call(adapter.send, open_id, _bind_first_notice(group_label))
        return

    # Standing grant already covers this group → continue without a card.
    # CHAT scope only: a live `once` lease is a granted-but-not-yet-replayed
    # request, and a replay dispatched here would carry no delegation_id, so the
    # run could not claim it — the model would just ask for authorization again.
    if _leases.find_active_lease(
        db_path,
        owner_open_id=open_id,
        borrower_profile=profile_name,
        scope=_leases.SCOPE_CHAT,
    ):
        await _dispatch_replay(
            gateway=gateway,
            event=event,
            chat_id=chat_id,
            profile_name=profile_name,
            open_id=open_id,
            text=str(getattr(event, "text", "") or "").strip(),
        )
        return

    entry = _Pending(
        delegation_id=secrets.token_urlsafe(16),
        owner_open_id=open_id,
        owner_profile=owner_profile,
        borrower_profile=profile_name,
        group_chat_id=str(chat_id or ""),
        replay_text=str(getattr(event, "text", "") or "").strip(),
        gateway=gateway,
        event=event,
        adapter=adapter,
    )
    if not _reserve_pending(entry):
        return  # one live card per (initiator, group)
    card = build_delegation_card(
        delegation_id=entry.delegation_id, group_label=group_label
    )
    # DM via THIS gateway app's own adapter (open_id as receive target — the
    # gateway's open-id send patch handles receive_id_type). lark-cli cannot
    # enter another app's p2p, this path can.
    try:
        entry.card_state = await send_auth_card(
            adapter=adapter, chat_id=open_id, card=card
        )
    except BaseException:
        # BaseException, not Exception: an `await` cancelled here raises
        # CancelledError, and the expiry task that would eventually free the
        # slot has not been created yet — the reservation would block every
        # retry for the full pending TTL and the user would never see a card.
        _pop_pending(entry.delegation_id)
        raise
    if entry.card_state is None:
        # Release the slot — a phantom reservation would block every retry for
        # the whole pending TTL while the initiator saw no card at all.
        _pop_pending(entry.delegation_id)
        logger.warning("[cred_delegation] delegation card DM send failed for %s", open_id)
        return
    asyncio.create_task(_expire_pending_later(entry.delegation_id))


def _resolve_shared_home() -> Path:
    import os

    explicit = os.getenv("HERMES_SHARED_HOME")
    if explicit:
        return Path(explicit).expanduser()
    home = Path(os.getenv("HERMES_HOME") or "~/.hermes").expanduser()
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


async def _expire_pending_later(delegation_id: str) -> None:
    await asyncio.sleep(PENDING_TTL_SECONDS)
    entry = _pop_pending(delegation_id)
    if entry is None:
        return  # resolved in time
    from . import router as _m
    from .feishu_auth_cards import update_auth_card

    try:
        await update_auth_card(
            adapter=entry.adapter, auth_card=entry.card_state, card=_expired_card()
        )
    except Exception:
        logger.debug("[cred_delegation] expiry card update failed", exc_info=True)
    if entry.group_chat_id:
        await _m._safe_call(
            entry.adapter.send,
            entry.group_chat_id,
            "刚才的操作需要发起人授权 GitLab 凭证，但授权请求已超时。"
            "需要时请重新发起，或私聊我发送 /auth 管理凭证。",
        )


async def _dispatch_replay(
    *,
    gateway: Any,
    event: Any,
    chat_id: str,
    profile_name: str,
    open_id: str,
    text: str,
    delegation_id: str = "",
) -> None:
    from . import router as _m

    await _m._dispatch_synthetic_auth_complete(
        event=event,
        gateway=gateway,
        chat_id=chat_id,
        profile_name=profile_name,
        open_id=open_id,
        text=text or SYNTHETIC_DELEGATION_COMPLETE_TEXT,
        delegation_id=delegation_id,
    )


# --- card action (dispatcher built-in, SDK callback thread) ---------------------

def handle_delegation_card_action(adapter: Any, cb: Any) -> Any:
    """Built-in ``cred_delegation`` handler. Consumed by the dispatcher —
    a raise here becomes a generic error response, never the model path."""
    from .feishu_card_action_dispatcher import _read, _text, _toast_response

    choice = _text(cb.value.get("choice"))
    delegation_id = _text(cb.value.get("delegation_id"))
    if choice not in {CHOICE_ALLOW_ONCE, CHOICE_ALLOW_CHAT, CHOICE_DENY} or not delegation_id:
        return _toast_response("授权信息不完整。", level="info")

    entry = _peek_pending(delegation_id)
    if entry is None:
        return _updated_card(_expired_card())

    # Anti-spoof: the clicking operator comes from the Feishu-SIGNED event —
    # never the button payload — and must BE the initiator we DM'd.
    operator = _read(cb.event, "operator") or _read(cb.event, "operator_id")
    operator_open_id = _text(_read(operator, "open_id"))
    if not operator_open_id or operator_open_id != entry.owner_open_id:
        shared_home = _resolve_shared_home()
        _leases.record_denial(
            shared_home / "multitenancy.db",
            owner_open_id=entry.owner_open_id,
            borrower_profile=entry.borrower_profile,
            chat_id=entry.group_chat_id,
            detail=f"operator mismatch: {operator_open_id or '<empty>'}",
        )
        return _toast_response("只有发起人本人可以授权。", level="warning")

    # Identity verified — this click resolves the pending entry exactly once.
    if _pop_pending(delegation_id) is None:
        return _updated_card(_expired_card())

    shared_home = _resolve_shared_home()
    db_path = shared_home / "multitenancy.db"

    if choice == CHOICE_DENY:
        _leases.record_denial(
            db_path,
            owner_open_id=entry.owner_open_id,
            borrower_profile=entry.borrower_profile,
            chat_id=entry.group_chat_id,
            detail="denied by initiator",
        )
        _schedule(adapter, _notify_group_denied(entry))
        return _updated_card(_result_card(granted=False))

    scope = _leases.SCOPE_ONCE if choice == CHOICE_ALLOW_ONCE else _leases.SCOPE_CHAT
    # 允许一次 == this request's run only: the lease carries the delegation id and
    # the replay run is the only run that presents it.
    once_delegation_id = delegation_id if scope == _leases.SCOPE_ONCE else ""
    _leases.create_lease(
        db_path,
        owner_profile=entry.owner_profile,
        owner_open_id=entry.owner_open_id,
        borrower_profile=entry.borrower_profile,
        scope=scope,
        chat_id=entry.group_chat_id,
        delegation_id=once_delegation_id,
    )
    _schedule(
        adapter,
        _dispatch_replay(
            gateway=entry.gateway,
            event=entry.event,
            chat_id=entry.group_chat_id,
            profile_name=entry.borrower_profile,
            open_id=entry.owner_open_id,
            text=entry.replay_text,
            delegation_id=once_delegation_id,
        ),
    )
    return _updated_card(_result_card(granted=True, scope=scope))


async def _notify_group_denied(entry: _Pending) -> None:
    from . import router as _m

    if entry.group_chat_id:
        await _m._safe_call(
            entry.adapter.send,
            entry.group_chat_id,
            "发起人未授权借用 GitLab 凭证，本次操作已终止。"
            "如需继续，可私聊我处理，或联系管理员为本群配置凭证。",
        )


def _schedule(adapter: Any, coro: Any) -> None:
    """Run a coroutine on the adapter loop from the SDK callback thread."""
    loop = getattr(adapter, "_loop", None)
    if loop is not None and not loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
            return
        except Exception:
            logger.debug("[cred_delegation] loop schedule failed", exc_info=True)
    # No usable loop (tests / degraded adapter): drop with a log, never raise.
    try:
        coro.close()
    except Exception:
        pass
    logger.warning("[cred_delegation] no adapter loop; async follow-up dropped")


def _updated_card(card: dict[str, Any]) -> Any:
    from .feishu_auth_hub_actions import _updated_card_response

    return _updated_card_response(card)
