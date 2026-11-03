"""RunBroker target-state contract tests."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import sys
from types import ModuleType, SimpleNamespace

import pytest

from tests._sync import SYNC_TIMEOUT


def test_run_request_requires_profile_user_and_content():
    from hermes_multitenancy.run_models import RunRequest

    with pytest.raises(ValueError, match="profile_name"):
        RunRequest(channel="feishu", profile_name="", user_key="ou_1", content="hi")

    with pytest.raises(ValueError, match="user_key"):
        RunRequest(channel="feishu", profile_name="owner", user_key="", content="hi")

    with pytest.raises(ValueError, match="content"):
        RunRequest(channel="feishu", profile_name="owner", user_key="ou_1", content="")


def test_run_request_rejects_unknown_channel():
    from hermes_multitenancy.run_models import RunRequest

    with pytest.raises(ValueError, match="channel"):
        RunRequest(channel="email", profile_name="owner", user_key="ou_1", content="hi")


def _write_cowork_expert_manifest(shared_home, *, audience="alice", **expert_overrides):
    import json

    managed = shared_home / ".hermes-plugin-managed"
    managed.mkdir(parents=True, exist_ok=True)
    expert = {
        "id": "finance",
        "name": "Finance",
        "version": "1.2.3",
        "agent_scope": "finance_lead",
        "skills": ["sheet", "report"],
        "hermes_tool_scopes": ["mail.read", "docs.read"],
        **expert_overrides,
    }
    path = managed / f"plugin-{len(list(managed.iterdir()))}.json"
    path.write_text(
        json.dumps({
            "status": "active",
            "plugin_id": path.stem,
            "audience": audience if isinstance(audience, dict) else {"profiles": [audience]},
            "experts": [expert],
        }),
        encoding="utf-8",
    )


def test_cowork_expert_mapping_is_unique_authorized_and_deterministic(tmp_path):
    from hermes_multitenancy.cowork_enterprise import (
        CoworkExpertConflict,
        CoworkExpertNotFound,
        resolve_expert_mapping,
    )

    shared = tmp_path / "shared"
    alice = shared / "profiles" / "alice"
    bob = shared / "profiles" / "bob"
    _write_cowork_expert_manifest(shared)

    mapping = resolve_expert_mapping(alice, "finance")
    repeated = resolve_expert_mapping(alice, "finance")
    assert mapping == repeated
    assert mapping.agent_scope == "finance_lead"
    assert mapping.expert_version == "1.2.3"
    assert mapping.hermes_tool_scopes == ("docs.read", "mail.read")
    assert len(mapping.source_fingerprint) == 64
    with pytest.raises(CoworkExpertNotFound):
        resolve_expert_mapping(bob, "finance")

    default = resolve_expert_mapping(alice, None)
    assert default.agent_scope == "lead_agent"
    assert default.expert_id is None
    assert default.hermes_tool_scopes == ()

    _write_cowork_expert_manifest(shared)
    with pytest.raises(CoworkExpertConflict):
        resolve_expert_mapping(alice, "finance")


def test_cowork_expert_mapping_fails_closed_for_disabled_or_incomplete(tmp_path):
    from hermes_multitenancy.cowork_enterprise import (
        CoworkCapabilityDenied,
        CoworkEnterpriseUnavailable,
        resolve_expert_mapping,
    )

    shared = tmp_path / "shared"
    profile = shared / "profiles" / "alice"
    _write_cowork_expert_manifest(shared, status="disabled")
    with pytest.raises(CoworkCapabilityDenied):
        resolve_expert_mapping(profile, "finance")

    for path in (shared / ".hermes-plugin-managed").iterdir():
        path.unlink()
    _write_cowork_expert_manifest(shared, agent_scope="")
    with pytest.raises(CoworkEnterpriseUnavailable):
        resolve_expert_mapping(profile, "finance")

    for path in (shared / ".hermes-plugin-managed").iterdir():
        path.unlink()
    _write_cowork_expert_manifest(
        shared,
        agent_scope=None,
        legacy_agent_scope="legacy_lead",
    )
    with pytest.raises(CoworkEnterpriseUnavailable, match="product-specific agent scope"):
        resolve_expert_mapping(profile, "finance")

    for path in (shared / ".hermes-plugin-managed").iterdir():
        path.unlink()
    _write_cowork_expert_manifest(shared, legacy_agent_scope="legacy_lead")
    with pytest.raises(CoworkEnterpriseUnavailable, match="product-specific agent scope"):
        resolve_expert_mapping(profile, "finance")

    for path in (shared / ".hermes-plugin-managed").iterdir():
        path.unlink()
    _write_cowork_expert_manifest(shared, agent_scope="runtime:lead")
    with pytest.raises(CoworkEnterpriseUnavailable, match="agent_scope is invalid"):
        resolve_expert_mapping(profile, "finance")


def test_cowork_expert_mapping_resolves_trusted_department_server_side(tmp_path, monkeypatch):
    from hermes_multitenancy import expert_overlay
    from hermes_multitenancy.cowork_enterprise import CoworkExpertNotFound, resolve_expert_mapping

    shared = tmp_path / "shared"
    profile = shared / "profiles" / "alice"
    _write_cowork_expert_manifest(shared, audience={"department_ids": ["dept-a"]})
    monkeypatch.setattr(
        expert_overlay, "resolve_caller_departments",
        lambda _home, **kwargs: ["dept-a"] if kwargs.get("open_id") == "actor-a" else None,
    )
    assert resolve_expert_mapping(profile, "finance", actor_subject="actor-a").expert_id == "finance"
    with pytest.raises(CoworkExpertNotFound, match="unavailable"):
        resolve_expert_mapping(profile, "finance", actor_subject="actor-b")


def test_cowork_capability_binds_every_dimension_and_is_one_use():
    from hermes_multitenancy.cowork_enterprise import (
        CoworkCapabilityDenied,
        CoworkCapabilityRegistry,
    )

    clock = [100.0]
    registry = CoworkCapabilityRegistry(clock=lambda: clock[0])
    token, _expires_at = registry.issue(
        profile_name="alice",
        actor_subject="actor-a",
        thread_id="thread-a",
        run_id="run-a",
        tool="mail.search",
        scope="mail.read",
        credential_subject="actor-a",
        allowed_scopes=("mail.read",),
        ttl_seconds=30,
    )
    assertions = dict(
        profile_name="alice",
        actor_subject="actor-a",
        thread_id="thread-a",
        run_id="run-a",
        tool="mail.search",
        scope="mail.read",
        credential_subject="actor-a",
    )
    with pytest.raises(CoworkCapabilityDenied, match="assertion mismatch"):
        registry.authorize(token, **(assertions | {"thread_id": "thread-b"}))
    with pytest.raises(CoworkCapabilityDenied, match="invalid or expired"):
        registry.authorize(token, **assertions)

    token, _ = registry.issue(
        profile_name="alice",
        actor_subject="actor-a",
        thread_id="thread-a",
        run_id="run-a",
        tool="mail.search",
        scope="mail.read",
        credential_subject="actor-a",
        allowed_scopes=("mail.read",),
    )
    assert registry.authorize(token, **assertions) == {
        "tool": "mail.search",
        "scope": "mail.read",
        "run_id": "run-a",
    }
    with pytest.raises(CoworkCapabilityDenied, match="invalid or expired"):
        registry.authorize(token, **assertions)

    expiring, _ = registry.issue(
        profile_name="alice",
        actor_subject="actor-a",
        thread_id="thread-a",
        run_id="run-b",
        tool="mail.search",
        scope="mail.read",
        credential_subject="actor-a",
        allowed_scopes=("mail.read",),
        ttl_seconds=1,
    )
    clock[0] += 2
    with pytest.raises(CoworkCapabilityDenied, match="invalid or expired"):
        registry.authorize(expiring, **(assertions | {"run_id": "run-b"}))


def test_cowork_capability_rejects_subject_scope_and_terminal_run():
    from hermes_multitenancy.cowork_enterprise import (
        CoworkCapabilityDenied,
        CoworkCapabilityRegistry,
    )

    registry = CoworkCapabilityRegistry()
    base = dict(
        profile_name="alice",
        actor_subject="actor-a",
        thread_id="thread-a",
        run_id="run-a",
        tool="mail.search",
        scope="mail.read",
        credential_subject="actor-a",
        allowed_scopes=("mail.read",),
    )
    with pytest.raises(CoworkCapabilityDenied, match="credential subject"):
        registry.issue(**(base | {"credential_subject": "actor-b"}))
    with pytest.raises(CoworkCapabilityDenied, match="scope"):
        registry.issue(**(base | {"scope": "mail.write"}))
    with pytest.raises(CoworkCapabilityDenied, match="scope"):
        registry.issue(**(base | {"tool": "docs.delete"}))

    token, _ = registry.issue(**base)
    assert registry.revoke_run(
        profile_name="alice", actor_subject="actor-a", run_id="run-a"
    ) == 1
    with pytest.raises(CoworkCapabilityDenied, match="invalid or expired"):
        registry.authorize(token, **{key: base[key] for key in (
            "profile_name", "actor_subject", "thread_id", "run_id", "tool",
            "scope", "credential_subject",
        )})


def test_cowork_capability_registry_is_bounded():
    from hermes_multitenancy.cowork_enterprise import (
        CoworkCapabilityRateLimited,
        CoworkCapabilityRegistry,
    )

    registry = CoworkCapabilityRegistry(max_records=1)
    base = dict(
        profile_name="alice", actor_subject="actor-a", thread_id="thread-a",
        run_id="run-a", tool="mail.search", scope="mail.read",
        credential_subject="actor-a", allowed_scopes=("mail.read",),
    )
    registry.issue(**base)
    with pytest.raises(CoworkCapabilityRateLimited):
        registry.issue(**(base | {"run_id": "run-b"}))


def test_cowork_internal_api_denial_is_redacted_and_never_dispatches(tmp_path, monkeypatch):
    import json
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.cowork_enterprise import register_routes

    shared = tmp_path / "shared"
    _write_cowork_expert_manifest(shared)
    audit_path = tmp_path / "security.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_ENABLED", "1")
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))
    dispatched = []

    def owner_tenant(_request, body=None, **_kwargs):
        payload = body or {}
        return str(payload.get("profile_name") or "alice"), str(payload.get("user_key") or "actor-a")

    async def exercise(credential_bound=True):
        app = web.Application()
        register_routes(
            app,
            authorize=lambda _request: True,
            owner_tenant=owner_tenant,
            profile_home=lambda profile: shared / "profiles" / profile,
            credential_bound=lambda *_args: credential_bound,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            granted = await client.post("/api/run-broker/internal/cowork/capabilities", json={
                "profile_name": "alice",
                "user_key": "actor-a",
                "expert_id": "finance",
                "thread_id": "thread-a",
                "run_id": "run-a",
                "tool": "mail.search",
                "scope": "mail.read",
                "credential_subject": "actor-a",
            })
            granted_payload = await granted.json()
            response = await client.post("/api/run-broker/internal/cowork/capabilities", json={
                "profile_name": "alice",
                "user_key": "actor-a",
                "expert_id": "finance",
                "thread_id": "thread-a",
                "run_id": "run-a",
                "tool": "mail.search",
                "scope": "mail.read",
                "credential_subject": "actor-b",
            })
            return granted.status, granted_payload, response.status, await response.json()
        finally:
            await client.close()

    granted_status, granted_payload, status, payload = asyncio.run(exercise())
    assert granted_status == 201
    assert granted_payload["capability"]
    assert status == 403
    assert payload["code"] == "COWORK_CAPABILITY_DENIED"
    assert dispatched == []
    audit_text = audit_path.read_text(encoding="utf-8")
    events = [json.loads(line) for line in audit_text.splitlines()]
    assert [event["decision"] for event in events] == ["granted", "denied"]
    event = events[-1]
    assert event["decision"] == "denied"
    assert event["reason"] == "COWORK_CAPABILITY_DENIED"
    assert event["open_id_hash"]
    assert "actor-a" not in audit_text
    assert "actor-b" not in audit_text
    assert granted_payload["capability"] not in audit_text

    audit_path.unlink()
    unavailable_status, _, _, _ = asyncio.run(exercise(credential_bound=False))
    assert unavailable_status == 503


def test_run_broker_writes_one_terminal_for_completed_expert(
    monkeypatch,
    tmp_path,
):
    import json

    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_ENABLED", "1")
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_PATH", str(audit_path))
    broker = RunBroker(dispatch_agent=lambda _request: "ok")
    request = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_private",
        content="secret prompt",
        idempotency_key="same-content-is-not-terminal-id",
        metadata={"expert_id": "resource-delivery"},
    )

    result = asyncio.run(broker.run(request))

    assert result.content == "ok"
    rows = [json.loads(line) for line in audit_path.read_text("utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["terminal_status"] == "completed"
    assert rows[0]["expert_resolution"] == "resolved"
    assert rows[0]["answer_completed"] is True
    assert "ou_private" not in json.dumps(rows[0])
    assert "secret prompt" not in json.dumps(rows[0])


def test_run_broker_writes_rejected_terminal_for_unavailable_expert(
    monkeypatch,
    tmp_path,
):
    import json

    from hermes_multitenancy.agent_real import ExpertUnavailableError
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_ENABLED", "1")
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_PATH", str(audit_path))

    def reject(_request):
        raise ExpertUnavailableError()

    broker = RunBroker(dispatch_agent=reject)
    request = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_private",
        content="hi",
        metadata={"expert_id": "missing"},
    )

    with pytest.raises(ExpertUnavailableError, match="EXPERT_UNAVAILABLE"):
        asyncio.run(broker.run(request))

    row = json.loads(audit_path.read_text("utf-8"))
    assert row["terminal_status"] == "rejected"
    assert row["failure_subsystem"] == "expert_resolution"
    assert row["error_code"] == "EXPERT_UNAVAILABLE"
    assert row["retryable"] is False
    assert row["answer_completed"] is False


def test_run_broker_terminal_marks_generic_retry_and_uses_unique_execution_ids(
    monkeypatch,
    tmp_path,
):
    import json

    from hermes_multitenancy.run_broker import RunBroker, mark_current_run_retried
    from hermes_multitenancy.run_models import RunRequest

    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_ENABLED", "1")
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_PATH", str(audit_path))

    def dispatch(_request):
        mark_current_run_retried()
        return "ok"

    broker = RunBroker(dispatch_agent=dispatch)
    request = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="owner",
        content="same",
    )

    asyncio.run(broker.run(request))
    asyncio.run(broker.run(request))

    rows = [json.loads(line) for line in audit_path.read_text("utf-8").splitlines()]
    assert len(rows) == 2
    assert len({row["terminal_event_id"] for row in rows}) == 2
    terminal_id_parts = [row["terminal_event_id"].split(".") for row in rows]
    assert all(len(parts) == 2 for parts in terminal_id_parts)
    assert terminal_id_parts[0][1] == terminal_id_parts[1][1]
    assert "owner" not in " ".join(row["terminal_event_id"] for row in rows)
    assert all(row["retried"] is True for row in rows)
    assert all(row["expert_requested"] is False for row in rows)
    assert all(row["expert_id"] is None for row in rows)
    assert all(row["expert_resolution"] == "not_requested" for row in rows)


def test_run_broker_terminal_marks_completed_expert_retry(
    monkeypatch,
    tmp_path,
):
    import json

    from hermes_multitenancy.run_broker import RunBroker, mark_current_run_retried
    from hermes_multitenancy.run_models import RunRequest

    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_ENABLED", "1")
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_PATH", str(audit_path))

    def dispatch(_request):
        mark_current_run_retried()
        return "ok"

    broker = RunBroker(dispatch_agent=dispatch)
    asyncio.run(
        broker.run(
            RunRequest(
                channel="webui",
                profile_name="owner",
                user_key="owner",
                content="hi",
                metadata={"expert_id": "resource-delivery"},
            )
        )
    )

    row = json.loads(audit_path.read_text("utf-8"))
    assert row["expert_resolution"] == "resolved"
    assert row["terminal_status"] == "completed"
    assert row["retried"] is True
    assert row["answer_completed"] is True


def test_run_broker_terminal_audit_failure_does_not_change_result(
    monkeypatch,
):
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_ENABLED", "1")
    monkeypatch.setattr(
        "hermes_multitenancy.conversation_audit.append_run_terminal_event",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("audit unavailable")),
    )
    broker = RunBroker(dispatch_agent=lambda _request: "ok")

    result = asyncio.run(
        broker.run(
            RunRequest(
                channel="webui",
                profile_name="owner",
                user_key="owner",
                content="hi",
            )
        )
    )

    assert result.content == "ok"


def test_run_broker_consumes_structured_failure_fields_without_text_parsing(
    monkeypatch,
    tmp_path,
):
    import json

    from hermes_multitenancy.run_broker import (
        RunBroker,
        record_current_run_failure,
    )
    from hermes_multitenancy.run_models import RunRequest

    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_ENABLED", "1")
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_PATH", str(audit_path))

    def dispatch(_request):
        record_current_run_failure(
            failure_subsystem="lark_api",
            error_code="FEISHU_RATE_LIMITED",
            retryable=True,
        )
        raise RuntimeError("opaque")

    broker = RunBroker(dispatch_agent=dispatch)
    with pytest.raises(RuntimeError, match="opaque"):
        asyncio.run(
            broker.run(
                RunRequest(
                    channel="webui",
                    profile_name="owner",
                    user_key="owner",
                    content="hi",
                )
            )
        )

    row = json.loads(audit_path.read_text("utf-8"))
    assert row["failure_subsystem"] == "lark_api"
    assert row["error_code"] == "FEISHU_RATE_LIMITED"
    assert row["retryable"] is True


def test_run_broker_does_not_promote_recovered_tool_failure_to_run_failure(
    monkeypatch,
    tmp_path,
):
    import json

    from hermes_multitenancy.run_broker import (
        RunBroker,
        record_current_run_failure,
    )
    from hermes_multitenancy.run_models import RunRequest

    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_ENABLED", "1")
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_PATH", str(audit_path))

    def dispatch(_request):
        record_current_run_failure(
            failure_subsystem="lark_api",
            error_code="FEISHU_RATE_LIMITED",
            retryable=True,
        )
        return "recovered answer"

    result = asyncio.run(
        RunBroker(dispatch_agent=dispatch).run(
            RunRequest(
                channel="webui",
                profile_name="owner",
                user_key="owner",
                content="hi",
            )
        )
    )

    row = json.loads(audit_path.read_text("utf-8"))
    assert result.content == "recovered answer"
    assert row["terminal_status"] == "completed"
    assert row["error_code"] is None
    assert row["failure_subsystem"] is None
    assert row["answer_completed"] is True


def test_run_broker_cancelled_execution_writes_exactly_one_terminal(
    monkeypatch,
    tmp_path,
):
    import json

    from hermes_multitenancy.run_broker import RunBroker, _issue_admitted
    from hermes_multitenancy.run_models import RunRequest

    async def run():
        audit_path = tmp_path / "audit.jsonl"
        monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_ENABLED", "1")
        monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_PATH", str(audit_path))
        started = asyncio.Event()

        async def dispatch(_request):
            started.set()
            await asyncio.Event().wait()

        broker = RunBroker(dispatch_agent=dispatch)
        request = RunRequest(
            channel="webui",
            profile_name="owner",
            user_key="owner",
            content="hi",
        )
        admitted = _issue_admitted(request, authority=broker)
        bound_terminal_id = admitted.terminal_event_id
        task = asyncio.create_task(broker._run_admitted(admitted))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        rows = [json.loads(line) for line in audit_path.read_text("utf-8").splitlines()]
        assert len(rows) == 1
        assert rows[0]["terminal_event_id"] == bound_terminal_id
        assert rows[0]["terminal_status"] == "cancelled"
        assert rows[0]["failure_subsystem"] == "runtime"
        assert rows[0]["error_code"] == "RUN_CANCELLED"
        assert rows[0]["answer_completed"] is False

    asyncio.run(run())


def test_run_broker_stable_execution_writes_terminal_after_waiter_disconnect(
    monkeypatch,
    tmp_path,
):
    import json

    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    async def run():
        audit_path = tmp_path / "audit.jsonl"
        monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_ENABLED", "1")
        monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_PATH", str(audit_path))
        started = asyncio.Event()
        release = asyncio.Event()

        async def dispatch(_request):
            started.set()
            await release.wait()
            return "ok"

        broker = RunBroker(dispatch_agent=dispatch)
        waiter = asyncio.create_task(
            broker.run(
                RunRequest(
                    channel="webui",
                    profile_name="owner",
                    user_key="owner",
                    content="hi",
                )
            )
        )
        await started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        for _ in range(100):
            if audit_path.exists():
                break
            await asyncio.sleep(0)
        rows = [json.loads(line) for line in audit_path.read_text("utf-8").splitlines()]
        assert len(rows) == 1
        assert rows[0]["terminal_status"] == "completed"

    asyncio.run(run())


def test_partial_stream_failure_writes_failed_output_terminal(
    monkeypatch,
    tmp_path,
):
    import json
    from types import SimpleNamespace

    from hermes_multitenancy import agent_real
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_ENABLED", "1")
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_PATH", str(audit_path))
    monkeypatch.setattr(
        "hermes_multitenancy.agent_real._core._resolve_explicit_expert_for_execution",
        lambda *_args: None,
    )

    async def partial(*_args, **_kwargs):
        yield "content", "partial"
        raise RuntimeError("provider failed")

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", partial)

    async def dispatch(_request):
        event = SimpleNamespace(text="hi", raw_event={"metadata": {}})
        async for _item in agent_real.stream_run_agent(event, tmp_path):
            pass
        return ""

    broker = RunBroker(dispatch_agent=dispatch)
    asyncio.run(
        broker.run(
            RunRequest(
                channel="webui",
                profile_name="owner",
                user_key="owner",
                content="hi",
            )
        )
    )

    row = json.loads(audit_path.read_text("utf-8"))
    assert row["terminal_status"] == "failed"
    assert row["failure_subsystem"] == "output"
    assert row["error_code"] == "OUTPUT_INCOMPLETE"
    assert row["answer_completed"] is False


def test_run_request_builds_stable_idempotency_key():
    from hermes_multitenancy.run_models import RunRequest

    request = RunRequest(
        channel="feishu",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        message_id="om_1",
    )

    assert request.effective_idempotency_key == "feishu:owner:ou_1:om_1"


def test_run_broker_rejects_host_tool_run_without_sandbox():
    from hermes_multitenancy.run_broker import RunBroker, RunRejected
    from hermes_multitenancy.run_models import RunRequest

    async def dispatch(_request):
        return "should not run"

    broker = RunBroker(
        dispatch_agent=dispatch,
        sandbox_available=lambda: False,
    )
    request = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        requires_host_tools=True,
    )

    with pytest.raises(RunRejected, match="sandbox"):
        asyncio.run(broker.run(request))


def test_run_broker_dedupes_before_dispatch():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    calls = []

    async def dispatch(request):
        calls.append(request.content)
        return "ok"

    seen = set()

    def mark_seen(request):
        key = request.effective_idempotency_key
        if key in seen:
            return False
        seen.add(key)
        return True

    broker = RunBroker(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        sandbox_available=lambda: True,
    )
    request = RunRequest(
        channel="feishu",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        message_id="om_1",
    )

    first = asyncio.run(broker.run(request))
    second = asyncio.run(broker.run(request))

    assert first.content == "ok"
    assert first.duplicate is False
    assert second.content == ""
    assert second.duplicate is True
    assert calls == ["hi"]


def test_run_broker_skips_preparation_for_known_duplicate():
    from dataclasses import replace

    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    prepared = []
    dispatched = []

    async def prepare(request):
        prepared.append(request.message_id)
        return replace(request, metadata={"litellm_billing_user_id": "user-1"})

    async def dispatch(request):
        dispatched.append(request.metadata)
        return "ok"

    broker = RunBroker(
        dispatch_agent=dispatch,
        prepare_request=prepare,
        is_seen=lambda request: request.message_id == "duplicate",
        mark_seen=lambda request: request.message_id != "duplicate",
    )
    duplicate = RunRequest(
        channel="feishu", profile_name="owner", user_key="ou_1",
        content="hi", message_id="duplicate",
    )
    fresh = replace(duplicate, message_id="fresh")

    asyncio.run(broker.run(duplicate))
    asyncio.run(broker.run(fresh))

    assert prepared == ["fresh"]
    assert dispatched == [{"litellm_billing_user_id": "user-1"}]


def test_run_broker_prepare_failure_does_not_consume_idempotency_key():
    from dataclasses import replace

    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    prepare_attempts = 0
    seen = set()
    dispatched = []

    async def prepare(request):
        nonlocal prepare_attempts
        prepare_attempts += 1
        if prepare_attempts == 1:
            raise RuntimeError("temporary billing lookup failure")
        return replace(request, metadata={"litellm_billing_user_id": "user-1"})

    def mark_seen(request):
        key = request.effective_idempotency_key
        if key in seen:
            return False
        seen.add(key)
        return True

    async def dispatch(request):
        dispatched.append(request.metadata)
        return "ok"

    broker = RunBroker(
        dispatch_agent=dispatch,
        prepare_request=prepare,
        is_seen=lambda request: request.effective_idempotency_key in seen,
        mark_seen=mark_seen,
        sandbox_available=lambda: True,
    )
    request = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        idempotency_key="webui:session-1:turn-1",
    )

    with pytest.raises(RuntimeError, match="temporary billing lookup failure"):
        asyncio.run(broker.run(request))
    retry = asyncio.run(broker.run(request))
    duplicate = asyncio.run(broker.run(request))

    assert retry.content == "ok"
    assert duplicate.duplicate is True
    assert prepare_attempts == 2
    assert len(seen) == 1
    assert dispatched == [{"litellm_billing_user_id": "user-1"}]


def test_run_broker_public_admit_prepares_before_consuming_idempotency():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    prepare_attempts = 0
    marked = []

    async def prepare(request):
        nonlocal prepare_attempts
        prepare_attempts += 1
        if prepare_attempts == 1:
            raise RuntimeError("temporary billing lookup failure")
        return request

    broker = RunBroker(
        dispatch_agent=lambda _request: "should not run",
        prepare_request=prepare,
        mark_seen=lambda request: marked.append(request.effective_idempotency_key) or True,
        sandbox_available=lambda: True,
    )
    request = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        idempotency_key="webui:session-1:turn-1",
    )

    with pytest.raises(RuntimeError, match="temporary billing lookup failure"):
        asyncio.run(broker.admit(request))
    admitted = asyncio.run(broker.admit(request))

    assert admitted.duplicate is False
    assert marked == ["webui:session-1:turn-1"]


def test_run_broker_policy_check_has_no_prepare_or_idempotency_side_effects():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    broker = RunBroker(
        dispatch_agent=lambda _request: "should not run",
        prepare_request=lambda _request: (_ for _ in ()).throw(AssertionError("must not prepare")),
        mark_seen=lambda _request: (_ for _ in ()).throw(AssertionError("must not mark")),
        sandbox_available=lambda: True,
    )
    request = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
    )

    assert broker.check_policy(request) == request


def test_run_broker_rejects_policy_before_preparing_request():
    from hermes_multitenancy.run_broker import RunBroker, RunRejected
    from hermes_multitenancy.run_models import RunRequest

    prepared = []

    async def prepare(request):
        prepared.append(request)
        return request

    broker = RunBroker(
        dispatch_agent=lambda _request: "should not run",
        prepare_request=prepare,
        sandbox_available=lambda: False,
    )
    request = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        requires_host_tools=True,
    )

    with pytest.raises(RunRejected, match="sandbox"):
        asyncio.run(broker.run(request))

    assert prepared == []


def test_router_mark_seen_dedupes_webui_with_explicit_idempotency_key(tmp_path):
    from hermes_multitenancy import router
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest
    from hermes_multitenancy.sessions import SessionStore

    router.override_session_store(SessionStore(tmp_path / "sessions.db"))
    calls = []
    prepares = []

    async def prepare(request):
        prepares.append(request.content)
        return request

    async def dispatch(request):
        calls.append(request.content)
        return "ok"

    broker = RunBroker(
        dispatch_agent=dispatch,
        prepare_request=prepare,
        is_seen=router._is_run_request_seen,
        mark_seen=router._mark_run_request_seen,
        sandbox_available=lambda: True,
    )

    first = asyncio.run(broker.run(RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content="first turn content with enough words to avoid short-content exemptions",
        idempotency_key="webui:session-1:turn-1",
    )))
    second = asyncio.run(broker.run(RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content="retry payload may differ but the explicit turn key is the same",
        idempotency_key="webui:session-1:turn-1",
    )))

    router.override_session_store(None)

    assert first.duplicate is False
    assert second.duplicate is True
    assert prepares == ["first turn content with enough words to avoid short-content exemptions"]
    assert calls == ["first turn content with enough words to avoid short-content exemptions"]


def test_router_mark_seen_propagates_session_store_failure():
    from hermes_multitenancy import router
    from hermes_multitenancy.run_models import RunRequest

    class FailingStore:
        def mark_event_processed(self, *_args, **_kwargs):
            raise RuntimeError("session store write failed")

    router.override_session_store(FailingStore())
    try:
        with pytest.raises(RuntimeError, match="session store write failed"):
            router._mark_run_request_seen(RunRequest(
                channel="feishu",
                profile_name="owner",
                user_key="ou_1",
                content="hi",
                message_id="om_store_failure",
            ))
    finally:
        router.override_session_store(None)


def test_router_canonical_dedupe_fails_closed_without_session_store(monkeypatch):
    from hermes_multitenancy import router
    from hermes_multitenancy.run_models import RunRequest

    monkeypatch.setattr(router, "_get_session_store", lambda: None)
    canonical = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        idempotency_key="webui:missing-store",
    )

    with pytest.raises(RuntimeError, match="SessionStore unavailable"):
        router._is_run_request_seen(canonical)
    with pytest.raises(RuntimeError, match="SessionStore unavailable"):
        router._mark_run_request_seen(canonical)

    unkeyed_webui = replace(canonical, idempotency_key=None)
    assert router._is_run_request_seen(unkeyed_webui) is False
    assert router._mark_run_request_seen(unkeyed_webui) is True


def test_router_mark_seen_does_not_content_dedupe_webui_without_explicit_key(tmp_path):
    from hermes_multitenancy import router
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest
    from hermes_multitenancy.sessions import SessionStore

    router.override_session_store(SessionStore(tmp_path / "sessions.db"))
    calls = []

    async def dispatch(request):
        calls.append(request.content)
        return "ok"

    broker = RunBroker(
        dispatch_agent=dispatch,
        mark_seen=router._mark_run_request_seen,
        sandbox_available=lambda: True,
    )
    content = "same webui prompt that is long enough to trigger the old content hash fallback"

    first = asyncio.run(broker.run(RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content=content,
    )))
    second = asyncio.run(broker.run(RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content=content,
    )))

    router.override_session_store(None)

    assert first.duplicate is False
    assert second.duplicate is False
    assert calls == [content, content]


def test_router_mark_seen_keeps_content_dedupe_for_non_webui_without_explicit_key(tmp_path):
    from hermes_multitenancy import router
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest
    from hermes_multitenancy.sessions import SessionStore

    router.override_session_store(SessionStore(tmp_path / "sessions.db"))
    calls = []

    async def dispatch(request):
        calls.append(request.content)
        return "ok"

    broker = RunBroker(
        dispatch_agent=dispatch,
        mark_seen=router._mark_run_request_seen,
        sandbox_available=lambda: True,
    )
    content = "same feishu prompt that remains protected by content hash fallback dedupe"

    first = asyncio.run(broker.run(RunRequest(
        channel="feishu",
        profile_name="owner",
        user_key="ou_1",
        content=content,
    )))
    second = asyncio.run(broker.run(RunRequest(
        channel="feishu",
        profile_name="owner",
        user_key="ou_1",
        content=content,
    )))

    router.override_session_store(None)

    assert first.duplicate is False
    assert second.duplicate is True
    assert calls == [content]


def test_run_broker_admit_prepared_runs_policy_and_dedupe_without_dispatch():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    calls = []
    seen = set()

    async def dispatch(request):
        calls.append(request.content)
        return "ok"

    def mark_seen(request):
        key = request.effective_idempotency_key
        if key in seen:
            return False
        seen.add(key)
        return True

    broker = RunBroker(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        sandbox_available=lambda: True,
    )
    request = RunRequest(
        channel="feishu",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        message_id="om_1",
    )

    prepared = asyncio.run(broker.prepare(request))
    first = asyncio.run(broker.admit_prepared(prepared))
    second = asyncio.run(broker.admit_prepared(prepared))

    assert first.duplicate is False
    assert second.duplicate is True
    assert calls == []


def test_run_broker_admit_prepared_rejects_raw_request():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    broker = RunBroker(
        dispatch_agent=lambda _request: "should not run",
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )
    request = RunRequest(
        channel="feishu",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        message_id="om_1",
    )

    with pytest.raises(TypeError, match="PreparedRun"):
        asyncio.run(broker.admit_prepared(request))


def test_run_broker_admit_prepared_rejects_different_prepare_boundary():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    async def prepare_a(request):
        return request

    async def prepare_b(request):
        return request

    issuer = RunBroker(dispatch_agent=lambda _request: "", prepare_request=prepare_a)
    admission = RunBroker(
        dispatch_agent=lambda _request: "",
        prepare_request=prepare_b,
        mark_seen=lambda _request: True,
    )
    request = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        idempotency_key="turn-1",
    )
    prepared = asyncio.run(issuer.prepare(request))

    with pytest.raises(TypeError, match="different preparation boundary"):
        asyncio.run(admission.admit_prepared(prepared))


def test_run_broker_prepared_capability_dispatches_once():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    calls = []

    async def dispatch(request):
        calls.append(("dispatch", request.content))
        return "ok"

    broker = RunBroker(
        dispatch_agent=dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    async def exercise():
        prepared = await broker.prepare(
            RunRequest(
            channel="feishu",
            profile_name="owner",
            user_key="ou_1",
            content="hi",
            message_id="om_1",
            )
        )
        first = await broker.run_prepared(prepared)
        second = await broker.run_prepared(prepared)
        return first, second

    result, duplicate = asyncio.run(exercise())

    assert result.content == "ok"
    assert result.duplicate is False
    assert duplicate.duplicate is True
    assert calls == [("dispatch", "hi")]


def test_run_broker_run_prepared_rejects_raw_request():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    broker = RunBroker(
        dispatch_agent=lambda _request: "must not run",
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )
    request = RunRequest(
        channel="feishu",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        message_id="om_1",
    )

    with pytest.raises(TypeError, match="PreparedRun"):
        asyncio.run(broker.run_prepared(request))


def test_run_broker_prepared_capability_rejects_cross_broker_dispatch():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    calls = []
    issuer = RunBroker(
        dispatch_agent=lambda request: calls.append(request.content) or "ok",
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )
    other = RunBroker(
        dispatch_agent=lambda request: calls.append(request.content) or "wrong",
        sandbox_available=lambda: True,
    )

    async def exercise():
        prepared = await issuer.prepare(RunRequest(
            channel="feishu",
            profile_name="owner",
            user_key="ou_1",
            content="hi",
            message_id="om_1",
        ))
        with pytest.raises(TypeError, match="different preparation boundary"):
            await other.run_prepared(prepared)
        return await issuer.run_prepared(prepared)

    result = asyncio.run(exercise())

    assert result.content == "ok"
    assert calls == ["hi"]


def test_run_broker_concurrent_duplicates_share_one_prepare_across_brokers():
    from dataclasses import replace

    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    prepare_calls = 0
    dispatches = []
    seen = set()

    async def prepare(request):
        nonlocal prepare_calls
        prepare_calls += 1
        await asyncio.sleep(0.01)
        return replace(request, metadata={"litellm_billing_user_id": "user-1"})

    def is_seen(request):
        return request.effective_idempotency_key in seen

    def mark_seen(request):
        key = request.effective_idempotency_key
        if key in seen:
            return False
        seen.add(key)
        return True

    async def dispatch(request):
        dispatches.append(request.metadata)
        return "ok"

    request = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        idempotency_key="turn-1",
    )

    async def exercise():
        brokers = [
            RunBroker(
                dispatch_agent=dispatch,
                prepare_request=prepare,
                is_seen=is_seen,
                mark_seen=mark_seen,
                sandbox_available=lambda: True,
            )
            for _ in range(2)
        ]
        return await asyncio.gather(*(broker.run(request) for broker in brokers))

    results = asyncio.run(exercise())

    assert prepare_calls == 1
    assert dispatches == [{"litellm_billing_user_id": "user-1"}]
    assert sorted(result.duplicate for result in results) == [False, True]


def test_run_broker_mark_failure_reaches_all_waiters_and_allows_retry():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    prepare_started = asyncio.Event()
    release_prepare = asyncio.Event()
    prepare_calls = 0
    mark_calls = 0
    dispatches = []

    async def prepare(request):
        nonlocal prepare_calls
        prepare_calls += 1
        if prepare_calls == 1:
            prepare_started.set()
            await release_prepare.wait()
        return request

    def mark_seen(_request):
        nonlocal mark_calls
        mark_calls += 1
        if mark_calls == 1:
            raise RuntimeError("temporary mark failure")
        return True

    request = RunRequest(
        channel="feishu",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        message_id="om_mark_failure",
    )

    async def exercise():
        brokers = [
            RunBroker(
                dispatch_agent=lambda req: dispatches.append(req.content) or "ok",
                prepare_request=prepare,
                mark_seen=mark_seen,
                sandbox_available=lambda: True,
            )
            for _ in range(2)
        ]
        async def execute(broker, admitted):
            return await broker._run_admitted(admitted)

        first = asyncio.create_task(brokers[0].prepare_and_execute(
            request,
            execute=lambda admitted: execute(brokers[0], admitted),
        ))
        await prepare_started.wait()
        second = asyncio.create_task(brokers[1].prepare_and_execute(
            request,
            execute=lambda admitted: execute(brokers[1], admitted),
        ))
        await asyncio.sleep(0)
        release_prepare.set()
        failures = await asyncio.gather(first, second, return_exceptions=True)

        result = await brokers[0].prepare_and_execute(
            request,
            execute=lambda admitted: execute(brokers[0], admitted),
        )
        return failures, result

    failures, result = asyncio.run(exercise())

    assert [str(failure) for failure in failures] == [
        "temporary mark failure",
        "temporary mark failure",
    ]
    assert all(isinstance(failure, RuntimeError) for failure in failures)
    assert prepare_calls == 2
    assert mark_calls == 2
    assert result.content == "ok"
    assert dispatches == ["hi"]


def test_run_broker_mark_task_cancel_still_has_stable_dispatch_owner():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    seen = set()
    dispatched = []

    def mark_seen(request):
        key = request.effective_idempotency_key
        if key in seen:
            return False
        seen.add(key)
        asyncio.current_task().cancel()
        return True

    broker = RunBroker(
        dispatch_agent=lambda request: dispatched.append(request.content) or "ok",
        mark_seen=mark_seen,
        sandbox_available=lambda: True,
    )
    request = RunRequest(
        channel="feishu",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        message_id="om_cancel_in_mark",
    )

    async def dispatch(request):
        dispatched.append(f"started:{request.content}")
        await asyncio.sleep(0)
        dispatched.append(f"completed:{request.content}")
        return "ok"

    async def exercise():
        return await broker.prepare_and_execute(
            request,
            execute=lambda admitted: broker._run_admitted(
                admitted,
                dispatch_agent=dispatch,
            ),
        )

    result = asyncio.run(exercise())

    duplicate = asyncio.run(broker.run(request))
    assert result.content == "ok"
    assert duplicate.duplicate is True
    assert dispatched == ["started:hi", "completed:hi"]


def test_run_broker_cancelled_internal_mark_task_still_dispatches_once_for_peers():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest, RunResult

    prepare_started = asyncio.Event()
    release_prepare = asyncio.Event()
    seen = set()
    dispatches = []

    async def prepare(request):
        prepare_started.set()
        await release_prepare.wait()
        return request

    def mark_seen(request):
        seen.add(request.effective_idempotency_key)
        asyncio.current_task().cancel()
        return True

    request = RunRequest(
        channel="feishu",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        message_id="om_cancel_leader",
    )

    async def exercise():
        brokers = [
            RunBroker(
                dispatch_agent=lambda req: dispatches.append(req.content) or "ok",
                prepare_request=prepare,
                mark_seen=mark_seen,
                sandbox_available=lambda: True,
            )
            for _ in range(2)
        ]

        async def run(broker):
            return await broker.prepare_and_execute(
                request,
                execute=lambda admitted: broker._run_admitted(admitted),
            )

        first = asyncio.create_task(run(brokers[0]))
        await prepare_started.wait()
        second = asyncio.create_task(run(brokers[1]))
        await asyncio.sleep(0)
        release_prepare.set()
        return await asyncio.gather(first, second, return_exceptions=True)

    results = asyncio.run(exercise())

    completed = [result for result in results if isinstance(result, RunResult)]
    assert len(completed) == 2
    assert sorted(result.duplicate for result in completed) == [False, True]
    assert [result.content for result in completed if not result.duplicate] == ["ok"]
    assert dispatches == ["hi"]


def test_run_broker_outer_cancel_after_mark_does_not_orphan_dispatch():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()
    dispatches = []
    seen = set()

    def mark_seen(request):
        key = request.effective_idempotency_key
        if key in seen:
            return False
        seen.add(key)
        return True

    async def dispatch(request):
        dispatches.append(f"started:{request.content}")
        dispatch_started.set()
        await release_dispatch.wait()
        dispatches.append(f"completed:{request.content}")
        return "ok"

    broker = RunBroker(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        sandbox_available=lambda: True,
    )
    request = RunRequest(
        channel="feishu",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        message_id="om_outer_cancel_after_mark",
    )

    async def exercise():
        waiter = asyncio.create_task(broker.run(request))
        await dispatch_started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release_dispatch.set()
        for _ in range(10):
            if dispatches == ["started:hi", "completed:hi"]:
                break
            await asyncio.sleep(0)
        for _ in range(10):
            await asyncio.sleep(0)
        return await broker.run(request)

    duplicate = asyncio.run(exercise())

    assert dispatches == ["started:hi", "completed:hi"]
    assert duplicate.duplicate is True


def test_run_broker_on_abandon_cleans_staged_resource_before_execution_owner():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    staged = []
    abandoned = []
    entry_done = []
    entry_completion_owned = asyncio.Event()
    executed = []
    broker = RunBroker(
        dispatch_agent=lambda _request: "unused",
        mark_seen=lambda _request: False,
        sandbox_available=lambda: True,
    )
    request = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="owner",
        content="stage then duplicate",
        idempotency_key="abandon-staged",
    )

    def transform(prepared):
        staged.append(prepared.request.effective_idempotency_key)
        return prepared.request

    async def exercise():
        return await broker.prepare_and_execute(
            request,
            execute=lambda _admitted: executed.append(True),
            transform_request=transform,
            on_abandon=lambda: abandoned.append("cleanup"),
            entry_completion_owned=entry_completion_owned,
            on_entry_done=lambda failed: entry_done.append(failed),
        )

    result = asyncio.run(exercise())

    assert result.duplicate is True
    assert staged == [request.effective_idempotency_key]
    assert abandoned == ["cleanup"]
    assert entry_completion_owned.is_set()
    assert entry_done == [False]
    assert executed == []


def test_run_broker_on_abandon_transfers_to_stable_execution_owner():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest, RunResult

    abandoned = []
    execution_owned = asyncio.Event()
    broker = RunBroker(
        dispatch_agent=lambda _request: "unused",
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    async def exercise():
        return await broker.prepare_and_execute(
            RunRequest(
                channel="webui",
                profile_name="owner",
                user_key="owner",
                content="execute staged",
                idempotency_key="own-staged",
            ),
            execute=lambda _admitted: RunResult(content="accepted", duplicate=False),
            execution_owned=execution_owned,
            on_abandon=lambda: abandoned.append("cleanup"),
        )

    result = asyncio.run(exercise())

    assert result.content == "accepted"
    assert execution_owned.is_set()
    assert abandoned == []


def test_run_broker_shared_entry_ownership_rolls_back_when_task_creation_fails():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest, RunResult

    shared_entry_owned = asyncio.Event()
    abandoned = []
    broker = RunBroker(
        dispatch_agent=lambda _request: "unused",
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    async def exercise():
        loop = asyncio.get_running_loop()
        real_create_task = loop.create_task

        def fail_shared_task(coro, *args, **kwargs):
            if getattr(getattr(coro, "cr_code", None), "co_name", "") == (
                "_prepare_admit_execute_once"
            ):
                coro.close()
                raise RuntimeError("shared task creation failed")
            return real_create_task(coro, *args, **kwargs)

        loop.create_task = fail_shared_task
        try:
            with pytest.raises(RuntimeError, match="shared task creation failed"):
                await broker.prepare_and_execute(
                    RunRequest(
                        channel="webui",
                        profile_name="owner",
                        user_key="owner",
                        content="task creation failure",
                        idempotency_key="shared-create-failure",
                    ),
                    execute=lambda _admitted: RunResult(
                        content="unexpected",
                        duplicate=False,
                    ),
                    shared_entry_owned=shared_entry_owned,
                    on_abandon=lambda: abandoned.append("cleanup"),
                )
        finally:
            loop.create_task = real_create_task

    asyncio.run(exercise())

    assert not shared_entry_owned.is_set()
    assert abandoned == []


def test_run_broker_shared_entry_finalizer_abandons_when_cancelled_before_first_step():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest, RunResult

    shared_entry_owned = asyncio.Event()
    entry_completion_owned = asyncio.Event()
    abandoned = []
    entry_done = []
    marked = []
    broker = RunBroker(
        dispatch_agent=lambda _request: "unused",
        mark_seen=lambda request: marked.append(
            request.effective_idempotency_key
        )
        or True,
        sandbox_available=lambda: True,
    )

    async def exercise():
        loop = asyncio.get_running_loop()
        real_create_task = loop.create_task

        def cancel_shared_task(coro, *args, **kwargs):
            task = real_create_task(coro, *args, **kwargs)
            if getattr(getattr(coro, "cr_code", None), "co_name", "") == (
                "_prepare_admit_execute_once"
            ):
                task.cancel()
            return task

        loop.create_task = cancel_shared_task
        try:
            with pytest.raises(asyncio.CancelledError):
                await broker.prepare_and_execute(
                    RunRequest(
                        channel="webui",
                        profile_name="owner",
                        user_key="owner",
                        content="cancel shared before first step",
                        idempotency_key="shared-cancel-before-step",
                    ),
                    execute=lambda _admitted: RunResult(
                        content="unexpected",
                        duplicate=False,
                    ),
                    shared_entry_owned=shared_entry_owned,
                    on_abandon=lambda: abandoned.append("cleanup"),
                    entry_completion_owned=entry_completion_owned,
                    on_entry_done=lambda failed: entry_done.append(failed),
                )
            for _ in range(10):
                if entry_done:
                    break
                await asyncio.sleep(0)
        finally:
            loop.create_task = real_create_task

    asyncio.run(exercise())

    assert shared_entry_owned.is_set()
    assert entry_completion_owned.is_set()
    assert abandoned == ["cleanup"]
    assert entry_done == [True]
    assert marked == []


def test_run_broker_post_mark_redelivery_inherits_shared_entry_completion_owner():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest, RunResult

    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()
    first_owned = asyncio.Event()
    retry_owned = asyncio.Event()
    first_completed = asyncio.Event()
    marked = []
    first_completions = []
    retry_completions = []
    broker = RunBroker(
        dispatch_agent=lambda _request: "unused",
        mark_seen=lambda request: marked.append(request.effective_idempotency_key) or True,
        sandbox_available=lambda: True,
    )
    request = RunRequest(
        channel="feishu",
        profile_name="owner",
        user_key="ou_owner",
        content="post mark retry",
        message_id="om_post_mark_retry",
    )

    async def execute(_admitted):
        dispatch_started.set()
        await release_dispatch.wait()
        return RunResult(content="ok", duplicate=False)

    async def complete_first(failed):
        first_completions.append(failed)
        first_completed.set()

    async def exercise():
        leader = asyncio.create_task(
            broker.prepare_and_execute(
                request,
                execute=execute,
                entry_completion_owned=first_owned,
                on_entry_done=complete_first,
            )
        )
        await dispatch_started.wait()
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader

        retry = await broker.prepare_and_execute(
            request,
            execute=execute,
            entry_completion_owned=retry_owned,
            on_entry_done=lambda failed: retry_completions.append(failed),
        )
        release_dispatch.set()
        await asyncio.wait_for(first_completed.wait(), timeout=SYNC_TIMEOUT)
        for _ in range(10):
            await asyncio.sleep(0)
        return retry

    result = asyncio.run(exercise())

    assert result.duplicate is True
    assert first_owned.is_set()
    assert retry_owned.is_set()
    assert first_completions == [False]
    assert retry_completions == []
    assert len(marked) == 1


def test_run_broker_rejects_cancelled_entry_finalizer_before_transferring_ownership():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest, RunResult

    completion_owned = asyncio.Event()
    completions = []
    marked = []
    broker = RunBroker(
        dispatch_agent=lambda _request: "unused",
        mark_seen=lambda request: marked.append(request.effective_idempotency_key) or True,
        sandbox_available=lambda: True,
    )

    async def exercise():
        loop = asyncio.get_running_loop()
        real_create_task = loop.create_task

        def cancel_finalizer_task(coro, *args, **kwargs):
            task = real_create_task(coro, *args, **kwargs)
            if getattr(getattr(coro, "cr_code", None), "co_name", "") == (
                "_run_entry_done"
            ):
                task.cancel()
            return task

        loop.create_task = cancel_finalizer_task
        try:
            with pytest.raises(RuntimeError, match="shared entry finalizer task unavailable"):
                await broker.prepare_and_execute(
                    RunRequest(
                        channel="feishu",
                        profile_name="owner",
                        user_key="ou_owner",
                        content="cancel finalizer before first step",
                        message_id="om_cancel_finalizer",
                    ),
                    execute=lambda _admitted: RunResult(
                        content="unexpected",
                        duplicate=False,
                    ),
                    entry_completion_owned=completion_owned,
                    on_entry_done=lambda failed: completions.append(failed),
                )
            await asyncio.sleep(0)
        finally:
            loop.create_task = real_create_task

    asyncio.run(exercise())

    assert not completion_owned.is_set()
    assert completions == []
    assert marked == []


def test_run_broker_stable_task_cancel_before_first_step_does_not_consume_key(
    monkeypatch,
):
    from hermes_multitenancy import run_broker as broker_mod
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest, RunResult

    marked = []
    executed = []
    finalized = []
    abandoned = []
    real_create_task = broker_mod.asyncio.create_task

    def cancel_stable_task(coro):
        task = real_create_task(coro)
        if getattr(getattr(coro, "cr_code", None), "co_name", "") == (
            "_run_after_admission"
        ):
            task.cancel()
        return task

    monkeypatch.setattr(broker_mod.asyncio, "create_task", cancel_stable_task)
    broker = RunBroker(
        dispatch_agent=lambda _request: "unused",
        mark_seen=lambda request: marked.append(
            request.effective_idempotency_key
        )
        or True,
        sandbox_available=lambda: True,
    )

    async def execute(_admitted):
        executed.append(True)
        return RunResult(content="unexpected", duplicate=False)

    async def exercise():
        with pytest.raises(RuntimeError, match="stable execution task unavailable"):
            await broker.prepare_and_execute(
                RunRequest(
                    channel="webui",
                    profile_name="owner",
                    user_key="owner",
                    content="cancel before first step",
                    idempotency_key="cancel-stable-before-step",
                ),
                execute=execute,
                on_abandon=lambda: abandoned.append("cleanup"),
                on_execution_done=lambda: finalized.append("cleanup"),
            )
        await asyncio.sleep(0)

    asyncio.run(exercise())

    assert marked == []
    assert executed == []
    assert abandoned == ["cleanup"]
    assert finalized == []


def test_run_broker_stable_task_creation_failure_is_retryable(monkeypatch):
    from hermes_multitenancy import run_broker as broker_mod
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest, RunResult

    marked = []
    dispatched = []
    abandoned = []
    finalized = []
    real_create_task = broker_mod.asyncio.create_task
    injected = False

    def fail_stable_task(coro):
        nonlocal injected
        if (
            not injected
            and getattr(getattr(coro, "cr_code", None), "co_name", "")
            == "_run_after_admission"
        ):
            injected = True
            raise RuntimeError("stable create failed")
        return real_create_task(coro)

    monkeypatch.setattr(broker_mod.asyncio, "create_task", fail_stable_task)
    broker = RunBroker(
        dispatch_agent=lambda _request: "unused",
        mark_seen=lambda request: marked.append(
            request.effective_idempotency_key
        )
        or True,
        sandbox_available=lambda: True,
    )
    request = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="owner",
        content="retry stable task creation",
        idempotency_key="retry-stable-create",
    )

    async def execute(_admitted):
        dispatched.append(True)
        return RunResult(content="accepted", duplicate=False)

    async def exercise():
        with pytest.raises(RuntimeError, match="stable create failed"):
            await broker.prepare_and_execute(
                request,
                execute=execute,
                on_abandon=lambda: abandoned.append("cleanup"),
                on_execution_done=lambda: finalized.append("cleanup"),
            )
        result = await broker.prepare_and_execute(
            request,
            execute=execute,
            on_abandon=lambda: abandoned.append("cleanup"),
            on_execution_done=lambda: finalized.append("cleanup"),
        )
        await asyncio.sleep(0)
        return result

    result = asyncio.run(exercise())

    assert result.content == "accepted"
    assert len(marked) == 1
    assert dispatched == [True]
    assert abandoned == ["cleanup"]
    assert finalized == ["cleanup"]


def test_run_prepared_task_creation_failure_does_not_consume_key(monkeypatch):
    from hermes_multitenancy import run_broker as broker_mod
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    marked = []
    dispatched = []
    real_create_task = broker_mod.asyncio.create_task
    injected = False

    def fail_stable_task(coro):
        nonlocal injected
        if (
            not injected
            and getattr(getattr(coro, "cr_code", None), "co_name", "")
            == "_run_after_admission"
        ):
            injected = True
            raise RuntimeError("stable create failed")
        return real_create_task(coro)

    monkeypatch.setattr(broker_mod.asyncio, "create_task", fail_stable_task)
    broker = RunBroker(
        dispatch_agent=lambda request: dispatched.append(request.content) or "ok",
        mark_seen=lambda request: marked.append(
            request.effective_idempotency_key
        )
        or True,
        sandbox_available=lambda: True,
    )
    request = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="owner",
        content="run prepared retry",
        idempotency_key="run-prepared-stable-create",
    )

    async def exercise():
        first = await broker.prepare(request)
        with pytest.raises(RuntimeError, match="stable create failed"):
            await broker.run_prepared(first)
        retry = await broker.prepare(request)
        return await broker.run_prepared(retry)

    result = asyncio.run(exercise())

    assert result.content == "ok"
    assert len(marked) == 1
    assert dispatched == ["run prepared retry"]


def test_run_admitted_does_not_recheck_policy_after_durable_admission():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    sandbox = {"available": True}
    dispatched = []
    broker = RunBroker(
        dispatch_agent=lambda request: dispatched.append(request.content) or "ok",
        mark_seen=lambda _request: True,
        sandbox_available=lambda: sandbox["available"],
    )

    async def exercise():
        async def execute(admitted):
            sandbox["available"] = False
            return await broker._run_admitted(admitted)

        return await broker.prepare_and_execute(
            RunRequest(
                channel="webui",
                profile_name="owner",
                user_key="ou_1",
                content="already admitted",
                idempotency_key="turn-policy",
                requires_host_tools=True,
            ),
            execute=execute,
        )

    result = asyncio.run(exercise())

    assert result.content == "ok"
    assert dispatched == ["already admitted"]


@pytest.mark.parametrize("cancel_stage", ["prepare", "transform", "before_admit"])
def test_run_broker_cancelled_last_waiter_does_not_consume_admission(cancel_stage):
    from dataclasses import replace

    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    stage_started = asyncio.Event()
    never_finish = asyncio.Event()
    prepare_calls = 0
    transform_calls = 0
    before_admit_calls = 0
    marked = []
    dispatched = []

    async def prepare(request):
        nonlocal prepare_calls
        prepare_calls += 1
        if cancel_stage == "prepare" and prepare_calls == 1:
            stage_started.set()
            await never_finish.wait()
        return request

    async def transform(prepared):
        nonlocal transform_calls
        transform_calls += 1
        if cancel_stage == "transform" and transform_calls == 1:
            stage_started.set()
            await never_finish.wait()
        return replace(prepared.request, content="enriched")

    async def before_admit(_prepared):
        nonlocal before_admit_calls
        before_admit_calls += 1
        if cancel_stage == "before_admit" and before_admit_calls == 1:
            stage_started.set()
            await never_finish.wait()

    broker = RunBroker(
        dispatch_agent=lambda request: dispatched.append(request.content) or "ok",
        prepare_request=prepare,
        mark_seen=lambda request: marked.append(request.effective_idempotency_key) or True,
        sandbox_available=lambda: True,
    )
    request = RunRequest(
        channel="feishu",
        profile_name="owner",
        user_key="ou_1",
        content="original",
        message_id="om_cancel",
    )

    async def exercise():
        cancelled = asyncio.create_task(
            broker.prepare_and_execute(
                request,
                execute=lambda admitted: broker._run_admitted(admitted),
                transform_request=transform,
                before_admit=before_admit,
            )
        )
        await stage_started.wait()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await asyncio.sleep(0)
        assert marked == []

        return await broker.prepare_and_execute(
            request,
            execute=lambda admitted: broker._run_admitted(admitted),
            transform_request=transform,
            before_admit=before_admit,
        )

    result = asyncio.run(exercise())

    assert result.content == "ok"
    assert prepare_calls == 2
    assert transform_calls == (1 if cancel_stage == "prepare" else 2)
    assert before_admit_calls == (1 if cancel_stage in {"prepare", "transform"} else 2)
    assert len(marked) == 1
    assert dispatched == ["enriched"]


def test_run_broker_emits_channel_neutral_events():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunEvent, RunRequest

    events = []

    async def dispatch(_request):
        return "hello"

    def emit(event: RunEvent):
        events.append((event.kind, event.text))

    broker = RunBroker(
        dispatch_agent=dispatch,
        emit_event=emit,
        sandbox_available=lambda: True,
    )

    result = asyncio.run(broker.run(
        RunRequest(
            channel="webui",
            profile_name="owner",
            user_key="ou_1",
            content="hi",
        )
    ))

    assert result.content == "hello"
    assert events == [
        ("content", "hello"),
        ("done", ""),
    ]


def test_run_broker_rewrites_hades_alias_before_dispatch(monkeypatch):
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    fake_skill_commands = SimpleNamespace(
        get_skill_commands=lambda: {"/kep-hades-cli": {"name": "kep-hades-cli"}},
        resolve_skill_command_key=lambda command: "/kep-hades-cli"
        if command.replace("_", "-") == "kep-hades-cli"
        else None,
        build_skill_invocation_message=lambda cmd_key, user_instruction, task_id=None: (
            f"[skill:{cmd_key} task:{task_id}] {user_instruction}"
        ),
    )
    monkeypatch.setitem(sys.modules, "agent", ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.skill_commands", fake_skill_commands)
    # the broker now scopes the skill loader per-profile before rewriting; stub
    # tools.skills_tool so _scope_profile_skill_loader can set SKILLS_DIR in any env
    # (the per-profile scope must establish, or the rewrite fails closed).
    fake_skills_tool = SimpleNamespace(HERMES_HOME=None, SKILLS_DIR=None)
    monkeypatch.setitem(sys.modules, "tools", ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.skills_tool", fake_skills_tool)

    seen = []

    async def dispatch(request):
        seen.append(request.content)
        return "ok"

    broker = RunBroker(dispatch_agent=dispatch, sandbox_available=lambda: True)

    result = asyncio.run(broker.run(
        RunRequest(
            channel="webui",
            profile_name="owner",
            user_key="owner",
            session_id="webui-session-1",
            content="/hades get 69df030c1f01cb45ba7ff585",
        )
    ))

    assert result.content == "ok"
    assert seen == [
        "[skill:/kep-hades-cli task:webui-session-1] get 69df030c1f01cb45ba7ff585"
    ]
