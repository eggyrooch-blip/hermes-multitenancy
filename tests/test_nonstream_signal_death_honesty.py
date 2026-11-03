"""The non-streaming path must not blame stale stderr when the gateway restarts.

Sibling of ``test_stream_transient_exit_after_done.py``: that one fixed the
streaming raise site (``streaming.py``), this one fixes the identical lie in
``_run_aiagent_subprocess`` — the path feishu / cron / non-streaming callers
take.

Two ways the same signal death used to surface as a lie:

* stdout is empty (the child died before writing its result JSON), so the code
  fell into the ``invalid JSON`` branch first and told the user the *child's
  protocol* was broken — with a stale stderr tail quoted as evidence.
* stdout parsed but ``returncode != 0``, so the ``exited -15: <stderr tail>``
  message quoted output produced seconds before the SIGTERM as if it were the
  cause of death.

Real crashes (``returncode > 0``) keep both messages verbatim: there the stderr
tail genuinely is the cause.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


_STALE_STDERR = (
    "⚠️  Encrypted reasoning replay was rejected by the provider — "
    "disabled replay and stripped 1 item(s) from 1 message(s), retrying..."
)


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


def _install_fake_proc(monkeypatch, *, stdout: bytes, stderr: bytes, returncode: int):
    class FakeProc:
        def __init__(self):
            self.returncode = returncode

        async def communicate(self, _payload):
            return stdout, stderr

        def kill(self):  # pragma: no cover - only the timeout path calls this
            self.returncode = -9

        async def wait(self):  # pragma: no cover
            return self.returncode

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)


@pytest.mark.asyncio
async def test_signal_death_with_empty_stdout_blames_the_restart(
    monkeypatch, tmp_path: Path, caplog
):
    """SIGTERM before the child wrote its JSON → honest message, not 'invalid JSON'."""
    from hermes_multitenancy import agent_real

    _install_fake_proc(
        monkeypatch,
        stdout=b"",
        stderr=_STALE_STDERR.encode("utf-8"),
        returncode=-15,
    )

    with caplog.at_level("WARNING"):
        with pytest.raises(agent_real.GatewayRestartInterruptedError) as excinfo:
            await agent_real._run_aiagent_subprocess(_event(), tmp_path)

    message = str(excinfo.value)
    assert "网关重启" in message and "信号 15" in message
    # The two lies this fixes: the invalid-JSON misdirection and the stale tail.
    assert "invalid JSON" not in message
    assert "Encrypted reasoning replay" not in message
    assert excinfo.value.error_code == "GATEWAY_RESTART_INTERRUPTED"
    # Triage must not degrade — the full stderr still reaches the log.
    assert any(
        "killed by signal 15" in record.getMessage()
        and "Encrypted reasoning replay" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_signal_death_with_delivered_json_blames_the_restart(
    monkeypatch, tmp_path: Path
):
    """The other signal-death shape: JSON arrived but carried no result."""
    from hermes_multitenancy import agent_real

    _install_fake_proc(
        monkeypatch,
        stdout=json.dumps({"result": "", "error": None}).encode("utf-8"),
        stderr=_STALE_STDERR.encode("utf-8"),
        returncode=-15,
    )

    with pytest.raises(agent_real.GatewayRestartInterruptedError) as excinfo:
        await agent_real._run_aiagent_subprocess(_event(), tmp_path)

    assert "网关重启" in str(excinfo.value)
    assert "AIAgent subprocess exited" not in str(excinfo.value)
    assert "Encrypted reasoning replay" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_signal_death_after_result_delivered_keeps_the_turn(
    monkeypatch, tmp_path: Path, caplog
):
    """The result JSON is the child's terminal report; a later signal is teardown.

    Mirrors the streaming side's ``saw_done`` rule — do not throw away an answer
    the child already produced.
    """
    from hermes_multitenancy import agent_real

    _install_fake_proc(
        monkeypatch,
        stdout=json.dumps({"result": "答案", "error": None}).encode("utf-8"),
        stderr=b"teardown noise",
        returncode=-15,
    )

    with caplog.at_level("WARNING"):
        assert await agent_real._run_aiagent_subprocess(_event(), tmp_path) == "答案"

    assert any(
        "AFTER delivering its result" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize("payload", ["null", "[]", '"half-written"'])
@pytest.mark.asyncio
async def test_signal_death_with_non_object_json_never_falls_back(
    monkeypatch, tmp_path: Path, payload: str
):
    """F002: valid JSON that isn't an object used to AttributeError into legacy.

    A half-written stdout can still parse (``null``, ``[]``, a bare string).
    Every ``.get`` below the parse then raised ``AttributeError``, which
    ``real_run_agent``'s catch-all converted into a legacy re-run — double tool
    side effects, double spend, and the restart hidden from the user.
    """
    from hermes_multitenancy import agent_real

    _install_fake_proc(
        monkeypatch,
        stdout=payload.encode("utf-8"),
        stderr=_STALE_STDERR.encode("utf-8"),
        returncode=-15,
    )

    async def legacy_must_not_run(event, profile_home, messages=None):
        raise AssertionError("legacy runner must not answer an interrupted turn")

    monkeypatch.setattr(agent_real, "_legacy_real_run_agent", legacy_must_not_run)

    with pytest.raises(agent_real.GatewayRestartInterruptedError) as excinfo:
        await agent_real.real_run_agent(_event(), tmp_path)

    assert "网关重启" in str(excinfo.value) and "信号 15" in str(excinfo.value)
    assert "Encrypted reasoning replay" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_true_crash_invalid_json_message_unchanged(monkeypatch, tmp_path: Path):
    """returncode > 0 with unparseable stdout → the old diagnostic, verbatim."""
    from hermes_multitenancy import agent_real

    _install_fake_proc(
        monkeypatch,
        stdout=b"Traceback (most recent call last):",
        stderr=b"Boom",
        returncode=1,
    )

    with pytest.raises(RuntimeError, match="returned invalid JSON") as excinfo:
        await agent_real._run_aiagent_subprocess(_event(), tmp_path)
    assert "Boom" in str(excinfo.value)
    assert not isinstance(excinfo.value, agent_real.GatewayRestartInterruptedError)


@pytest.mark.asyncio
async def test_true_crash_nonzero_exit_keeps_stderr_tail(monkeypatch, tmp_path: Path):
    """returncode > 0 with parsed stdout → the stderr tail IS the cause. Unchanged."""
    from hermes_multitenancy import agent_real

    _install_fake_proc(
        monkeypatch,
        stdout=json.dumps({"result": None, "error": None}).encode("utf-8"),
        stderr=b"Boom",
        returncode=1,
    )

    with pytest.raises(RuntimeError, match="AIAgent subprocess exited 1") as excinfo:
        await agent_real._run_aiagent_subprocess(_event(), tmp_path)
    assert "Boom" in str(excinfo.value)
    assert not isinstance(excinfo.value, agent_real.GatewayRestartInterruptedError)


@pytest.mark.asyncio
async def test_signal_death_never_falls_back_to_the_legacy_runner(
    monkeypatch, tmp_path: Path
):
    """``real_run_agent`` catches everything and answers via the legacy spike.

    For an interrupted turn that is wrong twice over: the legacy runner would
    re-run the whole prompt (double tool side effects, double spend) and answer
    as if nothing happened, hiding the restart entirely.
    """
    from hermes_multitenancy import agent_real

    _install_fake_proc(
        monkeypatch,
        stdout=b"",
        stderr=_STALE_STDERR.encode("utf-8"),
        returncode=-15,
    )

    async def legacy_must_not_run(event, profile_home, messages=None):
        raise AssertionError("legacy runner must not answer an interrupted turn")

    monkeypatch.setattr(agent_real, "_legacy_real_run_agent", legacy_must_not_run)

    with pytest.raises(agent_real.GatewayRestartInterruptedError) as excinfo:
        await agent_real.real_run_agent(_event(), tmp_path)
    assert "网关重启" in str(excinfo.value)
