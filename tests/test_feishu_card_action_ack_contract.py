"""WP02 — the card-action RESPONSE contract.

The dispatcher answers a click synchronously. This module pins what that answer
may say:

  * a rejected click returns ONE deterministic, data-free error — no reason, no
    identity, no payload, and byte-identical whatever the rejection was;
  * an unrecognised click returns ONE data-free ``unsupported`` answer, and the
    reserved-but-unimplemented ``inject_prompt`` slot is byte-identical to it,
    so a crafted click cannot learn that the name was recognised;
  * answering a click leaves no un-awaited coroutine behind and creates zero
    Run Broker / model / tool work.

There is no "accepted for processing" ACK left to assert: the only action that
scheduled work — ``inject_prompt`` — left this package on 2026-08-10 for slug
``feishu-card-inject-prompt`` (it never bound the clicking operator to the
ticket's actor). Nothing the dispatcher owns can start a turn any more, which is
what the zero-work assertions below prove.

The companion ``hermes-agent`` slug asserts its own edge behaviour natively in
``tests/gateway/``; this repo asserts the observables end-to-end through the
installed adapter, so a dead patch or a reverted agent change shows up as a red
test here rather than as a silent "the button does nothing".
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from hermes_multitenancy import feishu_card_action_dispatcher as dispatcher
from hermes_multitenancy.trusted_feishu_ingress import TrustedFeishuAdmission
from tests.conftest import card_response_bytes, card_toast


@pytest.fixture(autouse=True)
def _clean():
    dispatcher._reset_business_registry_for_tests()
    dispatcher._reset_claims_for_tests()
    yield
    dispatcher._reset_business_registry_for_tests()
    dispatcher._reset_claims_for_tests()


@dataclass(frozen=True)
class FakeTicket:
    """The ticket the adapter edge stamps onto every callback (WP01)."""

    actor_id: str = "ou_alice"
    actor_id_type: str = "open_id"
    account_id: str = "cli_test"
    chat_id: str = "oc_dm"
    thread_id: str = ""
    message_id: str = "om_1"

    def is_valid(self, *, account_id: str) -> bool:
        return account_id == self.account_id


def _admission(ticket):
    return TrustedFeishuAdmission(
        profile_name="profile_a",
        route_version=1,
        actor_id=ticket.actor_id,
        actor_id_type=ticket.actor_id_type,
        actor_subject=ticket.actor_id,
        chat_type="p2p",
        chat_id=ticket.chat_id,
        message_id=ticket.message_id,
        credential_subject=ticket.actor_id,
        tool_scope="feishu:user",
        ticket_fingerprint="fp_test",
    )


def _installed():
    """``calls["original"]`` = core's generic ``/card`` → model path.
    ``calls["turns"]`` = reaches for the normal inbound path (a model turn)."""
    calls = {"original": 0, "turns": []}

    class FakeFeishuAdapter:
        _app_id = "cli_test"
        _loop = None

        def _on_card_action_trigger(self, data):
            calls["original"] += 1
            return {"kind": "delegated"}

        def _handle_message_with_guards(self, event):
            calls["turns"].append(event)

    assert dispatcher.install_feishu_card_action_dispatcher(FakeFeishuAdapter) is True
    return FakeFeishuAdapter(), calls


def _data(value, *, chat="oc_dm", token=None, thread=None):
    context = SimpleNamespace(open_chat_id=chat, open_message_id="om_1")
    if thread is not None:
        context.open_thread_id = thread
    event = SimpleNamespace(
        action=SimpleNamespace(tag="button", name="btn", value=value, form_value=None),
        operator=SimpleNamespace(open_id="ou_alice", union_id=None, user_id=None),
        context=context,
    )
    if token is not None:
        event.token = token
    ticket = FakeTicket(chat_id=chat, thread_id=thread or "")
    return SimpleNamespace(
        event=event,
        trusted_feishu_ingress_ticket=ticket,
        trusted_feishu_ingress_admission=_admission(ticket),
    )


SECRET = "ou_secret_operator_9f2c"


def test_error_response_is_deterministic_and_carries_no_data(monkeypatch):
    """Different rejections, one answer — the response leaks nothing about which
    guard fired, who clicked, or what the payload held."""
    from hermes_multitenancy import feishu_auth_hub_actions

    adapter, calls = _installed()

    def boom(*_a, **_k):
        raise RuntimeError(f"failed for {SECRET}")

    monkeypatch.setattr(feishu_auth_hub_actions, "_handle_cred_auth_action", boom)
    dispatcher.register_business_action(
        "acme", lambda _a, _cb: (_ for _ in ()).throw(RuntimeError(f"acme hates {SECRET}"))
    )

    # The first click of the replay pair RUNS the handler; only its redelivery
    # is a rejection, so the pair is issued here and only the second collected.
    adapter._on_card_action_trigger(_data({"action": "acme"}, token="tk_dup"))

    responses = [
        # a built-in that raised
        adapter._on_card_action_trigger(_data({"hermes_action": "cred_auth", "cred": SECRET})),
        # a registered business handler that raised
        adapter._on_card_action_trigger(_data({"action": "acme", "note": SECRET})),
        # a redelivery of an already-claimed callback
        adapter._on_card_action_trigger(_data({"action": "acme"}, token="tk_dup")),
    ]

    assert len({card_response_bytes(r) for r in responses}) == 1
    toasts = [card_toast(r) for r in responses]
    assert toasts[0]["type"] == "error"
    for toast in toasts:
        assert SECRET not in toast["content"]
        assert "acme" not in toast["content"]
        assert "cred_auth" not in toast["content"]
    assert calls["original"] == 0
    assert calls["turns"] == []


def test_unsupported_response_is_informational_and_data_free():
    adapter, calls = _installed()
    toast = card_toast(
        adapter._on_card_action_trigger(_data({"hermes_action": f"unknown_{SECRET}"}))
    )
    assert toast["type"] == "info"
    assert SECRET not in toast["content"]
    assert calls["original"] == 0
    assert calls["turns"] == []


def test_the_reserved_inject_prompt_slot_answers_like_any_unknown_action():
    """The response contract side of the removal: ``inject_prompt`` is held as a
    slot (so no business namespace can claim it) but is not implemented here,
    and its answer is byte-identical to an arbitrary unknown action's."""
    adapter, calls = _installed()
    unknown = card_response_bytes(
        adapter._on_card_action_trigger(_data({"action": "no_such_action_at_all"}))
    )
    reserved = card_response_bytes(
        adapter._on_card_action_trigger(_data({"action": "inject_prompt", "prompt": "继续"}))
    )
    assert reserved == unknown
    assert calls["original"] == 0
    assert calls["turns"] == []


def test_the_error_and_unsupported_answers_are_distinct_from_each_other():
    """Negative control for the byte-identity assertions above: they would pass
    vacuously if every dispatcher answer were the same string."""
    adapter, _calls = _installed()
    dispatcher.register_business_action(
        "acme", lambda _a, _cb: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    failed = card_response_bytes(adapter._on_card_action_trigger(_data({"action": "acme"})))
    unknown = card_response_bytes(adapter._on_card_action_trigger(_data({"action": "nope"})))
    assert failed != unknown


@pytest.mark.parametrize(
    "value",
    [
        {"action": "inject_prompt", "prompt": "继续"},
        {"hermes_action": "cred_auth", "cred": "lark-cli"},
        {"action": "totally_unknown_xyz"},
        None,
    ],
    ids=["reserved_slot", "builtin", "unknown", "value_less"],
)
def test_answering_a_click_leaves_no_un_awaited_coroutine(value):
    """The old ``inject_prompt`` path built a ``_handle_message_with_guards``
    coroutine before checking the loop, so a forced submission failure left it
    un-awaited (``RuntimeWarning: coroutine was never awaited``). Nothing the
    dispatcher owns constructs a coroutine any more; this keeps it that way."""
    adapter, calls = _installed()
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        response = adapter._on_card_action_trigger(_data(value))
    assert calls["original"] == 0
    assert calls["turns"] == []
    assert card_toast(response)["type"] == "info"
