"""The one fixed Feishu card-action dispatcher (WP02).

Multi-Tenancy used to stack four independent class-level wrappers on
``FeishuAdapter._on_card_action_trigger`` (clarify, cred_auth/gitlab, group
reply-mode, push-confirm). Install order changed behaviour, and — worse — every
one of them fell back to ``original(self, data)`` when its own handler raised,
so a failed *recognized* button could still reach the core generic path that
synthesizes a ``/card`` command and feeds the raw callback JSON to the model.

This module replaces all four with a single dispatcher that:

  1. parses the callback exactly once into :class:`CardCallback`;
  2. selects a handler by a FIXED order — ``inject_prompt`` (slot reserved, NOT
     implemented here) → clarify → auth/credential → explicitly allowlisted
     Agent core action → registered namespaced business action;
  3. consumes every action it recognizes. A recognized handler that raises
     returns a generic, data-free error response — never a second handler and
     never the original. A genuinely unknown action is consumed as unsupported,
     so arbitrary callback JSON can no longer reach the model.

Security deviation from "unknown pass-through" is deliberate and is the P0 this
package exists for; see the SPEC's Implementation Decisions.
"""
from __future__ import annotations

import functools
import json
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, NamedTuple, Optional

logger = logging.getLogger(__name__)

_DISPATCHER_FLAG = "_hermes_multitenancy_card_action_dispatcher"

# Idempotency flags the four retired installers still check (and that their
# focused suites still assert on). The dispatcher stamps all of them, so any
# install order — and any leftover call site — converges on one live wrapper.
_LEGACY_FLAGS = (
    "_hermes_multitenancy_group_valve_card_action_patched",
    "_hermes_multitenancy_cred_auth_card_action_patched",
    "_hermes_multitenancy_clarify_card_action_patched",
    "_hermes_multitenancy_push_confirm_card_action_patched",
)

# Agent core actions we may hand to the original handler exactly once. Every one
# is BACKED by a real handler in `be5a764d0:gateway/platforms/feishu.py`:
# `feishu_auth` → `_handle_feishu_auth_card_action`; the four approval choices →
# `_handle_approval_card_action` via `_APPROVAL_CHOICE_MAP` (:229) — including
# `approve_always`, which the approval card emits as "✅ Always" (:2457).
_AGENT_CORE_ACTIONS = frozenset(
    {"feishu_auth", "approve_once", "approve_session", "approve_always", "deny"}
)

# The allowlist above is the WHOLE delegation surface, and it delegates ONLY the
# spelling core actually reads — `value["hermes_action"]` (feishu.py:3272). Core
# recognises no other spelling: hand it `{"action": "approve_once"}` or
# `action.name = "approve_once"` and it falls through to
# `_handle_card_action_event`, which synthesizes `/card` and feeds the raw
# callback JSON to the model. So an allowlisted NAME arriving under any other
# spelling is consumed here, never delegated. Do not add a value key without a
# real core handler to point at either: an unbacked key is reachable only by a
# crafted callback, and delegating it opens the same `/card` model path.
_AGENT_CORE_VALUE_KEY = "hermes_action"

# Core actions that arrive under their OWN top-level `value` key instead of
# `hermes_action` — core reads each key directly, so they never carry a `kind`.
#
# WHICH of these the running core implements differs by core LINE, so an entry
# names the adapter method to probe for at dispatch time rather than asserting
# the capability exists. A static list is right on whichever line it was
# measured against and wrong on the other: on 2026-08-11 this package removed
# `hermes_update_prompt_action` on evidence gathered from `main` (no handler,
# no card) while the production release line both emits the card and defines
# `_handle_update_prompt_card_action` — every production update-prompt card
# started answering "该操作暂不支持". Probing the LIVE adapter is correct on
# both lines without anyone having to remember there are two.
_AGENT_CORE_VALUE_KEY_HANDLERS: tuple[tuple[str, str], ...] = (
    ("hermes_update_prompt_action", "_handle_update_prompt_card_action"),
)

# Reserved but NOT implemented by this package. `inject_prompt` keeps the first
# precedence slot so no business namespace can claim the name and quietly
# implement it here; the slot itself answers exactly like a genuinely unknown
# action — same `unsupported_response()`, no claim, no handler — so a crafted
# callback cannot learn from the response that the name was recognised.
#
# Why it left: two rounds of opposite-family review proved the implementation
# never bound the callback's clicking operator to the ticket's actor, nor
# validated the admission profile. A valid ticket for `ou_alice` paired with
# callback operator `ou_mallory` still produced `toast=info, scheduled=1` — a
# leaked card clicked by a stranger ran as its original owner. The fix is an
# unforgeable capability, not another string comparison; it is the first Done
# line of slug `feishu-card-inject-prompt` (2026-08-10, sunke ratified).
_UNSUPPORTED_KINDS = frozenset({"inject_prompt"})

# module path + attribute for each built-in, imported lazily so the dispatcher
# never drags the credential/clarify/broker subsystems into an import cycle.
_BUILTINS: tuple[tuple[str, frozenset[str], str, str], ...] = (
    (
        "clarify",
        frozenset({"clarify"}),
        "feishu_clarify_cards",
        "handle_clarify_card_action",
    ),
    (
        "cred_auth",
        frozenset({"cred_auth", "gitlab_token"}),
        "feishu_auth_hub_actions",
        "handle_auth_hub_card_action",
    ),
)

_BUILTIN_NAMESPACES = frozenset(
    kind for _name, kinds, _mod, _fn in _BUILTINS for kind in kinds
) | _AGENT_CORE_ACTIONS | _UNSUPPORTED_KINDS


# --- callback envelope ---------------------------------------------------------

@dataclass(frozen=True)
class CardCallback:
    """One parse of an SDK card.action.trigger payload. Handlers read this
    instead of re-walking ``data`` (the old wrappers each walked it again)."""

    data: Any
    event: Any
    action: Any
    value: dict[str, Any]
    form_value: Any
    action_name: str
    form_name: str
    kind: str

    @property
    def context(self) -> Any:
        return _read(self.event, "context")

    @property
    def chat_id(self) -> str:
        """The SIGNED chat this card lives in. Never the button payload."""
        return _text(_read(self.context, "open_chat_id"))

    @property
    def message_id(self) -> str:
        for name in ("open_message_id", "message_id"):
            value = _text(_read(self.context, name))
            if value:
                return value
        return _text(_read(self.event, "open_message_id"))


def _read(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def parse_card_callback(data: Any) -> CardCallback:
    """Parse the SDK payload once. Upstream's ``value.action`` / ``action.name``
    / ``form_name`` order, plus Hermes' existing ``value.hermes_action`` key."""
    event = _read(data, "event")
    action = _read(event, "action")
    value = _as_dict(_read(action, "value"))
    form_value = _read(action, "form_value")
    action_name = _text(_read(action, "name"))
    form_name = _text(_read(action, "form_name")) or _text(_read(action, "form_id"))

    kind = ""
    for candidate in (value.get("action"), value.get("hermes_action")):
        if isinstance(candidate, str) and candidate.strip():
            kind = candidate.strip()
            break
    if not kind:
        kind = action_name or form_name

    return CardCallback(
        data=data,
        event=event,
        action=action,
        value=value,
        form_value=form_value,
        action_name=action_name,
        form_name=form_name,
        kind=kind,
    )


# --- minimal namespaced business registration ----------------------------------

BusinessHandler = Callable[[Any, CardCallback], Any]
BusinessMatcher = Callable[[CardCallback], bool]


class _Business(NamedTuple):
    namespace: str
    handler: BusinessHandler
    matcher: Optional[BusinessMatcher]


_BUSINESS: "OrderedDict[str, _Business]" = OrderedDict()

# MT's own business actions. Registered lazily on first dispatch so routing does
# NOT depend on which feature installer ran, or in what order — the exact
# fragility this package removes. (namespace, module, handler, matcher)
_DEFAULT_BUSINESS: tuple[tuple[str, str, str, Optional[str]], ...] = (
    ("group_reply_mode", "feishu_group_valve", "_handle_group_reply_mode_card_action", None),
    (
        "push_confirm",
        "push_card_confirm",
        "_handle_push_confirm_card_action",
        "_match_push_confirm_card_action",
    ),
)


def _ensure_default_business_registered() -> None:
    import importlib

    for namespace, module_name, handler_name, matcher_name in _DEFAULT_BUSINESS:
        if namespace in _BUSINESS:
            continue
        try:
            module = importlib.import_module(f".{module_name}", __package__)
            register_business_action(
                namespace,
                getattr(module, handler_name),
                matcher=getattr(module, matcher_name) if matcher_name else None,
            )
        except Exception:
            logger.warning(
                "[card_action] default business action %s unavailable", namespace, exc_info=True
            )


def register_business_action(
    namespace: str,
    handler: BusinessHandler,
    *,
    matcher: Optional[BusinessMatcher] = None,
) -> None:
    """Give ``namespace`` exactly one handler.

    ``matcher`` is for callbacks Feishu delivers WITHOUT the button value (a form
    submit strips it, leaving only ``form_value`` + ``action.name``); it is
    consulted only after the exact-namespace lookup misses.

    Re-registering the same handler is a no-op (installers are called from
    several entry points). A different handler for a taken namespace, or any
    collision with a built-in, is rejected — the built-in stays the owner.
    """
    ns = _text(namespace)
    if not ns:
        raise ValueError("business action namespace must be a non-empty string")
    if not callable(handler):
        raise ValueError(f"business action {ns!r} handler must be callable")
    if ns in _BUILTIN_NAMESPACES:
        raise ValueError(f"business action {ns!r} collides with a built-in card action")
    existing = _BUSINESS.get(ns)
    if existing is not None:
        if existing.handler is handler:
            return
        raise ValueError(f"business action namespace {ns!r} is already registered")
    _BUSINESS[ns] = _Business(ns, handler, matcher)


def registered_business_namespaces() -> tuple[str, ...]:
    return tuple(_BUSINESS)


def _reset_business_registry_for_tests() -> None:
    _BUSINESS.clear()


# --- responses (data-free by contract) -----------------------------------------

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


def unsupported_response() -> Any:
    """A genuinely unknown action. Consumed, never forwarded to the model."""
    return _toast_response("该操作暂不支持。", level="info")


# --- at-most-once ---------------------------------------------------------------

# ponytail: process-local bounded claim. One delivery of one callback runs its
# handler at most once — a redelivery/retry of the SAME callback is refused.
# Ceiling: a restart or eviction re-opens the window, and Feishu mints a fresh
# token per click, so two clicks are two callbacks and both run. The durable
# ledger that would close that is WP09; see the SPEC's Dead ends.
_CLAIMED_MAX = 4096
_claimed: "OrderedDict[str, None]" = OrderedDict()
_claimed_lock = threading.Lock()


def callback_identity(cb: CardCallback) -> str:
    """This callback's own identity: Feishu's per-delivery token, else the
    signed (message, chat, operator, kind) tuple."""
    token = _text(_read(cb.event, "token")) or _text(_read(cb.data, "token"))
    if token:
        return token
    if not cb.message_id:
        return ""
    operator = _read(cb.event, "operator") or _read(cb.event, "operator_id")
    who = (
        _text(_read(operator, "open_id"))
        or _text(_read(operator, "union_id"))
        or _text(_read(operator, "user_id"))
    )
    return "\x1f".join((cb.message_id, cb.chat_id, who, cb.kind))


def _claim_once(identity: str) -> bool:
    if not identity:
        # No identity to key on (no token, no message id): nothing to dedupe
        # against. The action is still consumed — it just isn't replay-guarded.
        return True
    with _claimed_lock:
        if identity in _claimed:
            return False
        _claimed[identity] = None
        while len(_claimed) > _CLAIMED_MAX:
            _claimed.popitem(last=False)
    return True


def _reset_claims_for_tests() -> None:
    with _claimed_lock:
        _claimed.clear()


# --- dispatch ------------------------------------------------------------------

def _note_live_adapter(adapter: Any) -> None:
    """Capture the live adapter for proactive push sends. The retired push
    wrapper did this on EVERY callback; without it cold-start push is dead."""
    try:
        from . import push_send_queue

        push_send_queue.note_live_adapter(adapter)
    except Exception:
        logger.debug("[card_action] live adapter capture failed", exc_info=True)


def _run_builtin(name: str, module_name: str, func_name: str, adapter: Any, cb: CardCallback) -> Any:
    import importlib

    module = importlib.import_module(f".{module_name}", __package__)
    return getattr(module, func_name)(adapter, cb)


def _adapter_implements(adapter: Any, handler_attr: str) -> bool:
    """Whether the LIVE adapter carries this core handler. Never raises.

    Reading the attribute can execute code — a property, a `__getattr__`, a
    descriptor — and `getattr(obj, name, None)` only swallows `AttributeError`,
    so anything else would escape `dispatch_card_action` into the SDK. That
    turns a refusal that is supposed to be indistinguishable from an unknown
    action into a crash a caller can provoke and observe. Absent, unreadable and
    non-callable all collapse to the same answer: not implemented, consume.
    """
    try:
        return callable(_read(adapter, handler_attr))
    except Exception:
        logger.debug("[card_action] handler probe raised for %s", handler_attr, exc_info=True)
        return False


def _core_routable_value(cb: CardCallback) -> Optional[dict]:
    """Whether core will see this callback's ``value`` as a routable mapping.

    The precondition for EVERY delegation: core guards both of its routing reads
    with ``isinstance(action_value, dict)``, so if the raw value is not a dict
    core reaches no handler at all — it falls into `_handle_card_action_event`,
    synthesizes `/card`, and feeds the raw callback JSON to the model. That is
    the P0.

    MT parses more liberally than core reads: `_as_dict` decodes a JSON STRING
    value into a dict, which is right for MT's own business handlers (they run
    here, not in core) and wrong as a basis for handing the callback to core.
    `{"action": {"value": '{"hermes_action": "approve_once"}'}}` — a string, not
    a dict — was delegated on the strength of MT's parse while core dropped it
    straight down the model path. Delegation must be decided on the object core
    will actually read, not on ours.

    TRUTHINESS matters as much as the type. Core's read is
    ``action_value = getattr(action, "value", {}) or {}`` — a FALSY dict is
    replaced by an empty one, so its keys are gone before core ever looks, and
    core routes nothing. `isinstance` alone would pass a `dict` subclass whose
    ``__bool__`` is False while it still answers `.get(key)` truthfully to us:
    MT delegates, core sees `{}`, and the callback lands on the model path.
    Mirror the `or {}` exactly.

    A CHECK IS NOT ENOUGH ON ITS OWN if the object can change its answer. Core
    re-reads `value` after we decide, so a mapping with a stateful `__bool__` or
    `.get` — or one that raises — beats any amount of inspection. That is a
    time-of-check/time-of-use gap, not a missing condition, and it cannot be
    closed by looking harder.

    So require a PLAIN dict, exactly, and refuse everything else. A plain dict
    has no hooks: `.get` and truthiness are inert, our read and core's read
    cannot disagree, and the whole class of adversarial mappings — falsy,
    stateful, exploding — is gone in one condition rather than one patch per
    trick. The type test comes FIRST so a hostile `__bool__` is never invoked.

    Refusing rather than normalizing is deliberate. Coercing a hostile mapping
    into a valid plain dict would make core act on input it would otherwise have
    ignored; consuming it is the fail-closed direction and the simpler rule.

    Real traffic is unaffected: the SDK deserializes the webhook body with
    `dict_obj = loads(json_str)` (`lark_oapi/core/json.py`, verified on the
    production host 2026-08-11), and `json.loads` yields exactly `dict`.

    Emptiness needs no test of its own here. Core's `or {}` only matters for a
    FALSY dict, and the sole falsy plain dict is `{}` — whose keys the callers
    read right after this, finding nothing and refusing to delegate. An explicit
    truthiness check would be unfalsifiable: no input can distinguish it.

    Returns the value core will route on, or ``None`` when it must not delegate.
    """
    raw = _read(cb.action, "value")
    return raw if type(raw) is dict else None


def _delegate(original: Callable[[Any, Any], Any], adapter: Any, data: Any) -> Any:
    """Hand the callback to core, and never let a bare ``None`` come back out.

    Core answers `P2CardActionTriggerResponse() if P2CardActionTriggerResponse
    else None`, so `None` means the SDK class was unavailable — never a real
    outcome. Returning it while an unknown action returns a toast would make
    "this name is recognised" readable again, one layer up from the branches
    this package just collapsed.

    WHAT THIS CANNOT DO, stated rather than implied: core's SUCCESS response is
    core's own, and it necessarily differs from a refusal — if a working button
    answered "该操作暂不支持" the feature would be broken. So the property this
    dispatcher can hold is "every REFUSAL looks alike", not "every response
    looks alike". Distinguishing a successful action from an unknown one is
    inherent to the feature working at all.
    """
    result = original(adapter, data)
    return unsupported_response() if result is None else result


def dispatch_card_action(adapter: Any, data: Any, original: Callable[[Any, Any], Any]) -> Any:
    """Route one callback. Logs carry action kind + outcome only."""
    _note_live_adapter(adapter)
    try:
        cb = parse_card_callback(data)
    except Exception:
        logger.warning("[card_action] unparseable callback; consumed")
        logger.debug("[card_action] parse traceback", exc_info=True)
        return unsupported_response()

    if cb.kind in _UNSUPPORTED_KINDS:
        # Reserved slot, no implementation. Deliberately identical to the
        # unknown-action path below: same response object, and no `_run_once`
        # claim. (Historically the claim mattered here because a claimed replay
        # answered differently; every exit now answers the same, so this is
        # belt-and-braces rather than the load-bearing part.)
        logger.info("[card_action] kind=unknown outcome=unsupported")
        return unsupported_response()

    for name, kinds, module_name, func_name in _BUILTINS:
        if cb.kind in kinds:
            return _run_once(cb, name, lambda: _run_builtin(name, module_name, func_name, adapter, cb))

    # The one object core will route on: frozen, judged once, and handed to core
    # as-is. `None` means core would reach no handler, so nothing may delegate.
    routable = _core_routable_value(cb)

    if cb.kind in _AGENT_CORE_ACTIONS:
        # Core reads ONE spelling. Any other spelling of an allowlisted name
        # would reach core's generic `/card` model path instead of its handler,
        # so it is consumed here — the P0 through a different door.
        if routable is None:
            logger.info("[card_action] kind=agent_core outcome=unsupported_spelling")
            return unsupported_response()
        if _text(routable.get(_AGENT_CORE_VALUE_KEY)) != cb.kind:
            logger.info("[card_action] kind=agent_core outcome=unsupported_spelling")
            return unsupported_response()
        try:
            return _delegate(original, adapter, data)
        except Exception:
            logger.warning("[card_action] kind=agent_core outcome=error")
            logger.debug("[card_action] handler traceback", exc_info=True)
            return unsupported_response()

    for value_key, handler_attr in _AGENT_CORE_VALUE_KEY_HANDLERS:
        if routable is None:
            continue
        # Raw truthiness, NOT `_text(...)`, and the invariant is exact:
        # delegating must imply core routes to the handler. Core tests these two
        # keys with bare `if`, while `_text` is `str(value).strip()` — so
        # `{value_key: 0}` reads as the non-empty string "0" here and as falsy
        # there. Under `_text` that callback would be delegated and then fall
        # through core's routing into `_handle_card_action_event`, which
        # synthesizes `/card` and feeds the raw callback JSON to the model: the
        # P0, reachable only through this path. Same for False/[]/{}/0.0.
        if not routable.get(value_key):
            continue
        if routable.get(_AGENT_CORE_VALUE_KEY):
            # Core reads `hermes_action` FIRST. Anything still here carries one
            # that ISN'T allowlisted (an allowlisted name returned above), so
            # delegating would hand core's approval handler exactly the name the
            # allowlist keeps out — smuggled in as this key's companion.
            logger.info("[card_action] kind=agent_core outcome=unsupported_spelling")
            return unsupported_response()
        if not _adapter_implements(adapter, handler_attr):
            # This core line doesn't implement it. Delegating would fall through
            # core's own routing into `_handle_card_action_event`, which
            # synthesizes `/card` and feeds the raw callback JSON to the model —
            # the P0. The response is byte-identical to a genuinely unknown
            # action so it can't be used to fingerprint which core line runs;
            # only the log line distinguishes them, for operators.
            logger.info("[card_action] kind=agent_core outcome=unsupported_capability")
            return unsupported_response()
        # No `_run_once` claim — same as the allowlist path above, and for the
        # same reason: core owns its own duplicate guard
        # (`_is_card_action_duplicate`) and its approval/prompt state.
        try:
            return _delegate(original, adapter, data)
        except Exception:
            logger.warning("[card_action] kind=agent_core outcome=error")
            logger.debug("[card_action] handler traceback", exc_info=True)
            return unsupported_response()

    _ensure_default_business_registered()
    entry = _BUSINESS.get(cb.kind)
    def _spelled(key: str) -> str:
        # ONE read per key. This used to be `.get(key)` for the type test and
        # `[key]` for the value; two reads of the same key can disagree, and the
        # second one raising escapes the dispatcher entirely.
        value = cb.value.get(key)
        return value.strip() if isinstance(value, str) else ""

    if entry is None and not any(_spelled(key) for key in ("action", "hermes_action")):
        for candidate in _BUSINESS.values():
            if candidate.matcher is not None and _safe_match(candidate, cb):
                entry = candidate
                break
    if entry is not None:
        if not _business_admission_is_valid(adapter, cb):
            # Answer exactly as an unrecognised action does. Any distinct
            # response here would let a crafted caller enumerate registered
            # business namespaces by watching which names answer differently.
            # The rejection stays visible to operators in the log line.
            logger.info("[card_action] kind=business outcome=rejected")
            return unsupported_response()
        return _run_once(cb, entry.namespace, lambda: entry.handler(adapter, cb))

    logger.info("[card_action] kind=unknown outcome=unsupported")
    return unsupported_response()


def _business_admission_is_valid(adapter: Any, cb: CardCallback) -> bool:
    """Business handlers bypass core, so enforce its admitted identity here."""
    try:
        from .trusted_feishu_ingress import TrustedFeishuAdmission

        ticket = _read(cb.data, "trusted_feishu_ingress_ticket")
        admission = _read(cb.data, "trusted_feishu_ingress_admission")
        authentic = isinstance(admission, TrustedFeishuAdmission) and admission.is_authentic()
        valid_ticket = callable(_read(ticket, "is_valid")) and ticket.is_valid(
            account_id=_text(_read(adapter, "_app_id"))
        )
        actor_id_type = _text(_read(ticket, "actor_id_type"))
        operator = _read(cb.event, "operator") or _read(cb.event, "operator_id")
        operator_id = _text(_read(operator, actor_id_type))
        context_thread = _text(_read(cb.context, "open_thread_id"))
        ticket_thread = _text(_read(ticket, "thread_id"))
        event_chat = _text(_read(cb.event, "open_chat_id"))
        event_message = _text(_read(cb.event, "open_message_id"))
        expected_scope = (
            "feishu:bot" if _text(_read(admission, "chat_type")) == "group" else "feishu:user"
        )
        return bool(
            authentic
            and valid_ticket
            and operator_id
            and operator_id
            == _text(_read(ticket, "actor_id"))
            == _text(_read(admission, "actor_id"))
            and actor_id_type == _text(_read(admission, "actor_id_type"))
            and cb.chat_id
            and cb.chat_id
            == _text(_read(ticket, "chat_id"))
            == _text(_read(admission, "chat_id"))
            and cb.message_id
            == _text(_read(ticket, "message_id"))
            == _text(_read(admission, "message_id"))
            and (not event_chat or event_chat == cb.chat_id)
            and (not event_message or event_message == cb.message_id)
            and context_thread == ticket_thread
            and _text(_read(admission, "profile_name"))
            and int(_read(admission, "route_version") or 0) > 0
            and _text(_read(admission, "actor_subject"))
            and _text(_read(admission, "credential_subject"))
            and _text(_read(admission, "tool_scope")) == expected_scope
        )
    except Exception:
        return False


def _run_once(cb: CardCallback, name: str, run: Callable[[], Any]) -> Any:
    """Run a RECOGNIZED handler at most once per callback, and consume its
    failure. Core delegation is excluded on purpose: core owns its own
    ``_is_card_action_duplicate`` token guard and approval state."""
    if not _claim_once(callback_identity(cb)):
        logger.info("[card_action] kind=%s outcome=duplicate", name)
        return unsupported_response()
    try:
        return run()
    except Exception:
        logger.warning("[card_action] kind=%s outcome=error", name)
        logger.debug("[card_action] handler traceback", exc_info=True)
        return unsupported_response()


def _safe_match(candidate: _Business, cb: CardCallback) -> bool:
    try:
        return bool(candidate.matcher(cb))  # type: ignore[misc]
    except Exception:
        logger.debug("[card_action] matcher %s failed", candidate.namespace, exc_info=True)
        return False


# --- installation --------------------------------------------------------------

def install_feishu_card_action_dispatcher(FeishuAdapter: Any = None) -> bool:
    """Idempotently install THE dispatcher. Returns True when one is live.

    ``FeishuAdapter`` may be passed explicitly (the re-arm path in
    ``push_send_queue`` supplies the live runtime class); otherwise it is
    resolved through ``feishu_adapter_compat``.
    """
    if FeishuAdapter is None:
        try:
            from .feishu_adapter_compat import load_feishu_adapter

            FeishuAdapter = load_feishu_adapter()
        except Exception as exc:
            try:
                from .feishu_adapter_compat import log_feishu_adapter_load_error

                log_feishu_adapter_load_error(
                    logger,
                    "[card_action] FeishuAdapter not importable yet; dispatcher deferred",
                    exc,
                )
            except Exception:
                logger.debug("[card_action] dispatcher install deferred", exc_info=True)
            return False

    original = getattr(FeishuAdapter, "_on_card_action_trigger", None)
    if original is None:
        return False
    if getattr(original, _DISPATCHER_FLAG, False):
        return True

    @functools.wraps(original)
    def wrapped(self: Any, data: Any) -> Any:
        return dispatch_card_action(self, data, original)

    setattr(wrapped, _DISPATCHER_FLAG, True)
    for flag in _LEGACY_FLAGS:
        setattr(wrapped, flag, True)
    FeishuAdapter._on_card_action_trigger = wrapped
    logger.info(
        "[card_action] installed the card-action dispatcher on %s.FeishuAdapter",
        getattr(FeishuAdapter, "__module__", "?"),
    )
    return True
