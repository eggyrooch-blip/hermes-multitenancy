from __future__ import annotations

import hashlib
import io
import json
import sqlite3
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
    skill_code: str = "daily-breaking",
    release_id: str = "rel_001",
    version: str = "1.0.0",
    download_url: str = "https://example.invalid/daily-breaking.zip",
    checksum_sha256: str | None = None,
    auth_type: str = "auth",
    users: list[str] | None = None,
) -> dict[str, object]:
    return {
        "event_type": "skill.install_approved",
        "skill_code": skill_code,
        "release_id": release_id,
        "version": version,
        "download_url": download_url,
        "checksum_sha256": checksum_sha256,
        "skill_status": "active",
        "audience": {
            "auth_type": auth_type,
            "users": [{"profile_id": user_id} for user_id in (users or [])],
        },
    }


def _normalize_event(payload: dict[str, object]) -> dict[str, object]:
    raw = json.dumps(payload)
    return skillhub_events.normalize_event(payload, raw_body=raw)


def _queue_event(store: skillhub_events.SkillhubEventStore, payload: dict[str, object]) -> str:
    raw = json.dumps(payload)
    event = skillhub_events.normalize_event(payload, raw_body=raw)
    status, duplicate = store.record(event, raw_payload=raw, signature_verified=False)
    assert duplicate is False
    assert status in {"queued", "queued_unknown_type"}
    return event["event_id"]


def test_all_audience_event_registers_shared_distribution_source_and_entry(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home)
    config_path = shared_home / "skill-distribution.yaml"
    existing_all = {"path": "other-skill", "audience": "all", "install_mode": "symlink"}
    existing_department = {
        "path": "dept-skill",
        "audience": {"department_ids": [2001]},
        "install_mode": "copy",
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(
            {"schema": 1, "skills": [existing_all, existing_department]},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    package = _build_skill_zip("daily-breaking")
    event = _normalize_event(
        _event_payload(
            auth_type="all",
            users=[],
            checksum_sha256=hashlib.sha256(package).hexdigest(),
        )
    )

    result = process_event(
        event,
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )

    canonical_root = shared_home / "_managed" / "aidock-skillhub" / "daily-breaking" / "1.0.0"
    shared_source = shared_home / "skills" / "daily-breaking"
    assert shared_source.is_symlink()
    assert shared_source.resolve() == canonical_root.resolve()

    merged = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert merged["schema"] == 1
    assert existing_all in merged["skills"]
    assert existing_department in merged["skills"]
    assert {
        "path": "daily-breaking",
        "audience": "all",
        "install_mode": "symlink",
        "origin": "aidock-skillhub",
    } in merged["skills"]

    assert result["action"] == "install"
    assert result["mode"] == "all"
    assert result["distribution"] == {
        "path": "daily-breaking",
        "audience": "all",
        "install_mode": "symlink",
        "config_path": str(config_path),
        "source": "registered",
        "entry_written": True,
    }


def test_all_audience_rerun_is_idempotent_and_keeps_existing_distribution_entries(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home)
    config_path = shared_home / "skill-distribution.yaml"
    existing_entries = [
        {"path": "other-skill", "audience": "all", "install_mode": "symlink"},
        {"path": "dept-skill", "audience": {"department_ids": [2001]}, "install_mode": "copy"},
    ]
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump({"skills": existing_entries}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    package = _build_skill_zip("daily-breaking")
    event = _normalize_event(
        _event_payload(
            auth_type="all",
            users=[],
            checksum_sha256=hashlib.sha256(package).hexdigest(),
        )
    )

    first = process_event(
        event,
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )
    config_after_first = config_path.read_text(encoding="utf-8")
    second = process_event(
        event,
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )
    config_after_second = config_path.read_text(encoding="utf-8")

    merged = yaml.safe_load(config_after_second)
    our_entries = [
        entry
        for entry in merged["skills"]
        if isinstance(entry, dict)
        and entry.get("path") == "daily-breaking"
        and entry.get("origin") == "aidock-skillhub"
    ]
    assert len(our_entries) == 1
    assert existing_entries[0] in merged["skills"]
    assert existing_entries[1] in merged["skills"]
    assert config_after_first == config_after_second
    assert (shared_home / "skills" / "daily-breaking").resolve() == (
        shared_home / "_managed" / "aidock-skillhub" / "daily-breaking" / "1.0.0"
    ).resolve()
    assert first["distribution"]["source"] == "registered"
    assert second["distribution"]["source"] == "present"


def test_all_audience_missing_distribution_file_fails_without_creating_side_effects(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import SkillhubInstallError, process_event, run_worker

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home)
    config_path = shared_home / "skill-distribution.yaml"
    package = _build_skill_zip("daily-breaking")
    payload = _event_payload(
        auth_type="all",
        users=[],
        checksum_sha256=hashlib.sha256(package).hexdigest(),
    )
    event = _normalize_event(payload)

    with pytest.raises(SkillhubInstallError) as exc_info:
        process_event(
            event,
            shared_home=shared_home,
            profiles_root=profiles_root,
            downloader=lambda _: package,
        )

    assert exc_info.value.error_code == "DISTRIBUTION_FILE_MISSING"
    assert not config_path.exists()
    assert not (shared_home / "skills" / "daily-breaking").exists()
    assert not (shared_home / "_managed" / "aidock-skillhub" / "daily-breaking" / "1.0.0").exists()

    store = skillhub_events.SkillhubEventStore(tmp_path / "events.db")
    event_id = _queue_event(store, payload)
    summary = run_worker(
        store=store,
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )

    assert summary == {"processed": 1, "installed": 0, "failed": 1, "skipped": 0}
    row = store.get(event_id)
    assert row is not None
    assert row["status"] == "failed"
    failure = json.loads(row["results_json"])
    assert failure["error_code"] == "DISTRIBUTION_FILE_MISSING"
    assert not config_path.exists()


def test_auth_event_regression_stays_per_profile_and_result_shape_is_unchanged(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])
    package = _build_skill_zip("daily-breaking")
    event = _normalize_event(
        _event_payload(
            auth_type="auth",
            users=["alice-ldap"],
            checksum_sha256=hashlib.sha256(package).hexdigest(),
        )
    )

    result = process_event(
        event,
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )

    assert result == {
        "action": "install",
        "skill_code": "daily-breaking",
        "version": "1.0.0",
        "release_id": "rel_001",
        "users": {"alice-ldap": {"status": "installed", "profile": "alice"}},
    }
    assert "mode" not in result
    assert (profiles_root / "alice" / "skills" / "daily-breaking").resolve() == (
        shared_home / "_managed" / "aidock-skillhub" / "daily-breaking" / "1.0.0"
    ).resolve()
    managed = json.loads(
        (profiles_root / "alice" / "skills" / ".hermes-managed.json").read_text(encoding="utf-8")
    )
    assert managed["skills"]["daily-breaking"]["origin"] == "aidock-skillhub"


def test_all_audience_to_auth_snapshot_removes_distribution_and_installs_subset(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home, "alice")
    _seed_routing_db(shared_home, [("alice-ldap", "alice")])
    config_path = shared_home / "skill-distribution.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump({"skills": []}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    package = _build_skill_zip("daily-breaking")

    all_result = process_event(
        _normalize_event(_event_payload(auth_type="all", users=[])),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )
    subset_result = process_event(
        _normalize_event(_event_payload(auth_type="auth", users=["alice-ldap"])),
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )

    assert all_result["mode"] == "all"
    assert subset_result["users"]["alice-ldap"] == {"status": "installed", "profile": "alice"}
    assert subset_result["distribution"]["entry_removed"] is True
    assert subset_result["distribution"]["source"] == "removed"
    merged = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert not [
        item
        for item in merged["skills"]
        if isinstance(item, dict)
        and item.get("path") == "daily-breaking"
        and item.get("origin") == "aidock-skillhub"
    ]
    assert not (shared_home / "skills" / "daily-breaking").exists()
    assert (profiles_root / "alice" / "skills" / "daily-breaking").resolve() == (
        shared_home / "_managed" / "aidock-skillhub" / "daily-breaking" / "1.0.0"
    ).resolve()


def test_all_audience_skips_foreign_non_symlink_source_but_still_writes_distribution_entry(tmp_path: Path) -> None:
    from hermes_multitenancy.skillhub_installer import process_event

    shared_home = tmp_path / ".hermes"
    profiles_root = _make_profile_dirs(shared_home)
    config_path = shared_home / "skill-distribution.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump({"skills": []}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    foreign_path = shared_home / "skills" / "daily-breaking"
    foreign_path.mkdir(parents=True, exist_ok=True)
    marker = foreign_path / "foreign.txt"
    marker.write_text("keep me", encoding="utf-8")

    package = _build_skill_zip("daily-breaking")
    event = _normalize_event(
        _event_payload(
            auth_type="all",
            users=[],
            checksum_sha256=hashlib.sha256(package).hexdigest(),
        )
    )

    result = process_event(
        event,
        shared_home=shared_home,
        profiles_root=profiles_root,
        downloader=lambda _: package,
    )

    assert not foreign_path.is_symlink()
    assert marker.read_text(encoding="utf-8") == "keep me"
    merged = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert {
        "path": "daily-breaking",
        "audience": "all",
        "install_mode": "symlink",
        "origin": "aidock-skillhub",
    } in merged["skills"]
    assert result["distribution"]["source"] == "skipped-foreign"
