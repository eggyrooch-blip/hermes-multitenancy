from pathlib import Path

import pytest


def test_materialize_shared_group_token_to_profile_workspaces(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.credential_materializer import materialize_credentials
    from hermes_multitenancy.credentials import CredentialStore

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    profiles = shared / "profiles"
    for name in ("alice", "bob", "carol"):
        (profiles / name).mkdir(parents=True)
    (shared / "lists").mkdir(parents=True)
    (shared / "lists" / "kep-prd-analysis.txt").write_text(
        "alice\n# comment\nbob\nmissing\n",
        encoding="utf-8",
    )
    (shared / "credential-materialization.yaml").write_text(
        """
credentials:
  - subject_id: kep-prd-analysis
    provider: gitlab
    secret_kind: token
    target: workspace/credentials/gitlab.token
    profile_file: lists/kep-prd-analysis.txt
    profiles: [carol]
""",
        encoding="utf-8",
    )
    store = CredentialStore(shared / "multitenancy.db")
    try:
        store.put_credential(
            profile_name="__shared__",
            subject_id="kep-prd-analysis",
            provider="gitlab",
            secret_kind="token",
            payload={"token": "glpat-test"},
        )
    finally:
        store.close()

    stats = materialize_credentials(shared_home=shared)

    assert stats["entries"] == 1
    assert stats["profiles_targeted"] == 4
    assert stats["written"] == 3
    assert stats["skipped_profiles"] == 1
    for name in ("alice", "bob", "carol"):
        token_file = profiles / name / "workspace" / "credentials" / "gitlab.token"
        assert token_file.read_text(encoding="utf-8") == "glpat-test\n"
        assert token_file.stat().st_mode & 0o777 == 0o600


def test_materialize_wildcard_profiles_targets_active_user_routes_not_group_children(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.credential_materializer import materialize_credentials
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.routing import RoutingTable

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    profiles = shared / "profiles"
    for name in ("alice", "bob", "group_chat", "inactive"):
        (profiles / name).mkdir(parents=True)
    (shared / "credential-materialization.yaml").write_text(
        """
credentials:
  - subject_id: kep-prd-skills
    provider: gitlab
    secret_kind: token
    target: workspace/credentials/gitlab.token
    env: GITLAB_TOKEN
    profiles: ["*"]
""",
        encoding="utf-8",
    )

    table = RoutingTable(shared / "multitenancy.db")
    try:
        table.upsert(user_id="alice", profile_name="alice", open_id="ou_alice")
        table.upsert(user_id="bob", profile_name="bob", open_id="ou_bob")
        table.upsert_group(chat_id="oc_group", profile_name="group_chat", owner_open_id="ou_alice")
        table.upsert(user_id="inactive", profile_name="inactive", open_id="ou_inactive")
        table.soft_delete("inactive")
    finally:
        table.close()

    store = CredentialStore(shared / "multitenancy.db")
    try:
        store.put_credential(
            profile_name="__shared__",
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
            payload={"token": "opaque-gitlab-token"},
        )
    finally:
        store.close()

    stats = materialize_credentials(shared_home=shared)

    assert stats["profiles_targeted"] == 2
    assert stats["written"] == 2
    for name in ("alice", "bob"):
        assert (profiles / name / "workspace" / "credentials" / "gitlab.token").read_text(encoding="utf-8") == (
            "opaque-gitlab-token\n"
        )
    assert not (profiles / "group_chat" / "workspace" / "credentials" / "gitlab.token").exists()
    assert not (profiles / "inactive" / "workspace" / "credentials" / "gitlab.token").exists()


def test_materialize_credentials_no_config_is_noop(tmp_path: Path):
    from hermes_multitenancy.credential_materializer import materialize_credentials

    stats = materialize_credentials(shared_home=tmp_path)

    assert stats["config_path"] is None
    assert stats["entries"] == 0
    assert stats["written"] == 0


def test_materialize_credentials_dry_run_does_not_write(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.credential_materializer import materialize_credentials
    from hermes_multitenancy.credentials import CredentialStore

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "alice"
    profile.mkdir(parents=True)
    (shared / "credential-materialization.yaml").write_text(
        """
credentials:
  - subject_id: team-token
    provider: gitlab
    secret_kind: token
    target: workspace/credentials/gitlab.token
    profiles: [alice]
""",
        encoding="utf-8",
    )
    store = CredentialStore(shared / "multitenancy.db")
    try:
        store.put_credential(
            profile_name="__shared__",
            subject_id="team-token",
            provider="gitlab",
            secret_kind="token",
            payload={"token": "glpat-test"},
        )
    finally:
        store.close()

    stats = materialize_credentials(shared_home=shared, dry_run=True)

    assert stats["would_write"] == 1
    assert not (profile / "workspace" / "credentials" / "gitlab.token").exists()


def test_materialize_credentials_rejects_profile_escape_target(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.credential_materializer import materialize_credentials

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    shared.mkdir()
    (shared / "credential-materialization.yaml").write_text(
        """
credentials:
  - subject_id: bad
    provider: gitlab
    target: ../.env
    profiles: [alice]
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="target must not escape"):
        materialize_credentials(shared_home=shared)
