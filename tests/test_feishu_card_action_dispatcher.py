"""WP02 — the ONE Feishu card-action dispatcher.

Every test drives the installed adapter callback with synthetic SDK-shaped data
and asserts externally observable routing: which handler ran, how many times the
ORIGINAL core handler was called (the generic ``/card`` → model path), and how
many trusted Run Broker turns were created. Nothing here inspects wrapper
nesting — that was the fragility this package removes.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from hermes_multitenancy import feishu_card_action_dispatcher as dispatcher
from hermes_multitenancy.trusted_feishu_ingress import TrustedFeishuAdmission
from tests.conftest import card_response_bytes


@pytest.fixture(autouse=True)
def _clean_registry():
    dispatcher._reset_business_registry_for_tests()
    dispatcher._reset_claims_for_tests()
    yield
    dispatcher._reset_business_registry_for_tests()
    dispatcher._reset_claims_for_tests()


_TOKENS = itertools.count()


def _token() -> str:
    """Feishu mints a token per DELIVERY. Tests that click twice must mint two,
    or they are asserting on a redelivery — which is refused by design."""
    return f"tk_{next(_TOKENS)}"


def _adapter_class():
    """A throwaway adapter class whose methods stand in for the core chain.

    ``calls["original"]`` counts the generic ``/card`` → model path.
    ``calls["turns"]`` counts reaches for the normal inbound path — the only
    other way a click could become model/tool work.
    """
    calls = {"original": 0, "turns": []}

    class FakeFeishuAdapter:
        _app_id = "cli_test"
        _loop = None

        def _on_card_action_trigger(self, data):
            calls["original"] += 1
            return {"kind": "delegated"}

        def _handle_message_with_guards(self, event):
            # ponytail: sync on purpose. Production awaits this, but recording
            # at CONSTRUCTION rather than at await catches a scheduling attempt
            # even against a dead loop — a stricter zero-turn observable.
            calls["turns"].append(event)

    return FakeFeishuAdapter, calls


def _installed():
    cls, calls = _adapter_class()
    assert dispatcher.install_feishu_card_action_dispatcher(cls) is True
    return cls(), calls


@dataclass(frozen=True)
class FakeTicket:
    """Same shape the adapter edge stamps (``TrustedFeishuIngressTicket``); the
    real class is exercised in tests/test_feishu_trusted_ingress.py."""

    actor_id: str = "ou_alice"
    actor_id_type: str = "open_id"
    account_id: str = "cli_test"
    chat_id: str = "oc_dm"
    thread_id: str = ""
    message_id: str = "om_1"
    valid: bool = True

    def is_valid(self, *, account_id: str) -> bool:
        return self.valid and account_id == self.account_id


def _admission(ticket, *, profile_name="profile_a", chat_type="p2p"):
    """What WP01's admitter hands back once a ticket is bound to a route."""
    return TrustedFeishuAdmission(
        profile_name=profile_name,
        route_version=1,
        actor_id=ticket.actor_id,
        actor_id_type=ticket.actor_id_type,
        actor_subject=ticket.actor_id,
        chat_type=chat_type,
        chat_id=ticket.chat_id,
        message_id=ticket.message_id,
        credential_subject=ticket.actor_id,
        tool_scope="feishu:user" if chat_type == "p2p" else "feishu:bot",
        ticket_fingerprint="fp_test",
    )


def _data(value=None, *, name="btn", form_value=None, chat="oc_dm", message="om_1",
          operator="ou_alice", thread=None, token=None, trusted=True,
          ticket=None, admission=None):
    context = SimpleNamespace(open_chat_id=chat, open_message_id=message)
    if thread is not None:
        context.open_thread_id = thread
    event = SimpleNamespace(
        action=SimpleNamespace(tag="button", name=name, value=value, form_value=form_value),
        operator=SimpleNamespace(open_id=operator, union_id=None, user_id=None),
        context=context,
    )
    if token is not None:
        event.token = token
    envelope = SimpleNamespace(event=event)
    if trusted:
        # What the adapter edge delivers: the callback wrapped in its ticket +
        # the admission WP01 bound it to.
        if ticket is None:
            ticket = FakeTicket(
                actor_id=operator, chat_id=chat, message_id=message, thread_id=thread or ""
            )
        envelope.trusted_feishu_ingress_ticket = ticket
        envelope.trusted_feishu_ingress_admission = (
            _admission(ticket) if admission is None else admission
        )
    return envelope


def _kind(response):
    return response["kind"] if isinstance(response, dict) else (
        "toast" if getattr(response, "toast", None) is not None else "card"
    )


def _toast_type(response):
    return response["toast"]["type"] if isinstance(response, dict) else response.toast["type"]


# --- installation ---------------------------------------------------------------

def test_installing_twice_leaves_one_live_dispatcher():
    cls, calls = _adapter_class()
    original = cls._on_card_action_trigger
    assert dispatcher.install_feishu_card_action_dispatcher(cls) is True
    once = cls._on_card_action_trigger
    assert dispatcher.install_feishu_card_action_dispatcher(cls) is True
    assert cls._on_card_action_trigger is once
    assert once is not original

    cls()._on_card_action_trigger(_data({"hermes_action": "nope"}))
    assert calls["original"] == 0


def test_every_retired_installer_converges_on_the_same_dispatcher():
    from hermes_multitenancy.feishu_auth_hub_actions import _patch_card_action as patch_auth
    from hermes_multitenancy.feishu_clarify_cards import _patch_card_action as patch_clarify
    from hermes_multitenancy.feishu_group_valve import _patch_on_card_action_trigger as patch_group
    from hermes_multitenancy.push_card_confirm import _patch_card_action as patch_push

    cls, _calls = _adapter_class()
    for patch in (patch_push, patch_group, patch_clarify, patch_auth):
        patch(cls)
    live = cls._on_card_action_trigger
    for patch in (patch_auth, patch_clarify, patch_group, patch_push):
        patch(cls)
    assert cls._on_card_action_trigger is live
    assert getattr(live, dispatcher._DISPATCHER_FLAG, False)
    for flag in dispatcher._LEGACY_FLAGS:
        assert getattr(live, flag, False), flag


# --- unknown / failure consumption (the P0) --------------------------------------

def test_truly_unknown_action_is_consumed_as_unsupported():
    adapter, calls = _installed()
    response = adapter._on_card_action_trigger(_data({"hermes_action": "totally_unknown_xyz"}))
    assert calls["original"] == 0
    assert _kind(response) == "toast"


def test_value_less_callback_is_consumed_not_turned_into_a_card_command():
    """The shape that used to reach ``_handle_card_action_event`` and become a
    synthetic ``/card`` model turn carrying the raw callback JSON."""
    adapter, calls = _installed()
    response = adapter._on_card_action_trigger(_data(None, name="mystery_button"))
    assert calls["original"] == 0
    assert _kind(response) == "toast"


def test_unparseable_callback_is_consumed():
    adapter, calls = _installed()
    response = adapter._on_card_action_trigger(object())
    assert calls["original"] == 0
    assert _kind(response) == "toast"


def test_known_handler_exception_is_consumed_with_a_data_free_error(monkeypatch):
    from hermes_multitenancy import feishu_auth_hub_actions

    adapter, calls = _installed()

    def boom(*_a, **_k):
        raise RuntimeError("employee-secret-12345")

    monkeypatch.setattr(feishu_auth_hub_actions, "_handle_cred_auth_action", boom)
    response = adapter._on_card_action_trigger(
        _data({"hermes_action": "cred_auth", "cred": "lark-cli"})
    )
    assert calls["original"] == 0
    assert _toast_type(response) == "error"
    assert "12345" not in str(response)
    assert "cred" not in str(response)


def test_business_handler_exception_is_consumed():
    adapter, calls = _installed()

    def boom(_adapter, _cb):
        raise RuntimeError("boom")

    dispatcher.register_business_action("acme_thing", boom)
    response = adapter._on_card_action_trigger(_data({"action": "acme_thing"}))
    assert calls["original"] == 0
    assert _toast_type(response) == "error"


def test_registered_business_action_requires_the_admitted_callback_identity():
    adapter, calls = _installed()
    seen = []
    dispatcher.register_business_action(
        "acme", lambda _a, _cb: seen.append("business") or {"kind": "business"}
    )
    ticket = FakeTicket()
    rejected = [
        _data({"action": "acme"}, trusted=False),
        _data({"action": "acme"}, ticket=replace(ticket, valid=False)),
        _data({"action": "acme"}, operator="ou_mallory", ticket=ticket),
        _data({"action": "acme"}, chat="oc_other", ticket=ticket),
        _data({"action": "acme"}, message="om_other", ticket=ticket),
        _data({"action": "acme"}, ticket=ticket, admission=_admission(ticket, profile_name="")),
        _data(
            {"action": "acme"},
            ticket=ticket,
            admission=SimpleNamespace(is_authentic=lambda: True),
        ),
        _data(
            {"action": "acme"},
            ticket=ticket,
            admission=replace(_admission(ticket), _seal=object()),
        ),
    ]

    # Every rejection must be byte-identical to what an unregistered action
    # gets. Asserting only `toast_type == "error"` would have let the namespace
    # become enumerable: a registered-but-unadmitted action answering `error`
    # while a bogus one answers `unsupported` is itself the oracle (sunke's
    # ruling, 2026-08-11). Pinning the bytes covers refusal AND indistinguish-
    # ability in one assertion.
    unknown_bytes = card_response_bytes(
        adapter._on_card_action_trigger(_data(_UNKNOWN_BASELINE, trusted=False))
    )
    for data in rejected:
        assert card_response_bytes(adapter._on_card_action_trigger(data)) == unknown_bytes

    assert seen == []
    assert calls["original"] == 0
    assert adapter._on_card_action_trigger(
        _data({"action": "acme"}, token=_token())
    ) == {"kind": "business"}


# --- explicit Agent core allowlist ------------------------------------------------

_CORE_ACTIONS = ["feishu_auth", "approve_once", "approve_session", "approve_always", "deny"]


@pytest.mark.parametrize("action", _CORE_ACTIONS)
def test_listed_agent_core_actions_delegate_exactly_once(action):
    adapter, calls = _installed()
    response = adapter._on_card_action_trigger(_data({"hermes_action": action}))
    assert calls["original"] == 1
    assert response == {"kind": "delegated"}


def _spelling(name):
    """The four ways a callback can name an action, as parsed by
    ``parse_card_callback``. Core reads exactly ONE of them."""
    return {
        "value.hermes_action": dict(value={"hermes_action": name}),
        "value.action": dict(value={"action": name}),
        "action.name": dict(value=None, name=name),
        "form_name": dict(value=None, name="", form_name=name),
    }


@pytest.mark.parametrize("action", _CORE_ACTIONS)
@pytest.mark.parametrize("spelling", sorted(_spelling("x")))
def test_only_the_spelling_core_reads_may_delegate(action, spelling):
    """Core resolves an approval/auth click from ``value["hermes_action"]`` ALONE
    (``be5a764d0:gateway/platforms/feishu.py:3272``). Under any other spelling it
    falls through to ``_handle_card_action_event``, which synthesizes ``/card``
    and hands the raw callback JSON to the model — the same P0 as the removed
    bogus fifth key, through a different door. So an allowlisted NAME wearing the
    wrong spelling must be consumed here, never delegated."""
    adapter, calls = _installed()
    kwargs = dict(_spelling(action)[spelling])
    form_name = kwargs.pop("form_name", None)
    data = _data(kwargs.pop("value"), **kwargs)
    if form_name is not None:
        data.event.action.form_name = form_name

    response = adapter._on_card_action_trigger(data)

    if spelling == "value.hermes_action":
        assert calls["original"] == 1
        assert response == {"kind": "delegated"}
    else:
        assert calls["original"] == 0
        assert _kind(response) == "toast"
        assert _toast_type(response) == "info"  # consumed as unsupported


def test_a_mis_spelled_core_action_cannot_be_captured_by_a_greedy_business_matcher():
    """Consumption must happen INSIDE the core branch: falling through to the
    business scan would hand a crafted `approve_once` to whoever registered a
    permissive matcher."""
    adapter, calls = _installed()
    seen = []
    dispatcher.register_business_action(
        "greedy", lambda _a, _cb: seen.append("business") or {"kind": "business"},
        matcher=lambda _cb: True,
    )
    response = adapter._on_card_action_trigger(_data({"action": "approve_once"}))
    assert seen == []
    assert calls["original"] == 0
    assert _toast_type(response) == "info"


def test_an_unbacked_top_level_value_key_does_not_delegate():
    """``hermes_update_prompt_action`` was once delegated on the theory that core
    owns it. It does not: zero hits in ``be5a764d0:gateway/platforms/feishu.py``,
    the hermes-agent checkout, or this repo. Nothing emits it, so the only way to
    produce one is a crafted callback — and delegating would drop it into core's
    generic ``/card`` path with the raw JSON as the model prompt. Consumed."""
    adapter, calls = _installed()
    response = adapter._on_card_action_trigger(_data({"hermes_update_prompt_action": "yes"}))
    assert calls["original"] == 0
    assert calls["turns"] == []
    assert _kind(response) == "toast"
    assert _toast_type(response) == "info"  # unsupported, not a handler failure


def test_the_agent_core_allowlist_is_exactly_the_backed_core_actions():
    """Every entry must be BACKED by a real core handler; an unbacked one is the
    P0 this package exists to close. ``approve_always`` is backed — core's
    approval card emits it ("✅ Always", feishu.py:2457) and
    ``_APPROVAL_CHOICE_MAP`` (:229) resolves it — so consuming it here would
    leave a blocked agent waiting forever."""
    assert dispatcher._AGENT_CORE_ACTIONS == frozenset(_CORE_ACTIONS)
    assert dispatcher._AGENT_CORE_VALUE_KEY == "hermes_action"
    assert not hasattr(dispatcher, "_AGENT_CORE_VALUE_KEYS")


def test_agent_core_delegate_exception_is_consumed():
    calls = {"original": 0}

    class ExplodingAdapter:
        _app_id = "cli_test"

        def _on_card_action_trigger(self, data):
            calls["original"] += 1
            raise RuntimeError("core blew up")

    assert dispatcher.install_feishu_card_action_dispatcher(ExplodingAdapter) is True
    response = ExplodingAdapter()._on_card_action_trigger(_data({"hermes_action": "deny"}))
    assert calls["original"] == 1
    assert _toast_type(response) == "error"


# --- fixed precedence + namespaced registration ----------------------------------

def test_builtin_outranks_a_business_action_that_matches_the_same_callback():
    from hermes_multitenancy import feishu_auth_hub_actions

    adapter, calls = _installed()
    seen = []
    dispatcher.register_business_action(
        "greedy", lambda _a, _cb: seen.append("business") or {"kind": "business"},
        matcher=lambda _cb: True,
    )
    feishu_auth_hub_actions._handle_cred_auth_action  # built-in exists
    response = adapter._on_card_action_trigger(
        _data({"hermes_action": "cred_auth"})  # no cred → built-in's own rejection
    )
    assert seen == []
    assert calls["original"] == 0
    assert _kind(response) == "toast"


def test_duplicate_namespace_is_rejected_and_the_first_handler_keeps_ownership():
    adapter, _calls = _installed()
    dispatcher.register_business_action("acme", lambda _a, _cb: {"kind": "first"})
    with pytest.raises(ValueError):
        dispatcher.register_business_action("acme", lambda _a, _cb: {"kind": "second"})
    assert adapter._on_card_action_trigger(_data({"action": "acme"})) == {"kind": "first"}


def test_re_registering_the_same_handler_is_a_no_op():
    handler = lambda _a, _cb: {"kind": "ok"}  # noqa: E731
    dispatcher.register_business_action("acme", handler)
    dispatcher.register_business_action("acme", handler)
    assert dispatcher.registered_business_namespaces() == ("acme",)


@pytest.mark.parametrize("namespace", ["cred_auth", "clarify", "inject_prompt", "feishu_auth", "", "  "])
def test_builtin_namespaces_cannot_be_claimed_by_a_business_action(namespace):
    with pytest.raises(ValueError):
        dispatcher.register_business_action(namespace, lambda _a, _cb: None)


def test_business_matcher_claims_a_value_stripped_form_submit():
    adapter, calls = _installed()
    dispatcher.register_business_action(
        "acme",
        lambda _a, _cb: {"kind": "acme"},
        matcher=lambda cb: cb.action_name.startswith("acme_submit_"),
    )
    response = adapter._on_card_action_trigger(_data(None, name="acme_submit_7", form_value={"x": 1}))
    assert calls["original"] == 0
    assert response == {"kind": "acme"}


def test_explicit_unknown_action_cannot_reach_a_form_submit_matcher():
    """An explicit action kind is not value-stripped, so its unrelated
    ``action.name`` must not make it eligible for matcher recovery."""
    adapter, _calls = _installed()
    form_value = {
        "amount": 68,
        "date": "上周五",
        "category": "打车",
        "reason": "去客户现场",
    }

    reserved = adapter._on_card_action_trigger(
        _data(
            {"action": "inject_prompt"},
            name="push_confirm_submit_op",
            form_value=form_value,
        )
    )
    unknown = adapter._on_card_action_trigger(
        _data(
            {"action": "definitely_not_a_real_action_9f2c"},
            name="push_confirm_submit_op",
            form_value=form_value,
        )
    )

    assert card_response_bytes(unknown) == card_response_bytes(reserved)


def test_upstream_value_action_key_is_honoured_alongside_hermes_action():
    adapter, _calls = _installed()
    dispatcher.register_business_action("acme", lambda _a, _cb: {"kind": "acme"})
    # Two clicks = two deliveries = two tokens. Reusing one token would be a
    # REDELIVERY, which at-most-once refuses (see the dedupe tests below).
    assert adapter._on_card_action_trigger(
        _data({"action": "acme"}, token=_token())
    ) == {"kind": "acme"}
    assert adapter._on_card_action_trigger(
        _data({"hermes_action": "acme"}, token=_token())
    ) == {"kind": "acme"}


# --- at-most-once (P1) -------------------------------------------------------------

def test_a_redelivered_business_callback_runs_its_handler_at_most_once():
    """One callback identity → one execution. Before this guard, replay
    protection existed only for ``inject_prompt``, so a redelivered business
    callback re-ran the handler (and its backend write) every time."""
    adapter, _calls = _installed()
    ran = []
    dispatcher.register_business_action("acme", lambda _a, _cb: ran.append(1) or {"kind": "acme"})

    first = adapter._on_card_action_trigger(_data({"action": "acme"}, token="tk_same"))
    second = adapter._on_card_action_trigger(_data({"action": "acme"}, token="tk_same"))

    assert ran == [1]
    assert first == {"kind": "acme"}
    assert _toast_type(second) == "error"


def test_at_most_once_falls_back_to_the_signed_callback_tuple_without_a_token():
    """Schema-2 callbacks can arrive without a token; the signed (message, chat,
    operator, kind) tuple is the identity then."""
    adapter, _calls = _installed()
    ran = []
    dispatcher.register_business_action("acme", lambda _a, _cb: ran.append(1) or {"kind": "acme"})

    adapter._on_card_action_trigger(_data({"action": "acme"}))
    adapter._on_card_action_trigger(_data({"action": "acme"}))

    assert ran == [1]


def test_a_different_action_on_the_same_card_message_still_runs():
    """Two different buttons on one card are two callbacks, not a redelivery."""
    adapter, _calls = _installed()
    ran = []
    dispatcher.register_business_action("acme", lambda _a, _cb: ran.append("acme") or {"kind": "a"})
    dispatcher.register_business_action("beta", lambda _a, _cb: ran.append("beta") or {"kind": "b"})

    adapter._on_card_action_trigger(_data({"action": "acme"}))
    adapter._on_card_action_trigger(_data({"action": "beta"}))

    assert ran == ["acme", "beta"]


def test_at_most_once_does_not_gate_agent_core_delegation():
    """Core owns its own token guard (``_is_card_action_duplicate``) and approval
    state; double-gating here would silently break a legitimate re-issue."""
    adapter, calls = _installed()
    adapter._on_card_action_trigger(_data({"hermes_action": "approve_once"}, token="tk_core"))
    adapter._on_card_action_trigger(_data({"hermes_action": "approve_once"}, token="tk_core"))
    assert calls["original"] == 2


def test_group_reply_mode_and_push_confirm_are_registered_without_their_installers():
    """Routing must not depend on which feature installer ran — the dispatcher
    registers MT's own business actions itself."""
    adapter, _calls = _installed()
    adapter._on_card_action_trigger(_data({"hermes_action": "no_such_action"}))
    assert set(dispatcher.registered_business_namespaces()) >= {"group_reply_mode", "push_confirm"}


# --- inject_prompt: a reserved slot this package does NOT implement ----------------
#
# The implementation left this package on 2026-08-10 (sunke ratified the split).
# Two rounds of opposite-family review proved it never bound the callback's
# CLICKING OPERATOR to the ticket's actor, nor validated the admission profile:
# a valid ticket for `ou_alice` paired with callback operator `ou_mallory` still
# produced `toast=info, scheduled=1`, so a leaked card clicked by a stranger ran
# as its original owner. Rebuilding it as an unforgeable capability is the first
# Done line of slug `feishu-card-inject-prompt`.
#
# What this package still owes is the SAFE consumption of the name: the slot is
# held so no business namespace can claim it, and it answers exactly like any
# other unknown action so a crafted callback cannot learn the name was known.

_UNKNOWN_BASELINE = {"action": "definitely_not_a_real_action_9f2c"}


def _inject_variants():
    """Every way a crafted click can try to reach the removed feature —
    all four spellings, and the payloads the old implementation branched on."""
    return {
        "value.action": dict(value={"action": "inject_prompt", "prompt": "继续"}),
        "value.hermes_action": dict(value={"hermes_action": "inject_prompt", "prompt": "继续"}),
        "action.name": dict(value=None, name="inject_prompt"),
        "form_name": dict(value=None, name="", form_name="inject_prompt"),
        "no_prompt": dict(value={"action": "inject_prompt"}),
        "oversized_prompt": dict(value={"action": "inject_prompt", "prompt": "x" * 5000}),
        "payload_disagrees_with_ticket": dict(
            value={
                "action": "inject_prompt",
                "prompt": "继续",
                "chat_id": "oc_other",
                "account_id": "cli_other",
                "thread_id": "omt_claimed",
            }
        ),
        "untrusted_envelope": dict(
            value={"action": "inject_prompt", "prompt": "继续"}, trusted=False
        ),
        # The exact attack that carved the feature out: a legitimate ticket for
        # ou_alice, clicked by someone else. It must now be as inert as noise.
        "leaked_card_clicked_by_a_stranger": dict(
            value={"action": "inject_prompt", "prompt": "继续"},
            operator="ou_mallory",
            ticket=FakeTicket(actor_id="ou_alice"),
        ),
    }


def _click(adapter, spelling_kwargs, **extra):
    kwargs = dict(spelling_kwargs)
    form_name = kwargs.pop("form_name", None)
    data = _data(kwargs.pop("value"), **kwargs, **extra)
    if form_name is not None:
        data.event.action.form_name = form_name
    return adapter._on_card_action_trigger(data)


@pytest.mark.parametrize("variant", sorted(_inject_variants()))
def test_inject_prompt_is_consumed_as_unsupported_with_zero_work(variant):
    """Model, tool and turn counts all 0: the dispatcher neither delegates to
    core's generic ``/card`` path nor reaches for the normal inbound path."""
    adapter, calls = _installed()
    response = _click(adapter, _inject_variants()[variant])
    assert calls["original"] == 0, "reached core's generic /card → model path"
    assert calls["turns"] == [], "reached the inbound path — a model turn"
    assert _kind(response) == "toast"
    assert _toast_type(response) == "info"


@pytest.mark.parametrize("variant", sorted(_inject_variants()))
def test_inject_prompt_is_byte_identical_to_an_arbitrary_unknown_action(variant):
    """A crafted variant must not be able to tell from the RESPONSE that the
    action name was recognised. Compared on the wire form, not on ``==``: two
    ``P2CardActionTriggerResponse`` instances are never equal (the SDK class
    defines no ``__eq__``), so identity has to be asserted on the bytes."""
    adapter, _calls = _installed()
    baseline = card_response_bytes(adapter._on_card_action_trigger(_data(_UNKNOWN_BASELINE)))
    crafted = card_response_bytes(_click(adapter, _inject_variants()[variant]))
    assert crafted == baseline


def test_a_replayed_inject_prompt_answers_exactly_like_a_replayed_unknown():
    """The slot must NOT take an at-most-once claim. A claimed replay answers
    ``error``, an unclaimed one answers ``unsupported`` — routing the slot
    through ``_run_once`` would hand a crafted click that exact oracle."""
    adapter, _calls = _installed()
    value = {"action": "inject_prompt", "prompt": "继续"}

    first = card_response_bytes(adapter._on_card_action_trigger(_data(value, token="tk_replay")))
    second = card_response_bytes(adapter._on_card_action_trigger(_data(value, token="tk_replay")))
    unknown_first = card_response_bytes(
        adapter._on_card_action_trigger(_data(_UNKNOWN_BASELINE, token="tk_replay_u"))
    )
    unknown_second = card_response_bytes(
        adapter._on_card_action_trigger(_data(_UNKNOWN_BASELINE, token="tk_replay_u"))
    )

    assert first == second == unknown_first == unknown_second


def test_the_reserved_slot_outranks_a_greedy_business_matcher():
    """Consumption must happen in the slot, ABOVE the business scan. Below it,
    whoever registers a permissive matcher inherits the removed feature's name
    — and with it every crafted payload aimed at it."""
    adapter, calls = _installed()
    seen = []
    dispatcher.register_business_action(
        "greedy", lambda _a, _cb: seen.append("business") or {"kind": "business"},
        matcher=lambda _cb: True,
    )
    response = adapter._on_card_action_trigger(
        _data({"action": "inject_prompt", "prompt": "继续"})
    )
    assert seen == []
    assert calls["original"] == 0
    assert calls["turns"] == []
    assert _toast_type(response) == "info"


def test_this_package_no_longer_ships_an_inject_prompt_implementation():
    """The module is gone, not merely unwired — an unwired module invites the
    next installer to wire it back before the capability exists."""
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("hermes_multitenancy.feishu_card_inject_prompt")
    assert dispatcher._UNSUPPORTED_KINDS == frozenset({"inject_prompt"})
    assert "inject_prompt" not in {
        kind for _n, kinds, _m, _f in dispatcher._BUILTINS for kind in kinds
    }


def test_registered_business_namespaces_are_not_enumerable_by_response():
    """A rejected business action must answer exactly as an unknown one does.

    Found by an independent reviewer (Fable, 2026-08-11) after the first
    oracle fix: with no admission attached, `group_reply_mode` and
    `push_confirm` answered `error` while an arbitrary name answered
    `unsupported`. That difference lets a crafted caller enumerate which
    namespaces are registered — the same recognised-vs-unknown oracle this
    package removes, one door over. sunke ruled to collapse the business
    rejection onto `unsupported_response()`; the rejection stays visible to
    operators in the `kind=business outcome=rejected` log line.

    Asserts ONE distinct response across every registered namespace plus two
    arbitrary names — so adding a namespace later cannot silently reopen it.
    """
    adapter, calls = _installed()
    dispatcher._ensure_default_business_registered()
    registered = sorted(dispatcher._BUSINESS)
    assert registered, "fixture is vacuous if nothing is registered"

    probes = {
        name: card_response_bytes(
            adapter._on_card_action_trigger(_data({"action": name}, trusted=False))
        )
        for name in registered + ["totally_unknown_xyz", "zz_another_unknown_5f1a"]
    }

    assert len(set(probes.values())) == 1, (
        "a registered namespace is distinguishable from an unknown one: "
        f"{ {k: v for k, v in probes.items()} }"
    )
    assert calls["original"] == 0
