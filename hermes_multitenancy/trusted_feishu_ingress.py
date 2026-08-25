"""Fail-closed admission for Agent-issued Feishu ingress tickets."""
from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .feishu_adapter_compat import load_feishu_module, load_live_feishu_module

logger = logging.getLogger(__name__)

_ADMISSION_SEAL = object()
_SEEN_TTL_SECONDS = 15 * 60
_SEEN_MAX = 4096
_TICKET_MAX_AGE_SECONDS = 300
_TICKET_CLOCK_SKEW_SECONDS = 30
# ponytail: fixed per-chat floor for bot-triggered runs; make it group-level
# config only if a real chat needs a different cadence.
_BOT_MIN_INTERVAL_SECONDS = 30.0
_BOT_ADMIT_TTL_SECONDS = 15 * 60
_BOT_ADMIT_MAX = 1024
_seen: "OrderedDict[tuple[str, str], float]" = OrderedDict()
_seen_lock = threading.Lock()
_bot_last_admit: "OrderedDict[str, float]" = OrderedDict()


@dataclass(frozen=True, slots=True)
class TrustedFeishuAdmission:
    profile_name: str
    route_version: int
    actor_id: str = field(repr=False)
    actor_id_type: str
    actor_subject: str = field(repr=False)
    chat_type: str
    chat_id: str = field(repr=False)
    message_id: str = field(repr=False)
    credential_subject: str = field(repr=False)
    tool_scope: str
    ticket_fingerprint: str
    # "user" = employee routing row resolved the actor (the original contract);
    # "bot" = controlled peer-bot path bound to the chat's group profile.
    actor_kind: str = "user"
    _seal: object = field(repr=False, compare=False, default=_ADMISSION_SEAL)

    def is_authentic(self) -> bool:
        return self._seal is _ADMISSION_SEAL


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _ticket_fingerprint(ticket: Any) -> str:
    return _fingerprint(f"{ticket.namespace}\x1f{ticket.event_key}\x1f{ticket.signature}")


def _ticket_is_fresh(ticket: Any, *, now: float | None = None) -> bool:
    checked_at = float(time.time() if now is None else now)
    try:
        issued_at = float(ticket.issued_at)
        expires_at = float(ticket.expires_at)
    except (AttributeError, TypeError, ValueError):
        return False
    lifetime = expires_at - issued_at
    return bool(
        all(math.isfinite(value) for value in (checked_at, issued_at, expires_at, lifetime))
        and issued_at <= checked_at + _TICKET_CLOCK_SKEW_SECONDS
        and checked_at - issued_at <= _TICKET_MAX_AGE_SECONDS
        and 0 < lifetime <= _TICKET_MAX_AGE_SECONDS
        and expires_at > checked_at
    )


def _claim_once(ticket: Any) -> bool:
    now = time.time()
    key = (ticket.namespace, ticket.event_key)
    with _seen_lock:
        while _seen:
            oldest_key, oldest_at = next(iter(_seen.items()))
            if now - oldest_at <= _SEEN_TTL_SECONDS and len(_seen) < _SEEN_MAX:
                break
            _seen.pop(oldest_key, None)
        if key in _seen:
            return False
        _seen[key] = now
    return True


def _resolve_ticket_context(
    table: Any, ticket: Any
) -> tuple[Any, str, str] | tuple[None, None, None]:
    if ticket.actor_id_type == "open_id":
        actor_row = table.lookup_by_open_id(ticket.actor_id)
    elif ticket.actor_id_type == "union_id":
        actor_row = table.lookup_by_union_id(ticket.actor_id)
    elif ticket.actor_id_type == "user_id":
        actor_row = table.lookup_by_user_id(ticket.actor_id)
    else:
        actor_row = None
    if actor_row is None or actor_row.kind != "user" or not actor_row.open_id:
        return None, None, None

    actor_context = table.resolve_context(
        ticket.actor_id,
        alt_id=ticket.actor_id,
    )
    if actor_context is None or actor_context.profile_name != actor_row.profile_name:
        return None, None, None
    if ticket.actor_id_type == "open_id" and actor_row.open_id != ticket.actor_id:
        return None, None, None
    if ticket.actor_id_type == "union_id" and actor_row.union_id != ticket.actor_id:
        return None, None, None
    if ticket.actor_id_type == "user_id":
        if actor_row.user_id != ticket.actor_id:
            return None, None, None

    group_row = table.lookup_by_chat_id(ticket.chat_id) if ticket.chat_id else None
    if group_row is not None:
        context = table.resolve_context(
            ticket.actor_id,
            alt_id=ticket.actor_id,
            chat_id=ticket.chat_id,
        )
        if context is None or not context.is_group or context.profile_name != group_row.profile_name:
            return None, None, None
        return context, ticket.account_id, str(actor_row.open_id)
    return actor_context, str(actor_row.open_id), str(actor_row.open_id)


def _bot_actor_subject(ticket: Any) -> str:
    # Feishu may omit sender ids on bot-sent messages; the sentinel keeps the
    # subject clearly non-employee either way (never a bare ou_/on_ id shape).
    actor = str(getattr(ticket, "actor_id", "") or "").strip()
    return f"bot:{actor}" if actor else "bot:unknown"


def _bot_throttle_claim(chat_id: str, now: float) -> bool:
    """Claim the per-chat bot slot; TTL/size-bounded like ``_seen``."""
    with _seen_lock:
        while _bot_last_admit:
            oldest_chat, oldest_at = next(iter(_bot_last_admit.items()))
            if now - oldest_at <= _BOT_ADMIT_TTL_SECONDS and len(_bot_last_admit) < _BOT_ADMIT_MAX:
                break
            _bot_last_admit.pop(oldest_chat, None)
        last = _bot_last_admit.get(chat_id)
        if last is not None and now - last < _BOT_MIN_INTERVAL_SECONDS:
            return False
        _bot_last_admit[chat_id] = now
        _bot_last_admit.move_to_end(chat_id)
    return True


def _admit_bot_ticket(
    *, ticket: Any, account_id: str, self_ids: frozenset = frozenset()
) -> TrustedFeishuAdmission | None:
    """Controlled peer-bot path: group chats with a routed group profile only.

    A bot sender never resolves to an employee routing row, so the admission
    binds to the CHAT's group profile instead. The credential subject stays on
    the app identity — identical to the human group path — so a bot-triggered
    run can never inherit an employee's credential scope. DMs and unrouted
    chats stay fail-closed, and a per-chat floor keeps a chatty alert bot from
    turning into a run storm. Every deny leaves a reasoned log line — the
    silent-drop era of bot messages is what this path exists to end.
    """
    chat_id = str(ticket.chat_id or "")
    if ticket.event_kind != "message":
        logger.warning(
            "[trusted-ingress] bot actor denied reason=event_kind:%s chat=%s",
            ticket.event_kind,
            chat_id,
        )
        return None
    # Our own outbound replies must never re-enter as a bot actor: the core
    # adapter's self-echo drop runs AFTER this admission, so admitting them
    # here would burn the per-chat floor (starving real alerts) even though
    # the event dies downstream.
    actor = str(ticket.actor_id or "").strip()
    if actor and self_ids and actor in self_ids:
        logger.warning(
            "[trusted-ingress] bot actor denied reason=self_echo chat=%s", chat_id
        )
        return None

    from .router import _get_routing_table, _profile_name_to_home

    table = _get_routing_table()
    if table is None:
        logger.warning(
            "[trusted-ingress] bot actor denied reason=no_routing_table chat=%s", chat_id
        )
        return None
    row = table.lookup_by_chat_id(chat_id) if chat_id else None
    if row is None or not row.active or row.kind != "group":
        logger.warning(
            "[trusted-ingress] bot actor denied reason=no_group_route chat=%s", chat_id
        )
        return None
    profile_home = _profile_name_to_home(row.profile_name)
    if not profile_home.is_dir():
        logger.warning(
            "[trusted-ingress] bot actor denied reason=missing_profile_home chat=%s profile=%s",
            chat_id,
            row.profile_name,
        )
        return None
    if not _bot_throttle_claim(chat_id, time.time()):
        logger.warning(
            "[trusted-ingress] bot actor denied reason=throttled chat=%s floor=%ss",
            chat_id,
            int(_BOT_MIN_INTERVAL_SECONDS),
        )
        return None
    if not _claim_once(ticket):
        logger.warning(
            "[trusted-ingress] bot actor denied reason=claim_once_duplicate chat=%s", chat_id
        )
        return None
    logger.info(
        "[trusted-ingress] bot actor admitted chat=%s profile=%s actor=%s",
        chat_id,
        row.profile_name,
        ticket.actor_id or "<no-id>",
    )
    return TrustedFeishuAdmission(
        profile_name=row.profile_name,
        route_version=int(row.version),
        actor_id=ticket.actor_id,
        actor_id_type=ticket.actor_id_type,
        actor_subject=_bot_actor_subject(ticket),
        chat_type="group",
        chat_id=chat_id,
        message_id=ticket.message_id,
        credential_subject=account_id,
        tool_scope="feishu:bot",
        ticket_fingerprint=_ticket_fingerprint(ticket),
        actor_kind="bot",
    )


def admit_trusted_feishu_ingress(*, ticket: Any, adapter: Any) -> TrustedFeishuAdmission | None:
    """Bind an authentic ticket to exactly one active route and tool identity."""
    module = load_feishu_module()
    ticket_type = getattr(module, "TrustedFeishuIngressTicket", None)
    account_id = str(getattr(adapter, "_app_id", "") or "")
    if ticket_type is None or type(ticket) is not ticket_type:
        return None
    if not ticket.is_valid(account_id=account_id) or not _ticket_is_fresh(ticket):
        return None
    if ticket.event_kind in {"comment", "vc"}:
        return None
    if ticket.principal_kind == "bot":
        self_ids = frozenset(
            str(value)
            for value in (
                getattr(adapter, "_bot_open_id", None),
                getattr(adapter, "_bot_user_id", None),
                account_id,
            )
            if value
        )
        return _admit_bot_ticket(ticket=ticket, account_id=account_id, self_ids=self_ids)
    if ticket.principal_kind != "human":
        return None

    from .router import _get_routing_table, _profile_name_to_home

    table = _get_routing_table()
    if table is None:
        return None
    context, credential_subject, actor_subject = _resolve_ticket_context(table, ticket)
    if context is None:
        return None
    profile_home = _profile_name_to_home(context.profile_name)
    if not profile_home.is_dir():
        return None
    if not _claim_once(ticket):
        return None

    return TrustedFeishuAdmission(
        profile_name=context.profile_name,
        route_version=context.route_version,
        actor_id=ticket.actor_id,
        actor_id_type=ticket.actor_id_type,
        actor_subject=actor_subject,
        chat_type="group" if context.is_group else "p2p",
        chat_id=ticket.chat_id,
        message_id=ticket.message_id,
        credential_subject=credential_subject,
        tool_scope="feishu:bot" if context.is_group else "feishu:user",
        ticket_fingerprint=_ticket_fingerprint(ticket),
    )


def _validate_bot_admission(
    admission: "TrustedFeishuAdmission", ticket: Any, event: Any, source: Any
) -> bool:
    """Recheck a bot-actor admission right before MT schedules model/tool work.

    Mirrors the user path minus the employee-row resolution: the binding is
    chat → group profile. The actor-candidate cross-check is intentionally
    skipped — bot senders may carry no ids on the MT event source — while the
    chat_id + message_id + ticket fingerprint equality still pins the sealed
    admission to this exact message.
    """
    if ticket.principal_kind != "bot" or ticket.event_kind != "message":
        return False
    source_chat_id = str(getattr(source, "chat_id", "") or "")
    source_message_id = str(
        getattr(event, "message_id", None)
        or getattr(source, "message_id", None)
        or ""
    )
    if (
        source_chat_id != ticket.chat_id
        or source_message_id != ticket.message_id
        or admission.actor_id != ticket.actor_id
        or admission.actor_id_type != ticket.actor_id_type
        or admission.chat_id != ticket.chat_id
        or admission.message_id != ticket.message_id
    ):
        return False
    if admission.ticket_fingerprint != _ticket_fingerprint(ticket):
        return False
    chat_type = str(getattr(source, "chat_type", "") or "").strip().lower()
    if chat_type not in {"group", "topic", "group_chat"}:
        return False

    from .router import _get_routing_table

    table = _get_routing_table()
    row = (
        table.lookup_by_chat_id(str(ticket.chat_id))
        if table is not None and ticket.chat_id
        else None
    )
    return bool(
        row is not None
        and row.active
        and row.kind == "group"
        and admission.chat_type == "group"
        and admission.profile_name == row.profile_name
        and admission.route_version == int(row.version)
        and admission.actor_subject == _bot_actor_subject(ticket)
        and admission.credential_subject == ticket.account_id
        and admission.tool_scope == "feishu:bot"
    )


def validate_admitted_feishu_event(event: Any, gateway: Any = None) -> bool:
    """Recheck the route immediately before MT schedules model/tool work."""
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None))
    if platform != "feishu":
        return True
    ticket = getattr(event, "trusted_feishu_ingress_ticket", None)
    admission = getattr(event, "trusted_feishu_ingress_admission", None)
    if not isinstance(admission, TrustedFeishuAdmission) or not admission.is_authentic():
        return False

    module = load_feishu_module()
    ticket_type = getattr(module, "TrustedFeishuIngressTicket", None)
    if ticket_type is None or type(ticket) is not ticket_type:
        return False
    expected_account = ticket.account_id
    if gateway is not None:
        from .router import _get_feishu_adapter

        adapter = _get_feishu_adapter(gateway)
        if adapter is None:
            return False
        expected_account = str(getattr(adapter, "_app_id", "") or "")
    if not ticket.is_valid(account_id=expected_account) or not _ticket_is_fresh(ticket):
        return False
    if getattr(admission, "actor_kind", "user") == "bot":
        return _validate_bot_admission(admission, ticket, event, source)
    source_actor_candidates = {
        str(value)
        for value in (
            getattr(event, "sender_open_id", None),
            getattr(source, "open_id", None),
            getattr(source, "user_id", None),
            getattr(source, "user_id_alt", None),
        )
        if value
    }
    source_chat_id = str(getattr(source, "chat_id", "") or "")
    source_message_id = str(
        getattr(event, "message_id", None)
        or getattr(source, "message_id", None)
        or ""
    )
    if (
        ticket.actor_id not in source_actor_candidates
        or source_chat_id != ticket.chat_id
        or source_message_id != ticket.message_id
        or admission.actor_id != ticket.actor_id
        or admission.actor_id_type != ticket.actor_id_type
        or admission.chat_id != ticket.chat_id
        or admission.message_id != ticket.message_id
    ):
        return False
    if admission.ticket_fingerprint != _ticket_fingerprint(ticket):
        return False

    from .router import _get_routing_table

    table = _get_routing_table()
    context, expected_subject, actor_subject = (
        _resolve_ticket_context(table, ticket) if table else (None, None, None)
    )
    chat_type = str(getattr(source, "chat_type", "") or "").strip().lower()
    event_is_group = chat_type in {"group", "topic", "group_chat"}
    event_is_direct = chat_type in {"dm", "p2p", "private"}
    expected_scope = "feishu:bot" if context and context.is_group else "feishu:user"
    return bool(
        context
        and (event_is_group or event_is_direct)
        and event_is_group == context.is_group
        and admission.chat_type == ("group" if context.is_group else "p2p")
        and context.profile_name == admission.profile_name
        and context.route_version == admission.route_version
        and admission.actor_subject == actor_subject
        and admission.credential_subject == expected_subject
        and admission.tool_scope == expected_scope
    )


def install_trusted_feishu_ingress_admission() -> None:
    module = load_live_feishu_module()
    adapter = getattr(module, "FeishuAdapter")
    if not hasattr(adapter, "_trusted_ingress_admitter"):
        raise RuntimeError("Feishu core lacks trusted ingress contract")
    adapter._trusted_ingress_admitter = staticmethod(admit_trusted_feishu_ingress)
    live_module = load_live_feishu_module()
    if (
        getattr(live_module, "FeishuAdapter", None) is not adapter
        or getattr(adapter, "_trusted_ingress_admitter", None) is not admit_trusted_feishu_ingress
    ):
        raise RuntimeError("trusted ingress was not installed on the live Feishu adapter")


def _reset_seen_for_tests() -> None:
    with _seen_lock:
        _seen.clear()
        _bot_last_admit.clear()
