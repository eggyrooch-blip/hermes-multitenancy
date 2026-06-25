from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import urllib.error
import zipfile
from pathlib import Path

import pytest

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
            "auth_type": "auth",
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


def test_inactive_status_change_skips_without_touching_profiles(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import run_worker

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])
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

    assert summary == {"processed": 1, "installed": 0, "failed": 0, "skipped": 1}
    row = store.get(event_id)
    assert row is not None
    assert row["status"] == "installed"
    assert json.loads(row["results_json"]) == {"action": "skipped_inactive", "users": {}}
    assert not (profiles_root / "alice" / "skills" / "daily-breaking").exists()
