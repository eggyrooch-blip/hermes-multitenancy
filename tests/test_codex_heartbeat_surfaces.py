"""Real-sink projection of the zero-model heartbeat kind (SPEC ticket 02)
onto the two production consumers that actually render it to a human: the
Feishu card surfaces in ``router/streaming.py``, and the WebUI SSE dispatch
in ``webui_broker/periphery.py``.

Complements ``tests/test_codex_event_heartbeat.py``, whose two-projection-
equivalence test (``_webui_style_project`` / ``_feishu_style_project``) uses
synthetic mirror functions that copy the kind-switch *shape* without ever
calling the real code. These tests instead drive the REAL production entry
points end to end, reusing the harness fixtures already used by
``test_streaming_card_transport.py`` and ``test_webui_broker_server.py`` for
the sibling "status" kind.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from hermes_multitenancy.agent_real.codex_event_heartbeat import heartbeat_payload
from hermes_multitenancy.run_models import RunEvent, RunRequest


def _assert_no_leakage(text: str) -> None:
    """A heartbeat text must be exactly the fixed Chinese status string --
    never a state enum literal, a path, a traceback, or an open_id."""
    for forbidden in ("waiting_tool", "waiting_gate", "running", "finishing", "/", "Traceback", "ou_"):
        assert forbidden not in text, f"heartbeat text leaked {forbidden!r}: {text!r}"


class _CardCapableAdapter:
    """Trimmed copy of test_streaming_card_transport.py's fixture -- same
    method contract ``_adapter_supports_streaming_card``/the legacy loop
    require, only the fields these tests read."""

    def __init__(self):
        self.updates = []
        self.status_updates = []

    def supports_streaming_card(self):
        return True

    async def start_streaming_card(self, *, chat_id, reply_to=None, metadata=None):
        return SimpleNamespace(success=True, message_id="card-1")

    async def update_streaming_card(self, *, chat_id, message_id, content, finalize=False):
        self.updates.append({"chat_id": chat_id, "message_id": message_id, "content": content, "finalize": finalize})
        return SimpleNamespace(success=True, message_id=message_id)

    async def update_streaming_card_status(self, *, chat_id, message_id, content):
        self.status_updates.append({"chat_id": chat_id, "message_id": message_id, "content": content})
        return SimpleNamespace(success=True, message_id=message_id)

    async def update_streaming_card_reasoning(self, *, chat_id, message_id, content):
        return SimpleNamespace(success=True, message_id=message_id)

    async def update_streaming_card_tool_started(self, *, chat_id, message_id, tool_name, preview=None, args=None):
        return SimpleNamespace(success=True, message_id=message_id)

    async def update_streaming_card_tool_completed(self, *, chat_id, message_id, tool_name, duration=None, is_error=False):
        return SimpleNamespace(success=True, message_id=message_id)

    async def fail_streaming_card(self, *, chat_id, message_id, content=None):
        return SimpleNamespace(success=True, message_id=message_id)

    async def reopen_streaming_card(self, *, chat_id, message_id):
        pass

    async def send(self, chat_id, content, *, reply_to=None, metadata=None):
        return SimpleNamespace(success=True, message_id="text-1")

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        return SimpleNamespace(success=True, message_id=message_id)


# ── Feishu: legacy per-message ("edit"/native-card) loop ─────────────────
# router/streaming.py's _stream_into_feishu, ~line 1319:
# `elif kind in ("status", "heartbeat")`. Reached whenever no
# GatewayStreamConsumer is installed -- same path exercised by
# test_stream_into_feishu_status_event_updates_card_without_polluting_content
# for the sibling plain "status" kind.


@pytest.mark.asyncio
async def test_feishu_edit_loop_renders_heartbeat_as_card_status(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real, router as router_mod

    expected = heartbeat_payload("waiting_tool")

    async def fake_stream(event, home, *, messages=None):
        yield ("content", "第一段\n")
        yield ("heartbeat", expected)
        yield ("content", "done")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    adapter = _CardCapableAdapter()
    response = await router_mod._stream_into_feishu(
        adapter,
        "chat-1",
        "profile",
        tmp_path,
        SimpleNamespace(text="hi"),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert response == "第一段\ndone"
    assert adapter.status_updates[-1] == {
        "chat_id": "chat-1",
        "message_id": "card-1",
        "content": expected["text"],
    }
    assert adapter.updates[-1]["content"] == "第一段\ndone"
    _assert_no_leakage(adapter.updates[-1]["content"])
    _assert_no_leakage(adapter.status_updates[-1]["content"])


# ── Feishu: shared GatewayStreamConsumer loop ─────────────────────────────
# router/streaming.py's _stream_into_feishu_shared_consumer, ~line 809:
# `if kind in ("status", "heartbeat")`. Reached when GatewayStreamConsumer
# IS installed -- same harness shape as
# test_stream_into_feishu_uses_gateway_stream_consumer_for_card_transport.


_created_consumers: list["_RecordingConsumer"] = []


class _RecordingConsumer:
    """Trimmed copy of that test's RecordingConsumer -- implements every
    method _stream_into_feishu_shared_consumer's required-method contract
    checks for, records only what these tests assert on. Registers itself
    in ``_created_consumers`` since _stream_into_feishu constructs it
    internally -- the test has no other handle on the instance."""

    def __init__(self, adapter, chat_id, config=None, metadata=None, initial_reply_to_id=None):
        self.adapter = adapter
        self.chat_id = chat_id
        self.statuses = []
        self.deltas = []
        self.finished = False
        self._done = asyncio.Event()
        _created_consumers.append(self)

    async def ensure_streaming_card_started(self):
        return True

    async def run(self):
        await self._done.wait()

    def on_delta(self, text):
        self.deltas.append(text)

    async def update_streaming_card_status(self, content):
        self.statuses.append(content)
        return True

    async def update_streaming_card_reasoning(self, content):
        return True

    async def update_streaming_card_tool_started(self, tool_name, *, preview=None, args=None):
        return True

    async def update_streaming_card_tool_completed(self, tool_name, *, duration=None, is_error=False):
        return True

    def finish(self):
        self.finished = True
        self._done.set()


@pytest.mark.asyncio
async def test_feishu_shared_consumer_renders_heartbeat_as_card_status(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real, router as router_mod

    expected = heartbeat_payload("waiting_gate")

    async def fake_stream(event, home, *, messages=None):
        yield ("heartbeat", expected)
        yield ("content", "answer")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)
    monkeypatch.setattr(router_mod, "GatewayStreamConsumer", _RecordingConsumer, raising=False)
    monkeypatch.setattr(
        router_mod, "StreamConsumerConfig", lambda **kwargs: SimpleNamespace(**kwargs), raising=False
    )
    _created_consumers.clear()

    adapter = _CardCapableAdapter()
    response = await router_mod._stream_into_feishu(
        adapter,
        "chat-1",
        "profile",
        tmp_path,
        SimpleNamespace(text="hi", message_id="om_1", source=SimpleNamespace(chat_type="group")),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert response == "answer"
    assert len(_created_consumers) == 1
    consumer = _created_consumers[0]
    assert expected["text"] in consumer.statuses
    for status in consumer.statuses:
        _assert_no_leakage(status)


# ── WebUI: broker dispatch loop ───────────────────────────────────────────
# webui_broker/periphery.py's _default_dispatch_agent, ~line 2488: new
# `if kind == "heartbeat":` branch. Calls the real dispatch function
# directly (not through the full HTTP/SSE layer) -- same harness shape as
# test_default_dispatch_attaches_only_server_prepared_codex_evidence, since
# only the projection contract (kind switch -> RunEvent), not the SSE
# transport, is new here.


@pytest.mark.asyncio
async def test_webui_default_dispatch_emits_heartbeat_run_event(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real, router as router_mod
    from hermes_multitenancy.webui_broker_server import _default_dispatch_agent

    expected = heartbeat_payload("finishing")

    async def fake_stream(event, home, *, messages=None):
        yield ("heartbeat", expected)
        yield ("content", "ok")

    monkeypatch.setattr(
        router_mod, "_profile_name_to_home", lambda _name: tmp_path / "profiles" / "owner"
    )
    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    emitted: list[RunEvent] = []

    async def emit(event):
        emitted.append(event)

    result = await _default_dispatch_agent(
        RunRequest(
            channel="webui",
            profile_name="owner",
            user_key="ou_owner",
            content="hi",
        ),
        emit_event=emit,
    )

    assert result == ""
    heartbeats = [e for e in emitted if e.kind == "heartbeat"]
    assert len(heartbeats) == 1
    hb = heartbeats[0]
    assert hb.text == expected["text"]
    assert set(hb.payload) == {"state", "text"}
    assert hb.payload == expected
    _assert_no_leakage(hb.text)
