from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import urllib.error
import zipfile
from pathlib import Path

import pytest
import yaml

from hermes_multitenancy import skillhub_events


def _build_skill_zip(
    skill_name: str,
    *,
    wrapper_dir: str | None = None,
    extra_files: dict[str, str] | None = None,
) -> bytes:
    buffer = io.BytesIO()
    root = f"{wrapper_dir.strip('/')}/" if wrapper_dir else ""
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            f"{root}SKILL.md",
            f"---\nname: {skill_name}\n---\n# {skill_name}\n",
        )
        for rel_path, content in (extra_files or {}).items():
            archive.writestr(f"{root}{rel_path}", content)
    return buffer.getvalue()


def _seed_routing_db(shared_home: Path, rows: list[tuple[str, str]]) -> None:
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
    for name in names:
        (profiles_root / name / "skills").mkdir(parents=True, exist_ok=True)
    return profiles_root


def _event_payload(
    *,
    event_type: str = "skill.install_approved",
    skill_code: str = "daily-breaking",
    release_id: str = "rel_001",
    version: str = "1.0.0",
    download_url: str = "https://example.invalid/daily-breaking.zip",
    checksum_sha256: str | None = None,
    skill_status: str = "active",
    auth_type: str = "auth",
    users: list[str] | None = None,
) -> dict[str, object]:
    return {
        "event_type": event_type,
        "skill_code": skill_code,
        "release_id": release_id,
        "version": version,
        "download_url": download_url,
        "checksum_sha256": checksum_sha256,
        "skill_status": skill_status,
        "audience": {
            "auth_type": auth_type,
            "users": [{"profile_id": user_id} for user_id in (users or ["alice-ldap"])],
        },
    }


def _queue_event(store: skillhub_events.SkillhubEventStore, payload: dict[str, object]) -> str:
    raw = json.dumps(payload)
    event = skillhub_events.normalize_event(payload, raw_body=raw)
    status, duplicate = store.record(event, raw_payload=raw, signature_verified=False)
    assert duplicate is False
    assert status in {"queued", "queued_unknown_type"}
    return event["event_id"]


def test_fresh_install_materializes_skill_and_updates_manifests(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "bob")
    _seed_routing_db(shared_home, [("alice-ldap", "alice"), ("bob-ldap", "bob")])

    package = _build_skill_zip("daily-breaking")
    checksum = hashlib.sha256(package).hexdigest().upper()
    event = _event_payload(
        users=["alice-ldap", "bob-ldap"],
        checksum_sha256=checksum,
    )

    results = process_event(
        event,
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )

    assert results["action"] == "install"
    assert results["skill_code"] == "daily-breaking"
    assert results["version"] == "1.0.0"
    assert results["users"] == {
        "alice-ldap": {"status": "installed", "profile": "alice"},
        "bob-ldap": {"status": "installed", "profile": "bob"},
    }

    canonical = shared_home / "_managed" / "aidock-skillhub" / "daily-breaking" / "1.0.0"
    assert (canonical / "SKILL.md").is_file()

    for profile_name in ("alice", "bob"):
        skill_dir = profiles_root / profile_name / "skills" / "daily-breaking"
        assert skill_dir.resolve() == canonical.resolve()

        managed = json.loads(
            (profiles_root / profile_name / "skills" / ".hermes-managed.json").read_text(encoding="utf-8")
        )
        entry = managed["skills"]["daily-breaking"]
        assert entry["version"] == "1.0.0"
        assert entry["credential"] == "kep-cli"
        assert entry["origin"] == "aidock-skillhub"
        assert entry["source"] == str(canonical)
        assert entry["target"] == str(canonical)

        lock = json.loads(
            (profiles_root / profile_name / "skills" / ".keephub" / "lock.json").read_text(encoding="utf-8")
        )
        assert lock == {
            "installed": {
                "daily-breaking": {"version": "1.0.0", "release_id": "rel_001"}
            }
        }


def test_idempotent_skip_for_direct_processing_and_second_worker_pass(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event, run_worker

    shared_home = tmp_path / "direct" / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])
    package = _build_skill_zip("daily-breaking")
    event = _event_payload(users=["alice-ldap"])

    first = process_event(
        event,
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )
    second = process_event(
        event,
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )

    assert first["users"]["alice-ldap"]["status"] == "installed"
    assert second["users"]["alice-ldap"]["status"] == "skipped"

    worker_home = tmp_path / "worker" / ".hermes"
    worker_profiles = _make_profile_dirs(worker_home, "alice")
    _seed_routing_db(worker_home, [("alice-ldap", "alice")])
    store = skillhub_events.SkillhubEventStore(tmp_path / "worker-events.db")
    _queue_event(store, event)

    summary_first = run_worker(
        store=store,
        shared_home=worker_home,
        profiles_root=worker_profiles,
        downloader=lambda _: package,
    )
    summary_second = run_worker(
        store=store,
        shared_home=worker_home,
        profiles_root=worker_profiles,
        downloader=lambda _: package,
    )

    assert summary_first == {"processed": 1, "installed": 1, "failed": 0, "skipped": 0}
    assert summary_second == {"processed": 0, "installed": 0, "failed": 0, "skipped": 0}


def test_version_upgrade_repoints_existing_profile_link(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])

    v1_zip = _build_skill_zip("daily-breaking")
    v2_zip = _build_skill_zip("daily-breaking", wrapper_dir="daily-breaking-1.0.1")

    first = process_event(
        _event_payload(version="1.0.0", release_id="rel_100"),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: v1_zip,
    )
    second = process_event(
        _event_payload(version="1.0.1", release_id="rel_101"),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: v2_zip,
    )

    assert first["users"]["alice-ldap"]["status"] == "installed"
    assert second["users"]["alice-ldap"]["status"] == "repointed"

    canonical_v1 = shared_home / "_managed" / "aidock-skillhub" / "daily-breaking" / "1.0.0"
    canonical_v2 = shared_home / "_managed" / "aidock-skillhub" / "daily-breaking" / "1.0.1" / "daily-breaking-1.0.1"
    skill_dir = profiles_root / "alice" / "skills" / "daily-breaking"
    assert (canonical_v1 / "SKILL.md").is_file()
    assert (canonical_v2 / "SKILL.md").is_file()
    assert skill_dir.resolve() == canonical_v2.resolve()

    lock = json.loads((profiles_root / "alice" / "skills" / ".keephub" / "lock.json").read_text(encoding="utf-8"))
    assert lock["installed"]["daily-breaking"] == {"version": "1.0.1", "release_id": "rel_101"}


def test_download_expired_marks_event_failed_without_polluting_profiles(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import run_worker

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])
    store = skillhub_events.SkillhubEventStore(tmp_path / "events.db")
    event_id = _queue_event(store, _event_payload())

    def expired(_: str) -> bytes:
        raise urllib.error.HTTPError(
            "https://example.invalid/daily-breaking.zip",
            403,
            "Forbidden",
            hdrs=None,
            fp=io.BytesIO(b"<Error><Code>AccessDenied</Code><Message>Request has expired</Message></Error>"),
        )

    summary = run_worker(
        store=store,
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=expired,
    )

    assert summary == {"processed": 1, "installed": 0, "failed": 1, "skipped": 0}
    row = store.get(event_id)
    assert row is not None
    assert row["status"] == "failed"
    failure = json.loads(row["results_json"])
    assert failure["error_code"] == "DOWNLOAD_EXPIRED"
    assert not (profiles_root / "alice" / "skills" / "daily-breaking").exists()
    assert not (shared_home / "_managed" / "aidock-skillhub" / "daily-breaking" / "1.0.0").exists()


def test_profile_not_found_is_recorded_but_good_profiles_still_install(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import run_worker

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])
    store = skillhub_events.SkillhubEventStore(":memory:")
    event_id = _queue_event(store, _event_payload(users=["alice-ldap", "ghost-ldap"]))
    package = _build_skill_zip("daily-breaking")

    summary = run_worker(
        store=store,
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )

    assert summary == {"processed": 1, "installed": 1, "failed": 0, "skipped": 0}
    row = store.get(event_id)
    assert row is not None
    assert row["status"] == "installed"
    results = json.loads(row["results_json"])
    assert results["users"]["alice-ldap"] == {"status": "installed", "profile": "alice"}
    assert results["users"]["ghost-ldap"] == {"status": "PROFILE_NOT_FOUND", "profile": None}


@pytest.mark.parametrize(
    ("event_type", "skill_code"),
    [
        ("skill.auth_type_changed", "auth-changed"),
        ("skill.permission_approved", "permission-approved"),
    ],
)
def test_incremental_event_types_follow_same_additive_install_path(
    tmp_path: Path,
    event_type: str,
    skill_code: str,
) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / skill_code / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])
    package = _build_skill_zip(skill_code)

    results = process_event(
        _event_payload(event_type=event_type, skill_code=skill_code),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )

    assert results["users"]["alice-ldap"] == {"status": "installed", "profile": "alice"}


def test_status_changed_is_a_known_type_and_store_methods_update_results_json(tmp_path: Path) -> None:
    payload = _event_payload(event_type="skill.status_changed", skill_code="status-skill")
    raw = json.dumps(payload)
    event = skillhub_events.normalize_event(payload, raw_body=raw)
    assert event["known_type"] is True

    store = skillhub_events.SkillhubEventStore(tmp_path / "events.db")
    status, duplicate = store.record(event, raw_payload=raw, signature_verified=False)
    assert duplicate is False
    assert status == "queued"

    queued = store.list_queued()
    assert [row["event_id"] for row in queued] == [event["event_id"]]

    assert store.mark_installed(event["event_id"], {"action": "install", "users": {}}) is True
    installed = store.get(event["event_id"])
    assert installed is not None
    assert installed["status"] == "installed"
    assert json.loads(installed["results_json"]) == {"action": "install", "users": {}}
    assert store.list_queued() == []

    payload2 = _event_payload(event_type="skill.mystery", skill_code="unknown-type")
    raw2 = json.dumps(payload2)
    event2 = skillhub_events.normalize_event(payload2, raw_body=raw2)
    store.record(event2, raw_payload=raw2, signature_verified=False)
    queued_unknown = store.list_queued()
    assert [row["status"] for row in queued_unknown] == ["queued_unknown_type"]

    assert store.mark_failed(event2["event_id"], "PACKAGE_INVALID", "bad zip") is True
    failed = store.get(event2["event_id"])
    assert failed is not None
    assert failed["status"] == "failed"
    failure = json.loads(failed["results_json"])
    assert failure["error_code"] == "PACKAGE_INVALID"
    assert failure["message"] == "bad zip"
    assert "failed_at" in failure


def test_keephub_lock_shape_merges_without_clobbering_existing_skill(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])

    process_event(
        _event_payload(skill_code="daily-breaking", release_id="rel_001", version="1.0.0"),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _build_skill_zip("daily-breaking"),
    )
    process_event(
        _event_payload(skill_code="weather-digest", release_id="rel_002", version="2.0.0"),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _build_skill_zip("weather-digest"),
    )

    lock = json.loads((profiles_root / "alice" / "skills" / ".keephub" / "lock.json").read_text(encoding="utf-8"))
    assert lock == {
        "installed": {
            "daily-breaking": {"version": "1.0.0", "release_id": "rel_001"},
            "weather-digest": {"version": "2.0.0", "release_id": "rel_002"},
        }
    }


def test_process_one_processes_one_queued_event_and_skips_terminal_reposts(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_one

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])
    store = skillhub_events.SkillhubEventStore(":memory:")
    package = _build_skill_zip("daily-breaking")
    event_id = _queue_event(store, _event_payload(users=["alice-ldap"]))

    first = process_one(
        event_id=event_id,
        store=store,
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )
    second = process_one(
        event_id=event_id,
        store=store,
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )

    assert first == {"processed": 1, "status": "installed"}
    assert second == {"processed": 0}
    row = store.get(event_id)
    assert row is not None
    assert row["status"] == "installed"
    assert (profiles_root / "alice" / "skills" / "daily-breaking").exists()


def test_uninstall_from_profile_is_idempotent_and_removes_manifest_and_lock(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import _uninstall_from_profile, process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])
    process_event(
        _event_payload(users=["alice-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _build_skill_zip("daily-breaking"),
    )
    profile_home = profiles_root / "alice"

    first = _uninstall_from_profile(profile_home=profile_home, skill_code="daily-breaking")
    second = _uninstall_from_profile(profile_home=profile_home, skill_code="daily-breaking")

    assert first == {"status": "removed", "profile": "alice"}
    assert second == {"status": "absent", "profile": "alice"}
    assert not (profile_home / "skills" / "daily-breaking").exists()
    managed = json.loads((profile_home / "skills" / ".hermes-managed.json").read_text(encoding="utf-8"))
    assert "daily-breaking" not in managed["skills"]
    lock = json.loads((profile_home / "skills" / ".keephub" / "lock.json").read_text(encoding="utf-8"))
    assert "daily-breaking" not in lock["installed"]


def test_uninstall_from_profile_skips_foreign_manifest_entry(tmp_path: Path) -> None:
    from hermes_multitenancy.skill_registry import MANAGED_SKILL_MANIFEST, _write_manifest
    from hermes_multitenancy.skillhub_installer import _uninstall_from_profile

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    profile_home = profiles_root / "alice"
    skill_dir = profile_home / "skills" / "daily-breaking"
    skill_dir.mkdir(parents=True)
    marker = skill_dir / "SKILL.md"
    marker.write_text("# user skill\n", encoding="utf-8")
    _write_manifest(
        profile_home,
        MANAGED_SKILL_MANIFEST,
        {"daily-breaking": {"origin": "user", "target": str(skill_dir)}},
    )

    result = _uninstall_from_profile(profile_home=profile_home, skill_code="daily-breaking")

    assert result == {"status": "skipped-foreign", "profile": "alice"}
    assert marker.read_text(encoding="utf-8") == "# user skill\n"
    managed = json.loads((profile_home / "skills" / ".hermes-managed.json").read_text(encoding="utf-8"))
    assert managed["skills"]["daily-breaking"]["origin"] == "user"


def test_inactive_status_change_purges_profiles_and_all_distribution_idempotently(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "bob")
    _seed_routing_db(shared_home, [("alice-ldap", "alice"), ("bob-ldap", "bob")])
    config_path = shared_home / "skill-distribution.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump({"skills": []}, sort_keys=False), encoding="utf-8")

    package = _build_skill_zip("daily-breaking")
    process_event(
        _event_payload(users=["alice-ldap", "bob-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )
    process_event(
        _event_payload(auth_type="all", users=[]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )

    result = process_event(
        _event_payload(
            event_type="skill.status_changed",
            skill_status="inactive",
            users=["alice-ldap", "bob-ldap"],
        ),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("inactive event should not download"),
    )
    second = process_event(
        _event_payload(event_type="skill.status_changed", skill_status="inactive"),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("inactive event should not download"),
    )

    assert result["action"] == "uninstall_inactive"
    assert result["skill_code"] == "daily-breaking"
    assert [entry["status"] for entry in result["removed"]] == ["removed", "removed"]
    assert result["distribution"]["entry_removed"] is True
    assert result["distribution"]["source"] == "removed"
    assert second["removed"] == []
    assert second["distribution"] == {
        "status": "absent",
        "entry_removed": False,
        "source": "absent",
    }
    for profile_name in ("alice", "bob"):
        assert not (profiles_root / profile_name / "skills" / "daily-breaking").exists()
    merged = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert merged["skills"] == []
    assert not (shared_home / "skills" / "daily-breaking").exists()


def test_auth_snapshot_shrink_removes_profiles_not_in_full_user_snapshot(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "bob")
    _seed_routing_db(shared_home, [("alice-ldap", "alice"), ("bob-ldap", "bob")])
    package = _build_skill_zip("daily-breaking")

    process_event(
        _event_payload(users=["alice-ldap", "bob-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )
    result = process_event(
        _event_payload(event_type="skill.status_changed", users=["alice-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )

    assert result["users"]["alice-ldap"] == {"status": "skipped", "profile": "alice"}
    assert result["removed"] == [{"status": "removed", "profile": "bob"}]
    assert (profiles_root / "alice" / "skills" / "daily-breaking").exists()
    assert not (profiles_root / "bob" / "skills" / "daily-breaking").exists()


def test_inactive_status_change_purges_profiles_without_downloading(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event, run_worker

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])
    process_event(
        _event_payload(users=["alice-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _build_skill_zip("daily-breaking"),
    )
    store = skillhub_events.SkillhubEventStore(":memory:")
    event_id = _queue_event(
        store,
        _event_payload(
            event_type="skill.status_changed",
            skill_status="inactive",
            users=["alice-ldap"],
        ),
    )

    summary = run_worker(
        store=store,
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("inactive event should not download"),
    )

    assert summary == {"processed": 1, "installed": 1, "failed": 0, "skipped": 0}
    row = store.get(event_id)
    assert row is not None
    assert row["status"] == "installed"
    results = json.loads(row["results_json"])
    assert results["action"] == "uninstall_inactive"
    assert results["skill_code"] == "daily-breaking"
    assert results["removed"] == [{"status": "removed", "profile": "alice"}]
    assert not (profiles_root / "alice" / "skills" / "daily-breaking").exists()


# === B: skill_status=pending => do not install ===


def test_pending_skill_status_is_a_noop_without_download(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])

    result = process_event(
        _event_payload(skill_status="pending", users=["alice-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("pending event must not download"),
    )

    assert result == {"action": "skipped_pending", "skill_code": "daily-breaking"}
    assert not (profiles_root / "alice" / "skills" / "daily-breaking").exists()
    assert not (shared_home / "_managed" / "aidock-skillhub" / "daily-breaking").exists()


def test_pending_skill_status_counts_as_skipped_in_worker(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import run_worker

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])
    store = skillhub_events.SkillhubEventStore(":memory:")
    event_id = _queue_event(store, _event_payload(skill_status="pending", users=["alice-ldap"]))

    summary = run_worker(
        store=store,
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("pending event must not download"),
    )

    assert summary == {"processed": 1, "installed": 0, "failed": 0, "skipped": 1}
    row = store.get(event_id)
    assert row is not None
    assert row["status"] == "installed"  # terminal (results persisted), classified skipped
    assert json.loads(row["results_json"])["action"] == "skipped_pending"


# === C: auth_type_changed is a FULL-snapshot event ===


def test_auth_type_changed_full_snapshot_shrink_removes_dropped_users(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "bob")
    _seed_routing_db(shared_home, [("alice-ldap", "alice"), ("bob-ldap", "bob")])
    package = _build_skill_zip("daily-breaking")

    process_event(
        _event_payload(event_type="skill.install_approved", users=["alice-ldap", "bob-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )
    result = process_event(
        _event_payload(event_type="skill.auth_type_changed", users=["alice-ldap"]),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )

    assert result["users"]["alice-ldap"] == {"status": "skipped", "profile": "alice"}
    assert result["removed"] == [{"status": "removed", "profile": "bob"}]
    assert (profiles_root / "alice" / "skills" / "daily-breaking").exists()
    assert not (profiles_root / "bob" / "skills" / "daily-breaking").exists()


# === F: permission_approved without download_url installs from existing ===


def test_permission_approved_without_download_url_installs_existing_version(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "bob")
    _seed_routing_db(shared_home, [("alice-ldap", "alice"), ("bob-ldap", "bob")])

    process_event(
        _event_payload(users=["alice-ldap"], version="1.0.0", release_id="rel_001"),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: _build_skill_zip("daily-breaking"),
    )

    result = process_event(
        {
            "event_type": "skill.permission_approved",
            "skill_code": "daily-breaking",
            "audience": {"auth_type": "auth", "users": [{"profile_id": "bob-ldap"}]},
        },
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: pytest.fail("permission_approved without url must not download"),
    )

    assert result["action"] == "install"
    assert result["mode"] == "from_existing"
    assert result["version"] == "1.0.0"
    assert result["release_id"] == "rel_001"
    assert result["users"]["bob-ldap"] == {"status": "installed", "profile": "bob"}
    canonical = shared_home / "_managed" / "aidock-skillhub" / "daily-breaking" / "1.0.0"
    assert (profiles_root / "bob" / "skills" / "daily-breaking").resolve() == canonical.resolve()
    # add-only: alice retained, no reconcile removal
    assert (profiles_root / "alice" / "skills" / "daily-breaking").exists()


def test_permission_approved_without_download_url_for_unknown_skill_raises(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import SkillhubInstallError, process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "bob")
    _seed_routing_db(shared_home, [("bob-ldap", "bob")])

    with pytest.raises(SkillhubInstallError) as excinfo:
        process_event(
            {
                "event_type": "skill.permission_approved",
                "skill_code": "never-installed",
                "audience": {"auth_type": "auth", "users": [{"profile_id": "bob-ldap"}]},
            },
            shared_home=shared_home,
            profiles_root=profiles_root,
            downloader=lambda _: pytest.fail("must not download"),
        )

    assert excinfo.value.error_code == "PACKAGE_INVALID"
    assert not (profiles_root / "bob" / "skills" / "never-installed").exists()


# === A: item_type routing ===
# NOTE: the v1 `plugin_deferred` no-op contract was replaced in v2 by the real
# plugin=Expert install path. Plugin routing/install/uninstall/reconcile is now
# covered end-to-end in tests/test_skillhub_plugin.py. The skill-side routing
# (explicit item_type=skill and absent item_type) stays asserted here.


def test_skill_item_type_and_absent_item_type_use_existing_skill_path(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice", "bob")
    _seed_routing_db(shared_home, [("alice-ldap", "alice"), ("bob-ldap", "bob")])
    package = _build_skill_zip("daily-breaking")

    explicit = _event_payload(users=["alice-ldap"])
    explicit["item_type"] = "skill"
    result_explicit = process_event(
        explicit,
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )
    result_absent = process_event(
        _event_payload(users=["bob-ldap"]),  # no item_type key at all
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )

    assert result_explicit["users"]["alice-ldap"] == {"status": "installed", "profile": "alice"}
    assert result_absent["users"]["bob-ldap"] == {"status": "installed", "profile": "bob"}
