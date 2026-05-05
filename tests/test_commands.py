"""US-009 — Slash command parsing + /stop /status /new dispatch."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


def _build_event(text: str, user_id: str = "ou_cmd", chat_id: str = "chat-cmd"):
    return SimpleNamespace(
        text=text,
        source=SimpleNamespace(
            chat_id=chat_id,
            user_id=user_id,
            user_name="cmd-user",
            chat_type="dm",
            platform=SimpleNamespace(value="feishu"),
        ),
    )


# -- parse_command ----------------------------------------------------------

def test_parse_known_commands():
    from hermes_multitenancy.commands import parse_command
    assert parse_command("/stop") == ("stop", "")
    assert parse_command("/status") == ("status", "")
    assert parse_command("/new") == ("new", "")
    assert parse_command("/model glm-5.1") == ("model", "glm-5.1")
    assert parse_command("/reasoning high") == ("reasoning", "high")
    assert parse_command("/reload_mcp") == ("reload-mcp", "")
    assert parse_command("/STOP") == ("stop", "")
    assert parse_command("/stop now please") == ("stop", "now please")


def test_parse_unknown_slash_is_still_handled():
    from hermes_multitenancy.commands import parse_command
    assert parse_command("/unknown") == ("unknown", "")
    assert parse_command("hello") is None
    assert parse_command("") is None
    assert parse_command("/") is None


def test_parse_rejects_paths():
    from hermes_multitenancy.commands import parse_command
    assert parse_command("/some/path") is None


def test_parse_command_reads_hermes_registry_dynamically(monkeypatch):
    """New Hermes COMMAND_REGISTRY entries must not require a plugin edit."""
    from types import SimpleNamespace
    import sys

    fake_commands = SimpleNamespace(
        resolve_command=lambda name: SimpleNamespace(name="fresh")
        if name.lower().lstrip("/") in {"fresh", "fresh_alias"}
        else None,
        is_gateway_known_command=lambda name: name in {"fresh", "fresh_alias"},
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.commands", fake_commands)

    from hermes_multitenancy.commands import parse_command

    assert parse_command("/fresh_alias arg") == ("fresh", "arg")


# -- /stop ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_cancels_inflight_task(monkeypatch):
    """/stop must cancel the user's currently-running dispatch."""
    from hermes_multitenancy.router import handle_async, _user_inflight_tasks
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy import runtime as runtime_mod

    clear_spike_routes()
    _user_inflight_tasks.clear()

    cancelled_evt = asyncio.Event()

    async def slow_runner(event, home):
        try:
            await asyncio.sleep(60)
            return "never"
        except asyncio.CancelledError:
            cancelled_evt.set()
            raise

    monkeypatch.setattr(runtime_mod, "_default_run_agent", slow_runner)

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        add_spike_route("ou_stop", home)

        sends = []

        class Adapter:
            async def send_typing(self, c): pass
            async def send(self, c, m, *, reply_to=None, metadata=None):
                sends.append(m)

        gateway = SimpleNamespace(adapters={"feishu": Adapter()})

        # Fire a long-running dispatch
        slow_task = asyncio.create_task(
            handle_async(event=_build_event("hello", user_id="ou_stop"), gateway=gateway)
        )
        # Wait until it registers in the inflight map
        for _ in range(50):
            await asyncio.sleep(0.005)
            if "ou_stop" in _user_inflight_tasks:
                break
        assert "ou_stop" in _user_inflight_tasks

        # Now send /stop — should cancel the slow task
        await handle_async(event=_build_event("/stop", user_id="ou_stop"), gateway=gateway)

        # The slow runner's CancelledError handler must have fired
        await asyncio.wait_for(cancelled_evt.wait(), timeout=1.0)
        with pytest.raises(asyncio.CancelledError):
            await slow_task

        # /stop reply was sent
        assert any("已停止当前任务" in s for s in sends), sends
        assert "ou_stop" not in _user_inflight_tasks

    clear_spike_routes()


@pytest.mark.asyncio
async def test_stop_when_idle_replies_no_inflight():
    from hermes_multitenancy.router import handle_async, _user_inflight_tasks

    _user_inflight_tasks.clear()
    sends = []

    class Adapter:
        async def send_typing(self, c): pass
        async def send(self, c, m, *, reply_to=None, metadata=None):
            sends.append(m)

    gateway = SimpleNamespace(adapters={"feishu": Adapter()})
    await handle_async(event=_build_event("/stop", user_id="ou_idle"), gateway=gateway)
    assert any("没有进行中的任务" in s for s in sends), sends


# -- /status ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_reports_idle_when_no_inflight():
    from hermes_multitenancy.router import handle_async, _user_inflight_tasks

    _user_inflight_tasks.clear()
    sends = []

    class Adapter:
        async def send_typing(self, c): pass
        async def send(self, c, m, *, reply_to=None, metadata=None):
            sends.append(m)

    gateway = SimpleNamespace(adapters={"feishu": Adapter()})
    await handle_async(event=_build_event("/status", user_id="ou_s"), gateway=gateway)
    assert any("空闲" in s for s in sends), sends


@pytest.mark.asyncio
async def test_status_reports_running_when_inflight(monkeypatch):
    from hermes_multitenancy.router import handle_async, _user_inflight_tasks
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy import runtime as runtime_mod

    clear_spike_routes()
    _user_inflight_tasks.clear()

    block = asyncio.Event()

    async def slow_runner(event, home):
        await block.wait()
        return "ok"

    monkeypatch.setattr(runtime_mod, "_default_run_agent", slow_runner)

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        add_spike_route("ou_run", home)

        sends = []

        class Adapter:
            async def send_typing(self, c): pass
            async def send(self, c, m, *, reply_to=None, metadata=None):
                sends.append(m)

        gateway = SimpleNamespace(adapters={"feishu": Adapter()})

        running = asyncio.create_task(
            handle_async(event=_build_event("hi", user_id="ou_run"), gateway=gateway)
        )
        for _ in range(50):
            await asyncio.sleep(0.005)
            if "ou_run" in _user_inflight_tasks:
                break

        await handle_async(event=_build_event("/status", user_id="ou_run"), gateway=gateway)
        assert any("运行中" in s for s in sends), sends

        block.set()
        await running

    clear_spike_routes()


# -- /new and /reset --------------------------------------------------------

@pytest.mark.asyncio
async def test_new_command_resets_session_history():
    """/new clears the per-user history when there is a route, replies otherwise."""
    from hermes_multitenancy.router import (
        handle_async,
        _user_inflight_tasks,
        _session_history,
        _history_key,
    )
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    import tempfile

    _user_inflight_tasks.clear()
    _session_history.clear()
    clear_spike_routes()

    sends = []

    class Adapter:
        async def send_typing(self, c): pass
        async def send(self, c, m, *, reply_to=None, metadata=None):
            sends.append(m)

    gateway = SimpleNamespace(adapters={"feishu": Adapter()})

    # Case 1: routed user with prior history → reset acknowledges
    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path
        home = Path(tmp)
        add_spike_route("ou_resetuser", home)
        key = _history_key(home.name, "ou_resetuser", None)
        _session_history[key] = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "old"}]

        await handle_async(event=_build_event("/new", user_id="ou_resetuser"), gateway=gateway)
        assert sends and "重置" in sends[-1], sends
        assert key not in _session_history, "history should be cleared"

    # Case 2: unrouted user → reply explains
    sends.clear()
    clear_spike_routes()
    await handle_async(event=_build_event("/new", user_id="ou_unrouted"), gateway=gateway)
    assert sends and "未路由" in sends[-1], sends


@pytest.mark.asyncio
async def test_help_command_lists_commands():
    from hermes_multitenancy.router import handle_async, _user_inflight_tasks
    _user_inflight_tasks.clear()
    sends = []

    class Adapter:
        async def send_typing(self, c): pass
        async def send(self, c, m, *, reply_to=None, metadata=None):
            sends.append(m)

    gateway = SimpleNamespace(adapters={"feishu": Adapter()})
    await handle_async(event=_build_event("/help", user_id="ou_h"), gateway=gateway)
    assert sends, "help should reply"
    text = sends[-1]
    assert "/help" in text
    assert "/status" in text
    assert "/new" in text
    assert "/stop" in text


@pytest.mark.asyncio
async def test_known_hermes_command_uses_gateway_handler_not_agent(monkeypatch, tmp_path, caplog):
    """/model and future Hermes commands should be handled by Hermes, not sent to AIAgent."""
    from hermes_multitenancy.router import handle_async, _user_inflight_tasks
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy import router as router_mod

    caplog.set_level("INFO")
    clear_spike_routes()
    _user_inflight_tasks.clear()
    add_spike_route("ou_model", tmp_path)

    sends = []

    class Adapter:
        async def send(self, c, m, *, reply_to=None, metadata=None):
            sends.append(m)

    class Gateway:
        adapters = {"feishu": Adapter()}

        async def _handle_model_command(self, event):
            return f"gateway handled {event.text}"

    class PoolShouldNotRun:
        async def dispatch(self, *args, **kwargs):
            raise AssertionError("Hermes slash command leaked into AIAgent dispatch")

    monkeypatch.setattr(router_mod, "_get_pool", lambda: PoolShouldNotRun())

    await handle_async(event=_build_event("/model glm-5.1", user_id="ou_model"), gateway=Gateway())

    assert sends == ["gateway handled /model glm-5.1"]
    assert "Hermes gateway command handled: model" in caplog.text
    clear_spike_routes()


@pytest.mark.asyncio
async def test_gateway_command_gets_profile_scoped_session_key(tmp_path):
    """Hermes command handlers must see a profile-aware session key."""
    from hermes_multitenancy.router import handle_async, _user_inflight_tasks
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    _user_inflight_tasks.clear()
    profile_home = tmp_path / "coder"
    profile_home.mkdir()
    add_spike_route("ou_profile_cmd", profile_home)

    sends = []

    class Adapter:
        async def send(self, c, m, *, reply_to=None, metadata=None):
            sends.append(m)

    class Gateway:
        adapters = {"feishu": Adapter()}

        def _session_key_for_source(self, source):
            return "native-session-key"

        async def _handle_model_command(self, event):
            return self._session_key_for_source(event.source)

    await handle_async(
        event=_build_event("/model glm-5.1", user_id="ou_profile_cmd"),
        gateway=Gateway(),
    )

    assert sends == ["multitenancy:feishu:coder:chat-cmd:ou_profile_cmd"]
    clear_spike_routes()


@pytest.mark.asyncio
async def test_unknown_slash_replies_without_agent_dispatch(monkeypatch, caplog):
    from hermes_multitenancy.router import handle_async, _user_inflight_tasks
    from hermes_multitenancy import router as router_mod

    caplog.set_level("INFO")
    _user_inflight_tasks.clear()
    sends = []

    class Adapter:
        async def send(self, c, m, *, reply_to=None, metadata=None):
            sends.append(m)

    class PoolShouldNotRun:
        async def dispatch(self, *args, **kwargs):
            raise AssertionError("unknown slash command leaked into AIAgent dispatch")

    monkeypatch.setattr(router_mod, "_get_pool", lambda: PoolShouldNotRun())

    gateway = SimpleNamespace(adapters={"feishu": Adapter()})
    await handle_async(event=_build_event("/definitely_unknown", user_id="ou_unknown_cmd"), gateway=gateway)

    assert sends == [
        "Unknown command `/definitely_unknown`. Type /commands to see what's available, "
        "or resend without the leading slash to send as a regular message."
    ]
    assert "Unknown command `/definitely_unknown`" in caplog.text


@pytest.mark.asyncio
async def test_skill_slash_command_rewrites_into_agent_prompt(monkeypatch, tmp_path, caplog):
    """Hermes skill slash commands are agent turns, not unknown gateway commands."""
    import sys
    from types import ModuleType

    from hermes_multitenancy.router import handle_async, _user_inflight_tasks
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy import router as router_mod

    caplog.set_level("INFO")
    clear_spike_routes()
    _user_inflight_tasks.clear()
    profile_home = tmp_path / "coder"
    profile_home.mkdir()
    add_spike_route("ou_skill", profile_home)

    fake_skill_commands = SimpleNamespace(
        get_skill_commands=lambda: {"/demo-skill": {"name": "demo-skill"}},
        resolve_skill_command_key=lambda command: "/demo-skill"
        if command.replace("_", "-") == "demo-skill"
        else None,
        build_skill_invocation_message=lambda cmd_key, user_instruction, task_id=None: (
            f"[skill:{cmd_key} task:{task_id}] {user_instruction}"
        ),
    )
    fake_skill_utils = SimpleNamespace(get_disabled_skill_names=lambda platform=None: set())
    monkeypatch.setitem(sys.modules, "agent", ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.skill_commands", fake_skill_commands)
    monkeypatch.setitem(sys.modules, "agent.skill_utils", fake_skill_utils)

    seen = {}

    class Pool:
        async def dispatch(self, profile_name, home, event):
            seen["profile_name"] = profile_name
            seen["home"] = home
            seen["text"] = event.text
            return "agent ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: Pool())

    sends = []

    class Adapter:
        async def send_typing(self, c): pass
        async def send(self, c, m, *, reply_to=None, metadata=None):
            sends.append(m)

    gateway = SimpleNamespace(adapters={"feishu": Adapter()})
    await handle_async(event=_build_event("/demo-skill write docs", user_id="ou_skill"), gateway=gateway)

    assert seen == {
        "profile_name": "coder",
        "home": profile_home,
        "text": "[skill:/demo-skill task:multitenancy:feishu:coder:chat-cmd:ou_skill] write docs",
    }
    assert sends == ["agent ok"]
    assert "Hermes skill slash invocation" in caplog.text
    clear_spike_routes()


@pytest.mark.asyncio
async def test_plugin_slash_command_uses_hermes_plugin_handler(monkeypatch, tmp_path, caplog):
    """Plugin-registered slash commands should be delegated before unknown handling."""
    import sys
    from types import ModuleType

    from hermes_multitenancy.router import handle_async, _user_inflight_tasks
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy import router as router_mod

    caplog.set_level("INFO")
    clear_spike_routes()
    _user_inflight_tasks.clear()
    add_spike_route("ou_plugin", tmp_path)

    called = {}

    async def plugin_handler(args):
        called["args"] = args
        return f"plugin handled {args}"

    fake_plugins = SimpleNamespace(
        get_plugin_command_handler=lambda name: plugin_handler if name == "demo-plugin" else None
    )
    monkeypatch.setitem(sys.modules, "hermes_cli", ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", fake_plugins)

    class PoolShouldNotRun:
        async def dispatch(self, *args, **kwargs):
            raise AssertionError("plugin slash command leaked into AIAgent dispatch")

    monkeypatch.setattr(router_mod, "_get_pool", lambda: PoolShouldNotRun())

    sends = []

    class Adapter:
        async def send(self, c, m, *, reply_to=None, metadata=None):
            sends.append(m)

    gateway = SimpleNamespace(adapters={"feishu": Adapter()})
    await handle_async(event=_build_event("/demo_plugin hi there", user_id="ou_plugin"), gateway=gateway)

    assert called == {"args": "hi there"}
    assert sends == ["plugin handled hi there"]
    assert "Hermes plugin slash handler" in caplog.text
    clear_spike_routes()


@pytest.mark.asyncio
async def test_quick_command_alias_reuses_gateway_handler(monkeypatch, tmp_path, caplog):
    """Gateway quick-command aliases should expand before fallback unknown handling."""
    from hermes_multitenancy.router import handle_async, _user_inflight_tasks
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy import router as router_mod

    caplog.set_level("INFO")
    clear_spike_routes()
    _user_inflight_tasks.clear()
    add_spike_route("ou_quick_alias", tmp_path)

    class PoolShouldNotRun:
        async def dispatch(self, *args, **kwargs):
            raise AssertionError("quick-command alias leaked into AIAgent dispatch")

    monkeypatch.setattr(router_mod, "_get_pool", lambda: PoolShouldNotRun())

    sends = []

    class Adapter:
        async def send(self, c, m, *, reply_to=None, metadata=None):
            sends.append(m)

    class Gateway:
        adapters = {"feishu": Adapter()}
        config = {"quick_commands": {"mini": {"type": "alias", "target": "/model glm-5.1"}}}

        async def _handle_model_command(self, event):
            return f"model handler saw: {event.text}"

    await handle_async(event=_build_event("/mini high", user_id="ou_quick_alias"), gateway=Gateway())

    assert sends == ["model handler saw: /model glm-5.1 high"]
    assert "Hermes quick command alias" in caplog.text
    clear_spike_routes()


@pytest.mark.asyncio
async def test_quick_command_exec_runs_without_agent_dispatch(monkeypatch, tmp_path, caplog):
    """Gateway quick-command exec entries are control-plane commands."""
    from hermes_multitenancy.router import handle_async, _user_inflight_tasks
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy import router as router_mod

    caplog.set_level("INFO")
    clear_spike_routes()
    _user_inflight_tasks.clear()
    add_spike_route("ou_quick_exec", tmp_path)

    class PoolShouldNotRun:
        async def dispatch(self, *args, **kwargs):
            raise AssertionError("quick-command exec leaked into AIAgent dispatch")

    monkeypatch.setattr(router_mod, "_get_pool", lambda: PoolShouldNotRun())

    sends = []

    class Adapter:
        async def send(self, c, m, *, reply_to=None, metadata=None):
            sends.append(m)

    gateway = SimpleNamespace(
        adapters={"feishu": Adapter()},
        config={"quick_commands": {"ping": {"type": "exec", "command": "printf quick-ok"}}},
    )
    await handle_async(event=_build_event("/ping", user_id="ou_quick_exec"), gateway=gateway)

    assert sends == ["quick-ok"]
    assert "Hermes quick command exec" in caplog.text
    clear_spike_routes()


# -- replace policy --------------------------------------------------------

@pytest.mark.asyncio
async def test_second_message_cancels_first(monkeypatch):
    """A new dispatch from the same user should replace the prior one."""
    from hermes_multitenancy.router import handle_async, _user_inflight_tasks
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy import runtime as runtime_mod

    clear_spike_routes()
    _user_inflight_tasks.clear()

    cancelled = asyncio.Event()
    second_done = asyncio.Event()
    call_count = [0]

    async def runner(event, home):
        call_count[0] += 1
        if call_count[0] == 1:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "never"
        else:
            await asyncio.sleep(0.01)
            second_done.set()
            return "second-ok"

    monkeypatch.setattr(runtime_mod, "_default_run_agent", runner)

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        add_spike_route("ou_replace", home)

        class Adapter:
            async def send_typing(self, c): pass
            async def send(self, c, m, *, reply_to=None, metadata=None): pass

        gateway = SimpleNamespace(adapters={"feishu": Adapter()})

        first = asyncio.create_task(
            handle_async(event=_build_event("first", user_id="ou_replace"), gateway=gateway)
        )
        for _ in range(50):
            await asyncio.sleep(0.005)
            if call_count[0] == 1:
                break

        # Second message comes in — should cancel first
        second = asyncio.create_task(
            handle_async(event=_build_event("second", user_id="ou_replace"), gateway=gateway)
        )
        await asyncio.wait_for(cancelled.wait(), timeout=1.0)
        await asyncio.wait_for(second_done.wait(), timeout=1.0)
        with pytest.raises(asyncio.CancelledError):
            await first
        await second

    clear_spike_routes()
