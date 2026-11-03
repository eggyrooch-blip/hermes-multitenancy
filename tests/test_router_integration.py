"""US-007/008 integration — router actually uses RoutingTable + RuntimePool.

Architect P0 finding: previous code wrote both components but router.py never
called them. These tests assert the wiring.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


def _build_event(text: str = "hi", user_id: str = "ou_intg", chat_id: str = "chat-i"):
    return SimpleNamespace(
        text=text,
        source=SimpleNamespace(
            chat_id=chat_id,
            user_id=user_id,
            user_name="intg-user",
            chat_type="dm",
            platform=SimpleNamespace(value="feishu"),
        ),
    )


@pytest.mark.asyncio
async def test_router_uses_sqlite_routing_table(tmp_path, monkeypatch):
    """router.handle_async must consult RoutingTable for the sender's profile_name."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.runtime import clear_spike_routes
    from hermes_multitenancy import runtime as runtime_mod

    clear_spike_routes()
    router_mod._user_inflight_tasks.clear()

    # Point router at a fresh in-memory routing db
    router_mod.override_routing_table(":memory:")
    router_mod.override_pool(None)  # reset pool too

    # Seed the table — open_id 'ou_router_test' → profile_name 'spike-test'
    table = router_mod._get_routing_table()
    profile_dir = tmp_path / "profiles" / "spike-test"
    profile_dir.mkdir(parents=True)
    table.upsert(
        user_id="u_router",
        profile_name="spike-test",
        open_id="ou_router_test",
        union_id="on_router",
    )

    # Force _profile_name_to_home to point inside tmp_path so we don't touch ~/.hermes
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda name: tmp_path / "profiles" / name,
    )

    # Capture which profile the runner saw
    seen_homes = []

    async def capture_runner(event, home):
        seen_homes.append(home)
        return f"profile={home.name}"

    monkeypatch.setattr(runtime_mod, "_default_run_agent", capture_runner)

    sends = []

    class Adapter:
        async def send_typing(self, c): pass
        async def send(self, c, m, *, reply_to=None, metadata=None):
            sends.append(m)

    gateway = SimpleNamespace(adapters={"feishu": Adapter()})
    await router_mod.handle_async(event=_build_event(user_id="ou_router_test"), gateway=gateway)

    # Runner should have been invoked with the SQLite-routed profile_home
    assert seen_homes == [tmp_path / "profiles" / "spike-test"]
    assert any("profile=spike-test" in s for s in sends), sends

    # And touch_active should have updated last_active_at
    row = table.lookup_by_open_id("ou_router_test")
    assert row is not None
    assert row.last_active_at is not None

    # Cleanup
    router_mod.override_routing_table(None)
    router_mod.override_pool(None)


@pytest.mark.asyncio
async def test_router_uses_runtime_pool(tmp_path, monkeypatch):
    """Two dispatches for the same profile must reuse the pool entry."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.pool import RuntimePool
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy import runtime as runtime_mod

    clear_spike_routes()
    router_mod._user_inflight_tasks.clear()
    router_mod.override_routing_table(None)

    factory_calls = []

    def counting_factory(name, home):
        from hermes_multitenancy.runtime import ProfileRuntime
        factory_calls.append(name)
        return ProfileRuntime(profile_home=home)

    pool = RuntimePool(runtime_factory=counting_factory)
    router_mod.override_pool(pool)

    async def stub_runner(event, home):
        return "ok"

    monkeypatch.setattr(runtime_mod, "_default_run_agent", stub_runner)

    profile = tmp_path / "shared"
    profile.mkdir()
    add_spike_route("ou_pool", profile)

    class Adapter:
        async def send_typing(self, c): pass
        async def send(self, c, m, *, reply_to=None, metadata=None): pass

    gateway = SimpleNamespace(adapters={"feishu": Adapter()})

    # Two dispatches for the same user
    await router_mod.handle_async(event=_build_event(user_id="ou_pool"), gateway=gateway)
    await router_mod.handle_async(event=_build_event(user_id="ou_pool", text="again"), gateway=gateway)

    # Factory should have run exactly once — pool reused the entry
    assert factory_calls == ["shared"], (
        f"expected pool to reuse runtime, factory called {factory_calls}"
    )

    clear_spike_routes()
    router_mod.override_pool(None)


@pytest.mark.asyncio
async def test_router_falls_back_to_spike_routing_when_table_misses(tmp_path, monkeypatch):
    """If SQLite has no row for the sender, router falls back to _SPIKE_ROUTING."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy import runtime as runtime_mod

    clear_spike_routes()
    router_mod._user_inflight_tasks.clear()

    # Empty routing table
    router_mod.override_routing_table(":memory:")
    router_mod.override_pool(None)

    # Spike route claims this sender
    profile = tmp_path / "fallback-profile"
    profile.mkdir()
    add_spike_route("ou_fallback", profile)

    seen = []
    async def runner(event, home):
        seen.append(home)
        return "from-fallback"

    monkeypatch.setattr(runtime_mod, "_default_run_agent", runner)

    sends = []
    class Adapter:
        async def send_typing(self, c): pass
        async def send(self, c, m, *, reply_to=None, metadata=None): sends.append(m)

    gateway = SimpleNamespace(adapters={"feishu": Adapter()})
    await router_mod.handle_async(event=_build_event(user_id="ou_fallback"), gateway=gateway)

    assert seen == [profile]
    assert any("from-fallback" in s for s in sends), sends

    clear_spike_routes()
    router_mod.override_routing_table(None)
    router_mod.override_pool(None)


# ===== push-card fill loop routed INSIDE handle_async (router bypasses =====
# ===== FeishuAdapter._dispatch_inbound_event, so the matcher patch there =====
# ===== never fires — this is the live integration seam) ==================

def _pushcard_gateway():
    """A Feishu gateway whose adapter carries both the router send surface and
    the ``_feishu_send_with_retry`` the fill loop's proactive send uses."""
    sent: list[tuple[str, dict]] = []

    class Adapter:
        async def send_typing(self, c): pass
        async def send(self, c, m, *, reply_to=None, metadata=None): pass
        async def _feishu_send_with_retry(self, *, chat_id, msg_type, payload,
                                          reply_to=None, metadata=None):
            import json as _json
            sent.append((chat_id, _json.loads(payload)))
            return {"message_id": f"om_out_{len(sent)}"}

    return SimpleNamespace(adapters={"feishu": Adapter()}), sent


def _seed_pending_push_card(reg, *, open_id="ou_alice", mid="om_card"):
    rid = reg.get_registry_store().create(
        scene="dev-acceptance-claim", skill="push-fill-form",
        target_open_id=open_id, profile_name=f"{open_id}-profile",
        business_key=f"dev-acceptance-claim:{open_id}:2026-07-20",
    ).row["registry_id"]
    reg.get_registry_store().mark_sent(rid, message_id=mid)  # → pending, message_id set
    return rid


@pytest.mark.asyncio
async def test_router_routes_quoted_push_card_reply_and_short_circuits(tmp_path, monkeypatch):
    """A DM reply quoting an open push card is routed into the fill loop from
    inside handle_async (registry → clarifying, clarify card sent) and the normal
    agent is NEVER invoked — reproduces the live seam where the router bypasses
    _dispatch_inbound_event so the matcher patch could not fire."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy import runtime as runtime_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy import push_card_matcher as matcher
    from hermes_multitenancy import push_registry as reg
    from hermes_multitenancy import push_send_queue as sendq

    clear_spike_routes()
    router_mod._user_inflight_tasks.clear()
    router_mod.override_routing_table(None)
    router_mod.override_pool(None)

    store = reg.PushRegistryStore(":memory:")
    reg.override_registry_store(store)
    matcher.override_matcher(None)
    rid = _seed_pending_push_card(reg, open_id="ou_alice", mid="om_card")

    agent_calls: list = []
    async def capture_runner(event, home):
        agent_calls.append(home)
        return "agent-ran"
    monkeypatch.setattr(runtime_mod, "_default_run_agent", capture_runner)

    # Give ou_alice a resolvable profile so the agent path is genuinely reachable
    # (proving the short-circuit, not a routing miss, is what stops the agent).
    profile = tmp_path / "alice-profile"
    profile.mkdir()
    add_spike_route("ou_alice", profile)

    gateway, sent = _pushcard_gateway()
    ev = _build_event(user_id="ou_alice", text="今天打车去客户现场花了58")
    ev.sender_open_id = "ou_alice"
    ev.reply_to_message_id = "om_card"  # quote-reply to the pending card

    try:
        await router_mod.handle_async(event=ev, gateway=gateway)

        # ① routed into fill loop: registry advanced + a form card sent to the DM
        assert store.get(rid)["status"] == reg.STATUS_CLARIFYING
        assert len(sent) == 1 and sent[0][0] == "ou_alice"
        assert any(e.get("tag") == "form" for e in sent[0][1]["body"]["elements"])
        # ② the normal streaming agent was NOT invoked (short-circuit)
        assert agent_calls == []
    finally:
        clear_spike_routes()
        reg.override_registry_store(None)
        matcher.override_matcher(None)
        sendq.note_live_adapter(None)
        sendq._live_adapter = None
        router_mod.override_pool(None)
        store.close()


@pytest.mark.asyncio
async def test_router_non_pushcard_message_still_reaches_agent(tmp_path, monkeypatch):
    """A message that matches no open push card is NOT eaten — even with a pending
    card open for the same user, off-topic chit-chat flows to the normal agent and
    the card is left untouched (the regression contract, design §2.3)."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy import runtime as runtime_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy import push_card_matcher as matcher
    from hermes_multitenancy import push_registry as reg
    from hermes_multitenancy import push_send_queue as sendq

    clear_spike_routes()
    router_mod._user_inflight_tasks.clear()
    router_mod.override_routing_table(None)
    router_mod.override_pool(None)

    store = reg.PushRegistryStore(":memory:")
    reg.override_registry_store(store)
    matcher.override_matcher(None)
    rid = _seed_pending_push_card(reg, open_id="ou_bob", mid="om_card_b")

    agent_calls: list = []
    async def capture_runner(event, home):
        agent_calls.append(home)
        return "agent-ran"
    monkeypatch.setattr(runtime_mod, "_default_run_agent", capture_runner)

    profile = tmp_path / "bob-profile"
    profile.mkdir()
    add_spike_route("ou_bob", profile)

    gateway, sent = _pushcard_gateway()
    ev = _build_event(user_id="ou_bob", text="中午吃什么好？")  # off-topic chit-chat
    ev.sender_open_id = "ou_bob"

    try:
        await router_mod.handle_async(event=ev, gateway=gateway)

        # ③ the agent ran normally and no fill card was sent; card stays pending
        assert agent_calls == [profile]
        assert sent == []
        assert store.get(rid)["status"] == reg.STATUS_PENDING
    finally:
        clear_spike_routes()
        reg.override_registry_store(None)
        matcher.override_matcher(None)
        sendq.note_live_adapter(None)
        sendq._live_adapter = None
        router_mod.override_pool(None)
        store.close()
