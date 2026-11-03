import hashlib
import json
from pathlib import Path

import pytest


def _digest(manifest: dict) -> str:
    raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _manifest(version: str, *, scopes: list[str] | None = None) -> dict:
    component_digest = hashlib.sha256(f"skill-{version}".encode()).hexdigest()
    return {
        "compatibility": ">=0.1,<1",
        "required_scopes": scopes or ["read:user"],
        "capabilities": ["get_me", "search_repositories"],
        "components": [
            {"kind": "remote_mcp", "id": "github", "endpoint": "https://api.githubcopilot.com/mcp/"},
            {"kind": "skill", "id": "github-mcp", "version": version, "digest": component_digest},
        ],
    }


def test_release_pin_canary_promote_rollback_and_scope_reauth_keep_one_credential(tmp_path: Path):
    from hermes_multitenancy.connector_releases import ConnectorReleaseStore
    from hermes_multitenancy.credentials import CredentialStore

    db = tmp_path / "multitenancy.db"
    credentials = CredentialStore(db, encryption_key="test-key")
    credentials.put_credential(
        profile_name="alice",
        subject_id="subject-alice",
        provider="github",
        secret_kind="pat",
        payload={"token": "secret-that-must-not-copy"},
        scopes=["read:user"],
    )
    before = credentials.get_secret_for_runtime_with_updated_at(
        profile_name="alice", subject_id="subject-alice", provider="github", secret_kind="pat"
    )
    credentials.close()

    releases = ConnectorReleaseStore(db)
    v1 = _manifest("1.0.0")
    v2 = _manifest("2.0.0")
    releases.publish("github-mcp", "1.0.0", _digest(v1), v1)
    releases.publish("github-mcp", "2.0.0", _digest(v2), v2)
    installed = releases.install(
        profile_name="alice",
        subject_id="subject-alice",
        connector_id="github-mcp",
        version="1.0.0",
        credential_provider="github",
        credential_kind="pat",
    )
    assert installed["current_version"] == "1.0.0"
    assert installed["state"] == "active"

    releases.stage("alice", "subject-alice", "github-mcp", "2.0.0")
    with pytest.raises(PermissionError, match="canary"):
        releases.promote("alice", "subject-alice", "github-mcp", canary_ok=False)
    promoted = releases.promote("alice", "subject-alice", "github-mcp", canary_ok=True)
    assert (promoted["current_version"], promoted["previous_version"]) == ("2.0.0", "1.0.0")
    rolled_back = releases.rollback("alice", "subject-alice", "github-mcp")
    assert (rolled_back["current_version"], rolled_back["previous_version"]) == ("1.0.0", "2.0.0")

    v3 = _manifest("3.0.0", scopes=["read:user", "repo:write"])
    releases.publish("github-mcp", "3.0.0", _digest(v3), v3)
    releases.stage("alice", "subject-alice", "github-mcp", "3.0.0")
    needs_auth = releases.promote("alice", "subject-alice", "github-mcp", canary_ok=True)
    assert needs_auth["state"] == "needs_auth"
    assert needs_auth["current_version"] == "1.0.0"
    assert needs_auth["staged_version"] == "3.0.0"
    releases.close()

    credentials = CredentialStore(db, encryption_key="test-key")
    after = credentials.get_secret_for_runtime_with_updated_at(
        profile_name="alice", subject_id="subject-alice", provider="github", secret_kind="pat"
    )
    credentials.close()
    assert after == before
    assert b"secret-that-must-not-copy" not in db.read_bytes()


def test_release_rejects_mutation_unpinned_components_and_arbitrary_commands(tmp_path: Path):
    from hermes_multitenancy.connector_releases import ConnectorReleaseStore

    releases = ConnectorReleaseStore(tmp_path / "multitenancy.db")
    manifest = _manifest("1.0.0")
    with pytest.raises(ValueError, match="digest"):
        releases.publish("github-mcp", "1.0.0", "0" * 64, manifest)

    unsafe = {
        **manifest,
        "components": [{"kind": "cli", "id": "github", "command": "npx -y github@latest"}],
    }
    with pytest.raises(ValueError, match="pinned"):
        releases.publish("github-mcp", "1.0.0", _digest(unsafe), unsafe)

    releases.publish("github-mcp", "1.0.0", _digest(manifest), manifest)
    changed = {**manifest, "capabilities": ["create_issue"]}
    with pytest.raises(ValueError, match="immutable"):
        releases.publish("github-mcp", "1.0.0", _digest(changed), changed)
    releases.close()
