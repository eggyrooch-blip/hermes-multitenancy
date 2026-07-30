"""A non-zero subprocess exit AFTER the ``done`` event must not become a red error.

Production (qiaojunlong, webui session ms747y7rm8nlmu, 2026-07-30 17:00): the
user switched model families mid-session, the core rejected the replayed
encrypted reasoning blob, self-healed (``disabled replay and stripped 1
item(s) ... retrying...``) and finished the turn — then the child process was
SIGTERMed (-15) during teardown. ``_stream_aiagent_subprocess`` raised on the
non-zero exit code, which ``stream_run_agent`` turned into a user-visible
``Error: AIAgent subprocess exited -15: ...`` bubble while throwing away the
answer the turn had already produced.

The ``done`` event is the child's terminal outcome report; anything after it is
teardown noise. Errors before ``done`` (and ``done`` events that carry an
error) must still surface.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest


_REPLAY_SELF_HEAL_STDERR = (
    "⚠️  Encrypted reasoning replay was rejected by the provider — "
    "disabled replay and stripped 1 item(s) from 1 message(s), retrying..."
)


def _event() -> SimpleNamespace:
    return SimpleNamespace(
        text="hello",
        message_id="om_test",
        source=SimpleNamespace(
            platform=SimpleNamespace(value="webui"),
            chat_id="oc_test",
            chat_name="chat",
            chat_type="dm",
            user_id="ou_test",
            user_name="tester",
            user_id_alt="on_test",
            message_id="om_source",
        ),
    )


def _make_profile_home(tmp_path: Path) -> Path:
    profile_home = tmp_path / "profiles" / "owner"
    profile_home.mkdir(parents=True)
    with sqlite3.connect(profile_home / "state.db") as conn:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT NOT NULL, "
            "role TEXT NOT NULL, "
            "content TEXT, "
            "reasoning TEXT, "
            "tool_name TEXT, "
            "tool_calls TEXT, "
            "timestamp REAL NOT NULL)"
        )
    return profile_home


def _install_fake_proc(monkeypatch, *, lines: list[bytes], stderr: bytes, returncode: int):
    class FakeStdin:
        def write(self, _payload):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    class FakeStdout:
        def __init__(self):
            self.lines = list(lines)

        async def readline(self):
            if self.lines:
                return self.lines.pop(0)
            return b""

    class FakeStderr:
        async def read(self):
            return stderr

    class FakeProc:
        def __init__(self):
            self.stdin = FakeStdin()
            self.stdout = FakeStdout()
            self.stderr = FakeStderr()
            self.pid = 4242
            self.returncode = None

        async def wait(self):
            self.returncode = returncode
            return returncode

        def kill(self):
            self.returncode = -9

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)


def test_transient_exit_after_done_emits_no_error(monkeypatch, tmp_path, caplog):
    """Self-heal + completed turn + SIGTERM on teardown → zero error events."""
    from hermes_multitenancy import agent_real

    profile_home = _make_profile_home(tmp_path)
    _install_fake_proc(
        monkeypatch,
        lines=[
            json.dumps({"event": "tool_started", "name": "terminal", "args": {}}).encode() + b"\n",
            json.dumps({"event": "content", "text": "答案"}).encode() + b"\n",
            b'{"event": "done", "result": "\\u7b54\\u6848", "error": null}\n',
        ],
        stderr=_REPLAY_SELF_HEAL_STDERR.encode("utf-8"),
        returncode=-15,
    )

    async def collect():
        return [
            item
            async for item in agent_real.stream_run_agent(_event(), profile_home)
        ]

    with caplog.at_level("WARNING"):
        events = asyncio.run(collect())

    assert ("content", "答案") in events
    assert [kind for kind, _ in events] == ["tool_started", "content"]
    # The self-heal detail stays in the logs — triage must not degrade.
    assert any(
        "AFTER delivering its done event" in record.getMessage()
        and "Encrypted reasoning replay" in record.getMessage()
        for record in caplog.records
    )


def test_nonzero_exit_without_done_still_raises(monkeypatch, tmp_path):
    """Killed before the turn produced a result → red error, unchanged."""
    from hermes_multitenancy import agent_real

    profile_home = _make_profile_home(tmp_path)
    _install_fake_proc(
        monkeypatch,
        lines=[json.dumps({"event": "content", "text": "half"}).encode() + b"\n"],
        stderr=b"Traceback (most recent call last):\nBoom",
        returncode=-15,
    )

    async def drain():
        async for _ in agent_real._stream_aiagent_subprocess(_event(), profile_home):
            pass

    with pytest.raises(RuntimeError, match="AIAgent subprocess exited -15"):
        asyncio.run(drain())


def test_selfheal_then_failure_still_raises(monkeypatch, tmp_path):
    """Retry after the strip failed too → the done-carried error still surfaces."""
    from hermes_multitenancy import agent_real

    profile_home = _make_profile_home(tmp_path)
    _install_fake_proc(
        monkeypatch,
        lines=[
            json.dumps({"event": "done", "result": "", "error": "AIAgent turn failed: HTTP 400"}).encode() + b"\n",
        ],
        stderr=_REPLAY_SELF_HEAL_STDERR.encode("utf-8"),
        returncode=-15,
    )

    async def drain():
        async for _ in agent_real._stream_aiagent_subprocess(_event(), profile_home):
            pass

    with pytest.raises(RuntimeError, match="HTTP 400"):
        asyncio.run(drain())
