"""Register-time defer → first-dispatch re-arm (the timing single-process unit
tests never exercised).

The bug: the three push-card adapter patches are installed exactly once inside
``register()``. When the runtime FeishuAdapter class is not yet importable at
that moment, ``load_feishu_adapter()`` raises and the install returns via the
``deferred`` branch without arming anything and with no retry — so the patches
stay permanently uninstalled: inbound replies never reach the fill loop, and
proactive cards never get a live adapter (parked in 'sending'). The sibling
failure (2026-07-25 prod) is a register-time install that SUCCEEDS on the wrong
class; both are rescued by the same re-arm, now that no module-level global
short-circuits it.

These tests force that exact sequence — register-time defer, then ONE dispatch
carrying a live adapter — and assert the patches are now armed on the *runtime*
adapter class, that a real inbound reply routes into the fill loop, and that the
router dispatch entry point actually reaches the re-arm.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from hermes_multitenancy import feishu_adapter_compat as compat
from hermes_multitenancy import push_card_confirm as confirm
from hermes_multitenancy import push_card_matcher as matcher
from hermes_multitenancy import push_card_metrics as metrics
from hermes_multitenancy import push_registry as reg
from hermes_multitenancy import push_send_queue as sendq

SCENE = "dev-acceptance-claim"
SKILL = "push-fill-form"


@pytest.fixture
def reset_hooks():
    """Each test patches a FRESH adapter class, so the per-function markers start
    clean with no global to reset; unwind the captured adapter / stores after."""
    s = reg.PushRegistryStore(":memory:")
    reg.override_registry_store(s)
    matcher.override_matcher(None)
    metrics.reset()
    yield s
    reg.override_registry_store(None)
    matcher.override_matcher(None)
    sendq.register_push_card_sender(None)
    sendq.note_live_adapter(None)
    sendq._live_adapter = None
    metrics.reset()


def _live_adapter_cls():
    """A FeishuAdapter-shaped class carrying all four patched surfaces."""

    class FakeAdapter:
        def __init__(self):
            self.original_dispatch_called = False
            self.card_action_original_called = False
            self.sent: list[tuple[str, dict]] = []

        async def _dispatch_inbound_event(self, event):
            self.original_dispatch_called = True
            return None

        def _on_card_action_trigger(self, data):
            self.card_action_original_called = True
            return "orig-card-action"

        def _on_message_recalled(self, data):
            return "orig-recall"

        async def _feishu_send_with_retry(self, *, chat_id, msg_type, payload, reply_to=None, metadata=None):
            self.sent.append((chat_id, json.loads(payload)))
            return {"message_id": f"om_out_{len(self.sent)}"}

    return FakeAdapter


def _defer_register_install(monkeypatch):
    """Reproduce register-time defer: load_feishu_adapter raises (synthetic
    runtime module not loaded yet) → both installs take the deferred branch."""
    attempts = []

    def _boom():
        attempts.append(1)
        raise ModuleNotFoundError("No module named 'hermes_plugins.feishu_platform.adapter'")

    monkeypatch.setattr(compat, "load_feishu_adapter", _boom)
    matcher.install_feishu_push_card_matcher_patch()
    confirm.install_feishu_push_card_confirm_patch()
    # Both installs hit the deferred branch and armed nothing — the
    # permanent-uninstall bug the re-arm path exists to rescue.
    assert len(attempts) == 2


def test_rearm_arms_both_patches_on_runtime_class(reset_hooks, monkeypatch):
    _defer_register_install(monkeypatch)

    adapter = _live_adapter_cls()()
    sendq.ensure_push_card_adapter_patches_armed(adapter)

    cls = type(adapter)
    # matcher: inbound dispatch + recall patched on the runtime class
    assert getattr(cls._dispatch_inbound_event, matcher._DISPATCH_FLAG, False)
    assert getattr(cls._on_message_recalled, matcher._RECALL_FLAG, False)
    # confirm: card-action patched on the runtime class
    assert getattr(cls._on_card_action_trigger, confirm._CARD_ACTION_FLAG, False)
    # note_live_adapter fed → proactive sender has an adapter immediately
    assert sendq.get_live_adapter() is adapter


def test_rearm_arms_a_second_class_after_a_wrong_class_install(reset_hooks, monkeypatch):
    """prod 2026-07-25→30: register() installed SUCCESSFULLY — on the wrong class.

    v0190's lazy platform loading made ``load_feishu_adapter()`` resolve to the
    fallback clone, so the register-time install armed that dead class and set the
    module-level ``_HOOK_INSTALLED`` global True. Every later re-arm against the
    live synthetic class then returned on line one, and the only designed rescue
    path in the codebase was a no-op. The per-function markers are per-class and
    already prevent double-wrapping, so the global had to go."""
    clone_cls = _live_adapter_cls()  # the fallback-import clone register() saw
    monkeypatch.setattr(compat, "load_feishu_adapter", lambda: clone_cls)
    matcher.install_feishu_push_card_matcher_patch()
    confirm.install_feishu_push_card_confirm_patch()
    assert getattr(clone_cls._dispatch_inbound_event, matcher._DISPATCH_FLAG, False)
    assert getattr(clone_cls._on_card_action_trigger, confirm._CARD_ACTION_FLAG, False)

    adapter = _live_adapter_cls()()  # the DIFFERENT class the gateway runs
    sendq.ensure_push_card_adapter_patches_armed(adapter)

    cls = type(adapter)
    assert getattr(cls._dispatch_inbound_event, matcher._DISPATCH_FLAG, False)
    assert getattr(cls._on_message_recalled, matcher._RECALL_FLAG, False)
    assert getattr(cls._on_card_action_trigger, confirm._CARD_ACTION_FLAG, False)


def test_rearm_is_idempotent(reset_hooks, monkeypatch):
    _defer_register_install(monkeypatch)
    adapter = _live_adapter_cls()()
    sendq.ensure_push_card_adapter_patches_armed(adapter)
    wrapped = type(adapter)._dispatch_inbound_event
    sendq.ensure_push_card_adapter_patches_armed(adapter)  # second dispatch
    # already armed → not re-wrapped
    assert type(adapter)._dispatch_inbound_event is wrapped


def test_router_dispatch_entry_reaches_rearm(reset_hooks, monkeypatch):
    """The re-arm落点 is on_pre_gateway_dispatch — prove the router helper pulls
    the live adapter off the gateway and arms the patches."""
    from hermes_multitenancy import router

    _defer_register_install(monkeypatch)
    adapter = _live_adapter_cls()()
    gateway = SimpleNamespace(adapters={"feishu": adapter})

    router._rearm_push_card_adapter_patches(gateway)

    assert getattr(type(adapter)._dispatch_inbound_event, matcher._DISPATCH_FLAG, False)
    assert getattr(type(adapter)._on_card_action_trigger, confirm._CARD_ACTION_FLAG, False)
    assert sendq.get_live_adapter() is adapter


def test_reply_hits_fill_loop_after_rearm(reset_hooks, monkeypatch):
    """After re-arm, a real inbound reply to a pending card routes into the fill
    loop and is CONSUMED — the exact behaviour that was silently dead."""
    store = reset_hooks
    _defer_register_install(monkeypatch)

    adapter = _live_adapter_cls()()
    sendq.ensure_push_card_adapter_patches_armed(adapter)

    rid = store.create(scene=SCENE, skill=SKILL, target_open_id="ou_alice",
                       profile_name="ou_alice-profile",
                       business_key=f"{SCENE}:ou_alice:2026-07-20").row["registry_id"]
    store.mark_sent(rid, message_id="om_card")  # → pending

    ev = SimpleNamespace(sender_open_id="ou_alice",
                         text="打车 上周五 去客户现场花了58", reply_to_message_id="")
    asyncio.run(adapter._dispatch_inbound_event(ev))

    # a clarify/confirm card was sent, the reply did NOT fall through to the agent
    assert len(adapter.sent) == 1
    assert adapter.original_dispatch_called is False
    assert store.get(rid)["status"] == reg.STATUS_CLARIFYING
    assert metrics.snapshot().get(metrics.FILL_RENDERED) == 1
