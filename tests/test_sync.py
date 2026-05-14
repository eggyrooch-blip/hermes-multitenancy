"""US-019 — feishu-sync apply_users idempotent reconciler."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def table():
    from hermes_multitenancy.routing import RoutingTable
    t = RoutingTable(":memory:")
    yield t
    t.close()


def test_apply_inserts_new_users(table):
    from hermes_multitenancy.sync import UserSpec, apply_users
    stats = apply_users(table, [
        UserSpec(user_id="u1", profile_name="alice", open_id="ou_1", union_id="on_1"),
        UserSpec(user_id="u2", profile_name="bob", open_id="ou_2", union_id="on_2"),
    ])
    assert stats == {"upserted": 2, "soft_deleted": 0, "kept": 0}
    assert table.count_active() == 2


def test_apply_idempotent_on_same_list(table):
    from hermes_multitenancy.sync import UserSpec, apply_users
    users = [UserSpec(user_id="u1", profile_name="alice", open_id="ou_1")]
    apply_users(table, users)
    stats = apply_users(table, users)
    assert stats == {"upserted": 0, "soft_deleted": 0, "kept": 1}, "second apply should be no-op"


def test_apply_soft_deletes_missing(table):
    from hermes_multitenancy.sync import UserSpec, apply_users
    apply_users(table, [
        UserSpec(user_id="u1", profile_name="alice", open_id="ou_1"),
        UserSpec(user_id="u2", profile_name="bob", open_id="ou_2"),
    ])
    # second apply leaves u1 only
    stats = apply_users(table, [
        UserSpec(user_id="u1", profile_name="alice", open_id="ou_1"),
    ])
    assert stats == {"upserted": 0, "soft_deleted": 1, "kept": 1}
    assert table.count_active() == 1


def test_apply_upserts_when_profile_changes(table):
    from hermes_multitenancy.sync import UserSpec, apply_users
    apply_users(table, [UserSpec(user_id="u1", profile_name="alice", open_id="ou_1")])
    stats = apply_users(table, [UserSpec(user_id="u1", profile_name="alice2", open_id="ou_1")])
    assert stats["upserted"] == 1
    row = table.lookup_by_open_id("ou_1")
    assert row.profile_name == "alice2"
    assert row.version == 2


def test_apply_replaces_auto_provision_open_id_route_with_user_id_route(table):
    from hermes_multitenancy.sync import UserSpec, apply_users

    apply_users(table, [UserSpec(user_id="ou_alice", profile_name="feishu_ou_alice", open_id="ou_alice")])
    stats = apply_users(table, [UserSpec(user_id="alice", profile_name="alice", open_id="ou_alice")])

    assert stats == {"upserted": 1, "soft_deleted": 1, "kept": 0}
    row = table.lookup_by_open_id("ou_alice")
    assert row.user_id == "alice"
    assert row.profile_name == "alice"
    assert table.count_active() == 1


def test_plan_users_is_dry_run(table):
    from hermes_multitenancy.sync import UserSpec, apply_users, plan_users

    apply_users(table, [
        UserSpec(user_id="u1", profile_name="alice", open_id="ou_1"),
        UserSpec(user_id="u2", profile_name="bob", open_id="ou_2"),
    ])
    stats = plan_users(table, [
        UserSpec(user_id="u1", profile_name="alice2", open_id="ou_1"),
    ])

    assert stats == {"upserted": 1, "soft_deleted": 1, "kept": 0}
    assert table.count_active() == 2


def test_plan_users_can_skip_soft_delete_for_scoped_sync(table):
    from hermes_multitenancy.sync import UserSpec, apply_users, plan_users

    apply_users(table, [
        UserSpec(user_id="u1", profile_name="alice", open_id="ou_1"),
        UserSpec(user_id="u2", profile_name="bob", open_id="ou_2"),
    ])
    stats = plan_users(
        table,
        [UserSpec(user_id="u1", profile_name="alice", open_id="ou_1")],
        soft_delete_missing=False,
    )

    assert stats == {"upserted": 0, "soft_deleted": 0, "kept": 1}


def test_cli_apply_end_to_end(tmp_path):
    """The CLI `apply` subcommand reads a JSON file and applies it to a DB."""
    from hermes_multitenancy.sync.cli import main

    users_json = tmp_path / "users.json"
    users_json.write_text(json.dumps([
        {"user_id": "u1", "profile_name": "alice", "open_id": "ou_1", "union_id": "on_1"},
        {"user_id": "u2", "profile_name": "bob",   "open_id": "ou_2"},
    ]))
    db = tmp_path / "routing.db"

    rc = main(["apply", str(users_json), "--db", str(db)])
    assert rc == 0
    # DB now has 2 active rows
    from hermes_multitenancy.routing import RoutingTable
    t = RoutingTable(str(db))
    try:
        assert t.count_active() == 2
    finally:
        t.close()


def test_profile_name_for_user_id_normalizes_to_hermes_profile_id():
    from hermes_multitenancy.sync import profile_name_for_user_id

    assert profile_name_for_user_id("Alice.User") == "alice_user"
    assert profile_name_for_user_id("owner") == "owner"
    assert profile_name_for_user_id("chat") == "feishu_chat"
    assert profile_name_for_user_id("-bad") == "bad"
    assert len(profile_name_for_user_id("A" * 100)) == 64


def test_build_org_snapshot_marks_leaders_and_builds_route_specs():
    from hermes_multitenancy.sync import (
        Department,
        DepartmentUser,
        build_org_snapshot,
        build_user_specs,
    )

    snapshot = build_org_snapshot(
        [
            Department(dept_id="od_sales", name="Sales", parent_id="0", leader_user_id="alice"),
            Department(dept_id="od_team", name="Team", parent_id="od_sales", leader_user_id="bob"),
        ],
        {
            "od_sales": [DepartmentUser(open_id="ou_alice", user_id="alice")],
            "od_team": [
                DepartmentUser(open_id="ou_bob", user_id="bob", union_id="on_bob"),
                DepartmentUser(open_id="ou_charlie", user_id="charlie"),
                DepartmentUser(open_id="ou_missing", user_id=None),
            ],
        },
    )

    assert snapshot.stats == {
        "total_depts": 2,
        "total_employees": 4,
        "leaders": 2,
        "empty_user_id": 1,
    }
    assert snapshot.employees["alice"].subordinates == ("bob",)
    assert snapshot.employees["bob"].leader_user_id == "alice"
    assert snapshot.employees["bob"].subordinates == ("charlie",)
    specs = build_user_specs(snapshot)
    assert [(u.user_id, u.profile_name, u.open_id, u.union_id) for u in specs] == [
        ("alice", "alice", "ou_alice", None),
        ("bob", "bob", "ou_bob", "on_bob"),
        ("charlie", "charlie", "ou_charlie", None),
    ]


def test_sync_profiles_manages_soul_block_without_overwriting_custom_text(tmp_path):
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared_home = tmp_path / "shared"
    shared_home.mkdir()
    (shared_home / "config.yaml").write_text(
        "model:\n  default: zai/glm-5.1\nplatforms:\n  feishu:\n    enabled: true\n    extra:\n      app_id: test-app\n",
        encoding="utf-8",
    )
    profiles_root = tmp_path / "profiles"
    profile_home = profiles_root / "alice"
    profile_home.mkdir(parents=True)
    (profile_home / "SOUL.md").write_text("# Alice\n\ncustom text\n", encoding="utf-8")

    snapshot = build_org_snapshot(
        [Department(dept_id="od_sales", name="Sales", leader_user_id="alice")],
        {"od_sales": [DepartmentUser(open_id="ou_alice", user_id="alice")]},
    )
    stats = sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)

    assert stats == {"created": 0, "updated": 1, "kept": 0, "skipped": 0}
    soul = (profile_home / "SOUL.md").read_text(encoding="utf-8")
    assert "custom text" in soul
    assert "BEGIN HERMES MULTITENANCY ORG SYNC" in soul
    assert "department: Sales (od_sales)" in soul
    assert "app_id: test-app" in (profile_home / "config.yaml").read_text(encoding="utf-8")

    snapshot2 = build_org_snapshot(
        [Department(dept_id="od_ops", name="Ops", leader_user_id="alice")],
        {"od_ops": [DepartmentUser(open_id="ou_alice", user_id="alice")]},
    )
    stats2 = sync_profiles(snapshot2, profiles_root=profiles_root, source_home=shared_home)
    soul2 = (profile_home / "SOUL.md").read_text(encoding="utf-8")
    assert stats2["updated"] == 1
    assert "custom text" in soul2
    assert "department: Ops (od_ops)" in soul2
    assert "department: Sales (od_sales)" not in soul2


def test_sync_profiles_copies_default_skills_without_secret_files(tmp_path):
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared_home = tmp_path / "shared"
    skill_source = shared_home / "skills" / "Keep" / "keep-record"
    skill_source.mkdir(parents=True)
    (shared_home / "config.yaml").write_text("model:\n  default: zai/glm-5.1\n", encoding="utf-8")
    (shared_home / "profile-skill-defaults.yaml").write_text(
        """
skills:
  - Keep/keep-record
""",
        encoding="utf-8",
    )
    (skill_source / "SKILL.md").write_text("# Keep Record\n", encoding="utf-8")
    (skill_source / ".env").write_text("TOKEN=do-not-copy\n", encoding="utf-8")
    (skill_source / "gitlab.token").write_text("do-not-copy\n", encoding="utf-8")
    (skill_source / "scripts").mkdir()
    (skill_source / "scripts" / "run.js").write_text("console.log('ok')\n", encoding="utf-8")

    profiles_root = tmp_path / "profiles"
    snapshot = build_org_snapshot(
        [Department(dept_id="od_sales", name="Sales", leader_user_id="alice")],
        {"od_sales": [DepartmentUser(open_id="ou_alice", user_id="alice")]},
    )

    stats = sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)

    target = profiles_root / "alice" / "skills" / "Keep" / "keep-record"
    assert stats["created"] == 1
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "# Keep Record\n"
    assert (target / "scripts" / "run.js").exists()
    assert not (target / ".env").exists()
    assert not (target / "gitlab.token").exists()


def test_sync_feishu_org_dry_run_does_not_write_profiles_or_db(tmp_path):
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.sync import Department, DepartmentUser, sync_feishu_org

    class FakeContact:
        def fetch_department_tree(self, root_id):
            assert root_id == "0"
            return []

        def fetch_department_detail(self, dept_id):
            assert dept_id == "0"
            return Department(dept_id="0", name="Root")

        def fetch_department_users(self, dept_id):
            return [DepartmentUser(open_id="ou_alice", user_id="alice")]

    db = tmp_path / "routing.db"
    profiles_root = tmp_path / "profiles"
    stats = sync_feishu_org(
        dry_run=True,
        client=FakeContact(),
        db_path=db,
        profiles_root=profiles_root,
        source_home=tmp_path / "shared",
        api_delay=0,
    )

    assert stats["profiles_created"] == 1
    assert stats["routes_upserted"] == 1
    assert stats["routes_soft_delete_enabled"] is True
    assert not profiles_root.exists()
    assert not db.exists()
    table = RoutingTable(db)
    try:
        assert table.count_active() == 0
    finally:
        table.close()


def test_sync_feishu_org_writes_profiles_routes_and_snapshot(tmp_path):
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.sync import Department, DepartmentUser, sync_feishu_org

    class FakeContact:
        def fetch_department_tree(self, root_id):
            return [Department(dept_id="od_sales", name="Sales")]

        def fetch_department_detail(self, dept_id):
            return Department(dept_id=dept_id, name="Pilot")

        def fetch_department_users(self, dept_id):
            return [DepartmentUser(open_id="ou_alice", user_id="alice", union_id="on_alice")]

    shared_home = tmp_path / "shared"
    shared_home.mkdir()
    (shared_home / "config.yaml").write_text("model:\n  default: zai/glm-5.1\n", encoding="utf-8")
    db = tmp_path / "routing.db"
    snapshot_dir = tmp_path / "snapshots"

    stats = sync_feishu_org(
        client=FakeContact(),
        db_path=db,
        profiles_root=tmp_path / "profiles",
        source_home=shared_home,
        snapshot_out=snapshot_dir,
        api_delay=0,
    )

    assert stats["profiles_created"] == 1
    assert stats["routes_upserted"] == 1
    assert stats["snapshot_path"]
    snapshot_path = Path(stats["snapshot_path"])
    assert snapshot_path.exists()
    assert (tmp_path / "profiles" / "alice" / "SOUL.md").exists()
    assert "ou_alice" in (tmp_path / "profiles" / "alice" / "SOUL.md").read_text(encoding="utf-8")
    table = RoutingTable(db)
    try:
        row = table.lookup_by_open_id("ou_alice")
        assert row is not None
        assert row.user_id == "alice"
        assert row.profile_name == "alice"
        assert row.union_id == "on_alice"
    finally:
        table.close()
    assert json.loads(snapshot_path.read_text(encoding="utf-8"))["stats"]["total_employees"] == 1


def test_sync_feishu_org_dept_sync_does_not_soft_delete_out_of_scope_routes(tmp_path):
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.sync import Department, DepartmentUser, UserSpec, apply_users, sync_feishu_org

    class FakeContact:
        def fetch_department_tree(self, root_id):
            assert root_id == "od_pilot"
            return []

        def fetch_department_detail(self, dept_id):
            return Department(dept_id=dept_id, name="Pilot")

        def fetch_department_users(self, dept_id):
            return [DepartmentUser(open_id="ou_alice", user_id="alice")]

    db = tmp_path / "routing.db"
    table = RoutingTable(db)
    try:
        apply_users(table, [UserSpec(user_id="bob", profile_name="bob", open_id="ou_bob")])
    finally:
        table.close()

    stats = sync_feishu_org(
        dept_id="od_pilot",
        client=FakeContact(),
        db_path=db,
        profiles_root=tmp_path / "profiles",
        source_home=tmp_path / "shared",
        api_delay=0,
    )

    assert stats["routes_soft_delete_enabled"] is False
    assert stats["routes_soft_deleted"] == 0
    table = RoutingTable(db)
    try:
        assert table.lookup_by_open_id("ou_bob") is not None
        assert table.lookup_by_open_id("ou_alice") is not None
    finally:
        table.close()


def test_cli_pull_feishu_delegates_to_sync(monkeypatch, capsys):
    from hermes_multitenancy.sync import cli

    calls = []

    def fake_sync_feishu_org(**kwargs):
        calls.append(kwargs)
        return {"dry_run": True, "employees": 1}

    monkeypatch.setattr(cli, "sync_feishu_org", fake_sync_feishu_org)
    rc = cli.main(["pull-feishu", "--dept", "od_x", "--dry-run", "--api-delay", "0"])

    assert rc == 0
    assert calls[0]["dept_id"] == "od_x"
    assert calls[0]["dry_run"] is True
    assert calls[0]["soft_delete_missing"] is None
    assert json.loads(capsys.readouterr().out)["employees"] == 1


# ---------------------------------------------------------------------------
# 档 A — profile directory hardening (chmod 0700 + isolation pivot dirs)
# ---------------------------------------------------------------------------


def test_sync_profiles_creates_isolation_pivot_dirs(tmp_path):
    """Provision must create the per-profile HOME/XDG/TMPDIR backing dirs."""
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared_home = tmp_path / "shared"
    shared_home.mkdir()
    (shared_home / "config.yaml").write_text(
        "model:\n  default: zai/glm-5.1\n",
        encoding="utf-8",
    )
    profiles_root = tmp_path / "profiles"
    snapshot = build_org_snapshot(
        [Department(dept_id="od_sales", name="Sales", leader_user_id="alice")],
        {"od_sales": [DepartmentUser(open_id="ou_alice", user_id="alice")]},
    )
    sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)

    profile_home = profiles_root / "alice"
    # Every isolation pivot dir referenced by _build_subprocess_env must exist
    # so the first child spawn doesn't have to create them.
    for sub in ("home", "workspace", "cache", "config", "state", "data", "tmp"):
        path = profile_home / sub
        assert path.is_dir(), f"{sub}/ missing — child subprocess would create it world-readable"


def test_sync_profiles_chmods_profile_tree_to_0700(tmp_path):
    """profile_home and all subdirs must be 0700 to prevent cross-user enumeration."""
    import stat as stat_mod
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared_home = tmp_path / "shared"
    shared_home.mkdir()
    (shared_home / "config.yaml").write_text(
        "model:\n  default: zai/glm-5.1\n",
        encoding="utf-8",
    )
    profiles_root = tmp_path / "profiles"
    snapshot = build_org_snapshot(
        [Department(dept_id="od_sales", name="Sales", leader_user_id="alice")],
        {"od_sales": [DepartmentUser(open_id="ou_alice", user_id="alice")]},
    )
    sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)

    profile_home = profiles_root / "alice"
    profile_mode = stat_mod.S_IMODE(profile_home.stat().st_mode)
    assert profile_mode == 0o700, f"profile_home is {oct(profile_mode)}, expected 0o700"

    for sub in ("home", "cache", "config", "state", "data", "tmp", "memories", "sessions"):
        path = profile_home / sub
        mode = stat_mod.S_IMODE(path.stat().st_mode)
        assert mode == 0o700, f"{sub}/ is {oct(mode)}, expected 0o700"


# ---------------------------------------------------------------------------
# 档 A — feishu_uat per-profile拆分 + migration
# ---------------------------------------------------------------------------


def test_migrate_feishu_uat_copies_from_shared_to_profile(tmp_path):
    from hermes_multitenancy.sync.feishu_org import _migrate_feishu_uat_for_employee
    import stat as stat_mod

    shared = tmp_path / "shared"
    profile = tmp_path / "profile"
    (shared / "feishu_uat").mkdir(parents=True)
    src = shared / "feishu_uat" / "ou_alice.json"
    src.write_text('{"access_token": "alice-token"}', encoding="utf-8")

    copied = _migrate_feishu_uat_for_employee(profile, shared, "ou_alice")

    assert copied is True
    dst = profile / "feishu_uat" / "ou_alice.json"
    assert dst.read_text(encoding="utf-8") == '{"access_token": "alice-token"}'
    # File chmod 0600, parent dir 0700.
    assert stat_mod.S_IMODE(dst.stat().st_mode) == 0o600
    assert stat_mod.S_IMODE(dst.parent.stat().st_mode) == 0o700


def test_migrate_feishu_uat_is_idempotent(tmp_path):
    from hermes_multitenancy.sync.feishu_org import _migrate_feishu_uat_for_employee

    shared = tmp_path / "shared"
    profile = tmp_path / "profile"
    (shared / "feishu_uat").mkdir(parents=True)
    (shared / "feishu_uat" / "ou_bob.json").write_text("token-v1", encoding="utf-8")

    assert _migrate_feishu_uat_for_employee(profile, shared, "ou_bob") is True
    # Second call: dst exists and has same/newer mtime — no copy.
    assert _migrate_feishu_uat_for_employee(profile, shared, "ou_bob") is False


def test_migrate_feishu_uat_refreshes_on_newer_source(tmp_path):
    import os as _os
    from hermes_multitenancy.sync.feishu_org import _migrate_feishu_uat_for_employee

    shared = tmp_path / "shared"
    profile = tmp_path / "profile"
    (shared / "feishu_uat").mkdir(parents=True)
    src = shared / "feishu_uat" / "ou_carol.json"
    src.write_text("v1", encoding="utf-8")
    _migrate_feishu_uat_for_employee(profile, shared, "ou_carol")

    # OAuth callback "refreshes" the shared token; bump mtime forward.
    src.write_text("v2", encoding="utf-8")
    future = src.stat().st_mtime + 10
    _os.utime(src, (future, future))

    assert _migrate_feishu_uat_for_employee(profile, shared, "ou_carol") is True
    assert (profile / "feishu_uat" / "ou_carol.json").read_text() == "v2"


def test_migrate_feishu_uat_rejects_non_ou_open_id(tmp_path):
    from hermes_multitenancy.sync.feishu_org import _migrate_feishu_uat_for_employee

    shared = tmp_path / "shared"
    profile = tmp_path / "profile"
    # Even if the file exists, a non-ou_ id is refused.
    (shared / "feishu_uat").mkdir(parents=True)
    (shared / "feishu_uat" / "on_alice.json").write_text("union-id-token", encoding="utf-8")

    assert _migrate_feishu_uat_for_employee(profile, shared, "on_alice") is False
    assert _migrate_feishu_uat_for_employee(profile, shared, "") is False


def test_migrate_feishu_uat_silent_when_source_missing(tmp_path):
    from hermes_multitenancy.sync.feishu_org import _migrate_feishu_uat_for_employee

    shared = tmp_path / "shared"
    profile = tmp_path / "profile"
    assert _migrate_feishu_uat_for_employee(profile, shared, "ou_never_bound") is False
    # Should not create empty destinations either.
    assert not (profile / "feishu_uat" / "ou_never_bound.json").exists()


def test_sync_profiles_pulls_feishu_uat_into_profile(tmp_path):
    """End-to-end: sync_profiles invokes the migration for each employee's open_id."""
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared_home = tmp_path / "shared"
    shared_home.mkdir()
    (shared_home / "config.yaml").write_text(
        "model:\n  default: zai/glm-5.1\n",
        encoding="utf-8",
    )
    # Drop a "previously OAuth'd" UAT token under the legacy shared path.
    (shared_home / "feishu_uat").mkdir()
    (shared_home / "feishu_uat" / "ou_alice.json").write_text("alice-token", encoding="utf-8")
    (shared_home / "feishu_uat" / "ou_unrelated.json").write_text("not-mine", encoding="utf-8")

    profiles_root = tmp_path / "profiles"
    snapshot = build_org_snapshot(
        [Department(dept_id="od_sales", name="Sales", leader_user_id="alice")],
        {"od_sales": [DepartmentUser(open_id="ou_alice", user_id="alice")]},
    )
    sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)

    alice_uat = profiles_root / "alice" / "feishu_uat" / "ou_alice.json"
    assert alice_uat.is_file()
    assert alice_uat.read_text() == "alice-token"
    # Strangers' tokens did NOT leak into alice's profile.
    stranger = profiles_root / "alice" / "feishu_uat" / "ou_unrelated.json"
    assert not stranger.exists()


def test_sync_profiles_mirrors_feishu_uat_into_credential_vault(monkeypatch, tmp_path):
    """Org sync keeps the DB credential source aligned with refreshed UAT JSON."""
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared_home = tmp_path / "shared"
    shared_home.mkdir()
    (shared_home / "config.yaml").write_text("model:\n  default: zai/glm-5.1\n", encoding="utf-8")
    (shared_home / "feishu_uat").mkdir()
    (shared_home / "feishu_uat" / "ou_alice.json").write_text(
        json.dumps({
            "access_token": "alice-token-v1",
            "refresh_token": "alice-refresh",
            "user_open_id": "ou_alice",
            "scope": "calendar:calendar im:message",
            "expires_at": 4102444800000,
        }),
        encoding="utf-8",
    )

    profiles_root = tmp_path / "profiles"
    snapshot = build_org_snapshot(
        [Department(dept_id="od_sales", name="Sales", leader_user_id="alice")],
        {"od_sales": [DepartmentUser(open_id="ou_alice", user_id="alice")]},
    )
    sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)

    store = CredentialStore(shared_home / "multitenancy.db", encryption_key="test-key")
    try:
        status = store.get_status(
            profile_name="alice",
            subject_id="ou_alice",
            provider="feishu",
            secret_kind="uat",
        )
        payload = store.get_secret_for_runtime(
            profile_name="alice",
            subject_id="ou_alice",
            provider="feishu",
            secret_kind="uat",
        )
    finally:
        store.close()

    assert status["status"] == "valid"
    assert status["expires_at"] == 4102444800000
    assert payload["access_token"] == "alice-token-v1"


def test_sync_profiles_reapplies_chmod_on_drift(tmp_path):
    """If someone loosens a dir to 0755 externally, next sync re-tightens it."""
    import stat as stat_mod
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared_home = tmp_path / "shared"
    shared_home.mkdir()
    (shared_home / "config.yaml").write_text(
        "model:\n  default: zai/glm-5.1\n",
        encoding="utf-8",
    )
    profiles_root = tmp_path / "profiles"
    snapshot = build_org_snapshot(
        [Department(dept_id="od_sales", name="Sales", leader_user_id="alice")],
        {"od_sales": [DepartmentUser(open_id="ou_alice", user_id="alice")]},
    )
    sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)

    profile_home = profiles_root / "alice"
    drifted = profile_home / "cache"
    import os as _os
    _os.chmod(drifted, 0o755)
    assert stat_mod.S_IMODE(drifted.stat().st_mode) == 0o755

    # Second sync must restore 0700.
    sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)
    assert stat_mod.S_IMODE(drifted.stat().st_mode) == 0o700
