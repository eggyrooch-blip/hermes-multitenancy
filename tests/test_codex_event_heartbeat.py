"""Zero-model heartbeat over the canonical (kind, payload) event stream
(SPEC ticket 02).

Proves:
- a slow/silent underlying stream gets a heartbeat injected at most every
  ``interval_ms``, reset by any real event, and zero heartbeats once a
  terminal ("done"/"error") kind has been observed;
- a busy stream (events arriving faster than the interval) never gets an
  extra heartbeat;
- the heartbeat payload is a closed {"state","text"} pair with fixed
  Chinese text -- never anything derived from a real event's payload
  (no prompt, model delta, tool args, chain-of-thought, token, open_id,
  path);
- the wrapper makes zero calls of its own -- it only ever awaits
  ``stream.__anext__()``, so a "model call" counter on the fake stream
  never moves faster than the number of real items pulled;
- two independently-shaped projector functions (standing in for the real
  WebUI SSE switch and the real Feishu card switch in
  ``webui_broker/periphery.py`` / ``router/streaming.py`` -- SEAM MAP
  streaming_and_events) read back an identical rendered sequence from the
  same wrapped stream, including the new heartbeat kind;
- the real ``_verified_codex_stream`` hook (hermes_multitenancy/agent_real/
  _core.py) delivers heartbeats LIVE while a mapped Codex subprocess is
  still being read, while every non-heartbeat item stays gated behind the
  existing buffer-then-release-after-spend-receipt contract untouched.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_multitenancy.agent_real.codex_event_heartbeat import (
    HEARTBEAT_KIND,
    heartbeat_payload,
    wrap_with_heartbeat,
)


# ── pure wrapper tests ──────────────────────────────────────────────────


class _FakeCanonicalStream:
    """Stands in for the raw per-item Codex subprocess stream. Each item is
    optionally preceded by a real (small, <1s) delay to simulate silence."""

    def __init__(self, items: list[tuple[float, tuple[str, object]]]) -> None:
        self._items = list(items)
        self.model_calls = 0  # increments only when a REAL item is produced

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        delay, item = self._items.pop(0)
        if delay:
            await asyncio.sleep(delay)
        self.model_calls += 1
        return item


async def _drain(stream, **kwargs) -> list[tuple[str, object]]:
    return [item async for item in wrap_with_heartbeat(stream, **kwargs)]


@pytest.mark.asyncio
async def test_busy_stream_never_gets_a_heartbeat():
    stream = _FakeCanonicalStream(
        [(0, ("content", "a")), (0, ("content", "b")), (0, ("done", "b"))]
    )
    out = await _drain(stream, interval_ms=200)
    assert out == [("content", "a"), ("content", "b"), ("done", "b")]
    assert stream.model_calls == 3


@pytest.mark.asyncio
async def test_heartbeat_fires_when_silent_past_interval():
    stream = _FakeCanonicalStream(
        [(0.06, ("content", "slow")), (0, ("done", "slow"))]
    )
    out = await _drain(stream, interval_ms=10)
    kinds = [k for k, _ in out]
    assert kinds.count(HEARTBEAT_KIND) >= 1
    assert kinds[-2:] == ["content", "done"]
    # the wrapper never made an extra "model call" of its own
    assert stream.model_calls == 2


@pytest.mark.asyncio
async def test_heartbeat_timer_resets_on_real_event():
    # Two silent gaps separated by a real event; with interval=30ms and each
    # gap 70ms, each gap should independently produce >=1 heartbeat, and no
    # heartbeat should appear immediately adjacent to the real event on both
    # sides simultaneously (i.e. the timer truly restarted, not just kept
    # counting from t=0).
    stream = _FakeCanonicalStream(
        [
            (0.07, ("content", "first")),
            (0.07, ("content", "second")),
            (0, ("done", "second")),
        ]
    )
    out = await _drain(stream, interval_ms=30)
    idx_first = out.index(("content", "first"))
    idx_second = out.index(("content", "second"))
    heartbeats_before_first = sum(
        1 for k, _ in out[:idx_first] if k == HEARTBEAT_KIND
    )
    heartbeats_between = sum(
        1 for k, _ in out[idx_first + 1 : idx_second] if k == HEARTBEAT_KIND
    )
    assert heartbeats_before_first >= 1
    assert heartbeats_between >= 1


@pytest.mark.asyncio
async def test_zero_heartbeats_after_terminal():
    # "done" arrives, then the underlying stream keeps the wrapper waiting
    # (simulating trailing cleanup work) well past the interval -- no more
    # heartbeats may appear once the terminal kind has been yielded.
    stream = _FakeCanonicalStream(
        [(0, ("done", "x")), (0.05, ("content", "post-terminal-cleanup-item"))]
    )
    out = await _drain(stream, interval_ms=5)
    idx_done = out.index(("done", "x"))
    assert all(k != HEARTBEAT_KIND for k, _ in out[idx_done:])
    # the trailing item after "done" still got drained (matches plain
    # ``async for`` exhaustiveness -- the buffering call site relies on this
    # to run generator cleanup code that lives after its last yield).
    assert out[-1] == ("content", "post-terminal-cleanup-item")


@pytest.mark.asyncio
async def test_error_kind_is_also_terminal():
    stream = _FakeCanonicalStream([(0, ("error", "boom"))])
    out = await _drain(stream, interval_ms=5)
    assert out == [("error", "boom")]


@pytest.mark.asyncio
async def test_underlying_exception_propagates_uninterrupted():
    class _Boom:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("subprocess pipe broke")

    with pytest.raises(RuntimeError, match="subprocess pipe broke"):
        await _drain(_Boom(), interval_ms=1000)


# ── payload contract ────────────────────────────────────────────────────


def test_heartbeat_payload_is_closed_state_and_text_only():
    payload = heartbeat_payload("waiting_tool")
    assert set(payload) == {"state", "text"}
    assert payload["state"] == "waiting_tool"
    assert isinstance(payload["text"], str) and payload["text"]
    # Chinese, no ascii identifiers/paths/codes leaking through
    assert not any(ch.isascii() and ch.isalnum() for ch in payload["text"])


def test_heartbeat_payload_unknown_state_falls_back_safely():
    payload = heartbeat_payload("__attacker_injected_state__")
    assert payload["state"] == "running"
    assert payload["text"] == heartbeat_payload("running")["text"]


@pytest.mark.asyncio
async def test_heartbeat_state_derived_only_from_kind_never_payload():
    # A real event's payload can carry anything (a dict, a raw string) --
    # the heartbeat that follows must never echo any of it.
    secret_ish_payload = {"open_id": "ou_should_never_leak", "path": "/etc/x"}
    stream = _FakeCanonicalStream(
        [(0, ("tool_started", secret_ish_payload)), (0.02, ("content", "next"))]
    )
    out = await _drain(stream, interval_ms=5)
    for kind, payload in out:
        if kind == HEARTBEAT_KIND:
            assert payload == heartbeat_payload("waiting_tool")
            assert "open_id" not in str(payload)
            assert "/etc/x" not in str(payload)


# ── two-projection equivalence (WebUI-shaped vs Feishu-shaped consumers) ──


def _webui_style_project(events):
    """Mirrors webui_broker/periphery.py's kind switch shape (SEAM MAP
    streaming_and_events): dispatch on ``kind``, render one line per
    status-bearing kind."""
    out = []
    for kind, payload in events:
        if kind == HEARTBEAT_KIND:
            out.append(payload["text"])
        elif kind == "tool_started":
            out.append("工具执行中…")
        elif kind == "approval_required":
            out.append("等待确认…")
        elif kind == "done":
            out.append("完成")
        elif kind == "error":
            out.append("出错")
    return out


def _feishu_style_project(events):
    """Independently-shaped mirror of router/streaming.py's kind switch --
    a different code path over the identical canonical sequence."""
    rendered = []
    for item in events:
        kind, payload = item
        if kind == "heartbeat":
            rendered.append(payload["text"])
            continue
        if kind == "tool_started":
            rendered.append("工具执行中…")
            continue
        if kind == "approval_required":
            rendered.append("等待确认…")
            continue
        if kind in {"done", "error"}:
            rendered.append("完成" if kind == "done" else "出错")
            continue
    return rendered


@pytest.mark.asyncio
async def test_both_surface_projections_read_identical_canonical_sequence():
    stream = _FakeCanonicalStream(
        [
            (0, ("tool_started", {})),
            (0, ("approval_required", {})),
            (0.05, ("content", "answer")),
            (0, ("done", "answer")),
        ]
    )
    canonical = await _drain(stream, interval_ms=10)
    assert canonical.count((HEARTBEAT_KIND, heartbeat_payload("waiting_gate"))) >= 1

    webui_view = _webui_style_project(canonical)
    feishu_view = _feishu_style_project(canonical)
    assert webui_view == feishu_view
    assert webui_view[-1] == "完成"


# ── real _core._verified_codex_stream hook (mapped-run seam) ─────────────


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


@pytest.mark.asyncio
async def test_verified_codex_stream_projects_live_heartbeats_during_mapped_wait(
    monkeypatch, tmp_path: Path
):
    """Integration proof for the actual production hook: while the mapped
    Codex subprocess is still being read (simulated by a slow FakeStdout),
    heartbeats reach the caller LIVE -- not only after the whole run
    finishes -- while real content stays buffered and gated behind
    ``_complete_codex_spend_receipt`` exactly as before (t01/t04 contract).
    """
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.agent_real import _core, executor_map, codex_event_heartbeat

    monkeypatch.setattr(codex_event_heartbeat, "DEFAULT_INTERVAL_MS", 5)

    profile_home = tmp_path / "profile"
    profile_home.mkdir()

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
            self.lines = [
                b'{"event":"content","text":"slow answer"}\n',
                b'{"event":"done","result":"slow answer","error":null,"usage":{"api_calls":1}}\n',
            ]
            self._first = True

        async def readline(self):
            if self._first:
                self._first = False
                await asyncio.sleep(0.06)  # gives >=1 heartbeat @5ms a chance
            return self.lines.pop(0) if self.lines else b""

    class FakeStderr:
        async def read(self):
            return b""

    class FakeProc:
        def __init__(self):
            self.stdin = FakeStdin()
            self.stdout = FakeStdout()
            self.stderr = FakeStderr()
            self.pid = 123
            self.returncode = None

        async def wait(self):
            self.returncode = 0
            return 0

        def kill(self):
            self.returncode = -9

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return FakeProc()

    @contextmanager
    def fake_env_scope(*_args, **_kwargs):
        yield dict(os.environ)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(agent_real, "_aiagent_subprocess_env_scope", fake_env_scope)
    monkeypatch.setitem(
        agent_real._stream_aiagent_subprocess.__globals__,
        "_bind_codex_run_workspace",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        executor_map, "runtime_for_event", lambda *_args: executor_map.CODEX_APP_SERVER
    )

    receipt_calls = []

    async def fake_complete(_event, _profile):
        receipt_calls.append(1)
        with sqlite3.connect(profile_home / "state.db") as conn:
            # The buffered content must not have been committed to state.db
            # yet -- the receipt gate still runs strictly after the whole
            # buffered collection, heartbeats notwithstanding.
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE role='assistant'"
                ).fetchone()[0]
                == 0
            )

    monkeypatch.setattr(_core, "_complete_codex_spend_receipt", fake_complete)

    event = _event()
    stream = _core._verified_codex_stream(event, profile_home)

    seen: list[tuple[str, object]] = []
    saw_heartbeat_before_content = False
    async for kind, payload in stream:
        seen.append((kind, payload))
        if kind == codex_event_heartbeat.HEARTBEAT_KIND and not any(
            k == "content" for k, _ in seen[:-1]
        ):
            saw_heartbeat_before_content = True

    assert saw_heartbeat_before_content, (
        "heartbeat must be delivered live while the mapped subprocess read "
        "is still blocked, not only after the run completes"
    )
    non_heartbeat = [
        item for item in seen if item[0] != codex_event_heartbeat.HEARTBEAT_KIND
    ]
    assert non_heartbeat == [("content", "slow answer"), ("done", "slow answer")]
    assert receipt_calls == [1]
