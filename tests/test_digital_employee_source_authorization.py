from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace


def test_private_sources_require_live_read_and_return_no_denied_metadata(tmp_path: Path):
    from hermes_multitenancy.source_authorization import authorize_private_source_refs

    profile = tmp_path / "profiles" / "alice"
    seen: list[str] = []

    def verify(_profile: Path, owner: str, locator: str):
        seen.append(f"{owner}:{locator}")
        if locator == "docAllowed123":
            return "https://feishu.cn/docx/docAllowed123"
        return None

    refs = [
        {"id": "allowed", "type": "lark_doc", "label": "Allowed policy", "locator": "docAllowed123"},
        {"id": "denied", "type": "lark_doc", "label": "Secret title", "locator": "docDenied123"},
        {"id": "web", "type": "web", "label": "Public", "uri": "https://example.com"},
    ]

    assert authorize_private_source_refs(profile, "ou_alice", refs, verify=verify) == [
        {
            "id": "allowed",
            "type": "lark_doc",
            "label": "Allowed policy",
            "target": "https://feishu.cn/docx/docAllowed123",
        }
    ]
    assert seen == ["ou_alice:docAllowed123", "ou_alice:docDenied123"]


def test_private_sources_fail_closed_before_verifier_for_bad_identity_or_envelope(tmp_path: Path):
    from hermes_multitenancy.source_authorization import authorize_private_source_refs

    calls = 0

    def verify(_profile: Path, _owner: str, _locator: str):
        nonlocal calls
        calls += 1
        return "https://feishu.cn/docx/unexpected"

    refs = [
        {"id": "bad", "type": "lark_doc", "label": "private", "locator": "../escape"},
        {"id": "credential", "type": "lark_doc", "label": "private", "locator": "sk-secret123"},
    ]
    assert authorize_private_source_refs(tmp_path / "profile", "", refs, verify=verify) == []
    assert authorize_private_source_refs(tmp_path / "profile", "ou_alice", refs, verify=verify) == []
    assert calls == 0


def test_broker_source_authorization_binds_trusted_owner_profile(monkeypatch, tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy import webui_broker_server as broker

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "broker-key")
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")
    monkeypatch.setattr(broker, "_resolve_owner_scoped_profile", lambda request, _payload: (
        ("alice", None)
        if request.headers.get("X-Hermes-Owner-Open-Id") == "ou_alice"
        else (None, "owner ou_private mismatch")
    ))
    monkeypatch.setattr(broker, "_profile_home_for_name", lambda _profile: tmp_path / "profiles" / "alice")
    monkeypatch.setattr(
        broker,
        "authorize_private_source_refs",
        lambda profile, owner, refs: [{"id": refs[0]["id"], "type": "lark_doc", "label": refs[0]["label"], "target": "https://feishu.cn/docx/safe"}]
        if profile.name == "alice" and owner == "ou_alice" else [],
    )

    async def run():
        client = TestClient(TestServer(broker.create_run_broker_app()))
        await client.start_server()
        try:
            body = {"profile_name": "forged", "refs": [{"id": "policy", "type": "lark_doc", "label": "Policy", "locator": "docSafe123"}]}
            denied = await client.post("/api/run-broker/source-refs/authorize", json=body, headers={"Authorization": "Bearer broker-key"})
            allowed = await client.post(
                "/api/run-broker/source-refs/authorize",
                json=body,
                headers={"Authorization": "Bearer broker-key", "X-Hermes-Owner-Open-Id": "ou_alice"},
            )
            assert denied.status == 403
            denied_body = await denied.json()
            assert denied_body == {"error": "source authorization unavailable"}
            assert "ou_private" not in json.dumps(denied_body)
            assert allowed.status == 200
            assert await allowed.json() == {"refs": [{"id": "policy", "type": "lark_doc", "label": "Policy", "target": "https://feishu.cn/docx/safe"}]}
        finally:
            await client.close()

    asyncio.run(run())


def test_live_lark_read_requires_exact_document_identity(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.source_authorization import _live_lark_doc_target

    @contextmanager
    def scope(_profile_home: Path, _owner: str):
        yield {
            "HERMES_LARK_CLI_BIN": "/trusted/lark-cli",
            "LARKSUITE_CLI_DEFAULT_AS": "user",
        }

    monkeypatch.setattr(agent_real, "_lark_cli_auth_broker_scope", scope)
    responses = iter([
        {"data": {"document": {}}},
        {"data": {"document": {"document_id": "docOther"}}},
        {"data": {"document": {"document_id": "docWanted"}}},
    ])
    monkeypatch.setattr(
        "hermes_multitenancy.source_authorization.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(next(responses)),
        ),
    )

    profile = tmp_path / "profiles" / "alice"
    assert _live_lark_doc_target(profile, "ou_alice", "docWanted") is None
    assert _live_lark_doc_target(profile, "ou_alice", "docWanted") is None
    assert _live_lark_doc_target(profile, "ou_alice", "docWanted") == "https://feishu.cn/docx/docWanted"


def test_live_lark_read_rejects_bot_scope_and_cli_failure(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.source_authorization import _live_lark_doc_target

    @contextmanager
    def bot_scope(_profile_home: Path, _owner: str):
        yield {
            "HERMES_LARK_CLI_BIN": "/trusted/lark-cli",
            "LARKSUITE_CLI_DEFAULT_AS": "bot",
        }

    monkeypatch.setattr(agent_real, "_lark_cli_auth_broker_scope", bot_scope)
    run = monkeypatch.setattr(
        "hermes_multitenancy.source_authorization.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="{}"),
    )
    assert run is None
    assert _live_lark_doc_target(tmp_path / "profile", "ou_alice", "docWanted") is None
