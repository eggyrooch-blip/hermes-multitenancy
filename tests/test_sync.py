"""US-019 — feishu-sync apply_users idempotent reconciler."""
from __future__ import annotations

import json
import sys
import types
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


def test_apply_soft_delete_missing_keeps_group_rows_active(table):
    """US-02 regression: soft_delete_missing must not delete kind='group' rows.

    A full org sync (soft_delete_missing=True) reconciles only user rows;
    group profiles must survive even though they are absent from the desired
    Feishu employee list. Without the kind-scoped snapshot in
    ``_desired_and_current`` the group row is soft-deleted (data loss).
    """
    from hermes_multitenancy.sync import UserSpec, apply_users

    table.upsert(user_id="u1", profile_name="alice", open_id="ou_1")
    group_user_id = table.upsert_group(
        chat_id="oc_grp1",
        profile_name="feishu_group_oc_grp1",
        owner_open_id="ou_owner",
        display_label="owner-IT组",
    )
    assert group_user_id == "group:oc_grp1"

    apply_users(
        table,
        [UserSpec(user_id="u1", profile_name="alice", open_id="ou_1")],
        soft_delete_missing=True,
    )

    assert table.lookup_by_chat_id("oc_grp1") is not None
    assert table.count_active(kind="group") == 1
    assert table.lookup_by_open_id("ou_1") is not None
    assert table.count_active(kind="user") == 1


def test_apply_soft_delete_missing_keeps_non_sync_agent_rows_active(table):
    """US-05 regression: full sync must soft-delete only sync-origin user rows.

    Auto-provisioned user rows and future self-built agent rows are outside the
    Feishu org snapshot ownership boundary, so a full org sync must keep them
    active even when they are absent from the desired employee list.
    """
    from hermes_multitenancy.sync import UserSpec, apply_users

    apply_users(table, [UserSpec(user_id="u1", profile_name="alice", open_id="ou_1")])
    table.upsert(user_id="ou_auto", profile_name="ou_auto", open_id="ou_auto")
    table.upsert_group(
        chat_id="oc_grp1",
        profile_name="feishu_group_oc_grp1",
        owner_open_id="ou_owner",
        display_label="owner-IT组",
    )

    auto_row = table._conn.execute(
        "SELECT provenance FROM multitenancy_routing WHERE user_id = ?",
        ("ou_auto",),
    ).fetchone()
    assert auto_row["provenance"] == "auto"

    apply_users(
        table,
        [UserSpec(user_id="u1", profile_name="alice", open_id="ou_1")],
        soft_delete_missing=True,
    )

    auto_row_after = table._conn.execute(
        "SELECT active, provenance FROM multitenancy_routing WHERE user_id = ?",
        ("ou_auto",),
    ).fetchone()
    assert auto_row_after["active"] == 1
    assert auto_row_after["provenance"] == "auto"
    assert table.lookup_by_open_id("ou_auto") is not None
    assert table.lookup_by_chat_id("oc_grp1") is not None
    assert table.count_active(kind="group") == 1
    assert table.lookup_by_open_id("ou_1") is not None


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


def test_apply_users_creates_sync_root_resolvable_by_open_id(table):
    from hermes_multitenancy.sync import UserSpec, apply_users

    stats = apply_users(
        table,
        [UserSpec(user_id="alice", profile_name="alice", open_id="ou_alice")],
    )

    assert stats == {"upserted": 1, "soft_deleted": 0, "kept": 0}
    row = table.resolve_owner_root("ou_alice")
    assert row is not None
    assert row.user_id == "alice"
    assert row.profile_name == "alice"


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


def test_plan_profile_skill_sync_reports_add_modified_removed_and_secret_guard(tmp_path):
    from hermes_multitenancy.sync.feishu_org import (
        Employee,
        MANAGED_SKILL_MANIFEST,
        plan_profile_skill_sync,
    )

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "alice"
    (shared / "skills" / "lark-base").mkdir(parents=True)
    (shared / "skills" / "lark-base" / "SKILL.md").write_text("# Lark Base\n", encoding="utf-8")
    (shared / "skills" / "internal" / "report").mkdir(parents=True)
    (shared / "skills" / "internal" / "report" / "SKILL.md").write_text("# Report\n", encoding="utf-8")
    (shared / "skills" / "lark-secret").mkdir(parents=True)
    (shared / "skills" / "lark-secret" / "SKILL.md").write_text("# Secret\n", encoding="utf-8")
    (shared / "skills" / "lark-secret" / ".env").write_text("TOKEN=should-not-leak\n", encoding="utf-8")
    (shared / "skill-distribution.yaml").write_text(
        """
skills:
  - path: lark-base
    install_mode: symlink
  - path: internal/report
    install_mode: copy
  - path: lark-secret
    install_mode: symlink
""",
        encoding="utf-8",
    )
    (profile / "skills" / "internal" / "report").mkdir(parents=True)
    (profile / "skills" / "internal" / "report" / "SKILL.md").write_text("# User edited\n", encoding="utf-8")
    (profile / "skills").mkdir(parents=True, exist_ok=True)
    (profile / "skills" / MANAGED_SKILL_MANIFEST).write_text(
        json.dumps(
            {
                "version": 1,
                "skills": {
                    "internal/report": {
                        "source": str(shared / "skills" / "internal" / "report"),
                        "install_mode": "copy",
                    },
                    "old-skill": {
                        "source": str(shared / "skills" / "old-skill"),
                        "install_mode": "copy",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    result = plan_profile_skill_sync(
        profile,
        shared,
        Employee(
            open_id="ou_alice",
            user_id="alice",
            agent_id="alice",
            profile_name="alice",
            name="Alice",
            dept_id="dept",
            dept_name="Dept",
            leader_user_id=None,
        ),
    )

    assert result["changed"] is True
    assert result["counts"]["upstream_added"] == 2
    assert result["counts"]["profile_modified"] == 1
    assert result["counts"]["multitenancy_removed"] == 1
    items = {item["path"]: item for item in result["items"]}
    assert items["lark-base"]["change"] == "upstream_added"
    assert items["lark-secret"]["secret_guard"] == "copy_filtered"
    assert items["internal/report"]["change"] == "profile_modified"
    assert items["old-skill"]["change"] == "multitenancy_removed"
    assert not (profile / "skills" / "lark-base").exists()
    assert "should-not-leak" not in json.dumps(result, ensure_ascii=False)


def test_sync_cli_plan_skills_outputs_secret_free_json(capsys, tmp_path):
    from hermes_multitenancy.sync.cli import main

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "alice"
    (shared / "skills" / "lark-docs").mkdir(parents=True)
    (shared / "skills" / "lark-docs" / "SKILL.md").write_text("# Docs\n", encoding="utf-8")
    (shared / "profile-skill-defaults.yaml").write_text("skills:\n  - lark-docs\n", encoding="utf-8")

    code = main([
        "plan-skills",
        "--home",
        str(shared),
        "--profile-home",
        str(profile),
        "--profile-name",
        "alice",
        "--open-id",
        "ou_alice",
    ])
    output = capsys.readouterr().out

    assert code == 0
    parsed = json.loads(output)
    assert parsed["changed"] is True
    assert parsed["counts"]["upstream_added"] == 1
    assert parsed["items"][0]["path"] == "lark-docs"
    assert parsed["secret_free"] is True


def test_plan_profile_skill_sync_reads_org_default_skill_bundle(tmp_path):
    from hermes_multitenancy.sync.feishu_org import Employee, plan_profile_skill_sync

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "alice"
    org_skill = shared / "skills" / "org" / "default-docs"
    org_skill.mkdir(parents=True)
    (org_skill / "SKILL.md").write_text("# Default Docs\n", encoding="utf-8")
    (shared / "skill-bundles.yaml").write_text(
        """
skill_bundles:
  default:
    - org-default
  bundles:
    org-default:
      audience: all
      skills:
        - path: org/default-docs
          install_mode: copy
          token_policy: none
""",
        encoding="utf-8",
    )

    result = plan_profile_skill_sync(
        profile,
        shared,
        Employee(
            open_id="ou_alice",
            user_id="alice",
            agent_id="alice",
            profile_name="alice",
            name="Alice",
            dept_id="od_ops",
            dept_name="Ops",
            leader_user_id=None,
        ),
    )

    items = {item["path"]: item for item in result["items"]}
    assert items["org/default-docs"]["change"] == "upstream_added"
    assert items["org/default-docs"]["token_policy"] == "none"
    assert items["org/default-docs"]["bundle"] == "org-default"
    assert result["counts"]["upstream_added"] == 1


def test_plan_profile_skill_sync_skips_non_default_global_skill_bundle(tmp_path):
    from hermes_multitenancy.sync.feishu_org import Employee, plan_profile_skill_sync

    shared = tmp_path / ".hermes"
    default_skill = shared / "skills" / "org" / "default-docs"
    optional_skill = shared / "skills" / "org" / "optional-lab"
    for skill in (default_skill, optional_skill):
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {skill.name}\n", encoding="utf-8")
    (shared / "skill-bundles.yaml").write_text(
        """
skill_bundles:
  default: [org-default]
  bundles:
    org-default:
      audience: all
      skills:
        - org/default-docs
    optional-lab:
      audience: all
      skills:
        - org/optional-lab
""",
        encoding="utf-8",
    )

    result = plan_profile_skill_sync(
        shared / "profiles" / "alice",
        shared,
        Employee(
            open_id="ou_alice",
            user_id="alice",
            agent_id="alice",
            profile_name="alice",
            name="Alice",
            dept_id="od_ops",
            dept_name="Ops",
            leader_user_id=None,
        ),
    )

    paths = {item["path"] for item in result["items"]}
    assert "org/default-docs" in paths
    assert "org/optional-lab" not in paths


def test_plan_profile_skill_sync_filters_department_skill_bundle_by_audience(tmp_path):
    from hermes_multitenancy.sync.feishu_org import Employee, plan_profile_skill_sync

    shared = tmp_path / ".hermes"
    finance_skill = shared / "skills" / "dept" / "finance-report"
    finance_skill.mkdir(parents=True)
    (finance_skill / "SKILL.md").write_text("# Finance Report\n", encoding="utf-8")
    (shared / "skill-bundles.yaml").write_text(
        """
skill_bundles:
  default: [org-default]
  bundles:
    org-default:
      audience: all
      skills: []
    finance:
      audience:
        departments: [od_finance]
      skills:
        - path: dept/finance-report
          install_mode: symlink
          requires_token: false
""",
        encoding="utf-8",
    )

    finance = plan_profile_skill_sync(
        shared / "profiles" / "alice",
        shared,
        Employee(
            open_id="ou_alice",
            user_id="alice",
            agent_id="alice",
            profile_name="alice",
            name="Alice",
            dept_id="od_finance",
            dept_name="Finance",
            leader_user_id=None,
        ),
    )
    ops = plan_profile_skill_sync(
        shared / "profiles" / "bob",
        shared,
        Employee(
            open_id="ou_bob",
            user_id="bob",
            agent_id="bob",
            profile_name="bob",
            name="Bob",
            dept_id="od_ops",
            dept_name="Ops",
            leader_user_id=None,
        ),
    )

    assert "dept/finance-report" in {item["path"] for item in finance["items"]}
    assert "dept/finance-report" not in {item["path"] for item in ops["items"]}


def test_child_profile_inherits_only_shareable_bundle_skills(tmp_path):
    from hermes_multitenancy.sync.feishu_org import Employee, _sync_default_profile_skills, plan_profile_skill_sync

    shared = tmp_path / ".hermes"
    shared_skill = shared / "skills" / "org" / "readonly"
    oauth_skill = shared / "skills" / "org" / "personal-oauth"
    secret_skill = shared / "skills" / "org" / "secret-shared"
    for skill in (shared_skill, oauth_skill, secret_skill):
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {skill.name}\n", encoding="utf-8")
    (secret_skill / ".env").write_text("TOKEN=should-not-leak\n", encoding="utf-8")
    (shared / "skill-bundles.yaml").write_text(
        """
skill_bundles:
  default: [org-default]
  bundles:
    org-default:
      audience: all
      skills:
        - path: org/readonly
          install_mode: symlink
          requires_token: false
        - path: org/personal-oauth
          install_mode: symlink
          token_policy: user_oauth
        - path: org/secret-shared
          install_mode: symlink
          token_policy: bot
""",
        encoding="utf-8",
    )
    owner = Employee(
        open_id="ou_alice",
        user_id="alice",
        agent_id="alice",
        profile_name="alice",
        name="Alice",
        dept_id="od_ops",
        dept_name="Ops",
        leader_user_id=None,
    )
    owner_home = shared / "profiles" / "alice"

    assert _sync_default_profile_skills(owner_home, shared, owner) is True
    child = plan_profile_skill_sync(
        shared / "profiles" / "feishu_group_ops",
        shared,
        upstream_profile_home=owner_home,
    )

    items = {item["path"]: item for item in child["items"]}
    assert "org/readonly" in items
    assert "org/secret-shared" in items
    assert "org/personal-oauth" not in items
    assert items["org/secret-shared"]["secret_guard"] == "copy_filtered"
    assert "should-not-leak" not in json.dumps(child, ensure_ascii=False)


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
    (skill_source / "node_modules" / "@keepclaw" / "skill-sdk").mkdir(parents=True)
    (skill_source / "node_modules" / "@keepclaw" / "skill-sdk" / "index.js").write_text(
        "module.exports = {}\n",
        encoding="utf-8",
    )

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
    assert (target / "node_modules").is_symlink()
    assert (target / "node_modules").resolve() == (skill_source / "node_modules").resolve()
    assert not (target / ".env").exists()
    assert not (target / "gitlab.token").exists()


def test_sync_profiles_symlinks_shared_lark_skills(tmp_path):
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared_home = tmp_path / "shared"
    keep_source = shared_home / "skills" / "Keep" / "keep-record"
    lark_source = shared_home / "skills" / "lark-base"
    keep_source.mkdir(parents=True)
    lark_source.mkdir(parents=True)
    (shared_home / "config.yaml").write_text("model:\n  default: zai/glm-5.1\n", encoding="utf-8")
    (shared_home / "profile-skill-defaults.yaml").write_text(
        """
skills:
  - Keep/keep-record
  - lark-base
""",
        encoding="utf-8",
    )
    (keep_source / "SKILL.md").write_text("# Keep Record\n", encoding="utf-8")
    (lark_source / "SKILL.md").write_text("# Lark Base\n", encoding="utf-8")

    profiles_root = tmp_path / "profiles"
    snapshot = build_org_snapshot(
        [Department(dept_id="od_sales", name="Sales", leader_user_id="alice")],
        {"od_sales": [DepartmentUser(open_id="ou_alice", user_id="alice")]},
    )

    sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)

    profile_skills = profiles_root / "alice" / "skills"
    keep_target = profile_skills / "Keep" / "keep-record"
    lark_target = profile_skills / "lark-base"
    assert keep_target.exists()
    assert not keep_target.is_symlink()
    assert lark_target.is_symlink()
    assert lark_target.resolve() == lark_source.resolve()


def test_sync_profiles_distributes_shared_skills_by_audience_without_copying(tmp_path):
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared_home = tmp_path / "shared"
    keep_source = shared_home / "skills" / "Keep" / "keep-record"
    finance_source = shared_home / "skills" / "internal" / "finance-cli"
    keep_source.mkdir(parents=True)
    finance_source.mkdir(parents=True)
    (shared_home / "config.yaml").write_text("model:\n  default: zai/glm-5.1\n", encoding="utf-8")
    (shared_home / "skill-distribution.yaml").write_text(
        """
skills:
  - path: Keep/keep-record
    install_mode: symlink
    audience: all
  - path: internal/finance-cli
    install_mode: symlink
    audience:
      departments: [od_finance]
""",
        encoding="utf-8",
    )
    (keep_source / "SKILL.md").write_text("# Keep Record\n", encoding="utf-8")
    (finance_source / "SKILL.md").write_text("# Finance CLI\n", encoding="utf-8")

    profiles_root = tmp_path / "profiles"
    snapshot = build_org_snapshot(
        [
            Department(dept_id="od_finance", name="Finance", leader_user_id="alice"),
            Department(dept_id="od_ops", name="Ops", leader_user_id="bob"),
        ],
        {
            "od_finance": [DepartmentUser(open_id="ou_alice", user_id="alice")],
            "od_ops": [DepartmentUser(open_id="ou_bob", user_id="bob")],
        },
    )

    sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)

    alice_skills = profiles_root / "alice" / "skills"
    bob_skills = profiles_root / "bob" / "skills"
    assert (alice_skills / "Keep" / "keep-record").is_symlink()
    assert (alice_skills / "Keep" / "keep-record").resolve() == keep_source.resolve()
    assert (bob_skills / "Keep" / "keep-record").is_symlink()
    assert (alice_skills / "internal" / "finance-cli").is_symlink()
    assert (alice_skills / "internal" / "finance-cli").resolve() == finance_source.resolve()
    assert not (bob_skills / "internal" / "finance-cli").exists()


def test_sync_profiles_distributes_shared_skills_by_profile_and_user_audience(tmp_path):
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared_home = tmp_path / "shared"
    profile_source = shared_home / "skills" / "internal" / "profile-only-cli"
    user_source = shared_home / "skills" / "internal" / "user-only-cli"
    profile_source.mkdir(parents=True)
    user_source.mkdir(parents=True)
    (shared_home / "config.yaml").write_text("model:\n  default: zai/glm-5.1\n", encoding="utf-8")
    (shared_home / "skill-distribution.yaml").write_text(
        """
skills:
  - path: internal/profile-only-cli
    install_mode: symlink
    audience:
      profiles: [alice]
  - path: internal/user-only-cli
    install_mode: symlink
    audience:
      users: [bob]
""",
        encoding="utf-8",
    )
    (profile_source / "SKILL.md").write_text("# Profile Only\n", encoding="utf-8")
    (user_source / "SKILL.md").write_text("# User Only\n", encoding="utf-8")

    profiles_root = tmp_path / "profiles"
    snapshot = build_org_snapshot(
        [Department(dept_id="od_ops", name="Ops", leader_user_id="alice")],
        {
            "od_ops": [
                DepartmentUser(open_id="ou_alice", user_id="alice"),
                DepartmentUser(open_id="ou_bob", user_id="bob"),
                DepartmentUser(open_id="ou_carol", user_id="carol"),
            ]
        },
    )

    sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)

    alice_skills = profiles_root / "alice" / "skills"
    bob_skills = profiles_root / "bob" / "skills"
    carol_skills = profiles_root / "carol" / "skills"
    assert (alice_skills / "internal" / "profile-only-cli").is_symlink()
    assert not (bob_skills / "internal" / "profile-only-cli").exists()
    assert not (carol_skills / "internal" / "profile-only-cli").exists()
    assert (bob_skills / "internal" / "user-only-cli").is_symlink()
    assert not (alice_skills / "internal" / "user-only-cli").exists()
    assert not (carol_skills / "internal" / "user-only-cli").exists()


def test_sync_profiles_always_distributes_available_lark_skills(tmp_path):
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared_home = tmp_path / "shared"
    business_source = shared_home / "skills" / "Keep" / "kep-hades-cli"
    lark_slides = shared_home / "skills" / "lark-slides"
    lark_wiki = shared_home / "skills" / "lark-wiki"
    lark_drive = shared_home / "skills" / "lark-drive"
    for source in (business_source, lark_slides, lark_wiki, lark_drive):
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text(f"# {source.name}\n", encoding="utf-8")
    (shared_home / "config.yaml").write_text("model:\n  default: zai/glm-5.1\n", encoding="utf-8")
    (shared_home / "skill-distribution.yaml").write_text(
        """
skills:
  - path: Keep/kep-hades-cli
    install_mode: symlink
    audience: all
""",
        encoding="utf-8",
    )
    profiles_root = tmp_path / "profiles"
    snapshot = build_org_snapshot(
        [Department(dept_id="od_ops", name="Ops", leader_user_id="alice")],
        {"od_ops": [DepartmentUser(open_id="ou_alice", user_id="alice")]},
    )

    sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)

    profile_skills = profiles_root / "alice" / "skills"
    for name, source in {
        "lark-slides": lark_slides,
        "lark-wiki": lark_wiki,
        "lark-drive": lark_drive,
    }.items():
        target = profile_skills / name
        assert target.is_symlink()
        assert target.resolve() == source.resolve()
    manifest = json.loads((profile_skills / ".hermes-managed.json").read_text(encoding="utf-8"))
    assert manifest["skills"]["lark-slides"]["source"] == str(lark_slides)
    assert manifest["skills"]["lark-slides"]["install_mode"] == "symlink"
    assert manifest["skills"]["lark-slides"]["share_with_children"] is True


def test_sync_profiles_lark_auto_distribution_is_idempotent_and_preserves_personal(tmp_path):
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared_home = tmp_path / "shared"
    business_source = shared_home / "skills" / "Keep" / "kep-hades-cli"
    lark_slides = shared_home / "skills" / "lark-slides"
    for source in (business_source, lark_slides):
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text(f"# {source.name}\n", encoding="utf-8")
    (shared_home / "config.yaml").write_text("model:\n  default: zai/glm-5.1\n", encoding="utf-8")
    (shared_home / "skill-distribution.yaml").write_text(
        """
skills:
  - path: Keep/kep-hades-cli
    install_mode: symlink
    audience: all
""",
        encoding="utf-8",
    )
    profiles_root = tmp_path / "profiles"
    snapshot = build_org_snapshot(
        [Department(dept_id="od_ops", name="Ops", leader_user_id="alice")],
        {"od_ops": [DepartmentUser(open_id="ou_alice", user_id="alice")]},
    )

    first = sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)
    profile_skills = profiles_root / "alice" / "skills"
    personal = profile_skills / "personal-tool"
    personal.mkdir()
    (personal / "SKILL.md").write_text("# Personal\n", encoding="utf-8")
    second = sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)

    assert first["created"] == 1
    assert second["kept"] == 1
    assert (profile_skills / "lark-slides").is_symlink()
    assert not (profile_skills / "lark-wiki").exists()
    assert (personal / "SKILL.md").read_text(encoding="utf-8") == "# Personal\n"


def test_sync_profiles_lark_auto_distribution_respects_secret_guard(tmp_path):
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared_home = tmp_path / "shared"
    lark_secret = shared_home / "skills" / "lark-secret"
    lark_secret.mkdir(parents=True)
    (lark_secret / "SKILL.md").write_text("# Lark Secret\n", encoding="utf-8")
    (lark_secret / ".env").write_text("TOKEN=do-not-link\n", encoding="utf-8")
    (shared_home / "config.yaml").write_text("model:\n  default: zai/glm-5.1\n", encoding="utf-8")
    profiles_root = tmp_path / "profiles"
    snapshot = build_org_snapshot(
        [Department(dept_id="od_ops", name="Ops", leader_user_id="alice")],
        {"od_ops": [DepartmentUser(open_id="ou_alice", user_id="alice")]},
    )

    sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)

    installed = profiles_root / "alice" / "skills" / "lark-secret"
    assert installed.exists()
    assert not installed.is_symlink()
    assert (installed / "SKILL.md").exists()
    assert not (installed / ".env").exists()
    manifest = json.loads((installed.parent / ".hermes-managed.json").read_text(encoding="utf-8"))
    entry = manifest["skills"]["lark-secret"]
    assert entry["install_mode"] == "copy"
    assert entry["requested_install_mode"] == "symlink"
    assert entry["secret_guard"] == "copy_filtered"


def test_sync_profiles_prunes_removed_managed_skill_but_preserves_self_installed(tmp_path):
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared_home = tmp_path / "shared"
    managed_source = shared_home / "skills" / "Keep" / "kep-hades-cli"
    removed_source = shared_home / "skills" / "internal" / "old-cli"
    managed_source.mkdir(parents=True)
    removed_source.mkdir(parents=True)
    (managed_source / "SKILL.md").write_text("# Hades\n", encoding="utf-8")
    (removed_source / "SKILL.md").write_text("# Old\n", encoding="utf-8")
    (shared_home / "config.yaml").write_text("model:\n  default: zai/glm-5.1\n", encoding="utf-8")
    (shared_home / "skill-distribution.yaml").write_text(
        """
skills:
  - path: Keep/kep-hades-cli
    install_mode: symlink
    audience: all
  - path: internal/old-cli
    install_mode: symlink
    audience: all
""",
        encoding="utf-8",
    )
    profiles_root = tmp_path / "profiles"
    snapshot = build_org_snapshot(
        [Department(dept_id="od_ops", name="Ops", leader_user_id="alice")],
        {"od_ops": [DepartmentUser(open_id="ou_alice", user_id="alice")]},
    )
    sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)
    profile_skills = profiles_root / "alice" / "skills"
    self_installed = profile_skills / "personal-tool"
    self_installed.mkdir()
    (self_installed / "SKILL.md").write_text("# Personal\n", encoding="utf-8")

    (shared_home / "skill-distribution.yaml").write_text(
        """
skills:
  - path: Keep/kep-hades-cli
    install_mode: symlink
    audience: all
""",
        encoding="utf-8",
    )
    sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)

    assert (profile_skills / "Keep" / "kep-hades-cli").is_symlink()
    assert not (profile_skills / "internal" / "old-cli").exists()
    assert (self_installed / "SKILL.md").read_text(encoding="utf-8") == "# Personal\n"


def test_sync_profiles_can_switch_managed_symlink_between_versions(tmp_path):
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared_home = tmp_path / "shared"
    v1_source = shared_home / "skill-releases" / "keep-record" / "v1"
    v2_source = shared_home / "skill-releases" / "keep-record" / "v2"
    v1_source.mkdir(parents=True)
    v2_source.mkdir(parents=True)
    (v1_source / "SKILL.md").write_text("# Keep Record v1\n", encoding="utf-8")
    (v2_source / "SKILL.md").write_text("# Keep Record v2\n", encoding="utf-8")
    (shared_home / "config.yaml").write_text("model:\n  default: zai/glm-5.1\n", encoding="utf-8")
    profiles_root = tmp_path / "profiles"
    snapshot = build_org_snapshot(
        [Department(dept_id="od_ops", name="Ops", leader_user_id="alice")],
        {"od_ops": [DepartmentUser(open_id="ou_alice", user_id="alice")]},
    )

    (shared_home / "skill-distribution.yaml").write_text(
        """
skills:
  - path: Keep/keep-record
    source: skill-releases/keep-record/v1
    version: v1
    install_mode: symlink
    audience: all
""",
        encoding="utf-8",
    )
    sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)
    target = profiles_root / "alice" / "skills" / "Keep" / "keep-record"
    assert target.resolve() == v1_source.resolve()

    (shared_home / "skill-distribution.yaml").write_text(
        """
skills:
  - path: Keep/keep-record
    source: skill-releases/keep-record/v2
    version: v2
    install_mode: symlink
    audience: all
""",
        encoding="utf-8",
    )
    sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)

    manifest = json.loads((profiles_root / "alice" / "skills" / ".hermes-managed.json").read_text(encoding="utf-8"))
    assert target.resolve() == v2_source.resolve()
    assert manifest["skills"]["Keep/keep-record"]["version"] == "v2"


def test_child_profile_inherits_only_child_shareable_skill_packages_without_tokens(tmp_path):
    from hermes_multitenancy.sync.feishu_org import Employee, _sync_default_profile_skills

    shared_home = tmp_path / "shared"
    free_source = shared_home / "skills" / "internal" / "readonly-report"
    oauth_source = shared_home / "skills" / "internal" / "personal-oauth"
    free_source.mkdir(parents=True)
    oauth_source.mkdir(parents=True)
    (free_source / "SKILL.md").write_text("# Readonly Report\n", encoding="utf-8")
    (oauth_source / "SKILL.md").write_text("# Personal OAuth\n", encoding="utf-8")
    (shared_home / "skill-distribution.yaml").write_text(
        """
skills:
  - path: internal/readonly-report
    install_mode: symlink
    audience:
      departments: [od_sales]
    requires_token: false
  - path: internal/personal-oauth
    install_mode: symlink
    audience:
      departments: [od_sales]
    token_policy: user_oauth
""",
        encoding="utf-8",
    )
    owner_home = shared_home / "profiles" / "alice"
    owner = Employee(
        open_id="ou_alice",
        user_id="alice",
        agent_id="alice",
        profile_name="alice",
        name="Alice",
        dept_id="od_sales",
        dept_name="Sales",
        leader_user_id=None,
    )

    assert _sync_default_profile_skills(owner_home, shared_home, owner) is True
    (owner_home / "tokens").mkdir(parents=True)
    (owner_home / "tokens" / "personal-oauth.json").write_text("secret", encoding="utf-8")

    child_home = shared_home / "profiles" / "feishu_group_sales"
    assert _sync_default_profile_skills(
        child_home,
        shared_home,
        upstream_profile_home=owner_home,
    ) is True

    child_skills = child_home / "skills"
    inherited = child_skills / "internal" / "readonly-report"
    assert inherited.is_symlink()
    assert inherited.resolve() == free_source.resolve()
    assert not (child_skills / "internal" / "personal-oauth").exists()
    assert not (child_home / "tokens" / "personal-oauth.json").exists()
    manifest = json.loads((child_skills / ".hermes-managed.json").read_text(encoding="utf-8"))
    assert manifest["skills"]["internal/readonly-report"]["inherited_from"] == "alice"
    assert manifest["skills"]["internal/readonly-report"]["requires_token"] is False


def test_child_profile_does_not_receive_all_audience_user_token_skills(tmp_path):
    from hermes_multitenancy.sync.feishu_org import Employee, _sync_default_profile_skills

    shared_home = tmp_path / "shared"
    free_source = shared_home / "skills" / "internal" / "team-readonly"
    oauth_source = shared_home / "skills" / "internal" / "all-user-oauth"
    free_source.mkdir(parents=True)
    oauth_source.mkdir(parents=True)
    (free_source / "SKILL.md").write_text("# Team Readonly\n", encoding="utf-8")
    (oauth_source / "SKILL.md").write_text("# User OAuth\n", encoding="utf-8")
    (shared_home / "skill-distribution.yaml").write_text(
        """
skills:
  - path: internal/team-readonly
    audience: all
    requires_token: false
  - path: internal/all-user-oauth
    audience: all
    token_policy: user_oauth
""",
        encoding="utf-8",
    )
    owner_home = shared_home / "profiles" / "alice"
    owner = Employee(
        open_id="ou_alice",
        user_id="alice",
        agent_id="alice",
        profile_name="alice",
        name="Alice",
        dept_id="",
        dept_name="",
        leader_user_id=None,
    )

    assert _sync_default_profile_skills(owner_home, shared_home, owner) is True
    assert (owner_home / "skills" / "internal" / "all-user-oauth").is_symlink()

    child_home = shared_home / "profiles" / "feishu_group_sales"
    assert _sync_default_profile_skills(
        child_home,
        shared_home,
        upstream_profile_home=owner_home,
    ) is True

    assert (child_home / "skills" / "internal" / "team-readonly").is_symlink()
    assert not (child_home / "skills" / "internal" / "all-user-oauth").exists()


def test_symlink_distribution_falls_back_to_filtered_copy_when_source_contains_secret_files(tmp_path):
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared_home = tmp_path / "shared"
    skill_source = shared_home / "skills" / "internal" / "unsafe-shared"
    skill_source.mkdir(parents=True)
    (skill_source / "SKILL.md").write_text("# Unsafe Shared\n", encoding="utf-8")
    (skill_source / ".env").write_text("TOKEN=do-not-link\n", encoding="utf-8")
    (shared_home / "skill-distribution.yaml").write_text(
        """
skills:
  - path: internal/unsafe-shared
    audience: all
    install_mode: symlink
""",
        encoding="utf-8",
    )
    profiles_root = tmp_path / "profiles"
    snapshot = build_org_snapshot(
        [Department(dept_id="od_root", name="Root")],
        {"od_root": [DepartmentUser(open_id="ou_a", user_id="alice")]},
    )

    sync_profiles(snapshot, profiles_root=profiles_root, source_home=shared_home)

    installed = profiles_root / "alice" / "skills" / "internal" / "unsafe-shared"
    assert installed.exists()
    assert not installed.is_symlink()
    assert (installed / "SKILL.md").exists()
    assert not (installed / ".env").exists()
    manifest = json.loads((installed.parent.parent / ".hermes-managed.json").read_text(encoding="utf-8"))
    entry = manifest["skills"]["internal/unsafe-shared"]
    assert entry["install_mode"] == "copy"
    assert entry["requested_install_mode"] == "symlink"
    assert entry["secret_guard"] == "copy_filtered"


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


def test_contact_client_uses_vault_app_credential_without_legacy_tools(monkeypatch, tmp_path):
    """Clean upstream no longer ships tools.feishu_oapi_client; org sync still works."""
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.sync import FeishuContactClient

    shared_home = tmp_path / "shared"
    shared_home.mkdir()
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    fake_tools = types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", fake_tools)
    monkeypatch.delitem(sys.modules, "tools.feishu_oapi_client", raising=False)

    store = CredentialStore(shared_home / "multitenancy.db", encryption_key="test-key")
    try:
        store.put_credential(
            profile_name="__global__",
            subject_id="feishu_app",
            provider="feishu",
            secret_kind="app",
            payload={"app_id": "cli_x", "app_secret": "sec_x", "domain": "feishu"},
        )
    finally:
        store.close()

    seen_urls: list[str] = []
    seen_auth_headers: list[str] = []

    class FakeResponse:
        def __init__(self, payload: dict):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

    def fake_urlopen(request, timeout=0):
        seen_urls.append(request.full_url)
        auth_header = request.headers.get("Authorization") or ""
        if auth_header:
            seen_auth_headers.append(auth_header)
        if request.full_url.endswith("/open-apis/auth/v3/tenant_access_token/internal"):
            return FakeResponse({"code": 0, "tenant_access_token": "tenant-token"})
        return FakeResponse({
            "code": 0,
            "msg": "ok",
            "data": {
                "items": [{
                    "open_id": "ou_alice",
                    "user_id": "alice",
                    "union_id": "on_alice",
                }]
            },
        })

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    contact = FeishuContactClient.for_current_home(api_delay=0)
    users = contact.fetch_department_users("od_sales")

    assert [u.open_id for u in users] == ["ou_alice"]
    assert any("/open-apis/contact/v3/users/find_by_department" in url for url in seen_urls)
    assert seen_auth_headers == ["Bearer tenant-token"]


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
