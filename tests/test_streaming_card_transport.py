"""Feishu streaming-card transport for the multitenancy router."""
from __future__ import annotations

import asyncio
import json
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


class _CleanFeishuLikeAdapter:
    """Official-clean Feishu adapter shape: card patching exists, streaming surface does not."""

    def __init__(self):
        self._client = object()
        self.card_sends = []
        self.card_patches = []
        self.sent = []
        self.edits = []

    async def _feishu_send_with_retry(
        self, *, chat_id, msg_type, payload, reply_to, metadata
    ):
        self.card_sends.append(
            {
                "chat_id": chat_id,
                "msg_type": msg_type,
                "payload": payload,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SimpleNamespace(code=0, data={"message_id": "compat-card-1"})

    def _finalize_send_result(self, response, default_message):
        message_id = getattr(response, "data", {}).get("message_id")
        return SimpleNamespace(
            success=bool(message_id),
            message_id=message_id,
            error=None if message_id else default_message,
            raw_response=response,
        )

    async def _patch_auth_card(self, message_id, card):
        self.card_patches.append({"message_id": message_id, "card": card})
        return True

    def format_message(self, content):
        return str(content or "")

    async def send(self, chat_id, content, *, reply_to=None, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content})
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


class _CleanFeishuUpdateOnlyAdapter(_CleanFeishuLikeAdapter):
    """Clean Feishu adapter shape that updates cards through message.update."""

    def __init__(self):
        super().__init__()
        self.card_patches = None
        self.update_requests = []
        self._patch_auth_card = None

        class MessageApi:
            def __init__(api_self, outer):
                api_self.outer = outer

            def update(api_self, request):
                api_self.outer.update_requests.append(request)
                return SimpleNamespace(code=0, msg="success")

        self._client = SimpleNamespace(
            im=SimpleNamespace(v1=SimpleNamespace(message=MessageApi(self)))
        )

    @staticmethod
    def _build_update_message_body(*, msg_type, content):
        return SimpleNamespace(msg_type=msg_type, content=content)

    @staticmethod
    def _build_update_message_request(message_id, request_body):
        return SimpleNamespace(message_id=message_id, request_body=request_body)

    def _finalize_send_result(self, response, default_message):
        if getattr(response, "code", 1) == 0:
            message_id = getattr(response, "data", {}).get("message_id")
            return SimpleNamespace(success=True, message_id=message_id, error=None, raw_response=response)
        return SimpleNamespace(success=False, message_id=None, error=default_message, raw_response=response)


class _OpenClawCardKitAdapter(_CleanFeishuLikeAdapter):
    """Feishu adapter exposing the CardKit APIs used by openclaw-lark."""

    def __init__(self):
        super().__init__()
        self._patch_auth_card = None
        self.created_cards = []
        self.content_updates = []
        self.settings_updates = []
        self.card_updates = []

        class CardApi:
            def __init__(api_self, outer):
                api_self.outer = outer

            def create(api_self, request):
                api_self.outer.created_cards.append(request)
                return SimpleNamespace(code=0, msg="success", data={"card_id": "ck-1"})

            def settings(api_self, request):
                api_self.outer.settings_updates.append(request)
                return SimpleNamespace(code=0, msg="success")

            def update(api_self, request):
                api_self.outer.card_updates.append(request)
                return SimpleNamespace(code=0, msg="success")

        class CardElementApi:
            def __init__(api_self, outer):
                api_self.outer = outer

            def content(api_self, request):
                api_self.outer.content_updates.append(request)
                return SimpleNamespace(code=0, msg="success")

        self._client = SimpleNamespace(
            cardkit=SimpleNamespace(
                v1=SimpleNamespace(
                    card=CardApi(self),
                    card_element=CardElementApi(self),
                )
            )
        )


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


def test_stream_into_feishu_installs_cardkit_compat_for_clean_feishu_adapter(
    monkeypatch, tmp_path
):
    asyncio.run(
        _run_stream_into_feishu_installs_cardkit_compat_for_clean_feishu_adapter(
            monkeypatch,
            tmp_path,
        )
    )


async def _run_stream_into_feishu_installs_cardkit_compat_for_clean_feishu_adapter(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    async def fake_stream(event, home, *, messages=None):
        yield ("tool_started", {"name": "lark_cli", "preview": "GET /user_info"})
        yield ("tool_completed", {"name": "lark_cli", "duration": 0.3, "is_error": False})
        yield ("content", "Hello from clean adapter")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)
    monkeypatch.setattr(router_mod, "GatewayStreamConsumer", None, raising=False)

    adapter = _CleanFeishuLikeAdapter()
    assert not hasattr(adapter, "supports_streaming_card")

    response = await router_mod._stream_into_feishu(
        adapter,
        "chat-1",
        "profile",
        tmp_path,
        SimpleNamespace(text="hi"),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert response == "Hello from clean adapter"
    assert adapter.supports_streaming_card() is True
    assert adapter.sent == []
    assert adapter.edits == []
    assert len(adapter.card_sends) == 1
    assert adapter.card_sends[0]["msg_type"] == "interactive"
    initial_card = json.loads(adapter.card_sends[0]["payload"])
    assert "header" not in initial_card
    assert adapter.card_patches[-1]["message_id"] == "compat-card-1"
    final_card = adapter.card_patches[-1]["card"]
    assert "header" not in final_card
    rendered = "\n".join(element["content"] for element in final_card["elements"])
    assert "**Tool calls:**" in rendered
    assert "`lark_cli` (300 ms)" in rendered
    assert "Hello from clean adapter" in rendered
    assert "Done (" in rendered


def test_stream_into_feishu_updates_interactive_card_without_auth_patch_helper(
    monkeypatch, tmp_path
):
    asyncio.run(
        _run_stream_into_feishu_updates_interactive_card_without_auth_patch_helper(
            monkeypatch,
            tmp_path,
        )
    )


async def _run_stream_into_feishu_updates_interactive_card_without_auth_patch_helper(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    async def fake_stream(event, home, *, messages=None):
        yield ("content", "Updated through Feishu message.update")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)
    monkeypatch.setattr(router_mod, "GatewayStreamConsumer", None, raising=False)

    adapter = _CleanFeishuUpdateOnlyAdapter()

    response = await router_mod._stream_into_feishu(
        adapter,
        "chat-1",
        "profile",
        tmp_path,
        SimpleNamespace(text="hi"),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert response == "Updated through Feishu message.update"
    assert adapter.sent == []
    assert len(adapter.card_sends) == 1
    assert len(adapter.update_requests) >= 1
    final_request = adapter.update_requests[-1]
    assert final_request.message_id == "compat-card-1"
    assert final_request.request_body.msg_type == "interactive"
    final_card = json.loads(final_request.request_body.content)
    assert "header" not in final_card
    rendered = "\n".join(element["content"] for element in final_card["elements"])
    assert "Updated through Feishu message.update" in rendered
    assert "Done (" in rendered


def test_cardkit_compat_matches_openclaw_reasoning_body_tool_layout():
    asyncio.run(_run_cardkit_compat_matches_openclaw_reasoning_body_tool_layout())


async def _run_cardkit_compat_matches_openclaw_reasoning_body_tool_layout():
    from hermes_multitenancy.feishu_cardkit_compat import ensure_feishu_cardkit_streaming

    adapter = ensure_feishu_cardkit_streaming(_CleanFeishuLikeAdapter())
    started = await adapter.start_streaming_card(chat_id="chat-1")

    await adapter.update_streaming_card_reasoning(
        chat_id="chat-1",
        message_id=started.message_id,
        content="Reasoning:\n_checking user context_",
    )
    await adapter.update_streaming_card_tool_started(
        chat_id="chat-1",
        message_id=started.message_id,
        tool_name="lark_cli",
        preview="GET /user_info",
    )
    await adapter.update_streaming_card_tool_completed(
        chat_id="chat-1",
        message_id=started.message_id,
        tool_name="lark_cli",
        duration=0.3,
        is_error=False,
    )
    await adapter.update_streaming_card(
        chat_id="chat-1",
        message_id=started.message_id,
        content="<think>hidden chain</think># Result\n\nVisible answer\n\n![bad](https://example.com/a.png)",
        finalize=True,
    )

    final_card = adapter.card_patches[-1]["card"]
    elements = final_card["elements"]
    assert [element["tag"] for element in elements] == [
        "markdown",
        "collapsible_panel",
        "markdown",
        "markdown",
    ]
    assert elements[0]["content"] == "**Tool calls:**\n- `lark_cli` (300 ms)"
    reasoning_panel = elements[1]
    assert reasoning_panel["expanded"] is False
    assert reasoning_panel["header"]["title"]["content"].startswith("💭 Thought")
    assert reasoning_panel["header"]["icon"]["token"] == "down-small-ccm_outlined"
    assert reasoning_panel["elements"][0]["content"] == "hidden chain"

    body_text = elements[2]["content"]
    assert body_text.startswith("#### Result")
    assert "Visible answer" in body_text
    assert "hidden chain" not in body_text
    assert "https://example.com/a.png" not in body_text

    assert elements[3]["content"].startswith("Done (")
    assert elements[3]["text_size"] == "notation"


def test_cardkit_compat_never_renders_raw_tool_call_xml():
    asyncio.run(_run_cardkit_compat_never_renders_raw_tool_call_xml())


async def _run_cardkit_compat_never_renders_raw_tool_call_xml():
    from hermes_multitenancy.feishu_cardkit_compat import ensure_feishu_cardkit_streaming

    adapter = ensure_feishu_cardkit_streaming(_CleanFeishuLikeAdapter())
    started = await adapter.start_streaming_card(chat_id="chat-1")
    raw_tool = (
        '<tool_call>{"name":"lark_cli","arguments":{"command":"lark-cli doc +create '
        '--title \\"测试文档\\" --content \\"测试\\" --markdown true"}}</tool_call>'
    )

    await adapter.update_streaming_card(
        chat_id="chat-1",
        message_id=started.message_id,
        content=raw_tool + raw_tool,
        finalize=True,
    )

    final_card = adapter.card_patches[-1]["card"]
    rendered = "\n".join(element.get("content", "") for element in final_card["elements"])
    assert "<tool_call>" not in rendered
    assert "lark-cli doc +create" not in rendered
    assert "**Tool calls:**" in rendered
    assert "- `lark_cli` failed" in rendered
    assert "Done (" in rendered


def test_stream_into_feishu_uses_openclaw_cardkit_protocol_when_available(
    monkeypatch, tmp_path
):
    asyncio.run(
        _run_stream_into_feishu_uses_openclaw_cardkit_protocol_when_available(
            monkeypatch,
            tmp_path,
        )
    )


async def _run_stream_into_feishu_uses_openclaw_cardkit_protocol_when_available(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    async def fake_stream(event, home, *, messages=None):
        yield ("tool_started", {"name": "lark_cli", "preview": "GET /user_info"})
        yield ("tool_completed", {"name": "lark_cli", "duration": 0.3, "is_error": False})
        yield ("content", "Hello")
        yield ("content", " CardKit")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)
    monkeypatch.setattr(router_mod, "GatewayStreamConsumer", None, raising=False)

    adapter = _OpenClawCardKitAdapter()

    response = await router_mod._stream_into_feishu(
        adapter,
        "chat-1",
        "profile",
        tmp_path,
        SimpleNamespace(text="hi"),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert response == "Hello CardKit"
    assert adapter.sent == []
    assert adapter.edits == []
    assert len(adapter.created_cards) == 1
    initial_request = adapter.created_cards[0]
    initial_card = json.loads(initial_request.request_body.data)
    assert initial_request.request_body.type == "card_json"
    assert initial_card["schema"] == "2.0"
    assert initial_card["config"]["streaming_mode"] is True
    assert initial_card["body"]["elements"][0]["element_id"] == "streaming_content"
    assert len(adapter.card_sends) == 1
    sent_payload = json.loads(adapter.card_sends[0]["payload"])
    assert sent_payload == {"type": "card", "data": {"card_id": "ck-1"}}
    assert adapter.content_updates
    assert any("Hello" in req.request_body.content for req in adapter.content_updates)
    assert adapter.settings_updates[-1].request_body.settings == json.dumps({"streaming_mode": False})
    assert adapter.card_updates[-1].card_id == "ck-1"
    final_card = json.loads(adapter.card_updates[-1].request_body.card["data"])
    assert final_card["schema"] == "2.0"
    assert "header" not in final_card
    final_text = "\n".join(
        element.get("content", "")
        for element in final_card["body"]["elements"]
        if element.get("tag") == "markdown"
    )
    assert "**Tool calls:**" in final_text
    assert "`lark_cli` (300 ms)" in final_text
    assert "Hello CardKit" in final_text
    assert "Done (" in final_text


def test_stream_into_feishu_streams_cardkit_cumulative_text_for_typewriter(
    monkeypatch, tmp_path
):
    asyncio.run(
        _run_stream_into_feishu_streams_cardkit_cumulative_text_for_typewriter(
            monkeypatch,
            tmp_path,
        )
    )


async def _run_stream_into_feishu_streams_cardkit_cumulative_text_for_typewriter(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    async def fake_stream(event, home, *, messages=None):
        for chunk in ("H", "e", "l", "l", "o"):
            yield ("content", chunk)

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)
    monkeypatch.setattr(router_mod, "GatewayStreamConsumer", None, raising=False)

    adapter = _OpenClawCardKitAdapter()

    response = await router_mod._stream_into_feishu(
        adapter,
        "chat-1",
        "profile",
        tmp_path,
        SimpleNamespace(text="hi"),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert response == "Hello"
    streamed_contents = [
        req.request_body.content
        for req in adapter.content_updates
    ]
    assert "H" in streamed_contents
    assert "Hello" in streamed_contents
    assert streamed_contents.index("H") < streamed_contents.index("Hello")


def test_stream_into_feishu_skips_legacy_shared_consumer_without_card_methods(
    monkeypatch, tmp_path
):
    asyncio.run(
        _run_stream_into_feishu_skips_legacy_shared_consumer_without_card_methods(
            monkeypatch,
            tmp_path,
        )
    )


async def _run_stream_into_feishu_skips_legacy_shared_consumer_without_card_methods(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    async def fake_stream(event, home, *, messages=None):
        yield ("content", "Compat survives legacy consumer")

    class LegacyGatewayStreamConsumer:
        def __init__(self, *args, **kwargs):
            raise AssertionError("legacy shared consumer should not be constructed")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)
    monkeypatch.setattr(
        router_mod,
        "GatewayStreamConsumer",
        LegacyGatewayStreamConsumer,
        raising=False,
    )
    monkeypatch.setattr(
        router_mod,
        "StreamConsumerConfig",
        lambda **kwargs: SimpleNamespace(**kwargs),
        raising=False,
    )

    adapter = _CleanFeishuLikeAdapter()

    response = await router_mod._stream_into_feishu(
        adapter,
        "chat-1",
        "profile",
        tmp_path,
        SimpleNamespace(text="hi"),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert response == "Compat survives legacy consumer"
    assert adapter.sent == []
    assert adapter.edits == []
    assert len(adapter.card_sends) == 1
    assert adapter.card_sends[0]["msg_type"] == "interactive"
    final_card = adapter.card_patches[-1]["card"]
    assert "header" not in final_card
    rendered = "\n".join(element["content"] for element in final_card["elements"])
    assert "Compat survives legacy consumer" in rendered
    assert "Done (" in rendered


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


def test_stream_into_feishu_prompts_for_child_approval_and_approve_resolves(
    monkeypatch, tmp_path
):
    asyncio.run(
        _run_stream_into_feishu_prompts_for_child_approval_and_approve_resolves(
            monkeypatch,
            tmp_path,
        )
    )


async def _run_stream_into_feishu_prompts_for_child_approval_and_approve_resolves(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    adapter = _CardCapableAdapter()
    decision_path = tmp_path / "approval-decision.json"

    async def fake_stream(event, home, *, messages=None):
        yield (
            "approval_required",
            {
                "approval_id": "approval-1",
                "session_key": "multitenancy:feishu:profile:chat-1:ou_user",
                "command": "python -c 'print(1)'",
                "description": "script execution via -c flag",
                "decision_path": str(decision_path),
            },
        )
        while not decision_path.exists():
            await asyncio.sleep(0.005)
        yield ("content", "approved")

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

    async def wait_for_approval_prompt():
        while not adapter.sent:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait_for_approval_prompt(), timeout=1)
    assert "requires approval" in adapter.sent[0]["content"]
    assert "/approve" in adapter.sent[0]["content"]

    event = SimpleNamespace(
        text="/approve",
        source=SimpleNamespace(
            chat_id="chat-1",
            user_id="ou_user",
            user_name="tester",
            chat_type="dm",
            platform=SimpleNamespace(value="feishu"),
        ),
        get_command_args=lambda: "",
    )
    gateway = SimpleNamespace(adapters={"feishu": adapter}, config={})

    await router_mod._handle_command(
        ("approve", ""),
        "ou_user",
        None,
        "profile",
        tmp_path,
        "chat-1",
        gateway,
        event,
    )

    assert json.loads(decision_path.read_text(encoding="utf-8")) == {"choice": "once"}
    assert await task == "approved"
    assert "approved" in adapter.sent[-1]["content"].lower()


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
