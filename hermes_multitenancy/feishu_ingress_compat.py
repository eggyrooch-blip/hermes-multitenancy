"""MT-owned trusted ingress for the stock Feishu adapter.

The ticket format preserves the existing fork contract. Wrappers sit on the
shared SDK/webhook handlers; credentials and route checks remain in MT.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import secrets
import threading
import time
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import wraps
from typing import Any, Optional

logger = logging.getLogger(__name__)
_FEISHU_TRUSTED_INGRESS_KEY = secrets.token_bytes(32)
_FEISHU_TRUSTED_INGRESS_TTL_SECONDS = 300
_transport = ContextVar("mt_feishu_ingress_transport", default="websocket")
# The admitted-callback wrapper keeps its inner callback in a mutable attribute
# so a later installer (the card-action dispatcher, including its deferred and
# re-arm paths) can slot in BENEATH ingress instead of wrapping outside it —
# outside, it would see raw, ticketless callbacks.
INGRESS_CALLBACK_FLAG = "_mt_trusted_ingress_callback"
INGRESS_INNER_ATTR = "_mt_trusted_inner"
_prompt_ingress = ContextVar("mt_feishu_prompt_ingress", default=None)
# Owner sealed when the originating message is admitted. Unlike the ticket it
# does not expire after 300s, so a long turn still gets an owner-bound card.
_prompt_owner = ContextVar("mt_feishu_prompt_owner", default=None)
# approval request_id -> owner fingerprint, for text /approve and /deny.
_approval_owners: "OrderedDict[str, str]" = OrderedDict()
_APPROVAL_OWNERS_MAX = 4096
_approval_owners_lock = threading.Lock()
_PROMPT_OWNER_UNAVAILABLE = "egress declined: trusted Feishu prompt owner unavailable"
_APPROVAL_OWNER_UNAVAILABLE_NOTICE = (
    "⌛ 这次命令审批已过期或无法确认发起人，命令没有执行。请重新发起。"
)


def _feishu_value(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _feishu_namespace(account_id: str) -> str:
    return "feishu:" + hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class TrustedFeishuIngressTicket:
    """Process-local proof that a Feishu callback crossed the adapter edge."""

    version: int
    transport: str
    event_kind: str
    event_type: str
    event_key: str
    account_id: str
    namespace: str
    actor_id: str
    actor_id_type: str
    principal_kind: str
    chat_id: str
    thread_id: str
    message_id: str
    issued_at: float
    expires_at: float
    signature: str = field(repr=False)

    def _signed_fields(self) -> tuple[Any, ...]:
        return (
            self.version,
            self.transport,
            self.event_kind,
            self.event_type,
            self.event_key,
            self.account_id,
            self.namespace,
            self.actor_id,
            self.actor_id_type,
            self.principal_kind,
            self.chat_id,
            self.thread_id,
            self.message_id,
            self.issued_at,
            self.expires_at,
        )

    @classmethod
    def issue(cls, **fields: Any) -> "TrustedFeishuIngressTicket":
        issued_at = float(fields.pop("issued_at", time.time()))
        unsigned = cls(
            version=1,
            issued_at=issued_at,
            expires_at=float(
                fields.pop(
                    "expires_at",
                    issued_at + _FEISHU_TRUSTED_INGRESS_TTL_SECONDS,
                )
            ),
            signature="",
            **fields,
        )
        signature = hmac.new(
            _FEISHU_TRUSTED_INGRESS_KEY,
            json.dumps(unsigned._signed_fields(), separators=(",", ":")).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return replace(unsigned, signature=signature)

    def is_valid(self, *, account_id: str, now: Optional[float] = None) -> bool:
        checked_at = float(now if now is not None else time.time())
        lifetime = self.expires_at - self.issued_at
        if (
            self.version != 1
            or self.account_id != account_id
            or self.namespace != _feishu_namespace(account_id)
            or self.transport not in {"websocket", "webhook"}
            or self.event_kind
            not in {"message", "reaction", "button", "form", "comment", "vc"}
            or self.actor_id_type not in {"open_id", "union_id", "user_id"}
            or self.principal_kind not in {"human", "bot", "system"}
            or not self.event_key
            or not self.actor_id
            or (
                self.event_kind in {"message", "reaction", "button", "form"}
                and not self.chat_id
            )
            or not all(
                math.isfinite(value)
                for value in (checked_at, self.issued_at, self.expires_at, lifetime)
            )
            or self.issued_at > checked_at + 30
            or lifetime <= 0
            or lifetime > _FEISHU_TRUSTED_INGRESS_TTL_SECONDS
            or self.expires_at <= checked_at
        ):
            return False
        expected = hmac.new(
            _FEISHU_TRUSTED_INGRESS_KEY,
            json.dumps(self._signed_fields(), separators=(",", ":")).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(self.signature, expected)


@dataclass(frozen=True, slots=True)
class _TrustedFeishuEnvelope:
    raw: Any
    trusted_feishu_ingress_ticket: TrustedFeishuIngressTicket
    trusted_feishu_ingress_admission: Any = field(repr=False, default=None)

    def __getattr__(self, key: str) -> Any:
        return _feishu_value(self.raw, key)


class _TicketMethods:
    @staticmethod
    def _trusted_ingress_kind(event_type: str, data: Any) -> str:
        if event_type == "im.message.receive_v1":
            return "message"
        if event_type.startswith("im.message.reaction."):
            return "reaction"
        if event_type == "card.action.trigger":
            action = _feishu_value(_feishu_value(data, "event"), "action")
            return "form" if _feishu_value(action, "form_value") is not None else "button"
        if event_type == "drive.notice.comment_add_v1":
            return "comment"
        return "vc"

    def _issue_trusted_ingress_ticket(
        self,
        event_type: str,
        data: Any,
        *,
        transport: str,
        trusted_chat_id: str = "",
    ) -> Optional[TrustedFeishuIngressTicket]:
        event = _feishu_value(data, "event")
        header = _feishu_value(data, "header")
        kind = self._trusted_ingress_kind(event_type, data)
        message = _feishu_value(event, "message")
        context = _feishu_value(event, "context")
        action = _feishu_value(event, "action")
        sender = _feishu_value(event, "sender")
        sender_id = _feishu_value(sender, "sender_id")
        user_id = _feishu_value(event, "user_id")
        operator = _feishu_value(event, "operator")

        actor_ids = (
            (
                "open_id",
                _feishu_value(sender_id, "open_id")
                or _feishu_value(user_id, "open_id")
                or _feishu_value(operator, "open_id"),
            ),
            (
                "union_id",
                _feishu_value(sender_id, "union_id")
                or _feishu_value(user_id, "union_id")
                or _feishu_value(operator, "union_id"),
            ),
            (
                "user_id",
                _feishu_value(sender_id, "user_id")
                or _feishu_value(user_id, "user_id")
                or _feishu_value(operator, "user_id"),
            ),
        )
        actor_id_type, actor_id = next(
            ((id_type, str(value)) for id_type, value in actor_ids if value),
            ("open_id", ""),
        )
        chat_id = str(
            trusted_chat_id
            or _feishu_value(message, "chat_id")
            or _feishu_value(context, "open_chat_id")
            or _feishu_value(event, "chat_id")
            or ""
        )
        thread_id = str(
            _feishu_value(message, "thread_id")
            or _feishu_value(message, "root_id")
            or _feishu_value(context, "open_thread_id")
            or ""
        )
        message_id = str(
            _feishu_value(message, "message_id")
            or _feishu_value(event, "message_id")
            or _feishu_value(context, "open_message_id")
            or ""
        )
        event_id = str(
            _feishu_value(header, "event_id")
            or _feishu_value(event, "event_id")
            or _feishu_value(event, "token")
            or ""
        )

        if kind == "comment":
            meta = _feishu_value(event, "notice_meta", {}) or {}
            from_user = _feishu_value(meta, "from_user_id", {}) or {}
            actor_id = str(_feishu_value(from_user, "open_id") or actor_id)
            actor_id_type = "open_id"
            message_id = str(
                _feishu_value(event, "reply_id")
                or _feishu_value(event, "comment_id")
                or message_id
            )
        if not event_id and message_id:
            action_name = str(
                _feishu_value(action, "name") or _feishu_value(action, "tag") or ""
            )
            event_id = hashlib.sha256(
                "\x1f".join((event_type, message_id, actor_id, action_name)).encode("utf-8")
            ).hexdigest()

        sender_type = str(
            _feishu_value(sender, "sender_type")
            or _feishu_value(event, "operator_type")
            or "user"
        ).lower()
        principal_kind = "bot" if sender_type in {"bot", "app"} else "human"
        account_id = str(self._app_id or "")
        if not account_id or not actor_id or not event_id:
            return None
        return TrustedFeishuIngressTicket.issue(
            transport=transport,
            event_kind=kind,
            event_type=event_type,
            event_key=event_id,
            account_id=account_id,
            namespace=_feishu_namespace(account_id),
            actor_id=actor_id,
            actor_id_type=actor_id_type,
            principal_kind=principal_kind,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
        )

    def _admit_trusted_ingress_ticket(self, ticket: Any) -> Any:
        admitter = getattr(type(self), "_trusted_ingress_admitter", None)
        if not (
            callable(admitter)
            and ticket
            and ticket.is_valid(account_id=str(self._app_id or ""))
        ):
            return None
        try:
            return admitter(ticket=ticket, adapter=self)
        except Exception:
            logger.error("[Feishu] trusted ingress admission failed", exc_info=True)
            return None


@dataclass(frozen=True, slots=True)
class _PendingReaction:
    raw: Any
    event_type: str
    transport: str

    def __getattr__(self, key):
        return _feishu_value(self.raw, key)


def _envelope_allowed(adapter, data):
    from types import SimpleNamespace
    from .trusted_feishu_ingress import validate_admitted_feishu_event

    if type(data) is not _TrustedFeishuEnvelope:
        return False
    ticket = data.trusted_feishu_ingress_ticket
    admission = data.trusted_feishu_ingress_admission
    if not ticket.is_valid(account_id=str(adapter._app_id or "")):
        return False
    event = SimpleNamespace(
        source=SimpleNamespace(platform="feishu", user_id=ticket.actor_id,
                               chat_id=ticket.chat_id, chat_type=admission.chat_type),
        message_id=ticket.message_id,
        trusted_feishu_ingress_ticket=ticket,
        trusted_feishu_ingress_admission=admission,
    )
    return validate_admitted_feishu_event(event, adapter=adapter)


def _envelope_reject_reason(adapter, data) -> str:
    from .trusted_feishu_ingress import TrustedFeishuAdmission

    ticket = data.trusted_feishu_ingress_ticket
    admission = data.trusted_feishu_ingress_admission
    try:
        if not ticket.is_valid(account_id=str(adapter._app_id or "")):
            return "ticket_invalid"
    except Exception:
        return "ticket_invalid"
    if not isinstance(admission, TrustedFeishuAdmission) or not admission.is_authentic():
        return "admission_not_from_this_copy"
    return "route_recheck_failed"


def _prompt_binding(adapter, data, chat_id):
    if not _envelope_allowed(adapter, data):
        return None
    ticket = data.trusted_feishu_ingress_ticket
    admission = data.trusted_feishu_ingress_admission
    if admission.actor_kind != "user" or not chat_id or ticket.chat_id != chat_id:
        return None
    return {
        "actor_id": ticket.actor_id,
        "actor_id_type": ticket.actor_id_type,
        "actor_subject": admission.actor_subject,
        "profile_name": admission.profile_name,
        "route_version": admission.route_version,
    }


@dataclass(frozen=True, slots=True)
class _PromptOwner:
    binding: dict
    ticket: TrustedFeishuIngressTicket = field(repr=False)


def _seal_prompt_owner(adapter, data) -> Optional[_PromptOwner]:
    ticket = getattr(data, "trusted_feishu_ingress_ticket", None)
    binding = _prompt_binding(adapter, data, getattr(ticket, "chat_id", ""))
    return _PromptOwner(binding, ticket) if binding else None


def _owner_fingerprint(profile_name: Any, actor_subject: Any) -> str:
    return hashlib.sha256(f"{profile_name}\x1f{actor_subject}".encode("utf-8")).hexdigest()[:32]


def _sealed_owner_binding(chat_id) -> Optional[dict]:
    """The admitted owner's binding, re-checked against the live route only."""
    from .router import _get_routing_table
    from .trusted_feishu_ingress import _resolve_ticket_context

    owner = _prompt_owner.get()
    if owner is None or not chat_id or owner.ticket.chat_id != chat_id:
        return None
    table = _get_routing_table()
    context, _subject, actor_subject = (
        _resolve_ticket_context(table, owner.ticket) if table else (None, None, None)
    )
    if (
        context is None
        or context.profile_name != owner.binding["profile_name"]
        or context.route_version != owner.binding["route_version"]
        or actor_subject != owner.binding["actor_subject"]
    ):
        return None
    return dict(owner.binding)


def _record_approval_owner(approval_data: Any) -> None:
    owner = _prompt_owner.get()
    request_id = str(_feishu_value(approval_data, "request_id") or "")
    if owner is None or not request_id:
        return
    fingerprint = _owner_fingerprint(owner.binding["profile_name"], owner.binding["actor_subject"])
    with _approval_owners_lock:
        _approval_owners[request_id] = fingerprint
        _approval_owners.move_to_end(request_id)
        while len(_approval_owners) > _APPROVAL_OWNERS_MAX:
            _approval_owners.popitem(last=False)


def text_approval_allowed(event: Any, pending: list, *, resolve_all: bool) -> bool:
    """Text /approve and /deny only act on approvals the sender requested."""
    from .trusted_feishu_ingress import TrustedFeishuAdmission

    admission = getattr(event, "trusted_feishu_ingress_admission", None)
    if (
        not isinstance(admission, TrustedFeishuAdmission)
        or not admission.is_authentic()
        or admission.actor_kind != "user"
    ):
        return False
    sender = _owner_fingerprint(admission.profile_name, admission.actor_subject)
    targets = pending if resolve_all else pending[:1]
    with _approval_owners_lock:
        owners = [_approval_owners.get(str(item.get("request_id") or "")) for item in targets]
    return bool(owners) and all(owner == sender for owner in owners)


def _install_approval_owner_capture() -> None:
    import tools.approval as approval

    original = approval.register_gateway_notify
    if getattr(original, "_hermes_multitenancy_owner_capture", False):
        return

    @wraps(original)
    def register(session_key, cb):
        @wraps(cb)
        def notify(approval_data):
            # Runs in the agent thread, inside the originating message's context.
            _record_approval_owner(approval_data)
            return cb(approval_data)

        return original(session_key, notify)

    register._hermes_multitenancy_owner_capture = True
    approval.register_gateway_notify = register


def _bound_prompt_click(adapter, state, event):
    data = _prompt_ingress.get()
    binding = _prompt_binding(adapter, data, state.get("chat_id"))
    if not binding or any(state.get(key) != value for key, value in binding.items()):
        return False
    ticket = data.trusted_feishu_ingress_ticket
    context = _feishu_value(event, "context")
    operator = _feishu_value(event, "operator")
    return bool(
        ticket.event_kind in {"button", "form"}
        and _feishu_value(operator, ticket.actor_id_type) == ticket.actor_id
        and _feishu_value(context, "open_chat_id") == ticket.chat_id
        and _feishu_value(context, "open_message_id") == ticket.message_id
        and ticket.message_id == state.get("message_id")
    )


def _bind_prompt_identity(adapter):
    original_send = adapter._send_interactive_card

    @wraps(original_send)
    async def send(self, chat_id, *args, **kwargs):
        from gateway.platforms.base import SendResult

        binding = _sealed_owner_binding(chat_id) or _prompt_binding(self, _prompt_ingress.get(), chat_id)
        if binding is None:
            if kwargs.get("state_map") is not getattr(self, "_approval_state", None):
                return SendResult(success=False, error="Trusted Feishu prompt identity unavailable")
            # Declined, not failed: a failure makes core re-send the prompt as
            # text that anyone in a shared thread could answer.
            try:
                metadata = args[1] if len(args) > 1 else None
                await self.send(chat_id, _APPROVAL_OWNER_UNAVAILABLE_NOTICE, metadata=metadata)
            except Exception:
                logger.warning("[trusted-ingress] approval owner notice failed", exc_info=True)
            return SendResult(
                success=False,
                error=_PROMPT_OWNER_UNAVAILABLE,
                raw_response={"success": False, "code": "egress_declined",
                              "error": _PROMPT_OWNER_UNAVAILABLE},
            )
        result = await original_send(self, chat_id, *args, **kwargs)
        if result.success:
            kwargs["state_map"][kwargs["state_id"]].update(binding)
        return result

    adapter._send_interactive_card = send
    original_validate = adapter._validate_card_action

    @wraps(original_validate)
    def validate(self, *, event, state, **kwargs):
        if not _bound_prompt_click(self, state, event):
            logger.warning("[trusted-ingress] prompt click denied reason=owner_mismatch")
            return None
        return original_validate(self, event=event, state=state, **kwargs)

    adapter._validate_card_action = validate
    original_pop = adapter._pop_validated_prompt_state

    @wraps(original_pop)
    def pop(self, *, states, ident, **kwargs):
        state = states.get(ident)
        data = _prompt_ingress.get()
        if not state or not _bound_prompt_click(self, state, _feishu_value(data, "event")):
            logger.warning("[trusted-ingress] prompt resolution denied reason=owner_mismatch")
            return None
        return original_pop(self, states=states, ident=ident, **kwargs)

    adapter._pop_validated_prompt_state = pop


def install_stock_feishu_ingress(module):
    adapter = module.FeishuAdapter
    required = ("_on_message_event", "_on_card_action_trigger", "_on_reaction_event",
                "_on_drive_comment_event", "_on_meeting_invited_event",
                "_handle_webhook_request", "_handle_message_with_guards",
                "_dispatch_inbound_event", "_dispatch_synthetic_event",
                "_send_interactive_card", "_validate_card_action", "_pop_validated_prompt_state")
    if any(not callable(getattr(adapter, name, None)) for name in required):
        raise RuntimeError("MT cannot secure this Feishu adapter ingress; startup denied")
    module.TrustedFeishuIngressTicket = TrustedFeishuIngressTicket
    module._feishu_namespace = _feishu_namespace
    for name in ("_trusted_ingress_kind", "_issue_trusted_ingress_ticket", "_admit_trusted_ingress_ticket"):
        setattr(adapter, name, _TicketMethods.__dict__[name])
    adapter._trusted_ingress_admitter = None
    _bind_prompt_identity(adapter)
    _install_approval_owner_capture()

    def dispatch(self, event_type, data, *, transport):
        kind = self._trusted_ingress_kind(event_type, data)
        if kind in {"comment", "vc"}:
            logger.warning("[trusted-ingress] denied unsupported event bridge kind=%s", kind)
            return None
        if kind == "reaction":
            return self._on_reaction_event(event_type, _PendingReaction(data, event_type, transport))
        ticket = self._issue_trusted_ingress_ticket(event_type, data, transport=transport)
        admission = self._admit_trusted_ingress_ticket(ticket)
        if admission is None:
            logger.warning("[trusted-ingress] denied callback kind=%s", kind)
            return self._card_response() if kind in {"button", "form"} else None
        envelope = _TrustedFeishuEnvelope(data, ticket, admission)
        callback = self._on_message_event if kind == "message" else self._on_card_action_trigger
        return callback(envelope)

    adapter._dispatch_trusted_ingress = dispatch

    def wrap_callback(name, event_type):
        original = getattr(adapter, name)

        @wraps(original)
        def admitted(self, data):
            if type(data) is _TrustedFeishuEnvelope:
                if _envelope_allowed(self, data):
                    token = _prompt_ingress.set(data)
                    try:
                        return getattr(admitted, INGRESS_INNER_ATTR)(self, data)
                    finally:
                        _prompt_ingress.reset(token)
                logger.warning(
                    "[trusted-ingress] envelope rejected callback=%s message_id=%s reason=%s",
                    name,
                    getattr(data.trusted_feishu_ingress_ticket, "message_id", ""),
                    _envelope_reject_reason(self, data),
                )
                return None
            return self._dispatch_trusted_ingress(event_type, data, transport=_transport.get())

        setattr(admitted, INGRESS_INNER_ATTR, original)
        setattr(admitted, INGRESS_CALLBACK_FLAG, True)
        setattr(adapter, name, admitted)

    for name, event_type in (
        ("_on_message_event", "im.message.receive_v1"),
        ("_on_card_action_trigger", "card.action.trigger"),
        ("_on_drive_comment_event", "drive.notice.comment_add_v1"),
        ("_on_meeting_invited_event", "vc.bot.meeting_invited_v1"),
    ):
        wrap_callback(name, event_type)

    original_reaction = adapter._on_reaction_event

    @wraps(original_reaction)
    def reaction(self, event_type, data):
        if type(data) is not _PendingReaction:
            data = _PendingReaction(data, event_type, _transport.get())
        return original_reaction(self, event_type, data)

    adapter._on_reaction_event = reaction
    original_synthetic = adapter._dispatch_synthetic_event

    @wraps(original_synthetic)
    async def synthetic(self, **kwargs):
        data = kwargs["raw_message"]
        if type(data) is _PendingReaction:
            # Stock core has fetched the message and verified its sender app_id.
            # The resolved chat, not any callback-supplied chat, binds the ticket.
            ticket = self._issue_trusted_ingress_ticket(
                data.event_type, data.raw, transport=data.transport,
                trusted_chat_id=kwargs["chat_id"],
            )
            admission = self._admit_trusted_ingress_ticket(ticket)
            if admission is None:
                return None
            data = _TrustedFeishuEnvelope(data.raw, ticket, admission)
        if not _envelope_allowed(self, data):
            return None
        ticket = data.trusted_feishu_ingress_ticket
        if kwargs["chat_id"] != ticket.chat_id:
            return None
        kwargs.update(raw_message=data, message_id=ticket.message_id,
                      event_chat_type=data.trusted_feishu_ingress_admission.chat_type)
        return await original_synthetic(self, **kwargs)

    adapter._dispatch_synthetic_event = synthetic
    original_guards = adapter._handle_message_with_guards

    @wraps(original_guards)
    async def guarded(self, event):
        from .trusted_feishu_ingress import validate_admitted_feishu_event

        raw = event.raw_message
        if not _envelope_allowed(self, raw):
            return None
        admission = raw.trusted_feishu_ingress_admission
        if admission.actor_kind == "user":
            # Stock core keeps tenant user_id + union_id and drops open_id.
            # Recover only the sealed actor's canonical ID after verifying every
            # source alias against that same active routing row.
            from .router import _get_routing_table

            table = _get_routing_table()
            row = table.lookup_by_open_id(admission.actor_subject) if table else None
            aliases = {str(value) for value in (
                getattr(row, "open_id", None), getattr(row, "user_id", None),
                getattr(row, "union_id", None),
            ) if value}
            source_ids = {str(value) for value in (
                getattr(event, "sender_open_id", None), getattr(event.source, "open_id", None),
                getattr(event.source, "user_id", None), getattr(event.source, "user_id_alt", None),
            ) if value}
            if row is None or not source_ids or not source_ids.issubset(aliases):
                return None
            event.sender_open_id = row.open_id
        for target in (event, event.source):
            target.trusted_feishu_ingress_ticket = raw.trusted_feishu_ingress_ticket
            target.trusted_feishu_ingress_admission = raw.trusted_feishu_ingress_admission
        if not validate_admitted_feishu_event(event, adapter=self):
            return None
        token = _prompt_ingress.set(raw)
        owner_token = _prompt_owner.set(_seal_prompt_owner(self, raw))
        try:
            return await original_guards(self, event)
        finally:
            _prompt_owner.reset(owner_token)
            _prompt_ingress.reset(token)

    adapter._handle_message_with_guards = guarded

    async def inbound(self, event):
        # A batch cannot represent more than one signed actor/message binding.
        return await self._handle_message_with_guards(event)

    adapter._dispatch_inbound_event = inbound
    original_webhook = adapter._handle_webhook_request

    @wraps(original_webhook)
    async def webhook(self, request):
        token = _transport.set("webhook")
        try:
            return await original_webhook(self, request)
        finally:
            _transport.reset(token)

    adapter._handle_webhook_request = webhook
