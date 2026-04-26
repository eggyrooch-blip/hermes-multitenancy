"""US-002 verification: hook callback fires asyncio.create_task and returns skip."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


def _build_event(text: str = "hi", chat_id: str = "chat-123", user_id: str = "ou_test"):
    """Build a minimal MessageEvent-shaped object for hook testing."""
    return SimpleNamespace(
        text=text,
        source=SimpleNamespace(
            chat_id=chat_id,
            user_id=user_id,
            user_name="test-user",
            chat_type="dm",
            platform=SimpleNamespace(value="feishu"),
        ),
    )


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
async def test_hook_no_route_silent():
    """If no route is registered, hook still returns skip but does nothing else."""
    from hermes_multitenancy import on_pre_gateway_dispatch
    from hermes_multitenancy.runtime import clear_spike_routes

    clear_spike_routes()

    event = _build_event(user_id="ou_unrouted")
    result = on_pre_gateway_dispatch(event=event, gateway=None, session_store=None)
    assert result["action"] == "skip"
    # Drain the no-op task so pytest-asyncio doesn't warn about unawaited coroutines
    await asyncio.sleep(0)
