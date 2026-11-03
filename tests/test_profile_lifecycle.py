from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def _db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE multitenancy_routing (
                profile_name TEXT, upstream_profile TEXT, active INTEGER
            );
            CREATE TABLE multitenancy_channel_bindings (
                profile_name TEXT, active INTEGER
            );
            CREATE TABLE multitenancy_sessions (profile_name TEXT);
            """
        )


def test_quarantine_only_moves_profiles_with_no_live_reference(tmp_path):
    from hermes_multitenancy.profile_lifecycle import quarantine_orphan_profiles

    profiles = tmp_path / "profiles"
    for name in ("routed", "bound", "session", "cron", "kept", "orphan"):
        (profiles / name).mkdir(parents=True)
        (profiles / name / "config.yaml").write_text("model: {}\n")
    (profiles / "kept" / ".keep").write_text("operator-owned\n")

    _db(tmp_path / "multitenancy.db")
    with sqlite3.connect(tmp_path / "multitenancy.db") as conn:
        conn.execute("INSERT INTO multitenancy_routing VALUES ('routed', NULL, 1)")
        conn.execute("INSERT INTO multitenancy_channel_bindings VALUES ('bound', 1)")
        conn.execute("INSERT INTO multitenancy_sessions VALUES ('session')")
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"profile_name": "cron"}]})
    )

    dry = quarantine_orphan_profiles(tmp_path, apply=False)
    assert dry.candidates == ["orphan"]
    assert (profiles / "orphan").is_dir()

    applied = quarantine_orphan_profiles(tmp_path, apply=True, services_stopped=True)
    assert applied.quarantined == ["orphan"]
    assert not (profiles / "orphan").exists()
    moved = tmp_path / "profile-quarantine" / applied.run_id / "orphan"
    assert moved.is_dir()
    manifest = json.loads(
        (tmp_path / "profile-quarantine" / applied.run_id / "manifest.json").read_text()
    )
    assert manifest["quarantined"] == ["orphan"]

    rerun = quarantine_orphan_profiles(tmp_path, apply=True, services_stopped=True)
    assert rerun.candidates == []
    assert rerun.quarantined == []


def test_stale_gateway_pid_is_not_a_live_reference(tmp_path):
    from hermes_multitenancy.profile_lifecycle import quarantine_orphan_profiles

    profile = tmp_path / "profiles" / "stale"
    profile.mkdir(parents=True)
    (profile / "gateway.pid").write_text("999999999\n")

    _db(tmp_path / "multitenancy.db")
    report = quarantine_orphan_profiles(tmp_path, apply=False)

    assert report.candidates == ["stale"]


def test_json_gateway_pid_and_profile_local_cron_are_live_references(tmp_path, monkeypatch):
    from hermes_multitenancy import profile_lifecycle

    profiles = tmp_path / "profiles"
    live = profiles / "live"
    scheduled = profiles / "scheduled"
    live.mkdir(parents=True)
    (scheduled / "cron").mkdir(parents=True)
    (live / "gateway.pid").write_text(json.dumps({"pid": 4242}))
    (scheduled / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{}]}))
    _db(tmp_path / "multitenancy.db")
    monkeypatch.setattr(profile_lifecycle.os, "kill", lambda pid, signal: None if pid == 4242 else None)

    report = profile_lifecycle.quarantine_orphan_profiles(tmp_path)

    assert report.candidates == []
    assert report.referenced == ["live", "scheduled"]


def test_missing_routing_database_fails_closed(tmp_path):
    from hermes_multitenancy.profile_lifecycle import quarantine_orphan_profiles

    (tmp_path / "profiles" / "only-profile").mkdir(parents=True)

    report = quarantine_orphan_profiles(tmp_path, apply=True, services_stopped=True)

    assert report.candidates == []
    assert report.quarantined == []
    assert (tmp_path / "profiles" / "only-profile").is_dir()


def _one_profile(tmp_path: Path, name: str = "p") -> Path:
    profile = tmp_path / "profiles" / name
    profile.mkdir(parents=True)
    return profile


def test_pid_owned_by_another_account_counts_as_live(tmp_path, monkeypatch):
    from hermes_multitenancy import profile_lifecycle

    (_one_profile(tmp_path) / "gateway.pid").write_text("4242\n")
    _db(tmp_path / "multitenancy.db")

    def _eperm(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(profile_lifecycle.os, "kill", _eperm)
    report = profile_lifecycle.quarantine_orphan_profiles(tmp_path)

    assert report.candidates == []
    assert report.referenced == ["p"]


def test_unparseable_pid_file_fails_closed(tmp_path):
    from hermes_multitenancy.profile_lifecycle import quarantine_orphan_profiles

    _one_profile(tmp_path, "garbled")
    (tmp_path / "profiles" / "garbled" / "gateway.pid").write_text("not-a-pid")
    _one_profile(tmp_path, "orphan")
    _db(tmp_path / "multitenancy.db")

    report = quarantine_orphan_profiles(tmp_path, apply=True, services_stopped=True)

    assert report.candidates == []
    assert (tmp_path / "profiles" / "orphan").is_dir()


def test_empty_database_fails_closed(tmp_path):
    from hermes_multitenancy.profile_lifecycle import quarantine_orphan_profiles

    _one_profile(tmp_path)
    sqlite3.connect(tmp_path / "multitenancy.db").close()

    report = quarantine_orphan_profiles(tmp_path, apply=True, services_stopped=True)

    assert report.candidates == []
    assert (tmp_path / "profiles" / "p").is_dir()


def test_partial_schema_fails_closed(tmp_path):
    from hermes_multitenancy.profile_lifecycle import quarantine_orphan_profiles

    _one_profile(tmp_path)
    with sqlite3.connect(tmp_path / "multitenancy.db") as conn:
        conn.execute(
            "CREATE TABLE multitenancy_routing (profile_name TEXT, upstream_profile TEXT, active INTEGER)"
        )

    assert quarantine_orphan_profiles(tmp_path).candidates == []


def test_apply_refuses_without_services_stopped(tmp_path):
    import pytest

    from hermes_multitenancy.profile_lifecycle import quarantine_orphan_profiles

    _one_profile(tmp_path)
    _db(tmp_path / "multitenancy.db")

    with pytest.raises(RuntimeError):
        quarantine_orphan_profiles(tmp_path, apply=True)
    assert (tmp_path / "profiles" / "p").is_dir()


def test_apply_refuses_while_shared_gateway_alive(tmp_path, monkeypatch):
    import pytest

    from hermes_multitenancy import profile_lifecycle

    _one_profile(tmp_path)
    _db(tmp_path / "multitenancy.db")
    (tmp_path / "gateway.pid").write_text("4242\n")
    monkeypatch.setattr(profile_lifecycle.os, "kill", lambda pid, sig: None)

    with pytest.raises(RuntimeError):
        profile_lifecycle.quarantine_orphan_profiles(tmp_path, apply=True, services_stopped=True)
    assert (tmp_path / "profiles" / "p").is_dir()


def test_reference_added_after_plan_is_rechecked_before_move(tmp_path, monkeypatch):
    from hermes_multitenancy import profile_lifecycle

    _one_profile(tmp_path, "revived")
    _one_profile(tmp_path, "orphan")
    _db(tmp_path / "multitenancy.db")

    shared = tmp_path.resolve()
    report, _ = profile_lifecycle._plan(shared, None)
    assert report.candidates == ["orphan", "revived"]
    with sqlite3.connect(tmp_path / "multitenancy.db") as conn:
        conn.execute("INSERT INTO multitenancy_routing VALUES ('revived', NULL, 1)")

    applied = profile_lifecycle._apply(shared, report, services_stopped=True)

    assert applied.quarantined == ["orphan"]
    assert (tmp_path / "profiles" / "revived").is_dir()


def test_core_tombstones_and_non_profile_dirs_are_never_candidates(tmp_path):
    from hermes_multitenancy.profile_lifecycle import quarantine_orphan_profiles

    profiles = tmp_path / "profiles"
    (profiles / ".deleted" / "gone").mkdir(parents=True)
    (profiles / "Not_A_Profile").mkdir()
    (profiles / "orphan").mkdir()
    (profiles / "orphan" / "config.yaml").write_text("model: {}\n")
    _db(tmp_path / "multitenancy.db")

    applied = quarantine_orphan_profiles(tmp_path, apply=True, services_stopped=True)
    assert applied.candidates == ["orphan"]
    assert (profiles / ".deleted" / "gone").is_dir()
    assert (profiles / "Not_A_Profile").is_dir()


def test_active_profile_is_a_reference(tmp_path):
    from hermes_multitenancy.profile_lifecycle import quarantine_orphan_profiles

    profiles = tmp_path / "profiles"
    for name in ("sticky", "orphan"):
        (profiles / name).mkdir(parents=True)
        (profiles / name / "config.yaml").write_text("model: {}\n")
    _db(tmp_path / "multitenancy.db")
    (tmp_path / "active_profile").write_text("sticky\n")

    assert quarantine_orphan_profiles(tmp_path).candidates == ["orphan"]


def test_cron_bot_chat_receiver_is_a_reference(tmp_path):
    from hermes_multitenancy.profile_lifecycle import quarantine_orphan_profiles

    profiles = tmp_path / "profiles"
    for name in ("sender", "receiver", "fallback", "orphan"):
        (profiles / name).mkdir(parents=True)
        (profiles / name / "config.yaml").write_text("model: {}\n")
    _db(tmp_path / "multitenancy.db")
    with sqlite3.connect(tmp_path / "multitenancy.db") as conn:
        conn.execute("INSERT INTO multitenancy_routing VALUES ('sender', NULL, 1)")
    (profiles / "sender" / "cron").mkdir()
    (profiles / "sender" / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"deliver": "feishu, bot-chat:Receiver", "failure_deliver": ["bot-chat:fallback"]}]})
    )

    assert quarantine_orphan_profiles(tmp_path).candidates == ["orphan"]


def test_router_profile_and_mixed_case_active_profile_are_references(tmp_path, monkeypatch):
    from hermes_multitenancy.gateway_ownership import DEFAULT_ROUTER_PROFILE
    from hermes_multitenancy.profile_lifecycle import quarantine_orphan_profiles

    monkeypatch.setenv("HERMES_MULTITENANCY_ROUTER_PROFILE", "custom-router")
    profiles = tmp_path / "profiles"
    for name in (DEFAULT_ROUTER_PROFILE, "custom-router", "work", "orphan"):
        (profiles / name).mkdir(parents=True)
        (profiles / name / "config.yaml").write_text("model: {}\n")
    _db(tmp_path / "multitenancy.db")
    (tmp_path / "active_profile").write_text("Work\n")

    applied = quarantine_orphan_profiles(tmp_path, apply=True, services_stopped=True)
    assert applied.candidates == ["orphan"]
    assert (profiles / DEFAULT_ROUTER_PROFILE).is_dir()
