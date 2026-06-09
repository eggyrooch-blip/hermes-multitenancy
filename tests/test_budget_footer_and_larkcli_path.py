from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest


def _event() -> SimpleNamespace:
    return SimpleNamespace(
        text="hello",
        message_id="om_test",
        source=SimpleNamespace(
            platform=SimpleNamespace(value="feishu"),
            chat_id="oc_test",
            chat_name="chat",
            chat_type="dm",
            user_id="ou_test",
            user_name="tester",
            user_id_alt="on_test",
            message_id="om_source",
        ),
    )


def test_budget_footer_appended_when_turns_reach_cap():
    from hermes_multitenancy import agent_real

    result = agent_real._maybe_budget_footer("PARTIAL PROGRESS", tool_turns=30, cap=30)

    assert result.startswith("PARTIAL PROGRESS")
    assert "已用满 30 步工具预算" in result


def test_budget_footer_absent_below_cap():
    from hermes_multitenancy import agent_real

    assert agent_real._maybe_budget_footer("done early", tool_turns=5, cap=30) == "done early"


def test_budget_footer_idempotent():
    from hermes_multitenancy import agent_real

    once = agent_real._maybe_budget_footer("PARTIAL PROGRESS", tool_turns=30, cap=30)
    twice = agent_real._maybe_budget_footer(once, tool_turns=30, cap=30)

    assert twice == once
    assert twice.count("已用满 30 步工具预算") == 1


@pytest.mark.asyncio
async def test_stream_run_agent_appends_budget_footer_when_cap_reached(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    async def fake_stream(event, profile_home):
        assert event.text == "hello"
        assert profile_home == tmp_path
        yield ("tool_started", {"name": "tool-1"})
        yield ("tool_started", {"name": "tool-2"})
        yield ("tool_started", {"name": "tool-3"})
        yield ("content", "streamed ")
        yield ("content", "partial progress")
        yield ("done", "final ignored because content already streamed")

    monkeypatch.setenv("HERMES_MAX_ITERATIONS", "3")
    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", fake_stream, raising=False)

    chunks = [chunk async for chunk in agent_real.stream_run_agent(_event(), tmp_path)]
    content_chunks = [payload for kind, payload in chunks if kind == "content"]

    assert "".join(content_chunks).startswith("streamed partial progress")
    assert "已用满 3 步工具预算" in content_chunks[-1]


@pytest.mark.asyncio
async def test_stream_run_agent_does_not_append_budget_footer_below_cap(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    async def fake_stream(event, profile_home):
        assert event.text == "hello"
        assert profile_home == tmp_path
        yield ("tool_started", {"name": "tool-1"})
        yield ("tool_started", {"name": "tool-2"})
        yield ("content", "done early")
        yield ("done", "done early")

    monkeypatch.setenv("HERMES_MAX_ITERATIONS", "3")
    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", fake_stream, raising=False)

    chunks = [chunk async for chunk in agent_real.stream_run_agent(_event(), tmp_path)]
    content_chunks = [payload for kind, payload in chunks if kind == "content"]

    assert content_chunks == ["done early"]
    assert not any("已用满 3 步工具预算" in chunk for chunk in content_chunks)


def test_shared_bin_precedes_node_modules_in_subprocess_path(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    profile_home = shared_home / "profiles" / "alice"
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()
    shared_bin = str(agent_real._resolve_shared_hermes_home(profile_home) / "bin")
    node_bin = str(tmp_path / "node_modules" / ".bin")
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join([node_bin, shared_bin, "/usr/bin"]),
    )

    env = agent_real._build_subprocess_env(profile_home, approval_dir=approval_dir)
    path_parts = env["PATH"].split(os.pathsep)

    assert path_parts[0] == shared_bin
    assert path_parts.count(shared_bin) == 1
    assert path_parts.index(node_bin) > path_parts.index(shared_bin)
