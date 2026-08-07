"""feishu-todo-progress-card — one stable todo row in the tool panel.

Covers the SPEC targeted tests:
- formatter: n/m counts, in_progress highlight, merge/read/malformed skips, truncation
- router path: todo is forwarded once as a tool event and never as body status
- CardKit path: repeated todo writes update one row and only the tool element
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from hermes_multitenancy.card import streaming_controller as streaming_mod
from hermes_multitenancy.card.state import _new_state, _states
from hermes_multitenancy.card.tool_use_display import (
    _TODO_PROGRESS_MAX_CHARS,
    _format_todo_progress,
    build_live_tool_use_panel,
)
from hermes_multitenancy.router.streaming import _update_feishu_stream_tool_event


def _todos(*statuses: str) -> list[dict]:
    return [
        {"id": str(i), "content": f"步骤{i}", "status": status}
        for i, status in enumerate(statuses, start=1)
    ]


# ---------------------------------------------------------------- formatter

def test_format_counts_and_icons():
    text = _format_todo_progress(
        {"todos": _todos("completed", "completed", "in_progress", "pending")}
    )
    assert text.startswith("任务进度 2/4 ✅✅●○")
    assert "步骤3" in text  # in_progress content is spelled out
    assert "步骤1" not in text  # completed items stay icon-only


def test_format_cancelled_icon_and_unknown_status_is_malformed():
    assert _format_todo_progress({"todos": _todos("cancelled")}).startswith("任务进度 0/1 ✕")
    assert _format_todo_progress(
        {"todos": [{"id": "2", "content": "x", "status": "bogus"}]}
    ) == ""


def test_format_skips_merge_reads_and_malformed():
    assert _format_todo_progress({"todos": _todos("pending"), "merge": True}) == ""
    assert _format_todo_progress({}) == ""  # read call: no todos param
    assert _format_todo_progress(None) == ""
    assert _format_todo_progress({"todos": []}) == ""
    assert _format_todo_progress({"todos": ["not-a-dict"]}) == ""


def test_format_truncates_to_cap():
    todos = [{"id": "1", "content": "长" * 300, "status": "in_progress"}]
    text = _format_todo_progress({"todos": todos})
    assert len(text) <= _TODO_PROGRESS_MAX_CHARS


def test_progress_numerator_counts_completed_items_only():
    assert _format_todo_progress({"todos": _todos("pending")}).startswith("任务进度 0/1")
    assert _format_todo_progress({"todos": _todos("in_progress")}).startswith("任务进度 0/1")
    assert _format_todo_progress({"todos": _todos("completed")}).startswith("任务进度 1/1")


# ------------------------------------------------- adapter-surface card path

class _FakeAdapter:
    def __init__(self, status_raises: bool = False):
        self.status_calls: list[dict] = []
        self.tool_started_calls: list[dict] = []
        self._status_raises = status_raises

    async def update_streaming_card_status(self, **kwargs):
        if self._status_raises:
            raise RuntimeError("boom")
        self.status_calls.append(kwargs)
        return SimpleNamespace(success=True)

    async def update_streaming_card_tool_started(self, **kwargs):
        self.tool_started_calls.append(kwargs)
        return SimpleNamespace(success=True)

    async def update_streaming_card_tool_completed(self, **kwargs):
        return SimpleNamespace(success=True)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_todo_write_only_pushes_one_tool_event():
    adapter = _FakeAdapter()
    _run(
        _update_feishu_stream_tool_event(
            adapter,
            "chat-1",
            "msg-1",
            {"name": "todo", "args": {"todos": _todos("completed", "in_progress")}},
            mode="card",
            completed=False,
        )
    )
    assert adapter.status_calls == []
    assert len(adapter.tool_started_calls) == 1


def test_non_todo_tool_and_todo_read_never_touch_status():
    adapter = _FakeAdapter()
    _run(
        _update_feishu_stream_tool_event(
            adapter, "c", "m", {"name": "terminal", "args": {"cmd": "ls"}},
            mode="card", completed=False,
        )
    )
    _run(
        _update_feishu_stream_tool_event(
            adapter, "c", "m", {"name": "todo", "args": None},
            mode="card", completed=False,
        )
    )
    assert adapter.status_calls == []
    assert len(adapter.tool_started_calls) == 2


def test_malformed_payload_logs_debug_but_normal_skips_stay_silent(caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        assert _format_todo_progress({"todos": "not-a-list"}) == ""  # malformed
        assert _format_todo_progress({"todos": ["str-item"]}) == ""  # malformed
    malformed_logs = [r for r in caplog.records if "malformed todo" in r.message]
    assert len(malformed_logs) == 2
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        assert _format_todo_progress({}) == ""  # read — normal
        assert _format_todo_progress({"todos": [], "merge": True}) == ""  # merge — normal
    assert [r for r in caplog.records if "malformed todo" in r.message] == []


# --------------------------------------------------- todo tool availability

def test_todo_tool_available_resolution():
    from hermes_multitenancy.agent_real._core import _todo_tool_available

    # merge_default / core default → available
    assert _todo_tool_available(None, None) is True
    # profile disabled wins over everything
    assert _todo_tool_available(None, ["todo"]) is False
    assert _todo_tool_available(["todo"], ["todo"]) is False
    # explicit list naming todo directly
    assert _todo_tool_available(["file", "todo"], None) is True
    # explicit list WITHOUT todo — must fail closed (review HIGH finding)
    assert _todo_tool_available(["file"], None) is False
    assert _todo_tool_available(["lark-cli"], None) is False
    # composite toolset that includes todo transitively resolves through core
    assert _todo_tool_available(["hermes-api-server"], None) is True
    assert _todo_tool_available(None, ["hermes-api-server"]) is False
    # malformed scope evidence must fail closed
    assert _todo_tool_available(None, "todo") is False
    assert _todo_tool_available(["todo"], {"unexpected": "shape"}) is False


def test_direct_todo_disable_does_not_depend_on_composite_resolver(monkeypatch):
    import toolsets
    from hermes_multitenancy.agent_real._core import _todo_tool_available

    def reject_bare_todo(items):
        assert "todo" not in items
        return []

    monkeypatch.setattr(toolsets, "resolve_multiple_toolsets", reject_bare_todo)
    assert _todo_tool_available(None, ["todo"]) is False


def test_status_updater_is_never_called_for_todo():
    adapter = _FakeAdapter(status_raises=True)
    _run(
        _update_feishu_stream_tool_event(
            adapter, "c", "m",
            {"name": "todo", "args": {"todos": _todos("in_progress")}},
            mode="card", completed=False,
        )
    )
    assert adapter.status_calls == []
    assert len(adapter.tool_started_calls) == 1


def test_repeated_todo_writes_update_one_tool_row(monkeypatch):
    writes: list[tuple[str, str]] = []

    async def fake_element(_adapter, _card_id, element_id, content, _sequence):
        writes.append((element_id, content))

    async def body_write_must_not_run(*_args, **_kwargs):
        raise AssertionError("todo progress must not write streaming_content")

    monkeypatch.setattr(streaming_mod, "_stream_cardkit_element", fake_element)
    monkeypatch.setattr(streaming_mod, "_stream_cardkit_content", body_write_must_not_run)

    async def exercise():
        adapter = SimpleNamespace()
        state = _new_state()
        state["card_id"] = "card-1"
        _states(adapter)["msg-1"] = state
        updates = [
            _todos("in_progress", "pending", "pending"),
            _todos("completed", "in_progress", "pending"),
            _todos("completed", "completed", "in_progress"),
            _todos("completed", "completed", "completed"),
        ]
        for todos in updates:
            before = len(writes)
            await streaming_mod._update_streaming_card_tool_started(
                adapter,
                chat_id="chat-1",
                message_id="msg-1",
                tool_name="todo",
                args={"todos": todos},
            )
            assert len(writes) == before + 1
            assert writes[-1][0] == "tool_calls"
            assert len([tool for tool in state["tools"] if tool["name"] == "todo"]) == 1
            before = len(writes)
            await streaming_mod._update_streaming_card_tool_completed(
                adapter,
                chat_id="chat-1",
                message_id="msg-1",
                tool_name="todo",
                duration=0.01,
            )
            assert len(writes) == before + 1
            assert writes[-1][0] == "tool_calls"
            assert len([tool for tool in state["tools"] if tool["name"] == "todo"]) == 1

        # Duplicate completion delivery still updates the same row.
        before = len(writes)
        await streaming_mod._update_streaming_card_tool_completed(
            adapter,
            chat_id="chat-1",
            message_id="msg-1",
            tool_name="todo",
            duration=0.02,
        )
        assert len(writes) == before + 1
        assert len([tool for tool in state["tools"] if tool["name"] == "todo"]) == 1
        return state

    state = _run(exercise())
    assert len(state["tools"]) == 1
    assert state["tools"][0]["todo_progress"].startswith("任务进度 3/3 ✅✅✅")
    assert len(writes) == 9  # one element write per started/completed event
    assert {element_id for element_id, _content in writes} == {"tool_calls"}
    assert writes[0][1].startswith("- 任务进度 0/3 ●○○")
    assert writes[-1][1].startswith("- 任务进度 3/3 ✅✅✅")

    final_panel = json.dumps(build_live_tool_use_panel(state["tools"]), ensure_ascii=False)
    assert final_panel.count("**Todo") == 1
    assert "任务进度 3/3 ✅✅✅" in final_panel

    state["tools"].extend(
        {"name": f"tool-{index}", "status": "done"} for index in range(25)
    )
    capped_panel = json.dumps(build_live_tool_use_panel(state["tools"]), ensure_ascii=False)
    assert capped_panel.count("**Todo") == 1
    assert "任务进度 3/3 ✅✅✅" in capped_panel


def test_malformed_todo_replaces_stale_progress_with_ordinary_row(monkeypatch):
    async def fake_element(*_args, **_kwargs):
        return None

    monkeypatch.setattr(streaming_mod, "_stream_cardkit_element", fake_element)

    async def exercise():
        adapter = SimpleNamespace()
        state = _new_state()
        state["card_id"] = "card-1"
        _states(adapter)["msg-1"] = state
        await streaming_mod._update_streaming_card_tool_started(
            adapter, chat_id="chat-1", message_id="msg-1", tool_name="todo",
            args={"todos": _todos("in_progress")},
        )
        await streaming_mod._update_streaming_card_tool_started(
            adapter, chat_id="chat-1", message_id="msg-1", tool_name="todo",
            args={"todos": "malformed"},
        )
        return state

    state = _run(exercise())
    assert len(state["tools"]) == 1
    assert "todo_progress" not in state["tools"][0]
