from pathlib import Path

import pytest

from hermes_multitenancy.credential_materializer import git_auth_env, git_identity_env


def _expected_credential_env(profile: str, token_env: dict[str, str]) -> dict[str, str]:
    """Identity is seeded first, then the token, then git auth extends the count
    (see test_git_identity_env.py for the seeding contract)."""
    env = git_identity_env({}, profile=profile)
    env.update(token_env)
    env.update(git_auth_env(env))
    return env


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


def _self_lane_home(tmp_path: Path, *, personal_holders: tuple[str, ...]) -> Path:
    """A ``vault_profile: __self__`` setup: alice/bob targeted, some with own token."""
    from hermes_multitenancy.credentials import CredentialStore

    shared = tmp_path / ".hermes"
    profiles = shared / "profiles"
    for name in ("alice", "bob"):
        (profiles / name).mkdir(parents=True)
    (shared / "credential-materialization.yaml").write_text(
        """
credentials:
  - subject_id: kep-prd-skills
    provider: gitlab
    secret_kind: token
    target: workspace/credentials/gitlab.token
    env: GITLAB_TOKEN
    vault_profile: __self__
    profiles: [alice, bob]
""",
        encoding="utf-8",
    )
    store = CredentialStore(shared / "multitenancy.db")
    try:
        store.put_credential(
            profile_name="__shared__",
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
            payload={"token": "global-token"},
        )
        for holder in personal_holders:
            store.put_credential(
                profile_name=holder,
                subject_id="kep-prd-skills",
                provider="gitlab",
                secret_kind="token",
                payload={"token": f"personal-{holder}"},
            )
    finally:
        store.close()
    return shared


def test_self_lane_personal_token_is_env_only_and_never_written_to_disk(monkeypatch, tmp_path: Path):
    """Done line, disk half: a user who supplied a token gets NO token file."""
    from hermes_multitenancy.credential_materializer import materialize_credentials

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _self_lane_home(tmp_path, personal_holders=("alice",))
    profiles = shared / "profiles"

    stats = materialize_credentials(shared_home=shared)

    # alice supplied her own token -> env-only, nothing on disk.
    assert stats["personal_env_only"] == 1
    assert not (profiles / "alice" / "workspace" / "credentials" / "gitlab.token").exists()
    # bob did not -> unchanged shared-credential behaviour, file still written.
    assert stats["written"] == 1
    assert (profiles / "bob" / "workspace" / "credentials" / "gitlab.token").read_text(
        encoding="utf-8"
    ) == "global-token\n"


def test_self_lane_env_prefers_personal_token_and_falls_back_to_shared(monkeypatch, tmp_path: Path):
    """Done line, env half: personal token wins; a user without one still gets global."""
    from hermes_multitenancy.agent_real._core import _credential_env_for_aiagent

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _self_lane_home(tmp_path, personal_holders=("alice",))
    profiles = shared / "profiles"

    for profile, token in (("alice", "personal-alice"), ("bob", "global-token")):
        # git auth env rides along with whichever token is live.
        assert _credential_env_for_aiagent(profiles / profile) == _expected_credential_env(
            profile, {"GITLAB_TOKEN": token}
        )


def test_self_lane_file_and_env_never_disagree_about_which_token_is_live(monkeypatch, tmp_path: Path):
    """The class-sweep guard: both write sites must resolve the SAME record.

    Patching only one of the two ``vault_profile`` resolution sites yields a
    profile whose on-disk token and injected env token are different tokens —
    the exact silent split this test exists to catch.
    """
    from hermes_multitenancy.agent_real._core import _credential_env_for_aiagent
    from hermes_multitenancy.credential_materializer import materialize_credentials

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _self_lane_home(tmp_path, personal_holders=("alice",))
    profiles = shared / "profiles"

    materialize_credentials(shared_home=shared)

    for name in ("alice", "bob"):
        token_file = profiles / name / "workspace" / "credentials" / "gitlab.token"
        in_env = _credential_env_for_aiagent(profiles / name).get("GITLAB_TOKEN")
        if token_file.exists():
            # Shared lane: the materializer wrote a file, so the env must carry
            # that same shared token.
            assert token_file.read_text(encoding="utf-8").strip() == in_env == "global-token", (
                f"{name}: disk={token_file.read_text(encoding='utf-8').strip()!r} env={in_env!r}"
            )
        else:
            # Personal lane: the materializer skipped the write *because* it
            # resolved a personal record — the env must have resolved the same
            # way. A file-absent/global-env pair is the silent split.
            assert in_env == f"personal-{name}", (
                f"{name}: no token file (personal lane) but env={in_env!r} — "
                "the two resolution sites diverged"
            )


def test_switching_to_personal_retires_that_users_legacy_token_file(monkeypatch, tmp_path: Path):
    """Production shape: the user ALREADY has a shared-token file on disk.

    Skipping the write is not enough — the stale file leaves their disk holding
    the global token while their env holds their own, the exact split this
    design exists to prevent, and keeps a broad credential readable in a profile
    that no longer needs it. (The first version of the parity test omitted this
    pre-existing file and therefore passed for the wrong reason.)
    """
    from hermes_multitenancy.agent_real._core import _credential_env_for_aiagent
    from hermes_multitenancy.credential_materializer import materialize_credentials

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _self_lane_home(tmp_path, personal_holders=("alice",))
    profiles = shared / "profiles"

    # Pre-existing legacy file, exactly as the 1426 live profiles have it.
    legacy = profiles / "alice" / "workspace" / "credentials"
    legacy.mkdir(parents=True)
    (legacy / "gitlab.token").write_text("global-token\n", encoding="utf-8")

    stats = materialize_credentials(shared_home=shared)

    assert stats["stale_files_removed"] == 1
    assert not (legacy / "gitlab.token").exists()
    assert _credential_env_for_aiagent(profiles / "alice")["GITLAB_TOKEN"] == "personal-alice"
    # bob never switched — his file must survive untouched.
    assert (profiles / "bob" / "workspace" / "credentials" / "gitlab.token").read_text(
        encoding="utf-8"
    ) == "global-token\n"


def test_expired_personal_token_is_not_injected_and_does_not_fall_back(monkeypatch, tmp_path: Path):
    """Expiry must bite at runtime, not only in the status card.

    Falling back to the shared credential here would quietly re-escalate the
    user to the broad admin token they opted out of — worse than no token.
    """
    from hermes_multitenancy.agent_real._core import _credential_env_for_aiagent
    from hermes_multitenancy.credentials import CredentialStore

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _self_lane_home(tmp_path, personal_holders=())
    profiles = shared / "profiles"
    store = CredentialStore(shared / "multitenancy.db")
    try:
        store.put_credential(
            profile_name="alice",
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
            payload={"token": "personal-expired"},
            expires_at=1000,  # long past
        )
    finally:
        store.close()

    env = _credential_env_for_aiagent(profiles / "alice")
    assert "GITLAB_TOKEN" not in env, "expired personal token must not be injected"
    assert "global-token" not in env.values(), "must NOT silently re-escalate to the shared token"


def test_env_extra_ships_companion_vars_only_when_the_credential_resolved(monkeypatch, tmp_path: Path):
    """GITLAB_HOST rides along with the token — but never without it.

    A profile carrying GITLAB_HOST and no GITLAB_TOKEN would make glab look
    configured while every call 401s, which is worse than looking unconfigured.
    """
    from hermes_multitenancy.agent_real._core import _credential_env_for_aiagent
    from hermes_multitenancy.credentials import CredentialStore

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    profiles = shared / "profiles"
    for name in ("alice", "bob"):
        (profiles / name).mkdir(parents=True)
    (shared / "credential-materialization.yaml").write_text(
        """
credentials:
  - subject_id: kep-prd-skills
    provider: gitlab
    secret_kind: token
    env: GITLAB_TOKEN
    env_extra:
      GITLAB_HOST: gitlab.example.com
      "bad name": ignored
    vault_profile: __self__
    profiles: [alice, bob]
""",
        encoding="utf-8",
    )
    store = CredentialStore(shared / "multitenancy.db")
    try:
        store.put_credential(
            profile_name="alice",
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
            payload={"token": "personal-alice"},
        )
    finally:
        store.close()

    alice = _credential_env_for_aiagent(profiles / "alice")
    assert alice == _expected_credential_env(
        "alice", {"GITLAB_TOKEN": "personal-alice", "GITLAB_HOST": "gitlab.example.com"}
    )

    # bob has neither a personal record nor a shared one -> no token, and
    # therefore no companion vars either; only the seeded commit identity stays.
    assert _credential_env_for_aiagent(profiles / "bob") == git_identity_env({}, profile="bob")


def test_default_shared_lane_is_untouched_by_the_self_marker(monkeypatch, tmp_path: Path):
    """Regression guard: entries without ``vault_profile`` keep fanning out as before."""
    from hermes_multitenancy.credential_materializer import materialize_credentials
    from hermes_multitenancy.credentials import CredentialStore

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    profiles = shared / "profiles"
    (profiles / "alice").mkdir(parents=True)
    (shared / "credential-materialization.yaml").write_text(
        """
credentials:
  - subject_id: kep-prd-skills
    provider: gitlab
    secret_kind: token
    target: workspace/credentials/gitlab.token
    profiles: [alice]
""",
        encoding="utf-8",
    )
    store = CredentialStore(shared / "multitenancy.db")
    try:
        # A personal record exists but the entry never opted into __self__.
        store.put_credential(
            profile_name="alice",
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
            payload={"token": "personal-alice"},
        )
        store.put_credential(
            profile_name="__shared__",
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
            payload={"token": "global-token"},
        )
    finally:
        store.close()

    stats = materialize_credentials(shared_home=shared)

    assert stats["written"] == 1
    assert stats["personal_env_only"] == 0
    assert (profiles / "alice" / "workspace" / "credentials" / "gitlab.token").read_text(
        encoding="utf-8"
    ) == "global-token\n"


def test_self_lane_wildcard_targets_only_groups_with_their_own_row(monkeypatch, tmp_path: Path):
    """group-agent-gitlab-binding: a group that bound its own token is picked up
    automatically; a group without one is NOT targeted — otherwise the
    __self__→__shared__ fallback would materialize the shared secret into every
    group workspace."""
    from hermes_multitenancy.credential_materializer import materialize_credentials
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.routing import RoutingTable

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    profiles = shared / "profiles"
    for name in ("alice", "group_bound", "group_empty"):
        (profiles / name).mkdir(parents=True)
    (shared / "credential-materialization.yaml").write_text(
        """
credentials:
  - subject_id: kep-prd-skills
    provider: gitlab
    secret_kind: token
    target: workspace/credentials/gitlab.token
    env: GITLAB_TOKEN
    vault_profile: __self__
    profiles: ["*"]
""",
        encoding="utf-8",
    )

    table = RoutingTable(shared / "multitenancy.db")
    try:
        table.upsert(user_id="alice", profile_name="alice", open_id="ou_alice")
        table.upsert_group(chat_id="oc_bound", profile_name="group_bound", owner_open_id="ou_alice")
        table.upsert_group(chat_id="oc_empty", profile_name="group_empty", owner_open_id="ou_alice")
    finally:
        table.close()

    store = CredentialStore(shared / "multitenancy.db")
    try:
        store.put_credential(
            profile_name="__shared__", subject_id="kep-prd-skills",
            provider="gitlab", secret_kind="token",
            payload={"token": "shared-token"},
        )
        store.put_credential(
            profile_name="group_bound", subject_id="kep-prd-skills",
            provider="gitlab", secret_kind="token",
            payload={"token": "group-own-token"},
        )
    finally:
        store.close()

    stats = materialize_credentials(shared_home=shared)

    assert stats["profiles_targeted"] == 2  # alice + group_bound
    # group's OWN token is env-only by construction — no file lands on disk,
    # the run env assembly resolves it via resolve_runtime_secret instead.
    assert stats["personal_env_only"] == 1
    assert not (profiles / "group_bound" / "workspace" / "credentials" / "gitlab.token").exists()
    from hermes_multitenancy.credential_materializer import resolve_runtime_secret
    import yaml as _yaml
    entry = _yaml.safe_load(
        (shared / "credential-materialization.yaml").read_text(encoding="utf-8")
    )["credentials"][0]
    store = CredentialStore(shared / "multitenancy.db")
    try:
        payload, vault_profile = resolve_runtime_secret(store, entry, profile_name="group_bound")
    finally:
        store.close()
    assert vault_profile == "group_bound"
    assert payload and payload.get("token") == "group-own-token"
    # alice has no own row → self lane falls back to the shared row (user lane unchanged)
    alice_file = profiles / "alice" / "workspace" / "credentials" / "gitlab.token"
    assert alice_file.read_text(encoding="utf-8") == "shared-token\n"
    # the unbound group must not receive the shared fallback
    assert not (profiles / "group_empty" / "workspace" / "credentials" / "gitlab.token").exists()


def test_entry_targets_profile_gate_matrix(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.credential_materializer import entry_targets_profile
    from hermes_multitenancy.routing import RoutingTable

    shared = tmp_path / ".hermes"
    shared.mkdir(parents=True)
    table = RoutingTable(shared / "multitenancy.db")
    try:
        table.upsert(user_id="alice", profile_name="alice", open_id="ou_alice")
        table.upsert_group(chat_id="oc_g", profile_name="grp", owner_open_id="ou_alice")
    finally:
        table.close()

    self_lane = {
        "subject_id": "kep-prd-skills", "provider": "gitlab", "secret_kind": "token",
        "vault_profile": "__self__", "profiles": ["*"],
    }
    shared_lane = {**self_lane, "vault_profile": ""}
    listed_only = {**self_lane, "profiles": ["alice"]}

    # active group before any row: self-lane wildcard admits it
    assert entry_targets_profile(self_lane, shared_home=shared, profile_name="grp")
    # user route still admitted via the wildcard expansion
    assert entry_targets_profile(self_lane, shared_home=shared, profile_name="alice")
    # shared-lane wildcard never admits a group
    assert not entry_targets_profile(shared_lane, shared_home=shared, profile_name="grp")
    # no marker → explicit list only, groups don't ride in
    assert not entry_targets_profile(listed_only, shared_home=shared, profile_name="grp")
    # unknown profile stays rejected
    assert not entry_targets_profile(self_lane, shared_home=shared, profile_name="ghost")
