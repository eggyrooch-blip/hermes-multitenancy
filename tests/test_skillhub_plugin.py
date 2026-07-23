from __future__ import annotations

import json
import sqlite3
import threading
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
import yaml

from hermes_multitenancy import plugin_ingest as pi

PLUGIN_ID = "keep-resource-delivery"
PLUGIN_VERSION = "0.1.0"
PLUGIN_SKILLS = [
    "using-resource-delivery",
    "kep-trevi-delivery-orchestrate",
    "kep-halo-cli",
]


def _seed_routing_db(shared_home: Path, rows: list[tuple[str, str]]) -> None:
    if not any(user_id == "sunke" for user_id, _ in rows):
        rows = [*rows, ("sunke", "sunke")]
    db_path = shared_home / "multitenancy.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE multitenancy_routing (
                user_id TEXT PRIMARY KEY NOT NULL,
                profile_name TEXT NOT NULL,
                open_id TEXT,
                union_id TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                deleted_at INTEGER,
                synced_at INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1,
                last_active_at INTEGER,
                created_at INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0,
                kind TEXT NOT NULL DEFAULT 'user',
                chat_id TEXT,
                owner_open_id TEXT,
                display_label TEXT
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO multitenancy_routing(
                user_id, profile_name, open_id, active, synced_at, created_at, updated_at, kind
            ) VALUES (?, ?, ?, 1, 0, 0, 0, 'user')
            """,
            [(user_id, profile_name, f"ou_{user_id}") for user_id, profile_name in rows],
        )
        conn.commit()


def _make_profile_dirs(shared_home: Path, *names: str) -> Path:
    profiles_root = shared_home / "profiles"
    for name in (*names, "sunke"):
        (profiles_root / name / "skills").mkdir(parents=True, exist_ok=True)
    return profiles_root


def _plugin_zip(
    *,
    plugin_id: str = PLUGIN_ID,
    version: str = PLUGIN_VERSION,
    content_tag: str = "",
    approval_gates: list[str] | None = None,
    documented_gates: list[str] | None = None,
) -> bytes:
    approval_gates = approval_gates or ["x approve"]
    documented_gates = documented_gates or ["x approve"]
    manifest = {
        "schema": pi.SUPPORTED_SCHEMA,
        "id": plugin_id,
        "name": "资源投放专家",
        "version": version,
        "entry_skill": "using-resource-delivery",
        "skills": {"dir": "./skills/", "list": list(PLUGIN_SKILLS)},
        "install_mode": "copy",
        "audience": {"department_ids": []},
        "clis": [],
        "connectors": [{"id": "kep-cli", "required": True}],
        "governance": {
            "env_default": "pre",
            "approval_required": approval_gates,
            "online_requires": "explicit_action",
        },
        "persona_policy": "skill_inline",
    }
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("__MACOSX/foo", "")
        archive.writestr(".DS_Store", "junk")
        archive.writestr(
            "keep-rd-plugin/.hermes-plugin/plugin.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        for name in PLUGIN_SKILLS:
            body = f"---\nname: {name}\n---\n# {name}\n"
            if "orchestrat" in name:
                body += "\n".join(f"Gates: `{gate}` requires explicit confirmation." for gate in documented_gates) + "\n"
            if content_tag:
                body += f"{content_tag}\n"
            archive.writestr(f"keep-rd-plugin/skills/{name}/SKILL.md", body)
    return buffer.getvalue()


def _bad_plugin_zip() -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("__MACOSX/foo", "")
        archive.writestr(".DS_Store", "junk")
        archive.writestr("keep-rd-plugin/README.md", "not a plugin\n")
    return buffer.getvalue()


def _plugin_event(
    *,
    event_type: str = "skill.install_approved",
    skill_status: str = "active",
    auth_type: str = "auth",
    users: list[str] | None = None,
    download_url: str | None = "https://example.invalid/keep-rd-plugin.zip",
    skill_code: str = PLUGIN_ID,
    version: str | None = None,
    release_id: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_type": event_type,
        "item_type": "plugin",
        "skill_code": skill_code,
        "skill_status": skill_status,
        "audience": {
            "auth_type": auth_type,
            "users": [{"profile_id": user_id} for user_id in (users or [])],
        },
    }
    if download_url is not None:
        payload["download_url"] = download_url
    if version is not None:
        payload["version"] = version
    if release_id is not None:
        payload["release_id"] = release_id
    return payload


def _managed_manifest(shared_home: Path, plugin_id: str = PLUGIN_ID) -> dict[str, object]:
    return json.loads((shared_home / pi.MANAGED_DIR / f"{plugin_id}.json").read_text(encoding="utf-8"))


def _distribution_plugin_entries(shared_home: Path, plugin_id: str = PLUGIN_ID) -> list[dict[str, object]]:
    raw = yaml.safe_load((shared_home / pi.SKILL_DISTRIBUTION_FILE).read_text(encoding="utf-8")) or {}
    return [item for item in raw.get("skills", []) if isinstance(item, dict) and item.get("plugin") == plugin_id]


def test_plugin_install_approved_installs_for_authorized_profiles_only(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "bob", "charlie")
    _seed_routing_db(
        shared_home,
        [("alice-ldap", "alice"), ("bob-ldap", "bob"), ("charlie-ldap", "charlie")],
    )

    result = process_event(
        _plugin_event(users=["alice-ldap", "bob-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )

    assert result["action"] == "plugin_install"
    assert result["plugin_id"] == PLUGIN_ID
    assert result["users"] == {
        "alice-ldap": {"status": "resolved", "profile": "alice"},
        "bob-ldap": {"status": "resolved", "profile": "bob"},
    }
    managed = _managed_manifest(shared_home)
    assert managed["audience"]["profiles"] == ["alice", "bob", "sunke"]
    assert Path(str(managed["repo"])) == shared_home / "_managed" / "aidock-skillhub-plugin" / PLUGIN_ID / PLUGIN_VERSION
    assert (Path(str(managed["repo"])) / pi.PLUGIN_MANIFEST_REL).is_file()
    assert (profiles_root / "alice" / "skills" / "kep-halo-cli").exists()
    assert (profiles_root / "bob" / "skills" / "kep-halo-cli").exists()
    assert not (profiles_root / "charlie" / "skills" / "kep-halo-cli").exists()


def test_plugin_governance_failure_is_auditable_and_inactive(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import SkillhubInstallError, process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])

    with pytest.raises(SkillhubInstallError) as exc:
        process_event(
            _plugin_event(users=["alice-ldap"]),
            shared_home=shared_home,
            profiles_root=profiles_root,
            downloader=lambda _: _plugin_zip(
                approval_gates=["x approve", "missing gate"],
                documented_gates=["x approve"],
            ),
        )

    assert exc.value.error_code == "PLUGIN_INGEST_FAILED"
    manifest = _managed_manifest(shared_home)
    assert manifest["status"] == "inactive"
    assert manifest["audience"]["profiles"] == ["alice", "sunke"]


def test_plugin_new_release_same_inner_version_refreshes_content(tmp_path: Path) -> None:
    # 2026-07-03 incident: upstream published release 168 (v1.0.1) whose zip carried NEW
    # content but an unbumped inner plugin.json version ("0.1.0"); the inner-version cache
    # key matched the stale r165 dir and the new content was silently dropped. The cache
    # must key on the upstream release identity, not the zip's self-declared version.
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])

    result_a = process_event(
        _plugin_event(users=["alice-ldap"], version="1.0.0", release_id="165"),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )
    assert result_a["action"] == "plugin_install"
    repo_a = Path(str(_managed_manifest(shared_home)["repo"]))
    assert repo_a.name == "1.0.0-r165"

    result_b = process_event(
        _plugin_event(users=["alice-ldap"], version="1.0.1", release_id="168"),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(content_tag="RELEASE-168-CONTENT"),
    )
    assert result_b["action"] == "plugin_install"
    repo_b = Path(str(_managed_manifest(shared_home)["repo"]))
    assert repo_b.name == "1.0.1-r168"
    skill_md = repo_b / "skills" / "kep-halo-cli" / "SKILL.md"
    assert "RELEASE-168-CONTENT" in skill_md.read_text(encoding="utf-8")
    installed = profiles_root / "alice" / "skills" / "kep-halo-cli" / "SKILL.md"
    assert "RELEASE-168-CONTENT" in installed.read_text(encoding="utf-8")

    # Same release re-delivered → idempotent reuse, no error, pointer unchanged.
    result_dup = process_event(
        _plugin_event(users=["alice-ldap"], version="1.0.1", release_id="168"),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(content_tag="RELEASE-168-CONTENT"),
    )
    assert result_dup["action"] == "plugin_install"
    assert Path(str(_managed_manifest(shared_home)["repo"])).name == "1.0.1-r168"


def test_plugin_pending_skips_without_download(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])

    result = process_event(
        _plugin_event(skill_status="pending", users=["alice-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("pending plugin event must not download"),
    )

    assert result == {
        "action": "skipped_pending",
        "plugin_id": PLUGIN_ID,
        "item_type": "plugin",
    }
    assert not (shared_home / pi.MANAGED_DIR / f"{PLUGIN_ID}.json").exists()


def test_plugin_without_users_is_stored_for_sunke(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "sunke")
    _seed_routing_db(shared_home, [("alice-ldap", "alice"), ("sunke", "sunke")])

    result = process_event(
        _plugin_event(event_type="skill.permission_approved", users=[]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )

    assert result["action"] == "plugin_install"
    assert _managed_manifest(shared_home)["audience"]["profiles"] == ["sunke"]
    assert (profiles_root / "sunke" / "skills" / "kep-halo-cli").exists()


def test_plugin_full_snapshot_empty_users_uninstalls_everyone(tmp_path: Path) -> None:
    # REGRESSION (codex 2026-07-02): a full-snapshot event (status_changed) with an empty
    # authorized list = de-authorize everyone. Previously the empty-users early-return
    # short-circuited to plugin_no_audience BEFORE reconcile, leaving installs orphaned.
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "bob", "sunke")
    _seed_routing_db(
        shared_home,
        [("alice-ldap", "alice"), ("bob-ldap", "bob"), ("sunke", "sunke")],
    )

    process_event(
        _plugin_event(users=["alice-ldap", "bob-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )
    assert (profiles_root / "alice" / "skills" / "kep-halo-cli").exists()
    assert (profiles_root / "bob" / "skills" / "kep-halo-cli").exists()

    result = process_event(
        _plugin_event(event_type="skill.status_changed", users=[]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )

    assert result["action"] == "plugin_install"
    assert not (profiles_root / "alice" / "skills" / "kep-halo-cli").exists()
    assert not (profiles_root / "bob" / "skills" / "kep-halo-cli").exists()
    assert (profiles_root / "sunke" / "skills" / "kep-halo-cli").exists()
    assert _managed_manifest(shared_home)["audience"]["profiles"] == ["sunke"]


def test_plugin_download_checksum_mismatch_raises(tmp_path: Path) -> None:
    # REGRESSION (codex 2026-07-02): plugin downloads must verify checksum like the skill path.
    from hermes_multitenancy.skillhub_installer import process_event, SkillhubInstallError

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])

    event = _plugin_event(users=["alice-ldap"])
    event["checksum_sha256"] = "deadbeef" * 8  # wrong on purpose

    with pytest.raises(SkillhubInstallError) as exc:
        process_event(
            event,
            shared_home=shared_home,
            profiles_root=profiles_root,
            downloader=lambda _: _plugin_zip(),
        )
    assert exc.value.error_code == "PACKAGE_INVALID"
    assert not (shared_home / pi.MANAGED_DIR / f"{PLUGIN_ID}.json").exists()


def test_plugin_profile_then_all_then_inactive_leaves_no_orphan(tmp_path: Path) -> None:
    # REGRESSION (codex 2026-07-02): install per-profile for alice, switch to auth_type=all,
    # then inactive. Previously the all-ingest overwrote the manifest to mode=all with empty
    # owned_skills, so inactive only stripped distribution and orphaned alice's per-profile copy.
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])
    (shared_home / pi.SKILL_DISTRIBUTION_FILE).write_text("skills: []\n", encoding="utf-8")

    # 1) per-profile install for alice
    process_event(
        _plugin_event(users=["alice-ldap"]),
        shared_home=shared_home, profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )
    assert (profiles_root / "alice" / "skills" / "kep-halo-cli").exists()

    # 2) switch same plugin to all-audience
    process_event(
        _plugin_event(event_type="skill.auth_type_changed", auth_type="all", users=[]),
        shared_home=shared_home, profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )
    # alice's stale per-profile copy must be gone (now covered via all-distribution instead)
    assert not (profiles_root / "alice" / "skills" / "kep-halo-cli").exists()

    # 3) inactive → package retained for audit/reactivation, all execution entries removed
    process_event(
        _plugin_event(event_type="skill.status_changed", skill_status="inactive", users=[]),
        shared_home=shared_home, profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("inactive must not download"),
    )
    assert _managed_manifest(shared_home)["status"] == "inactive"
    assert not (profiles_root / "alice" / "skills" / "kep-halo-cli").exists()
    assert not _distribution_plugin_entries(shared_home)


def test_plugin_all_then_permission_is_noop_no_orphan_on_inactive(tmp_path: Path) -> None:
    # PREEMPTIVE (same transition-orphan class): once a plugin is all-audience, an incremental
    # permission_approved must be a no-op (not downgrade the manifest to profile-mode), else a
    # later inactive uninstall would orphan the all-distribution.
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])
    (shared_home / pi.SKILL_DISTRIBUTION_FILE).write_text("skills: []\n", encoding="utf-8")

    process_event(
        _plugin_event(event_type="skill.install_approved", auth_type="all", users=[]),
        shared_home=shared_home, profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )
    assert _distribution_plugin_entries(shared_home)  # all-distribution present

    result = process_event(
        _plugin_event(event_type="skill.permission_approved", users=["alice-ldap"]),
        shared_home=shared_home, profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )
    assert result["action"] == "plugin_noop_already_all"
    assert _managed_manifest(shared_home)["audience"]["mode"] == "all"  # not downgraded

    # inactive keeps the package manifest but removes distribution
    process_event(
        _plugin_event(event_type="skill.status_changed", skill_status="inactive", users=[]),
        shared_home=shared_home, profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("inactive must not download"),
    )
    assert not _distribution_plugin_entries(shared_home)
    assert _managed_manifest(shared_home)["status"] == "inactive"


def test_plugin_profile_to_all_without_download_reuses_cached_repo(tmp_path: Path) -> None:
    # REGRESSION (codex round 4): auth_type=all with NO download_url must reuse the cached repo.
    # Earlier the uninstall-prior ran BEFORE resolving the cached repo, unlinking the manifest
    # that holds the repo pointer -> plugin_no_package after already destroying the install.
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])
    (shared_home / pi.SKILL_DISTRIBUTION_FILE).write_text("skills: []\n", encoding="utf-8")

    process_event(
        _plugin_event(users=["alice-ldap"]),
        shared_home=shared_home, profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )
    assert (profiles_root / "alice" / "skills" / "kep-halo-cli").exists()

    # switch to all-audience with NO package fields — must reuse the cached repo
    result = process_event(
        _plugin_event(event_type="skill.auth_type_changed", auth_type="all", users=[], download_url=None),
        shared_home=shared_home, profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("no-download all event must not download"),
    )

    assert result["action"] == "plugin_install"
    assert result["mode"] == "all"
    assert _distribution_plugin_entries(shared_home)  # all-distribution registered
    assert _managed_manifest(shared_home)["audience"]["mode"] == "all"
    assert not (profiles_root / "alice" / "skills" / "kep-halo-cli").exists()  # stale profile copy cleared


def test_plugin_inactive_disables_and_second_call_is_idempotent(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])

    process_event(
        _plugin_event(users=["alice-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )

    first = process_event(
        _plugin_event(event_type="skill.status_changed", skill_status="inactive", users=["alice-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("inactive plugin event must not download"),
    )
    second = process_event(
        _plugin_event(event_type="skill.status_changed", skill_status="inactive", users=["alice-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("inactive plugin event must not download"),
    )

    assert first["action"] == "plugin_disable"
    assert first["plugin_id"] == PLUGIN_ID
    assert not (profiles_root / "alice" / "skills" / "kep-halo-cli").exists()
    assert _managed_manifest(shared_home)["status"] == "inactive"
    assert second["action"] == "plugin_disable"
    assert second["status"] == "inactive"

    active = process_event(
        _plugin_event(
            event_type="skill.status_changed",
            skill_status="active",
            users=["alice-ldap"],
            download_url=None,
        ),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("reactivation must reuse the cached package"),
    )
    assert active["action"] == "plugin_install"
    assert _managed_manifest(shared_home)["status"] == "active"


def test_plugin_reactivation_with_empty_users_retains_latest_audience(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "bob")
    _seed_routing_db(shared_home, [("alice-ldap", "alice"), ("bob-ldap", "bob")])
    process_event(
        _plugin_event(users=["alice-ldap", "bob-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )
    process_event(
        _plugin_event(event_type="skill.status_changed", skill_status="inactive", users=[]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("inactive must not download"),
    )

    process_event(
        _plugin_event(
            event_type="skill.status_changed",
            skill_status="active",
            users=[],
            download_url=None,
        ),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("reactivation must use cached package"),
    )

    manifest = _managed_manifest(shared_home)
    assert manifest["status"] == "active"
    assert manifest["audience"]["profiles"] == ["alice", "bob", "sunke"]


def test_statusless_permission_does_not_reactivate_inactive_plugin(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "bob")
    _seed_routing_db(shared_home, [("alice-ldap", "alice"), ("bob-ldap", "bob")])
    process_event(
        _plugin_event(users=["alice-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )
    process_event(
        _plugin_event(event_type="skill.status_changed", skill_status="inactive", users=[]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("inactive must not download"),
    )
    permission = _plugin_event(
        event_type="skill.permission_approved", users=["bob-ldap"], download_url=None
    )
    permission.pop("skill_status")
    skipped = process_event(
        permission,
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("permission must use cached package"),
    )

    manifest = _managed_manifest(shared_home)
    assert manifest["status"] == "inactive"
    assert skipped["action"] == "plugin_skipped_inactive"
    assert manifest["audience"]["profiles"] == ["alice", "sunke"]
    assert not (profiles_root / "bob" / "skills" / "kep-halo-cli").exists()


def test_explicit_active_and_failed_ingest_share_one_plugin_transaction(tmp_path, monkeypatch):
    from hermes_multitenancy import skillhub_installer as si

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])
    si.process_event(
        _plugin_event(users=["alice-ldap"], release_id="valid"),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )
    si.process_event(
        _plugin_event(event_type="skill.status_changed", skill_status="inactive", users=[]),
        shared_home=shared_home,
        profiles_root=profiles_root,
    )

    first_paused = threading.Event()
    release_first = threading.Event()
    second_mutating = threading.Event()
    release_second = threading.Event()
    original_assert = pi.assert_profile_governance
    original_install = pi._install_skills_to_profile
    original_set_status = si._set_plugin_status

    def pause_first(plugin, *args):
        result = original_assert(plugin, *args)
        if threading.current_thread().name == "active-event":
            first_paused.set()
            assert release_first.wait(2)
        return result

    def pause_second(plugin, *args, **kwargs):
        if threading.current_thread().name == "invalid-event":
            second_mutating.set()
            assert release_second.wait(2)
        return original_install(plugin, *args, **kwargs)

    def reject_out_of_transaction_active(shared, plugin_id, status):
        assert status != "active"
        return original_set_status(shared, plugin_id, status)

    monkeypatch.setattr(pi, "assert_profile_governance", pause_first)
    monkeypatch.setattr(pi, "_install_skills_to_profile", pause_second)
    monkeypatch.setattr(si, "_set_plugin_status", reject_out_of_transaction_active)
    errors = []

    def run_active():
        try:
            si.process_event(
                _plugin_event(
                    event_type="skill.status_changed",
                    skill_status="active",
                    users=[],
                    download_url=None,
                ),
                shared_home=shared_home,
                profiles_root=profiles_root,
            )
        except Exception as exc:
            errors.append(exc)

    def run_invalid():
        try:
            si.process_event(
                _plugin_event(users=["alice-ldap"], release_id="invalid"),
                shared_home=shared_home,
                profiles_root=profiles_root,
                downloader=lambda _: _plugin_zip(
                    approval_gates=["missing gate"], documented_gates=["x approve"]
                ),
            )
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=run_active, name="active-event")
    second = threading.Thread(target=run_invalid, name="invalid-event")
    first.start()
    assert first_paused.wait(2)
    assert _managed_manifest(shared_home)["status"] == "inactive"
    second.start()
    assert not second_mutating.wait(0.1)
    release_first.set()
    assert second_mutating.wait(2)
    assert _managed_manifest(shared_home)["status"] == "inactive"
    release_second.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive() and not second.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], si.SkillhubInstallError)
    assert _managed_manifest(shared_home)["status"] == "inactive"


def test_full_snapshot_reconcile_and_ingest_are_one_plugin_transaction(tmp_path, monkeypatch):
    from hermes_multitenancy import skillhub_installer as si

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "bob")
    _seed_routing_db(shared_home, [("alice-ldap", "alice"), ("bob-ldap", "bob")])
    si.process_event(
        _plugin_event(users=["alice-ldap", "bob-ldap"], release_id="initial"),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )

    reconcile_paused = threading.Event()
    release_reconcile = threading.Event()
    invalid_mutating = threading.Event()
    release_invalid = threading.Event()
    original_uninstall = pi._uninstall_locked
    original_install = pi._install_skills_to_profile

    def pause_reconcile(*args, **kwargs):
        if threading.current_thread().name == "reconcile-event":
            assert _managed_manifest(shared_home)["status"] == "active"
            reconcile_paused.set()
            assert release_reconcile.wait(2)
        return original_uninstall(*args, **kwargs)

    def pause_invalid(plugin, *args, **kwargs):
        if threading.current_thread().name == "invalid-event":
            invalid_mutating.set()
            assert release_invalid.wait(2)
        return original_install(plugin, *args, **kwargs)

    monkeypatch.setattr(pi, "_uninstall_locked", pause_reconcile)
    monkeypatch.setattr(pi, "_install_skills_to_profile", pause_invalid)
    errors = []

    def run_reconcile():
        try:
            si.process_event(
                _plugin_event(
                    event_type="skill.status_changed",
                    users=["alice-ldap"],
                    release_id="snapshot",
                ),
                shared_home=shared_home,
                profiles_root=profiles_root,
                downloader=lambda _: _plugin_zip(),
            )
        except Exception as exc:
            errors.append(exc)

    def run_invalid():
        try:
            si.process_event(
                _plugin_event(users=["alice-ldap"], release_id="invalid"),
                shared_home=shared_home,
                profiles_root=profiles_root,
                downloader=lambda _: _plugin_zip(
                    approval_gates=["missing gate"], documented_gates=["x approve"]
                ),
            )
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=run_reconcile, name="reconcile-event")
    second = threading.Thread(target=run_invalid, name="invalid-event")
    first.start()
    assert reconcile_paused.wait(2)
    second.start()
    assert not invalid_mutating.wait(0.1)
    release_reconcile.set()
    assert invalid_mutating.wait(2)
    assert _managed_manifest(shared_home)["status"] == "inactive"
    release_invalid.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive() and not second.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], si.SkillhubInstallError)
    assert _managed_manifest(shared_home)["status"] == "inactive"


def test_inactive_unknown_plugin_never_downloads(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import SkillhubInstallError, process_event

    with pytest.raises(SkillhubInstallError, match="no managed manifest"):
        process_event(
            _plugin_event(
                event_type="skill.status_changed",
                skill_status="inactive",
                users=[],
            ),
            shared_home=tmp_path / ".hermes",
            downloader=lambda _: pytest.fail("inactive must not download"),
        )


def test_plugin_install_fails_closed_when_default_profile_is_unproven(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import SkillhubInstallError, process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])
    with sqlite3.connect(shared_home / "multitenancy.db") as conn:
        conn.execute("DELETE FROM multitenancy_routing WHERE user_id = 'sunke'")
        conn.commit()

    with pytest.raises(SkillhubInstallError) as exc:
        process_event(
            _plugin_event(users=["alice-ldap"]),
            shared_home=shared_home,
            profiles_root=profiles_root,
            downloader=lambda _: _plugin_zip(),
        )
    assert exc.value.error_code == "DEFAULT_PROFILE_UNRESOLVED"
    assert not (shared_home / pi.MANAGED_DIR / f"{PLUGIN_ID}.json").exists()


def test_plugin_permission_approved_uses_cached_repo_and_keeps_existing_audience(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "bob", "sunke")
    _seed_routing_db(
        shared_home,
        [("alice-ldap", "alice"), ("bob-ldap", "bob"), ("sunke", "sunke")],
    )

    process_event(
        _plugin_event(users=["alice-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )

    result = process_event(
        _plugin_event(
            event_type="skill.permission_approved",
            users=["bob-ldap"],
            download_url=None,
        ),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("cached incremental plugin event must not download"),
    )

    managed = _managed_manifest(shared_home)
    assert result["action"] == "plugin_install"
    assert result["mode"] == "from_existing"
    assert result["users"] == {"bob-ldap": {"status": "resolved", "profile": "bob"}}
    assert set(managed["audience"]["profiles"]) == {"alice", "bob", "sunke"}
    assert (profiles_root / "alice" / "skills" / "kep-halo-cli").exists()
    assert (profiles_root / "bob" / "skills" / "kep-halo-cli").exists()


def test_plugin_install_resolves_wrapper_repo_root_and_ignores_zip_cruft(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])

    result = process_event(
        _plugin_event(users=["alice-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )

    managed = _managed_manifest(shared_home)
    repo_path = Path(str(managed["repo"]))
    assert result["action"] == "plugin_install"
    assert repo_path.name == PLUGIN_VERSION
    assert (repo_path / pi.PLUGIN_MANIFEST_REL).is_file()
    assert not (repo_path / "__MACOSX").exists()


def test_plugin_bad_package_raises_package_invalid(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import SkillhubInstallError, process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])

    with pytest.raises(SkillhubInstallError) as excinfo:
        process_event(
            _plugin_event(users=["alice-ldap"]),
            shared_home=shared_home,
            profiles_root=profiles_root,
            downloader=lambda _: _bad_plugin_zip(),
        )

    assert excinfo.value.error_code == "PACKAGE_INVALID"


def test_plugin_full_snapshot_shrink_reconciles_removed_profiles(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "bob")
    _seed_routing_db(shared_home, [("alice-ldap", "alice"), ("bob-ldap", "bob")])

    process_event(
        _plugin_event(users=["alice-ldap", "bob-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )

    result = process_event(
        _plugin_event(users=["alice-ldap"], download_url="https://example.invalid/fresh-plugin.zip"),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )

    managed = _managed_manifest(shared_home)
    assert result["action"] == "plugin_install"
    assert managed["audience"]["profiles"] == ["alice", "sunke"]
    assert (profiles_root / "alice" / "skills" / "kep-halo-cli").exists()
    assert not (profiles_root / "bob" / "skills" / "kep-halo-cli").exists()


def test_plugin_full_snapshot_shrink_removes_only_dropped_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "bob")
    _seed_routing_db(shared_home, [("alice-ldap", "alice"), ("bob-ldap", "bob")])

    process_event(
        _plugin_event(users=["alice-ldap", "bob-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )

    removed_profiles: list[str] = []
    real_uninstall = pi.uninstall_personal_skill_for_profile

    def spy_uninstall(*, profile_home: Path, skill_path: str) -> dict[str, object]:
        removed_profiles.append(profile_home.name)
        return real_uninstall(profile_home=profile_home, skill_path=skill_path)

    monkeypatch.setattr(pi, "uninstall_personal_skill_for_profile", spy_uninstall)

    result = process_event(
        _plugin_event(users=["alice-ldap"], download_url="https://example.invalid/fresh-plugin.zip"),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )

    managed = _managed_manifest(shared_home)
    assert result["action"] == "plugin_install"
    assert set(removed_profiles) == {"bob"}
    assert managed["audience"]["profiles"] == ["alice", "sunke"]
    assert (profiles_root / "alice" / "skills" / "kep-halo-cli").exists()
    assert not (profiles_root / "bob" / "skills" / "kep-halo-cli").exists()


def test_plugin_all_audience_installs_global_distribution(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])

    result = process_event(
        _plugin_event(auth_type="all", users=[]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
        allow_create_distribution=True,  # plugin-all now requires opt-in (MED-4)
    )

    assert result["action"] == "plugin_install"
    assert result["mode"] == "all"
    entries = _distribution_plugin_entries(shared_home)
    assert {entry["path"] for entry in entries} == set(PLUGIN_SKILLS)
    assert all(entry["audience"] == "all" for entry in entries)
    assert _managed_manifest(shared_home)["audience"]["mode"] == "all"

    inactive = process_event(
        _plugin_event(
            event_type="skill.status_changed",
            skill_status="inactive",
            auth_type="all",
            download_url=None,
        ),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("inactive all-audience plugin event must not download"),
    )

    assert inactive["action"] == "plugin_disable"
    assert not _distribution_plugin_entries(shared_home)
    assert _managed_manifest(shared_home)["status"] == "inactive"


def test_plugin_all_audience_installs_with_download(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])

    result = process_event(
        _plugin_event(auth_type="all", users=["alice-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
        allow_create_distribution=True,  # plugin-all now requires opt-in (MED-4)
    )

    assert result["action"] == "plugin_install"
    assert result["mode"] == "all"
    assert _managed_manifest(shared_home)["audience"]["mode"] == "all"


def test_plugin_all_without_opt_in_refuses_to_create_distribution(tmp_path: Path) -> None:
    """MED-4 regression: plugin auth_type='all' must NOT auto-create
    skill-distribution.yaml without allow_create_distribution, matching the skill
    path. Pre-fix it hardcoded True and silently created it — overriding every
    profile's default-skill source. FAILS on pre-fix code (no raise)."""
    from hermes_multitenancy.skillhub_installer import process_event, SkillhubInstallError

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])

    with pytest.raises(SkillhubInstallError):
        process_event(
            _plugin_event(auth_type="all", users=[]),
            shared_home=shared_home,
            profiles_root=profiles_root,
            downloader=lambda _: _plugin_zip(),
            # no allow_create_distribution → default False → must refuse
        )
    # the guard refused: skill-distribution.yaml was NOT created
    assert not (shared_home / "skill-distribution.yaml").exists()


def test_plugin_no_cached_repo_returns_plugin_no_package(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])

    result = process_event(
        _plugin_event(
            event_type="skill.permission_approved",
            users=["alice-ldap"],
            download_url=None,
        ),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("plugin_no_package path must not download"),
    )

    assert result == {
        "action": "plugin_no_package",
        "plugin_id": PLUGIN_ID,
        "reason": "no download_url and no cached plugin repo",
    }


def _write_sticky(shared_home: Path, profiles: list[str], plugin_id: str = PLUGIN_ID) -> None:
    (shared_home / pi.MANAGED_DIR / f"{plugin_id}.sticky.json").write_text(
        json.dumps({"plugin_id": plugin_id, "profiles": profiles}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_sticky_survives_full_snapshot_shrink_but_nonsticky_dropped(tmp_path: Path) -> None:
    # INCIDENT 2026-07-02 regression: a full-snapshot status_changed with a short upstream
    # list must NOT remove the manually-pinned (sticky) whitelist; non-sticky still drop.
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "dave", "carol")
    _seed_routing_db(shared_home, [("alice-ldap", "alice"), ("dave-ldap", "dave"), ("carol-ldap", "carol")])
    (shared_home / pi.SKILL_DISTRIBUTION_FILE).write_text("skills: []\n", encoding="utf-8")

    # install for alice + dave, then pin ONLY alice sticky
    process_event(_plugin_event(users=["alice-ldap", "dave-ldap"]),
                  shared_home=shared_home, profiles_root=profiles_root, downloader=lambda _: _plugin_zip())
    _write_sticky(shared_home, ["alice"])

    # full-snapshot event authorizing only carol (neither alice nor dave)
    process_event(_plugin_event(event_type="skill.status_changed", users=["carol-ldap"]),
                  shared_home=shared_home, profiles_root=profiles_root, downloader=lambda _: _plugin_zip())

    assert (profiles_root / "alice" / "skills" / "using-resource-delivery").exists()   # sticky KEPT
    assert not (profiles_root / "dave" / "skills" / "using-resource-delivery").exists() # non-sticky DROPPED
    assert (profiles_root / "carol" / "skills" / "using-resource-delivery").exists()    # new installed
    assert "alice" in _managed_manifest(shared_home)["audience"]["profiles"]            # sticky stays in manifest


def test_inactive_revokes_sticky_and_all_installed_profiles(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "dave")
    _seed_routing_db(shared_home, [("alice-ldap", "alice"), ("dave-ldap", "dave")])
    (shared_home / pi.SKILL_DISTRIBUTION_FILE).write_text("skills: []\n", encoding="utf-8")

    process_event(_plugin_event(users=["alice-ldap", "dave-ldap"]),
                  shared_home=shared_home, profiles_root=profiles_root, downloader=lambda _: _plugin_zip())
    _write_sticky(shared_home, ["alice"])

    result = process_event(_plugin_event(event_type="skill.status_changed", skill_status="inactive", users=[]),
                           shared_home=shared_home, profiles_root=profiles_root,
                           downloader=lambda _: pytest.fail("inactive must not download"))

    assert result["action"] == "plugin_disable"
    assert not (profiles_root / "alice" / "skills" / "using-resource-delivery").exists()
    assert not (profiles_root / "dave" / "skills" / "using-resource-delivery").exists()
    assert _managed_manifest(shared_home)["status"] == "inactive"


def test_inactive_prunes_existing_org_fanout_and_sync_cannot_reinstall(
    tmp_path: Path,
) -> None:
    from hermes_multitenancy.skillhub_installer import process_event
    from hermes_multitenancy.skill_registry import list_profile_skill_slash_commands
    from hermes_multitenancy.sync.feishu_org import _sync_default_profile_skills

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])
    (shared_home / pi.SKILL_DISTRIBUTION_FILE).write_text(
        "skills: []\n", encoding="utf-8"
    )
    process_event(
        _plugin_event(auth_type="all", users=[]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )
    alice = profiles_root / "alice"
    manifest_before = (shared_home / pi.MANAGED_DIR / f"{PLUGIN_ID}.json").read_bytes()
    distribution_before = (
        shared_home / pi.SKILL_DISTRIBUTION_FILE
    ).read_bytes()
    _sync_default_profile_skills(alice, shared_home)
    assert (alice / "skills" / "kep-halo-cli").exists()
    assert _managed_manifest(shared_home)["status"] == "active"
    assert (shared_home / pi.MANAGED_DIR / f"{PLUGIN_ID}.json").read_bytes() == manifest_before
    assert (shared_home / pi.SKILL_DISTRIBUTION_FILE).read_bytes() == distribution_before

    process_event(
        _plugin_event(
            event_type="skill.status_changed",
            skill_status="inactive",
            users=[],
        ),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("inactive must not download"),
    )

    assert not (alice / "skills" / "kep-halo-cli").exists()
    assert not any(
        command["name"] == "kep-halo-cli"
        for command in list_profile_skill_slash_commands(profile_home=alice)
    )
    assert not _distribution_plugin_entries(shared_home)
    (shared_home / "profile-skill-defaults.yaml").write_text(
        "skills:\n  - kep-halo-cli\n", encoding="utf-8"
    )
    _sync_default_profile_skills(alice, shared_home)
    assert not (alice / "skills" / "kep-halo-cli").exists()


def test_inactive_cleanup_failure_still_revokes_execution(
    tmp_path: Path, monkeypatch
) -> None:
    from hermes_multitenancy import skillhub_installer as si
    from hermes_multitenancy.skill_registry import list_profile_skill_slash_commands
    from hermes_multitenancy.sync.feishu_org import _sync_default_profile_skills
    from hermes_multitenancy.run_broker import RunBroker, RunRejected
    from hermes_multitenancy.run_models import RunRequest
    import hermes_multitenancy.router as router

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])
    (shared_home / pi.SKILL_DISTRIBUTION_FILE).write_text(
        "skills: []\n", encoding="utf-8"
    )
    si.process_event(
        _plugin_event(auth_type="all", users=[]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )
    _sync_default_profile_skills(profiles_root / "alice", shared_home)
    monkeypatch.setattr(
        router,
        "_profile_name_to_home",
        lambda _profile: profiles_root / "alice",
    )
    broker = RunBroker(
        dispatch_agent=lambda _request: pytest.fail("must not dispatch"),
        sandbox_available=lambda: True,
    )
    request = RunRequest(
        channel="webui",
        profile_name="alice",
        user_key="alice",
        content="hello",
    )
    assert broker.check_policy(request) == request
    monkeypatch.setattr(
        pi,
        "_prune_plugin_managed_fanout",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            pi.PluginIngestError("injected cleanup failure")
        ),
    )

    with pytest.raises(si.SkillhubInstallError, match="injected cleanup failure"):
        si.process_event(
            _plugin_event(
                event_type="skill.status_changed",
                skill_status="inactive",
                users=[],
            ),
            shared_home=shared_home,
            profiles_root=profiles_root,
        )
    assert _managed_manifest(shared_home)["status"] == "inactive"
    assert (profiles_root / "alice" / "skills" / "kep-halo-cli").exists()
    with pytest.raises(RunRejected, match="plugin state"):
        broker.check_policy(request)
    assert not any(
        command["name"] == "kep-halo-cli"
        for command in list_profile_skill_slash_commands(
            profile_home=profiles_root / "alice"
        )
    )
    from agent import skill_utils
    from hermes_multitenancy.router import (
        _restore_profile_skill_loader,
        _scope_profile_skill_loader,
    )

    states = _scope_profile_skill_loader(profiles_root / "alice")
    try:
        assert not any(
            "kep-halo-cli" in path.parts
            for path in skill_utils.iter_skill_index_files(
                profiles_root / "alice" / "skills",
                "SKILL.md",
            )
        )
    finally:
        _restore_profile_skill_loader(states)


def test_inactive_never_deletes_foreign_overwrite(tmp_path: Path) -> None:
    from hermes_multitenancy import skillhub_installer as si
    from hermes_multitenancy.sync.feishu_org import _sync_default_profile_skills

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])
    (shared_home / pi.SKILL_DISTRIBUTION_FILE).write_text(
        "skills: []\n", encoding="utf-8"
    )
    si.process_event(
        _plugin_event(auth_type="all", users=[]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _plugin_zip(),
    )
    alice = profiles_root / "alice"
    _sync_default_profile_skills(alice, shared_home)
    target = alice / "skills" / "kep-halo-cli"
    if target.is_symlink():
        target.unlink()
    else:
        import shutil

        shutil.rmtree(target)
    target.mkdir()
    (target / "SKILL.md").write_text("# employee-owned\n", encoding="utf-8")

    with pytest.raises(si.SkillhubInstallError, match="cannot prune modified"):
        si.process_event(
            _plugin_event(
                event_type="skill.status_changed",
                skill_status="inactive",
                users=[],
            ),
            shared_home=shared_home,
            profiles_root=profiles_root,
        )
    assert _managed_manifest(shared_home)["status"] == "inactive"
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "# employee-owned\n"


def test_no_sticky_file_preserves_normal_shrink(tmp_path: Path) -> None:
    # Regression: with NO sticky file, diff-shrink behaves exactly as before (drop the dropped).
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "bob")
    _seed_routing_db(shared_home, [("alice-ldap", "alice"), ("bob-ldap", "bob")])
    (shared_home / pi.SKILL_DISTRIBUTION_FILE).write_text("skills: []\n", encoding="utf-8")

    process_event(_plugin_event(users=["alice-ldap", "bob-ldap"]),
                  shared_home=shared_home, profiles_root=profiles_root, downloader=lambda _: _plugin_zip())
    process_event(_plugin_event(event_type="skill.status_changed", users=["alice-ldap"]),
                  shared_home=shared_home, profiles_root=profiles_root, downloader=lambda _: _plugin_zip())

    assert (profiles_root / "alice" / "skills" / "using-resource-delivery").exists()
    assert not (profiles_root / "bob" / "skills" / "using-resource-delivery").exists()  # normal drop intact


def test_sticky_kept_in_manifest_on_cached_repo_full_snapshot(tmp_path: Path) -> None:
    # codex 2026-07-02: the NO-download (cached repo) full-snapshot path must also union sticky
    # into the ingest target, else ingest rewrites the manifest audience WITHOUT sticky.
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "carol")
    _seed_routing_db(shared_home, [("alice-ldap", "alice"), ("carol-ldap", "carol")])
    (shared_home / pi.SKILL_DISTRIBUTION_FILE).write_text("skills: []\n", encoding="utf-8")

    # first install (with download) so a cached repo + manifest exist
    process_event(_plugin_event(users=["alice-ldap"]),
                  shared_home=shared_home, profiles_root=profiles_root, downloader=lambda _: _plugin_zip())
    _write_sticky(shared_home, ["alice"])

    # full-snapshot with NO download_url (cached-repo path), new list = carol only
    process_event(_plugin_event(event_type="skill.status_changed", users=["carol-ldap"], download_url=None),
                  shared_home=shared_home, profiles_root=profiles_root,
                  downloader=lambda _: pytest.fail("cached-repo path must not download"))

    audience = _managed_manifest(shared_home)["audience"]["profiles"]
    assert "alice" in audience   # sticky RETAINED in manifest (was the bug: dropped from audience)
    assert (profiles_root / "alice" / "skills" / "using-resource-delivery").exists()


def test_sticky_survives_shrink_to_zero(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "dave")
    _seed_routing_db(shared_home, [("alice-ldap", "alice"), ("dave-ldap", "dave")])
    (shared_home / pi.SKILL_DISTRIBUTION_FILE).write_text("skills: []\n", encoding="utf-8")

    process_event(_plugin_event(users=["alice-ldap", "dave-ldap"]),
                  shared_home=shared_home, profiles_root=profiles_root, downloader=lambda _: _plugin_zip())
    _write_sticky(shared_home, ["alice"])

    # full-snapshot with EMPTY users (de-authorize everyone) — sticky must still survive
    process_event(_plugin_event(event_type="skill.status_changed", users=[], download_url=None),
                  shared_home=shared_home, profiles_root=profiles_root,
                  downloader=lambda _: pytest.fail("shrink-to-zero must not download"))

    assert (profiles_root / "alice" / "skills" / "using-resource-delivery").exists()    # sticky KEPT
    assert not (profiles_root / "dave" / "skills" / "using-resource-delivery").exists()  # non-sticky gone
    assert _managed_manifest(shared_home)["audience"]["profiles"] == ["alice", "sunke"] # sticky + default retained
