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
