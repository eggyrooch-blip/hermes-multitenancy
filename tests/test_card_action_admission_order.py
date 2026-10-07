"""Card-action dispatcher vs. trusted Feishu ingress: install order.

On core 0.21.4 MT's own ingress layer (``feishu_ingress_compat``) signs the
ticket and admits the actor inside ``FeishuAdapter._on_card_action_trigger``.
The card-action dispatcher must sit BENEATH it: business handlers check the
admission the ingress attached, and built-in handlers must never run on a
callback the ingress has not admitted. These tests install the whole stack in
``plugin_entry.register``'s order (ingress first, then the dispatcher, then the
four re-arm call sites) and in the reverse order, and click real-shaped
callbacks through the SDK-registered callback and the webhook transport.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace as NS

import pytest

from hermes_multitenancy import feishu_card_action_dispatcher as dispatcher
from hermes_multitenancy import router
from hermes_multitenancy import trusted_feishu_ingress as ingress
from hermes_multitenancy.routing import RoutingTable


@pytest.fixture
def stack(tmp_path, monkeypatch):
    from hermes_cli import __version__

    if tuple(int(part) for part in __version__.split(".")[:3]) < (0, 21, 3):
        pytest.skip("MT-owned Feishu ingress only exists on core >= 0.21.3")
    from plugins.platforms.feishu import adapter as stock

    from hermes_multitenancy import (
        feishu_adapter_compat,
        feishu_auth_hub_actions,
        feishu_clarify_cards,
        feishu_group_valve,
        push_card_confirm,
    )

    table = RoutingTable(":memory:")
    table.upsert(user_id="u_a", profile_name="profile_a", open_id="ou_a", union_id="on_a")
    (tmp_path / "profile_a").mkdir()
    monkeypatch.setattr(router, "_routing_table", table)
    monkeypatch.setattr(router, "_profile_name_to_home", lambda profile: tmp_path / profile)

    class Adapter(stock.FeishuAdapter):
        pass

    core_calls = []
    monkeypatch.setattr(Adapter, "_on_card_action_trigger", lambda self, data: core_calls.append(data))
    module = NS(FeishuAdapter=Adapter)
    monkeypatch.setattr(ingress, "load_live_feishu_module", lambda: module)
    monkeypatch.setattr(ingress, "load_feishu_module", lambda: module)
    monkeypatch.setattr(feishu_adapter_compat, "load_feishu_adapter", lambda: Adapter)
    monkeypatch.setattr(feishu_clarify_cards, "load_feishu_adapter", lambda: Adapter)
    monkeypatch.setattr(feishu_clarify_cards, "_HOOK_INSTALLED", False, raising=False)

    handled = {"group_reply_mode": [], "push_confirm": [], "clarify": [], "cred_auth": []}

    def recorder(name):
        def handler(adapter, cb):
            handled[name].append(cb)
            return {"toast": {"type": "info", "content": name}}
        return handler

    monkeypatch.setattr(
        feishu_group_valve, "_handle_group_reply_mode_card_action", recorder("group_reply_mode")
    )
    monkeypatch.setattr(push_card_confirm, "_handle_push_confirm_card_action", recorder("push_confirm"))
    monkeypatch.setattr(feishu_clarify_cards, "handle_clarify_card_action", recorder("clarify"))
    monkeypatch.setattr(feishu_auth_hub_actions, "handle_auth_hub_card_action", recorder("cred_auth"))

    dispatcher._reset_business_registry_for_tests()
    dispatcher._reset_claims_for_tests()
    ingress._reset_seen_for_tests()

    def install(order: str) -> None:
        from hermes_multitenancy import plugin_entry

        def ingress_step():
            plugin_entry._install_trusted_feishu_ingress()

        def dispatcher_step():
            dispatcher.install_feishu_card_action_dispatcher()

        first, second = (
            (ingress_step, dispatcher_step) if order == "register" else (dispatcher_step, ingress_step)
        )
        first()
        second()
        # The four re-arm call sites, in the order register / cron reach them.
        feishu_clarify_cards.install_feishu_clarify_card_action_patch()
        push_card_confirm.install_feishu_push_card_confirm_patch()
        feishu_group_valve._patch_on_card_action_trigger(Adapter)
        feishu_auth_hub_actions._patch_card_action(Adapter)

    adapter = object.__new__(Adapter)
    adapter._app_id = "cli_trusted"
    adapter.platform = stock.Platform.FEISHU
    yield NS(
        adapter=adapter,
        Adapter=Adapter,
        stock=stock,
        install=install,
        handled=handled,
        core_calls=core_calls,
    )
    dispatcher._reset_business_registry_for_tests()
    dispatcher._reset_claims_for_tests()
    ingress._reset_seen_for_tests()
    table.close()


def _click(action: str, *, actor: str = "ou_a", event_id: str, extra: dict | None = None):
    value = {"action": action, **(extra or {})}
    return NS(
        header=NS(event_id=event_id),
        event=NS(
            operator=NS(open_id=actor),
            token=f"tok-{event_id}",
            context=NS(open_chat_id="oc_dm", open_message_id=f"om_{event_id}"),
            action=NS(tag="button", value=json.dumps(value), form_value=None, name=""),
        ),
    )


def _sdk_card_callback(stack, monkeypatch):
    """The callback the lark SDK actually holds: bound at _build_event_handler."""
    callbacks = {}

    class Builder:
        def __getattr__(self, name):
            def register(*args):
                callbacks[args[0] if name == "register_p2_customized_event" else name] = args[-1]
                return self
            return register

        def build(self):
            return self

    monkeypatch.setattr(stack.stock, "EventDispatcherHandler", NS(builder=lambda *_a: Builder()))
    stack.adapter._encrypt_key = ""
    stack.adapter._verification_token = ""
    stack.adapter._build_event_handler()
    return callbacks["register_p2_card_action_trigger"]


def _webhook_card_callback(stack):
    from hermes_multitenancy.feishu_ingress_compat import _transport

    def call(data):
        token = _transport.set("webhook")
        try:
            return stack.adapter._on_card_action_trigger(data)
        finally:
            _transport.reset(token)

    return call


@pytest.fixture(params=["sdk", "webhook"])
def entry(request, stack, monkeypatch):
    """Resolve the live callback AFTER installation, as the gateway does: the
    SDK binds ``self._on_card_action_trigger`` when it connects, which is after
    every plugin has registered."""
    if request.param == "sdk":
        return lambda: _sdk_card_callback(stack, monkeypatch)
    return lambda: _webhook_card_callback(stack)


@pytest.mark.parametrize("order", ["register", "reverse"])
@pytest.mark.parametrize("action", ["group_reply_mode", "push_confirm"])
def test_business_button_reaches_its_handler_exactly_once(stack, entry, order, action):
    stack.install(order)
    entry = entry()
    evt = f"{action}-{order}"

    entry(_click(action, event_id=evt, extra={"mode": "mention"}))
    assert len(stack.handled[action]) == 1, "valid business click must reach its handler"
    cb = stack.handled[action][0]
    admission = dispatcher._read(cb.data, "trusted_feishu_ingress_admission")
    assert admission is not None and admission.profile_name == "profile_a"

    entry(_click(action, event_id=evt, extra={"mode": "mention"}))  # replay
    assert len(stack.handled[action]) == 1, "a replayed callback must not run twice"

    entry(_click(action, actor="ou_unknown", event_id=f"{evt}-stranger"))
    assert len(stack.handled[action]) == 1, "an unadmitted actor must not reach the handler"
    assert stack.core_calls == [], "business clicks never fall through to core"


@pytest.mark.parametrize("order", ["register", "reverse"])
@pytest.mark.parametrize("action", ["clarify", "cred_auth"])
def test_builtin_button_runs_only_after_ingress_admission(stack, entry, order, action):
    stack.install(order)
    entry = entry()
    evt = f"{action}-{order}"

    entry(_click(action, actor="ou_unknown", event_id=f"{evt}-stranger"))
    assert stack.handled[action] == [], "built-ins must not bypass ingress admission"

    entry(_click(action, event_id=evt))
    assert len(stack.handled[action]) == 1
    cb = stack.handled[action][0]
    assert dispatcher._read(cb.data, "trusted_feishu_ingress_ticket") is not None


@pytest.mark.parametrize("order", ["register", "reverse"])
def test_ingress_is_the_outermost_layer_with_one_dispatcher_beneath(stack, order):
    stack.install(order)
    stack.install(order)  # every installer is re-run: still one layer each

    outer = stack.Adapter._on_card_action_trigger
    assert getattr(outer, "_mt_trusted_ingress_callback", False) is True
    inner = outer._mt_trusted_inner
    assert getattr(inner, dispatcher._DISPATCHER_FLAG, False) is True
    assert not getattr(inner.__wrapped__, dispatcher._DISPATCHER_FLAG, False)
    assert not getattr(inner.__wrapped__, "_mt_trusted_ingress_callback", False)


def test_dispatcher_install_log_is_emitted_beneath_ingress(stack, caplog):
    """The install line still fires when the dispatcher arrives after ingress
    (deferred install / re-arm), so it is visible once gateway logging exists."""
    from hermes_multitenancy import plugin_entry

    plugin_entry._install_trusted_feishu_ingress()
    with caplog.at_level(logging.INFO, logger=dispatcher.__name__):
        assert dispatcher.install_feishu_card_action_dispatcher() is True
    assert "[card_action] installed the card-action dispatcher" in caplog.text
    assert "beneath trusted ingress" in caplog.text


@pytest.mark.parametrize("action", ["group_reply_mode", "push_confirm"])
def test_business_click_logs_dispatched_once_and_never_on_replay_or_rejection(
    stack, monkeypatch, caplog, action
):
    """The per-click line is the observable proof the dispatcher is live: the
    install line is emitted before gateway logging exists on core 0.21.4."""
    stack.install("register")
    entry = _sdk_card_callback(stack, monkeypatch)
    dispatched = f"kind=business namespace={action} outcome=dispatched"

    with caplog.at_level(logging.INFO, logger=dispatcher.__name__):
        entry(_click(action, event_id=f"{action}-log"))
    assert caplog.text.count(dispatched) == 1

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=dispatcher.__name__):
        entry(_click(action, event_id=f"{action}-log"))  # replay: ingress refuses it
        dispatcher.dispatch_card_action(  # unadmitted raw callback reaching the dispatcher
            stack.adapter, _click(action, event_id=f"{action}-raw"), lambda *_a: None
        )
    assert dispatched not in caplog.text
    assert "kind=business outcome=rejected" in caplog.text
    assert len(stack.handled[action]) == 1
