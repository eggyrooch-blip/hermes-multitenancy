"""Fail-closed admission for Agent-issued Feishu ingress tickets."""
from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .feishu_adapter_compat import load_feishu_module, load_live_feishu_module

_ADMISSION_SEAL = object()
_SEEN_TTL_SECONDS = 15 * 60
_SEEN_MAX = 4096
_TICKET_MAX_AGE_SECONDS = 300
_TICKET_CLOCK_SKEW_SECONDS = 30
_seen: "OrderedDict[tuple[str, str], float]" = OrderedDict()
_seen_lock = threading.Lock()


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


def admit_trusted_feishu_ingress(*, ticket: Any, adapter: Any) -> TrustedFeishuAdmission | None:
    """Bind an authentic ticket to exactly one active route and tool identity."""
    module = load_feishu_module()
    ticket_type = getattr(module, "TrustedFeishuIngressTicket", None)
    account_id = str(getattr(adapter, "_app_id", "") or "")
    if ticket_type is None or type(ticket) is not ticket_type:
        return None
    if not ticket.is_valid(account_id=account_id) or not _ticket_is_fresh(ticket):
        return None
    if ticket.principal_kind != "human" or ticket.event_kind in {"comment", "vc"}:
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
