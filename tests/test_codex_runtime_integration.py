"""C8 integration: mapped runs bind the real workspace and Codex home."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes_multitenancy import agent_real
from hermes_multitenancy.agent_real import _core
from hermes_multitenancy.agent_real import codex_provider_proxy
from hermes_multitenancy.agent_real import executor_map
from hermes_multitenancy.agent_real import run_workspace
from hermes_multitenancy import billing_identity
from hermes_multitenancy.billing_credentials import BillingIdentity
from hermes_multitenancy.gitlab_owner_scope_attestation import (
    create_attestation,
    issue_trusted_gitlab_run_attestation,
)
from hermes_multitenancy.gitlab_token_intake import SCOPE_BINDING_UNVERIFIED
from hermes_multitenancy.single_actor_spend_receipt import (
    begin_single_actor_spend_receipt,
)
from hermes_multitenancy.trusted_runtime_principal import (
    TrustedRuntimePrincipal,
    issue_webui_principal,
)


def _git_repo(path: Path, filename: str) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / filename).write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", filename], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=test",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return path


def _event(repo: Path, hub: Path):
    return SimpleNamespace(
        text="analyze the repository",
        sender_open_id="ou_alice",
        source=SimpleNamespace(
            platform=SimpleNamespace(value="webui"),
            chat_id="chat-1",
            chat_type="p2p",
            user_id="ou_alice",
        ),
        raw_event={
            "session_id": "workflow-1",
            "metadata": {
                "expert_id": "kep-server",
                "repo_git_url": str(repo),
                "spec_hub_git_url": str(hub),
            },
        },
    )


def test_codex_runtime_env_rejects_empty_broker_role(tmp_path: Path, monkeypatch):
    from hermes_multitenancy.agent_real import harness_webui_runtime

    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    workspace = SimpleNamespace(root=tmp_path / "run", repo_dir=tmp_path / "run" / "repo")
    workspace.repo_dir.mkdir(parents=True)
    event = _event(tmp_path / "repo-source", tmp_path / "hub-source")
    monkeypatch.setattr(harness_webui_runtime, "require_event_admission", lambda *_a: None)
    monkeypatch.setattr(_core, "_codex_expert_plugin_dir", lambda *_a: tmp_path / "plugin")
    monkeypatch.setattr(
        _core,
        "_codex_model_and_base_url",
        lambda *_a: ("gpt-5.6-terra", "https://litellm.example/v1"),
    )
    monkeypatch.setattr(_core, "_broker_role_override_block_for_event", lambda *_a: None)

    with pytest.raises(_core.ExpertUnavailableError):
        _core._codex_runtime_env(
            event, profile, workspace, "hcx_test", "http://127.0.0.1:12345/v1"
        )


def test_codex_runtime_env_wraps_missing_plugin_source(tmp_path: Path, monkeypatch):
    from hermes_multitenancy.agent_real import harness_webui_runtime

    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    workspace = SimpleNamespace(root=tmp_path / "run", repo_dir=tmp_path / "run" / "repo")
    workspace.repo_dir.mkdir(parents=True)
    event = _event(tmp_path / "repo-source", tmp_path / "hub-source")
    monkeypatch.setattr(harness_webui_runtime, "require_event_admission", lambda *_a: None)
    monkeypatch.setattr(
        _core, "_codex_expert_plugin_dir", lambda *_a: tmp_path / "missing-plugin"
    )
    monkeypatch.setattr(
        _core,
        "_codex_model_and_base_url",
        lambda *_a: ("gpt-5.6-terra", "https://litellm.example/v1"),
    )
    monkeypatch.setattr(
        _core, "_broker_role_override_block_for_event", lambda *_a: "SEALED ROLE"
    )

    with pytest.raises(executor_map.ExecutorUnavailable, match="CODEX_HOME could not"):
        _core._codex_runtime_env(
            event, profile, workspace, "hcx_test", "http://127.0.0.1:12345/v1"
        )


def _bind_readonly_gitlab(
    shared: Path,
    profile_name: str,
    open_id: str,
    *,
    credential_profile: str | None = None,
    scopes: list[str] | None = None,
    owner_actor_subject: str | None = None,
    token_owner_verified: bool = True,
) -> None:
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.routing import RoutingTable

    (shared / "credential-materialization.yaml").write_text(
        "credentials:\n"
        "  - subject_id: kep-prd-skills\n"
        "    provider: gitlab\n"
        "    secret_kind: token\n"
        "    env: GITLAB_TOKEN\n"
        "    vault_profile: __self__\n"
        "    profiles: ['*']\n",
        encoding="utf-8",
    )
    table = RoutingTable(shared / "multitenancy.db")
    try:
        table.upsert(user_id=profile_name, profile_name=profile_name, open_id=open_id)
    finally:
        table.close()
    store = CredentialStore(shared / "multitenancy.db")
    try:
        store.put_credential(
            profile_name=credential_profile or profile_name,
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
            payload={
                "token": "actor-read-token",
                "owner_actor_subject": owner_actor_subject or open_id,
                "token_owner_verified": token_owner_verified,
            },
            scopes=scopes or ["read_api", "read_repository"],
        )
    finally:
        store.close()


def _seal_webui_event(event, profile: Path, actor: str) -> None:
    event.trusted_runtime_principal = issue_webui_principal(
        profile_name=profile.name,
        actor_subject=actor,
        credential_subject=actor,
    )


def _attach_local_codex_evidence(
    event,
    profile: Path,
    *,
    api_key: str = "sk-local-actor-key",
) -> None:
    """Attach real signed objects backed by deterministic local-only fakes."""
    now_ms = int(time.time() * 1000)
    fingerprint_key = b"f" * 32
    signing_key = Ed25519PrivateKey.generate()
    receipt = create_attestation(
        {
            "id": 91,
            "user_id": 7,
            "scopes": ["read_api", "read_repository"],
            "active": True,
            "revoked": False,
            "token": "actor-read-token",
        },
        expected_gitlab_user_id=7,
        get_current_user=lambda _token: {"id": 7},
        actor_subject=event.trusted_runtime_principal.actor_subject,
        profile=profile.name,
        run_id="workflow-1",
        audience=_core._CODEX_EVIDENCE_AUDIENCE,
        private_key=signing_key,
        fingerprint_key=fingerprint_key,
        now_ms=now_ms,
    )
    event.trusted_gitlab_run_attestation = issue_trusted_gitlab_run_attestation(
        receipt,
        public_key=signing_key.public_key(),
        expected_audience=_core._CODEX_EVIDENCE_AUDIENCE,
        expected_run_id="workflow-1",
        actor_subject=event.trusted_runtime_principal.actor_subject,
        profile=profile.name,
        token="actor-read-token",
        expected_gitlab_user_id=7,
        fingerprint_key=fingerprint_key,
        consume_nonce=lambda _nonce, _expires: True,
        now_ms=now_ms,
    )
    event.trusted_gitlab_fingerprint_key = fingerprint_key

    identity = BillingIdentity(
        employee_user_id=profile.name,
        profile_name=profile.name,
        litellm_user_id=f"payer-{profile.name}",
        key_id="hashed-actor-key",
        expires_at=now_ms + 3_600_000,
        migration_state="enforced",
    )
    spend_key = hashlib.sha256(api_key.encode()).hexdigest()

    class SpendClient:
        calls = 0

        def get(self, path, query):
            assert path == "/spend/logs/v2"
            assert query["api_key"] == spend_key
            self.calls += 1
            rows = [] if self.calls == 1 else [
                {
                    "request_id": "litellm-real-request",
                    "api_key": spend_key,
                    "spend": "0.001",
                }
            ]
            return {
                "data": rows,
                "total": len(rows),
                "total_pages": 0 if not rows else 1,
                "total_is_capped": False,
                "page": 1,
                "page_size": 100,
            }

    route = SimpleNamespace(
        open_id=event.trusted_runtime_principal.actor_subject,
        profile_name=profile.name,
        user_id=profile.name,
        active=True,
        kind="user",
        provenance="sync",
    )
    event.trusted_single_actor_spend_state = begin_single_actor_spend_receipt(
        principal=event.trusted_runtime_principal,
        routing=SimpleNamespace(lookup_by_open_id=lambda _actor: route),
        store=SimpleNamespace(get=lambda _user_id: identity),
        credentials=SimpleNamespace(runtime_api_key=lambda _metadata: api_key),
        client=SpendClient(),
        profile_is_solely_owned=lambda _profile, _actor: True,
        run_id="workflow-1",
        audience=_core._CODEX_EVIDENCE_AUDIENCE,
        billing_base_url="https://litellm.example/v1",
        signing_key=Ed25519PrivateKey.generate(),
        fingerprint_key=b"s" * 32,
        now_ms=now_ms,
    )


def test_codex_git_inputs_prefer_run_metadata_over_sticky_env(monkeypatch):
    event = SimpleNamespace(
        raw_event={
            "metadata": {
                "repo_git_url": "https://gitlab.example.com/mall/current.git",
                "repo_ref": "current-ref",
                "spec_hub_git_url": (
                    "https://gitlab.example.com/kep/current-hub.git"
                ),
            }
        }
    )
    monkeypatch.setenv(
        "HERMES_CODEX_REPO_GIT_URL",
        "https://gitlab.example.com/mall/stale.git",
    )
    monkeypatch.setenv("HERMES_CODEX_REPO_REF", "stale-ref")
    monkeypatch.setenv(
        "HERMES_CODEX_SPEC_HUB_GIT_URL",
        "https://gitlab.example.com/kep/stale-hub.git",
    )

    assert _core._codex_git_inputs_for_event(event) == {
        "repo_git_url": "https://gitlab.example.com/mall/current.git",
        "repo_ref": "current-ref",
        "spec_hub_git_url": "https://gitlab.example.com/kep/current-hub.git",
    }


def test_codex_git_inputs_never_fall_back_to_ambient_env(monkeypatch):
    event = SimpleNamespace(raw_event={"metadata": {}})
    monkeypatch.setenv(
        "HERMES_CODEX_REPO_GIT_URL",
        "https://gitlab.example.com/mall/stale.git",
    )
    monkeypatch.setenv("HERMES_CODEX_REPO_REF", "stale-ref")
    monkeypatch.setenv(
        "HERMES_CODEX_SPEC_HUB_GIT_URL",
        "https://gitlab.example.com/kep/stale-hub.git",
    )

    assert _core._codex_git_inputs_for_event(event) == {
        "repo_git_url": "",
        "repo_ref": "",
        "spec_hub_git_url": "",
    }


def test_codex_model_endpoint_never_uses_request_metadata(tmp_path: Path):
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "model:\n"
        "  default: custom:litellm/gpt-5.4\n"
        "  base_url: https://server.example/v1\n",
        encoding="utf-8",
    )
    event = SimpleNamespace(
        raw_event={
            "metadata": {
                "model": "gpt-5.4",
                "provider": "custom:litellm",
                "litellm_billing_base_url": "https://caller.example/v1",
            }
        }
    )

    assert _core._codex_model_and_base_url(event, profile) == (
        "gpt-5.4",
        "https://server.example/v1",
    )

    (profile / "config.yaml").write_text(
        "model:\n  default: custom:litellm/gpt-5.4\n",
        encoding="utf-8",
    )
    with pytest.raises(executor_map.ExecutorUnavailable, match="billing base_url"):
        _core._codex_model_and_base_url(event, profile)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("GPT-5.5-priority", "GPT-5.5-priority"),
        ("gpt-5.4", "gpt-5.4"),
        ("tencent/gpt-5.6-terra-standard", "tencent/gpt-5.6-terra-standard"),
    ],
)
def test_codex_accepts_explicit_gpt_models_from_bound_provider(
    tmp_path: Path,
    model: str,
    expected: str,
):
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "model:\n"
        "  default: custom:litellm-sre/auto\n"
        "  base_url: https://server.example/v1\n",
        encoding="utf-8",
    )
    event = SimpleNamespace(raw_event={"metadata": {
        "model": model,
        "provider": "custom:litellm-sre",
    }})

    assert _core._codex_model_and_base_url(event, profile) == (
        expected,
        "https://server.example/v1",
    )


@pytest.mark.parametrize(
    "model",
    ["", "auto", "claude-sonnet-5", "Gemini-3.5-Flash", "anthropic/gpt-4o-mini"],
)
def test_codex_rejects_auto_and_non_gpt_models_before_runtime(
    tmp_path: Path,
    model: str,
):
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "model:\n"
        "  default: custom:litellm-sre/auto\n"
        "  base_url: https://server.example/v1\n",
        encoding="utf-8",
    )
    event = SimpleNamespace(raw_event={"metadata": {
        "model": model,
        "provider": "custom:litellm-sre",
    }})

    with pytest.raises(executor_map.ExecutorUnavailable, match="explicit GPT model"):
        _core._codex_model_and_base_url(event, profile)


def test_codex_rejects_request_provider_override(tmp_path: Path):
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "model:\n"
        "  default: custom:litellm-sre/auto\n"
        "  base_url: https://server.example/v1\n",
        encoding="utf-8",
    )
    event = SimpleNamespace(raw_event={"metadata": {
        "model": "gpt-5.4",
        "provider": "openai",
    }})

    with pytest.raises(executor_map.ExecutorUnavailable, match="provider mismatch"):
        _core._codex_model_and_base_url(event, profile)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("credential_profile", "scopes"),
    [
        ("__shared__", ["read_api", "read_repository"]),
        (
            "alice",
            ["api", "read_api", "read_repository", "write_repository"],
        ),
        ("alice", ["read_api", "read_repository", SCOPE_BINDING_UNVERIFIED]),
    ],
)
async def test_mapped_public_run_rejects_unbound_or_write_gitlab_token(
    tmp_path: Path,
    monkeypatch,
    credential_profile: str,
    scopes: list[str],
):
    """Neither global/shared fallback nor a write credential may clone."""
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    repo = _git_repo(tmp_path / "mall", "README.md")
    hub = _git_repo(tmp_path / "KepSpecHub", "SPEC.md")
    map_path = tmp_path / "executor-map.yaml"
    map_path.write_text("kep-server: codex_app_server\n", encoding="utf-8")
    event = _event(repo, hub)
    _seal_webui_event(event, profile, "ou_alice")

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    monkeypatch.setenv("HERMES_EXECUTOR_MAP", str(map_path))
    monkeypatch.setenv("HERMES_READONLY_GITLAB_TOKEN", "shared-token-must-not-win")
    _bind_readonly_gitlab(
        tmp_path,
        "alice",
        "ou_alice",
        credential_profile=credential_profile,
        scopes=scopes,
    )
    monkeypatch.setattr(_core, "_resolve_explicit_expert_for_execution", lambda *_: None)

    async def bind_then_start(event, profile_home, **_kwargs):
        _core._bind_codex_run_workspace(event, profile_home)
        return "started"

    monkeypatch.setattr(_core, "_run_aiagent_subprocess", bind_then_start)

    # t04: real_run_agent now re-raises ExecutorUnavailable with a fixed
    # employee-facing Chinese message; the internal "read credential" reason
    # this test actually cares about survives on the wrapped __cause__.
    with pytest.raises(executor_map.ExecutorUnavailable) as excinfo:
        await agent_real.real_run_agent(event, profile)
    assert "read credential" in str(excinfo.value.__cause__)


@pytest.mark.asyncio
async def test_mapped_public_run_rejects_unadmitted_sender_even_when_route_matches(
    tmp_path: Path, monkeypatch
):
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    repo = _git_repo(tmp_path / "mall", "README.md")
    hub = _git_repo(tmp_path / "KepSpecHub", "SPEC.md")
    map_path = tmp_path / "executor-map.yaml"
    map_path.write_text("kep-server: codex_app_server\n", encoding="utf-8")
    event = _event(repo, hub)
    event.raw_event["metadata"].update(
        {
            "trusted_ticket_fingerprint": "forged",
            "trusted_actor_subject": "ou_alice",
            "trusted_credential_subject": "ou_alice",
            "trusted_profile_name": "alice",
        }
    )
    event.trusted_runtime_principal = TrustedRuntimePrincipal(
        channel="webui",
        profile_name="alice",
        actor_subject="ou_alice",
        credential_subject="ou_alice",
    )
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    monkeypatch.setenv("HERMES_EXECUTOR_MAP", str(map_path))
    _bind_readonly_gitlab(tmp_path, "alice", "ou_alice")
    monkeypatch.setattr(_core, "_resolve_explicit_expert_for_execution", lambda *_: None)

    async def start(event, profile_home, **_kwargs):
        _core._bind_codex_run_workspace(event, profile_home)
        return "started"

    monkeypatch.setattr(_core, "_run_aiagent_subprocess", start)

    # t04: real_run_agent now re-raises ExecutorUnavailable with a fixed
    # employee-facing Chinese message; the internal "read credential" reason
    # this test actually cares about survives on the wrapped __cause__.
    with pytest.raises(executor_map.ExecutorUnavailable) as excinfo:
        await agent_real.real_run_agent(event, profile)
    assert "read credential" in str(excinfo.value.__cause__)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "owner_actor_subject", "token_owner_verified"),
    [
        ("ou_alice", "ou_alice", False),
        ("ou_bob", "ou_alice", True),
    ],
)
async def test_mapped_run_rejects_unverified_or_stale_credential_owner(
    tmp_path: Path,
    monkeypatch,
    actor: str,
    owner_actor_subject: str,
    token_owner_verified: bool,
):
    from hermes_multitenancy.routing import RoutingTable

    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    repo = _git_repo(tmp_path / "mall", "README.md")
    hub = _git_repo(tmp_path / "KepSpecHub", "SPEC.md")
    map_path = tmp_path / "executor-map.yaml"
    map_path.write_text("kep-server: codex_app_server\n", encoding="utf-8")
    event = _event(repo, hub)
    _seal_webui_event(event, profile, actor)
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    monkeypatch.setenv("HERMES_EXECUTOR_MAP", str(map_path))
    _bind_readonly_gitlab(
        tmp_path,
        "alice",
        "ou_alice",
        owner_actor_subject=owner_actor_subject,
        token_owner_verified=token_owner_verified,
    )
    if actor == "ou_bob":
        table = RoutingTable(tmp_path / "multitenancy.db")
        try:
            table.upsert(user_id="alice", profile_name="alice", open_id="ou_bob")
        finally:
            table.close()

    with pytest.raises(executor_map.ExecutorUnavailable, match="read credential"):
        _core._bind_codex_run_workspace(event, profile)


@pytest.mark.parametrize("missing", ["gitlab", "spend"])
def test_mapped_run_rejects_missing_signed_evidence_before_clone(
    tmp_path: Path, monkeypatch, missing: str
):
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    repo = _git_repo(tmp_path / "mall", "README.md")
    hub = _git_repo(tmp_path / "KepSpecHub", "SPEC.md")
    map_path = tmp_path / "executor-map.yaml"
    map_path.write_text("kep-server: codex_app_server\n", encoding="utf-8")
    event = _event(repo, hub)
    _seal_webui_event(event, profile, "ou_alice")
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    monkeypatch.setenv("HERMES_EXECUTOR_MAP", str(map_path))
    _bind_readonly_gitlab(tmp_path, "alice", "ou_alice")
    if missing == "spend":
        _attach_local_codex_evidence(event, profile)
        event.trusted_single_actor_spend_state = None

    prepared = False

    def must_not_clone(*_args, **_kwargs):
        nonlocal prepared
        prepared = True
        raise AssertionError("clone must not start")

    monkeypatch.setattr(
        "hermes_multitenancy.agent_real.run_workspace.prepare", must_not_clone
    )
    expected = "attestation" if missing == "gitlab" else "spend snapshot"
    with pytest.raises(executor_map.ExecutorUnavailable, match=expected):
        _core._bind_codex_run_workspace(event, profile)
    assert prepared is False


def test_mapped_run_verifies_attestation_and_spend_before_clone(
    tmp_path: Path, monkeypatch
):
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    map_path = tmp_path / "executor-map.yaml"
    map_path.write_text("kep-server: codex_app_server\n", encoding="utf-8")
    event = _event(tmp_path / "repo", tmp_path / "hub")
    _seal_webui_event(event, profile, "ou_alice")
    _attach_local_codex_evidence(event, profile)
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    monkeypatch.setenv("HERMES_EXECUTOR_MAP", str(map_path))
    _bind_readonly_gitlab(tmp_path, "alice", "ou_alice")
    order: list[str] = []

    monkeypatch.setattr(
        "hermes_multitenancy.gitlab_owner_scope_attestation.require_trusted_gitlab_run_attestation",
        lambda *_args, **_kwargs: order.append("attestation"),
    )
    monkeypatch.setattr(
        _core,
        "_require_codex_spend_state_for_event",
        lambda *_args, **_kwargs: order.append("spend"),
    )

    def prepare(*_args, **_kwargs):
        order.append("clone")
        return SimpleNamespace(
            repo_dir=profile / "workspace" / "runs" / "workflow-1" / "repo"
        )

    monkeypatch.setattr("hermes_multitenancy.agent_real.run_workspace.prepare", prepare)

    _core._bind_codex_run_workspace(event, profile)
    assert order == ["attestation", "spend", "clone"]


@pytest.mark.asyncio
async def test_mapped_stream_withholds_content_when_spend_receipt_fails(
    tmp_path: Path, monkeypatch
):
    import hermes_multitenancy.single_actor_spend_receipt as receipt_module

    monkeypatch.setattr(receipt_module, "_SPEND_POLL_TIMEOUT_S", 0)
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    map_path = tmp_path / "executor-map.yaml"
    map_path.write_text("kep-server: codex_app_server\n", encoding="utf-8")
    event = _event(tmp_path / "repo", tmp_path / "hub")
    _seal_webui_event(event, profile, "ou_alice")
    _attach_local_codex_evidence(event, profile)
    monkeypatch.setenv("HERMES_EXECUTOR_MAP", str(map_path))

    class NoSpend:
        def get(self, _path, _query):
            return {
                "data": [], "total": 0, "total_pages": 0,
                "total_is_capped": False, "page": 1, "page_size": 100,
            }

    event.trusted_single_actor_spend_state._client = NoSpend()
    event._trusted_codex_proxy_request_limit = 1
    event._trusted_codex_proxy_audit = {
        "request_count": 1,
        "request_limit": 1,
        "rejected_requests": 0,
        "response_completed": 1,
        "usage_response_count": 1,
        "usage_present": True,
        "store_forced": True,
        "input_tokens": 7,
        "output_tokens": 3,
        "total_tokens": 10,
        "cache_read_tokens": 2,
    }

    async def child(_event, _profile, **_kwargs):
        yield "content", "must stay buffered"
        _event._trusted_codex_usage = {"api_calls": 1}
        yield "done", "must stay buffered"

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", child)
    monkeypatch.setattr(_core, "_resolve_explicit_expert_for_execution", lambda *_: None)
    visible = []
    # t04: stream_run_agent now re-raises ExecutorUnavailable with a fixed
    # employee-facing Chinese message; the internal "spend receipt" reason
    # this test actually cares about survives on the wrapped __cause__.
    with pytest.raises(executor_map.ExecutorUnavailable) as excinfo:
        async for item in agent_real.stream_run_agent(event, profile):
            visible.append(item)
    assert "spend receipt" in str(excinfo.value.__cause__)
    assert visible == []


@pytest.mark.asyncio
async def test_spend_poll_does_not_block_the_webui_event_loop(tmp_path: Path, monkeypatch):
    import hermes_multitenancy.single_actor_spend_receipt as receipt_module

    event = _event(tmp_path / "repo", tmp_path / "hub")
    event._trusted_codex_usage = {"api_calls": 0}
    event._trusted_codex_proxy_request_limit = 1
    event._trusted_codex_proxy_audit = {
        "request_count": 1,
        "request_limit": 1,
        "rejected_requests": 0,
        "response_completed": 1,
        "usage_response_count": 1,
        "usage_present": True,
        "store_forced": True,
        "input_tokens": 7,
        "output_tokens": 3,
        "total_tokens": 10,
        "cache_read_tokens": 2,
    }
    monkeypatch.setattr(
        _core, "_require_codex_spend_state_for_event", lambda *_args: object()
    )

    def slow_finish(*_args, **_kwargs):
        time.sleep(0.05)
        return {"signature": "local-test"}

    monkeypatch.setattr(
        receipt_module, "finish_single_actor_spend_receipt", slow_finish
    )
    ledger_writes = []
    monkeypatch.setattr(
        _core,
        "_write_token_ledger_from_child",
        lambda _event, _profile, usage, **kwargs: ledger_writes.append(
            (usage, kwargs)
        ),
    )
    loop_advanced = False

    async def tick():
        nonlocal loop_advanced
        await asyncio.sleep(0.01)
        loop_advanced = True

    ticker = asyncio.create_task(tick())
    await _core._complete_codex_spend_receipt(event, tmp_path)
    await ticker

    assert loop_advanced is True
    assert event.trusted_single_actor_spend_receipt == {"signature": "local-test"}
    assert ledger_writes == [
        (
            {
                "api_calls": 1,
                "input_tokens": 7,
                "output_tokens": 3,
                "total_tokens": 10,
                "cache_read_tokens": 2,
                "cache_write_tokens": 0,
            },
            {"verified_codex": True},
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("run_mode", ["nonstream", "stream"])
async def test_workspace_clone_does_not_block_the_webui_event_loop(
    tmp_path: Path, monkeypatch, run_mode: str
):
    event = _event(tmp_path / "repo", tmp_path / "hub")
    profile = tmp_path / "profiles" / "alice"
    (profile / "workspace").mkdir(parents=True)

    def slow_bind(*_args):
        time.sleep(0.05)
        raise RuntimeError("stop after clone probe")

    monkeypatch.setattr(_core, "_bind_codex_run_workspace", slow_bind)
    monkeypatch.setitem(
        agent_real._stream_aiagent_subprocess.__globals__,
        "_bind_codex_run_workspace",
        slow_bind,
    )
    event.broker_role_override = {
        "expert_id": "kep-server",
        "block": "local test expert",
        "skills": [],
    }
    loop_advanced = False

    async def tick():
        nonlocal loop_advanced
        await asyncio.sleep(0.01)
        loop_advanced = True

    ticker = asyncio.create_task(tick())
    with pytest.raises(RuntimeError, match="clone probe"):
        if run_mode == "stream":
            async for _item in agent_real._stream_aiagent_subprocess(event, profile):
                pass
        else:
            await _core._run_aiagent_subprocess(event, profile)
    assert loop_advanced is True
    await ticker


@pytest.mark.asyncio
async def test_proxy_scope_teardown_does_not_block_the_webui_event_loop():
    class SlowScope:
        def __exit__(self, *_args):
            time.sleep(0.05)

    loop_advanced = False

    async def tick():
        nonlocal loop_advanced
        await asyncio.sleep(0.01)
        loop_advanced = True

    ticker = asyncio.create_task(tick())
    await _core._exit_aiagent_subprocess_env_scope(SlowScope(), (None, None, None))
    assert loop_advanced is True
    await ticker


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_audit",
    [
        None,
        {"request_count": 0},
        {"request_count": 2},
        {"request_limit": 2},
        {"rejected_requests": 1},
        {"rejected_requests": 1, "budget_exhausted": True, "rejected_concurrent": 0},
        {"response_completed": 0},
        {"usage_response_count": 0},
        {"usage_present": False},
        {"store_forced": False},
    ],
)
async def test_mapped_output_requires_one_completed_proxy_request(
    tmp_path: Path, monkeypatch, bad_audit: dict | None
):
    event = _event(tmp_path / "repo", tmp_path / "hub")
    event._trusted_codex_usage = {"api_calls": 0}
    event._trusted_codex_proxy_request_limit = 1
    if bad_audit is not None:
        event._trusted_codex_proxy_audit = {
            "request_count": 1,
            "request_limit": 1,
            "rejected_requests": 0,
            "response_completed": 1,
            "usage_response_count": 1,
            "usage_present": True,
            "store_forced": True,
            **bad_audit,
        }
    monkeypatch.setattr(
        _core, "_require_codex_spend_state_for_event", lambda *_args: object()
    )

    with pytest.raises(executor_map.ExecutorUnavailable, match="provider proxy audit"):
        await _core._complete_codex_spend_receipt(event, tmp_path)


@pytest.mark.asyncio
async def test_local_harness_receipt_accepts_complete_multi_request_audit(
    tmp_path: Path, monkeypatch
):
    from hermes_multitenancy.agent_real import harness_webui_runtime

    event = _event(tmp_path / "repo", tmp_path / "hub")
    event._trusted_codex_usage = {"api_calls": 0}
    limit = codex_provider_proxy.MAX_HARNESS_REQUESTS
    event._trusted_codex_proxy_request_limit = limit
    event._trusted_codex_proxy_audit = {
        "request_count": 2,
        "request_limit": limit,
        "rejected_requests": 0,
        "rejected_concurrent": 0,
        "budget_exhausted": False,
        "response_completed": 2,
        "usage_response_count": 2,
        "usage_present": True,
        "store_forced": True,
        "input_tokens": 5,
        "output_tokens": 3,
        "total_tokens": 8,
        "cache_read_tokens": 2,
    }
    monkeypatch.setattr(
        _core, "_require_codex_spend_state_for_event", lambda *_args: object()
    )
    monkeypatch.setattr(
        harness_webui_runtime, "require_event_admission", lambda *_args: object()
    )
    writes = []
    monkeypatch.setattr(
        _core,
        "_write_token_ledger_from_child",
        lambda _event, _profile, usage, **kwargs: writes.append((usage, kwargs)),
    )

    await _core._complete_codex_spend_receipt(event, tmp_path)

    assert writes == [
        (
            {
                "api_calls": 2,
                "input_tokens": 5,
                "output_tokens": 3,
                "total_tokens": 8,
                "cache_read_tokens": 2,
                "cache_write_tokens": 0,
                "budget_exhausted": False,
            },
            {"verified_codex": True},
        )
    ]


def _budget_audit(**overrides) -> dict:
    audit = {
        "request_count": codex_provider_proxy.MAX_HARNESS_REQUESTS,
        "request_limit": codex_provider_proxy.MAX_HARNESS_REQUESTS,
        "rejected_requests": 1,
        "rejected_over_limit": 1,
        "rejected_concurrent": 0,
        "budget_exhausted": True,
        "response_completed": codex_provider_proxy.MAX_HARNESS_REQUESTS,
        "usage_response_count": codex_provider_proxy.MAX_HARNESS_REQUESTS,
        "usage_present": True,
        "store_forced": True,
        "input_tokens": 5,
        "output_tokens": 3,
        "total_tokens": 8,
        "cache_read_tokens": 2,
    }
    audit.update(overrides)
    return audit


@pytest.mark.asyncio
async def test_budget_exhausted_harness_run_releases_its_output(
    tmp_path: Path, monkeypatch, caplog
):
    """事故 2026-09-03: 预算用尽的一轮曾被当成审计不完整,整轮产出被丢掉。"""
    from hermes_multitenancy.agent_real import harness_webui_runtime

    event = _event(tmp_path / "repo", tmp_path / "hub")
    event._trusted_codex_usage = {"api_calls": 0}
    event._trusted_codex_proxy_request_limit = codex_provider_proxy.MAX_HARNESS_REQUESTS
    event._trusted_codex_proxy_audit = _budget_audit()
    monkeypatch.setattr(
        _core, "_require_codex_spend_state_for_event", lambda *_args: object()
    )
    monkeypatch.setattr(
        harness_webui_runtime, "require_event_admission", lambda *_args: object()
    )
    # 打在真台账写入函数上：上一版打在被 monkeypatch 掉的
    # _write_token_ledger_from_child 上，而真实现按白名单逐键取参、把
    # budget_exhausted 扔了，用例照样绿。
    from hermes_multitenancy import token_usage_ledger

    monkeypatch.setenv("HERMES_TOKEN_USAGE_LEDGER_ENABLED", "1")
    monkeypatch.setenv(
        "HERMES_TOKEN_USAGE_LEDGER_PATH", str(tmp_path / "token-usage.jsonl")
    )
    writes: list[dict] = []
    monkeypatch.setattr(
        token_usage_ledger, "append_token_usage", lambda **kwargs: writes.append(kwargs)
    )

    with caplog.at_level(
        logging.INFO, logger="hermes_multitenancy.agent_real._core"
    ):
        await _core._complete_codex_spend_receipt(event, tmp_path)

    assert writes[0]["budget_exhausted"] is True
    assert writes[0]["api_calls"] == codex_provider_proxy.MAX_HARNESS_REQUESTS
    assert writes[0]["total_tokens"] == 8
    # 预算用尽这一轮必须留下可运维检索的日志线索,而不是静默放行。
    assert "budget_exhausted requests=" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_audit",
    [
        {"rejected_concurrent": 1, "rejected_requests": 2},
        {"budget_exhausted": False},
        {"usage_present": False},
        {"response_completed": 1},
        {"store_forced": False},
        # 伪造派生布尔但计数器对不上 —— 放行只认计数器,这些一律 fail-closed。
        {"rejected_over_limit": 0},
        {"rejected_over_limit": None},
        {"rejected_requests": 3},
        {"request_count": codex_provider_proxy.MAX_HARNESS_REQUESTS - 1},
    ],
)
async def test_budget_pass_through_never_covers_a_broken_audit(
    tmp_path: Path, monkeypatch, bad_audit: dict
):
    from hermes_multitenancy.agent_real import harness_webui_runtime

    event = _event(tmp_path / "repo", tmp_path / "hub")
    event._trusted_codex_usage = {"api_calls": 0}
    event._trusted_codex_proxy_request_limit = codex_provider_proxy.MAX_HARNESS_REQUESTS
    event._trusted_codex_proxy_audit = _budget_audit(**bad_audit)
    monkeypatch.setattr(
        _core, "_require_codex_spend_state_for_event", lambda *_args: object()
    )
    monkeypatch.setattr(
        harness_webui_runtime, "require_event_admission", lambda *_args: object()
    )

    with pytest.raises(executor_map.ExecutorUnavailable, match="provider proxy audit"):
        await _core._complete_codex_spend_receipt(event, tmp_path)


@pytest.mark.asyncio
async def test_mapped_run_env_scope_binds_workspace_codex_home_and_one_key(
    tmp_path: Path, monkeypatch
):
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    approval = tmp_path / "approval"
    approval.mkdir()
    repo = _git_repo(tmp_path / "mall", "README.md")
    hub = _git_repo(tmp_path / "KepSpecHub", "SPEC.md")
    plugin = tmp_path / "kep-plugin"
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / "skills" / "using-server-dev").mkdir(parents=True)
    (plugin / ".codex-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "keep-server-dev",
                "version": "1.0.0",
                "skills": "./skills/",
            }
        ),
        encoding="utf-8",
    )
    (plugin / "skills" / "using-server-dev" / "SKILL.md").write_text(
        "---\nname: using-server-dev\ndescription: Server workflow.\n---\n\n# step 0\n",
        encoding="utf-8",
    )
    map_path = tmp_path / "executor-map.yaml"
    map_path.write_text("kep-server: codex_app_server\n", encoding="utf-8")
    event = _event(repo, hub)
    key = "sk-employee-runtime-key-123456"

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    _bind_readonly_gitlab(tmp_path, "alice", "ou_alice")
    monkeypatch.setenv("HERMES_EXECUTOR_MAP", str(map_path))
    monkeypatch.setattr(
        _core,
        "_codex_expert_plugin_dir",
        lambda _profile, _expert: plugin,
    )
    monkeypatch.setattr(
        _core,
        "_codex_model_and_base_url",
        lambda _event, _profile: ("gpt-5.6-terra", "https://litellm.example/v1"),
    )
    monkeypatch.setattr(
        billing_identity,
        "runtime_env_for_billing_metadata",
        lambda _metadata: {
            "HERMES_LITELLM_RUNTIME_API_KEY": key,
            "HERMES_LITELLM_RUNTIME_BASE_URL": "https://litellm.example/v1",
            "HERMES_LITELLM_RUNTIME_EMPLOYEE_ID": "alice",
        },
    )

    @contextlib.contextmanager
    def no_lark_broker(*_args, **_kwargs):
        yield {}

    monkeypatch.setattr(_core, "_lark_cli_auth_broker_scope", no_lark_broker)
    _seal_webui_event(event, profile, "ou_alice")
    setattr(
        event,
        _core._BROKER_ROLE_OVERRIDE_EVENT_KEY,
        {
            _core._BROKER_ROLE_OVERRIDE_EXPERT_ID_KEY: "kep-server",
            _core._BROKER_ROLE_OVERRIDE_BLOCK_KEY: "SEALED SERVER EXPERT",
            _core._BROKER_ROLE_OVERRIDE_SKILLS_KEY: ["using-server-dev"],
        },
    )
    _attach_local_codex_evidence(event, profile, api_key=key)

    async def inspect_env():
        workspace_obj = agent_real._bind_codex_run_workspace(event, profile)

        def must_not_prepare_twice(*_args, **_kwargs):
            raise AssertionError("prepared workspace must be reused")

        monkeypatch.setattr(run_workspace, "prepare", must_not_prepare_twice)
        with agent_real._aiagent_subprocess_env_scope(
            event,
            profile,
            approval_dir=approval,
            codex_workspace=workspace_obj,
        ) as env:
            blocked = {
                name
                for name in env
                if name.startswith("GITLAB_")
                or name.startswith("_HERMES_FORCE_GIT")
                or name == "GIT_CONFIG_COUNT"
                or name.startswith("GIT_CONFIG_KEY_")
                or name.startswith("GIT_CONFIG_VALUE_")
            }
            assert blocked == set()
            assert env["GIT_CONFIG_GLOBAL"] == os.devnull
            assert env["GIT_CONFIG_SYSTEM"] == os.devnull
            assert env["GIT_TERMINAL_PROMPT"] == "0"
            workflow = profile / "workspace" / "runs" / "workflow-1"
            assert event.raw_event["workspace"] == "runs/workflow-1/repo"
            assert (workflow / "repo" / ".git").exists()
            assert (workflow / "KepSpecHub" / ".git").exists()
            assert env[_core.EXECUTOR_RUNTIME_ENV] == "codex_app_server"
            assert env["HERMES_CODEX_HOST_TOOLS"] == "lark_cli"
            assert env[_core.CODEX_RUNTIME_KEY_ENV].startswith("hcx_")
            assert env[_core.CODEX_RUNTIME_KEY_ENV] != key
            assert key not in env.values()
            assert env[_core.CODEX_PROXY_BASE_URL_ENV].startswith("http://127.0.0.1:")
            assert env["CODEX_HOME"] == str(workflow / "codex-home")
            assert env["KEP_SPEC_HUB_DIR"] == str(workflow / "KepSpecHub")
            assert env["KEP_WORKSPACE_DIR"] == str(workflow)
            assert "actor-read-token" not in env.values()
            config = (workflow / "codex-home" / "config.toml").read_text("utf-8")
            assert 'env_key = "CODEX_RUNTIME_KEY"' in config
            assert "plugins = false" in config
            assert "developer_instructions = " in config
            assert 'lark_cli` tool with `mode=\\"script\\"' in config
            assert key not in config
            assert key not in "\n".join(
                p.read_text("utf-8", errors="replace")
                for p in workflow.rglob("*")
                if p.is_file() and p.name != ".git-credentials"
            )
            return "ok"

    assert await inspect_env() == "ok"

    workflow = profile / "workspace" / "runs" / "workflow-1"
    credential_file = workflow / ".git-credentials"
    assert credential_file.stat().st_mode & 0o777 == 0o600
    assert "actor-read-token" in credential_file.read_text("utf-8")


def test_mapped_proxy_rejects_billing_endpoint_drift_before_start(
    tmp_path: Path, monkeypatch
):
    profile = tmp_path / "profiles" / "alice"
    (profile / "workspace").mkdir(parents=True)
    approval = tmp_path / "approval"
    approval.mkdir()
    event = _event(tmp_path / "repo", tmp_path / "hub")
    monkeypatch.setattr(_core, "_codex_run_workspace_for_event", lambda *_a: object())
    monkeypatch.setattr(
        billing_identity,
        "runtime_env_for_billing_metadata",
        lambda _metadata: {
            "HERMES_LITELLM_RUNTIME_API_KEY": "sk-actor-key",
            "HERMES_LITELLM_RUNTIME_BASE_URL": "https://litellm.example/evil",
            "HERMES_LITELLM_RUNTIME_EMPLOYEE_ID": "alice",
        },
    )
    monkeypatch.setattr(
        _core,
        "_codex_model_and_base_url",
        lambda *_a: ("gpt-5.6", "https://litellm.example/v1"),
    )

    with pytest.raises(RuntimeError, match="unapproved LiteLLM endpoint"):
        with _core._aiagent_subprocess_env_scope(
            event, profile, approval_dir=approval
        ):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("run_mode", ["nonstream", "stream"])
async def test_mapped_run_crosses_os_child_with_bound_repo_and_codex_runtime(
    tmp_path: Path, monkeypatch, run_mode: str
):
    """The public run seam carries W0 state across the real OS-child boundary."""
    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "sunke"
    (profile / "workspace").mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "\n".join(
            [
                "model:",
                "  default: custom:litellm-sre/gpt-5.6-terra",
                "  base_url: https://litellm.example/v1",
                "custom_providers:",
                "  - name: litellm-sre",
                "    base_url: https://litellm.example/v1",
                "    api_key: profile-key-must-not-win",
            ]
        ),
        encoding="utf-8",
    )
    repo = _git_repo(tmp_path / "mall", "README.md")
    hub = _git_repo(tmp_path / "real-KepSpecHub", "SPEC.md")

    plugin = shared / ".hermes-plugin-managed" / ".sources" / "keep-server-dev"
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / "skills" / "using-server-dev").mkdir(parents=True)
    (plugin / "agent.md").write_text("Analyze server requirements.", encoding="utf-8")
    (plugin / "skills" / "using-server-dev" / "SKILL.md").write_text(
        "---\nname: using-server-dev\ndescription: Server workflow.\n---\n\n# Step 0\n",
        encoding="utf-8",
    )
    source_script = plugin / "skills" / "using-server-dev" / "scripts" / "probe.py"
    source_script.parent.mkdir()
    source_script.write_text("print('trusted')\n", encoding="utf-8")
    (plugin / ".codex-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "keep-server-dev",
                "version": "1.0.0",
                "skills": "./skills/",
            }
        ),
        encoding="utf-8",
    )
    managed = shared / ".hermes-plugin-managed"
    managed.mkdir(exist_ok=True)
    (managed / "keep-server-dev.json").write_text(
        json.dumps(
            {
                "plugin_id": "keep-server-dev-plugin",
                "repo": str(plugin),
                "status": "active",
                "audience": {"profiles": ["sunke"]},
                "experts": [
                    {
                        "id": "kep-server",
                        "name": "Server expert",
                        "agent_md": "agent.md",
                        "skills": ["using-server-dev"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    executor_map = tmp_path / "executors.yaml"
    executor_map.write_text("kep-server: codex_app_server\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex.chmod(0o755)

    fake_modules = tmp_path / "fake-modules"
    fake_modules.mkdir()
    (fake_modules / "run_agent.py").write_text(
        """
import json
import os
import subprocess
import urllib.request

class AIAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.session_api_calls = 0
        self._api_call_count = 0
        self._session_input_tokens = 1
        self._session_output_tokens = 1
        self._session_cache_read_tokens = 0
        self._session_cache_write_tokens = 0

    def run_conversation(self, **_kwargs):
        request = urllib.request.Request(
            self.kwargs["base_url"] + "/responses",
            data=b'{"stream":true}',
            headers={
                "Authorization": "Bearer " + self.kwargs["api_key"],
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            response.read()
        return {
            "final_response": json.dumps({
                "api_mode": self.kwargs.get("api_mode"),
                "cwd": os.getcwd(),
                "terminal_cwd": os.environ.get("TERMINAL_CWD"),
                "kep_agent_mode": os.environ.get("KEP_AGENT_MODE"),
                "kep_workspace_dir": os.environ.get("KEP_WORKSPACE_DIR"),
                "kep_spec_hub_dir": os.environ.get("KEP_SPEC_HUB_DIR"),
                "forced_kep_workspace_dir": os.environ.get("_HERMES_FORCE_KEP_WORKSPACE_DIR"),
                "forced_kep_spec_hub_dir": os.environ.get("_HERMES_FORCE_KEP_SPEC_HUB_DIR"),
                "billing_alias_present": "HERMES_LITELLM_RUNTIME_API_KEY" in os.environ,
                "git_config_global": os.environ.get("GIT_CONFIG_GLOBAL"),
                "git_config_system": os.environ.get("GIT_CONFIG_SYSTEM"),
                "git_terminal_prompt": os.environ.get("GIT_TERMINAL_PROMPT"),
                "codex_key_present": bool(os.environ.get("CODEX_RUNTIME_KEY")),
                "raw_billing_key_present": "sk-employee-runtime-key-cross-child-123456" in os.environ.values(),
                "proxy_base_is_loopback": str(self.kwargs.get("base_url") or "").startswith("http://127.0.0.1:"),
                "api_key_matches_codex_key": (
                    self.kwargs.get("api_key") == os.environ.get("CODEX_RUNTIME_KEY")
                ),
                "gitlab_secret_env_names": sorted(
                    name for name, value in os.environ.items()
                    if value == "actor-read-token"
                ),
                "gitlab_credential_env_names": sorted(
                    name for name in os.environ
                    if name.startswith("GITLAB_")
                    or name.startswith("_HERMES_FORCE_GIT")
                    or name == "GIT_CONFIG_COUNT"
                    or name.startswith("GIT_CONFIG_KEY_")
                    or name.startswith("GIT_CONFIG_VALUE_")
                ),
                "tool_gitlab_credential_env_names": sorted(
                    line.split("=", 1)[0]
                    for line in subprocess.check_output(["env"], text=True).splitlines()
                    if line.startswith("GITLAB_")
                    or line.startswith("_HERMES_FORCE_GIT")
                    or line.startswith("GIT_CONFIG_COUNT=")
                    or line.startswith("GIT_CONFIG_KEY_")
                    or line.startswith("GIT_CONFIG_VALUE_")
                ),
            }, sort_keys=True),
            "completed": True,
        }

    def cleanup(self):
        pass
""".lstrip(),
        encoding="utf-8",
    )
    (fake_modules / "sitecustomize.py").write_text(
        """
import importlib.util
import pathlib
import sys

path = pathlib.Path(__file__).with_name("run_agent.py")
spec = importlib.util.spec_from_file_location("run_agent", path)
module = importlib.util.module_from_spec(spec)
sys.modules["run_agent"] = module
spec.loader.exec_module(module)
""".lstrip(),
        encoding="utf-8",
    )

    event = _event(repo, hub)
    _seal_webui_event(event, profile, "ou_alice")
    event.raw_event["metadata"].update(
        {
            "model": "gpt-5.6-terra",
            "provider": "custom:litellm-sre",
            "litellm_billing_enforced": True,
            "litellm_billing_user_id": "payer-sunke",
            "litellm_billing_employee_user_id": "sunke",
            "litellm_billing_base_url": "https://litellm.example/v1",
        }
    )
    key = "sk-employee-runtime-key-cross-child-123456"
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    monkeypatch.setattr(
        "hermes_multitenancy.single_actor_spend_receipt._SPEND_POLL_TIMEOUT_S", 0
    )
    _bind_readonly_gitlab(shared, "sunke", "ou_alice")
    _attach_local_codex_evidence(event, profile, api_key=key)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    monkeypatch.setenv("HERMES_EXECUTOR_MAP", str(executor_map))
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")]))
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join([str(fake_modules), os.environ.get("PYTHONPATH", "")]),
    )
    for name in (
        "HERMES_USE_SANDBOX",
        "HERMES_MULTITENANCY_REQUIRE_SANDBOX",
        "HERMES_MULTITENANCY_STRICT_CONTEXT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        billing_identity,
        "runtime_env_for_billing_metadata",
        lambda _metadata: {
            "HERMES_LITELLM_RUNTIME_API_KEY": key,
            "HERMES_LITELLM_RUNTIME_BASE_URL": "https://litellm.example/v1",
            "HERMES_LITELLM_RUNTIME_EMPLOYEE_ID": "sunke",
        },
    )

    @contextlib.contextmanager
    def no_lark_broker(*_args, **_kwargs):
        yield {}

    monkeypatch.setattr(_core, "_lark_cli_auth_broker_scope", no_lark_broker)
    upstream_authorizations = []

    class UpstreamResponse:
        status = 200
        reason = "OK"
        headers = {"Content-Type": "text/event-stream"}

        def __init__(self):
            self.chunks = iter(
                [
                    b'data:{"type":"response.completed","response":{"id":"litellm-real-request","usage":{"input_tokens":1}}}\r\n\r\n'
                ]
            )

        def read(self, _size=-1):
            return next(self.chunks, b"")

    class UpstreamConnection:
        def request(self, _method, _path, *, body, headers):
            assert json.loads(body) == {"stream": True, "store": False}
            upstream_authorizations.append(headers["Authorization"])

        def getresponse(self):
            return UpstreamResponse()

        def close(self):
            return None

    monkeypatch.setattr(
        codex_provider_proxy, "_open_upstream", lambda _origin: UpstreamConnection()
    )

    async def dispatch():
        if run_mode == "stream":
            chunks = [
                str(payload)
                async for kind, payload in agent_real.stream_run_agent(event, profile)
                if kind == "content"
            ]
            return "".join(chunks)
        return await agent_real.real_run_agent(event, profile)

    observed = json.loads(await dispatch())
    assert upstream_authorizations == [f"Bearer {key}"]
    workflow = profile / "workspace" / "runs" / "workflow-1"
    expected_repo = workflow / "repo"
    assert observed == {
        "api_key_matches_codex_key": True,
        "api_mode": "codex_app_server",
        "billing_alias_present": False,
        "codex_key_present": True,
        "proxy_base_is_loopback": True,
        "raw_billing_key_present": False,
        "cwd": str(expected_repo),
        "forced_kep_spec_hub_dir": str(workflow / "KepSpecHub"),
        "forced_kep_workspace_dir": str(workflow),
        "gitlab_secret_env_names": [],
        "gitlab_credential_env_names": [],
        "git_config_global": os.devnull,
        "git_config_system": os.devnull,
        "git_terminal_prompt": "0",
        "kep_agent_mode": "online",
        "kep_spec_hub_dir": str(workflow / "KepSpecHub"),
        "kep_workspace_dir": str(workflow),
        "terminal_cwd": str(expected_repo),
        "tool_gitlab_credential_env_names": [],
    }
    assert subprocess.run(
        ["git", "-C", str(expected_repo), "rev-parse", "--verify", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert subprocess.run(
        ["git", "-C", str(workflow / "KepSpecHub"), "rev-parse", "--verify", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    config = (workflow / "codex-home" / "config.toml").read_text("utf-8")
    assert 'env_key = "CODEX_RUNTIME_KEY"' in config
    assert "plugins = false" in config
    assert "developer_instructions = " in config
    assert 'lark_cli` tool with `mode=\\"script\\"' in config
    assert (workflow / "codex-home" / "plugins" / "keep-server-dev").is_dir()
    assert (workflow / "codex-home" / "skills" / "using-server-dev").is_dir()
    from hermes_multitenancy.lark_cli_tool import _resolve_skill_script

    materialized_script = (
        workflow / "codex-home" / "skills" / "using-server-dev" / "scripts" / "probe.py"
    )
    resolved_script, resolve_error = _resolve_skill_script(
        str(materialized_script),
        {
            "HERMES_HOME": str(profile),
            "HERMES_PROFILE": profile.name,
            "HERMES_SHARED_HOME": str(shared),
            "CODEX_HOME": str(workflow / "codex-home"),
            "HERMES_CODEX_PLUGIN_SOURCE": str(plugin),
        },
    )
    assert resolve_error is None
    assert resolved_script == source_script.resolve()
    assert key not in config
    assert key not in json.dumps(observed)
    receipt = event.trusted_single_actor_spend_receipt
    assert receipt["request_count"] == 1
    assert receipt["spend_delta"] == "0.001"
    assert receipt["signature"]
    serialized_receipt = json.dumps(receipt)
    assert key not in serialized_receipt
    assert "ou_alice" not in serialized_receipt
    assert "litellm-real-request" not in serialized_receipt
