"""US-002 verification: hook callback fires asyncio.create_task and returns skip."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


def _build_event(
    text: str = "hi",
    chat_id: str = "chat-123",
    user_id: str = "ou_test",
    sender_open_id: str | None = None,
):
    """Build a minimal MessageEvent-shaped object for hook testing."""
    event = SimpleNamespace(
        text=text,
        source=SimpleNamespace(
            chat_id=chat_id,
            user_id=user_id,
            user_name="test-user",
            chat_type="dm",
            platform=SimpleNamespace(value="feishu"),
        ),
    )
    if sender_open_id is not None:
        event.sender_open_id = sender_open_id
    return event


@pytest.mark.asyncio
async def test_hook_returns_skip_action():
    """callback returns {action: skip} so Hermes main flow halts."""
    from hermes_multitenancy import on_pre_gateway_dispatch

    event = _build_event()
    gateway = SimpleNamespace(adapters={})  # no adapter — handle_async will no-op

    result = on_pre_gateway_dispatch(event=event, gateway=gateway, session_store=None)

    assert isinstance(result, dict)
    assert result.get("action") == "skip"
    assert "reason" in result
    # Drain any tasks that were scheduled so pytest-asyncio doesn't warn
    await asyncio.sleep(0)


def test_startup_watch_starts_cron_worker_when_adapters_ready(monkeypatch):
    """The plugin startup watcher initializes cron without Feishu inbound."""
    from hermes_multitenancy import cron_worker

    calls = []
    monkeypatch.setattr(
        cron_worker,
        "ensure_cron_worker_started",
        lambda gateway: calls.append(gateway),
    )
    gateway = SimpleNamespace(adapters={"feishu": object()})

    asyncio.run(cron_worker._start_worker_when_adapters_ready(gateway, attempts=1))

    assert calls == [gateway]


def test_cron_delivery_patch_resolves_owner_open_id(monkeypatch):
    """Bare deliver=feishu can target the WebUI owner's Feishu open_id."""
    import sys
    import types

    from hermes_multitenancy import cron_worker

    cron_pkg = types.ModuleType("cron")
    scheduler = types.ModuleType("cron.scheduler")

    def original_resolver(_job, _deliver_value):
        return None

    scheduler._resolve_single_delivery_target = original_resolver
    cron_pkg.scheduler = scheduler
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)

    cron_worker._patch_scheduler_owner_open_id_delivery()

    target = scheduler._resolve_single_delivery_target(
        {"deliver": "feishu", "owner_open_id": "ou_test_owner"},
        "feishu",
    )

    assert target == {
        "platform": "feishu",
        "chat_id": "ou_test_owner",
        "thread_id": None,
    }


def test_cron_delivery_mirror_persists_owner_context(tmp_path, monkeypatch):
    """Successful cron delivery is remembered for the owner's next Feishu turn."""
    from hermes_multitenancy import cron_worker
    from hermes_multitenancy.router import override_session_store
    from hermes_multitenancy.sessions import SessionStore

    store = SessionStore(tmp_path / "multitenancy.db")
    override_session_store(store)
    try:
        cron_worker._mirror_cron_delivery_to_owner(
            {
                "id": "job123",
                "name": "Daily summary",
                "owner_profile": "sunke",
                "owner_open_id": "ou_test_owner",
            },
            "summary content",
        )

        messages = store.load_recent("sunke", "ou_test_owner", 5)
        assert messages == [{
            "role": "assistant",
            "content": (
                "[Scheduled task delivery]\n"
                "Task: Daily summary\n"
                "Job ID: job123\n\n"
                "summary content"
            ),
        }]
    finally:
        override_session_store(None)
        store.close()


@pytest.mark.asyncio
async def test_hook_schedules_background_task():
    """callback creates a background asyncio task (fire-and-forget)."""
    from hermes_multitenancy import on_pre_gateway_dispatch
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from pathlib import Path
    import tempfile

    clear_spike_routes()
    with tempfile.TemporaryDirectory() as tmp:
        add_spike_route("ou_test_bg", Path(tmp))

        event = _build_event(user_id="ou_test_bg")

        # Track adapter calls so we can verify the task ran
        send_typing_calls = []
        send_calls = []

        class MockAdapter:
            async def send_typing(self, chat_id):
                send_typing_calls.append(chat_id)

            async def send(self, chat_id, content, *, reply_to=None, metadata=None):
                send_calls.append((chat_id, content))

        gateway = SimpleNamespace(adapters={"feishu": MockAdapter()})

        # Count tasks before
        before = len(asyncio.all_tasks())

        result = on_pre_gateway_dispatch(event=event, gateway=gateway, session_store=None)
        # Skip is returned synchronously
        assert result["action"] == "skip"

        # A new task was scheduled
        after = len(asyncio.all_tasks())
        assert after > before, f"expected >1 task scheduled (before={before}, after={after})"

        # Let the task run
        await asyncio.sleep(0.05)

        # Adapter should have received both calls (full loop runs)
        assert len(send_typing_calls) == 1
        assert send_typing_calls[0] == "chat-123"
        assert len(send_calls) == 1
        assert send_calls[0][0] == "chat-123"

    clear_spike_routes()


@pytest.mark.asyncio
async def test_hook_defers_gateway_processing_complete_for_routed_message(monkeypatch):
    """Base gateway completion must not remove Feishu Typing while router task runs."""
    from hermes_multitenancy import on_pre_gateway_dispatch
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from pathlib import Path
    import tempfile

    clear_spike_routes()
    with tempfile.TemporaryDirectory() as tmp:
        add_spike_route("ou_defer", Path(tmp))
        event = _build_event(user_id="ou_defer")
        event.message_id = "om_defer"
        calls = []

        class MockAdapter:
            def defer_processing_complete(self, ev):
                calls.append(("defer", ev.message_id))

        async def fake_handle_async(*, event, gateway):
            calls.append(("handle", getattr(event, "message_id", None)))

        monkeypatch.setattr("hermes_multitenancy.router.handle_async", fake_handle_async)

        gateway = SimpleNamespace(adapters={"feishu": MockAdapter()})
        result = on_pre_gateway_dispatch(event=event, gateway=gateway, session_store=None)
        await asyncio.sleep(0)

        assert result["action"] == "skip"
        assert calls == [("defer", "om_defer"), ("handle", "om_defer")]

    clear_spike_routes()


@pytest.mark.asyncio
async def test_handle_async_uses_real_open_id_for_explicit_profile_route(monkeypatch, tmp_path):
    """A known Feishu user must route by real ou_* open_id, not SDK short user_id."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import clear_spike_routes

    clear_spike_routes()
    db_path = tmp_path / "routing.db"
    router_mod.override_routing_table(db_path)
    table = router_mod._get_routing_table()
    table.upsert(
        user_id="sunke",
        profile_name="coder",
        open_id="ou_sunke",
        union_id="on_sunke",
    )

    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )

    event = _build_event(user_id="g41a5b5g", sender_open_id="ou_sunke")
    event.source.user_id_alt = "on_sunke"

    dispatched = {}

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            dispatched["profile_name"] = profile_name
            dispatched["profile_home"] = profile_home
            return "ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    await router_mod.handle_async(event=event, gateway=SimpleNamespace(adapters={}))

    assert dispatched == {
        "profile_name": "coder",
        "profile_home": tmp_path / "profiles" / "coder",
    }
    assert table.lookup_by_open_id("g41a5b5g") is None

    router_mod.override_routing_table(None)


@pytest.mark.asyncio
async def test_handle_async_auto_provisions_new_user_to_distinct_profile(monkeypatch, tmp_path):
    """An unseen Feishu sender must get a dedicated profile and continue dispatching."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.runtime import clear_spike_routes

    clear_spike_routes()
    db_path = tmp_path / "routing.db"
    (tmp_path / "config.yaml").write_text(
        "model:\n"
        "  default: glm-5.1\n"
        "  provider: zai\n"
        "platform_toolsets:\n"
        "  feishu:\n"
        "    - feishu_docx\n"
        "platforms:\n"
        "  feishu:\n"
        "    enabled: true\n"
        "    extra:\n"
        "      app_id: test-app\n"
        "      app_secret: test-secret\n",
        encoding="utf-8",
    )
    (tmp_path / "auth.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".env").write_text("ZAI_API_KEY=test-key\n", encoding="utf-8")
    router_mod.override_routing_table(db_path)
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )

    event = _build_event(user_id="ou_new_user")
    event.source.user_id_alt = "on_new_user"

    sent = []
    typing = []
    dispatched = {}

    class MockAdapter:
        async def send_typing(self, chat_id):
            typing.append(chat_id)

        async def send(self, chat_id, content, *, reply_to=None, metadata=None):
            sent.append((chat_id, content))

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            dispatched["profile_name"] = profile_name
            dispatched["profile_home"] = profile_home
            dispatched["text"] = agent_event.text
            return f"[{profile_name}] ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    gateway = SimpleNamespace(adapters={"feishu": MockAdapter()})

    await router_mod.handle_async(event=event, gateway=gateway)

    row = RoutingTable(db_path).lookup_by_open_id("ou_new_user")
    profile_home = tmp_path / "profiles" / "feishu_ou_new_user"

    assert row is not None
    assert row.profile_name == "feishu_ou_new_user"
    assert row.union_id == "on_new_user"
    assert row.profile_name != "coder"
    assert profile_home.is_dir()
    profile_config = (profile_home / "config.yaml")
    assert profile_config.is_file()
    assert "default: zai/glm-5.1" in profile_config.read_text(encoding="utf-8")
    assert "feishu_docx" in profile_config.read_text(encoding="utf-8")
    assert "app_id: test-app" in profile_config.read_text(encoding="utf-8")
    assert (profile_home / "auth.json").exists()
    assert (profile_home / ".env").exists()
    assert (profile_home / "SOUL.md").is_file()
    assert dispatched == {
        "profile_name": "feishu_ou_new_user",
        "profile_home": profile_home,
        "text": "hi",
    }
    assert typing == ["chat-123"]
    assert sent == [("chat-123", "[feishu_ou_new_user] ok")]

    router_mod.override_routing_table(None)


def test_auto_profile_config_does_not_invent_default_model():
    from hermes_multitenancy.router import _normalize_profile_config

    assert _normalize_profile_config({}) == {}
    assert _normalize_profile_config({"tools": ["web"]}) == {"tools": ["web"]}


@pytest.mark.asyncio
async def test_new_open_id_auto_provisions_before_stale_alt_route(monkeypatch, tmp_path):
    """A new app-scoped Feishu open_id must not be absorbed by an old union route."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.runtime import clear_spike_routes

    clear_spike_routes()
    db_path = tmp_path / "routing.db"
    (tmp_path / "config.yaml").write_text(
        "model:\n"
        "  default: zai/glm-5.1\n"
        "platform_toolsets:\n"
        "  feishu:\n"
        "    - feishu_user_info\n",
        encoding="utf-8",
    )
    router_mod.override_routing_table(db_path)
    table = router_mod._get_routing_table()
    table.upsert(
        user_id="alice",
        profile_name="spike_alice",
        open_id="on_existing_user",
        union_id="on_existing_user",
    )
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )

    event = _build_event(user_id="short_brand_new", sender_open_id="ou_brand_new")
    event.source.user_id_alt = "on_existing_user"

    dispatched = {}

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            dispatched["profile_name"] = profile_name
            dispatched["profile_home"] = profile_home
            return "ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    await router_mod.handle_async(
        event=event,
        gateway=SimpleNamespace(adapters={}),
    )

    fresh_table = RoutingTable(db_path)
    new_row = fresh_table.lookup_by_open_id("ou_brand_new")
    old_row = fresh_table.lookup_by_open_id("on_existing_user")

    assert new_row is not None
    assert new_row.profile_name == "feishu_ou_brand_new"
    assert new_row.union_id == "on_existing_user"
    assert old_row is not None
    assert old_row.profile_name == "spike_alice"
    assert dispatched["profile_name"] == "feishu_ou_brand_new"
    assert dispatched["profile_home"] == tmp_path / "profiles" / "feishu_ou_brand_new"
    assert fresh_table.lookup_by_open_id("short_brand_new") is None

    fresh_table.close()
    router_mod.override_routing_table(None)


@pytest.mark.asyncio
async def test_legacy_alt_route_used_when_real_open_id_unavailable(monkeypatch, tmp_path):
    """If no ou_* is present, a legacy alt route should win over auto-provision."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import clear_spike_routes

    clear_spike_routes()
    db_path = tmp_path / "routing.db"
    router_mod.override_routing_table(db_path)
    table = router_mod._get_routing_table()
    table.upsert(
        user_id="legacy",
        profile_name="legacy_profile",
        open_id="on_legacy_user",
        union_id="on_legacy_user",
    )
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )

    event = _build_event(user_id="short_without_open_id")
    event.source.user_id_alt = "on_legacy_user"
    dispatched = {}

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            dispatched["profile_name"] = profile_name
            dispatched["profile_home"] = profile_home
            return "ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    await router_mod.handle_async(event=event, gateway=SimpleNamespace(adapters={}))

    assert dispatched == {
        "profile_name": "legacy_profile",
        "profile_home": tmp_path / "profiles" / "legacy_profile",
    }
    assert table.lookup_by_open_id("short_without_open_id") is None

    router_mod.override_routing_table(None)


@pytest.mark.asyncio
async def test_handle_async_sets_resolved_raw_event_open_id_on_agent_event(monkeypatch, tmp_path):
    """The sender selected by router must be visible to AIAgent/subprocess payload code."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    profile_home = tmp_path / "raw-sender"
    profile_home.mkdir()
    add_spike_route("ou_raw_event_sender", profile_home)

    event = _build_event(user_id="short_sender_without_ou")
    event.raw_event = {
        "event": {
            "message": {
                "sender": {
                    "sender_id": {
                        "open_id": "ou_raw_event_sender",
                        "union_id": "on_raw_event_sender",
                    }
                }
            }
        }
    }
    captured = {}

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            captured["profile_name"] = profile_name
            captured["sender_open_id"] = getattr(agent_event, "sender_open_id", None)
            return "ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    await router_mod.handle_async(event=event, gateway=SimpleNamespace(adapters={}))

    assert captured == {
        "profile_name": "raw-sender",
        "sender_open_id": "ou_raw_event_sender",
    }

    clear_spike_routes()


@pytest.mark.asyncio
async def test_auto_provision_normalizes_existing_profile_config(monkeypatch, tmp_path):
    """Existing auto profiles are repaired if their model.default lacks provider."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import clear_spike_routes

    clear_spike_routes()
    db_path = tmp_path / "routing.db"
    (tmp_path / "config.yaml").write_text(
        "platforms:\n"
        "  feishu:\n"
        "    enabled: true\n"
        "    extra:\n"
        "      app_id: repair-app\n"
        "      app_secret: repair-secret\n",
        encoding="utf-8",
    )
    profile_home = tmp_path / "profiles" / "feishu_ou_existing"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n"
        "  default: glm-5.1\n"
        "  provider: zai\n",
        encoding="utf-8",
    )
    router_mod.override_routing_table(db_path)
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            return "ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    event = _build_event(user_id="ou_existing")
    await router_mod.handle_async(
        event=event,
        gateway=SimpleNamespace(adapters={}),
    )

    assert "default: zai/glm-5.1" in (profile_home / "config.yaml").read_text(encoding="utf-8")
    assert "app_id: repair-app" in (profile_home / "config.yaml").read_text(encoding="utf-8")

    router_mod.override_routing_table(None)


@pytest.mark.asyncio
async def test_existing_auto_profile_route_repairs_config_on_dispatch(monkeypatch, tmp_path):
    """Already-routed auto profiles are repaired before AIAgent dispatch."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import clear_spike_routes

    clear_spike_routes()
    db_path = tmp_path / "routing.db"
    (tmp_path / "config.yaml").write_text(
        "platforms:\n"
        "  feishu:\n"
        "    enabled: true\n"
        "    extra:\n"
        "      app_id: routed-repair-app\n"
        "      app_secret: routed-repair-secret\n",
        encoding="utf-8",
    )
    profile_home = tmp_path / "profiles" / "feishu_ou_existing"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n"
        "  default: glm-5.1\n"
        "  provider: zai\n",
        encoding="utf-8",
    )
    router_mod.override_routing_table(db_path)
    table = router_mod._get_routing_table()
    table.upsert(
        user_id="ou_existing",
        profile_name="feishu_ou_existing",
        open_id="ou_existing",
        union_id=None,
    )
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            return "ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    await router_mod.handle_async(
        event=_build_event(user_id="ou_existing"),
        gateway=SimpleNamespace(adapters={}),
    )

    assert "default: zai/glm-5.1" in (profile_home / "config.yaml").read_text(encoding="utf-8")
    assert "app_id: routed-repair-app" in (profile_home / "config.yaml").read_text(encoding="utf-8")

    router_mod.override_routing_table(None)


@pytest.mark.asyncio
async def test_handle_async_streams_enriched_text_to_aiagent(monkeypatch, tmp_path):
    """File-only Feishu messages have empty event.text; stream path must use enrichment."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    add_spike_route("ou_file_sender", tmp_path)

    event = _build_event(text="", user_id="ou_file_sender")
    event.message_id = "om_file"
    event.media_urls = ["/tmp/hermes-file.md"]
    event.media_types = ["text/plain"]

    captured = {}

    async def fake_enrich(event, gateway):
        return "[Content of hermes-file.md]:\nhello from uploaded file"

    async def fake_stream(adapter, chat_id, profile_name, profile_home, event, *, messages=None):
        captured["event_text"] = event.text
        captured["user_message"] = messages[-1]["content"]
        return "read it"

    class FullFeishuAdapter:
        async def on_processing_start(self, event):
            return None

        async def on_processing_complete(self, event, outcome):
            return None

        async def edit_message(self, *args, **kwargs):
            return None

    monkeypatch.setattr(router_mod, "_enrich_via_hermes_pipeline", fake_enrich)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", fake_stream)

    gateway = SimpleNamespace(adapters={"feishu": FullFeishuAdapter()})

    await router_mod.handle_async(event=event, gateway=gateway)

    assert captured["event_text"] == "[Content of hermes-file.md]:\nhello from uploaded file"
    assert captured["user_message"] == "[Content of hermes-file.md]:\nhello from uploaded file"

    clear_spike_routes()


@pytest.mark.asyncio
async def test_handle_async_reuses_gateway_media_delivery_after_stream(monkeypatch, tmp_path):
    """Multitenant streaming must reuse Hermes' native MEDIA:<path> delivery path."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    add_spike_route("ou_media_sender", tmp_path)

    event = _build_event(text="make a file", user_id="ou_media_sender")
    event.message_id = "om_media"
    outbound_path = tmp_path / "cache" / "documents" / "hermes-output.md"
    outbound_path.parent.mkdir(parents=True)
    outbound_path.write_text("profile-scoped outbound file", encoding="utf-8")

    async def fake_enrich(event, gateway):
        return "make a file"

    async def fake_stream(adapter, chat_id, profile_name, profile_home, event, *, messages=None):
        assert profile_home == tmp_path
        return f"created\nMEDIA:{outbound_path}"

    class FullFeishuAdapter:
        async def on_processing_start(self, event):
            return None

        async def on_processing_complete(self, event, outcome):
            return None

        async def edit_message(self, *args, **kwargs):
            return None

    delivered = []

    async def deliver_media(response, delivered_event, adapter):
        delivered.append(
            {
                "response": response,
                "event_text": delivered_event.text,
                "chat_id": delivered_event.source.chat_id,
                "adapter": adapter,
            }
        )

    adapter = FullFeishuAdapter()
    gateway = SimpleNamespace(
        adapters={"feishu": adapter},
        _deliver_media_from_response=deliver_media,
    )

    monkeypatch.setattr(router_mod, "_enrich_via_hermes_pipeline", fake_enrich)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", fake_stream)

    await router_mod.handle_async(event=event, gateway=gateway)

    assert delivered == [
        {
            "response": f"created\nMEDIA:{outbound_path}",
            "event_text": "make a file",
            "chat_id": "chat-123",
            "adapter": adapter,
        }
    ]

    clear_spike_routes()


@pytest.mark.asyncio
async def test_handle_async_blocks_outbound_media_outside_profile(monkeypatch, tmp_path):
    """A tenant must not be able to attach files outside its routed profile home."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    profile_home = tmp_path / "profiles" / "media-profile"
    profile_home.mkdir(parents=True)
    add_spike_route("ou_media_block", profile_home)

    outside_path = tmp_path / "other-profile" / "secret.md"
    outside_path.parent.mkdir()
    outside_path.write_text("do not send", encoding="utf-8")

    async def fake_enrich(event, gateway):
        return "make a file"

    async def fake_stream(adapter, chat_id, profile_name, profile_home, event, *, messages=None):
        return f"created\nMEDIA:{outside_path}"

    class FullFeishuAdapter:
        async def on_processing_start(self, event):
            return None

        async def on_processing_complete(self, event, outcome):
            return None

        async def edit_message(self, *args, **kwargs):
            return None

    delivered = []

    async def deliver_media(response, delivered_event, adapter):
        delivered.append(response)

    monkeypatch.setattr(router_mod, "_enrich_via_hermes_pipeline", fake_enrich)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", fake_stream)

    gateway = SimpleNamespace(
        adapters={"feishu": FullFeishuAdapter()},
        _deliver_media_from_response=deliver_media,
    )

    await router_mod.handle_async(event=_build_event(text="make a file", user_id="ou_media_block"), gateway=gateway)

    assert delivered == []

    clear_spike_routes()


@pytest.mark.asyncio
async def test_concurrent_uploaded_files_keep_profile_and_prompt_isolated(monkeypatch, tmp_path):
    """Same-named uploads from different users must not cross profile or prompt state."""
    from pathlib import Path

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    router_mod._user_inflight_tasks.clear()

    profile_a = tmp_path / "profiles" / "alice"
    profile_b = tmp_path / "profiles" / "bob"
    profile_a.mkdir(parents=True)
    profile_b.mkdir(parents=True)
    add_spike_route("ou_file_a", profile_a)
    add_spike_route("ou_file_b", profile_b)

    upload_a = tmp_path / "uploads" / "a" / "same-name.md"
    upload_b = tmp_path / "uploads" / "b" / "same-name.md"
    upload_a.parent.mkdir(parents=True)
    upload_b.parent.mkdir(parents=True)
    upload_a.write_text("MARKER_FILE_A_ONLY", encoding="utf-8")
    upload_b.write_text("MARKER_FILE_B_ONLY", encoding="utf-8")

    event_a = _build_event(text="", chat_id="chat-a", user_id="ou_file_a")
    event_a.message_id = "om_file_a"
    event_a.media_urls = [str(upload_a)]
    event_a.media_types = ["text/markdown"]
    event_b = _build_event(text="", chat_id="chat-b", user_id="ou_file_b")
    event_b.message_id = "om_file_b"
    event_b.media_urls = [str(upload_b)]
    event_b.media_types = ["text/markdown"]

    async def fake_enrich(event, gateway):
        path = Path(event.media_urls[0])
        await asyncio.sleep(0.01 if event.source.user_id == "ou_file_a" else 0)
        return f"[Content of {path.name}]:\n{path.read_text(encoding='utf-8')}"

    captured = []

    async def fake_stream(adapter, chat_id, profile_name, profile_home, event, *, messages=None):
        captured.append(
            {
                "user_id": event.source.user_id,
                "profile_name": profile_name,
                "profile_home": profile_home,
                "event_text": event.text,
                "user_message": messages[-1]["content"],
            }
        )
        return f"ok-{event.source.user_id}"

    class FullFeishuAdapter:
        async def on_processing_start(self, event):
            return None

        async def on_processing_complete(self, event, outcome):
            return None

        async def edit_message(self, *args, **kwargs):
            return None

    monkeypatch.setattr(router_mod, "_enrich_via_hermes_pipeline", fake_enrich)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", fake_stream)

    gateway = SimpleNamespace(adapters={"feishu": FullFeishuAdapter()})

    await asyncio.gather(
        router_mod.handle_async(event=event_a, gateway=gateway),
        router_mod.handle_async(event=event_b, gateway=gateway),
    )

    by_user = {item["user_id"]: item for item in captured}
    assert by_user["ou_file_a"]["profile_home"] == profile_a
    assert by_user["ou_file_b"]["profile_home"] == profile_b
    assert "MARKER_FILE_A_ONLY" in by_user["ou_file_a"]["event_text"]
    assert "MARKER_FILE_B_ONLY" not in by_user["ou_file_a"]["event_text"]
    assert "MARKER_FILE_B_ONLY" in by_user["ou_file_b"]["user_message"]
    assert "MARKER_FILE_A_ONLY" not in by_user["ou_file_b"]["user_message"]

    clear_spike_routes()
