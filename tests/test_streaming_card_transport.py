"""Feishu streaming-card transport for the multitenancy router."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


class _CardCapableAdapter:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.started = []
        self.updates = []
        self.status_updates = []
        self.reasoning_updates = []
        self.tool_starts = []
        self.tool_completions = []
        self.aborts = []

    def supports_streaming_card(self):
        return True

    async def start_streaming_card(self, *, chat_id, reply_to=None, metadata=None):
        self.started.append(
            {"chat_id": chat_id, "reply_to": reply_to, "metadata": metadata}
        )
        return SimpleNamespace(success=True, message_id="card-1")

    async def update_streaming_card(
        self, *, chat_id, message_id, content, finalize=False
    ):
        self.updates.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "content": content,
                "finalize": finalize,
            }
        )
        return SimpleNamespace(success=True, message_id=message_id)

    async def update_streaming_card_status(
        self, *, chat_id, message_id, content
    ):
        self.status_updates.append(
            {"chat_id": chat_id, "message_id": message_id, "content": content}
        )
        return SimpleNamespace(success=True, message_id=message_id)

    async def update_streaming_card_reasoning(
        self, *, chat_id, message_id, content
    ):
        self.reasoning_updates.append(
            {"chat_id": chat_id, "message_id": message_id, "content": content}
        )
        return SimpleNamespace(success=True, message_id=message_id)

    async def update_streaming_card_tool_started(
        self, *, chat_id, message_id, tool_name, preview=None, args=None
    ):
        self.tool_starts.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "tool_name": tool_name,
                "preview": preview,
                "args": args,
            }
        )
        return SimpleNamespace(success=True, message_id=message_id)

    async def update_streaming_card_tool_completed(
        self, *, chat_id, message_id, tool_name, duration=None, is_error=False
    ):
        self.tool_completions.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "tool_name": tool_name,
                "duration": duration,
                "is_error": is_error,
            }
        )
        return SimpleNamespace(success=True, message_id=message_id)

    async def abort_streaming_card(self, *, chat_id, message_id, content=None):
        self.aborts.append(
            {"chat_id": chat_id, "message_id": message_id, "content": content}
        )
        return SimpleNamespace(success=True, message_id=message_id)

    async def send(self, chat_id, content, *, reply_to=None, metadata=None):
        self.sent.append(
            {"chat_id": chat_id, "content": content, "reply_to": reply_to, "metadata": metadata}
        )
        return SimpleNamespace(success=True, message_id="text-1")

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "content": content,
                "finalize": finalize,
            }
        )
        return SimpleNamespace(success=True, message_id=message_id)


class _DeferredLifecycleCardAdapter(_CardCapableAdapter):
    def __init__(self):
        super().__init__()
        self.lifecycle = []

    async def on_processing_start(self, event):
        self.lifecycle.append(("start", getattr(event, "message_id", None)))

    async def on_processing_complete(self, event, outcome):
        self.lifecycle.append(("complete", str(outcome)))

    async def complete_deferred_processing(self, event, outcome):
        self.lifecycle.append(("complete_deferred", str(outcome)))


@pytest.mark.asyncio
async def test_stream_into_feishu_uses_card_transport_when_adapter_supports_it(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    async def fake_stream(event, home, *, messages=None):
        yield ("content", "Hello")
        yield ("content", " world")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    adapter = _CardCapableAdapter()
    event = SimpleNamespace(text="hi")

    response = await router_mod._stream_into_feishu(
        adapter,
        "chat-1",
        "profile",
        tmp_path,
        event,
        messages=[{"role": "user", "content": "hi"}],
    )

    assert response == "Hello world"
    assert adapter.started == [
        {"chat_id": "chat-1", "reply_to": None, "metadata": None}
    ]
    assert adapter.sent == []
    assert adapter.edits == []
    assert adapter.updates[-1] == {
        "chat_id": "chat-1",
        "message_id": "card-1",
        "content": "Hello world",
        "finalize": True,
    }


@pytest.mark.asyncio
async def test_stream_into_feishu_uses_gateway_stream_consumer_for_card_transport(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    async def fake_stream(event, home, *, messages=None):
        yield ("thinking", "checking")
        yield ("tool_started", {"name": "feishu_calendar_list_events", "preview": "week"})
        yield (
            "tool_completed",
            {"name": "feishu_calendar_list_events", "duration": 0.2, "is_error": False},
        )
        yield ("content", "Hello")
        yield ("content", " world")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    created = []

    class RecordingConsumer:
        def __init__(self, adapter, chat_id, config=None, metadata=None):
            self.adapter = adapter
            self.chat_id = chat_id
            self.config = config
            self.metadata = metadata
            self.deltas = []
            self.statuses = []
            self.reasoning = []
            self.tool_starts = []
            self.tool_completions = []
            self.finished = False
            self._done = asyncio.Event()
            created.append(self)

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
            self.reasoning.append(content)
            return True

        async def update_streaming_card_tool_started(self, tool_name, *, preview=None, args=None):
            self.tool_starts.append({"tool_name": tool_name, "preview": preview, "args": args})
            return True

        async def update_streaming_card_tool_completed(self, tool_name, *, duration=None, is_error=False):
            self.tool_completions.append(
                {"tool_name": tool_name, "duration": duration, "is_error": is_error}
            )
            return True

        def finish(self):
            self.finished = True
            self._done.set()

    monkeypatch.setattr(router_mod, "GatewayStreamConsumer", RecordingConsumer, raising=False)
    monkeypatch.setattr(
        router_mod,
        "StreamConsumerConfig",
        lambda **kwargs: SimpleNamespace(**kwargs),
        raising=False,
    )

    adapter = _CardCapableAdapter()
    response = await router_mod._stream_into_feishu(
        adapter,
        "chat-1",
        "profile",
        tmp_path,
        SimpleNamespace(text="hi"),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert response == "Hello world"
    assert len(created) == 1
    consumer = created[0]
    assert consumer.adapter is adapter
    assert consumer.chat_id == "chat-1"
    assert consumer.statuses == ["Hermes 正在准备响应..."]
    assert consumer.reasoning == ["checking"]
    assert consumer.tool_starts == [
        {"tool_name": "feishu_calendar_list_events", "preview": "week", "args": None}
    ]
    assert consumer.tool_completions == [
        {"tool_name": "feishu_calendar_list_events", "duration": 0.2, "is_error": False}
    ]
    assert consumer.deltas == ["Hello", " world"]
    assert consumer.finished is True


@pytest.mark.asyncio
async def test_stream_into_feishu_hides_media_directives_from_card(monkeypatch, tmp_path):
    """MEDIA tags are control directives for native file delivery, not visible card text."""
    from hermes_multitenancy import agent_real, router as router_mod

    async def fake_stream(event, home, *, messages=None):
        yield ("content", "created\n")
        yield ("content", "MEDIA:/tmp/hermes-output.md")

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

    assert response == "created\nMEDIA:/tmp/hermes-output.md"
    assert adapter.updates[-1] == {
        "chat_id": "chat-1",
        "message_id": "card-1",
        "content": "created",
        "finalize": True,
    }


@pytest.mark.asyncio
async def test_handle_async_completes_deferred_processing_lifecycle(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    add_spike_route("ou_life", tmp_path)

    async def fake_stream(event, home, *, messages=None):
        yield ("content", "done")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    adapter = _DeferredLifecycleCardAdapter()
    gateway = SimpleNamespace(adapters={"feishu": adapter})
    event = SimpleNamespace(
        text="hi",
        message_id="om_life",
        source=SimpleNamespace(
            chat_id="chat-life",
            user_id="ou_life",
            user_id_alt=None,
            user_name="tester",
            chat_type="dm",
            platform=SimpleNamespace(value="feishu"),
        ),
    )

    await router_mod.handle_async(event=event, gateway=gateway)

    assert adapter.lifecycle == [
        ("start", "om_life"),
        ("complete_deferred", "ProcessingOutcome.SUCCESS"),
    ]

    clear_spike_routes()


@pytest.mark.asyncio
async def test_stream_into_feishu_primes_card_before_waiting_for_agent_stream(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    adapter = _CardCapableAdapter()
    stream_entered = asyncio.Event()
    release_stream = asyncio.Event()

    async def fake_stream(event, home, *, messages=None):
        stream_entered.set()
        await release_stream.wait()
        yield ("content", "ready")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    task = asyncio.create_task(
        router_mod._stream_into_feishu(
            adapter,
            "chat-1",
            "profile",
            tmp_path,
            SimpleNamespace(text="hi"),
            messages=[{"role": "user", "content": "hi"}],
        )
    )

    await asyncio.wait_for(stream_entered.wait(), timeout=1)

    assert adapter.started == [
        {"chat_id": "chat-1", "reply_to": None, "metadata": None}
    ]
    assert adapter.status_updates == [
        {
            "chat_id": "chat-1",
            "message_id": "card-1",
            "content": "Hermes 正在准备响应...",
        }
    ]
    assert adapter.reasoning_updates == []

    release_stream.set()
    assert await task == "ready"


@pytest.mark.asyncio
async def test_stream_into_feishu_keeps_card_alive_while_agent_is_silent(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    monkeypatch.setattr(router_mod, "_STREAM_CARD_IDLE_HEARTBEAT_SECONDS", 0.01)
    adapter = _CardCapableAdapter()
    stream_entered = asyncio.Event()
    release_stream = asyncio.Event()

    async def fake_stream(event, home, *, messages=None):
        stream_entered.set()
        await release_stream.wait()
        yield ("content", "ready")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    task = asyncio.create_task(
        router_mod._stream_into_feishu(
            adapter,
            "chat-1",
            "profile",
            tmp_path,
            SimpleNamespace(text="hi"),
            messages=[{"role": "user", "content": "hi"}],
        )
    )

    await asyncio.wait_for(stream_entered.wait(), timeout=1)

    async def wait_for_status_heartbeat():
        while len(adapter.status_updates) < 2:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait_for_status_heartbeat(), timeout=1)
    assert adapter.status_updates[0]["content"] == "Hermes 正在准备响应..."
    assert adapter.status_updates[1]["content"] != adapter.status_updates[0]["content"]
    assert adapter.reasoning_updates == []

    release_stream.set()
    assert await task == "ready"
    assert adapter.updates[-1]["content"] == "ready"
    assert adapter.updates[-1]["finalize"] is True


@pytest.mark.asyncio
async def test_stream_into_feishu_aborts_card_when_cancelled(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real, router as router_mod

    stream_waiting = asyncio.Event()
    never_release = asyncio.Event()

    async def fake_stream(event, home, *, messages=None):
        yield ("content", "partial answer")
        stream_waiting.set()
        await never_release.wait()

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    adapter = _CardCapableAdapter()
    task = asyncio.create_task(
        router_mod._stream_into_feishu(
            adapter,
            "chat-1",
            "profile",
            tmp_path,
            SimpleNamespace(text="hi"),
            messages=[{"role": "user", "content": "hi"}],
        )
    )

    await asyncio.wait_for(stream_waiting.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert adapter.aborts == [
        {
            "chat_id": "chat-1",
            "message_id": "card-1",
            "content": "partial answer",
        }
    ]


@pytest.mark.asyncio
async def test_stream_into_feishu_aborts_card_when_cancelled_during_start(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    class SlowStartAdapter(_CardCapableAdapter):
        def __init__(self):
            super().__init__()
            self.start_entered = asyncio.Event()
            self.release_start = asyncio.Event()

        async def start_streaming_card(self, *, chat_id, reply_to=None, metadata=None):
            self.start_entered.set()
            await self.release_start.wait()
            return await super().start_streaming_card(
                chat_id=chat_id,
                reply_to=reply_to,
                metadata=metadata,
            )

    async def fake_stream(event, home, *, messages=None):
        yield ("content", "should not stream")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    adapter = SlowStartAdapter()
    task = asyncio.create_task(
        router_mod._stream_into_feishu(
            adapter,
            "chat-1",
            "profile",
            tmp_path,
            SimpleNamespace(text="hi"),
            messages=[{"role": "user", "content": "hi"}],
        )
    )

    await asyncio.wait_for(adapter.start_entered.wait(), timeout=1)
    task.cancel()
    adapter.release_start.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert adapter.started == [
        {"chat_id": "chat-1", "reply_to": None, "metadata": None}
    ]
    assert adapter.aborts == [
        {"chat_id": "chat-1", "message_id": "card-1", "content": "Aborted."}
    ]


@pytest.mark.asyncio
async def test_stream_into_feishu_aborts_card_when_cancelled_during_prime(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    class SlowPrimeAdapter(_CardCapableAdapter):
        def __init__(self):
            super().__init__()
            self.prime_entered = asyncio.Event()
            self.release_prime = asyncio.Event()

        async def update_streaming_card_status(
            self, *, chat_id, message_id, content
        ):
            if content == "Hermes 正在准备响应...":
                self.prime_entered.set()
                await self.release_prime.wait()
            return await super().update_streaming_card_status(
                chat_id=chat_id,
                message_id=message_id,
                content=content,
            )

    async def fake_stream(event, home, *, messages=None):
        yield ("content", "should not stream")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    adapter = SlowPrimeAdapter()
    task = asyncio.create_task(
        router_mod._stream_into_feishu(
            adapter,
            "chat-1",
            "profile",
            tmp_path,
            SimpleNamespace(text="hi"),
            messages=[{"role": "user", "content": "hi"}],
        )
    )

    await asyncio.wait_for(adapter.prime_entered.wait(), timeout=1)
    task.cancel()
    adapter.release_prime.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert adapter.aborts == [
        {"chat_id": "chat-1", "message_id": "card-1", "content": "Aborted."}
    ]


@pytest.mark.asyncio
async def test_stream_into_feishu_updates_card_for_tool_and_reasoning_events(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    async def fake_stream(event, home, *, messages=None):
        yield ("tool_started", {"name": "feishu_task_tasklist", "preview": "rename"})
        yield ("thinking", "正在等待飞书任务接口")
        yield (
            "tool_completed",
            {"name": "feishu_task_tasklist", "duration": 1.2, "is_error": False},
        )
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

    assert response == "done"
    assert adapter.tool_starts == [
        {
            "chat_id": "chat-1",
            "message_id": "card-1",
            "tool_name": "feishu_task_tasklist",
            "preview": "rename",
            "args": None,
        }
    ]
    assert adapter.status_updates == [
        {
            "chat_id": "chat-1",
            "message_id": "card-1",
            "content": "Hermes 正在准备响应...",
        },
    ]
    assert adapter.reasoning_updates == [
        {
            "chat_id": "chat-1",
            "message_id": "card-1",
            "content": "正在等待飞书任务接口",
        }
    ]
    assert adapter.tool_completions == [
        {
            "chat_id": "chat-1",
            "message_id": "card-1",
            "tool_name": "feishu_task_tasklist",
            "duration": 1.2,
            "is_error": False,
        }
    ]
    assert adapter.updates[-1]["content"] == "done"
    assert adapter.updates[-1]["finalize"] is True


def test_stream_into_feishu_streams_raw_reasoning_in_card(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real, router as router_mod

    async def fake_stream(event, home, *, messages=None):
        yield ("thinking", "The user wants to update a Feishu record.")
        yield ("thinking", " Let me inspect the app token.")
        yield ("content", "done")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    adapter = _CardCapableAdapter()
    response = asyncio.run(
        router_mod._stream_into_feishu(
            adapter,
            "chat-1",
            "profile",
            tmp_path,
            SimpleNamespace(text="hi"),
            messages=[{"role": "user", "content": "hi"}],
        )
    )

    assert response == "done"
    assert [item["content"] for item in adapter.status_updates] == [
        "Hermes 正在准备响应...",
    ]
    assert [item["content"] for item in adapter.reasoning_updates] == [
        "The user wants to update a Feishu record.",
    ]


@pytest.mark.asyncio
async def test_stream_into_feishu_falls_back_to_text_edit_when_card_start_fails(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    class FailingCardAdapter(_CardCapableAdapter):
        async def start_streaming_card(self, *, chat_id, reply_to=None, metadata=None):
            self.started.append(
                {"chat_id": chat_id, "reply_to": reply_to, "metadata": metadata}
            )
            return SimpleNamespace(success=False, error="card disabled")

    async def fake_stream(event, home, *, messages=None):
        yield ("content", "fallback text")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    adapter = FailingCardAdapter()
    response = await router_mod._stream_into_feishu(
        adapter,
        "chat-1",
        "profile",
        tmp_path,
        SimpleNamespace(text="hi"),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert response == "fallback text"
    assert [item["content"] for item in adapter.sent] == ["..."]
    assert adapter.edits[-1]["content"] == "fallback text"
    assert adapter.edits[-1]["finalize"] is True
    assert adapter.updates == []
