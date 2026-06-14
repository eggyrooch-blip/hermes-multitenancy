from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest


def _read_jsonl(path: Path) -> list[dict[str, str]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _serialized(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _assert_redacted(raw: str, *forbidden: str) -> None:
    for value in (
        "access_token",
        "refresh_token",
        "app_secret",
        "api_key",
        *forbidden,
    ):
        assert value not in raw


def test_credential_lease_events_keep_safe_fields_and_hash_open_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hermes_multitenancy.security_audit import append_security_event

    audit_path = tmp_path / "security.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))

    append_security_event(
        event_type="credential.lease.granted",
        open_id="ou_secret_open_id",
        profile="alice",
        lease_kind="feishu_uat",
        decision="granted",
        access_token="secret-token-value",
    )
    append_security_event(
        event_type="credential.lease.denied",
        open_id="ou_secret_open_id",
        profile="alice",
        lease_kind="provider_env",
        decision="denied",
        reason="lease_verification_failed",
        app_secret="secret-app-value",
    )

    rows = _read_jsonl(audit_path)
    assert rows[0]["event_type"] == "credential.lease.granted"
    assert rows[0]["open_id_hash"] == hashlib.sha256(b"ou_secret_open_id").hexdigest()[:12]
    assert rows[0]["lease_kind"] == "feishu_uat"
    assert rows[0]["decision"] == "granted"
    assert rows[1]["event_type"] == "credential.lease.denied"
    assert rows[1]["lease_kind"] == "provider_env"
    assert rows[1]["decision"] == "denied"
    assert rows[1]["reason"] == "lease_verification_failed"
    raw = _serialized(audit_path)
    assert "open_id" not in rows[0]
    _assert_redacted(raw, "ou_secret_open_id", "secret-token-value", "secret-app-value")


def test_approval_requested_hashes_command_and_keeps_command_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hermes_multitenancy.security_audit import append_security_event

    audit_path = tmp_path / "security.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))
    command = "rm -rf /"

    append_security_event(
        event_type="approval.requested",
        open_id="ou_approval_user",
        command_hash=hashlib.sha256(command.encode("utf-8")).hexdigest()[:12],
        command_kind="rm",
        reason="dangerous_command",
        decision="requested",
        command=command,
        refresh_token="refresh-secret",
    )

    row = _read_jsonl(audit_path)[0]
    assert row["event_type"] == "approval.requested"
    assert row["command_hash"] == hashlib.sha256(command.encode("utf-8")).hexdigest()[:12]
    assert row["command_kind"] == "rm"
    assert row["reason"] == "dangerous_command"
    assert row["decision"] == "requested"
    assert row["open_id_hash"] == hashlib.sha256(b"ou_approval_user").hexdigest()[:12]
    raw = _serialized(audit_path)
    _assert_redacted(raw, command, "ou_approval_user", "refresh-secret")


def test_group_everyone_ignored_hashes_chat_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hermes_multitenancy.security_audit import append_security_event

    audit_path = tmp_path / "security.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))

    append_security_event(
        event_type="group.everyone.ignored",
        chat_id="oc_secret_chat",
        open_id="ou_sender",
        reason="at_everyone_broadcast",
        api_key="api-key-secret",
    )

    row = _read_jsonl(audit_path)[0]
    assert row["event_type"] == "group.everyone.ignored"
    assert row["chat_id_hash"] == hashlib.sha256(b"oc_secret_chat").hexdigest()[:12]
    assert row["open_id_hash"] == hashlib.sha256(b"ou_sender").hexdigest()[:12]
    assert row["reason"] == "at_everyone_broadcast"
    raw = _serialized(audit_path)
    assert "chat_id" not in row
    _assert_redacted(raw, "oc_secret_chat", "ou_sender", "api-key-secret")


def test_credential_lease_denied_route_audits_without_raw_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import (
        create_run_broker_app,
        register_credential_broker_token,
    )

    audit_path = tmp_path / "security.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")

    async def runner() -> None:
        app = create_run_broker_app()
        register_credential_broker_token(
            token="audit-route-token",
            profile_name="alice",
            open_id="ou_route_user",
            run_id="run-audit",
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post(
                "/api/run-broker/credentials/lease",
                headers={"Authorization": "Bearer audit-route-token"},
                json={
                    "lease": "invalid-lease",
                    "kind": "feishu_uat",
                    "profile_name": "alice",
                    "open_id": "ou_route_user",
                    "run_id": "run-audit",
                },
            )
            body = await response.json()
        finally:
            await client.close()

        assert response.status == 403
        assert body["error"] == "forbidden"

    asyncio.run(runner())

    row = _read_jsonl(audit_path)[-1]
    assert row["event_type"] == "credential.lease.denied"
    assert row["open_id_hash"] == hashlib.sha256(b"ou_route_user").hexdigest()[:12]
    assert row["profile"] == "alice"
    assert row["lease_kind"] == "feishu_uat"
    assert row["decision"] == "denied"
    assert row["reason"] == "lease_verification_failed"
    _assert_redacted(_serialized(audit_path), "ou_route_user", "invalid-lease", "test-key")
