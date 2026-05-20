"""US-009 — Slash command parsing + /stop /status /new dispatch."""
from __future__ import annotations

import asyncio
import os
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
            user_id_alt=None,
            user_name="cmd-user",
            chat_type="dm",
            thread_id=None,
            platform=SimpleNamespace(value="feishu"),
        ),
    )


# -- parse_command ----------------------------------------------------------

def test_parse_known_commands():
    from hermes_multitenancy.commands import parse_command
    assert parse_command("/stop") == ("stop", "")
    assert parse_command("/status") == ("status", "")
    assert parse_command("/new") == ("new", "")
    assert parse_command("/feishu_auth") == ("feishu-auth", "")
    assert parse_command("/feishu_reauth wiki:wiki") == ("feishu-auth", "wiki:wiki")
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


def test_parse_unescapes_feishu_markdown_command_name():
    from hermes_multitenancy.commands import parse_command, unknown_command_message

    assert parse_command("/keep\\-record") == ("keep-record", "")
    assert parse_command("/reload\\_mcp") == ("reload-mcp", "")
    assert "keep-record" in unknown_command_message("keep\\-record")
    assert "keep\\-record" not in unknown_command_message("keep\\-record")


def test_parse_rejects_paths():
    from hermes_multitenancy.commands import parse_command
    assert parse_command("/some/path") is None


def test_resolve_sender_strips_feishu_sender_type_prefix():
    from hermes_multitenancy.router import _resolve_sender_for_routing

    event = _build_event("/feishu_auth", user_id="user:ou_prefixed")

    assert _resolve_sender_for_routing(event) == "ou_prefixed"


def test_lark_cli_history_sanitizer_removes_stale_bot_identity_note():
    from hermes_multitenancy.router import _clean_stream_display_text, _strip_wrong_lark_cli_bot_identity_note

    cleaned = _strip_wrong_lark_cli_bot_identity_note(
        "测试标记：BASE\n\n"
        "多维表格创建成功。\n"
        "该表格由 bot 身份创建，默认包含一张空数据表。\n"
        "链接：https://example.feishu.cn/base/base_token"
    )

    assert "bot 身份创建" not in cleaned
    assert "测试标记：BASE" in cleaned
    assert "https://example.feishu.cn/base/base_token" in cleaned

    display = _clean_stream_display_text(
        "测试标记：CALENDAR\n\n"
        "命令执行成功，但今天日程数量为 0。\n"
        "当前 bot 身份下的 primary 日历今日无日程。",
        Path("/Users/kite/.hermes/profiles/feishu_g41a5b5g"),
    )
    assert "bot 身份" not in display
    assert "今天日程数量为 0" in display


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


def test_skill_slash_uses_routed_profile_home(monkeypatch, tmp_path):
    """Router profile may not have tenant default skills; target profile does."""
    import sys
    from types import ModuleType

    from hermes_multitenancy.router import handle_async, _user_inflight_tasks
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy import router as router_mod

    clear_spike_routes()
    _user_inflight_tasks.clear()
    router_home = tmp_path / "multitenancy_router"
    profile_home = tmp_path / "owner"
    router_home.mkdir()
    profile_home.mkdir()
    add_spike_route("ou_hades_profile", profile_home)
    monkeypatch.setenv("HERMES_HOME", str(router_home))

    def resolve_skill_command_key(command):
        if os.environ.get("HERMES_HOME") != str(profile_home):
            return None
        return "/kep-hades-cli" if command.replace("_", "-") == "kep-hades-cli" else None

    fake_skill_commands = SimpleNamespace(
        get_skill_commands=lambda: {"/kep-hades-cli": {"name": "kep-hades-cli"}}
        if os.environ.get("HERMES_HOME") == str(profile_home)
        else {},
        resolve_skill_command_key=resolve_skill_command_key,
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
    asyncio.run(handle_async(
        event=_build_event("/hades get 69df030c1f01cb45ba7ff585", user_id="ou_hades_profile"),
        gateway=gateway,
    ))

    assert seen == {
        "profile_name": "owner",
        "home": profile_home,
        "text": (
            "[skill:/kep-hades-cli task:multitenancy:feishu:owner:chat-cmd:ou_hades_profile] "
            "get 69df030c1f01cb45ba7ff585"
        ),
    }
    assert sends == ["agent ok"]
    assert os.environ.get("HERMES_HOME") == str(router_home)
    clear_spike_routes()


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


@pytest.mark.asyncio
async def test_feishu_auth_command_starts_multitenancy_uat_session(monkeypatch, tmp_path):
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.router import handle_async, _user_inflight_tasks
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy import router as router_mod

    clear_spike_routes()


@pytest.mark.asyncio
async def test_feishu_auth_command_uses_profile_open_id_for_short_sender(monkeypatch, tmp_path):
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.router import handle_async, _user_inflight_tasks
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy import router as router_mod

    clear_spike_routes()
    _user_inflight_tasks.clear()
    profile_home = tmp_path / "feishu_g41a5b5g"
    profile_home.mkdir()
    add_spike_route("g41a5b5g", profile_home)

    started = {}

    monkeypatch.setattr(router_mod, "_profile_open_id_for_auth", lambda profile_name: "ou_from_profile")
    monkeypatch.setattr(
        feishu_uat_auth,
        "start_session",
        lambda **kwargs: started.update(kwargs) or {
            "session_id": "sess_auth",
            "verification_uri": "https://accounts.feishu.cn/device",
            "user_code": "ABCD",
            "interval": 3,
        },
    )
    monkeypatch.setattr(router_mod, "_start_feishu_auth_poll_task", lambda **_kwargs: None)

    sends = []

    class Adapter:
        async def send_typing(self, c): pass
        async def send(self, c, m, *, reply_to=None, metadata=None):
            sends.append(m)

    gateway = SimpleNamespace(adapters={"feishu": Adapter()})
    await handle_async(event=_build_event("/feishu_auth", user_id="g41a5b5g"), gateway=gateway)

    assert started["profile_name"] == profile_home.name
    assert started["open_id"] == "ou_from_profile"
    assert any("accounts.feishu.cn" in message for message in sends)

    clear_spike_routes()
    _user_inflight_tasks.clear()
    profile_home = tmp_path / "owner"
    profile_home.mkdir()
    add_spike_route("ou_auth", profile_home)

    started = {}
    poll_tasks = []

    def fake_start_session(**kwargs):
        started.update(kwargs)
        return {
            "session_id": "sess_auth",
            "status": "pending",
            "verification_uri": "https://accounts.feishu.cn/device?user_code=ABCD",
            "user_code": "ABCD",
            "expires_at": 9999999999,
            "interval": 3,
        }

    monkeypatch.setattr(feishu_uat_auth, "start_session", fake_start_session)
    monkeypatch.setattr(router_mod, "_start_feishu_auth_poll_task", lambda **kwargs: poll_tasks.append(kwargs))

    sends = []

    class Adapter:
        async def send_typing(self, c): pass
        async def send(self, c, m, *, reply_to=None, metadata=None):
            sends.append((c, m))

    gateway = SimpleNamespace(adapters={"feishu": Adapter()})
    await handle_async(event=_build_event("/feishu_auth wiki:wiki", user_id="ou_auth"), gateway=gateway)

    assert started["profile_name"] == profile_home.name
    assert started["open_id"] == "ou_auth"
    assert started["scope"] == "wiki:wiki"
    assert any("https://accounts.feishu.cn/device" in message and "ABCD" in message for _chat, message in sends)
    assert poll_tasks and poll_tasks[0]["session_id"] == "sess_auth"

    clear_spike_routes()


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

    # Case 2: routed user with no prior history still gets a formal ack.
    sends.clear()
    clear_spike_routes()
    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path
        home = Path(tmp)
        add_spike_route("ou_emptyreset", home)

        await handle_async(event=_build_event("/new", user_id="ou_emptyreset"), gateway=gateway)
        assert sends[-1] == "会话已重置 ✅"

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

    event = _build_event("/model glm-5.1", user_id="ou_profile_cmd")
    event.source.user_id_alt = "shared_alt"
    await handle_async(event=event, gateway=Gateway())

    assert sends == ["multitenancy:feishu:coder:chat-cmd:ou_profile_cmd"]
    clear_spike_routes()


@pytest.mark.asyncio
async def test_concurrent_gateway_commands_keep_profile_context(tmp_path):
    """Concurrent slash handlers must not observe another profile's HERMES_HOME/session key."""
    from hermes_multitenancy.router import handle_async, _user_inflight_tasks
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    _user_inflight_tasks.clear()
    profile_a = tmp_path / "alice"
    profile_b = tmp_path / "bob"
    profile_a.mkdir()
    profile_b.mkdir()
    add_spike_route("ou_cmd_a", profile_a)
    add_spike_route("ou_cmd_b", profile_b)

    sends = []
    observations = []

    class Adapter:
        async def send(self, c, m, *, reply_to=None, metadata=None):
            sends.append((c, m))

    class Gateway:
        adapters = {"feishu": Adapter()}

        def _session_key_for_source(self, source):
            return "native-session-key"

        async def _handle_model_command(self, event):
            source = event.source
            observations.append(
                (
                    source.user_id,
                    "start",
                    os.environ.get("HERMES_HOME"),
                    self._session_key_for_source(source),
                )
            )
            await asyncio.sleep(0.01)
            observations.append(
                (
                    source.user_id,
                    "end",
                    os.environ.get("HERMES_HOME"),
                    self._session_key_for_source(source),
                )
            )
            return os.environ.get("HERMES_HOME")

    gateway = Gateway()
    await asyncio.gather(
        handle_async(event=_build_event("/model glm-5.1", user_id="ou_cmd_a", chat_id="chat-a"), gateway=gateway),
        handle_async(event=_build_event("/model glm-5.1", user_id="ou_cmd_b", chat_id="chat-b"), gateway=gateway),
    )

    expected_home = {"ou_cmd_a": str(profile_a), "ou_cmd_b": str(profile_b)}
    expected_key = {
        "ou_cmd_a": "multitenancy:feishu:alice:chat-a:ou_cmd_a",
        "ou_cmd_b": "multitenancy:feishu:bob:chat-b:ou_cmd_b",
    }
    assert sorted(message for _chat, message in sends) == sorted(expected_home.values())
    assert observations
    for user_id, _when, home, session_key in observations:
        assert home == expected_home[user_id]
        assert session_key == expected_key[user_id]

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
async def test_hades_slash_alias_invokes_kep_hades_skill(monkeypatch, tmp_path):
    """Keep's historical /hades shorthand should load kep-hades-cli."""
    import sys
    from types import ModuleType

    from hermes_multitenancy.router import handle_async, _user_inflight_tasks
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy import router as router_mod

    clear_spike_routes()
    _user_inflight_tasks.clear()
    profile_home = tmp_path / "coder"
    profile_home.mkdir()
    add_spike_route("ou_hades", profile_home)

    fake_skill_commands = SimpleNamespace(
        get_skill_commands=lambda: {"/kep-hades-cli": {"name": "kep-hades-cli"}},
        resolve_skill_command_key=lambda command: "/kep-hades-cli"
        if command.replace("_", "-") == "kep-hades-cli"
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
            seen["text"] = event.text
            return "agent ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: Pool())

    sends = []

    class Adapter:
        async def send_typing(self, c): pass
        async def send(self, c, m, *, reply_to=None, metadata=None):
            sends.append(m)

    gateway = SimpleNamespace(adapters={"feishu": Adapter()})
    await handle_async(
        event=_build_event("/hades get 69df030c1f01cb45ba7ff585", user_id="ou_hades"),
        gateway=gateway,
    )

    assert seen["text"] == (
        "[skill:/kep-hades-cli task:multitenancy:feishu:coder:chat-cmd:ou_hades] "
        "get 69df030c1f01cb45ba7ff585"
    )
    assert sends == ["agent ok"]
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
        called["hermes_home"] = os.environ.get("HERMES_HOME")
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

    assert called["args"] == "hi there"
    assert called["hermes_home"] == str(tmp_path)
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
async def test_quick_command_exec_disabled_by_default(monkeypatch, tmp_path):
    """Shell quick commands must be opt-in for multitenant Feishu."""
    from hermes_multitenancy.router import handle_async, _user_inflight_tasks
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy import router as router_mod

    clear_spike_routes()
    _user_inflight_tasks.clear()
    add_spike_route("ou_quick_exec_denied", tmp_path)

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
    await handle_async(event=_build_event("/ping", user_id="ou_quick_exec_denied"), gateway=gateway)

    assert sends == [
        "Quick command '/ping' exec is disabled for Feishu multitenancy. "
        "Enable only after profile sandboxing is in place."
    ]
    clear_spike_routes()


@pytest.mark.asyncio
async def test_quick_command_exec_runs_in_profile_env_when_explicitly_enabled(monkeypatch, tmp_path, caplog):
    """Allowed exec commands inherit the routed profile HERMES_HOME."""
    from hermes_multitenancy.router import handle_async, _user_inflight_tasks
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy import router as router_mod

    caplog.set_level("INFO")
    clear_spike_routes()
    _user_inflight_tasks.clear()
    profile_home = tmp_path / "quick-exec-profile"
    profile_home.mkdir()
    add_spike_route("ou_quick_exec", profile_home)

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
        config={
            "multitenancy": {"allow_quick_exec": True},
            "quick_commands": {"ping": {"type": "exec", "command": "printf \"$HERMES_HOME\""}},
        },
    )
    await handle_async(event=_build_event("/ping", user_id="ou_quick_exec"), gateway=gateway)

    assert sends == [str(profile_home)]
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


def test_session_guard_transfers_to_replacement_dispatch(monkeypatch):
    """A cancelled prior dispatch must not remove the newer flush guard."""
    import sys
    from hermes_multitenancy.router import _register_session_guard_for_dispatch

    fake_session = SimpleNamespace(
        build_session_key=lambda source, **_kwargs: f"{source.chat_id}:{source.user_id}",
    )
    monkeypatch.setitem(sys.modules, "gateway.session", fake_session)

    class FakeTask:
        def __init__(self):
            self.callbacks = []

        def add_done_callback(self, callback):
            self.callbacks.append(callback)

        def complete(self):
            for callback in list(self.callbacks):
                callback(self)

    adapter = SimpleNamespace(
        _active_sessions={},
        config=SimpleNamespace(extra={}),
    )
    gateway = SimpleNamespace(adapters={"feishu": adapter})
    event = _build_event("first", user_id="ou_replace_guard", chat_id="chat-guard")

    first = FakeTask()
    second = FakeTask()
    _register_session_guard_for_dispatch(event, gateway, first)
    session_key = next(iter(adapter._active_sessions))
    first_guard = adapter._active_sessions[session_key]

    _register_session_guard_for_dispatch(
        _build_event("second", user_id="ou_replace_guard", chat_id="chat-guard"),
        gateway,
        second,
    )
    second_guard = adapter._active_sessions[session_key]

    assert second_guard is not first_guard

    first.complete()
    assert adapter._active_sessions.get(session_key) is second_guard

    second.complete()
    assert session_key not in adapter._active_sessions
