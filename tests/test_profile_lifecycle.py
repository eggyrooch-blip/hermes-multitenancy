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

    applied = quarantine_orphan_profiles(tmp_path, apply=True)
    assert applied.quarantined == ["orphan"]
    assert not (profiles / "orphan").exists()
    moved = tmp_path / "profile-quarantine" / applied.run_id / "orphan"
    assert moved.is_dir()
    manifest = json.loads(
        (tmp_path / "profile-quarantine" / applied.run_id / "manifest.json").read_text()
    )
    assert manifest["quarantined"] == ["orphan"]

    rerun = quarantine_orphan_profiles(tmp_path, apply=True)
    assert rerun.candidates == []
    assert rerun.quarantined == []


def test_stale_gateway_pid_is_not_a_live_reference(tmp_path):
    from hermes_multitenancy.profile_lifecycle import quarantine_orphan_profiles

    profile = tmp_path / "profiles" / "stale"
    profile.mkdir(parents=True)
    (profile / "gateway.pid").write_text("999999999\n")

    report = quarantine_orphan_profiles(tmp_path, apply=False)

    assert report.candidates == ["stale"]
