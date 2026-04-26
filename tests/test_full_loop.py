"""US-004 verification: full loop send_typing -> AIAgent.run -> adapter.send."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_full_loop_calls_adapter_in_order():
    """End-to-end: hook -> handle_async -> typing -> agent -> send. Order verified."""
    from hermes_multitenancy import on_pre_gateway_dispatch
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    with tempfile.TemporaryDirectory() as tmp:
        profile_home = Path(tmp) / "loop_profile"
        profile_home.mkdir()
        add_spike_route("ou_loop", profile_home)

        call_log = []

        class MockAdapter:
            async def send_typing(self, chat_id):
                call_log.append(("send_typing", chat_id))

            async def send(self, chat_id, content, *, reply_to=None, metadata=None):
                call_log.append(("send", chat_id, content))

        gateway = SimpleNamespace(adapters={"feishu": MockAdapter()})

        event = SimpleNamespace(
            text="hi router",
            source=SimpleNamespace(
                chat_id="chat-loop",
                user_id="ou_loop",
                user_name="tester",
                chat_type="dm",
                platform=SimpleNamespace(value="feishu"),
            ),
        )

        result = on_pre_gateway_dispatch(event=event, gateway=gateway, session_store=None)
        assert result == {"action": "skip", "reason": "multitenancy router took over"}

        # Drain the background task
        for _ in range(20):
            await asyncio.sleep(0.01)
            if len(call_log) >= 2:
                break

        assert len(call_log) == 2, f"expected 2 calls, got {len(call_log)}: {call_log}"

        # Order: send_typing first, send second
        assert call_log[0][0] == "send_typing"
        assert call_log[0][1] == "chat-loop"
        assert call_log[1][0] == "send"
        assert call_log[1][1] == "chat-loop"

        # The response carries the profile name (from default _default_run_agent stub)
        response_text = call_log[1][2]
        assert "[profile=loop_profile]" in response_text
        assert "hi router" in response_text

    clear_spike_routes()


@pytest.mark.asyncio
async def test_full_loop_with_injected_agent(monkeypatch):
    """Same loop but with a custom agent runner injected via runtime.

    Demonstrates that ProfileRuntime is composable — Phase 2 will pass a real
    AIAgent runner here instead of the stub. Uses monkeypatch on ProfileRuntime's
    default runner attribute (cleaner than swapping __init__).
    """
    from hermes_multitenancy.router import handle_async
    from hermes_multitenancy import runtime as runtime_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    with tempfile.TemporaryDirectory() as tmp:
        profile_home = Path(tmp)

        async def fake_agent(event, home):
            return f"AGENT[{home.name}]: handled {event.text}"

        # Replace the module-level default runner — cleaner than __init__ swap
        monkeypatch.setattr(runtime_mod, "_default_run_agent", fake_agent)

        add_spike_route("ou_inject", profile_home)

        calls = []

        class A:
            async def send_typing(self, c): calls.append(("t", c))
            async def send(self, c, m, *, reply_to=None, metadata=None): calls.append(("s", c, m))

        gateway = SimpleNamespace(adapters={"feishu": A()})
        event = SimpleNamespace(
            text="ping",
            source=SimpleNamespace(
                chat_id="chat-inj",
                user_id="ou_inject",
                platform=SimpleNamespace(value="feishu"),
            ),
        )
        await handle_async(event=event, gateway=gateway)

        assert len(calls) == 2
        assert calls[0] == ("t", "chat-inj")
        assert calls[1][0] == "s"
        assert calls[1][1] == "chat-inj"
        assert "AGENT[" in calls[1][2]
        assert "handled ping" in calls[1][2]

    clear_spike_routes()
