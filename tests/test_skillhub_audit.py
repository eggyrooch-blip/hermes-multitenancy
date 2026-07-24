from __future__ import annotations

import sqlite3
import time
import json
from pathlib import Path

import pytest

from hermes_multitenancy.analytics.report import build_skillhub_audit, render_skillhub_markdown


def _full_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE skillhub_events (event_id TEXT PRIMARY KEY, event_type TEXT,"
        " skill_code TEXT, release_id TEXT, version TEXT, status TEXT,"
        " received_at INTEGER, updated_at INTEGER, raw_payload TEXT, results_json TEXT)"
    )


def _insert_full(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    profile: str,
    status: str,
    at: int,
    version: str = "1.0.0",
    release_id: str | None = "193",
    desired_state: str = "active",
    batched_events: int | None = None,
    employee_id: str | None = None,
) -> None:
    user = {"profile_id": profile}
    if employee_id:
        user["employee_id"] = employee_id
    payload = {
        "event_id": event_id,
        "event_type": "skill.permission_approved",
        "item_type": "plugin",
        "skill_code": "keep-sharetemplate",
        "release_id": release_id,
        "version": version,
        "skill_status": desired_state,
        "audience": {"auth_type": "auth", "users": [user]},
    }
    result = {"error_code": "PLUGIN_GOVERNANCE_FAILED", "message": f"failed for {profile}"}
    if batched_events is not None:
        result["batched_events"] = batched_events
    conn.execute(
        "INSERT INTO skillhub_events VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            payload["event_type"],
            payload["skill_code"],
            release_id,
            version,
            status,
            at,
            at,
            json.dumps(payload),
            json.dumps(result),
        ),
    )


def _seed(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE skillhub_events (event_id TEXT PRIMARY KEY, skill_code TEXT, status TEXT,"
        " received_at INTEGER, raw_payload TEXT, results_json TEXT)"
    )
    now = int(time.time())
    rows = [
        ("e1", "sk-a", "installed", now, '{"item_type":"skill"}', '{"action":"install"}'),
        ("e2", "sk-b", "installed", now, '{"item_type":"skill"}', '{"action":"install"}'),
        ("e3", "pl-a", "installed", now, '{"item_type":"plugin"}', '{"action":"plugin_install"}'),
        ("e4", "keep-calorie", "failed", now, '{"item_type":"plugin"}',
         '{"error_code":"PACKAGE_INVALID","message":"package missing plugin manifest"}'),
        ("e5", "sk-c", "queued", now, '{"item_type":"skill"}', None),
        ("e6", "sk-d", "queued_unknown_type", now - 10 * 86400, '{"item_type":"skill"}', None),  # old
        # status='installed' but a benign no-op action -> must count as skipped, not processed
        ("e7", "sk-e", "installed", now, '{"item_type":"skill"}', '{"action":"skipped_pending"}'),
    ]
    conn.executemany("INSERT INTO skillhub_events VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_build_skillhub_audit_counts_failures_and_distribution(tmp_path: Path) -> None:
    db = tmp_path / "multitenancy.db"
    _seed(db)
    a = build_skillhub_audit(db, days=7)

    assert a["all_time"] == {
        "received": 7, "processed": 3, "failed": 1, "queued": 1, "queued_unknown": 1, "skipped": 1,
    }
    assert a["last_n_days"]["received"] == 6   # e6 is 10 days old -> outside window
    assert a["last_n_days"]["queued"] == 1
    assert a["last_n_days"]["queued_unknown"] == 0
    assert a["failures"]["by_error_code"] == {"PACKAGE_INVALID": 1}
    sample = a["failures"]["samples"]["PACKAGE_INVALID"][0]
    assert sample["skill_code"] == "keep-calorie"
    assert sample["message"] == "package missing plugin manifest"
    assert a["by_item_type"] == {"skill": 5, "plugin": 2}

    md = render_skillhub_markdown(a)
    assert "PACKAGE_INVALID" in md and "package missing plugin manifest" in md
    assert "收到 7" in md and "成功 3" in md and "无操作 1" in md


def test_build_skillhub_audit_all_flag_skips_window(tmp_path: Path) -> None:
    db = tmp_path / "multitenancy.db"
    _seed(db)
    a = build_skillhub_audit(db, all_time_only=True)
    assert "last_n_days" not in a
    assert a["all_time"]["received"] == 7


def test_render_escapes_pipes_and_newlines(tmp_path: Path) -> None:
    db = tmp_path / "multitenancy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE skillhub_events (event_id TEXT PRIMARY KEY, skill_code TEXT, status TEXT,"
        " received_at INTEGER, raw_payload TEXT, results_json TEXT)"
    )
    conn.execute(
        "INSERT INTO skillhub_events VALUES (?,?,?,?,?,?)",
        ("d1", "dirty", "failed", int(time.time()), '{"item_type":"skill"}',
         '{"error_code":"BAD","message":"a | b\\nc"}'),
    )
    conn.commit(); conn.close()
    md = render_skillhub_markdown(build_skillhub_audit(db))
    # the dirty message's pipe/newline must not break the table row (one line, no raw pipe from msg)
    table_lines = [ln for ln in md.splitlines() if ln.startswith("| BAD")]
    assert len(table_lines) == 1
    assert "\n" not in table_lines[0] and "a / b c" in table_lines[0]


def test_item_type_honors_nested_skill_shape(tmp_path: Path) -> None:
    # writer's normalize_event reads item_type from top-level OR nested skill.item_type.
    db = tmp_path / "multitenancy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE skillhub_events (event_id TEXT PRIMARY KEY, skill_code TEXT, status TEXT,"
        " received_at INTEGER, raw_payload TEXT, results_json TEXT)"
    )
    now = int(time.time())
    conn.executemany(
        "INSERT INTO skillhub_events VALUES (?,?,?,?,?,?)",
        [
            ("n1", "pl-x", "installed", now, '{"skill":{"item_type":"plugin"}}', '{"action":"plugin_install"}'),
            ("n2", "sk-y", "installed", now, '{"item_type":"skill"}', '{"action":"install"}'),
        ],
    )
    conn.commit(); conn.close()
    a = build_skillhub_audit(db)
    assert a["by_item_type"] == {"plugin": 1, "skill": 1}  # nested plugin NOT miscounted as skill


def test_item_type_normalizes_case_and_garbage_like_writer(tmp_path: Path) -> None:
    # normalize_event maps only lower-case "plugin" -> plugin, everything else -> skill.
    db = tmp_path / "multitenancy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE skillhub_events (event_id TEXT PRIMARY KEY, skill_code TEXT, status TEXT,"
        " received_at INTEGER, raw_payload TEXT, results_json TEXT)"
    )
    now = int(time.time())
    conn.executemany(
        "INSERT INTO skillhub_events VALUES (?,?,?,?,?,?)",
        [
            ("g1", "a", "installed", now, '{"item_type":"Plugin"}', '{"action":"install"}'),   # .lower()==plugin -> plugin
            ("g2", "b", "installed", now, '{"item_type":"weird"}', '{"action":"install"}'),     # garbage -> skill
            ("g3", "c", "installed", now, '{"item_type":"plugin"}', '{"action":"plugin_install"}'),  # -> plugin
        ],
    )
    conn.commit(); conn.close()
    a = build_skillhub_audit(db)
    # normalize_event uses `.lower()=="plugin"` (case-insensitive) -> "Plugin" & "plugin" both plugin; "weird" -> skill
    assert a["by_item_type"] == {"plugin": 2, "skill": 1}


def test_build_skillhub_audit_missing_db_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build_skillhub_audit(tmp_path / "nope.db")


def test_build_skillhub_audit_missing_table_raises(tmp_path: Path) -> None:
    db = tmp_path / "multitenancy.db"
    sqlite3.connect(db).close()  # empty DB, no skillhub_events table
    with pytest.raises(ValueError):
        build_skillhub_audit(db)


def test_build_skillhub_audit_path_with_question_mark(tmp_path: Path) -> None:
    # a literal '?' in the path must not be parsed as URI query (as_uri encodes it)
    db = tmp_path / "weird?name.db"
    _seed(db)
    a = build_skillhub_audit(db)
    assert a["all_time"]["received"] == 7


def test_build_skillhub_audit_bad_schema_raises_valueerror(tmp_path: Path) -> None:
    # table present but wrong shape (missing columns) -> friendly ValueError, not a raw crash
    db = tmp_path / "multitenancy.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE skillhub_events (event_id TEXT, oops TEXT)")  # missing real columns
    conn.commit(); conn.close()
    with pytest.raises(ValueError):
        build_skillhub_audit(db)


def test_293_row_release_fanout_is_one_failure_without_losing_raw_rows(tmp_path: Path) -> None:
    db = tmp_path / "multitenancy.db"
    conn = sqlite3.connect(db)
    _full_schema(conn)
    now = int(time.time())
    for index in range(293):
        _insert_full(
            conn,
            event_id=f"evt-{index:03d}",
            profile=f"profile-{index:03d}",
            status="failed",
            at=now,
            batched_events=293,
        )
    conn.commit()
    conn.close()

    audit = build_skillhub_audit(db)

    assert audit["all_time"]["failed"] == 293  # legacy field remains raw
    assert audit["raw_events"]["failed"] == 293
    assert audit["fanouts"]["total"] == 1
    assert audit["fanouts"]["failed"] == 1
    assert audit["fanouts"]["affected_targets"] == 293
    assert audit["fanouts"]["duplicate_aggregate_failures"] == 0
    assert audit["fanouts"]["collapsed_raw_failures"] == 292
    assert audit["target_final_states"]["total"] == 293
    assert audit["target_final_states"]["failed"] == 293


def test_target_final_state_uses_latest_status_and_keeps_version_and_desired_state(tmp_path: Path) -> None:
    db = tmp_path / "multitenancy.db"
    conn = sqlite3.connect(db)
    _full_schema(conn)
    now = int(time.time())
    _insert_full(conn, event_id="failed", profile="p-one", status="failed", at=now)
    _insert_full(conn, event_id="fixed", profile="p-one", status="installed", at=now + 1)
    _insert_full(
        conn, event_id="v2", profile="p-one", status="installed", at=now + 2, version="2.0.0"
    )
    _insert_full(
        conn,
        event_id="inactive",
        profile="p-one",
        status="installed",
        at=now + 3,
        version="2.0.0",
        desired_state="inactive",
    )
    conn.commit()
    conn.close()

    audit = build_skillhub_audit(db)

    assert audit["raw_events"]["received"] == 4
    assert audit["target_final_states"]["total"] == 3
    assert audit["target_final_states"]["completed"] == 3
    assert audit["target_final_states"]["failed"] == 0
    assert {(item["version"], item["desired_state"]) for item in audit["target_final_states"]["items"]} == {
        ("1.0.0", "active"),
        ("2.0.0", "active"),
        ("2.0.0", "inactive"),
    }


def test_inferred_fanout_window_is_inclusive_at_ten_minutes(tmp_path: Path) -> None:
    db = tmp_path / "multitenancy.db"
    conn = sqlite3.connect(db)
    _full_schema(conn)
    now = int(time.time())
    for event_id, offset in (("a", 0), ("b", 600), ("c", 1201)):
        _insert_full(
            conn,
            event_id=event_id,
            profile=f"profile-{event_id}",
            status="failed",
            at=now + offset,
            release_id=None,
            batched_events=3,
        )
    conn.commit()
    conn.close()

    fanouts = build_skillhub_audit(db)["fanouts"]

    assert fanouts["total"] == 2
    assert fanouts["failed"] == 2
    assert fanouts["inferred"] == 2


def test_report_anonymizes_skillhub_target_identifiers(tmp_path: Path) -> None:
    db = tmp_path / "multitenancy.db"
    conn = sqlite3.connect(db)
    _full_schema(conn)
    _insert_full(
        conn,
        event_id="private",
        profile="ou_sensitive_profile",
        employee_id="employee-12345",
        status="failed",
        at=int(time.time()),
    )
    conn.commit()
    conn.close()

    audit = build_skillhub_audit(db)
    rendered = json.dumps(audit, ensure_ascii=False) + render_skillhub_markdown(audit)

    assert "ou_sensitive_profile" not in rendered
    assert "employee-12345" not in rendered
    assert audit["target_final_states"]["items"][0]["anonymous_profile"].startswith("profile_")


def test_legacy_schema_marks_single_event_fallback_and_markdown_layers(tmp_path: Path) -> None:
    db = tmp_path / "multitenancy.db"
    _seed(db)

    audit = build_skillhub_audit(db)
    markdown = render_skillhub_markdown(audit)

    assert audit["fanouts"]["single_event"] == 7
    assert "原始事件" in markdown
    assert "批量结果" in markdown
