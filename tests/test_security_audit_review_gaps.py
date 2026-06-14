"""Regression coverage for the three cross-model-review gaps on P1-9 audit:

1. `credential.lease.granted` must be emitted at the MINT/发放 point, not only
   after a later HTTP redemption (a lease that is never redeemed was a blind spot).
2. `approval.requested` must NOT leak a raw filesystem path in `command_kind`
   (an absolute-path executable leaked the path; only the basename is safe).
3. `group.everyone.ignored` must also be audited on the fork
   `_should_accept_group_message` suppression path, deduped by message id so the
   two valve shapes don't double-count one broadcast.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _rows(path: Path) -> list[dict[str, str]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _raw(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Gap 1 — lease audited at mint
# --------------------------------------------------------------------------- #

def test_mint_lease_audits_granted_at_issuance(monkeypatch, tmp_path):
    from hermes_multitenancy.credential_broker import mint_lease

    audit = tmp_path / "security.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit))

    token = mint_lease(
        profile_name="alice",
        open_id="ou_mint_secret",
        run_id="run-xyz",
        secret=b"unit-secret",
        ttl_seconds=120,
    )
    assert token.startswith("v1:")

    rows = _rows(audit)
    granted = [r for r in rows if r["event_type"] == "credential.lease.granted"]
    assert granted, "mint must emit credential.lease.granted at issuance"
    row = granted[-1]
    assert row["decision"] == "granted"
    assert row["point"] == "mint"
    assert row["profile"] == "alice"
    assert row["ttl_seconds"] == "120"  # audit coerces all fields to strings
    assert row["open_id_hash"] == hashlib.sha256(b"ou_mint_secret").hexdigest()[:12]
    # Redaction: no raw open_id, no signing secret, no lease token bytes.
    assert "ou_mint_secret" not in _raw(audit)
    assert "unit-secret" not in _raw(audit)
    assert "open_id" not in row


def test_mint_lease_audit_failure_never_blocks_minting(monkeypatch, tmp_path):
    """Fail-open: even if the audit sink explodes, a lease is still minted."""
    from hermes_multitenancy import security_audit
    from hermes_multitenancy.credential_broker import mint_lease

    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_ENABLED", "1")

    def boom(*_a, **_k):
        raise RuntimeError("audit sink down")

    monkeypatch.setattr(security_audit, "append_security_event", boom)
    token = mint_lease(
        profile_name="alice", open_id="ou_x", run_id="r", secret=b"s", ttl_seconds=60
    )
    assert token.startswith("v1:")


# --------------------------------------------------------------------------- #
# Gap 2 — approval.requested must not leak a raw path
# --------------------------------------------------------------------------- #

def test_approval_requested_strips_path_from_command_kind(monkeypatch, tmp_path):
    from hermes_multitenancy import router

    audit = tmp_path / "security.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit))

    secret_path = "/Users/alice/secret-bin/doit"
    payload = {
        "command": f"{secret_path} --force /private/data",
        "description": "dangerous",
        "open_id": "ou_path_user",
        "session_key": "sk",
        "approval_id": "ap1",
    }
    asyncio.run(router._handle_child_approval_required(None, "oc_chat", payload))

    row = [r for r in _rows(audit) if r["event_type"] == "approval.requested"][-1]
    assert row["command_kind"] == "doit"  # basename only, never the path
    raw = _raw(audit)
    assert "/Users/alice/secret-bin" not in raw  # no raw filesystem path leaked
    assert "/private/data" not in raw
    assert "ou_path_user" not in raw
    # Full command remains correlatable via hash.
    assert row["command_hash"] == hashlib.sha256(
        payload["command"].encode("utf-8")
    ).hexdigest()[:12]


def test_approval_requested_strips_env_assignment_prefix(monkeypatch, tmp_path):
    """Re-review gap C: a leading VAR=secret env assignment must not leak into
    command_kind (its value is often a secret)."""
    from hermes_multitenancy import router

    audit = tmp_path / "security.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit))

    payload = {
        "command": "AWS_SECRET_ACCESS_KEY=topsecret /opt/tools/sync --all",
        "description": "dangerous",
        "open_id": "ou_env_user",
        "session_key": "sk",
        "approval_id": "ap2",
    }
    asyncio.run(router._handle_child_approval_required(None, "oc_chat", payload))

    row = [r for r in _rows(audit) if r["event_type"] == "approval.requested"][-1]
    assert row["command_kind"] == "sync"  # env prefix skipped, basename only
    raw = _raw(audit)
    assert "topsecret" not in raw  # the secret value never reaches the log
    assert "AWS_SECRET_ACCESS_KEY" not in raw
    assert "/opt/tools" not in raw


def test_approval_requested_env_only_command(monkeypatch, tmp_path):
    from hermes_multitenancy import router

    audit = tmp_path / "security.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit))
    payload = {
        "command": "FOO=bar BAZ=qux",
        "description": "d",
        "open_id": "ou_u",
        "session_key": "sk",
        "approval_id": "ap3",
    }
    asyncio.run(router._handle_child_approval_required(None, "oc_chat", payload))
    row = [r for r in _rows(audit) if r["event_type"] == "approval.requested"][-1]
    assert row["command_kind"] == "<env-only>"
    assert "bar" not in _raw(audit) and "qux" not in _raw(audit)


# --------------------------------------------------------------------------- #
# Gap 3 — fork suppression path audits, deduped by message id
# --------------------------------------------------------------------------- #

class _ForkMentionAdapter:
    def __init__(self, *, allow: bool = True, bot_open_id: str = "ou_bot"):
        self.allow = allow
        self._bot_open_id = bot_open_id
        self._bot_user_id = ""
        self._bot_name = ""

    def _allow_group_message(self, sender_id, chat_id):
        return self.allow

    def _bot_identity(self):
        return SimpleNamespace(open_id=self._bot_open_id, user_id="", name="")

    def _message_mentions_bot(self, mentions):
        for m in mentions:
            mid = getattr(m, "id", None)
            if mid is not None and getattr(mid, "open_id", "") == self._bot_open_id:
                return True
        return False

    def _post_mentions_bot(self, mentions):
        return any(getattr(m, "is_self", False) for m in mentions)

    def _should_accept_group_message(self, message, sender_id, chat_id=""):
        if "@_all" in (getattr(message, "content", "") or ""):
            return True
        return False


def _at_all_msg(message_id="om_1"):
    return SimpleNamespace(
        content='{"text":"@_all 全员通知"}',
        mentions=[SimpleNamespace(key="@_all")],
        message_type="text",
        chat_id="oc_secret_chat",
        sender_id="ou_sender",
        message_id=message_id,
    )


def _reset_dedupe():
    from hermes_multitenancy import feishu_group_valve

    feishu_group_valve._AUDITED_EVERYONE_MSG_IDS.clear()


def test_fork_suppression_path_audits_everyone_ignored(monkeypatch, tmp_path):
    from hermes_multitenancy import feishu_group_valve, router as router_mod

    audit = tmp_path / "security.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit))
    _reset_dedupe()
    feishu_group_valve._patch_should_accept_group_message(_ForkMentionAdapter)
    router_mod.override_routing_table(":memory:")

    accepted = _ForkMentionAdapter()._should_accept_group_message(
        _at_all_msg(), "ou_sender", "oc_secret_chat"
    )
    assert accepted is False  # @everyone broadcast suppressed

    rows = [r for r in _rows(audit) if r["event_type"] == "group.everyone.ignored"]
    assert len(rows) == 1
    assert rows[0]["chat_id_hash"] == hashlib.sha256(b"oc_secret_chat").hexdigest()[:12]
    assert rows[0]["open_id_hash"] == hashlib.sha256(b"ou_sender").hexdigest()[:12]
    assert "oc_secret_chat" not in _raw(audit)


def test_everyone_ignored_deduped_across_both_valve_paths(monkeypatch, tmp_path):
    """One broadcast must yield exactly one audit row even if both the _admit
    valve and the fork suppression path fire for the same message id."""
    from hermes_multitenancy import feishu_group_valve

    audit = tmp_path / "security.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit))
    _reset_dedupe()

    msg = _at_all_msg(message_id="om_dedupe")
    feishu_group_valve._audit_everyone_ignored(msg)
    feishu_group_valve._audit_everyone_ignored(msg)  # second valve path, same msg

    rows = [r for r in _rows(audit) if r["event_type"] == "group.everyone.ignored"]
    assert len(rows) == 1  # deduped by message id


def test_fork_audit_falls_back_to_explicit_args_when_message_lacks_them(
    monkeypatch, tmp_path
):
    """Re-review gap F: the fork message object may not carry chat_id/sender_id;
    the audit must use the explicit fork-path args so hashes are never empty."""
    from hermes_multitenancy import feishu_group_valve

    audit = tmp_path / "security.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit))
    _reset_dedupe()

    # Bare message: no chat_id / sender_id attributes, only a message id.
    bare = SimpleNamespace(message_id="om_bare")
    feishu_group_valve._audit_everyone_ignored(
        bare, chat_id="oc_fork_chat", sender_id="ou_fork_sender"
    )

    row = [r for r in _rows(audit) if r["event_type"] == "group.everyone.ignored"][-1]
    assert row["chat_id_hash"] == hashlib.sha256(b"oc_fork_chat").hexdigest()[:12]
    assert row["open_id_hash"] == hashlib.sha256(b"ou_fork_sender").hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Gap E — denied lease attributed to the authenticated token, not request JSON
# --------------------------------------------------------------------------- #

def test_denied_lease_attributed_to_token_not_request_body(monkeypatch, tmp_path):
    import asyncio as _asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import (
        create_run_broker_app,
        register_credential_broker_token,
    )

    audit = tmp_path / "security.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit))
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")

    async def runner():
        app = create_run_broker_app()
        register_credential_broker_token(
            token="tok-real",
            profile_name="alice_real",
            open_id="ou_real_owner",
            run_id="run-real",
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            # Attacker controls the request body but holds a valid token bound to
            # a DIFFERENT identity. The audit must blame the token, not the body.
            resp = await client.post(
                "/api/run-broker/credentials/lease",
                headers={"Authorization": "Bearer tok-real"},
                json={
                    "lease": "invalid-lease",
                    "kind": "feishu_uat",
                    "profile_name": "victim_profile",
                    "open_id": "ou_spoofed_victim",
                    "run_id": "run-real",
                },
            )
            assert resp.status == 403
        finally:
            await client.close()

    _asyncio.run(runner())

    row = [r for r in _rows(audit) if r["event_type"] == "credential.lease.denied"][-1]
    assert row["profile"] == "alice_real"  # token_record, not request body
    assert row["open_id_hash"] == hashlib.sha256(b"ou_real_owner").hexdigest()[:12]
    raw = _raw(audit)
    assert "ou_spoofed_victim" not in raw
    assert "victim_profile" not in raw
