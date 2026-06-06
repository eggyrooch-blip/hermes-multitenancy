"""Feishu streaming-card transport for the multitenancy router."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest


def _card_text(card_or_elements):
    elements = card_or_elements.get("elements", card_or_elements) if isinstance(card_or_elements, dict) else card_or_elements
    parts = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        content = element.get("content")
        if content:
            parts.append(str(content))
        nested = element.get("elements")
        if nested:
            parts.append(_card_text(nested))
    return "\n".join(part for part in parts if part)


def _assert_tool_panel(card_or_elements):
    """Pin the full Tool calls collapsible_panel contract on a tool-bearing card:
    panel sits at elements[0], tag is collapsible_panel, expanded is False (closed
    by default per openclaw layout), and the header title is exactly 'Tool calls'.

    Returns the panel dict so callers can drill into ``panel['elements'][0]['content']``
    when they need to assert the inner tool-rows text directly.
    """
    elements = (
        card_or_elements.get("elements", card_or_elements)
        if isinstance(card_or_elements, dict)
        else card_or_elements
    )
    panel = elements[0]
    assert panel["tag"] == "collapsible_panel"
    assert panel["expanded"] is False
    assert panel["header"]["title"]["content"] == "Tool calls"
    return panel


def _assert_loading_icon_element(element):
    assert element["element_id"] == "loading_icon"
    assert element["tag"] == "markdown"
    assert element["content"] == " "
    assert element["icon"] == {
        "tag": "custom_icon",
        "img_key": "img_v3_02vb_496bec09-4b43-4773-ad6b-0cdd103cd2bg",
        "size": "16px 16px",
    }


def test_thread_metadata_is_suppressed_for_dm_to_avoid_topic():
    from hermes_multitenancy import router as router_mod

    calls = []

    class Gateway:
        def _reply_anchor_for_event(self, event):
            return "om_dm_anchor"

        def _thread_metadata_for_source(self, source, reply_anchor):
            calls.append((source.chat_type, reply_anchor))
            return {"thread_id": "omt_dm_topic"}

    event = SimpleNamespace(
        message_id="om_dm_anchor",
        source=SimpleNamespace(chat_type="dm"),
    )

    assert router_mod._thread_metadata_for_media_delivery(Gateway(), event) is None
    assert calls == []


def test_thread_metadata_is_preserved_for_group_replies():
    from hermes_multitenancy import router as router_mod

    class Gateway:
        def _reply_anchor_for_event(self, event):
            return "om_group_anchor"

        def _thread_metadata_for_source(self, source, reply_anchor):
            return {"thread_id": f"{source.chat_type}:{reply_anchor}"}

    event = SimpleNamespace(
        message_id="om_group_anchor",
        source=SimpleNamespace(chat_type="group"),
    )

    assert router_mod._thread_metadata_for_media_delivery(Gateway(), event) == {
        "thread_id": "group:om_group_anchor"
    }


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


class _DeferredLifecycleMediaAdapter(_DeferredLifecycleCardAdapter):
    async def send_document(self, **kwargs):
        self.lifecycle.append(("send_document", kwargs.get("reply_to"), kwargs.get("file_name")))
        return SimpleNamespace(success=True)


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
    event = SimpleNamespace(
        text="hi",
        message_id="om_source_1",
        source=SimpleNamespace(chat_type="group"),
    )

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
        {"chat_id": "chat-1", "reply_to": "om_source_1", "metadata": None}
    ]
    assert adapter.sent == []
    assert adapter.edits == []
    assert adapter.updates[-1] == {
        "chat_id": "chat-1",
        "message_id": "card-1",
        "content": "Hello world",
        "finalize": True,
    }


def test_stream_into_feishu_does_not_reply_to_dm_message_to_avoid_topic(
    monkeypatch, tmp_path
):
    asyncio.run(
        _run_stream_into_feishu_does_not_reply_to_dm_message_to_avoid_topic(
            monkeypatch, tmp_path
        )
    )


async def _run_stream_into_feishu_does_not_reply_to_dm_message_to_avoid_topic(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    async def fake_stream(event, home, *, messages=None):
        yield ("content", "Hello")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    adapter = _CardCapableAdapter()
    event = SimpleNamespace(
        text="hi",
        message_id="om_dm_source",
        source=SimpleNamespace(chat_type="dm"),
    )

    response = await router_mod._stream_into_feishu(
        adapter,
        "dm-chat",
        "profile",
        tmp_path,
        event,
        messages=[{"role": "user", "content": "hi"}],
    )

    assert response == "Hello"
    assert adapter.started == [
        {"chat_id": "dm-chat", "reply_to": None, "metadata": None}
    ]


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
    assert initial_card["config"]["wide_screen_mode"] is False
    assert adapter.card_patches[-1]["message_id"] == "compat-card-1"
    final_card = adapter.card_patches[-1]["card"]
    assert "header" not in final_card
    assert final_card["config"]["wide_screen_mode"] is True
    _assert_tool_panel(final_card)
    rendered = _card_text(final_card)
    assert "**Tool calls:**" not in rendered
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
    rendered = _card_text(final_card)
    assert all(element.get("tag") == "markdown" for element in final_card["elements"])
    assert final_card["elements"][-1]["tag"] == "markdown"
    assert "Updated through Feishu message.update" in rendered
    assert "Done (" in rendered


def test_cardkit_compat_matches_openclaw_reasoning_body_tool_layout():
    asyncio.run(_run_cardkit_compat_matches_openclaw_reasoning_body_tool_layout())


def test_cardkit_compat_flushes_tool_events_during_stream():
    asyncio.run(_run_cardkit_compat_flushes_tool_events_during_stream())


def test_cardkit_compat_initial_card_uses_compact_empty_placeholders():
    asyncio.run(_run_cardkit_compat_initial_card_uses_compact_empty_placeholders())


async def _run_cardkit_compat_initial_card_uses_compact_empty_placeholders():
    from hermes_multitenancy.feishu_cardkit_compat import ensure_feishu_cardkit_streaming

    adapter = ensure_feishu_cardkit_streaming(_CleanFeishuLikeAdapter())
    await adapter.start_streaming_card(chat_id="chat-1")

    initial_card = json.loads(adapter.card_sends[0]["payload"])
    assert initial_card["config"]["wide_screen_mode"] is False
    elements = initial_card["elements"]
    assert len(elements) == 1
    assert all(element.get("tag") == "markdown" for element in elements)
    assert [element["content"] for element in elements] == ["..."]
    assert "Thinking" not in _card_text(elements)


async def _run_cardkit_compat_flushes_tool_events_during_stream():
    from hermes_multitenancy.feishu_cardkit_compat import ensure_feishu_cardkit_streaming

    adapter = ensure_feishu_cardkit_streaming(_CleanFeishuLikeAdapter())
    started = await adapter.start_streaming_card(chat_id="chat-1")

    await adapter.update_streaming_card_tool_started(
        chat_id="chat-1",
        message_id=started.message_id,
        tool_name="lark_cli",
        preview="GET /open-apis/authen/v1/user_info",
    )
    assert adapter.card_patches
    running_card = adapter.card_patches[-1]["card"]
    _assert_tool_panel(running_card)
    rendered_running = _card_text(running_card)
    assert "- `lark_cli` running" in rendered_running
    assert "GET /open-apis/authen/v1/user_info" not in rendered_running

    await adapter.update_streaming_card_tool_completed(
        chat_id="chat-1",
        message_id=started.message_id,
        tool_name="lark_cli",
        duration=0.42,
        is_error=False,
    )
    done_card = adapter.card_patches[-1]["card"]
    _assert_tool_panel(done_card)
    rendered_done = _card_text(done_card)
    assert "`lark_cli` (420 ms)" in rendered_done


def test_cardkit_compat_hides_tool_argument_summary():
    asyncio.run(_run_cardkit_compat_hides_tool_argument_summary())


async def _run_cardkit_compat_hides_tool_argument_summary():
    from hermes_multitenancy.feishu_cardkit_compat import ensure_feishu_cardkit_streaming

    adapter = ensure_feishu_cardkit_streaming(_CleanFeishuLikeAdapter())
    started = await adapter.start_streaming_card(chat_id="chat-1")

    await adapter.update_streaming_card_tool_started(
        chat_id="chat-1",
        message_id=started.message_id,
        tool_name="execute_code",
        preview="generating arguments",
        args={"language": "python", "path": "home/generated_art.png"},
    )

    tool_card = adapter.card_patches[-1]["card"]
    _assert_tool_panel(tool_card)
    rendered = _card_text(tool_card)
    assert "generating arguments" not in rendered
    assert "`execute_code` running" in rendered
    assert "language=python" not in rendered
    assert "path=home/generated_art.png" not in rendered


def test_cardkit_compat_cardkit_streams_tool_and_reasoning_without_body_rewrite():
    asyncio.run(_run_cardkit_compat_cardkit_streams_tool_and_reasoning_without_body_rewrite())


async def _run_cardkit_compat_cardkit_streams_tool_and_reasoning_without_body_rewrite():
    from hermes_multitenancy.feishu_cardkit_compat import ensure_feishu_cardkit_streaming

    adapter = ensure_feishu_cardkit_streaming(_OpenClawCardKitAdapter())
    started = await adapter.start_streaming_card(chat_id="chat-1")

    await adapter.update_streaming_card_tool_started(
        chat_id="chat-1",
        message_id=started.message_id,
        tool_name="terminal",
        preview="python -c 'print(1)'",
    )
    await adapter.update_streaming_card_reasoning(
        chat_id="chat-1",
        message_id=started.message_id,
        content="我在检查参数",
    )
    await adapter.update_streaming_card(
        chat_id="chat-1",
        message_id=started.message_id,
        content="最终结果",
        finalize=False,
    )

    assert len(adapter.content_updates) >= 3
    assert adapter.card_updates == []
    streamed_content = [
        request.request_body.content
        for request in adapter.content_updates
        if getattr(request, "element_id", "") == "streaming_content"
    ]
    streamed_tools = [
        request.request_body.content
        for request in adapter.content_updates
        if getattr(request, "element_id", "") == "tool_calls"
    ]
    streamed_reasoning = [
        request.request_body.content
        for request in adapter.content_updates
        if getattr(request, "element_id", "") == "reasoning_content"
    ]
    assert any("- `terminal` running" in item for item in streamed_tools)
    assert all("python -c" not in item for item in streamed_tools)
    assert streamed_reasoning == []
    assert any("我在检查参数" in item for item in streamed_content)
    assert streamed_content[-1] == "最终结果"


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
        "collapsible_panel",
        "collapsible_panel",
        "markdown",
        "markdown",
    ]
    tool_panel = elements[0]
    assert tool_panel["expanded"] is False
    assert tool_panel["header"]["title"]["content"] == "Tool calls"
    assert tool_panel["elements"][0]["content"] == "- `lark_cli` (300 ms)"
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
    _assert_tool_panel(final_card)
    rendered = _card_text(final_card)
    assert "<tool_call>" not in rendered
    assert "lark-cli doc +create" not in rendered
    assert "**Tool calls:**" not in rendered
    assert "- `lark_cli` failed" in rendered
    assert "Done (" in rendered


def test_cardkit_compat_filters_tool_process_narration_from_final_body():
    asyncio.run(_run_cardkit_compat_filters_tool_process_narration_from_final_body())


async def _run_cardkit_compat_filters_tool_process_narration_from_final_body():
    from hermes_multitenancy.feishu_cardkit_compat import ensure_feishu_cardkit_streaming

    adapter = ensure_feishu_cardkit_streaming(_CleanFeishuLikeAdapter())
    started = await adapter.start_streaming_card(chat_id="chat-1")

    await adapter.update_streaming_card_tool_started(
        chat_id="chat-1",
        message_id=started.message_id,
        tool_name="lark_cli",
        preview="im +chat-messages-list",
    )
    await adapter.update_streaming_card_tool_completed(
        chat_id="chat-1",
        message_id=started.message_id,
        tool_name="lark_cli",
        duration=31.289,
        is_error=False,
    )
    await adapter.update_streaming_card(
        chat_id="chat-1",
        message_id=started.message_id,
        content=(
            "我需要先找到你刚发的 markdown 文件。让我先在群聊的最近消息中查找文件。\n\n"
            "让我先获取群聊中最近的消息，找到你发送的 markdown 文件。\n\n"
            "找到了文件消息。现在让我下载这个文件来读取内容。\n\n"
            "文件已下载，让我读取内容。\n\n"
            "已读取你发送的 markdown 文件，文件内容中的**测试内容标记**为：\n\n"
            "`GROUP_FILE_CONTENT_RERUN_20260519_233509`"
        ),
        finalize=True,
    )

    final_card = adapter.card_patches[-1]["card"]
    _assert_tool_panel(final_card)
    rendered = _card_text(final_card)
    assert "我需要先找到" not in rendered
    assert "让我先获取" not in rendered
    assert "现在让我下载" not in rendered
    assert "文件已下载，让我读取内容" not in rendered
    assert "已读取你发送的 markdown 文件" in rendered
    assert "GROUP_FILE_CONTENT_RERUN_20260519_233509" in rendered


def test_cardkit_compat_collapses_argument_generation_tool_rows():
    asyncio.run(_run_cardkit_compat_collapses_argument_generation_tool_rows())


async def _run_cardkit_compat_collapses_argument_generation_tool_rows():
    from hermes_multitenancy.feishu_cardkit_compat import ensure_feishu_cardkit_streaming

    adapter = ensure_feishu_cardkit_streaming(_CleanFeishuLikeAdapter())
    started = await adapter.start_streaming_card(chat_id="chat-1")

    await adapter.update_streaming_card_tool_started(
        chat_id="chat-1",
        message_id=started.message_id,
        tool_name="lark_cli",
        preview="generating arguments",
    )
    await adapter.update_streaming_card_tool_started(
        chat_id="chat-1",
        message_id=started.message_id,
        tool_name="lark_cli",
        preview=None,
        args={"argv": ["auth", "status"]},
    )
    await adapter.update_streaming_card_tool_completed(
        chat_id="chat-1",
        message_id=started.message_id,
        tool_name="lark_cli",
        duration=0.487,
        is_error=False,
    )
    await adapter.update_streaming_card(
        chat_id="chat-1",
        message_id=started.message_id,
        content="CARDKIT_PRIVATE_SPOT\n\n查询结果摘要",
        finalize=True,
    )

    final_card = adapter.card_patches[-1]["card"]
    _assert_tool_panel(final_card)
    rendered = _card_text(final_card)
    assert "generating arguments" not in rendered
    assert rendered.count("`lark_cli`") == 1
    assert "- `lark_cli` (487 ms)" in rendered


def test_cardkit_compat_hides_internal_skill_view_tool_rows():
    asyncio.run(_run_cardkit_compat_hides_internal_skill_view_tool_rows())


async def _run_cardkit_compat_hides_internal_skill_view_tool_rows():
    from hermes_multitenancy.feishu_cardkit_compat import ensure_feishu_cardkit_streaming

    adapter = ensure_feishu_cardkit_streaming(_CleanFeishuLikeAdapter())
    started = await adapter.start_streaming_card(chat_id="chat-1")

    await adapter.update_streaming_card_tool_started(
        chat_id="chat-1",
        message_id=started.message_id,
        tool_name="skill_view",
        preview="lark-im",
    )
    await adapter.update_streaming_card_tool_completed(
        chat_id="chat-1",
        message_id=started.message_id,
        tool_name="skill_view",
        duration=0.037,
        is_error=False,
    )
    await adapter.update_streaming_card_tool_started(
        chat_id="chat-1",
        message_id=started.message_id,
        tool_name="lark_cli",
        preview=None,
    )
    await adapter.update_streaming_card_tool_completed(
        chat_id="chat-1",
        message_id=started.message_id,
        tool_name="lark_cli",
        duration=1.159,
        is_error=False,
    )
    await adapter.update_streaming_card(
        chat_id="chat-1",
        message_id=started.message_id,
        content="CARDKIT_GROUP_SPOT\n\n结果摘要",
        finalize=True,
    )

    final_card = adapter.card_patches[-1]["card"]
    panel = _assert_tool_panel(final_card)
    tool_panel_text = _card_text(panel)
    assert "`skill_view`" not in tool_panel_text
    rendered = _card_text(final_card)
    assert "`skill_view`" not in rendered
    assert "- `lark_cli` (1159 ms)" in rendered


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
    assert initial_card["config"]["summary"] == {
        "content": "Processing...",
        "i18n_content": {"zh_cn": "处理中...", "en_us": "Processing..."},
    }
    assert [
        element["element_id"]
        for element in initial_card["body"]["elements"]
        if "element_id" in element
    ] == [
        "tool_calls",
        "streaming_content",
        "loading_icon",
    ]
    _assert_loading_icon_element(initial_card["body"]["elements"][-1])
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
    tool_panel = final_card["body"]["elements"][0]
    assert tool_panel["tag"] == "collapsible_panel"
    assert tool_panel["expanded"] is False
    assert tool_panel["header"]["title"]["content"] == "Tool calls"
    assert "`lark_cli` (300 ms)" in tool_panel["elements"][0]["content"]
    assert "**Tool calls:**" not in final_text
    assert "Hello CardKit" in final_text
    assert "Done (" in final_text


def test_cardkit_initial_card_has_delay_streaming_loading_and_no_visible_thinking():
    from hermes_multitenancy.feishu_cardkit_compat import _render_cardkit_initial_card

    card = _render_cardkit_initial_card()
    config = card["config"]

    assert config["streaming_mode"] is True
    assert config["streaming_config"]["print_strategy"] == "delay"
    assert config["streaming_config"]["print_frequency_ms"] == {
        "default": 100,
        "android": 100,
        "ios": 100,
        "pc": 100,
    }
    assert config["summary"] == {
        "content": "Processing...",
        "i18n_content": {"zh_cn": "处理中...", "en_us": "Processing..."},
    }
    assert "Thinking" not in json.dumps(card, ensure_ascii=False)
    assert "思考" not in json.dumps(card, ensure_ascii=False)
    assert "CARD DUMP" not in json.dumps(card, ensure_ascii=False)
    assert "multitenancy DEBUG" not in json.dumps(card, ensure_ascii=False)
    element_ids = [
        element["element_id"]
        for element in card["body"]["elements"]
        if "element_id" in element
    ]
    assert element_ids == ["tool_calls", "streaming_content", "loading_icon"]
    assert "status_content" not in element_ids
    assert "reasoning_content" not in element_ids
    _assert_loading_icon_element(card["body"]["elements"][-1])


def test_stream_throttle_timing_matches_openclaw_lark():
    from hermes_multitenancy import router as router_mod

    assert router_mod._STREAM_CARDKIT_CONTENT_MIN_SECONDS == 0.1
    assert router_mod._STREAM_CONTENT_MIN_SECONDS == 1.5


def test_markdown_table_over_limit_degrades_to_openclaw_code_mode():
    from hermes_multitenancy.feishu_cardkit_compat import _optimize_markdown_style
    from hermes_multitenancy.card.markdown_style import _FEISHU_CARD_TABLE_LIMIT

    # Feishu CardKit renders the first _FEISHU_CARD_TABLE_LIMIT tables natively;
    # one MORE than that and the overflow degrades to openclaw code-mode so the
    # card never trips 230099/11310 ("table number over limit").
    n = _FEISHU_CARD_TABLE_LIMIT + 1
    tables = "\n\n".join(
        f"| Name{i} | URL |\n|---|---|\n| **A{i}** | [site{i}](https://example.com) |"
        for i in range(n)
    )
    text = "Before\n\n" + tables + "\n\nAfter"

    optimized = _optimize_markdown_style(text)

    assert "Before" in optimized
    assert "After" in optimized
    # The overflow table is degraded → a code fence appears and its inline
    # markdown is stripped to plain text.
    assert "```" in optimized
    assert f"**A{n - 1}**" not in optimized
    assert f"[site{n - 1}](https://example.com)" not in optimized
    # The first table stays NATIVE → its inline markdown is preserved verbatim
    # (this is the whole point: real tables render in the card).
    assert "**A0**" in optimized
    assert "[site0](https://example.com)" in optimized


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


def test_stream_into_feishu_cardkit_continues_after_visible_segment_limit(
    monkeypatch, tmp_path
):
    asyncio.run(
        _run_stream_into_feishu_cardkit_continues_after_visible_segment_limit(
            monkeypatch,
            tmp_path,
        )
    )


async def _run_stream_into_feishu_cardkit_continues_after_visible_segment_limit(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    async def fake_stream(event, home, *, messages=None):
        yield ("content", "12345")
        yield ("content", "67890")
        yield ("content", "abcdef")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)
    monkeypatch.setattr(router_mod, "GatewayStreamConsumer", None, raising=False)
    monkeypatch.setattr(router_mod, "_STREAM_MAX_VISIBLE_CHARS", 10)

    adapter = _OpenClawCardKitAdapter()

    response = await router_mod._stream_into_feishu(
        adapter,
        "chat-1",
        "profile",
        tmp_path,
        SimpleNamespace(text="hi"),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert response == "1234567890abcdef"
    rendered_final_cards = [
        json.loads(req.request_body.card["data"])
        for req in adapter.card_updates
    ]
    rendered_final_text = "\n".join(
        element.get("content", "")
        for card in rendered_final_cards
        for element in card["body"]["elements"]
        if element.get("tag") == "markdown"
    )
    assert "1234567890" in rendered_final_text
    assert "abcdef" in rendered_final_text
    assert "已截断" not in rendered_final_text


def test_cardkit_compat_tool_events_do_not_rewrite_streaming_content():
    asyncio.run(_run_cardkit_compat_tool_events_do_not_rewrite_streaming_content())


async def _run_cardkit_compat_tool_events_do_not_rewrite_streaming_content():
    from hermes_multitenancy.feishu_cardkit_compat import ensure_feishu_cardkit_streaming

    adapter = ensure_feishu_cardkit_streaming(_OpenClawCardKitAdapter())
    started = await adapter.start_streaming_card(chat_id="chat-1")

    await adapter.update_streaming_card_tool_started(
        chat_id="chat-1",
        message_id=started.message_id,
        tool_name="lark_cli",
        preview="generating arguments",
    )
    await adapter.update_streaming_card_tool_completed(
        chat_id="chat-1",
        message_id=started.message_id,
        tool_name="lark_cli",
        duration=0.4,
        is_error=False,
    )

    assert [
        getattr(request, "element_id", "")
        for request in adapter.content_updates
    ] == ["tool_calls", "tool_calls"]
    assert all(
        getattr(request, "element_id", "") != "streaming_content"
        for request in adapter.content_updates
    )

    await adapter.update_streaming_card(
        chat_id="chat-1",
        message_id=started.message_id,
        content="final answer",
        finalize=True,
    )

    final_card = json.loads(adapter.card_updates[-1].request_body.card["data"])
    _assert_tool_panel(final_card["body"]["elements"])
    final_text = _card_text(final_card["body"]["elements"])
    assert "**Tool calls:**" not in final_text
    assert "`lark_cli` (400 ms)" in final_text
    assert "final answer" in final_text


def test_cardkit_tool_rows_do_not_print_argument_details():
    from hermes_multitenancy.card.tool_use_display import _render_tool_calls_section

    rendered = _render_tool_calls_section(
        [
            {
                "name": "web_search",
                "status": "done",
                "duration": 2.436,
                "args": {"query": "北京天气 2026年5月22日", "limit": 3},
            },
            {
                "name": "web_extract",
                "status": "done",
                "duration": 20.501,
                "args": {
                    "urls": [
                        "https://www.accuweather.com/zh/cn/beijing/101924/daily-weather-forecast/101924"
                    ]
                },
            },
            {
                "name": "lark_cli",
                "status": "done",
                "duration": 0.165,
                "args": {
                    "argv": ["POST", "/open-apis/docx/v1/documents"],
                    "identity": "user",
                    "reason": "创建飞书云文档写入天气信息",
                },
            },
        ]
    )

    assert "`web_search` (2436 ms)" in rendered
    assert "`web_extract` (20501 ms)" in rendered
    assert "`lark_cli` (165 ms)" in rendered
    assert "query=" not in rendered
    assert "北京天气" not in rendered
    assert "limit=3" not in rendered
    assert "urls=" not in rendered
    assert "accuweather.com" not in rendered
    assert "argv=" not in rendered
    assert "/open-apis/docx/v1/documents" not in rendered
    assert "identity=" not in rendered
    assert "reason=" not in rendered


def test_cardkit_tool_rows_keep_failed_status_when_duration_present():
    from hermes_multitenancy.card.tool_use_display import _render_tool_calls_section

    rendered = _render_tool_calls_section(
        [
            {
                "name": "lark_cli",
                "status": "error",
                "duration": 0.165,
                "args": {"argv": ["POST", "/open-apis/docx/v1/documents"]},
            }
        ]
    )

    assert "`lark_cli` failed (165 ms)" in rendered
    assert "argv=" not in rendered


def test_cardkit_invisible_status_does_not_patch_fallback_card():
    asyncio.run(_run_cardkit_invisible_status_does_not_patch_fallback_card())


async def _run_cardkit_invisible_status_does_not_patch_fallback_card():
    from hermes_multitenancy.feishu_cardkit_compat import ensure_feishu_cardkit_streaming

    adapter = ensure_feishu_cardkit_streaming(_CleanFeishuLikeAdapter())
    started = await adapter.start_streaming_card(chat_id="chat-1")

    result = await adapter.update_streaming_card_status(
        chat_id="chat-1",
        message_id=started.message_id,
        content="\u200b",
    )

    assert result.success is True
    assert adapter.card_patches == []


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
    assert all(element.get("tag") == "markdown" for element in final_card["elements"])
    assert final_card["elements"][-1]["tag"] == "markdown"
    rendered = _card_text(final_card)
    assert "Compat survives legacy consumer" in rendered
    assert "Done (" in rendered


def test_stream_into_feishu_uses_gateway_stream_consumer_for_card_transport(
    monkeypatch, tmp_path
):
    asyncio.run(
        _run_stream_into_feishu_uses_gateway_stream_consumer_for_card_transport(
            monkeypatch,
            tmp_path,
        )
    )


async def _run_stream_into_feishu_uses_gateway_stream_consumer_for_card_transport(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    async def fake_stream(event, home, *, messages=None):
        yield ("thinking", "checking")
        yield (
            "tool_started",
            {
                "name": "feishu_calendar_list_events",
                "preview": str(home / "home" / "generated_art.png"),
                "args": {
                    "path": str(home / "workspace" / "docs" / "plan.md"),
                    "host_path": "/Users/kite/.ssh/id_rsa",
                },
            },
        )
        yield (
            "tool_completed",
            {"name": "feishu_calendar_list_events", "duration": 0.2, "is_error": False},
        )
        yield ("content", "Hello")
        yield ("content", " world")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    created = []

    class RecordingConsumer:
        def __init__(
            self,
            adapter,
            chat_id,
            config=None,
            metadata=None,
            initial_reply_to_id=None,
        ):
            self.adapter = adapter
            self.chat_id = chat_id
            self.config = config
            self.metadata = metadata
            self.initial_reply_to_id = initial_reply_to_id
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
        SimpleNamespace(
            text="hi",
            message_id="om_shared_group",
            source=SimpleNamespace(chat_type="group"),
        ),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert response == "Hello world"
    assert len(created) == 1
    consumer = created[0]
    assert consumer.adapter is adapter
    assert consumer.chat_id == "chat-1"
    assert consumer.initial_reply_to_id == "om_shared_group"
    assert consumer.config.edit_interval <= 0.3
    assert consumer.config.buffer_threshold <= 40
    assert [
        router_mod._strip_stream_status_animation_markers(status)
        for status in consumer.statuses
    ] == [""]
    assert consumer.reasoning == ["checking"]
    assert consumer.tool_starts == [
        {
            "tool_name": "feishu_calendar_list_events",
            "preview": "home/generated_art.png",
            "args": {"path": "workspace/docs/plan.md", "host_path": "[宿主路径已隐藏]"},
        }
    ]
    assert consumer.tool_completions == [
        {"tool_name": "feishu_calendar_list_events", "duration": 0.2, "is_error": False}
    ]
    assert consumer.deltas == ["Hello", " world"]
    assert consumer.finished is True


def test_shared_consumer_flushes_short_reasoning_before_first_content(monkeypatch, tmp_path):
    asyncio.run(
        _run_shared_consumer_flushes_short_reasoning_before_first_content(
            monkeypatch,
            tmp_path,
        )
    )


async def _run_shared_consumer_flushes_short_reasoning_before_first_content(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    async def fake_stream(event, home, *, messages=None):
        yield ("thinking", "first ")
        yield ("thinking", "second")
        yield ("content", "answer")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    created = []

    class RecordingConsumer:
        def __init__(
            self,
            adapter,
            chat_id,
            config=None,
            metadata=None,
            initial_reply_to_id=None,
        ):
            self.reasoning = []
            self.statuses = []
            self.deltas = []
            self.initial_reply_to_id = initial_reply_to_id
            self.tool_starts = []
            self.tool_completions = []
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
            self._done.set()

    monkeypatch.setattr(router_mod, "GatewayStreamConsumer", RecordingConsumer, raising=False)
    monkeypatch.setattr(
        router_mod,
        "StreamConsumerConfig",
        lambda **kwargs: SimpleNamespace(**kwargs),
        raising=False,
    )

    response = await router_mod._stream_into_feishu(
        _CardCapableAdapter(),
        "chat-1",
        "profile",
        tmp_path,
        SimpleNamespace(text="hi"),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert response == "answer"
    assert created[0].reasoning[-1] == "first second"
    assert created[0].deltas == ["answer"]


def test_stream_card_idle_status_emits_invisible_marker_while_refreshing():
    """Idle heartbeat keeps the card refreshing via rotating zero-width markers
    only — no visible waiting text (no `Thinking...`, no dots). The marker
    cycles each tick so Feishu does not dedupe identical payloads."""
    from hermes_multitenancy import router as router_mod

    statuses = [router_mod._stream_card_idle_status(i) for i in range(1, 5)]
    assert len(set(statuses)) == 4
    assert [
        router_mod._strip_stream_status_animation_markers(status)
        for status in statuses
    ] == ["", "", "", ""]


@pytest.mark.asyncio
async def test_shared_consumer_stream_does_not_truncate_at_visible_limit(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    async def fake_stream(event, home, *, messages=None):
        yield ("content", "12345")
        yield ("content", "67890")
        yield ("content", "abcdef")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)
    monkeypatch.setattr(router_mod, "_STREAM_MAX_VISIBLE_CHARS", 10)

    created = []

    class RecordingConsumer:
        def __init__(
            self,
            adapter,
            chat_id,
            config=None,
            metadata=None,
            initial_reply_to_id=None,
        ):
            self.adapter = adapter
            self.chat_id = chat_id
            self.config = config
            self.metadata = metadata
            self.initial_reply_to_id = initial_reply_to_id
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

    response = await router_mod._stream_into_feishu(
        _CardCapableAdapter(),
        "chat-1",
        "profile",
        tmp_path,
        SimpleNamespace(text="hi"),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert response == "1234567890abcdef"
    assert created[0].deltas == ["12345", "67890", "abcdef"]
    assert all("已截断" not in delta for delta in created[0].deltas)


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


def test_handle_async_keeps_processing_reaction_until_media_delivery_finishes(
    monkeypatch, tmp_path
):
    asyncio.run(
        _run_handle_async_keeps_processing_reaction_until_media_delivery_finishes(
            monkeypatch, tmp_path
        )
    )


async def _run_handle_async_keeps_processing_reaction_until_media_delivery_finishes(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    profile_home = tmp_path / "profiles" / "ou_media_life"
    profile_home.mkdir(parents=True)
    source = profile_home / ".ai-docs" / "report.md"
    source.parent.mkdir(parents=True)
    source.write_text("# report", encoding="utf-8")
    add_spike_route("ou_media_life", profile_home)

    async def fake_stream_into_feishu(*args, **kwargs):
        return f"方案已保存：{source}"

    monkeypatch.setattr(router_mod, "_stream_into_feishu", fake_stream_into_feishu)

    adapter = _DeferredLifecycleMediaAdapter()
    gateway = SimpleNamespace(adapters={"feishu": adapter})
    event = SimpleNamespace(
        text="生成报告",
        message_id="om_media_life",
        source=SimpleNamespace(
            chat_id="chat-life",
            user_id="ou_media_life",
            user_id_alt=None,
            user_name="tester",
            chat_type="dm",
            platform=SimpleNamespace(value="feishu"),
        ),
    )

    await router_mod.handle_async(event=event, gateway=gateway)

    assert adapter.lifecycle == [
        ("start", "om_media_life"),
        ("send_document", None, "report.md"),
        ("complete_deferred", "ProcessingOutcome.SUCCESS"),
    ]

    clear_spike_routes()


def test_deliver_profile_scoped_media_preserves_group_reply_to(tmp_path):
    asyncio.run(_run_deliver_profile_scoped_media_preserves_group_reply_to(tmp_path))


async def _run_deliver_profile_scoped_media_preserves_group_reply_to(tmp_path):
    from hermes_multitenancy import router as router_mod

    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    source = tmp_path / "group-report.md"
    source.write_text("# group report", encoding="utf-8")

    adapter = _DeferredLifecycleMediaAdapter()
    event = SimpleNamespace(
        message_id="om_group_media",
        source=SimpleNamespace(chat_id="group-chat", chat_type="group"),
    )

    delivered = await router_mod._deliver_profile_scoped_media_directives(
        adapter,
        event,
        SimpleNamespace(),
        f"MEDIA:{source}",
        profile_home=profile_home,
    )

    assert delivered == 1
    assert adapter.lifecycle == [
        ("send_document", "om_group_media", "group-report.md"),
    ]


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
            SimpleNamespace(
                text="hi",
                message_id="om_source_prime",
                source=SimpleNamespace(chat_type="group"),
            ),
            messages=[{"role": "user", "content": "hi"}],
        )
    )

    await asyncio.wait_for(stream_entered.wait(), timeout=1)

    assert adapter.started == [
        {"chat_id": "chat-1", "reply_to": "om_source_prime", "metadata": None}
    ]
    assert len(adapter.status_updates) == 1
    assert router_mod._strip_stream_status_animation_markers(
        adapter.status_updates[0]["content"]
    ) == ""
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
    assert [
        router_mod._strip_stream_status_animation_markers(update["content"])
        for update in adapter.status_updates[:2]
    ] == ["", ""]
    assert adapter.reasoning_updates == []

    release_stream.set()
    assert await task == "ready"
    assert adapter.updates[-1]["content"] == "ready"
    assert adapter.updates[-1]["finalize"] is True


def test_stream_into_feishu_keeps_status_animating_after_tool_event(monkeypatch, tmp_path):
    asyncio.run(
        _run_stream_into_feishu_keeps_status_animating_after_tool_event(
            monkeypatch,
            tmp_path,
        )
    )


async def _run_stream_into_feishu_keeps_status_animating_after_tool_event(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    monkeypatch.setattr(router_mod, "_STREAM_CARD_IDLE_HEARTBEAT_SECONDS", 0.01)
    adapter = _CardCapableAdapter()
    tool_started = asyncio.Event()
    release_stream = asyncio.Event()

    async def fake_stream(event, home, *, messages=None):
        yield ("tool_started", {"name": "execute_code", "args": {"language": "python"}})
        tool_started.set()
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

    await asyncio.wait_for(tool_started.wait(), timeout=1)

    async def wait_for_status_heartbeat():
        while len(adapter.status_updates) < 2:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait_for_status_heartbeat(), timeout=1)
    assert [
        router_mod._strip_stream_status_animation_markers(update["content"])
        for update in adapter.status_updates[:2]
    ] == ["", ""]
    assert adapter.tool_starts[0]["tool_name"] == "execute_code"

    release_stream.set()
    assert await task == "ready"


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
            if content:
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
        SimpleNamespace(text="hi", message_id="om_source_tool"),
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
    assert len(adapter.status_updates) == 1
    assert router_mod._strip_stream_status_animation_markers(
        adapter.status_updates[0]["content"]
    ) == ""
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


@pytest.mark.asyncio
async def test_stream_into_feishu_status_event_updates_card_without_polluting_content(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    async def fake_stream(event, home, *, messages=None):
        yield ("content", "Phase 2: 子 Agent 并行验证\n")
        yield ("status", "Hermes 正在等待当前工具或子任务输出，已等待 315 秒。")
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

    assert response == "Phase 2: 子 Agent 并行验证\ndone"
    assert adapter.status_updates[-1] == {
        "chat_id": "chat-1",
        "message_id": "card-1",
        "content": "Hermes 正在等待当前工具或子任务输出，已等待 315 秒。",
    }
    assert adapter.updates[-1]["content"] == "Phase 2: 子 Agent 并行验证\ndone"
    assert "315 秒" not in adapter.updates[-1]["content"]


@pytest.mark.asyncio
async def test_stream_into_feishu_idle_timeout_finalizes_without_nonstream_rerun(
    monkeypatch, tmp_path
):
    from hermes_multitenancy import agent_real, router as router_mod

    async def fake_stream(event, home, *, messages=None):
        yield ("content", "Phase 2 running")
        raise RuntimeError("AIAgent subprocess produced no stream events for 1200s")

    async def fail_nonstream(*_args, **_kwargs):
        raise AssertionError("idle timeout must not rerun the whole agent")

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)
    monkeypatch.setattr(agent_real, "real_run_agent", fail_nonstream)

    adapter = _CardCapableAdapter()
    response = await router_mod._stream_into_feishu(
        adapter,
        "chat-1",
        "profile",
        tmp_path,
        SimpleNamespace(text="hi"),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert "Phase 2 running" in response
    assert "produced no stream events" in response
    assert adapter.status_updates[-1]["content"] == "任务长时间没有新的运行事件，已停止。"
    assert "produced no stream events" in adapter.updates[-1]["content"]
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
    assert [
        router_mod._strip_stream_status_animation_markers(item["content"])
        for item in adapter.status_updates
    ] == [""]
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
        SimpleNamespace(
            text="hi",
            message_id="om_source_fallback",
            source=SimpleNamespace(chat_type="group"),
        ),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert response == "fallback text"
    assert adapter.started == [
        {"chat_id": "chat-1", "reply_to": "om_source_fallback", "metadata": None}
    ]
    assert adapter.sent == [
        {
            "chat_id": "chat-1",
            "content": "\u200b",
            "reply_to": "om_source_fallback",
            "metadata": None,
        }
    ]
    assert adapter.edits[-1]["content"] == "fallback text"
    assert adapter.edits[-1]["finalize"] is True
    assert adapter.updates == []


@pytest.mark.asyncio
async def test_stream_into_feishu_text_fallback_omits_dm_reply_to(
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
        "dm-chat",
        "profile",
        tmp_path,
        SimpleNamespace(
            text="hi",
            message_id="om_dm_fallback",
            source=SimpleNamespace(chat_type="dm"),
        ),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert response == "fallback text"
    assert adapter.started == [
        {"chat_id": "dm-chat", "reply_to": None, "metadata": None}
    ]
    assert adapter.sent == [
        {
            "chat_id": "dm-chat",
            "content": "\u200b",
            "reply_to": None,
            "metadata": None,
        }
    ]
