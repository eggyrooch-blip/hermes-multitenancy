#!/usr/bin/env python3
"""Hermes skills unified-management UAT matrix.

This is intentionally not a pytest test. It builds isolated Hermes homes and
emits a JSON evidence file that can be attached to review/ship notes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _write_skill(path: Path, name: str, body: str = "") -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n{body}\n",
        encoding="utf-8",
    )


def _ok(name: str, **evidence: Any) -> dict[str, Any]:
    return {"name": name, "ok": True, **evidence}


def _fail(name: str, reason: str, **evidence: Any) -> dict[str, Any]:
    return {"name": name, "ok": False, "reason": reason, **evidence}


def _assert(condition: bool, reason: str) -> None:
    if not condition:
        raise AssertionError(reason)


class CaseFailure(AssertionError):
    def __init__(self, reason: str, **evidence: Any) -> None:
        super().__init__(reason)
        self.evidence = evidence


def _run_case(name: str, fn) -> dict[str, Any]:
    started = time.time()
    try:
        evidence = fn()
        if not isinstance(evidence, dict):
            evidence = {"evidence": evidence}
        return _ok(name, duration_ms=round((time.time() - started) * 1000), **evidence)
    except CaseFailure as exc:
        return _fail(
            name,
            f"{exc.__class__.__name__}: {exc}",
            duration_ms=round((time.time() - started) * 1000),
            **exc.evidence,
        )
    except Exception as exc:  # noqa: BLE001 - UAT matrix must keep going.
        return _fail(
            name,
            f"{exc.__class__.__name__}: {exc}",
            duration_ms=round((time.time() - started) * 1000),
        )


def _build_offline_home(root: Path) -> tuple[Path, dict[str, str]]:
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared = root / "shared"
    shared.mkdir(parents=True)
    (shared / "config.yaml").write_text("model:\n  default: test/model\n", encoding="utf-8")

    _write_skill(shared / "skill-releases" / "weather" / "v1", "weather-shared", "v1")
    _write_skill(shared / "skill-releases" / "weather" / "v2", "weather-shared", "v2")
    _write_skill(shared / "skills" / "internal" / "finance-weather", "finance-weather")
    _write_skill(shared / "skills" / "internal" / "personal-oauth", "personal-oauth")
    _write_skill(shared / "skills" / "lark-calendar", "lark-calendar")

    (shared / "skill-distribution.yaml").write_text(
        """
skills:
  - path: weather/shared
    source: skill-releases/weather/v1
    version: v1
    audience: all
    requires_token: false
  - path: internal/finance-weather
    audience:
      departments: [od_finance]
    token_policy: tokenless
  - path: internal/personal-oauth
    audience:
      departments: [od_finance]
    token_policy: user_oauth
  - path: lark-calendar
    audience: all
    token_policy: brokered
""",
        encoding="utf-8",
    )

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
    sync_profiles(snapshot, profiles_root=shared / "profiles", source_home=shared)
    return shared, {"owner": "alice", "other": "bob", "owner_open_id": "ou_alice"}


def case_keep_four_skill_policy_model(tmp_root: Path) -> dict[str, Any]:
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared = tmp_root / "shared"
    shared.mkdir(parents=True)
    (shared / "config.yaml").write_text("model:\n  default: test/model\n", encoding="utf-8")
    for rel, name in (
        ("Keep/keep-record", "keep-record-qr"),
        ("Keep/kep-cli", "kep-cli-oauth"),
        ("Keep/kep-prd-analysis", "kep-prd-analysis-shared-token"),
        ("Keep/kep-hades-cli", "kep-hades-cli-tokenless"),
    ):
        _write_skill(shared / "skills" / rel, name)
    (shared / "skill-distribution.yaml").write_text(
        """
skills:
  - path: Keep/keep-record
    audience:
      departments: [od_keep]
    token_policy: user_qr
  - path: Keep/kep-cli
    audience:
      departments: [od_keep]
    token_policy: user_oauth
  - path: Keep/kep-prd-analysis
    audience:
      departments: [od_keep]
    token_policy: shared
  - path: Keep/kep-hades-cli
    audience: all
    token_policy: tokenless
""",
        encoding="utf-8",
    )
    snapshot = build_org_snapshot(
        [
            Department(dept_id="od_keep", name="Keep", leader_user_id="alice"),
            Department(dept_id="od_ops", name="Ops", leader_user_id="bob"),
        ],
        {
            "od_keep": [DepartmentUser(open_id="ou_alice", user_id="alice")],
            "od_ops": [DepartmentUser(open_id="ou_bob", user_id="bob")],
        },
    )
    sync_profiles(snapshot, profiles_root=shared / "profiles", source_home=shared)
    alice = shared / "profiles" / "alice" / "skills" / "Keep"
    bob = shared / "profiles" / "bob" / "skills" / "Keep"
    _assert((alice / "keep-record").is_symlink(), "keep-record should install for Keep department")
    _assert((alice / "kep-cli").is_symlink(), "kep-cli should install for Keep department")
    _assert((alice / "kep-prd-analysis").is_symlink(), "shared-token skill should install for Keep department")
    _assert((alice / "kep-hades-cli").is_symlink(), "tokenless default should install for Keep user")
    _assert(not (bob / "keep-record").exists(), "ops user should not get Keep QR skill")
    _assert(not (bob / "kep-cli").exists(), "ops user should not get Keep OAuth skill")
    _assert((bob / "kep-hades-cli").is_symlink(), "ops user should get all-audience tokenless skill")
    return {
        "keep_user_skills": sorted(path.name for path in alice.iterdir()),
        "ops_user_skills": sorted(path.name for path in bob.iterdir()),
    }


def case_distribution_and_versions(tmp_root: Path) -> dict[str, Any]:
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared, ids = _build_offline_home(tmp_root)
    owner = shared / "profiles" / ids["owner"]
    other = shared / "profiles" / ids["other"]
    weather = owner / "skills" / "weather" / "shared"
    finance = owner / "skills" / "internal" / "finance-weather"

    _assert(weather.is_symlink(), "weather/shared must be a profile symlink")
    _assert(weather.resolve() == (shared / "skill-releases" / "weather" / "v1").resolve(), "weather v1 target mismatch")
    _assert(finance.is_symlink(), "finance-weather must be symlinked for finance user")
    _assert(not (other / "skills" / "internal" / "finance-weather").exists(), "ops user should not receive finance skill")
    _assert((owner / "skills" / "lark-calendar").is_symlink(), "lark-calendar should be create-ready as shared brokered skill")

    self_installed = owner / "skills" / "personal-local-weather"
    _write_skill(self_installed, "personal-local-weather")

    (shared / "skill-distribution.yaml").write_text(
        """
skills:
  - path: weather/shared
    source: skill-releases/weather/v2
    version: v2
    audience: all
    requires_token: false
  - path: lark-calendar
    audience: all
    token_policy: brokered
""",
        encoding="utf-8",
    )
    snapshot = build_org_snapshot(
        [Department(dept_id="od_finance", name="Finance", leader_user_id="alice")],
        {"od_finance": [DepartmentUser(open_id="ou_alice", user_id="alice")]},
    )
    sync_profiles(snapshot, profiles_root=shared / "profiles", source_home=shared)

    manifest = json.loads((owner / "skills" / ".hermes-managed.json").read_text(encoding="utf-8"))
    _assert(weather.resolve() == (shared / "skill-releases" / "weather" / "v2").resolve(), "weather symlink did not switch to v2")
    _assert(manifest["skills"]["weather/shared"]["version"] == "v2", "managed manifest version not updated")
    _assert(self_installed.joinpath("SKILL.md").exists(), "self-installed skill was pruned")
    _assert(not finance.exists(), "removed managed finance skill was not pruned")

    stable_weather_path = str(weather.relative_to(owner / "skills"))
    (shared / "skill-distribution.yaml").write_text(
        """
skills:
  - path: weather/shared
    source: skill-releases/weather/v1
    version: v1
    audience: all
    requires_token: false
  - path: lark-calendar
    audience: all
    token_policy: brokered
""",
        encoding="utf-8",
    )
    sync_profiles(snapshot, profiles_root=shared / "profiles", source_home=shared)
    rollback_manifest = json.loads((owner / "skills" / ".hermes-managed.json").read_text(encoding="utf-8"))
    rollback_target = weather.resolve()
    lark_calendar_manifest = rollback_manifest["skills"]["lark-calendar"]
    _assert(str(weather.relative_to(owner / "skills")) == stable_weather_path, "profile skill path changed during rollback")
    _assert(rollback_target == (shared / "skill-releases" / "weather" / "v1").resolve(), "weather symlink did not roll back to v1")
    _assert(rollback_manifest["skills"]["weather/shared"]["version"] == "v1", "managed manifest version did not roll back")
    _assert(lark_calendar_manifest["install_mode"] == "symlink", "lark-calendar brokered skill should be symlinked")
    _assert(lark_calendar_manifest["token_policy"] == "brokered", "lark-calendar brokered token policy missing")
    _assert(lark_calendar_manifest["share_with_children"] is True, "lark-calendar brokered skill should be child-shareable")
    _assert(self_installed.joinpath("SKILL.md").exists(), "self-installed skill was pruned during rollback")
    return {
        "shared_home": str(shared),
        "weather_target": str((shared / "skill-releases" / "weather" / "v2").resolve()),
        "manifest_version": manifest["skills"]["weather/shared"]["version"],
        "stable_profile_skill_path": stable_weather_path,
        "rollback_weather_target": str(rollback_target),
        "rollback_manifest_version": rollback_manifest["skills"]["weather/shared"]["version"],
        "lark_calendar_install_mode": lark_calendar_manifest["install_mode"],
        "lark_calendar_token_policy": lark_calendar_manifest["token_policy"],
        "lark_calendar_share_with_children": lark_calendar_manifest["share_with_children"],
        "self_installed_preserved": True,
    }


def case_profile_user_audience_distribution(tmp_root: Path) -> dict[str, Any]:
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared = tmp_root / "shared"
    profile_source = shared / "skills" / "internal" / "profile-only-cli"
    user_source = shared / "skills" / "internal" / "user-only-cli"
    _write_skill(profile_source, "profile-only-cli")
    _write_skill(user_source, "user-only-cli")
    (shared / "config.yaml").write_text("model:\n  default: test/model\n", encoding="utf-8")
    (shared / "skill-distribution.yaml").write_text(
        """
skills:
  - path: internal/profile-only-cli
    audience:
      profiles: [alice]
  - path: internal/user-only-cli
    audience:
      users: [bob]
""",
        encoding="utf-8",
    )

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
    sync_profiles(snapshot, profiles_root=shared / "profiles", source_home=shared)
    alice = shared / "profiles" / "alice" / "skills"
    bob = shared / "profiles" / "bob" / "skills"
    carol = shared / "profiles" / "carol" / "skills"
    _assert((alice / "internal" / "profile-only-cli").is_symlink(), "profile audience skill missing for alice")
    _assert(not (bob / "internal" / "profile-only-cli").exists(), "profile audience skill leaked to bob")
    _assert(not (carol / "internal" / "profile-only-cli").exists(), "profile audience skill leaked to carol")
    _assert((bob / "internal" / "user-only-cli").is_symlink(), "user audience skill missing for bob")
    _assert(not (alice / "internal" / "user-only-cli").exists(), "user audience skill leaked to alice")
    _assert(not (carol / "internal" / "user-only-cli").exists(), "user audience skill leaked to carol")
    return {
        "profile_audience_profile": "alice",
        "user_audience_user": "bob",
        "carol_received_targeted_skill": False,
    }


def case_hermes_loader_discovers_symlinked_skills(tmp_root: Path) -> dict[str, Any]:
    from agent import skill_utils

    shared = tmp_root / "shared"
    weather_source = shared / "skill-releases" / "weather" / "v2"
    lark_source = shared / "skills" / "lark-calendar"
    _write_skill(weather_source, "weather-shared", "v2")
    _write_skill(lark_source, "lark-calendar")

    profile = shared / "profiles" / "alice"
    weather_target = profile / "skills" / "weather" / "shared"
    lark_target = profile / "skills" / "lark-calendar"
    weather_target.parent.mkdir(parents=True, exist_ok=True)
    lark_target.parent.mkdir(parents=True, exist_ok=True)
    weather_target.symlink_to(weather_source, target_is_directory=True)
    lark_target.symlink_to(lark_source, target_is_directory=True)

    matches = list(skill_utils.iter_skill_index_files(profile / "skills", "SKILL.md"))
    relative = sorted(str(path.relative_to(profile)) for path in matches)
    weather_skill_discovered = "skills/weather/shared/SKILL.md" in relative
    lark_skill_discovered = "skills/lark-calendar/SKILL.md" in relative
    _assert(weather_skill_discovered, "Hermes loader did not discover symlinked weather skill")
    _assert(lark_skill_discovered, "Hermes loader did not discover symlinked lark skill")
    return {
        "loader_checked": True,
        "discovered_count": len(relative),
        "weather_skill_discovered": weather_skill_discovered,
        "lark_skill_discovered": lark_skill_discovered,
        "discovered_relative_paths": relative,
    }


def case_new_hire_sync_auto_installs_managed_skills(tmp_root: Path) -> dict[str, Any]:
    from hermes_multitenancy.sync import Department, DepartmentUser, build_org_snapshot, sync_profiles

    shared = tmp_root / "shared"
    profiles = shared / "profiles"
    shared.mkdir(parents=True)
    (shared / "config.yaml").write_text("model:\n  default: test/model\n", encoding="utf-8")
    _write_skill(shared / "skill-releases" / "weather" / "v1", "weather-shared", "v1")
    _write_skill(shared / "skill-releases" / "weather" / "v2", "weather-shared", "v2")
    _write_skill(shared / "skills" / "internal" / "finance-weather", "finance-weather")
    _write_skill(shared / "skills" / "lark-calendar", "lark-calendar")
    (shared / "skill-distribution.yaml").write_text(
        """
skills:
  - path: weather/shared
    source: skill-releases/weather/v1
    version: v1
    audience: all
    requires_token: false
  - path: internal/finance-weather
    audience:
      departments: [od_finance]
    token_policy: tokenless
  - path: lark-calendar
    audience: all
    token_policy: brokered
""",
        encoding="utf-8",
    )

    departments = [Department(dept_id="od_finance", name="Finance", leader_user_id="alice")]
    initial_snapshot = build_org_snapshot(
        departments,
        {"od_finance": [DepartmentUser(open_id="ou_alice", user_id="alice")]},
    )
    initial_stats = sync_profiles(initial_snapshot, profiles_root=profiles, source_home=shared)

    (shared / "skill-distribution.yaml").write_text(
        """
skills:
  - path: weather/shared
    source: skill-releases/weather/v2
    version: v2
    audience: all
    requires_token: false
  - path: internal/finance-weather
    audience:
      departments: [od_finance]
    token_policy: tokenless
  - path: lark-calendar
    audience: all
    token_policy: brokered
""",
        encoding="utf-8",
    )
    new_hire_snapshot = build_org_snapshot(
        departments,
        {
            "od_finance": [
                DepartmentUser(open_id="ou_alice", user_id="alice"),
                DepartmentUser(open_id="ou_carol", user_id="carol"),
            ],
        },
    )
    new_hire_stats = sync_profiles(new_hire_snapshot, profiles_root=profiles, source_home=shared)
    new_hire = profiles / "carol"
    manifest = json.loads((new_hire / "skills" / ".hermes-managed.json").read_text(encoding="utf-8"))
    weather_manifest = manifest["skills"]["weather/shared"]
    lark_manifest = manifest["skills"]["lark-calendar"]
    weather = new_hire / "skills" / "weather" / "shared"

    _assert(new_hire.is_dir(), "new hire profile was not created")
    _assert(weather.is_symlink(), "new hire weather skill was not symlinked")
    _assert(weather.resolve() == (shared / "skill-releases" / "weather" / "v2").resolve(), "new hire weather did not get current v2")
    _assert(weather_manifest["version"] == "v2", "new hire weather manifest version missing")
    _assert((new_hire / "skills" / "internal" / "finance-weather").is_symlink(), "new hire did not get department skill")
    _assert(lark_manifest["token_policy"] == "brokered", "new hire lark-calendar token policy missing")
    _assert(lark_manifest["share_with_children"] is True, "new hire brokered lark skill is not child-shareable")

    personal = new_hire / "skills" / "personal" / "new-hire-notes"
    _write_skill(personal, "new-hire-notes")
    resync_stats = sync_profiles(new_hire_snapshot, profiles_root=profiles, source_home=shared)
    _assert(personal.joinpath("SKILL.md").exists(), "new hire personal install was pruned by later sync")

    return {
        "initial_stats": initial_stats,
        "new_hire_stats": new_hire_stats,
        "resync_stats": resync_stats,
        "new_hire_profile_created": new_hire.is_dir(),
        "new_hire_weather_install_mode": weather_manifest["install_mode"],
        "new_hire_weather_version": weather_manifest["version"],
        "new_hire_weather_target": str(weather.resolve()),
        "new_hire_lark_calendar_token_policy": lark_manifest["token_policy"],
        "new_hire_lark_calendar_share_with_children": lark_manifest["share_with_children"],
        "new_hire_finance_skill": (new_hire / "skills" / "internal" / "finance-weather").is_symlink(),
        "new_hire_personal_install_preserved_after_resync": personal.joinpath("SKILL.md").exists(),
    }


def case_child_inherits_skills_not_tokens(tmp_root: Path) -> dict[str, Any]:
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.router import _ensure_group_profile

    shared, ids = _build_offline_home(tmp_root)
    owner = shared / "profiles" / ids["owner"]
    owner.joinpath("tokens").mkdir(parents=True, exist_ok=True)
    owner.joinpath("tokens", "personal-oauth.json").write_text("do-not-copy", encoding="utf-8")
    owner.joinpath("feishu_uat").mkdir(exist_ok=True)
    owner.joinpath("feishu_uat", f"{ids['owner_open_id']}.json").write_text("do-not-copy", encoding="utf-8")

    router_mod.override_routing_table(":memory:")
    try:
        table = router_mod._get_routing_table()
        _assert(table is not None, "routing table unavailable")
        table.upsert(
            user_id=ids["owner"],
            profile_name=ids["owner"],
            open_id=ids["owner_open_id"],
            provenance="sync",
        )
        group = shared / "profiles" / "feishu_group_weather"
        _ensure_group_profile(
            profile_name="feishu_group_weather",
            profile_home=group,
            chat_id="oc_weather",
            owner_open_id=ids["owner_open_id"],
            display_label="Finance Weather",
        )
    finally:
        router_mod.override_routing_table(None)

    _assert((group / "skills" / "weather" / "shared").is_symlink(), "group did not inherit tokenless weather skill")
    _assert((group / "skills" / "internal" / "finance-weather").is_symlink(), "group did not inherit tokenless department skill")
    _assert(not (group / "skills" / "internal" / "personal-oauth").exists(), "group inherited user_oauth skill")
    _assert(not list((group / "tokens").glob("*")), "group copied owner token files")
    _assert(not list((group / "feishu_uat").glob("*.json")), "group copied owner Feishu UAT")
    marker = json.loads((group / "group_profile.json").read_text(encoding="utf-8"))
    return {
        "group_profile": group.name,
        "owner_open_id": marker["owner_open_id"],
        "token_files": 0,
        "uat_files": 0,
    }


def case_webui_child_agent_inherits_skills_not_tokens(tmp_root: Path) -> dict[str, Any]:
    from hermes_multitenancy.sync.feishu_org import _sync_default_profile_skills

    shared, ids = _build_offline_home(tmp_root)
    owner = shared / "profiles" / ids["owner"]
    owner.joinpath("tokens").mkdir(parents=True, exist_ok=True)
    owner.joinpath("tokens", "personal-oauth.json").write_text("do-not-copy", encoding="utf-8")
    owner.joinpath("feishu_uat").mkdir(exist_ok=True)
    owner.joinpath("feishu_uat", f"{ids['owner_open_id']}.json").write_text("do-not-copy", encoding="utf-8")

    webui_child = shared / "profiles" / "webui_child_research"
    webui_child.mkdir(parents=True)
    changed = _sync_default_profile_skills(
        webui_child,
        shared,
        upstream_profile_home=owner,
    )

    manifest = json.loads((webui_child / "skills" / ".hermes-managed.json").read_text(encoding="utf-8"))["skills"]
    weather_manifest = manifest["weather/shared"]
    lark_manifest = manifest["lark-calendar"]
    _assert(changed is True, "webui child sync did not create inherited skills")
    _assert((webui_child / "skills" / "weather" / "shared").is_symlink(), "webui child did not inherit tokenless weather skill")
    _assert((webui_child / "skills" / "lark-calendar").is_symlink(), "webui child did not inherit brokered lark skill")
    _assert(not (webui_child / "skills" / "internal" / "personal-oauth").exists(), "webui child inherited user_oauth skill")
    _assert(not list((webui_child / "tokens").glob("*")), "webui child copied owner token files")
    _assert(not list((webui_child / "feishu_uat").glob("*.json")), "webui child copied owner Feishu UAT")
    _assert(weather_manifest["inherited_from"] == ids["owner"], "webui child weather manifest lost upstream profile")
    _assert(lark_manifest["inherited_from"] == ids["owner"], "webui child lark manifest lost upstream profile")
    return {
        "webui_child_profile": webui_child.name,
        "inherited_from": weather_manifest["inherited_from"],
        "weather_skill": True,
        "weather_install_mode": weather_manifest["install_mode"],
        "weather_token_policy": weather_manifest["token_policy"],
        "lark_calendar_skill": True,
        "lark_calendar_install_mode": lark_manifest["install_mode"],
        "lark_calendar_token_policy": lark_manifest["token_policy"],
        "personal_oauth_skill": False,
        "token_files": 0,
        "uat_files": 0,
    }


def case_child_install_does_not_sync_back_to_parent(tmp_root: Path) -> dict[str, Any]:
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.router import _ensure_group_profile
    from hermes_multitenancy.skill_registry import install_shared_skill_for_profile

    shared, ids = _build_offline_home(tmp_root)
    child_only = shared / "skills" / "child" / "group-only-tool"
    _write_skill(child_only, "group-only-tool")
    owner = shared / "profiles" / ids["owner"]

    router_mod.override_routing_table(":memory:")
    try:
        table = router_mod._get_routing_table()
        _assert(table is not None, "routing table unavailable")
        table.upsert(user_id=ids["owner"], profile_name=ids["owner"], open_id=ids["owner_open_id"], provenance="sync")
        group = shared / "profiles" / "feishu_group_reverse"
        _ensure_group_profile(
            profile_name="feishu_group_reverse",
            profile_home=group,
            chat_id="oc_reverse",
            owner_open_id=ids["owner_open_id"],
            display_label="Reverse Test",
        )
    finally:
        router_mod.override_routing_table(None)

    install_shared_skill_for_profile(
        shared_home=shared,
        profile_home=group,
        skill_path="child/group-only-tool",
    )
    _assert((group / "skills" / "child" / "group-only-tool").is_symlink(), "child personal install missing")
    _assert(not (owner / "skills" / "child" / "group-only-tool").exists(), "child skill synced back to owner")
    return {"group_personal_install": True, "owner_received_child_install": False}


def case_shared_token_materialization_is_scoped(tmp_root: Path) -> dict[str, Any]:
    from hermes_multitenancy.credential_materializer import materialize_credentials
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.routing import RoutingTable

    old_key = os.environ.get("HERMES_MULTITENANCY_CREDENTIAL_KEY")
    os.environ["HERMES_MULTITENANCY_CREDENTIAL_KEY"] = "skills-uat-test-key"
    try:
        shared = tmp_root / "shared"
        profiles = shared / "profiles"
        for name in ("alice", "bob", "feishu_group_shared"):
            (profiles / name).mkdir(parents=True, exist_ok=True)
        table = RoutingTable(shared / "multitenancy.db")
        try:
            table.upsert(user_id="alice", profile_name="alice", open_id="ou_alice", provenance="sync")
            table.upsert(user_id="bob", profile_name="bob", open_id="ou_bob", provenance="sync")
            table.upsert_group(chat_id="oc_shared", profile_name="feishu_group_shared", owner_open_id="ou_alice", upstream_profile="alice")
        finally:
            table.close()
        (shared / "credential-materialization.yaml").write_text(
            """
credentials:
  - subject_id: kep-prd-analysis
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
                subject_id="kep-prd-analysis",
                provider="gitlab",
                secret_kind="token",
                payload={"token": "shared-token-secret"},
            )
        finally:
            store.close()
        stats = materialize_credentials(shared_home=shared)
    finally:
        if old_key is None:
            os.environ.pop("HERMES_MULTITENANCY_CREDENTIAL_KEY", None)
        else:
            os.environ["HERMES_MULTITENANCY_CREDENTIAL_KEY"] = old_key

    alice_token = profiles / "alice" / "workspace" / "credentials" / "gitlab.token"
    _assert(alice_token.exists(), "shared token was not materialized to alice")
    _assert(alice_token.stat().st_mode & 0o777 == 0o600, "shared token mode is not 0600")
    _assert(not (profiles / "bob" / "workspace" / "credentials" / "gitlab.token").exists(), "shared token leaked to bob")
    _assert(not (profiles / "feishu_group_shared" / "workspace" / "credentials" / "gitlab.token").exists(), "shared token leaked to group")
    return {
        "written": stats["written"],
        "profiles_targeted": stats["profiles_targeted"],
        "alice_token_mode": oct(alice_token.stat().st_mode & 0o777),
        "bob_has_token": False,
        "group_has_token": False,
    }


def case_wildcard_shared_token_skips_group_profiles(tmp_root: Path) -> dict[str, Any]:
    from hermes_multitenancy.credential_materializer import materialize_credentials
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.routing import RoutingTable

    old_key = os.environ.get("HERMES_MULTITENANCY_CREDENTIAL_KEY")
    os.environ["HERMES_MULTITENANCY_CREDENTIAL_KEY"] = "skills-uat-test-key"
    try:
        shared = tmp_root / "shared"
        profiles = shared / "profiles"
        for name in ("alice", "bob", "feishu_group_shared", "inactive"):
            (profiles / name).mkdir(parents=True, exist_ok=True)
        table = RoutingTable(shared / "multitenancy.db")
        try:
            table.upsert(user_id="alice", profile_name="alice", open_id="ou_alice", provenance="sync")
            table.upsert(user_id="bob", profile_name="bob", open_id="ou_bob", provenance="sync")
            table.upsert_group(chat_id="oc_shared", profile_name="feishu_group_shared", owner_open_id="ou_alice", upstream_profile="alice")
            table.upsert(user_id="inactive", profile_name="inactive", open_id="ou_inactive", provenance="sync")
            table.soft_delete("inactive")
        finally:
            table.close()
        (shared / "credential-materialization.yaml").write_text(
            """
credentials:
  - subject_id: kep-prd-analysis
    provider: gitlab
    secret_kind: token
    target: workspace/credentials/gitlab.token
    profiles: ["*"]
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
                payload={"token": "shared-token-secret"},
            )
        finally:
            store.close()
        stats = materialize_credentials(shared_home=shared)
    finally:
        if old_key is None:
            os.environ.pop("HERMES_MULTITENANCY_CREDENTIAL_KEY", None)
        else:
            os.environ["HERMES_MULTITENANCY_CREDENTIAL_KEY"] = old_key

    token_rel = Path("workspace") / "credentials" / "gitlab.token"
    _assert((profiles / "alice" / token_rel).exists(), "wildcard token missing for active user alice")
    _assert((profiles / "bob" / token_rel).exists(), "wildcard token missing for active user bob")
    _assert(not (profiles / "feishu_group_shared" / token_rel).exists(), "wildcard token leaked to group profile")
    _assert(not (profiles / "inactive" / token_rel).exists(), "wildcard token leaked to inactive user")
    return {
        "profiles_targeted": stats["profiles_targeted"],
        "written": stats["written"],
        "alice_has_token": True,
        "bob_has_token": True,
        "group_has_token": False,
        "inactive_has_token": False,
    }


def case_personal_token_stays_profile_local(tmp_root: Path) -> dict[str, Any]:
    from hermes_multitenancy.skill_storage import get_token_path, write_token

    alice = tmp_root / "profiles" / "alice"
    bob = tmp_root / "profiles" / "bob"
    token_path = write_token("kep-cli", "oauth-token-secret", profile_home=alice)
    bob_token_path = get_token_path("kep-cli", profile_home=bob)
    _assert(token_path.exists(), "alice token missing")
    _assert(token_path.stat().st_mode & 0o777 == 0o600, "alice token mode is not 0600")
    _assert(not bob_token_path.exists(), "personal token leaked to bob")
    return {
        "alice_token_exists": True,
        "alice_token_mode": oct(token_path.stat().st_mode & 0o777),
        "bob_token_exists": False,
    }


def case_registry_audit_and_loop_guard(tmp_root: Path) -> dict[str, Any]:
    from hermes_multitenancy.skill_registry import audit_installed_skills, install_shared_skill_for_profile

    shared, ids = _build_offline_home(tmp_root)
    profile = shared / "profiles" / ids["owner"]
    other_profile = shared / "profiles" / ids["other"]
    personal_source = shared / "skills" / "personal" / "weather-addon"
    _write_skill(personal_source, "weather-addon")
    install_shared_skill_for_profile(
        shared_home=shared,
        profile_home=profile,
        skill_path="personal/weather-addon",
    )

    loop = personal_source / "loop"
    if not loop.exists():
        loop.symlink_to(personal_source, target_is_directory=True)

    unknown = other_profile / "skills" / "local-scratch"
    _write_skill(unknown, "local-scratch")

    report = audit_installed_skills(shared_home=shared)
    owner_report = report["profiles"][ids["owner"]]
    all_rows = [
        row
        for profile_report in report["profiles"].values()
        for row in profile_report.get("skills", [])
    ]
    source_counts: dict[str, int] = {}
    for row in all_rows:
        source = str(row.get("source") or "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1
    by_path = {item["skill_path"]: item for item in owner_report["skills"]}
    _assert(by_path["personal/weather-addon"]["source"] == "personal", "personal install not audited")
    _assert(by_path["weather/shared"]["source"] == "managed", "managed skill not audited")
    _assert(source_counts.get("unknown", 0) >= 1, "unknown local skill not audited")
    return {
        "profile": ids["owner"],
        "profile_count": len(report["profiles"]),
        "audited_profiles": sum(1 for profile_report in report["profiles"].values() if profile_report.get("skills")),
        "source_counts": source_counts,
        "skill_count": len(owner_report["skills"]),
        "personal_install": by_path["personal/weather-addon"]["source"],
        "managed_install": by_path["weather/shared"]["source"],
    }


def case_real_home_skill_inventory(real_home: Path) -> dict[str, Any]:
    from hermes_multitenancy.skill_registry import audit_installed_skills

    profiles_root = real_home / "profiles"
    if not profiles_root.is_dir():
        return {
            "checked": False,
            "reason": "real profiles directory missing",
            "profile_count": 0,
            "audited_profiles": 0,
            "total_skills": 0,
            "source_counts": {},
            "token_file_marker_count": 0,
            "secret_free": True,
        }

    report = audit_installed_skills(shared_home=real_home)
    profiles = report.get("profiles") if isinstance(report.get("profiles"), dict) else {}
    source_counts: dict[str, int] = {}
    warning_counts: dict[str, int] = {}
    token_file_marker_count = 0
    total_skills = 0
    samples: list[dict[str, Any]] = []
    for profile_name, profile_report in sorted(profiles.items()):
        rows = profile_report.get("skills", []) if isinstance(profile_report, dict) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            total_skills += 1
            source = str(row.get("source") or "unknown")
            source_counts[source] = source_counts.get(source, 0) + 1
            if row.get("token_files_present"):
                token_file_marker_count += 1
            for warning in row.get("warnings") or []:
                warning_key = str(warning)
                warning_counts[warning_key] = warning_counts.get(warning_key, 0) + 1
            if len(samples) < 10:
                samples.append({
                    "profile": str(profile_name),
                    "skill_path": row.get("skill_path"),
                    "source": source,
                    "install_mode": row.get("install_mode"),
                    "version": row.get("version"),
                    "token_files_present": bool(row.get("token_files_present")),
                    "warning_count": len(row.get("warnings") or []),
                })

    return {
        "checked": True,
        "profile_count": len(profiles),
        "audited_profiles": sum(
            1
            for profile_report in profiles.values()
            if isinstance(profile_report, dict) and profile_report.get("skills")
        ),
        "total_skills": total_skills,
        "source_counts": source_counts,
        "token_file_marker_count": token_file_marker_count,
        "warning_counts": warning_counts,
        "sample_profiles": sorted(str(name) for name in profiles.keys())[:10],
        "sample_skills": samples,
        "secret_free": True,
    }


def _real_uat_user_info_canaries(real_home: Path) -> list[dict[str, Any]]:
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy import feishu_uat_auth

    db = real_home / "multitenancy.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    users = conn.execute(
        "SELECT profile_name, open_id FROM multitenancy_routing "
        "WHERE active=1 AND kind='user' AND open_id IS NOT NULL ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    store = CredentialStore(db)
    results: list[dict[str, Any]] = []
    try:
        for row in users[:3]:
            profile = row["profile_name"]
            open_id = row["open_id"]
            status = store.get_status(
                profile_name=profile,
                subject_id=open_id,
                provider="feishu",
                secret_kind="uat",
            )
            item: dict[str, Any] = {
                "profile_name": profile,
                "open_id": open_id,
                "status": status["status"],
                "secret_free": True,
            }
            if status["status"] == "expired":
                item["refresh_attempted"] = True
                try:
                    refreshed = feishu_uat_auth.refresh_uat_for_user(
                        profile_name=profile,
                        open_id=open_id,
                        shared_home=real_home,
                    )
                    item["refresh"] = {
                        "ok": True,
                        "expires_at": refreshed.get("expires_at"),
                    }
                    status = store.get_status(
                        profile_name=profile,
                        subject_id=open_id,
                        provider="feishu",
                        secret_kind="uat",
                    )
                    item["status"] = status["status"]
                except Exception as exc:  # noqa: BLE001 - secret-free UAT matrix evidence.
                    item["refresh"] = {
                        "ok": False,
                        "error": str(exc)[:200],
                    }
            if status["status"] == "valid":
                payload = store.get_secret_for_runtime(
                    profile_name=profile,
                    subject_id=open_id,
                    provider="feishu",
                    secret_kind="uat",
                )
                token = str(payload.get("access_token") or "")
                request = urllib.request.Request(
                    "https://open.feishu.cn/open-apis/authen/v1/user_info",
                    headers={"Authorization": f"Bearer {token}"},
                )
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - fixed Feishu host.
                        body = json.loads(response.read().decode("utf-8"))
                    item.update(
                        {
                            "http_status": 200,
                            "feishu_code": body.get("code"),
                            "name_present": bool(((body.get("data") or {}).get("name"))),
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    item.update({"http_status": "error", "error": exc.__class__.__name__})
            results.append(item)
    finally:
        store.close()
    return results


def case_real_uat_user_info(real_home: Path) -> dict[str, Any]:
    results = _real_uat_user_info_canaries(real_home)
    _assert(results, "no real user route to check")
    if not any(item.get("feishu_code") == 0 for item in results):
        refresh_errors = [
            item.get("refresh", {}).get("error")
            for item in results
            if item.get("refresh", {}).get("ok") is False and item.get("refresh", {}).get("error")
        ]
        suffix = f"; refresh_errors={refresh_errors}" if refresh_errors else ""
        raise CaseFailure(f"no valid user UAT canary succeeded{suffix}", results=results)
    return {"results": results}


def case_real_uat_scope_inventory_secret_free(real_home: Path) -> dict[str, Any]:
    from hermes_multitenancy.credentials import CredentialStore

    db = real_home / "multitenancy.db"
    _assert(db.exists(), "real multitenancy.db missing")
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    users = conn.execute(
        "SELECT profile_name, open_id FROM multitenancy_routing "
        "WHERE active=1 AND kind='user' AND open_id IS NOT NULL ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    _assert(users, "no active user route to inspect")
    required_scopes = {
        "auth:user.id:read",
        "offline_access",
        "im:message.send_as_user",
        "docx:document:create",
        "docs:document.content:read",
        "drive:file:upload",
    }
    store = CredentialStore(db)
    results: list[dict[str, Any]] = []
    try:
        for row in users[:5]:
            status = store.get_status(
                profile_name=row["profile_name"],
                subject_id=row["open_id"],
                provider="feishu",
                secret_kind="uat",
                required_scopes=required_scopes,
            )
            scopes = status.get("scopes") if isinstance(status.get("scopes"), list) else []
            missing = status.get("missing_scopes") if isinstance(status.get("missing_scopes"), list) else []
            results.append({
                "profile_name": row["profile_name"],
                "open_id": row["open_id"],
                "status": status.get("status"),
                "scope_count": len(scopes),
                "has_payload": bool(status.get("has_payload")),
                "missing_core_scopes": sorted(str(scope) for scope in missing),
                "expires_at_present": status.get("expires_at") is not None,
                "secret_free": True,
            })
    finally:
        store.close()
    valid_core = [
        item
        for item in results
        if item.get("status") == "valid"
        and item.get("has_payload") is True
        and not item.get("missing_core_scopes")
    ]
    _assert(valid_core, "no valid real user UAT has the required core lark-cli scopes")
    return {
        "checked": True,
        "required_core_scopes": sorted(required_scopes),
        "valid_core_identity_count": len(valid_core),
        "results": results,
        "secret_free": True,
    }


def case_real_tat_bot_token(real_home: Path) -> dict[str, Any]:
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.lark_cli_auth_broker import _mint_tenant_access_token

    db = real_home / "multitenancy.db"
    _assert(db.exists(), "real multitenancy.db missing")
    store = CredentialStore(db)
    try:
        payload = store.get_secret_for_runtime(
            profile_name="__global__",
            subject_id="feishu_app",
            provider="feishu",
            secret_kind="app",
        )
    finally:
        store.close()
    tat = _mint_tenant_access_token(payload, timeout=10)
    _assert(bool(tat), "TAT mint returned empty token")
    return {
        "tat_minted": True,
        "token_length": len(tat),
        "secret_free": True,
    }


async def _interruption_resume_probe() -> dict[str, Any]:
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy import runtime as runtime_mod
    from hermes_multitenancy.router import handle_async
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    router_mod._user_inflight_tasks.clear()
    router_mod._session_history.clear()
    router_mod._session_loaded.clear()
    router_mod.override_session_store(":memory:")

    seen_second = {}
    started = asyncio.Event()

    async def runner(event, home):
        text = getattr(event, "text", "")
        if "天气" in text:
            started.set()
            await asyncio.sleep(60)
        seen_second["text"] = text
        return "continued-ok"

    runtime_mod._default_run_agent = runner
    with tempfile.TemporaryDirectory(prefix="hermes-interrupt-") as tmp:
        home = Path(tmp) / "resume_profile"
        home.mkdir()
        add_spike_route("ou_resume_uat", home)

        class Adapter:
            async def send_typing(self, _chat): pass
            async def send(self, _chat, _msg, *, reply_to=None, metadata=None): pass

        event1 = SimpleNamespace(
            text="帮我生成天气 skill 共享报告，执行时间长一点",
            source=SimpleNamespace(chat_id="chat", user_id="ou_resume_uat", user_id_alt=None, chat_type="dm"),
        )
        event2 = SimpleNamespace(
            text="继续",
            source=SimpleNamespace(chat_id="chat", user_id="ou_resume_uat", user_id_alt=None, chat_type="dm"),
        )
        gateway = SimpleNamespace(adapters={"feishu": Adapter()})
        first = asyncio.create_task(handle_async(event=event1, gateway=gateway))
        await asyncio.wait_for(started.wait(), timeout=1)
        first.cancel()
        try:
            await first
        except asyncio.CancelledError:
            pass
        await handle_async(event=event2, gateway=gateway)
        history = router_mod._session_history[(home.name, "ou_resume_uat")]

    clear_spike_routes()
    router_mod.override_session_store(None)
    return {
        "history": history,
        "second_event_text": seen_second.get("text"),
        "interruption_marker": any("中断或取消" in item["content"] for item in history),
    }


def case_interruption_resume_context(_tmp_root: Path) -> dict[str, Any]:
    result = asyncio.run(_interruption_resume_probe())
    _assert(result["history"][0]["content"].startswith("帮我生成天气"), "interrupted user request missing from history")
    _assert(result["interruption_marker"], "interruption marker missing from history")
    _assert(result["history"][-2]["content"] == "继续", "continue turn missing from history")
    _assert(result["history"][-1]["content"] == "continued-ok", "continue response missing from history")
    return result


async def _continue_turn_reconstructs_interrupted_request_probe(
    followup_text: str = "继续",
) -> dict[str, Any]:
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy import runtime as runtime_mod
    from hermes_multitenancy.router import handle_async
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    previous_default_run_agent = runtime_mod._default_run_agent
    previous_pool = router_mod._pool
    clear_spike_routes()
    router_mod.override_pool(None)
    router_mod._user_inflight_tasks.clear()
    router_mod._user_inflight_history_keys.clear()
    router_mod._suppress_interruption_marker_tasks.clear()
    router_mod._session_history.clear()
    router_mod._session_loaded.clear()
    router_mod.override_session_store(":memory:")

    started = asyncio.Event()
    state: dict[str, Any] = {}
    suffix = "magic" if followup_text == "继续" else "arbitrary"
    user_id = f"ou_resume_reconstruct_{suffix}"
    chat_id = f"chat-{suffix}"

    async def runner(event, home):
        text = getattr(event, "text", "")
        history_key = (home.name, user_id)
        if "天气" in text:
            started.set()
            await asyncio.sleep(60)
        if text == followup_text:
            history_before = router_mod._session_history.get(history_key, [])
            contents = [str(item.get("content") or "") for item in history_before]
            used_previous = (
                any("帮我生成天气 skill 共享报告" in content for content in contents)
                and any("中断或取消" in content for content in contents)
            )
            response = "continued-weather-report-from-interrupted-request" if used_previous else "missing-context"
            state.update({
                "followup_text": followup_text,
                "magic_continue_required": followup_text == "继续",
                "continue_used_previous_request": used_previous,
                "continue_response": response,
                "continue_history_before_response": contents,
                "interruption_marker": any("中断或取消" in content for content in contents),
                "interrupted_request_visible_to_followup": any(
                    "帮我生成天气 skill 共享报告" in content for content in contents
                ),
                "interruption_marker_visible_to_followup": any(
                    "中断或取消" in content for content in contents
                ),
            })
            return response
        return "unexpected-first-response"

    runtime_mod._default_run_agent = runner
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-continue-reconstruct-") as tmp:
            home = Path(tmp) / "resume_reconstruct_profile"
            home.mkdir()
            add_spike_route(user_id, home)

            class Adapter:
                async def send_typing(self, _chat): pass
                async def send(self, _chat, _msg, *, reply_to=None, metadata=None): pass

            event1 = SimpleNamespace(
                text="帮我生成天气 skill 共享报告，执行时间长一点",
                message_id=f"om_resume_{suffix}_first",
                source=SimpleNamespace(
                    chat_id=chat_id,
                    user_id=user_id,
                    user_id_alt=None,
                    chat_type="dm",
                    message_id=f"om_resume_{suffix}_first",
                ),
            )
            event2 = SimpleNamespace(
                text=followup_text,
                message_id=f"om_resume_{suffix}_followup",
                source=SimpleNamespace(
                    chat_id=chat_id,
                    user_id=user_id,
                    user_id_alt=None,
                    chat_type="dm",
                    message_id=f"om_resume_{suffix}_followup",
                ),
            )
            gateway = SimpleNamespace(adapters={"feishu": Adapter()})
            first = asyncio.create_task(handle_async(event=event1, gateway=gateway))
            await asyncio.wait_for(started.wait(), timeout=1)
            first.cancel()
            try:
                await first
            except asyncio.CancelledError:
                pass
            await handle_async(event=event2, gateway=gateway)
            state["final_history"] = router_mod._session_history[(home.name, user_id)]
    finally:
        runtime_mod._default_run_agent = previous_default_run_agent
        router_mod.override_pool(previous_pool)
        clear_spike_routes()
        router_mod.override_session_store(None)

    return state


def case_continue_turn_reconstructs_interrupted_request(_tmp_root: Path) -> dict[str, Any]:
    result = asyncio.run(_continue_turn_reconstructs_interrupted_request_probe())
    _assert(result.get("continue_used_previous_request") is True, "continue turn did not use interrupted request")
    _assert(
        result.get("continue_response") == "continued-weather-report-from-interrupted-request",
        "continue response was not based on interrupted request",
    )
    _assert(
        any("中断或取消" in content for content in result.get("continue_history_before_response") or []),
        "continue turn did not see interruption marker before answering",
    )
    return result


def case_interruption_arbitrary_followup_context(_tmp_root: Path) -> dict[str, Any]:
    result = asyncio.run(_continue_turn_reconstructs_interrupted_request_probe("刚才那个报告还在吗？接着跑"))
    _assert(result.get("magic_continue_required") is False, "arbitrary follow-up incorrectly required magic continue text")
    _assert(result.get("continue_used_previous_request") is True, "arbitrary follow-up did not use interrupted request")
    _assert(
        result.get("continue_response") == "continued-weather-report-from-interrupted-request",
        "arbitrary follow-up response was not based on interrupted request",
    )
    _assert(
        result.get("interrupted_request_visible_to_followup") is True,
        "interrupted request was not visible to arbitrary follow-up",
    )
    _assert(
        result.get("interruption_marker_visible_to_followup") is True,
        "interruption marker was not visible to arbitrary follow-up",
    )
    return result


def case_production_feedback_interruption_quote_resume(_tmp_root: Path) -> dict[str, Any]:
    quote = "先报个问题，我遇到两次了，就是会中断，执行一半突然就没了。我得说点啥，才能让他继续。"
    phrases = ["会中断", "执行一半突然就没了", "我得说点啥", "才能让他继续"]
    result = asyncio.run(_continue_turn_reconstructs_interrupted_request_probe("我得说点啥，才能让他继续"))
    phrase_coverage = {phrase: phrase in quote for phrase in phrases}
    exact_feedback_covered = (
        all(phrase_coverage.values())
        and result.get("magic_continue_required") is False
        and result.get("continue_used_previous_request") is True
        and result.get("interrupted_request_visible_to_followup") is True
        and result.get("interruption_marker_visible_to_followup") is True
    )
    result.update({
        "production_feedback_quote": quote,
        "feedback_phrases": phrases,
        "feedback_phrase_coverage": phrase_coverage,
        "first_problem_exact_feedback_covered": exact_feedback_covered,
    })
    _assert(exact_feedback_covered, "production feedback first-problem quote did not map to executable resume UAT")
    _assert(result.get("followup_text") == "我得说点啥，才能让他继续", "production feedback follow-up text mismatch")
    return result


async def _midrun_exception_recovery_probe() -> dict[str, Any]:
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy import runtime as runtime_mod
    from hermes_multitenancy.router import handle_async
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    previous_default_run_agent = runtime_mod._default_run_agent
    previous_pool = router_mod._pool
    previous_logger_disabled = router_mod.logger.disabled
    clear_spike_routes()
    router_mod.override_pool(None)
    router_mod._user_inflight_tasks.clear()
    router_mod._user_inflight_history_keys.clear()
    router_mod._suppress_interruption_marker_tasks.clear()
    router_mod._session_history.clear()
    router_mod._session_loaded.clear()
    router_mod.override_session_store(":memory:")

    user_id = "ou_midrun_failure_uat"
    chat_id = "chat-midrun-failure"
    followup_text = "刚刚那个执行到一半没了，接着来"
    state: dict[str, Any] = {}

    async def runner(event, home):
        text = getattr(event, "text", "")
        history_key = (home.name, user_id)
        if "半路失败" in text:
            raise RuntimeError("simulated mid-run worker failure")
        if text == followup_text:
            history_before = router_mod._session_history.get(history_key, [])
            contents = [str(item.get("content") or "") for item in history_before]
            failed_request_visible = any("天气 skill 半路失败报告" in content for content in contents)
            failure_marker_visible = any("执行失败或中断" in content for content in contents)
            used_failed_request = failed_request_visible and failure_marker_visible
            response = "resumed-after-midrun-failure" if used_failed_request else "missing-midrun-failure-context"
            state.update({
                "followup_text": followup_text,
                "failed_request_visible_to_followup": failed_request_visible,
                "failure_marker_visible_to_followup": failure_marker_visible,
                "followup_used_failed_request": used_failed_request,
                "followup_response": response,
                "followup_history_before_response": contents,
            })
            return response
        return "unexpected"

    runtime_mod._default_run_agent = runner
    router_mod.logger.disabled = True
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-midrun-failure-") as tmp:
            home = Path(tmp) / "midrun_failure_profile"
            home.mkdir()
            add_spike_route(user_id, home)

            class Adapter:
                async def send_typing(self, _chat): pass
                async def send(self, _chat, _msg, *, reply_to=None, metadata=None): pass

            gateway = SimpleNamespace(adapters={"feishu": Adapter()})
            event1 = SimpleNamespace(
                text="帮我生成天气 skill 半路失败报告，执行到一半模拟异常",
                message_id="om_midrun_failure_first",
                source=SimpleNamespace(
                    chat_id=chat_id,
                    user_id=user_id,
                    user_id_alt=None,
                    chat_type="dm",
                    message_id="om_midrun_failure_first",
                ),
            )
            event2 = SimpleNamespace(
                text=followup_text,
                message_id="om_midrun_failure_followup",
                source=SimpleNamespace(
                    chat_id=chat_id,
                    user_id=user_id,
                    user_id_alt=None,
                    chat_type="dm",
                    message_id="om_midrun_failure_followup",
                ),
            )
            await handle_async(event=event1, gateway=gateway)
            await handle_async(event=event2, gateway=gateway)
            state["final_history"] = router_mod._session_history[(home.name, user_id)]
    finally:
        runtime_mod._default_run_agent = previous_default_run_agent
        router_mod.logger.disabled = previous_logger_disabled
        router_mod.override_pool(previous_pool)
        clear_spike_routes()
        router_mod.override_session_store(None)

    return state


def case_midrun_exception_preserves_recovery_context(_tmp_root: Path) -> dict[str, Any]:
    result = asyncio.run(_midrun_exception_recovery_probe())
    _assert(result.get("failed_request_visible_to_followup") is True, "failed request missing from follow-up context")
    _assert(result.get("failure_marker_visible_to_followup") is True, "failure marker missing from follow-up context")
    _assert(result.get("followup_used_failed_request") is True, "follow-up did not use failed request context")
    _assert(result.get("followup_response") == "resumed-after-midrun-failure", "follow-up did not resume failed request")
    return result


async def _slow_model_idle_feedback_probe(tmp_root: Path) -> dict[str, Any]:
    from hermes_multitenancy import agent_real
    from hermes_multitenancy import router as router_mod

    previous_heartbeat = router_mod._STREAM_CARD_IDLE_HEARTBEAT_SECONDS
    previous_stream = agent_real.stream_run_agent
    stream_entered = asyncio.Event()
    release_stream = asyncio.Event()

    class Adapter:
        def __init__(self) -> None:
            self.started: list[dict[str, Any]] = []
            self.status_updates: list[dict[str, Any]] = []
            self.reasoning_updates: list[dict[str, Any]] = []
            self.updates: list[dict[str, Any]] = []

        def supports_streaming_card(self) -> bool:
            return True

        async def start_streaming_card(self, *, chat_id, reply_to=None, metadata=None):
            self.started.append({"chat_id": chat_id, "reply_to": reply_to, "metadata": metadata})
            return SimpleNamespace(success=True, message_id="card-slow")

        async def update_streaming_card(self, *, chat_id, message_id, content, finalize=False):
            self.updates.append({
                "chat_id": chat_id,
                "message_id": message_id,
                "content": content,
                "finalize": finalize,
            })
            return SimpleNamespace(success=True, message_id=message_id)

        async def update_streaming_card_status(self, *, chat_id, message_id, content):
            self.status_updates.append({"chat_id": chat_id, "message_id": message_id, "content": content})
            return SimpleNamespace(success=True, message_id=message_id)

        async def update_streaming_card_reasoning(self, *, chat_id, message_id, content):
            self.reasoning_updates.append({"chat_id": chat_id, "message_id": message_id, "content": content})
            return SimpleNamespace(success=True, message_id=message_id)

        async def update_streaming_card_tool_started(self, **kwargs):
            return SimpleNamespace(success=True, message_id=kwargs.get("message_id"))

        async def update_streaming_card_tool_completed(self, **kwargs):
            return SimpleNamespace(success=True, message_id=kwargs.get("message_id"))

        async def abort_streaming_card(self, **kwargs):
            return SimpleNamespace(success=True, message_id=kwargs.get("message_id"))

        async def send(self, chat_id, content, *, reply_to=None, metadata=None):
            return SimpleNamespace(success=True, message_id="text-slow")

        async def edit_message(self, chat_id, message_id, content, *, finalize=False):
            self.updates.append({
                "chat_id": chat_id,
                "message_id": message_id,
                "content": content,
                "finalize": finalize,
            })
            return SimpleNamespace(success=True, message_id=message_id)

    async def fake_stream(event, home, *, messages=None):
        stream_entered.set()
        await release_stream.wait()
        yield ("content", "slow-model-final")

    adapter = Adapter()
    try:
        router_mod._STREAM_CARD_IDLE_HEARTBEAT_SECONDS = 0.01
        agent_real.stream_run_agent = fake_stream
        task = asyncio.create_task(
            router_mod._stream_into_feishu(
                adapter,
                "oc_slow_idle",
                "slow_profile",
                tmp_root,
                SimpleNamespace(text="SLOW_MODEL_IDLE_UAT"),
                messages=[{"role": "user", "content": "SLOW_MODEL_IDLE_UAT"}],
            )
        )
        await asyncio.wait_for(stream_entered.wait(), timeout=1)
        deadline = time.monotonic() + 1.0
        while len(adapter.status_updates) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        release_stream.set()
        result = await asyncio.wait_for(task, timeout=1)
    finally:
        router_mod._STREAM_CARD_IDLE_HEARTBEAT_SECONDS = previous_heartbeat
        agent_real.stream_run_agent = previous_stream

    return {
        "initial_status": adapter.status_updates[0]["content"] if adapter.status_updates else "",
        "status_update_count": len(adapter.status_updates),
        "status_updates": [item["content"] for item in adapter.status_updates[:3]],
        "reasoning_update_count": len(adapter.reasoning_updates),
        "final_result": result,
        "finalized": bool(adapter.updates and adapter.updates[-1].get("finalize") is True),
    }


def case_slow_model_idle_feedback(tmp_root: Path) -> dict[str, Any]:
    result = asyncio.run(_slow_model_idle_feedback_probe(tmp_root))
    _assert(result["status_update_count"] >= 2, "slow model wait did not emit visible heartbeat status")
    _assert(result["initial_status"] == "Hermes 正在准备响应...", "initial progress status missing")
    _assert(result["status_updates"][1] != result["initial_status"], "heartbeat status did not advance")
    _assert(result["reasoning_update_count"] == 0, "heartbeat status leaked into reasoning")
    _assert(result["final_result"] == "slow-model-final", "slow stream final content missing")
    _assert(result["finalized"], "slow stream did not finalize card")
    return result


def case_real_home_secret_free(real_home: Path) -> dict[str, Any]:
    from hermes_multitenancy.lark_cli_canary import lark_cli_canary_preflight

    db = real_home / "multitenancy.db"
    if not db.exists():
        return {"checked": False, "reason": "real multitenancy.db missing"}
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT kind, profile_name, open_id, chat_id, owner_open_id, upstream_profile, provenance "
        "FROM multitenancy_routing WHERE active = 1 ORDER BY kind, updated_at DESC"
    ).fetchall()
    conn.close()
    groups = [dict(row) for row in rows if row["kind"] == "group"]
    users = [dict(row) for row in rows if row["kind"] == "user"]
    group_uat_files = []
    for group in groups:
        uat_dir = real_home / "profiles" / group["profile_name"] / "feishu_uat"
        group_uat_files.extend(str(path) for path in uat_dir.glob("*.json"))

    preflights = []
    for user in users[:3]:
        if not user.get("open_id"):
            continue
        preflights.append(
            lark_cli_canary_preflight(
                shared_home=real_home,
                profile_name=user["profile_name"],
                open_id=user["open_id"],
            )
        )
    return {
        "checked": True,
        "user_routes": len(users),
        "group_routes": len(groups),
        "groups_have_upstream_profile": all(bool(group.get("upstream_profile")) for group in groups),
        "group_uat_file_count": len(group_uat_files),
        "preflights": [
            {
                "profile_name": item.get("profile_name"),
                "open_id": item.get("open_id"),
                "ready": item.get("ready"),
                "missing": item.get("missing"),
                "secret_free": item.get("secret_free"),
            }
            for item in preflights
        ],
    }

def _real_group_route_snapshot(real_home: Path) -> dict[str, str]:
    db = real_home / "multitenancy.db"
    _assert(db.exists(), "real multitenancy.db missing")
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        group = conn.execute(
            """
            SELECT profile_name, chat_id, owner_open_id, upstream_profile, display_label
            FROM multitenancy_routing
            WHERE active = 1 AND kind = 'group'
              AND chat_id IS NOT NULL
              AND owner_open_id IS NOT NULL
              AND upstream_profile IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()
        _assert(group is not None, "no active group route with upstream_profile")
        owner = conn.execute(
            """
            SELECT user_id, profile_name, open_id
            FROM multitenancy_routing
            WHERE active = 1 AND kind = 'user' AND open_id = ?
            LIMIT 1
            """,
            (group["owner_open_id"],),
        ).fetchone()
    finally:
        conn.close()
    _assert(owner is not None, "group owner user route missing")
    return {
        "group_profile": group["profile_name"],
        "chat_id": group["chat_id"],
        "owner_open_id": group["owner_open_id"],
        "upstream_profile": group["upstream_profile"],
        "display_label": group["display_label"] or group["chat_id"],
        "owner_user_id": owner["user_id"],
        "owner_profile": owner["profile_name"],
    }


async def _real_group_replacement_replay(real_home: Path) -> dict[str, Any]:
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.router import handle_async
    from hermes_multitenancy.routing import RoutingTable

    route = _real_group_route_snapshot(real_home)
    second_messages: dict[str, Any] = {}
    first_started = asyncio.Event()
    first_cancelled = asyncio.Event()
    completions: list[str] = []

    async def fake_stream(adapter, chat_id, profile_name, profile_home, event, *, messages):
        text = getattr(event, "text", "")
        if "GROUP_RACE_REPLAY_FIRST" in text:
            first_started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                first_cancelled.set()
                raise
        second_messages["messages"] = list(messages)
        second_messages["profile_name"] = profile_name
        second_messages["profile_home"] = str(profile_home)
        second_messages["chat_id"] = chat_id
        return "GROUP_RACE_REPLAY_SECOND_OK"

    original_stream = router_mod._stream_into_feishu
    router_mod._stream_into_feishu = fake_stream
    router_mod._session_history.clear()
    router_mod._session_loaded.clear()
    router_mod._user_inflight_tasks.clear()
    router_mod._user_inflight_history_keys.clear()
    router_mod._suppress_interruption_marker_tasks.clear()
    router_mod.override_session_store(":memory:")

    try:
        with tempfile.TemporaryDirectory(prefix="hermes-group-replay-") as tmp:
            tmp_db = Path(tmp) / "multitenancy.db"
            table = RoutingTable(tmp_db)
            try:
                table.upsert(
                    user_id=route["owner_user_id"],
                    profile_name=route["owner_profile"],
                    open_id=route["owner_open_id"],
                    provenance="sync",
                )
                table.upsert_group(
                    chat_id=route["chat_id"],
                    profile_name=route["group_profile"],
                    owner_open_id=route["owner_open_id"],
                    display_label=route["display_label"],
                    upstream_profile=route["upstream_profile"],
                )
            finally:
                table.close()

            router_mod.override_routing_table(tmp_db)

            class Adapter:
                async def send(self, _chat, _msg, *, reply_to=None, metadata=None): pass
                async def edit_message(self, *args, **kwargs): pass
                async def on_processing_start(self, _event): pass
                async def on_processing_complete(self, _event, outcome):
                    completions.append(str(outcome))

            def event(text: str, message_id: str):
                return SimpleNamespace(
                    text=text,
                    message_id=message_id,
                    source=SimpleNamespace(
                        chat_id=route["chat_id"],
                        user_id=route["owner_open_id"],
                        user_id_alt=None,
                        user_name="group-owner",
                        chat_type="group",
                        platform=SimpleNamespace(value="feishu"),
                        message_id=message_id,
                        thread_id=None,
                    ),
                )

            gateway = SimpleNamespace(adapters={"feishu": Adapter()})
            first = asyncio.create_task(
                handle_async(
                    event=event("GROUP_RACE_REPLAY_FIRST create a doc slowly", "uat_group_replay_first"),
                    gateway=gateway,
                )
            )
            await asyncio.wait_for(first_started.wait(), timeout=1)
            second = asyncio.create_task(
                handle_async(
                    event=event("GROUP_RACE_REPLAY_SECOND continue same group", "uat_group_replay_second"),
                    gateway=gateway,
                )
            )
            await asyncio.wait_for(first_cancelled.wait(), timeout=1)
            try:
                await first
            except asyncio.CancelledError:
                pass
            await second
    finally:
        router_mod._stream_into_feishu = original_stream
        router_mod.override_routing_table(None)
        router_mod.override_session_store(None)

    messages = second_messages.get("messages") or []
    return {
        "group_profile": route["group_profile"],
        "chat_id": route["chat_id"],
        "upstream_profile": route["upstream_profile"],
        "message_roles": [item.get("role") for item in messages],
        "message_contents": [item.get("content") for item in messages],
        "second_profile_name": second_messages.get("profile_name"),
        "second_chat_id": second_messages.get("chat_id"),
        "completion_count": len(completions),
    }


def case_real_group_replacement_replay(real_home: Path) -> dict[str, Any]:
    result = asyncio.run(_real_group_replacement_replay(real_home))
    contents = result["message_contents"]
    _assert(result["second_profile_name"] == result["group_profile"], "replacement turn did not use group profile")
    _assert(result["second_chat_id"] == result["chat_id"], "replacement turn did not preserve group chat_id")
    _assert(contents[0].startswith("GROUP_RACE_REPLAY_FIRST"), "interrupted group request missing from replacement messages")
    _assert("中断或取消" in contents[1], "group replacement interruption marker missing")
    _assert(contents[2].startswith("GROUP_RACE_REPLAY_SECOND"), "replacement group request missing")
    return result


def case_vision_failure_surfaces_recovery_context() -> dict[str, Any]:
    """Production-feedback guard: screenshots must not disappear on vision auth/model failure."""
    from hermes_multitenancy.router import _local_enrich_with_vision_only

    fake_mod = types.ModuleType("tools.vision_tools")

    async def fake_tool(*, image_url, user_prompt):
        return json.dumps(
            {
                "success": False,
                "error": "Error code: 429 - subscription does not include GLM-5V-Turbo vision",
            }
        )

    fake_mod.vision_analyze_tool = fake_tool
    original_tools = sys.modules.get("tools")
    original_vision = sys.modules.get("tools.vision_tools")
    sys.modules["tools"] = original_tools or types.ModuleType("tools")
    sys.modules["tools.vision_tools"] = fake_mod
    try:
        event = SimpleNamespace(
            text="我该如何在这个web页面中给你发送截图呢?",
            media_urls=["/Users/kite/.hermes/profiles/coder/images/clip_20260506_132649_1.png"],
            media_types=["image/png"],
        )
        result = asyncio.run(_local_enrich_with_vision_only(event))
    finally:
        if original_vision is None:
            sys.modules.pop("tools.vision_tools", None)
        else:
            sys.modules["tools.vision_tools"] = original_vision
        if original_tools is None:
            sys.modules.pop("tools", None)
        else:
            sys.modules["tools"] = original_tools

    _assert(result is not None, "vision failure returned no context")
    _assert("Image analysis unavailable" in result, "vision failure note missing")
    _assert("vision-capable model or permission is required" in result, "recovery guidance missing")
    _assert("clip_20260506_132649_1.png" in result, "attached image path missing")
    _assert(result.endswith("我该如何在这个web页面中给你发送截图呢?"), "user text not preserved")
    return {
        "context_present": True,
        "recovery_guidance": True,
        "path_preserved": "clip_20260506_132649_1.png",
    }


async def _context_continuity_private_and_group_probe(tmp_root: Path) -> dict[str, Any]:
    """Candidate production-feedback guard: no-tool follow-up must keep prior turns."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.router import handle_async
    from hermes_multitenancy.routing import RoutingTable

    private_profile = tmp_root / "profiles" / "ctx_private"
    group_profile = tmp_root / "profiles" / "feishu_group_ctx"
    private_profile.mkdir(parents=True, exist_ok=True)
    group_profile.mkdir(parents=True, exist_ok=True)
    db = tmp_root / "multitenancy.db"
    table = RoutingTable(db)
    try:
        table.upsert(
            user_id="ctx-user",
            profile_name="ctx_private",
            open_id="ou_ctx_private",
            provenance="sync",
        )
        table.upsert_group(
            chat_id="oc_ctx_group",
            profile_name="feishu_group_ctx",
            owner_open_id="ou_ctx_private",
            display_label="Context UAT Group",
            upstream_profile="ctx_private",
        )
    finally:
        table.close()

    seen: dict[str, list[dict[str, Any]]] = {}

    async def fake_stream(adapter, chat_id, profile_name, profile_home, event, *, messages):
        text = getattr(event, "text", "")
        key = f"{profile_name}:{text}"
        seen[key] = [dict(item) for item in messages]
        if "CTX_PRIVATE_A" in text:
            return "私聊临时代号是苜蓿"
        if "CTX_GROUP_A" in text:
            return "群聊临时代号是苜蓿"
        return "召回成功"

    class Adapter:
        async def send(self, _chat, _msg, *, reply_to=None, metadata=None): pass
        async def edit_message(self, *args, **kwargs): pass
        async def on_processing_start(self, _event): pass
        async def on_processing_complete(self, _event, outcome): pass

    def event(text: str, *, chat_id: str, user_id: str, chat_type: str, message_id: str):
        return SimpleNamespace(
            text=text,
            message_id=message_id,
            source=SimpleNamespace(
                chat_id=chat_id,
                user_id=user_id,
                user_id_alt=None,
                user_name="ctx-user",
                chat_type=chat_type,
                platform=SimpleNamespace(value="feishu"),
                message_id=message_id,
                thread_id=None,
            ),
        )

    original_stream = router_mod._stream_into_feishu
    router_mod._stream_into_feishu = fake_stream
    router_mod._session_history.clear()
    router_mod._session_loaded.clear()
    router_mod._user_inflight_tasks.clear()
    router_mod._user_inflight_history_keys.clear()
    router_mod.override_routing_table(db)
    router_mod.override_session_store(":memory:")
    try:
        gateway = SimpleNamespace(adapters={"feishu": Adapter()})
        await handle_async(
            event=event(
                "CTX_PRIVATE_A 请记住 no-tool 临时代号",
                chat_id="oc_private",
                user_id="ou_ctx_private",
                chat_type="p2p",
                message_id="ctx_private_a",
            ),
            gateway=gateway,
        )
        await handle_async(
            event=event(
                "CTX_PRIVATE_B 上一轮临时代号是什么",
                chat_id="oc_private",
                user_id="ou_ctx_private",
                chat_type="p2p",
                message_id="ctx_private_b",
            ),
            gateway=gateway,
        )
        await handle_async(
            event=event(
                "CTX_GROUP_A 请记住 no-tool 群聊临时代号",
                chat_id="oc_ctx_group",
                user_id="ou_ctx_private",
                chat_type="group",
                message_id="ctx_group_a",
            ),
            gateway=gateway,
        )
        await handle_async(
            event=event(
                "CTX_GROUP_B 上一轮群聊临时代号是什么",
                chat_id="oc_ctx_group",
                user_id="ou_ctx_private",
                chat_type="group",
                message_id="ctx_group_b",
            ),
            gateway=gateway,
        )
    finally:
        router_mod._stream_into_feishu = original_stream
        router_mod.override_routing_table(None)
        router_mod.override_session_store(None)

    private_second = seen.get("ctx_private:CTX_PRIVATE_B 上一轮临时代号是什么") or []
    group_second = seen.get("feishu_group_ctx:CTX_GROUP_B 上一轮群聊临时代号是什么") or []
    return {
        "private_second_messages": private_second,
        "group_second_messages": group_second,
        "private_second_contents": [item.get("content") for item in private_second],
        "group_second_contents": [item.get("content") for item in group_second],
    }


def case_context_continuity_private_and_group(tmp_root: Path) -> dict[str, Any]:
    result = asyncio.run(_context_continuity_private_and_group_probe(tmp_root))
    private_contents = result["private_second_contents"]
    group_contents = result["group_second_contents"]
    _assert(any("CTX_PRIVATE_A" in str(item) for item in private_contents), "private follow-up lost prior user turn")
    _assert(any("私聊临时代号是苜蓿" in str(item) for item in private_contents), "private follow-up lost prior assistant turn")
    _assert(private_contents[-1].startswith("CTX_PRIVATE_B"), "private follow-up current user turn missing")
    _assert(any("CTX_GROUP_A" in str(item) for item in group_contents), "group follow-up lost prior user turn")
    _assert(any("群聊临时代号是苜蓿" in str(item) for item in group_contents), "group follow-up lost prior assistant turn")
    _assert(group_contents[-1].startswith("CTX_GROUP_B"), "group follow-up current user turn missing")
    return {
        "private_message_count": len(private_contents),
        "group_message_count": len(group_contents),
        "private_recalled": True,
        "group_recalled": True,
    }


async def _inflight_scoped_private_group_probe(tmp_root: Path) -> dict[str, Any]:
    """Production-feedback guard: group work must not cancel the owner's private work."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.router import handle_async
    from hermes_multitenancy.routing import RoutingTable

    root = tmp_root / "home"
    profiles = root / ".hermes" / "profiles"
    (profiles / "alice").mkdir(parents=True, exist_ok=True)
    (profiles / "feishu_group_sales").mkdir(parents=True, exist_ok=True)
    db_path = tmp_root / "multitenancy.db"
    table = RoutingTable(db_path)
    try:
        table.upsert(user_id="alice", profile_name="alice", open_id="ou_same_owner", provenance="sync")
        table.upsert_group(
            chat_id="oc_sales",
            profile_name="feishu_group_sales",
            owner_open_id="ou_same_owner",
            display_label="Sales",
            upstream_profile="alice",
        )
    finally:
        table.close()

    original_home = os.environ.get("HOME")
    os.environ["HOME"] = str(root)
    router_mod._user_inflight_tasks.clear()
    router_mod._user_inflight_history_keys.clear()
    router_mod._session_history.clear()
    router_mod._session_loaded.clear()
    router_mod.override_routing_table(db_path)
    router_mod.override_session_store(":memory:")

    dm_started = asyncio.Event()
    group_started = asyncio.Event()
    dm_cancelled = asyncio.Event()
    release_dm = asyncio.Event()
    release_group = asyncio.Event()
    sends: list[tuple[str, str]] = []

    class Pool:
        async def dispatch(self, profile_name, home, event):
            if profile_name == "alice":
                dm_started.set()
                try:
                    await release_dm.wait()
                except asyncio.CancelledError:
                    dm_cancelled.set()
                    raise
                return "dm-ok"
            if profile_name == "feishu_group_sales":
                group_started.set()
                await release_group.wait()
                return "group-ok"
            raise AssertionError(f"unexpected profile {profile_name}")

    class Adapter:
        async def send_typing(self, _chat): pass
        async def send(self, chat, msg, *, reply_to=None, metadata=None):
            sends.append((chat, msg))

    def event(text: str, *, chat_id: str, chat_type: str, message_id: str):
        return SimpleNamespace(
            text=text,
            message_id=message_id,
            source=SimpleNamespace(
                chat_id=chat_id,
                user_id="ou_same_owner",
                user_id_alt=None,
                user_name="same-owner",
                chat_type=chat_type,
                platform=SimpleNamespace(value="feishu"),
                message_id=message_id,
                thread_id=None,
            ),
        )

    router_mod.override_pool(Pool())
    try:
        gateway = SimpleNamespace(adapters={"feishu": Adapter()})
        dm_task = asyncio.create_task(
            handle_async(
                event=event("PRIVATE_LONG_RUNNING_UAT", chat_id="dm-chat", chat_type="p2p", message_id="private_long"),
                gateway=gateway,
            )
        )
        await asyncio.wait_for(dm_started.wait(), timeout=1)
        group_task = asyncio.create_task(
            handle_async(
                event=event("GROUP_LONG_RUNNING_UAT", chat_id="oc_sales", chat_type="group", message_id="group_long"),
                gateway=gateway,
            )
        )
        await asyncio.wait_for(group_started.wait(), timeout=1)
        await asyncio.sleep(0.05)
        still_running_before_release = not dm_task.done()
        dm_cancelled_before_release = dm_cancelled.is_set()
        release_group.set()
        release_dm.set()
        await group_task
        await dm_task
    finally:
        router_mod.override_pool(None)
        router_mod.override_routing_table(None)
        router_mod.override_session_store(None)
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home

    return {
        "dm_cancelled_before_release": dm_cancelled_before_release,
        "dm_still_running_before_release": still_running_before_release,
        "sent": [{"chat_id": chat, "message": msg} for chat, msg in sends],
    }


def case_inflight_replacement_scoped_private_group(tmp_root: Path) -> dict[str, Any]:
    result = asyncio.run(_inflight_scoped_private_group_probe(tmp_root))
    sent = {(item["chat_id"], item["message"]) for item in result["sent"]}
    _assert(not result["dm_cancelled_before_release"], "group turn cancelled the owner's private turn")
    _assert(result["dm_still_running_before_release"], "private turn finished before explicit release")
    _assert(("oc_sales", "group-ok") in sent, "group response missing")
    _assert(("dm-chat", "dm-ok") in sent, "private response missing")
    return result


def case_session_guard_replacement_no_duplicate_dispatch() -> dict[str, Any]:
    """A cancelled prior dispatch must not remove the newer Feishu flush guard."""
    from types import ModuleType

    from hermes_multitenancy.router import _register_session_guard_for_dispatch

    class FakeTask:
        def __init__(self, name: str):
            self.name = name
            self.callbacks: list[Any] = []

        def add_done_callback(self, callback):
            self.callbacks.append(callback)

        def complete(self) -> None:
            for callback in list(self.callbacks):
                callback(self)

    session_mod = ModuleType("gateway.session")
    session_mod.build_session_key = lambda source, **_kwargs: f"{source.chat_id}:{source.user_id}"  # type: ignore[attr-defined]
    original_session = sys.modules.get("gateway.session")
    sys.modules["gateway.session"] = session_mod
    try:
        adapter = SimpleNamespace(
            _active_sessions={},
            config=SimpleNamespace(extra={}),
        )
        gateway = SimpleNamespace(adapters={"feishu": adapter})
        event1 = SimpleNamespace(
            text="guard first",
            source=SimpleNamespace(chat_id="chat-guard", user_id="ou_guard"),
        )
        event2 = SimpleNamespace(
            text="guard second",
            source=SimpleNamespace(chat_id="chat-guard", user_id="ou_guard"),
        )
        first = FakeTask("first")
        second = FakeTask("second")

        _register_session_guard_for_dispatch(event1, gateway, first)
        _assert(len(adapter._active_sessions) == 1, "first dispatch did not register a guard")
        session_key = next(iter(adapter._active_sessions))
        first_guard = adapter._active_sessions[session_key]

        _register_session_guard_for_dispatch(event2, gateway, second)
        _assert(len(adapter._active_sessions) == 1, "replacement dispatch created duplicate guard entries")
        second_guard = adapter._active_sessions[session_key]
        _assert(second_guard is not first_guard, "replacement dispatch did not take ownership of the guard")

        first.complete()
        _assert(
            adapter._active_sessions.get(session_key) is second_guard,
            "old dispatch cleanup removed the replacement guard",
        )
        second.complete()
        _assert(session_key not in adapter._active_sessions, "replacement guard did not clean up after completion")
        return {
            "session_key": session_key,
            "guard_count_after_replace": 1,
            "old_cleanup_preserved_replacement": True,
            "replacement_cleanup_removed_guard": True,
        }
    finally:
        if original_session is None:
            sys.modules.pop("gateway.session", None)
        else:
            sys.modules["gateway.session"] = original_session


async def _persistent_event_dedupe_probe(tmp_root: Path) -> dict[str, Any]:
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    profile_home = tmp_root / "profiles" / "dedupe-user"
    profile_home.mkdir(parents=True)
    add_spike_route("ou_persistent_dedupe", profile_home)

    db_path = tmp_root / "sessions.db"
    router_mod.override_session_store(db_path)
    router_mod._session_history.clear()
    router_mod._session_loaded.clear()
    router_mod._user_inflight_tasks.clear()
    router_mod._user_inflight_history_keys.clear()
    dispatches: list[dict[str, str]] = []
    completions: list[dict[str, str]] = []
    original_stream = router_mod._stream_into_feishu

    async def fake_stream(_adapter, _chat_id, profile_name, _profile_home, agent_event, *, messages=None):
        dispatches.append({
            "profile_name": str(profile_name),
            "text": str(getattr(agent_event, "text", "") or ""),
        })
        return "dedupe-ok"

    class Adapter:
        async def edit_message(self, *args, **kwargs):  # pragma: no cover - interface marker only
            return None

        async def on_processing_start(self, event):
            return None

        async def on_processing_complete(self, event, outcome):
            completions.append({
                "message_id": str(getattr(event, "message_id", "") or ""),
                "text": str(getattr(event, "text", "") or ""),
                "outcome": str(outcome),
                "method": "on_processing_complete",
            })

        async def complete_deferred_processing(self, event, outcome):
            completions.append({
                "message_id": str(getattr(event, "message_id", "") or ""),
                "text": str(getattr(event, "text", "") or ""),
                "outcome": str(outcome),
                "method": "complete_deferred_processing",
            })

    def event(text: str, *, message_id: str | None):
        return SimpleNamespace(
            text=text,
            message_id=message_id,
            raw_event={"event": {"message": {"message_id": message_id}}} if message_id else {},
            source=SimpleNamespace(
                chat_id="oc_dedupe_private",
                user_id="ou_persistent_dedupe",
                user_id_alt=None,
                user_name="dedupe-user",
                chat_type="p2p",
                platform=SimpleNamespace(value="feishu"),
                message_id=message_id,
                thread_id=None,
            ),
        )

    same_text = "DEDUP_UAT 查一下昨天 IT&Sec 消息并总结，确认不要因为 Feishu 重投递重复启动。"
    long_text = (
        "检索下昨天 it&sec 群聊，专项诊断智慧芽、公安相关字样的事件全貌，"
        "更新复盘文档并给出后续可靠方案。"
    )
    gateway = SimpleNamespace(adapters={"feishu": Adapter()})
    try:
        router_mod._stream_into_feishu = fake_stream
        await router_mod.handle_async(event=event(same_text, message_id="om_persistent_same"), gateway=gateway)
        await router_mod.handle_async(event=event(same_text, message_id="om_persistent_same"), gateway=gateway)
        same_message_id_dispatch_count = sum(1 for row in dispatches if row["text"] == same_text)
        same_message_id_sent_count = sum(1 for row in completions if row["message_id"] == "om_persistent_same")

        await router_mod.handle_async(event=event(long_text, message_id=None), gateway=gateway)
        await router_mod.handle_async(event=event(long_text, message_id=None), gateway=gateway)
        long_content_dispatch_count = sum(1 for row in dispatches if row["text"] == long_text)
        long_content_completion_count = sum(1 for row in completions if row["text"] == long_text)
    finally:
        router_mod._stream_into_feishu = original_stream
        router_mod.override_session_store(None)
        router_mod._session_history.clear()
        router_mod._session_loaded.clear()
        router_mod._user_inflight_tasks.clear()
        router_mod._user_inflight_history_keys.clear()
        clear_spike_routes()

    with sqlite3.connect(db_path) as conn:
        processed_event_rows = int(conn.execute("SELECT COUNT(*) FROM multitenancy_processed_events").fetchone()[0])

    return {
        "same_message_id_dispatch_count": same_message_id_dispatch_count,
        "same_message_id_completion_count": same_message_id_sent_count,
        "same_message_id_duplicate_suppressed": same_message_id_dispatch_count == 1,
        "long_content_dispatch_count": long_content_dispatch_count,
        "long_content_completion_count": long_content_completion_count,
        "long_content_duplicate_suppressed": long_content_dispatch_count == 1,
        "processed_event_rows": processed_event_rows,
        "duplicate_processing_completed": same_message_id_sent_count >= 2 and long_content_completion_count >= 2,
    }


def case_persistent_event_dedupe_skips_redelivery(tmp_root: Path) -> dict[str, Any]:
    result = asyncio.run(_persistent_event_dedupe_probe(tmp_root))
    _assert(result["same_message_id_dispatch_count"] == 1, "same Feishu message_id dispatched more than once")
    _assert(result["same_message_id_duplicate_suppressed"] is True, "same message_id duplicate was not suppressed")
    _assert(result["long_content_dispatch_count"] == 1, "long content fallback dispatched more than once")
    _assert(result["long_content_duplicate_suppressed"] is True, "long content duplicate was not suppressed")
    _assert(result["processed_event_rows"] >= 2, "persistent processed-event table did not record both dedupe keys")
    _assert(result["duplicate_processing_completed"] is True, "duplicate Feishu lifecycle was not completed")
    return result


def case_personal_skillhub_install_secret_guard(tmp_root: Path) -> dict[str, Any]:
    from hermes_multitenancy.skill_registry import install_shared_skill_for_profile, list_installed_skills

    shared_home = tmp_root / ".hermes"
    source = shared_home / "skills" / "hub" / "secret-tool"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (source / "SKILL.md").write_text("# Secret Tool\n", encoding="utf-8")
    (source / ".env").write_text("TOKEN=do-not-link\n", encoding="utf-8")
    (source / "auth.json").write_text('{"access_token":"do-not-copy"}', encoding="utf-8")
    (nested / "api.token").write_text("do-not-copy\n", encoding="utf-8")
    (nested / "README.md").write_text("safe docs\n", encoding="utf-8")

    profile = shared_home / "profiles" / "alice"
    stale_target = profile / "skills" / "hub" / "secret-tool"
    stale_target.mkdir(parents=True)
    (stale_target / "old.token").write_text("old leaked token\n", encoding="utf-8")

    result = install_shared_skill_for_profile(
        shared_home=shared_home,
        profile_home=profile,
        skill_path="hub/secret-tool",
    )
    target = profile / "skills" / "hub" / "secret-tool"
    installed = list_installed_skills(profile_home=profile)
    secret_paths = [
        target / ".env",
        target / "auth.json",
        target / "nested" / "api.token",
        target / "old.token",
    ]
    _assert(result.get("install_mode") == "copy", "secret-bearing personal install did not fall back to copy")
    _assert(result.get("requested_install_mode") == "symlink", "requested install mode was not recorded")
    _assert(result.get("secret_guard") == "copy_filtered", "secret guard mode was not recorded")
    _assert(target.is_dir() and not target.is_symlink(), "target should be a filtered directory copy")
    _assert((target / "SKILL.md").exists(), "safe skill file was not copied")
    _assert((target / "nested" / "README.md").exists(), "safe nested file was not copied")
    _assert(not any(path.exists() for path in secret_paths), "secret-like file leaked into profile install")
    _assert(installed["hub/secret-tool"]["install_mode"] == "copy", "personal manifest did not record copy mode")
    _assert(
        installed["hub/secret-tool"]["secret_guard"] == "copy_filtered",
        "personal manifest did not record secret guard",
    )
    return {
        "install_mode": result.get("install_mode"),
        "requested_install_mode": result.get("requested_install_mode"),
        "secret_guard": result.get("secret_guard"),
        "target_is_symlink": target.is_symlink(),
        "safe_files_present": [
            str((target / "SKILL.md").relative_to(target)),
            str((target / "nested" / "README.md").relative_to(target)),
        ],
        "secret_files_present": [str(path.relative_to(target)) for path in secret_paths if path.exists()],
    }


def case_personal_skillhub_clean_install_symlink(tmp_root: Path) -> dict[str, Any]:
    from hermes_multitenancy.skill_registry import (
        audit_installed_skills,
        install_shared_skill_for_profile,
        list_installed_skills,
        PERSONAL_SKILL_MANIFEST,
    )

    shared_home = tmp_root / ".hermes"
    source = shared_home / "skills" / "hub" / "clean-weather"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# Clean Weather\n", encoding="utf-8")
    (source / "README.md").write_text("No secrets in this SkillHub skill.\n", encoding="utf-8")

    profile = shared_home / "profiles" / "alice"
    result = install_shared_skill_for_profile(
        shared_home=shared_home,
        profile_home=profile,
        skill_path="hub/clean-weather",
        version="2026.05.20",
    )
    target = profile / "skills" / "hub" / "clean-weather"
    personal_manifest = json.loads(
        (profile / "skills" / PERSONAL_SKILL_MANIFEST).read_text(encoding="utf-8")
    )["skills"]["hub/clean-weather"]
    listed = list_installed_skills(profile_home=profile)["hub/clean-weather"]
    audit = audit_installed_skills(shared_home=shared_home)
    audit_row = next(
        row
        for row in audit["profiles"]["alice"]["skills"]
        if row["skill_path"] == "hub/clean-weather"
    )
    _assert(result.get("install_mode") == "symlink", "clean SkillHub install should use symlink")
    _assert(target.is_symlink(), "clean SkillHub target should be symlink")
    _assert(personal_manifest["source"] == "personal", "clean SkillHub install missing personal manifest source")
    _assert(listed["source"] == "personal", "clean SkillHub install not listed as personal")
    _assert(audit_row["source"] == "personal", "clean SkillHub install not audited as personal")
    _assert(audit_row["token_files_present"] is False, "clean SkillHub install should not report token files")
    return {
        "install_mode": result.get("install_mode"),
        "target_is_symlink": target.is_symlink(),
        "personal_manifest_source": personal_manifest.get("source"),
        "personal_manifest_version": personal_manifest.get("version"),
        "listed_source": listed.get("source"),
        "audit_source": audit_row.get("source"),
        "audit_install_mode": audit_row.get("install_mode"),
        "audit_token_files_present": audit_row.get("token_files_present"),
    }


async def _webui_skillhub_owner_scoped_install_probe(tmp_root: Path) -> dict[str, Any]:
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared = tmp_root / ".hermes"
    source = shared / "skills" / "hub" / "clean-weather"
    profile = shared / "profiles" / "owner_sync_profile"
    _write_skill(source, "clean-weather")
    profile.mkdir(parents=True)
    old_shared = os.environ.get("HERMES_SHARED_HOME")
    os.environ["HERMES_SHARED_HOME"] = str(shared)
    db_path = shared / "multitenancy.db"
    seeded = RoutingTable(db_path)
    try:
        seeded.upsert(user_id="root-owner", profile_name=profile.name, open_id="ou_owner", provenance="sync")
    finally:
        seeded.close()

    router_mod.override_routing_table(db_path)
    try:
        app = create_run_broker_app(
            dispatch_agent=lambda request: f"echo:{request.content}",
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post(
                "/api/run-broker/skills/install",
                headers={"X-Hermes-Owner-Open-Id": "ou_owner"},
                json={
                    "profile_name": "spoofed_profile",
                    "skill_path": "hub/clean-weather",
                    "version": "2026.05.20",
                },
            )
            body = await response.json()
            audit_response = await client.get("/api/run-broker/skills/audit")
            audit = await audit_response.json()
        finally:
            await client.close()
    finally:
        router_mod.override_routing_table(None)
        if old_shared is None:
            os.environ.pop("HERMES_SHARED_HOME", None)
        else:
            os.environ["HERMES_SHARED_HOME"] = old_shared

    target = profile / "skills" / "hub" / "clean-weather"
    _assert(response.status == 200, f"SkillHub install endpoint failed status={response.status}")
    _assert(body["profile_name"] == profile.name, "SkillHub endpoint trusted client-spoofed profile")
    _assert(body["install"]["install_mode"] == "symlink", "SkillHub endpoint did not symlink clean skill")
    _assert(target.is_symlink(), "SkillHub endpoint target is not symlinked")
    _assert(not (shared / "profiles" / "spoofed_profile").exists(), "SkillHub endpoint created spoofed profile")
    _assert(audit_response.status == 200, f"Skill audit endpoint failed status={audit_response.status}")
    _assert(profile.name in audit["profiles"], "skill audit endpoint did not include owner profile")
    return {
        "status": response.status,
        "profile_name": body["profile_name"],
        "install_mode": body["install"]["install_mode"],
        "target_is_symlink": target.is_symlink(),
        "spoofed_profile_created": False,
        "audit_status": audit_response.status,
        "audit_profiles": sorted(audit["profiles"]),
    }


def case_webui_skillhub_owner_scoped_install_and_audit(tmp_root: Path) -> dict[str, Any]:
    return asyncio.run(_webui_skillhub_owner_scoped_install_probe(tmp_root))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skills-uat-matrix")
    parser.add_argument("--real-home", type=Path, default=Path("~/.hermes"))
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    out = args.output or Path("/tmp/hermes-skills-uat") / f"skills-uat-{int(time.time())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    real_home = args.real_home.expanduser()

    cases: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="hermes-skills-uat-") as tmp:
        tmp_root = Path(tmp)
        cases.append(_run_case("offline_keep_four_skill_policy_model", lambda: case_keep_four_skill_policy_model(tmp_root / "case0")))
        cases.append(_run_case("offline_distribution_audience_symlink_version_self_install", lambda: case_distribution_and_versions(tmp_root / "case1")))
        cases.append(_run_case("offline_profile_user_audience_distribution", lambda: case_profile_user_audience_distribution(tmp_root / "case1_profile_user")))
        cases.append(_run_case("offline_hermes_loader_discovers_symlinked_skills", lambda: case_hermes_loader_discovers_symlinked_skills(tmp_root / "case1_loader")))
        cases.append(_run_case("offline_new_hire_sync_auto_installs_managed_skills", lambda: case_new_hire_sync_auto_installs_managed_skills(tmp_root / "case1a")))
        cases.append(_run_case("offline_child_agent_inherits_skills_not_tokens", lambda: case_child_inherits_skills_not_tokens(tmp_root / "case2")))
        cases.append(_run_case("offline_webui_child_agent_inherits_skills_not_tokens", lambda: case_webui_child_agent_inherits_skills_not_tokens(tmp_root / "case2_webui")))
        cases.append(_run_case("offline_child_install_does_not_sync_back_to_parent", lambda: case_child_install_does_not_sync_back_to_parent(tmp_root / "case3")))
        cases.append(_run_case("offline_shared_token_materialization_is_scoped", lambda: case_shared_token_materialization_is_scoped(tmp_root / "case4")))
        cases.append(_run_case("offline_wildcard_shared_token_skips_group_profiles", lambda: case_wildcard_shared_token_skips_group_profiles(tmp_root / "case4_wildcard")))
        cases.append(_run_case("offline_personal_token_stays_profile_local", lambda: case_personal_token_stays_profile_local(tmp_root / "case5")))
        cases.append(_run_case("offline_registry_audit_personal_managed_loop_guard", lambda: case_registry_audit_and_loop_guard(tmp_root / "case6")))
        cases.append(_run_case("offline_interruption_resume_context", lambda: case_interruption_resume_context(tmp_root / "case7")))
        cases.append(_run_case("offline_continue_turn_reconstructs_interrupted_request", lambda: case_continue_turn_reconstructs_interrupted_request(tmp_root / "case7a")))
        cases.append(_run_case("offline_interruption_arbitrary_followup_resume_context", lambda: case_interruption_arbitrary_followup_context(tmp_root / "case7b")))
        cases.append(_run_case("offline_production_feedback_interruption_quote_resume", lambda: case_production_feedback_interruption_quote_resume(tmp_root / "case7bb")))
        cases.append(_run_case("offline_midrun_exception_preserves_recovery_context", lambda: case_midrun_exception_preserves_recovery_context(tmp_root / "case7bbb")))
        cases.append(_run_case("offline_slow_model_idle_feedback_heartbeat", lambda: case_slow_model_idle_feedback(tmp_root / "case7c")))
        cases.append(_run_case("offline_vision_failure_surfaces_recovery_context", case_vision_failure_surfaces_recovery_context))
        cases.append(_run_case("offline_context_continuity_private_and_group", lambda: case_context_continuity_private_and_group(tmp_root / "case8")))
        cases.append(_run_case("offline_inflight_replacement_scoped_private_group", lambda: case_inflight_replacement_scoped_private_group(tmp_root / "case9")))
        cases.append(_run_case("offline_session_guard_replacement_no_duplicate_dispatch", case_session_guard_replacement_no_duplicate_dispatch))
        cases.append(_run_case("offline_persistent_event_dedupe_skips_redelivery", lambda: case_persistent_event_dedupe_skips_redelivery(tmp_root / "case9a")))
        cases.append(_run_case("offline_personal_skillhub_install_secret_guard", lambda: case_personal_skillhub_install_secret_guard(tmp_root / "case10")))
        cases.append(_run_case("offline_personal_skillhub_clean_install_symlink", lambda: case_personal_skillhub_clean_install_symlink(tmp_root / "case11")))
        cases.append(_run_case("offline_webui_skillhub_owner_scoped_install_and_audit", lambda: case_webui_skillhub_owner_scoped_install_and_audit(tmp_root / "case12")))
    cases.append(_run_case("real_home_secret_free_routes_uat_readiness", lambda: case_real_home_secret_free(real_home)))
    cases.append(_run_case("real_home_skill_inventory_secret_free", lambda: case_real_home_skill_inventory(real_home)))
    cases.append(_run_case("real_group_replacement_race_replay", lambda: case_real_group_replacement_replay(real_home)))
    cases.append(_run_case("real_feishu_uat_user_info", lambda: case_real_uat_user_info(real_home)))
    cases.append(_run_case("real_feishu_uat_scope_inventory_secret_free", lambda: case_real_uat_scope_inventory_secret_free(real_home)))
    cases.append(_run_case("real_feishu_tat_bot_token", lambda: case_real_tat_bot_token(real_home)))

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "repo": str(ROOT),
        "real_home": str(real_home),
        "cases": cases,
        "ok": all(case["ok"] for case in cases),
    }
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "output": str(out), "cases": cases}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
