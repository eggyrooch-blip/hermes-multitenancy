"""A non-zero subprocess exit AFTER the ``done`` event must not become a red error.

Production (wangwu, webui session ms747y7rm8nlmu, 2026-07-30 17:00): the
user switched model families mid-session, the core rejected the replayed
encrypted reasoning blob, self-healed (``disabled replay and stripped 1
item(s) ... retrying...``) and finished the turn — then the child process was
SIGTERMed (-15) during teardown. ``_stream_aiagent_subprocess`` raised on the
non-zero exit code, which ``stream_run_agent`` turned into a user-visible
``Error: AIAgent subprocess exited -15: ...`` bubble while throwing away the
answer the turn had already produced.

Two separate defects at the same raise site, fixed together:

1. ``done`` is the child's terminal outcome report — anything after it is
   teardown noise, so a non-zero exit *after* done must not destroy the
   completed turn. Errors before ``done`` (and ``done`` events carrying an
   error) must still surface.
2. When the child is killed by a signal (returncode < 0) the stderr tail is
   stale output from seconds earlier, not the cause of death. Quoting it in the
   user-facing error is a lie — the 2026-07-30 incident sent everyone chasing a
   phantom "encrypted reasoning" bug. Real crashes (returncode > 0) keep the
   tail, because there it genuinely is the cause.
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


def test_true_crash_without_done_keeps_stderr_tail(monkeypatch, tmp_path):
    """returncode > 0 is a real crash — the stderr tail IS the cause. Unchanged."""
    from hermes_multitenancy import agent_real

    profile_home = _make_profile_home(tmp_path)
    _install_fake_proc(
        monkeypatch,
        lines=[json.dumps({"event": "content", "text": "half"}).encode() + b"\n"],
        stderr=b"Traceback (most recent call last):\nBoom",
        returncode=1,
    )

    async def drain():
        async for _ in agent_real._stream_aiagent_subprocess(_event(), profile_home):
            pass

    with pytest.raises(RuntimeError, match="AIAgent subprocess exited 1") as excinfo:
        asyncio.run(drain())
    assert "Boom" in str(excinfo.value)


def test_signal_death_blames_the_restart_not_the_stale_stderr(monkeypatch, tmp_path, caplog):
    """returncode < 0 = gateway restart killed the run. Say so; never quote stderr.

    Regression for 2026-07-30: the message spliced in a 15s-old core warning
    ("Encrypted reasoning replay was rejected...") that had nothing to do with
    the SIGTERM, and sent the whole investigation after a phantom bug.
    """
    from hermes_multitenancy import agent_real

    profile_home = _make_profile_home(tmp_path)
    _install_fake_proc(
        monkeypatch,
        lines=[json.dumps({"event": "thinking", "text": "..."}).encode() + b"\n"],
        stderr=_REPLAY_SELF_HEAL_STDERR.encode("utf-8"),
        returncode=-15,
    )

    async def drain():
        async for _ in agent_real._stream_aiagent_subprocess(_event(), profile_home):
            pass

    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError) as excinfo:
            asyncio.run(drain())

    message = str(excinfo.value)
    assert "网关重启" in message and "信号 15" in message
    # The lie we are fixing: no stale stderr, no raw exit-code jargon.
    assert "Encrypted reasoning replay" not in message
    assert "AIAgent subprocess exited" not in message
    # Triage must not degrade — full stderr still reaches the log.
    assert any(
        "killed by signal 15" in record.getMessage()
        and "Encrypted reasoning replay" in record.getMessage()
        for record in caplog.records
    )


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


# ── MT-001: the honest message must actually reach the user ──────────────────
# stream_run_agent's except ladder has three exits below the tag check, two of
# which used to destroy the message: content_parts → _PARTIAL_FAILURE_NOTICE
# replaces it, and the zero-content/zero-tool path falls through to the legacy
# stream, which answers as if nothing happened.


def test_signal_death_message_survives_to_the_user_no_legacy_fallback(monkeypatch, tmp_path):
    """Zero content, zero tools → the old code fell through to _stream_loop."""
    from hermes_multitenancy import agent_real

    profile_home = _make_profile_home(tmp_path)
    _install_fake_proc(
        monkeypatch,
        lines=[json.dumps({"event": "thinking", "text": "..."}).encode() + b"\n"],
        stderr=_REPLAY_SELF_HEAL_STDERR.encode("utf-8"),
        returncode=-15,
    )

    async def legacy_must_not_run(event, profile_home, messages=None):
        raise AssertionError("legacy stream must not answer an interrupted turn")
        yield  # pragma: no cover

    monkeypatch.setattr(agent_real, "_stream_loop", legacy_must_not_run)

    async def collect():
        return [item async for item in agent_real.stream_run_agent(_event(), profile_home)]

    with pytest.raises(agent_real.GatewayRestartInterruptedError) as excinfo:
        asyncio.run(collect())
    assert "网关重启" in str(excinfo.value)
    assert excinfo.value.error_code == "GATEWAY_RESTART_INTERRUPTED"


def test_signal_death_appends_reason_after_partial_content(monkeypatch, tmp_path):
    """Partial output stays, reason is APPENDED — not replaced by the generic notice."""
    from hermes_multitenancy import agent_real

    profile_home = _make_profile_home(tmp_path)
    _install_fake_proc(
        monkeypatch,
        lines=[json.dumps({"event": "content", "text": "前半段答案"}).encode() + b"\n"],
        stderr=b"stale",
        returncode=-15,
    )

    async def legacy_must_not_run(event, profile_home, messages=None):
        raise AssertionError("legacy stream must not answer an interrupted turn")
        yield  # pragma: no cover

    monkeypatch.setattr(agent_real, "_stream_loop", legacy_must_not_run)

    async def collect():
        return [item async for item in agent_real.stream_run_agent(_event(), profile_home)]

    events = asyncio.run(collect())
    streamed = "".join(payload for kind, payload in events if kind == "content")
    assert "前半段答案" in streamed          # what the user already saw survives
    assert "网关重启" in streamed            # ...with the real reason appended
    assert agent_real._PARTIAL_FAILURE_NOTICE not in streamed


# ── MT-002 (partial): post-done log level carries the sign's meaning ──────────


def _run_post_done_exit(monkeypatch, tmp_path, returncode: int):
    from hermes_multitenancy import agent_real

    profile_home = _make_profile_home(tmp_path)
    _install_fake_proc(
        monkeypatch,
        lines=[b'{"event": "done", "result": "ok", "error": null}\n'],
        stderr=b"teardown noise",
        returncode=returncode,
    )

    async def collect():
        return [
            item
            async for item in agent_real._stream_aiagent_subprocess(_event(), profile_home)
        ]

    return asyncio.run(collect())


def test_post_done_signal_exit_logs_warning(monkeypatch, tmp_path, caplog):
    with caplog.at_level("WARNING"):
        _run_post_done_exit(monkeypatch, tmp_path, -15)
    post_done = [r for r in caplog.records if "AFTER delivering its done event" in r.getMessage()]
    assert post_done and all(r.levelname == "WARNING" for r in post_done)


def test_post_done_crash_exit_logs_error(monkeypatch, tmp_path, caplog):
    """A non-zero CODE after a successful done = the child crashed in teardown."""
    with caplog.at_level("WARNING"):
        _run_post_done_exit(monkeypatch, tmp_path, 3)
    post_done = [r for r in caplog.records if "AFTER delivering its done event" in r.getMessage()]
    assert post_done and all(r.levelname == "ERROR" for r in post_done)


# ── MT-004: "full stderr" must mean full, not a 4000-char tail ────────────────


def test_signal_death_logs_stderr_head_and_tail(monkeypatch, tmp_path, caplog):
    from hermes_multitenancy import agent_real

    profile_home = _make_profile_home(tmp_path)
    huge = "HEAD-MARKER" + ("x" * 12000) + "TAIL-MARKER"
    _install_fake_proc(
        monkeypatch,
        lines=[json.dumps({"event": "thinking", "text": "..."}).encode() + b"\n"],
        stderr=huge.encode("utf-8"),
        returncode=-15,
    )

    async def drain():
        async for _ in agent_real._stream_aiagent_subprocess(_event(), profile_home):
            pass

    with caplog.at_level("WARNING"):
        with pytest.raises(agent_real.GatewayRestartInterruptedError):
            asyncio.run(drain())

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "HEAD-MARKER" in logged and "TAIL-MARKER" in logged


# ── MT-002 rebuttal evidence (round-2) ───────────────────────────────────────
# Review holds that keeping the saw_done guard manufactures a "false success"
# (done + SIGTERM) and hides crashes (done + exit 3). Both tests below run at
# the public stream_run_agent layer the review itself named, and show the user
# receives the COMPLETE answer in both cases — a turn whose answer was fully
# delivered is a real success, not a fake one. The sign only changes log level.


def _collect_stream_run_agent_after_done_exit(monkeypatch, tmp_path, returncode: int):
    from hermes_multitenancy import agent_real

    profile_home = _make_profile_home(tmp_path)
    _install_fake_proc(
        monkeypatch,
        lines=[
            json.dumps({"event": "content", "text": "完整"}).encode() + b"\n",
            json.dumps({"event": "content", "text": "答案"}).encode() + b"\n",
            b'{"event": "done", "result": "\\u5b8c\\u6574\\u7b54\\u6848", "error": null}\n',
        ],
        stderr=b"teardown stderr HEAD" + (b"y" * 9000) + b"teardown stderr TAIL",
        returncode=returncode,
    )

    async def legacy_must_not_run(event, profile_home, messages=None):
        raise AssertionError("a completed turn must never re-answer via the legacy stream")
        yield  # pragma: no cover

    monkeypatch.setattr(agent_real, "_stream_loop", legacy_must_not_run)

    async def collect():
        return [item async for item in agent_real.stream_run_agent(_event(), profile_home)]

    return asyncio.run(collect())


def test_public_layer_signal_after_done_still_delivers_whole_answer(monkeypatch, tmp_path, caplog):
    """done + SIGTERM: the user gets the full answer → success is real, not false."""
    with caplog.at_level("WARNING"):
        events = _collect_stream_run_agent_after_done_exit(monkeypatch, tmp_path, -15)

    assert "".join(p for k, p in events if k == "content") == "完整答案"
    assert not [k for k, _ in events if k == "error"]
    post_done = [r for r in caplog.records if "AFTER delivering its done event" in r.getMessage()]
    assert post_done and all(r.levelname == "WARNING" for r in post_done)


def test_public_layer_crash_after_done_delivers_answer_and_logs_error(monkeypatch, tmp_path, caplog):
    """done + exit 3: answer still complete, but the crash is ERROR + full stderr."""
    with caplog.at_level("WARNING"):
        events = _collect_stream_run_agent_after_done_exit(monkeypatch, tmp_path, 3)

    assert "".join(p for k, p in events if k == "content") == "完整答案"
    assert not [k for k, _ in events if k == "error"]
    post_done = [r for r in caplog.records if "AFTER delivering its done event" in r.getMessage()]
    assert post_done and all(r.levelname == "ERROR" for r in post_done)
    logged = "\n".join(r.getMessage() for r in post_done)
    assert "teardown stderr HEAD" in logged and "teardown stderr TAIL" in logged


def test_core_discards_final_text_on_any_exception(monkeypatch, tmp_path):
    """Why the saw_done guard must stay — anchors the MT-002 rebuttal.

    ``final_text`` is assigned from the done event at _core.py:283 but is only
    ever handed to the consumer on the normal exit (_core.py:326). EVERY path
    out of ``except Exception`` at _core.py:328 drops it on the floor:
    _core.py:394-395 replaces it with _PARTIAL_FAILURE_NOTICE, _core.py:400
    re-raises, _core.py:421 falls through to the legacy stream.

    So raising on a post-``done`` non-zero exit — what the review asks for —
    does not "surface a hidden failure"; it deletes an answer the child already
    produced and the user was already owed. This test pins that behaviour, so
    if anyone removes the guard the consequence is visible, not theoretical.
    """
    from hermes_multitenancy import agent_real

    async def done_then_die(event, profile_home, messages=None):
        yield "done", "完整答案"                       # child delivered the answer
        raise RuntimeError("post-done teardown failure")

    async def legacy(event, profile_home, messages=None):
        yield "content", "LEGACY-REANSWER"

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", done_then_die)
    monkeypatch.setattr(agent_real, "_stream_loop", legacy)

    async def collect():
        return [item async for item in agent_real.stream_run_agent(_event(), tmp_path)]

    streamed = "".join(p for k, p in asyncio.run(collect()) if k == "content")
    assert "完整答案" not in streamed        # ← the answer is GONE
    assert streamed == "LEGACY-REANSWER"     # ← user silently re-answered instead
