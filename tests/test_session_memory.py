"""Session memory + reply context (Phase 3 follow-on).

Asserts that:
  - 2nd turn from the same user passes the prior turn(s) to the runner
  - reply_to_text gets spliced into the user message
  - /new (and /reset) clears the per-user history
  - history is per-(profile, user) — different users don't see each other
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


def _build_event(text: str, *, user_id: str = "ou_mem", chat_id: str = "chat-mem", reply_to_text: str | None = None):
    ev = SimpleNamespace(
        text=text,
        reply_to_text=reply_to_text,
        source=SimpleNamespace(
            chat_id=chat_id,
            user_id=user_id,
            user_id_alt=None,
            user_name="memuser",
            chat_type="dm",
            platform=SimpleNamespace(value="feishu"),
        ),
    )
    return ev


class _StreamAdapter:
    """Minimal fake FeishuAdapter exposing the streaming + reaction surface."""

    def __init__(self):
        self.sent = []
        self.edits = []
        self.reactions = []

    async def send(self, chat_id, content, *, reply_to=None, metadata=None):
        self.sent.append((chat_id, content))
        return SimpleNamespace(success=True, message_id=f"msg-{len(self.sent)}")

    async def send_typing(self, chat_id):
        return None

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        self.edits.append((message_id, content, finalize))
        return SimpleNamespace(success=True, message_id=message_id)

    async def on_processing_start(self, event):
        self.reactions.append("start")

    async def on_processing_complete(self, event, outcome):
        self.reactions.append(("complete", str(outcome)))


@pytest.mark.asyncio
async def test_session_memory_accumulates_across_turns(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real, router as router_mod
    from hermes_multitenancy.router import (
        handle_async,
        _user_inflight_tasks,
        _session_history,
    )
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    _user_inflight_tasks.clear()
    _session_history.clear()
    clear_spike_routes()

    profile_home = tmp_path / "memprofile"
    profile_home.mkdir()
    add_spike_route("ou_mem", profile_home)

    # Capture what messages the runner sees on each call
    seen_messages: list[list[dict]] = []

    async def fake_stream(event, home, *, messages=None):
        seen_messages.append(messages)
        yield ("content", f"reply-{len(seen_messages)}")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    adapter = _StreamAdapter()
    gateway = SimpleNamespace(adapters={"feishu": adapter})

    # Turn 1
    await handle_async(event=_build_event("hello"), gateway=gateway)
    # Turn 2
    await handle_async(event=_build_event("how are you"), gateway=gateway)
    # Turn 3
    await handle_async(event=_build_event("byeee"), gateway=gateway)

    # Each call's messages list captured:
    assert len(seen_messages) == 3

    # Turn 1: only the current user message
    t1 = seen_messages[0]
    assert t1 == [{"role": "user", "content": "hello"}]

    # Turn 2: prior user+assistant + current user
    t2 = seen_messages[1]
    assert t2[-1] == {"role": "user", "content": "how are you"}
    assert {"role": "user", "content": "hello"} in t2
    assert {"role": "assistant", "content": "reply-1"} in t2

    # Turn 3: full prior history
    t3 = seen_messages[2]
    assert {"role": "assistant", "content": "reply-2"} in t3

    clear_spike_routes()


@pytest.mark.asyncio
async def test_reply_context_spliced_into_user_message(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.router import handle_async, _user_inflight_tasks, _session_history
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    _user_inflight_tasks.clear()
    _session_history.clear()
    clear_spike_routes()

    profile_home = tmp_path / "rep"
    profile_home.mkdir()
    add_spike_route("ou_replyer", profile_home)

    seen = []
    async def fake_stream(event, home, *, messages=None):
        seen.append(messages)
        yield ("content", "ok")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    adapter = _StreamAdapter()
    gateway = SimpleNamespace(adapters={"feishu": adapter})

    await handle_async(
        event=_build_event("what does this mean", user_id="ou_replyer", reply_to_text="原话: foo bar"),
        gateway=gateway,
    )
    user_msg = seen[0][-1]
    assert user_msg["role"] == "user"
    assert "原话: foo bar" in user_msg["content"]
    assert "what does this mean" in user_msg["content"]

    clear_spike_routes()


@pytest.mark.asyncio
async def test_history_persists_across_simulated_restart(monkeypatch, tmp_path):
    """SessionStore-backed history survives clearing the in-memory dict (i.e. a gateway restart)."""
    from hermes_multitenancy import agent_real, router as router_mod
    from hermes_multitenancy.router import handle_async, _user_inflight_tasks, _session_history, _session_loaded
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy.sessions import SessionStore

    _user_inflight_tasks.clear()
    _session_history.clear()
    _session_loaded.clear()
    clear_spike_routes()

    # Wire a real (file-backed) SessionStore so persistence is exercised.
    db = tmp_path / "persist.db"
    router_mod.override_session_store(str(db))

    profile_home = tmp_path / "persistprof"
    profile_home.mkdir()
    add_spike_route("ou_persist", profile_home)

    seen: list[list[dict]] = []
    async def fake_stream(event, home, *, messages=None):
        seen.append(messages)
        yield ("content", f"reply-{len(seen)}")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    adapter = _StreamAdapter()
    gateway = SimpleNamespace(adapters={"feishu": adapter})

    # Turn 1 — cold cache, no prior history
    await handle_async(event=_build_event("first", user_id="ou_persist"), gateway=gateway)
    # Simulate gateway restart: drop in-memory cache + loaded set, keep DB
    _session_history.clear()
    _session_loaded.clear()
    # Turn 2 — cache is empty, but SessionStore should hydrate from disk
    await handle_async(event=_build_event("second", user_id="ou_persist"), gateway=gateway)

    # Turn 2's messages should include turn 1's user+assistant from persistent store
    assert len(seen) == 2
    t2 = seen[1]
    assert {"role": "user", "content": "first"} in t2
    assert {"role": "assistant", "content": "reply-1"} in t2
    assert t2[-1] == {"role": "user", "content": "second"}

    # Cleanup
    router_mod.override_session_store(None)
    clear_spike_routes()


@pytest.mark.asyncio
async def test_history_isolated_between_users(monkeypatch, tmp_path):
    """Two users on the same profile must have independent history."""
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.router import handle_async, _user_inflight_tasks, _session_history
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    _user_inflight_tasks.clear()
    _session_history.clear()
    clear_spike_routes()

    home = tmp_path / "shared"
    home.mkdir()
    add_spike_route("ou_a", home)
    add_spike_route("ou_b", home)

    seen = []
    async def fake_stream(event, hh, *, messages=None):
        seen.append((event.source.user_id, [m["content"] for m in messages]))
        yield ("content", f"resp-{event.source.user_id}-{len(seen)}")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    adapter = _StreamAdapter()
    gateway = SimpleNamespace(adapters={"feishu": adapter})

    await handle_async(event=_build_event("alice-1", user_id="ou_a"), gateway=gateway)
    await handle_async(event=_build_event("bob-1",   user_id="ou_b"), gateway=gateway)
    await handle_async(event=_build_event("alice-2", user_id="ou_a"), gateway=gateway)

    # Alice's 2nd turn should see only alice-1 + alice's response, never bob's text
    _user, msgs = seen[2]
    assert _user == "ou_a"
    assert any("alice-1" in m for m in msgs)
    assert all("bob" not in m for m in msgs), msgs

    clear_spike_routes()


@pytest.mark.asyncio
async def test_history_key_prefers_sender_over_shared_alt(monkeypatch, tmp_path):
    """A stale/shared alternate ID must not merge two users' memory."""
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.router import handle_async, _session_history, _user_inflight_tasks
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    _user_inflight_tasks.clear()
    _session_history.clear()
    clear_spike_routes()

    home = tmp_path / "shared-alt"
    home.mkdir()
    add_spike_route("ou_alt_a", home)
    add_spike_route("ou_alt_b", home)

    seen = []

    async def fake_stream(event, hh, *, messages=None):
        seen.append((event.source.user_id, [m["content"] for m in messages]))
        yield ("content", f"resp-{event.source.user_id}-{len(seen)}")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    adapter = _StreamAdapter()
    gateway = SimpleNamespace(adapters={"feishu": adapter})

    event_a1 = _build_event("alice-1", user_id="ou_alt_a")
    event_a1.source.user_id_alt = "shared_alt"
    event_b1 = _build_event("bob-1", user_id="ou_alt_b")
    event_b1.source.user_id_alt = "shared_alt"
    event_a2 = _build_event("alice-2", user_id="ou_alt_a")
    event_a2.source.user_id_alt = "shared_alt"

    await handle_async(event=event_a1, gateway=gateway)
    await handle_async(event=event_b1, gateway=gateway)
    await handle_async(event=event_a2, gateway=gateway)

    _user, msgs = seen[2]
    assert _user == "ou_alt_a"
    assert any("alice-1" in m for m in msgs)
    assert all("bob" not in m for m in msgs), msgs
    assert (home.name, "ou_alt_a") in _session_history
    assert (home.name, "shared_alt") not in _session_history

    clear_spike_routes()
