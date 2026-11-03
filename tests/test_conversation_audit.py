from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


def _event() -> SimpleNamespace:
    return SimpleNamespace(
        text="hello audit",
        message_id="om_test",
        source=SimpleNamespace(
            platform=SimpleNamespace(value="feishu"),
            chat_id="oc_test",
            chat_type="dm",
            user_id="ou_test",
            message_id="om_source",
        ),
    )


def test_append_conversation_audit_event_writes_messages_jsonl_shape(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from hermes_multitenancy.conversation_audit import append_conversation_audit_event

    audit_path = tmp_path / "conversation-audit.jsonl"
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_ENABLED", "1")
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_PATH", str(audit_path))

    append_conversation_audit_event(
        profile_name="owner",
        platform="feishu",
        chat_type="dm",
        session_id="agent:profile:owner:session",
        message_id=42,
        role="assistant",
        content="done",
        timestamp=1779964585.0,
        tool_name=None,
        tool_calls=None,
        finish_reason="stop",
        source="state_db_mirror",
    )

    rows = [json.loads(line) for line in audit_path.read_text("utf-8").splitlines()]
    assert rows == [
        {
            "@timestamp": "2026-05-28T18:36:25+08:00",
            "event_type": "conversation_message",
            "profile": "owner",
            "platform": "feishu",
            "chat_type": "dm",
            "session_id": "agent:profile:owner:session",
            "message_id": 42,
            "role": "assistant",
            "content": "done",
            "tool_name": None,
            "tool_calls": None,
            "finish_reason": "stop",
            "source": "state_db_mirror",
        }
    ]


def test_conversation_audit_disabled_does_not_create_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from hermes_multitenancy.conversation_audit import append_conversation_audit_event

    audit_path = tmp_path / "conversation-audit.jsonl"
    monkeypatch.delenv("HERMES_CONVERSATION_AUDIT_ENABLED", raising=False)
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_PATH", str(audit_path))

    append_conversation_audit_event(
        profile_name="owner",
        platform="feishu",
        chat_type="dm",
        session_id="s",
        message_id=1,
        role="user",
        content="hello",
        timestamp=1779964585.0,
    )

    assert not audit_path.exists()


def test_conversation_audit_failures_are_non_blocking(monkeypatch, tmp_path: Path) -> None:
    from hermes_multitenancy.conversation_audit import append_conversation_audit_event

    bad_parent = tmp_path / "not-a-directory"
    bad_parent.write_text("x", encoding="utf-8")
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_ENABLED", "1")
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_PATH", str(bad_parent / "audit.jsonl"))

    append_conversation_audit_event(
        profile_name="owner",
        platform="feishu",
        chat_type="dm",
        session_id="s",
        message_id=1,
        role="user",
        content="hello",
        timestamp=1779964585.0,
    )


def test_build_conversation_audit_context_reads_multitenancy_event() -> None:
    from hermes_multitenancy.conversation_audit import build_conversation_audit_context

    context = build_conversation_audit_context(_event(), Path("/home/agent/.hermes/profiles/owner"))

    assert context == {
        "profile_name": "owner",
        "platform": "feishu",
        "chat_type": "dm",
    }


def test_append_run_terminal_event_is_structured_and_redacted(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from hermes_multitenancy.conversation_audit import append_run_terminal_event

    audit_path = tmp_path / "conversation-audit.jsonl"
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_ENABLED", "1")
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_PATH", str(audit_path))

    append_run_terminal_event(
        terminal_event_id="run-1",
        profile_name="feishu_ou_private",
        platform="webui",
        chat_type="webui",
        expert_requested=True,
        expert_id="resource-delivery",
        expert_resolution="resolved",
        terminal_status="completed",
        error_code=None,
        failure_subsystem=None,
        retryable=False,
        retried=False,
        answer_completed=True,
        duration_ms=12,
    )

    row = json.loads(audit_path.read_text("utf-8"))
    assert row == {
        "@timestamp": row["@timestamp"],
        "event_type": "run_terminal",
        "schema_version": 1,
        "terminal_event_id": "run-1",
        "profile": row["profile"],
        "platform": "webui",
        "chat_type": "webui",
        "source": "run_broker",
        "expert_requested": True,
        "expert_id": "resource-delivery",
        "expert_resolution": "resolved",
        "terminal_status": "completed",
        "error_code": None,
        "failure_subsystem": None,
        "retryable": False,
        "retried": False,
        "answer_completed": True,
        "duration_ms": 12,
    }
    serialized = json.dumps(row, ensure_ascii=False)
    assert row["profile"].startswith("feishu_ou_")
    for secret in ("ou_private", "prompt", "token", "open_id", "content", "tool_calls"):
        assert secret not in serialized
