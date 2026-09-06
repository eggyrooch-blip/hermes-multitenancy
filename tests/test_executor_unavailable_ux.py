"""Unavailable UX boundary (PLAN.md W1 t04): stable code + audit stay internal,
the employee gets a fixed Chinese action message and nothing else.

``executor_map.ExecutorUnavailable`` carries a raw English ``reason`` built for
logs (it can embed a wrapped exception's own text — a git-clone failure's
stderr, a filesystem path, an unknown-runtime key). That string must never
reach WebUI/Feishu. This file proves three things about
``hermes_multitenancy.agent_real.executor_unavailable_ux``:

1. ``classify()`` maps a caught exception to one of a small set of stable
   internal codes, reading ONLY the exception's own ``.reason`` — never event
   or request content (so a crafted event field cannot pick its own code).
2. The Chinese message shown to an employee is looked up BY CODE from a fixed
   table — never derived from the reason string — so nothing internal
   (error codes, exception class names, paths, hosts, headers, stack frames,
   hex fingerprints, command templates) can leak through by construction.
3. The two real/streaming raise sites in ``_core.py`` (commit 7ceadc8) are
   actually wired to this module, and a mapped-Codex failure still never
   falls back to the native tool loop.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_multitenancy.agent_real import executor_map
from hermes_multitenancy.agent_real import executor_unavailable_ux as ux
from hermes_multitenancy.agent_real._core import ExpertUnavailableError
from hermes_multitenancy.agent_real.codex_gate_resume import (
    GateResumeRejected,
    GateResumeStore,
    consume_gate_capability,
    enter_waiting_gate,
)
from hermes_multitenancy.agent_real.codex_session_bridge import (
    CodexSessionBridgeRejected,
    CodexSessionBridgeStore,
    plan_codex_thread,
    record_codex_thread,
)
from hermes_multitenancy.trusted_runtime_principal import issue_webui_principal


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _map_file(tmp_path: Path, payload: str) -> Path:
    path = tmp_path / "executors.yaml"
    path.write_text(payload, encoding="utf-8")
    return path


def _event(expert_id: str = "kep-server", run_id: str = "w1-ux-run", **extra_metadata):
    metadata: dict[str, object] = {"expert_id": expert_id, "run_id": run_id}
    metadata.update(extra_metadata)
    return SimpleNamespace(
        text="ship it",
        message_id="m1",
        source=SimpleNamespace(
            platform=SimpleNamespace(value="webui"),
            chat_id="webui-1",
            chat_name="",
            chat_type="p2p",
            user_id="ou_alice",
            user_name="Alice",
        ),
        raw_event={"metadata": metadata},
    )


def _real_stale_binding_rejection(tmp_path: Path) -> CodexSessionBridgeRejected:
    """Trigger the REAL t01 raise site (codex_session_bridge.plan_codex_thread)
    and return the genuine CodexSessionBridgeRejected('binding_stale') it
    raises — not a hand-fabricated string standing in for it."""
    store = CodexSessionBridgeStore(tmp_path / "bridge.db")
    principal = issue_webui_principal(
        profile_name="alice", actor_subject="ou_alice", credential_subject="ou_alice"
    )
    kwargs = dict(
        store=store, principal=principal, profile_name="alice",
        executor="codex_app_server", workflow_id="wf-1",
    )
    plan1 = plan_codex_thread(now_ms=1_000, **kwargs)
    record_codex_thread(plan1, store=store, thread_id="thread_abc", now_ms=1_000)
    far_future = 1_000 + (25 * 60 * 60 * 1000)  # past the 24h staleness ceiling
    with pytest.raises(CodexSessionBridgeRejected) as excinfo:
        plan_codex_thread(now_ms=far_future, **kwargs)
    return excinfo.value


def _real_malformed_event_rejection(tmp_path: Path) -> CodexSessionBridgeRejected:
    """Trigger the REAL t01 raise site for a structurally malformed event:
    ``plan_codex_thread``'s own opaque-id validation (``_opaque``) rejects a
    ``workflow_id`` that isn't a plain token -- exactly what happens when
    ``run_workspace.workflow_id_for(event)`` is fed a malformed event whose
    ``metadata.workflow_id`` is not a clean string (SPEC's own illustrative
    example). Not a hand-fabricated reason string standing in for it."""
    store = CodexSessionBridgeStore(tmp_path / "bridge-malformed.db")
    principal = issue_webui_principal(
        profile_name="alice", actor_subject="ou_alice", credential_subject="ou_alice"
    )
    with pytest.raises(CodexSessionBridgeRejected) as excinfo:
        plan_codex_thread(
            store=store, principal=principal, profile_name="alice",
            executor="codex_app_server",
            workflow_id="not a valid workflow id!!",  # fails the opaque-id regex
            now_ms=1_000,
        )
    return excinfo.value


def _real_gate_denied_rejection(tmp_path: Path) -> GateResumeRejected:
    """Trigger the REAL t03 raise site (codex_gate_resume.consume_gate_capability)
    and return the genuine GateResumeRejected('capability_unknown') it raises
    for an unissued/forged capability token -- not a hand-fabricated reason
    string standing in for it. Same denial shape as
    test_codex_gate_resume.py's own
    test_gate_resume_rejected_is_recognized_by_existing_unavailable_ux."""
    store = GateResumeStore(tmp_path / "gate-denied.db")
    enter_waiting_gate(
        store=store, run_id="w1-ux-run", actor_subject="ou_alice",
        thread_id="thread_abc", gate="A", now_ms=1_000,
    )
    with pytest.raises(GateResumeRejected) as excinfo:
        consume_gate_capability(
            store=store, token="token-never-issued", run_id="w1-ux-run",
            actor_subject="ou_alice", thread_id="thread_abc", gate="A",
            now_ms=1_000,
        )
    return excinfo.value


# --------------------------------------------------------------------------- #
# 1. classify() — stable codes, from the trusted exception only
# --------------------------------------------------------------------------- #
def test_classify_binary_missing():
    exc = executor_map.ExecutorUnavailable(
        "codex_app_server was mapped but no 'codex' binary is on the run PATH"
    )
    assert ux.classify(exc) == ux.CODEX_BINARY_MISSING


def test_classify_thread_stale():
    exc = executor_map.ExecutorUnavailable(
        "codex thread binding is stale for this workflow: session moved profiles"
    )
    assert ux.classify(exc) == ux.CODEX_THREAD_STALE


def test_classify_thread_stale_matches_real_bridge_rejection_token(tmp_path):
    """t01's real raise site (codex_session_bridge.plan_codex_thread) raises
    CodexSessionBridgeRejected('binding_stale') -- a bare stable token, not
    prose. classify() must recognize the genuine exception object, not just
    a hand-written sentence containing the word 'thread'."""
    exc = _real_stale_binding_rejection(tmp_path)
    assert str(exc) == "binding_stale"
    assert ux.classify(exc) == ux.CODEX_THREAD_STALE


# t03 (gate A->B->C->D machinery) is now built; its real raise site
# (codex_gate_resume.GateResumeRejected) always carries an explicit
# `.code = CODEX_GATE_DENIED`, so classify()'s explicit-code path wins over
# this keyword heuristic outright for the genuine exception (see classify()'s
# docstring). This test stays as a classify()-unit-level check against
# invented English reason text for a plain ExecutorUnavailable that merely
# mentions "gate denied" -- the end-to-end proof through the two _core.py
# raise sites, driven by a real GateResumeRejected, lives in section 4:
# test_nonstream_real_gate_denied_surfaces_chinese_only_and_never_falls_back /
# test_stream_real_gate_denied_surfaces_chinese_only_and_never_falls_back.
def test_classify_gate_denied():
    exc = executor_map.ExecutorUnavailable(
        "gate C denied: capability was minted for a different run_id"
    )
    assert ux.classify(exc) == ux.CODEX_GATE_DENIED


def test_classify_malformed_event():
    exc = executor_map.ExecutorUnavailable(
        "malformed event: metadata.workflow_id is not a string"
    )
    assert ux.classify(exc) == ux.CODEX_EVENT_MALFORMED


def test_classify_malformed_event_matches_real_bridge_rejection_token(tmp_path):
    """t01's real raise site for a malformed event
    (codex_session_bridge.plan_codex_thread's opaque-id validation) raises
    CodexSessionBridgeRejected('workflow_id_invalid') -- a bare stable token,
    not prose. classify() must recognize the genuine exception object, not
    just the hand-written sentence above."""
    exc = _real_malformed_event_rejection(tmp_path)
    assert str(exc) == "workflow_id_invalid"
    assert ux.classify(exc) == ux.CODEX_EVENT_MALFORMED


def test_classify_unknown_reason_falls_back_to_generic():
    exc = executor_map.ExecutorUnavailable(
        "single-actor spend receipt failed: sqlite3.OperationalError('locked')"
    )
    assert ux.classify(exc) == ux.CODEX_RUNTIME_ERROR


def test_classify_explicit_code_attribute_wins():
    """A future raiser (ticket 01/03) can set `.code` directly; keyword match
    is only the bridge for exceptions that don't."""
    exc = executor_map.ExecutorUnavailable("some english text mentioning a gate")
    exc.code = ux.CODEX_THREAD_STALE
    assert ux.classify(exc) == ux.CODEX_THREAD_STALE


def test_classify_ignores_request_content_never_just_exception_reason(tmp_path):
    """Classification must come from the trusted exception, never the event.

    Two adversarial events — one with no injected fields, one where the
    caller stuffed a forged executor/gate/code-shaped payload into metadata —
    must classify identically for the same internal exception: only ``exc``
    is ever consulted.
    """
    exc = executor_map.ExecutorUnavailable(
        "codex_app_server was mapped but no 'codex' binary is on the run PATH"
    )
    plain_event = _event()
    injected_event = _event(
        executor="codex_app_server",
        runtime="codex_app_server",
        gate="A",
        code=ux.CODEX_GATE_DENIED,
        injected_message="本次操作未获批准，已停止执行。",
    )
    code_plain = ux.classify(exc)
    # render_unavailable takes the event only for run_id/audit — never for
    # classification. Prove the resulting code is identical either way.
    result_plain = ux.render_unavailable(exc, event=plain_event)
    result_injected = ux.render_unavailable(exc, event=injected_event)
    assert code_plain == ux.CODEX_BINARY_MISSING
    assert result_plain.code == result_injected.code == ux.CODEX_BINARY_MISSING
    assert str(result_plain) == str(result_injected)


# --------------------------------------------------------------------------- #
# 2. employee_message() — fixed table, one line per code, no derivation
# --------------------------------------------------------------------------- #
_LEAK_PATTERNS = [
    re.compile(r"[A-Z][A-Z0-9_]{2,}"),  # SCREAMING_SNAKE error codes / class-ish tokens
    re.compile(r"Traceback"),
    re.compile(r"/[A-Za-z0-9_./-]+"),  # absolute/relative filesystem paths
    re.compile(r"\b[a-z0-9-]+\.(com|net|org|local|internal)\b", re.I),  # hostnames
    re.compile(r"\bAuthorization\b", re.I),
    re.compile(r"\bBearer\b"),
    re.compile(r"\b[0-9a-f]{16,}\b", re.I),  # hex fingerprints/tokens
    re.compile(r"\bgit clone\b|\bcurl \b|\bssh \b"),  # command templates
]


def test_every_employee_message_is_pure_chinese_action_text():
    for code in (
        ux.CODEX_BINARY_MISSING,
        ux.CODEX_THREAD_STALE,
        ux.CODEX_GATE_DENIED,
        ux.CODEX_EVENT_MALFORMED,
        ux.CODEX_RUNTIME_ERROR,
    ):
        message = ux.employee_message(code)
        assert message, code
        for pattern in _LEAK_PATTERNS:
            assert not pattern.search(message), (code, message, pattern.pattern)


def test_unknown_code_falls_back_to_generic_message():
    assert ux.employee_message("SOME_FUTURE_CODE") == ux.employee_message(
        ux.CODEX_RUNTIME_ERROR
    )


# --------------------------------------------------------------------------- #
# 3. render_unavailable() — the whole leak surface, adversarial reasons
# --------------------------------------------------------------------------- #
_ADVERSARIAL_REASONS = [
    "executor map unreadable: /Users/hermes/.config/hermes/executors.yaml",
    "run workspace could not be prepared: fatal: unable to access "
    "'https://gitlab.example.com/sunke/hermes-web-ui.git/': "
    "The requested URL returned error: 403",
    "single-actor spend receipt failed: Traceback (most recent call last): "
    "ValueError: bad row at sqlite3.py line 42",
    "codex proxy upstream Authorization: Bearer hcx_9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c",
    "CODEX_HOME could not be materialized: PermissionError(13, 'codex-home/config.toml')",
    "no run-scoped Codex provider proxy credential for host proxy.internal.example.com",
]


@pytest.mark.parametrize("reason", _ADVERSARIAL_REASONS)
def test_render_unavailable_never_leaks_the_internal_reason(reason, tmp_path, monkeypatch):
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))
    exc = executor_map.ExecutorUnavailable(reason)
    result = ux.render_unavailable(exc, event=_event())
    rendered = str(result)
    assert reason not in rendered
    assert "EXECUTOR_UNAVAILABLE" not in rendered
    assert "ExecutorUnavailable" not in rendered
    assert type(exc).__name__ not in rendered
    for pattern in _LEAK_PATTERNS:
        assert not pattern.search(rendered), (reason, rendered, pattern.pattern)
    # The employee message must be exactly one of the fixed table entries.
    assert rendered in ux._EMPLOYEE_MESSAGE.values()


def test_render_unavailable_result_is_still_executor_unavailable_isinstance():
    """Downstream `except executor_map.ExecutorUnavailable:` re-raise points
    (t01's attestation checks, etc.) must keep working unmodified."""
    exc = executor_map.ExecutorUnavailable("codex' binary is on the run PATH missing")
    result = ux.render_unavailable(exc, event=_event())
    assert isinstance(result, executor_map.ExecutorUnavailable)
    assert isinstance(result, ExpertUnavailableError)
    assert result.error_code == "EXPERT_UNAVAILABLE"
    assert result.failure_subsystem == "expert_resolution"


def test_render_unavailable_writes_structured_audit_with_code_and_run_id(
    tmp_path, monkeypatch
):
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))
    reason = (
        "codex_app_server was mapped but no 'codex' binary is on the run PATH"
    )
    exc = executor_map.ExecutorUnavailable(reason)
    ux.render_unavailable(exc, event=_event(run_id="w1-ux-audit-run"))

    lines = [json.loads(line) for line in audit_path.read_text().splitlines() if line]
    ours = [l for l in lines if l["event_type"] == ux.AUDIT_EVENT_TYPE]
    assert len(ours) == 1
    record = ours[0]
    assert record["run_id"] == "w1-ux-audit-run"
    assert ux.CODEX_BINARY_MISSING in record["reason"]
    # The raw internal reason text must not appear verbatim in the audit line
    # (hashed/non-secret context only) — only the fixed classified code and a
    # short fingerprint travel.
    assert reason not in json.dumps(record)
    assert "/Users" not in json.dumps(record)


# --------------------------------------------------------------------------- #
# 4. wiring into _core.py's two raise sites (commit 7ceadc8) — non-stream
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_nonstream_mapped_failure_surfaces_chinese_only_and_never_falls_back(
    tmp_path: Path, monkeypatch
):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.agent_real import _core

    profile_home = tmp_path / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    map_path = _map_file(tmp_path, "kep-server: codex_app_server\n")
    monkeypatch.setenv(executor_map.EXECUTOR_MAP_ENV, str(map_path))
    monkeypatch.setattr(
        _core, "_resolve_explicit_expert_for_execution", lambda *_args: None
    )

    async def failed_run(*_args, **_kwargs):
        raise executor_map.ExecutorUnavailable(
            "codex_app_server was mapped but no 'codex' binary is on the run PATH"
        )

    legacy_calls = 0

    async def legacy_run(*_args, **_kwargs):
        nonlocal legacy_calls
        legacy_calls += 1
        return "legacy"

    monkeypatch.setattr(_core, "_run_aiagent_subprocess", failed_run)
    monkeypatch.setattr(_core, "_legacy_real_run_agent", legacy_run)

    with pytest.raises(executor_map.ExecutorUnavailable) as excinfo:
        await agent_real.real_run_agent(_event("kep-server"), profile_home)

    assert legacy_calls == 0
    rendered = str(excinfo.value)
    assert rendered == ux.employee_message(ux.CODEX_BINARY_MISSING)
    assert "PATH" not in rendered
    assert "binary" not in rendered.lower()


@pytest.mark.asyncio
async def test_stream_mapped_failure_surfaces_chinese_only_and_never_falls_back(
    tmp_path: Path, monkeypatch
):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.agent_real import _core

    profile_home = tmp_path / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    map_path = _map_file(tmp_path, "kep-server: codex_app_server\n")
    monkeypatch.setenv(executor_map.EXECUTOR_MAP_ENV, str(map_path))
    monkeypatch.setattr(
        _core, "_resolve_explicit_expert_for_execution", lambda *_args: None
    )

    async def failed_stream(*_args, **_kwargs):
        raise executor_map.ExecutorUnavailable(
            "codex thread binding is stale for this workflow"
        )
        yield  # pragma: no cover - unreachable, makes this an async generator

    legacy_calls = 0

    async def legacy_stream(*_args, **_kwargs):
        nonlocal legacy_calls
        legacy_calls += 1
        yield "content", "legacy"

    monkeypatch.setattr(_core, "_verified_codex_stream", failed_stream)
    monkeypatch.setattr(agent_real, "_stream_loop", legacy_stream)

    with pytest.raises(executor_map.ExecutorUnavailable) as excinfo:
        _ = [
            item
            async for item in agent_real.stream_run_agent(
                _event("kep-server"), profile_home
            )
        ]

    assert legacy_calls == 0
    rendered = str(excinfo.value)
    assert rendered == ux.employee_message(ux.CODEX_THREAD_STALE)
    assert "thread" not in rendered.lower()
    assert "stale" not in rendered.lower()
    assert "workflow" not in rendered.lower()


@pytest.mark.asyncio
async def test_nonstream_real_bridge_thread_stale_surfaces_chinese_only_and_never_falls_back(
    tmp_path: Path, monkeypatch
):
    """t01's real rejection type is CodexSessionBridgeRejected, NOT
    ExecutorUnavailable -- prove the _core.py boundary (executor_unavailable_ux
    .is_unavailable) catches that type too, using a genuine stale-binding
    rejection from plan_codex_thread (not a hand-fabricated reason string)."""
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.agent_real import _core

    profile_home = tmp_path / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    map_path = _map_file(tmp_path, "kep-server: codex_app_server\n")
    monkeypatch.setenv(executor_map.EXECUTOR_MAP_ENV, str(map_path))
    monkeypatch.setattr(
        _core, "_resolve_explicit_expert_for_execution", lambda *_args: None
    )

    async def failed_run(*_args, **_kwargs):
        raise _real_stale_binding_rejection(tmp_path)

    legacy_calls = 0

    async def legacy_run(*_args, **_kwargs):
        nonlocal legacy_calls
        legacy_calls += 1
        return "legacy"

    monkeypatch.setattr(_core, "_run_aiagent_subprocess", failed_run)
    monkeypatch.setattr(_core, "_legacy_real_run_agent", legacy_run)

    with pytest.raises(executor_map.ExecutorUnavailable) as excinfo:
        await agent_real.real_run_agent(_event("kep-server"), profile_home)

    assert legacy_calls == 0
    rendered = str(excinfo.value)
    assert rendered == ux.employee_message(ux.CODEX_THREAD_STALE)
    assert "binding_stale" not in rendered
    assert "stale" not in rendered.lower()


@pytest.mark.asyncio
async def test_stream_real_bridge_thread_stale_surfaces_chinese_only_and_never_falls_back(
    tmp_path: Path, monkeypatch
):
    """Same proof as the non-stream case above, for the streaming raise site."""
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.agent_real import _core

    profile_home = tmp_path / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    map_path = _map_file(tmp_path, "kep-server: codex_app_server\n")
    monkeypatch.setenv(executor_map.EXECUTOR_MAP_ENV, str(map_path))
    monkeypatch.setattr(
        _core, "_resolve_explicit_expert_for_execution", lambda *_args: None
    )

    async def failed_stream(*_args, **_kwargs):
        raise _real_stale_binding_rejection(tmp_path)
        yield  # pragma: no cover - unreachable, makes this an async generator

    legacy_calls = 0

    async def legacy_stream(*_args, **_kwargs):
        nonlocal legacy_calls
        legacy_calls += 1
        yield "content", "legacy"

    monkeypatch.setattr(_core, "_verified_codex_stream", failed_stream)
    monkeypatch.setattr(agent_real, "_stream_loop", legacy_stream)

    with pytest.raises(executor_map.ExecutorUnavailable) as excinfo:
        _ = [
            item
            async for item in agent_real.stream_run_agent(
                _event("kep-server"), profile_home
            )
        ]

    assert legacy_calls == 0
    rendered = str(excinfo.value)
    assert rendered == ux.employee_message(ux.CODEX_THREAD_STALE)
    assert "binding_stale" not in rendered
    assert "stale" not in rendered.lower()


@pytest.mark.asyncio
async def test_nonstream_real_bridge_malformed_event_surfaces_chinese_only_and_never_falls_back(
    tmp_path: Path, monkeypatch
):
    """Same proof as the thread-stale case above, for the malformed-event
    face: a genuine CodexSessionBridgeRejected('workflow_id_invalid') from
    plan_codex_thread must render as CODEX_EVENT_MALFORMED's Chinese text
    only, with no fallback to the native tool loop."""
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.agent_real import _core

    profile_home = tmp_path / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    map_path = _map_file(tmp_path, "kep-server: codex_app_server\n")
    monkeypatch.setenv(executor_map.EXECUTOR_MAP_ENV, str(map_path))
    monkeypatch.setattr(
        _core, "_resolve_explicit_expert_for_execution", lambda *_args: None
    )

    async def failed_run(*_args, **_kwargs):
        raise _real_malformed_event_rejection(tmp_path)

    legacy_calls = 0

    async def legacy_run(*_args, **_kwargs):
        nonlocal legacy_calls
        legacy_calls += 1
        return "legacy"

    monkeypatch.setattr(_core, "_run_aiagent_subprocess", failed_run)
    monkeypatch.setattr(_core, "_legacy_real_run_agent", legacy_run)

    with pytest.raises(executor_map.ExecutorUnavailable) as excinfo:
        await agent_real.real_run_agent(_event("kep-server"), profile_home)

    assert legacy_calls == 0
    rendered = str(excinfo.value)
    assert rendered == ux.employee_message(ux.CODEX_EVENT_MALFORMED)
    assert "workflow_id_invalid" not in rendered
    assert "workflow" not in rendered.lower()


@pytest.mark.asyncio
async def test_stream_real_bridge_malformed_event_surfaces_chinese_only_and_never_falls_back(
    tmp_path: Path, monkeypatch
):
    """Same proof as the non-stream case above, for the streaming raise site."""
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.agent_real import _core

    profile_home = tmp_path / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    map_path = _map_file(tmp_path, "kep-server: codex_app_server\n")
    monkeypatch.setenv(executor_map.EXECUTOR_MAP_ENV, str(map_path))
    monkeypatch.setattr(
        _core, "_resolve_explicit_expert_for_execution", lambda *_args: None
    )

    async def failed_stream(*_args, **_kwargs):
        raise _real_malformed_event_rejection(tmp_path)
        yield  # pragma: no cover - unreachable, makes this an async generator

    legacy_calls = 0

    async def legacy_stream(*_args, **_kwargs):
        nonlocal legacy_calls
        legacy_calls += 1
        yield "content", "legacy"

    monkeypatch.setattr(_core, "_verified_codex_stream", failed_stream)
    monkeypatch.setattr(agent_real, "_stream_loop", legacy_stream)

    with pytest.raises(executor_map.ExecutorUnavailable) as excinfo:
        _ = [
            item
            async for item in agent_real.stream_run_agent(
                _event("kep-server"), profile_home
            )
        ]

    assert legacy_calls == 0
    rendered = str(excinfo.value)
    assert rendered == ux.employee_message(ux.CODEX_EVENT_MALFORMED)
    assert "workflow_id_invalid" not in rendered
    assert "workflow" not in rendered.lower()


@pytest.mark.asyncio
async def test_nonstream_real_gate_denied_surfaces_chinese_only_and_never_falls_back(
    tmp_path: Path, monkeypatch
):
    """t03's real rejection type is GateResumeRejected, already recognized by
    is_unavailable() via its explicit `.code = CODEX_GATE_DENIED` -- prove the
    _core.py boundary actually routes a genuine gate denial through the same
    Chinese-only, no-fallback, audited path as binary-missing/thread-stale/
    malformed-event, using a real rejection from consume_gate_capability (not
    a hand-fabricated reason string)."""
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.agent_real import _core

    profile_home = tmp_path / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    map_path = _map_file(tmp_path, "kep-server: codex_app_server\n")
    monkeypatch.setenv(executor_map.EXECUTOR_MAP_ENV, str(map_path))
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))
    monkeypatch.setattr(
        _core, "_resolve_explicit_expert_for_execution", lambda *_args: None
    )

    async def failed_run(*_args, **_kwargs):
        raise _real_gate_denied_rejection(tmp_path)

    legacy_calls = 0

    async def legacy_run(*_args, **_kwargs):
        nonlocal legacy_calls
        legacy_calls += 1
        return "legacy"

    monkeypatch.setattr(_core, "_run_aiagent_subprocess", failed_run)
    monkeypatch.setattr(_core, "_legacy_real_run_agent", legacy_run)

    with pytest.raises(executor_map.ExecutorUnavailable) as excinfo:
        await agent_real.real_run_agent(
            _event("kep-server", run_id="w1-ux-gate-run"), profile_home
        )

    assert legacy_calls == 0
    rendered = str(excinfo.value)
    assert rendered == ux.employee_message(ux.CODEX_GATE_DENIED)
    assert rendered == "本次操作未获批准，已停止执行。"
    assert "capability_unknown" not in rendered
    assert "GateResumeRejected" not in rendered
    assert "CODEX_GATE_DENIED" not in rendered
    assert "Traceback" not in rendered
    assert "/" not in rendered

    lines = [json.loads(line) for line in audit_path.read_text().splitlines() if line]
    ours = [l for l in lines if l["event_type"] == ux.AUDIT_EVENT_TYPE]
    assert len(ours) == 1
    assert ours[0]["run_id"] == "w1-ux-gate-run"
    assert ux.CODEX_GATE_DENIED in ours[0]["reason"]


@pytest.mark.asyncio
async def test_stream_real_gate_denied_surfaces_chinese_only_and_never_falls_back(
    tmp_path: Path, monkeypatch
):
    """Same proof as the non-stream case above, for the streaming raise site."""
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.agent_real import _core

    profile_home = tmp_path / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    map_path = _map_file(tmp_path, "kep-server: codex_app_server\n")
    monkeypatch.setenv(executor_map.EXECUTOR_MAP_ENV, str(map_path))
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))
    monkeypatch.setattr(
        _core, "_resolve_explicit_expert_for_execution", lambda *_args: None
    )

    async def failed_stream(*_args, **_kwargs):
        raise _real_gate_denied_rejection(tmp_path)
        yield  # pragma: no cover - unreachable, makes this an async generator

    legacy_calls = 0

    async def legacy_stream(*_args, **_kwargs):
        nonlocal legacy_calls
        legacy_calls += 1
        yield "content", "legacy"

    monkeypatch.setattr(_core, "_verified_codex_stream", failed_stream)
    monkeypatch.setattr(agent_real, "_stream_loop", legacy_stream)

    with pytest.raises(executor_map.ExecutorUnavailable) as excinfo:
        _ = [
            item
            async for item in agent_real.stream_run_agent(
                _event("kep-server", run_id="w1-ux-gate-stream-run"), profile_home
            )
        ]

    assert legacy_calls == 0
    rendered = str(excinfo.value)
    assert rendered == ux.employee_message(ux.CODEX_GATE_DENIED)
    assert rendered == "本次操作未获批准，已停止执行。"
    assert "capability_unknown" not in rendered
    assert "GateResumeRejected" not in rendered
    assert "CODEX_GATE_DENIED" not in rendered
    assert "Traceback" not in rendered
    assert "/" not in rendered

    lines = [json.loads(line) for line in audit_path.read_text().splitlines() if line]
    ours = [l for l in lines if l["event_type"] == ux.AUDIT_EVENT_TYPE]
    assert len(ours) == 1
    assert ours[0]["run_id"] == "w1-ux-gate-stream-run"
    assert ux.CODEX_GATE_DENIED in ours[0]["reason"]


# --------------------------------------------------------------------------- #
# 5. hermes_default (unmapped) path is untouched by this ticket
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unmapped_run_still_falls_back_to_legacy_on_generic_failure(
    tmp_path: Path, monkeypatch
):
    """No executor map at all: a plain failure must still hit the pre-existing
    legacy fallback ladder, byte-for-byte — this module must be a no-op for
    the unmapped path (it never raises ExecutorUnavailable in the first place)."""
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.agent_real import _core

    profile_home = tmp_path / "profiles" / "bob"
    profile_home.mkdir(parents=True)
    # HERMES_EXECUTOR_MAP intentionally unset — default-off, hermes_default.
    monkeypatch.delenv(executor_map.EXECUTOR_MAP_ENV, raising=False)
    monkeypatch.setattr(
        _core, "_resolve_explicit_expert_for_execution", lambda *_args: None
    )

    async def failed_run(*_args, **_kwargs):
        raise ValueError("some ordinary transient failure, not codex-related")

    legacy_calls = 0

    async def legacy_run(*_args, **_kwargs):
        nonlocal legacy_calls
        legacy_calls += 1
        return "legacy answer"

    monkeypatch.setattr(_core, "_run_aiagent_subprocess", failed_run)
    monkeypatch.setattr(_core, "_legacy_real_run_agent", legacy_run)

    result = await agent_real.real_run_agent(_event("no-such-expert"), profile_home)

    assert result == "legacy answer"
    assert legacy_calls == 1
