"""scripts/prod_fts5_rebuild.py on synthetic state.db copies (no production data).

Schema = minimal subset of core's v23+/v30 external-content trigram layout (view + fts5
content='messages_fts_trigram_src' tokenize='trigram' + insert trigger) plus a base messages_fts
index that the script must never touch.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prod_fts5_rebuild.py"
_spec = importlib.util.spec_from_file_location("prod_fts5_rebuild", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)

MALFORMED = "malformed inverted index for FTS5 table main.messages_fts_trigram"

DDL = """
CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
INSERT INTO state_meta VALUES ('fts_storage_version', '3');
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, model_config TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id), role TEXT NOT NULL,
    content TEXT, tool_name TEXT, tool_calls TEXT);
CREATE VIRTUAL TABLE messages_fts USING fts5(content);
CREATE VIEW messages_fts_trigram_src AS
    SELECT m.id, m.role, m.content, m.tool_name FROM messages AS m
    JOIN sessions AS s ON s.id = m.session_id
    WHERE m.role <> 'tool' AND s.source NOT IN ('cron', 'subagent')
      AND json_extract(CASE WHEN json_valid(s.model_config) THEN s.model_config
                       ELSE json_object() END, '$._delegate_from') IS NULL;
CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(content, tool_name,
    content='messages_fts_trigram_src', content_rowid='id', tokenize='trigram');
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content); END;
CREATE TRIGGER messages_fts_trigram_insert AFTER INSERT ON messages
WHEN new.role <> 'tool' AND EXISTS (SELECT 1 FROM sessions WHERE id = new.session_id
     AND source NOT IN ('cron', 'subagent'))
BEGIN INSERT INTO messages_fts_trigram(rowid, content, tool_name)
      VALUES (new.id, new.content, new.tool_name); END;
"""

WORDS = ["中文搜索", "会话历史", "数据库索引", "飞书消息", "员工报销", "季度预算", "机械硬盘"]


def make_db(path: Path, kind: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    conn.execute("INSERT INTO sessions VALUES ('s1', 'feishu', NULL)")
    conn.execute("INSERT INTO sessions VALUES ('s2', 'cron', NULL)")
    for i in range(400):
        sid = "s2" if i % 10 == 0 else "s1"
        role = "tool" if i % 7 == 0 else "user"
        conn.execute("INSERT INTO messages(session_id, role, content) VALUES (?, ?, ?)",
                     (sid, role, f"第{i}条 {WORDS[i % len(WORDS)]}测试 {WORDS[(i * 3) % len(WORDS)]} {i * 7}"))
    conn.commit()
    if kind == "structural":  # drop every segment leaf: reproduces the production error text
        conn.execute("DELETE FROM messages_fts_trigram_data WHERE id > 10")
    elif kind == "drift":  # index row with no content row: only integrity-check (rank=1) sees it
        conn.execute("INSERT INTO messages_fts_trigram(rowid, content, tool_name) VALUES (99999, '幽灵内容', NULL)")
    conn.commit()
    conn.close()
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def quick(path: Path) -> list[str]:
    conn = sqlite3.connect(mod._uri(path, "ro"), uri=True)
    try:
        return [r[0] for r in conn.execute("PRAGMA quick_check")]
    finally:
        conn.close()


def integrity(path: Path) -> str:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        return mod.health(conn, in_txn=False)["integrity"]
    finally:
        conn.close()


def fp(path: Path) -> dict:
    conn = sqlite3.connect(mod._uri(path, "ro"), uri=True)
    try:
        return mod.fingerprint(conn)
    finally:
        conn.close()


def base_index(path: Path) -> str:
    conn = sqlite3.connect(mod._uri(path, "ro"), uri=True)
    try:
        return hashlib.sha256(repr(conn.execute(
            "SELECT id, block FROM messages_fts_data ORDER BY id").fetchall()).encode()).hexdigest()
    finally:
        conn.close()


@contextlib.contextmanager
def flock_admission(db_path, *, timeout_seconds=None):
    """Same contract/lock path as core hermes_state_common.fts_rebuild_admission (non-blocking)."""
    with open(f"{db_path}.fts_rebuild.lock", "a+b") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class FakeIO:
    def __init__(self, values, step=5.0):
        self.values, self.step, self.t, self.calls = list(values), step, 0.0, 0

    def clock(self):
        return self.t

    def sample(self):
        self.t += self.step
        self.calls += 1
        v = self.values.pop(0) if self.values else 10.0
        return {"write_ms": v, "writes": 1, "sync": "inactive"}


@pytest.fixture
def env(tmp_path):
    profiles = tmp_path / "profiles"
    dbs = {k: make_db(profiles / k / "state.db", k) for k in ("structural", "drift", "healthy")}
    lst = tmp_path / "A13-fts-baseline.txt"
    lines = ["# A13 baseline test python=x sqlite=y total=3 bad=2"]
    lines += [json.dumps({"path": str(p), "quick_check": [MALFORMED], "error": None}) for p in dbs.values()]
    lst.write_text("\n".join(lines) + "\n")
    return tmp_path, dbs, lst


def run(tmp_path, lst, *extra, io=None, **hooks):
    io = io or FakeIO([])
    deps = mod.Deps(admission=hooks.pop("admission", flock_admission), sampler=io.sample, clock=io.clock, **hooks)
    snap = tmp_path / "snap"
    ledger = tmp_path / "ledger.jsonl"
    code = mod.main(["--list", str(lst), "--snapshot-dir", str(snap), "--ledger", str(ledger), *extra], deps=deps)
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    return code, {r["db"]: r for r in rows if "db" in r and "status" in r}, rows, snap


def test_fixture_reproduces_both_corruptions(env):
    _, dbs, _ = env
    assert quick(dbs["structural"]) == [MALFORMED]
    assert quick(dbs["drift"]) == ["ok"]  # quick_check is blind to content drift
    assert integrity(dbs["drift"]) != "ok"
    assert quick(dbs["healthy"]) == ["ok"] and integrity(dbs["healthy"]) == "ok"


def test_dry_run_snapshots_measures_and_never_writes_original(env):
    tmp_path, dbs, lst = env
    before = {k: (sha(p), p.stat().st_mtime_ns) for k, p in dbs.items()}
    code, res, rows, snap = run(tmp_path, lst)  # dry-run is the default
    assert code == 0
    assert {k: (sha(p), p.stat().st_mtime_ns) for k, p in dbs.items()} == before
    st = {k: res[str(p)] for k, p in dbs.items()}
    assert st["structural"]["status"] == "would_rebuild"
    assert st["drift"]["status"] == "would_rebuild"
    assert st["healthy"]["status"] == "would_skip_healthy"
    assert st["structural"]["before_quick_check"] == [MALFORMED]
    for k in ("structural", "drift"):
        assert st[k]["rebuild_seconds_on_snapshot"] >= 0
        assert st[k]["critical_section_seconds_on_snapshot"] >= st[k]["rebuild_seconds_on_snapshot"]
        assert st[k]["after"]["healthy"] is True
        probe = st[k]["search_after"]
        assert len(probe) == 5 and all(p["match"] == p["like"] for p in probe)
    # structural corruption: trigram search on the broken index errors or under-counts
    assert any("match_error" in p or p["match"] != p["like"] for p in st["structural"]["search_before"])
    snaps = sorted(snap.glob("*.snapshot.db"))
    assert len(snaps) == 3
    for k, p in dbs.items():  # snapshot stays a faithful copy (dry-run rebuild was rolled back)
        s = Path(st[k]["snapshot"]["path"])
        assert sha(s) == st[k]["snapshot"]["sha256"]
        assert fp(s) == fp(p)
    assert quick(Path(st["structural"]["snapshot"]["path"])) == [MALFORMED]
    run_row = rows[0]
    assert run_row["kind"] == "run" and run_row["mode"] == "dry_run" and run_row["list_headers"]


def test_apply_repairs_both_then_rerun_is_skipped_healthy_zero_writes(env):
    tmp_path, dbs, lst = env
    fps = {k: fp(p) for k, p in dbs.items()}
    bases = {k: base_index(p) for k, p in dbs.items()}
    healthy_sha = sha(dbs["healthy"])
    code, res, _, snap = run(tmp_path, lst, "--apply")
    assert code == 0
    st = {k: res[str(p)]["status"] for k, p in dbs.items()}
    assert st == {"structural": "rebuilt", "drift": "rebuilt", "healthy": "skipped_healthy"}
    for k, p in dbs.items():
        assert quick(p) == ["ok"]
        assert integrity(p) == "ok"
        assert fp(p) == fps[k]
        assert base_index(p) == bases[k]  # messages_fts never touched
    assert sha(dbs["healthy"]) == healthy_sha
    assert len(list(snap.glob("*.snapshot.db"))) == 2  # healthy DB needs no snapshot

    shas = {k: sha(p) for k, p in dbs.items()}
    code, res, _, snap = run(tmp_path, lst, "--apply")
    assert code == 0
    assert {res[str(p)]["status"] for p in dbs.values()} == {"skipped_healthy"}
    assert {k: sha(p) for k, p in dbs.items()} == shas
    assert len(list(snap.glob("*.snapshot.db"))) == 2


def test_io_pause_then_resume_after_three_quiet_samples(env):
    tmp_path, dbs, lst = env
    io = FakeIO([150, 150, 50, 150, 50, 50, 50])
    code, res, rows, _ = run(tmp_path, lst, "--apply", io=io)
    assert code == 0
    kinds = [r.get("kind") for r in rows]
    assert kinds.count("io_pause") == 1 and kinds.count("io_resume") == 1
    resume = next(r for r in rows if r.get("kind") == "io_resume")
    assert resume["paused_seconds"] == 30.0  # 6 samples after the busy one
    assert res[str(dbs["structural"])]["status"] == "rebuilt"


def test_io_pause_cap_aborts_with_exit_3_and_no_writes(env):
    tmp_path, dbs, lst = env
    before = {k: sha(p) for k, p in dbs.items()}
    io = FakeIO([150] * 100)
    code, res, _, snap = run(tmp_path, lst, "--apply", "--pause-cap-seconds", "20", io=io)
    assert code == 3
    assert res[str(dbs["structural"])]["status"] == "aborted_io"
    assert res[str(dbs["drift"])]["status"] == "aborted_io"
    assert {k: sha(p) for k, p in dbs.items()} == before
    assert not list(snap.glob("*.snapshot.db"))


def test_lock_held_by_other_process_defers(env, tmp_path):
    _, dbs, _lst = env
    only = tmp_path / "one.txt"
    only.write_text(json.dumps({"path": str(dbs["structural"])}) + "\n")
    before = sha(dbs["structural"])
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl,sys,time;f=open(sys.argv[1],'a+b');fcntl.flock(f,fcntl.LOCK_EX);"
        "print('held',flush=True);time.sleep(60)"), f"{dbs['structural']}.fts_rebuild.lock"],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        code, res, _, _ = run(tmp_path, only, "--apply")
    finally:
        holder.kill()
        holder.wait()
    assert code == 5
    assert res[str(dbs["structural"])]["status"] == "deferred_lock"
    assert sha(dbs["structural"]) == before and quick(dbs["structural"]) == [MALFORMED]


def test_write_lock_held_by_other_connection_defers_busy(env):
    tmp_path, dbs, lst = env
    before = {k: sha(p) for k, p in dbs.items()}
    holders = []
    for k in ("structural", "drift"):
        c = sqlite3.connect(dbs[k], isolation_level=None)
        c.execute("BEGIN IMMEDIATE")
        holders.append(c)
    try:
        code, res, _, _ = run(tmp_path, lst, "--apply", "--busy-timeout-ms", "100")
    finally:
        for c in holders:
            c.execute("ROLLBACK")
            c.close()
    assert code == 5
    assert res[str(dbs["structural"])]["status"] == "deferred_busy"
    assert res[str(dbs["drift"])]["status"] == "deferred_busy"
    assert {k: sha(p) for k, p in dbs.items()} == before


def test_bad_snapshot_leaves_original_untouched_and_stops(env):
    tmp_path, dbs, lst = env
    before = {k: sha(p) for k, p in dbs.items()}
    code, res, _, _ = run(tmp_path, lst, "--apply", snapshot_quick_check=lambda c: ["page 3 is never used"])
    assert code == 4
    assert res[str(dbs["structural"])]["status"] == "failed_snapshot"
    assert res[str(dbs["drift"])]["status"] == "not_started"
    assert {k: sha(p) for k, p in dbs.items()} == before


def test_in_transaction_mismatch_rolls_back_and_stops(env):
    tmp_path, dbs, lst = env
    before = {k: sha(p) for k, p in dbs.items()}
    code, res, _, _ = run(tmp_path, lst, "--apply", in_txn_verify=lambda b, a: "injected hash mismatch")
    assert code == 4
    assert res[str(dbs["structural"])]["status"] == "failed_verify"
    assert res[str(dbs["drift"])]["status"] == "not_started"
    assert {k: sha(p) for k, p in dbs.items()} == before
    assert quick(dbs["structural"]) == [MALFORMED]


def _user_writes(db):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO messages(session_id, role, content) VALUES ('s1', 'user', '提交后新消息')")
    conn.execute("UPDATE messages SET tool_calls = '[{\"x\": 1}]' WHERE id = 2")
    conn.commit()
    conn.close()


def test_post_commit_failure_is_never_auto_restored_online(env):
    tmp_path, dbs, lst = env
    rows_before = fp(dbs["structural"])["messages"]["rows"]
    code, res, _, _ = run(tmp_path, lst, "--apply", after_commit=_user_writes,
                          post_commit_verify=lambda p: "injected post-commit failure")
    assert code == 4
    row = res[str(dbs["structural"])]
    assert row["status"] == "failed_needs_manual" and row["committed"] is True
    assert "restore" not in row
    assert res[str(dbs["drift"])]["status"] == "not_started"
    conn = sqlite3.connect(dbs["structural"])
    try:  # concurrent user writes (incl. a column outside the old content hash) survive
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == rows_before + 1
        assert conn.execute("SELECT tool_calls FROM messages WHERE id = 2").fetchone()[0] == '[{"x": 1}]'
    finally:
        conn.close()
    assert quick(dbs["structural"]) == ["ok"]  # the rebuild stays; nothing written back


def test_user_write_after_commit_is_not_a_failure(env):
    tmp_path, dbs, lst = env
    code, res, _, _ = run(tmp_path, lst, "--apply", after_commit=_user_writes)
    assert code == 0
    assert res[str(dbs["structural"])]["status"] == "rebuilt"
    assert res[str(dbs["structural"])]["post_commit"]["healthy"] is True
    assert quick(dbs["structural"]) == ["ok"] and integrity(dbs["structural"]) == "ok"


def test_fingerprint_covers_every_column(env):
    _, dbs, _ = env
    before = fp(dbs["healthy"])
    conn = sqlite3.connect(dbs["healthy"])
    conn.execute("UPDATE messages SET tool_calls = 'changed' WHERE id = 1")
    conn.commit()
    conn.close()
    assert fp(dbs["healthy"])["messages"] != before["messages"]


def test_apply_refused_when_core_importable_but_wrong_interpreter(env):
    tmp_path, _, lst = env
    stub = tmp_path / "stubcore"
    stub.mkdir()
    (stub / "hermes_state_common.py").write_text(
        "import contextlib\n@contextlib.contextmanager\ndef fts_rebuild_admission(p, *, timeout_seconds=None):\n    yield True\n")
    proc = subprocess.run([sys.executable, "-S", str(SCRIPT), "--list", str(lst), "--apply",
                           "--snapshot-dir", str(tmp_path / "snap")], capture_output=True, text=True,
                          check=False, env={"PYTHONPATH": str(stub), "PATH": "/usr/bin:/bin"})
    assert proc.returncode == 2, proc.stderr
    assert "sys.prefix" in proc.stderr
    assert not (tmp_path / "snap").exists()


def test_validate_interpreter_checks_prefix_module_and_sqlite(tmp_path):
    release = str(Path(sys.prefix).parent)  # a venv at <release>/.venv
    inside = str(Path(release) / "hermes_state_common.py")
    assert mod.validate_interpreter(inside, release, sqlite3.sqlite_version) is None
    assert "sqlite" in mod.validate_interpreter(inside, release, "0.0.1")
    assert "outside" in mod.validate_interpreter(str(tmp_path / "x.py"), release, sqlite3.sqlite_version)
    assert "sys.prefix" in mod.validate_interpreter(inside, str(tmp_path), sqlite3.sqlite_version)
    assert "cannot import" in mod.validate_interpreter(None, release, sqlite3.sqlite_version)


def test_apply_refused_without_core_interpreter(env):
    tmp_path, _, lst = env
    # -S -I: no site-packages, so hermes_state_common cannot be importable (script is stdlib-only)
    proc = subprocess.run([sys.executable, "-S", "-I", str(SCRIPT), "--list", str(lst), "--apply",
                           "--snapshot-dir", str(tmp_path / "snap")], capture_output=True, text=True, check=False)
    assert proc.returncode == 2
    assert "refusing --apply" in proc.stderr
    assert not (tmp_path / "snap").exists()


def test_only_listed_files_touched_and_no_delete_logic(env):
    tmp_path, _dbs, lst = env
    extra = make_db(tmp_path / "profiles" / "unlisted" / "state.db", "structural")
    extra_sha = sha(extra)
    before = {p for p in tmp_path.rglob("*") if p.is_file()}
    run(tmp_path, lst, "--apply")
    after = {p for p in tmp_path.rglob("*") if p.is_file()}
    new = {p for p in after - before if not str(p).startswith(str(tmp_path / "snap"))}
    allowed = (".fts_rebuild.lock", "-wal", "-shm", "ledger.jsonl")
    assert all(str(p).endswith(allowed) for p in new), new
    assert before - after == set()  # nothing deleted
    assert sha(extra) == extra_sha and not Path(f"{extra}.fts_rebuild.lock").exists()
    src = SCRIPT.read_text()
    for banned in ("unlink(", "rmtree", "os.remove", "rmdir(", "DROP ", "VACUUM"):
        assert banned not in src


def test_run_refused_when_io_guard_unavailable(env):
    tmp_path, _dbs, lst = env
    proc = subprocess.run([sys.executable, "-S", "-I", str(SCRIPT), "--list", str(lst), "--dry-run",
                           "--io-device", "no-such-device-xyz", "--snapshot-dir", str(tmp_path / "snap")],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 2
    assert "IO guard unavailable" in proc.stderr
    assert not (tmp_path / "snap").exists()


def test_reads_real_a13_header_format(tmp_path):
    lst = tmp_path / "a13.txt"
    lst.write_text(
        "# A13 baseline 2026-09-24T16:33:22+08:00 python=/x/.venv/bin/python sqlite=3.53.1 total=503 bad=18\n"
        + json.dumps({"path": "/p/a/state.db", "quick_check": [MALFORMED], "error": None}) + "\n"
        + json.dumps({"path": "/p/a/state.db", "quick_check": [MALFORMED], "error": None}) + "\n\n"
        + "/p/b/state.db\n")
    headers, dbs = mod.read_list(lst)
    assert len(headers) == 1 and headers[0].startswith("# A13 baseline")
    assert dbs == [Path("/p/a/state.db"), Path("/p/b/state.db")]



def test_path_with_uri_metacharacters_is_opened_safely(tmp_path):
    db = make_db(tmp_path / "p?mode=rwc#frag%41 中文" / "state.db", "structural")
    lst = tmp_path / "l.txt"
    lst.write_text(json.dumps({"path": str(db)}) + "\n")
    before = {p for p in tmp_path.rglob("*") if p.is_file()}
    code, res, _, _ = run(tmp_path, lst, "--apply")
    assert code == 0 and res[str(db)]["status"] == "rebuilt"
    assert quick(db) == ["ok"]
    new = {p for p in tmp_path.rglob("*") if p.is_file()} - before
    assert all(str(p).startswith(str(tmp_path / "snap")) or str(p).endswith((".fts_rebuild.lock", "-wal", "-shm", "ledger.jsonl"))
               for p in new), new


def test_ledger_aliasing_a_database_or_non_jsonl_is_refused(env):
    tmp_path, dbs, lst = env
    before = {k: sha(p) for k, p in dbs.items()}
    io = FakeIO([])
    deps = mod.Deps(admission=flock_admission, sampler=io.sample, clock=io.clock)
    code = mod.main(["--list", str(lst), "--snapshot-dir", str(tmp_path / "snap"), "--ledger", str(dbs["healthy"])], deps=deps)
    assert code == 2
    link = tmp_path / "ledger-link.jsonl"
    link.symlink_to(dbs["drift"])
    assert mod.main(["--list", str(lst), "--snapshot-dir", str(tmp_path / "snap"), "--ledger", str(link)], deps=deps) == 2
    assert mod.main(["--list", str(lst), "--snapshot-dir", str(tmp_path / "snap"), "--ledger", str(lst)], deps=deps) == 2
    junk = tmp_path / "notes.txt"
    junk.write_text("hello\n")
    assert mod.main(["--list", str(lst), "--snapshot-dir", str(tmp_path / "snap"), "--ledger", str(junk)], deps=deps) == 2
    assert junk.read_text() == "hello\n"
    assert {k: sha(p) for k, p in dbs.items()} == before
    assert not (tmp_path / "snap").exists()


def test_snapshot_reservation_is_exclusive_across_processes(tmp_path):
    db = tmp_path / "x" / "state.db"
    code = ("import importlib.util,sys;from pathlib import Path;"
            f"s=importlib.util.spec_from_file_location('m','{SCRIPT}');m=importlib.util.module_from_spec(s);"
            "sys.modules['m']=m;s.loader.exec_module(m);"
            "[print(m.reserve_snapshot(Path(sys.argv[1]),Path(sys.argv[2]))) for _ in range(20)]")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(db)], stdout=subprocess.PIPE, text=True)
             for _ in range(3)]
    names = [line for p in procs for line in p.communicate()[0].split()]
    assert len(names) == 60 and len(set(names)) == 60
    existing = Path(names[0])
    existing.write_bytes(b"keep")
    assert mod.reserve_snapshot(tmp_path, db) != existing and existing.read_bytes() == b"keep"


def test_backup_is_paced_between_steps_and_deadline_bounded(tmp_path):
    src_path = tmp_path / "big.db"
    conn = sqlite3.connect(src_path)
    conn.execute("CREATE TABLE t(x)")
    conn.executemany("INSERT INTO t VALUES (?)", [("x" * 2000,)] * 100)
    conn.commit()
    conn.close()
    src, dst = sqlite3.connect(src_path), sqlite3.connect(":memory:")
    t0 = time.monotonic()
    stats = mod.take_snapshot(src, dst, pages=8, pace=0.02, deadline=30)
    assert stats["steps"] >= 5 and time.monotonic() - t0 >= 0.02 * (stats["steps"] - 1)
    src.close()
    dst.close()
    # persistent BUSY (rollback-journal source held EXCLUSIVE by another writer) ends at the deadline
    holder = sqlite3.connect(src_path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("INSERT INTO t VALUES ('y')")
    try:
        src = sqlite3.connect(src_path, timeout=0.05)
        t0 = time.monotonic()
        with pytest.raises(mod.BackupTimeout):
            mod.take_snapshot(src, sqlite3.connect(":memory:"), pages=8, pace=0.01, deadline=0.5)
        assert time.monotonic() - t0 < 5
        src.close()
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_io_gate_idle_needs_proof_and_cap_checked_after_last_sample():
    t = mod.is_quiet
    assert t({"write_ms": None, "in_flight": 0, "sync": "inactive"}, 100)
    assert not t({"write_ms": None, "in_flight": 3, "sync": "inactive"}, 100)
    assert not t({"write_ms": 50, "sync": "active"}, 100)
    assert not t({"write_ms": 100, "sync": "inactive"}, 100)
    io = FakeIO([150, 150, 50, 50, 50])
    gate = mod.IOGate(io.sample, io.clock, 100, 3, 15, lambda e: None)
    with pytest.raises(mod.IOAbort):  # 3rd quiet sample lands at 20 s paused > 15 s cap
        gate.wait()
    io = FakeIO([150, 50, 50, 50])
    gate = mod.IOGate(io.sample, io.clock, 100, 3, 15, lambda e: None)
    assert gate.wait()["paused_seconds"] == 15


def _add_cjk_table(db):
    conn = sqlite3.connect(db)
    conn.execute("CREATE VIRTUAL TABLE messages_fts_cjk USING fts5(content)")
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute("UPDATE sqlite_master SET sql = replace(sql, 'fts5(content)', "
                 "'fts5(content, tokenize=''cjk_unicode61'')') WHERE name = 'messages_fts_cjk'")
    conn.commit()
    conn.close()


def test_cjk_index_without_loadable_tokenizer_is_refused_per_db(env):
    tmp_path, dbs, lst = env
    _add_cjk_table(dbs["structural"])
    before = {k: sha(p) for k, p in dbs.items()}
    code, res, _, _ = run(tmp_path, lst, "--apply", "--cjk-so", str(tmp_path / "missing.so"))
    assert code == 4
    row = res[str(dbs["structural"])]
    assert row["status"] == "failed_cjk_tokenizer" and "tokenizer" in row["error"]
    assert res[str(dbs["drift"])]["status"] == "not_started"
    assert {k: sha(p) for k, p in dbs.items()} == before


def test_non_success_statuses_and_empty_list_exit_nonzero(env, tmp_path):
    _, dbs, _ = env
    lst = tmp_path / "m.txt"
    lst.write_text(json.dumps({"path": str(tmp_path / "gone" / "state.db")}) + "\n" +
                   json.dumps({"path": str(dbs["structural"])}) + "\n")
    code, res, rows, _ = run(tmp_path, lst, "--apply")
    assert code == 4
    assert res[str(tmp_path / "gone" / "state.db")]["status"] == "skipped_missing"
    assert res[str(dbs["structural"])]["status"] == "not_started"
    assert rows[-1]["kind"] == "summary" and rows[-1]["exit"] == 4
    empty = tmp_path / "empty.txt"
    empty.write_text("# header only\n")
    io = FakeIO([])
    assert mod.main(["--list", str(empty), "--snapshot-dir", str(tmp_path / "s2")],
                    deps=mod.Deps(admission=flock_admission, sampler=io.sample, clock=io.clock)) == 2


def test_per_db_exceptions_land_in_ledger_with_stage(env, monkeypatch):
    tmp_path, dbs, lst = env
    real = mod.fingerprint
    monkeypatch.setattr(mod, "fingerprint", lambda c: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    code, res, rows, _ = run(tmp_path, lst, "--apply")
    assert code == 4
    row = res[str(dbs["structural"])]
    assert row["status"] == "failed_snapshot" and "disk I/O error" in row["error"] and row["committed"] is False
    assert rows[-1]["kind"] == "summary"
    monkeypatch.setattr(mod, "fingerprint", real)

    def boom(db):
        raise OSError("post-commit probe exploded")
    code, res, _, _ = run(tmp_path, lst, "--apply", after_commit=boom)
    assert code == 4
    row = res[str(dbs["structural"])]
    assert row["status"] == "failed_needs_manual" and row["committed"] is True
