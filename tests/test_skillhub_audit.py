from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_multitenancy.analytics.report import build_skillhub_audit, render_skillhub_markdown


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
