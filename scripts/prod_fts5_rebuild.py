#!/usr/bin/env python3
"""Rebuild the ``messages_fts_trigram`` FTS5 index on an explicit list of Hermes state.db files.

Production ops script (slug mt-prod-fts5-rebuild-18dbs; runbook docs/prod-fts5-rebuild-runbook.md).

Per database, strictly one at a time:

1. health: ``PRAGMA quick_check`` + trigram ``integrity-check`` (rank=1).  A healthy DB is
   ``skipped_healthy`` and never written (apply) — the integrity probe runs inside a transaction
   that is always rolled back.
2. free-space check (>= 3x db+wal) -> 5 s write-latency gate -> paced, deadline-bounded SQLite
   online-backup snapshot into an exclusively created file in ``--snapshot-dir`` -> ``quick_check``.
3. ``--dry-run`` (default): the critical section below runs on the SNAPSHOT inside a transaction
   that is rolled back, so the original is only opened read-only and the snapshot stays a faithful
   copy; its duration is recorded as ``critical_section_seconds_on_snapshot``.
   ``--apply``: core ``hermes_state_common.fts_rebuild_admission`` lock, ``BEGIN IMMEDIATE`` on the
   original, one statement ``INSERT INTO messages_fts_trigram(messages_fts_trigram) VALUES('rebuild')``.
4. verify inside the same write transaction: quick_check ok, integrity-check ok, messages/sessions
   row counts and full-row hashes identical before/after.  Mismatch -> ROLLBACK (the original is
   exactly as before), stop the run.  After COMMIT, a fresh read transaction re-checks health only
   (users may legitimately write after COMMIT).  A post-commit failure is NEVER auto-restored
   online (a live restore would drop concurrent writes): it is ``failed_needs_manual``; the
   runbook's offline restore procedure applies.
5. one JSON line per DB into ``--ledger`` (append-only); every exception is caught per DB, recorded
   with its stage and whether the write was committed, and the run stops with a summary line.

There is deliberately no file-deletion code anywhere in this file: snapshots and ledgers are left
for a human.  Only ``messages_fts_trigram`` is written; ``messages_fts``/``messages_fts_cjk`` are
never touched (the cjk tokenizer is loaded read-side only so ``quick_check`` can visit that index).
Only the stdlib and the core admission lock / cjk-extension path are imported.

Exit codes: 0 every DB rebuilt/skipped_healthy (dry-run: would_rebuild/would_skip_healthy);
2 usage / wrong interpreter / IO guard unavailable / unsafe ledger path / empty list; 3 aborted on
IO pause cap; 4 stopped on a failure or a non-success status; 5 finished but some DBs were deferred.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager as ContextManager
from pathlib import Path
from typing import Any

TABLE = "messages_fts_trigram"
CJK_TABLE = "messages_fts_cjk"
REBUILD_SQL = f"INSERT INTO {TABLE}({TABLE}) VALUES('rebuild')"
INTEGRITY_SQL = f"INSERT INTO {TABLE}({TABLE}, rank) VALUES('integrity-check', 1)"
EXTERNAL_SRC = "messages_fts_trigram_src"
PROBE_TERMS = 5
CJK3 = re.compile(r"[一-鿿]{3}")

EXIT_OK, EXIT_USAGE, EXIT_IO, EXIT_FAILED, EXIT_DEFERRED = 0, 2, 3, 4, 5
SUCCESS = {"apply": {"rebuilt", "skipped_healthy"}, "dry_run": {"would_rebuild", "would_skip_healthy"}}
DEFER_STATUSES = {"deferred_lock", "deferred_busy"}

DEFAULT_RELEASE = "/home/hermes/releases/hermes-agent-v0214-2082ff0c17"
DEFAULT_SQLITE = "3.53.1"


class IOAbort(Exception):
    pass


class BackupTimeout(Exception):
    pass


# ── core integration ────────────────────────────────────────────────────────────────────────────

def load_core_admission() -> tuple[Callable[..., ContextManager[bool]] | None, str | None]:
    try:
        import hermes_state_common  # type: ignore
    except Exception:  # noqa: BLE001 - any import failure means "not the core venv"
        return None, None
    fn = getattr(hermes_state_common, "fts_rebuild_admission", None)
    return fn, getattr(hermes_state_common, "__file__", None)


def default_cjk_so() -> Path | None:
    try:
        from hermes_state_fts import fts5_cjk_so_path  # type: ignore
        return Path(fts5_cjk_so_path())
    except Exception:  # noqa: BLE001
        home = os.environ.get("HERMES_HOME")
        return Path(home) / "lib" / "libfts5_cjk.so" if home else None


def validate_interpreter(core_file: str | None, release: str, want_sqlite: str) -> str | None:
    """None when this interpreter is the approved core venv; else the reason to refuse --apply."""
    if core_file is None:
        return f"{sys.executable} cannot import hermes_state_common.fts_rebuild_admission"
    rel = os.path.realpath(release)
    if os.path.realpath(sys.prefix) != os.path.realpath(os.path.join(rel, ".venv")):
        return f"sys.prefix {sys.prefix} is not {rel}/.venv"
    if not os.path.realpath(core_file).startswith(rel + os.sep):
        return f"hermes_state_common loaded from {core_file}, outside {rel}"
    if sqlite3.sqlite_version != want_sqlite:
        return f"sqlite {sqlite3.sqlite_version} != approved {want_sqlite}"
    return None


# ── IO gate (A-stage-ops/io_guard.py criterion, stricter on idle windows) ───────────────────────

def _read_stat(device: str) -> list[int]:
    return list(map(int, Path(f"/sys/block/{device}/stat").read_text().split()))


def _unit_state(unit: str) -> str:
    return subprocess.check_output(
        ["systemctl", "--user", "show", unit, "-p", "ActiveState", "--value"],
        text=True, timeout=10).strip()


def make_sampler(device: str, seconds: float, sync_unit: str) -> Callable[[], dict[str, Any]]:
    def sample() -> dict[str, Any]:
        a = _read_stat(device)
        time.sleep(seconds)
        b = _read_stat(device)
        writes, ticks = b[4] - a[4], b[7] - a[7]
        return {"write_ms": (ticks / writes) if writes > 0 else None, "writes": writes,
                "in_flight": b[8], "sync": _unit_state(sync_unit) if sync_unit else "disabled"}
    return sample


def is_quiet(row: dict[str, Any], threshold_ms: float) -> bool:
    """Quiet = measured latency below threshold, or a window with no completed writes AND nothing
    in flight (provably idle).  No completions with requests in flight counts as busy."""
    if row.get("sync") in ("active", "activating"):
        return False
    wm = row.get("write_ms")
    if wm is None:
        return row.get("in_flight") == 0
    return wm < threshold_ms


class IOGate:
    def __init__(self, sampler, clock, threshold_ms: float, resume_consecutive: int,
                 pause_cap_seconds: float, emit):
        self.sampler, self.clock, self.emit = sampler, clock, emit
        self.threshold_ms, self.resume_consecutive = threshold_ms, resume_consecutive
        self.pause_cap, self.paused_total = pause_cap_seconds, 0.0

    def wait(self) -> dict[str, Any]:
        row = self.sampler()
        if is_quiet(row, self.threshold_ms):
            return {"sample": row, "paused_seconds": 0.0}
        start = self.clock()
        self.emit({"kind": "io_pause", "sample": row})
        consecutive = 0
        while True:
            row = self.sampler()
            elapsed = self.clock() - start
            if self.paused_total + elapsed > self.pause_cap:  # checked after EVERY sample
                self.paused_total += elapsed
                raise IOAbort(f"paused {self.paused_total:.0f}s > cap {self.pause_cap:.0f}s")
            consecutive = consecutive + 1 if is_quiet(row, self.threshold_ms) else 0
            if consecutive >= self.resume_consecutive:
                break
        self.paused_total += elapsed
        self.emit({"kind": "io_resume", "sample": row, "paused_seconds": round(elapsed, 3)})
        return {"sample": row, "paused_seconds": round(elapsed, 3)}


# ── sqlite helpers ─────────────────────────────────────────────────────────────────────────────

def _uri(path: Path, mode: str) -> str:
    # as_uri() percent-encodes '?', '#', '%' and non-ASCII, so a file name can never become URI
    # parameters (e.g. drop mode=ro) or point at another database.
    return f"{Path(os.path.abspath(path)).as_uri()}?mode={mode}"


class Connector:
    """All connections go through here: URI-safe, never create a DB, and load the cjk tokenizer
    (read-side only) when the DB carries a messages_fts_cjk index."""

    def __init__(self, cjk_so: Path | None, busy_timeout_ms: int):
        self.cjk_so, self.busy_timeout_ms = cjk_so, busy_timeout_ms

    def open(self, path: Path, mode: str) -> sqlite3.Connection:
        conn = sqlite3.connect(_uri(path, mode), uri=True, isolation_level=None,
                               timeout=self.busy_timeout_ms / 1000)
        conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        if has_table(conn, CJK_TABLE):
            self.load_cjk(conn)
        return conn

    def load_cjk(self, conn: sqlite3.Connection) -> None:
        if self.cjk_so is None or not self.cjk_so.exists():
            raise CjkUnavailable(f"{CJK_TABLE} present but tokenizer extension not found ({self.cjk_so})")
        if not hasattr(conn, "enable_load_extension"):
            raise CjkUnavailable("this python sqlite3 cannot load extensions")
        try:
            conn.enable_load_extension(True)
            try:
                conn.load_extension(str(self.cjk_so))
            finally:
                conn.enable_load_extension(False)
        except sqlite3.Error as exc:
            raise CjkUnavailable(f"loading {self.cjk_so} failed: {exc}") from exc


class CjkUnavailable(Exception):
    pass


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def quick_check(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("PRAGMA quick_check").fetchall()]


def is_busy_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and ("locked" in msg or "busy" in msg)


def integrity_in_txn(conn: sqlite3.Connection) -> str | None:
    """Run the trigram integrity-check inside the caller's open transaction; None = ok."""
    try:
        conn.execute(INTEGRITY_SQL)
        return None
    except sqlite3.DatabaseError as exc:
        if is_busy_error(exc):
            raise  # lock contention is not corruption
        return str(exc)


def health(conn: sqlite3.Connection, *, in_txn: bool) -> dict[str, Any]:
    """quick_check + integrity-check in ONE consistent transaction (rolled back unless in_txn)."""
    if not in_txn:
        conn.execute("BEGIN")
    try:
        qc = quick_check(conn)
        t0 = time.monotonic()
        ic = integrity_in_txn(conn)
    finally:
        if not in_txn:
            conn.execute("ROLLBACK")
    return {"quick_check": qc, "integrity": ic or "ok",
            "integrity_seconds": round(time.monotonic() - t0, 3),
            "healthy": qc == ["ok"] and ic is None}


def table_info(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()
    info: dict[str, Any] = {"present": row is not None, "cjk_index": has_table(conn, CJK_TABLE)}
    if row:
        info["layout"] = "external" if EXTERNAL_SRC in (row[0] or "") else "inline"
    if has_table(conn, "state_meta"):
        info["state_meta"] = {k: v for k, v in conn.execute(
            "SELECT key, value FROM state_meta WHERE key LIKE 'fts%'").fetchall()}
    return info


def fingerprint(conn: sqlite3.Connection) -> dict[str, Any]:
    """Row count + sha256 over EVERY column of messages and sessions."""
    out: dict[str, Any] = {}
    for table in ("messages", "sessions"):
        h, n = hashlib.sha256(), 0
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            h.update(repr(tuple(row)).encode())
            n += 1
        out[table] = {"rows": n, "sha256": h.hexdigest()}
    return out


def _truth_source(layout: str) -> tuple[str, str]:
    """(FROM, WHERE-prefix) of the rows the trigram index should cover (LIKE ground truth)."""
    if layout == "external":
        return EXTERNAL_SRC, ""
    return "messages", "role <> 'tool' AND "


def pick_terms(conn: sqlite3.Connection, layout: str) -> list[str]:
    frm, pre = _truth_source(layout)
    total = conn.execute(f"SELECT count(*) FROM {frm} WHERE {pre}1").fetchone()[0]
    terms: list[str] = []
    step = max(total // (PROBE_TERMS * 4), 1)
    for offset in range(0, total, step):
        row = conn.execute(f"SELECT content FROM {frm} WHERE {pre}1 LIMIT 1 OFFSET ?", (offset,)).fetchone()
        for m in CJK3.finditer((row[0] or "") if row else ""):
            if m.group() not in terms:
                terms.append(m.group())
                break
        if len(terms) >= PROBE_TERMS:
            break
    return terms


def search_probe(conn: sqlite3.Connection, terms: list[str], layout: str) -> list[dict[str, Any]]:
    """Counts only (no text in the ledger): trigram MATCH hits vs LIKE ground truth."""
    frm, pre = _truth_source(layout)
    res = []
    for i, t in enumerate(terms):
        item: dict[str, Any] = {"term": i}
        try:
            item["match"] = conn.execute(
                f"SELECT count(*) FROM {TABLE} WHERE {TABLE} MATCH ?", ('"' + t + '"',)).fetchone()[0]
        except sqlite3.DatabaseError as exc:
            item["match_error"] = str(exc)
        item["like"] = conn.execute(
            f"SELECT count(*) FROM {frm} WHERE {pre}content LIKE ?", ("%" + t + "%",)).fetchone()[0]
        res.append(item)
    return res


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def reserve_snapshot(snapshot_dir: Path, db: Path) -> Path:
    """Atomically create a NEW empty file (O_EXCL): two runs can never share or overwrite one."""
    base = re.sub(r"[^A-Za-z0-9._-]+", "__", str(db).strip("/"))
    n = 0
    while True:
        cand = snapshot_dir / (f"{base}.snapshot.db" if n == 0 else f"{base}.snapshot.{n}.db")
        try:
            os.close(os.open(cand, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return cand
        except FileExistsError:
            n += 1


def take_snapshot(src: sqlite3.Connection, dest: sqlite3.Connection, pages: int, pace: float,
                  deadline: float, clock=time.monotonic) -> dict[str, Any]:
    """Online backup with an explicit sleep between successful partial steps and a hard deadline
    (checked on every step, BUSY/LOCKED retries included)."""
    stats = {"steps": 0, "busy": 0}
    end = clock() + deadline

    def progress(status: int, remaining: int, total: int) -> None:
        stats["steps"] += 1
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            stats["busy"] += 1
        if clock() > end:
            raise BackupTimeout(f"backup exceeded {deadline:.0f}s ({stats})")
        if remaining and status == sqlite3.SQLITE_OK:
            time.sleep(pace)

    src.backup(dest, pages=pages, progress=progress, sleep=pace)
    return stats


# ── per-db runner ──────────────────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class Deps:
    admission: Callable[..., ContextManager[bool]] | None
    sampler: Callable[[], dict[str, Any]]
    clock: Callable[[], float] = time.monotonic
    snapshot_quick_check: Callable[[sqlite3.Connection], list[str]] = quick_check
    in_txn_verify: Callable[[dict, dict], str | None] | None = None  # test fault hook
    after_commit: Callable[[Path], None] | None = None  # test hook: concurrent user write
    post_commit_verify: Callable[[Path], str | None] | None = None  # test fault hook


@dataclasses.dataclass
class Opts:
    apply: bool
    snapshot_dir: Path
    connector: Connector
    lock_timeout: float = 10.0
    backup_pages: int = 256
    backup_pace: float = 0.05
    backup_timeout: float = 1800.0
    space_factor: float = 3.0


def db_bytes(db: Path) -> int:
    wal = Path(str(db) + "-wal")
    return db.stat().st_size + (wal.stat().st_size if wal.exists() else 0)


def compare(before: dict, after: dict) -> str | None:
    for key in ("messages", "sessions"):
        if before[key] != after[key]:
            return f"{key} changed: {before[key]} -> {after[key]}"
    return None


def process_db(db: Path, opts: Opts, deps: Deps, gate: IOGate, row: dict[str, Any]) -> dict[str, Any]:
    """Mutates ``row`` as it goes (stage/committed) so the caller can classify an exception."""
    c = opts.connector
    if not db.is_file():
        return {**row, "status": "skipped_missing"}

    row["stage"] = "health"
    ro = c.open(db, "ro")
    try:
        info = table_info(ro)
        row["table"] = info
        if not info["present"]:
            return {**row, "status": "skipped_no_trigram"}
        row["before_quick_check"] = quick_check(ro)
    finally:
        ro.close()
    if opts.apply and row["before_quick_check"] == ["ok"]:
        rw = c.open(db, "rw")
        try:
            row["before"] = health(rw, in_txn=False)
        finally:
            rw.close()
        if row["before"]["healthy"]:
            return {**row, "status": "skipped_healthy"}

    row["stage"] = "space"
    need = int(db_bytes(db) * opts.space_factor)
    free = shutil.disk_usage(opts.snapshot_dir).free
    row["space"] = {"need": need, "free": free}
    if free < need:
        return {**row, "status": "failed_space"}
    row["stage"] = "io_gate"
    row["io"] = gate.wait()

    row["stage"] = "snapshot"
    snap = reserve_snapshot(opts.snapshot_dir, db)
    row["snapshot"] = {"path": str(snap)}
    t0 = time.monotonic()
    src, dst = c.open(db, "ro"), sqlite3.connect(_uri(snap, "rw"), uri=True, isolation_level=None)
    try:
        try:
            row["snapshot"]["backup"] = take_snapshot(src, dst, opts.backup_pages, opts.backup_pace,
                                                      opts.backup_timeout)
        except BackupTimeout as exc:
            return {**row, "status": "failed_snapshot", "error": str(exc)}
    finally:
        dst.close()
        src.close()
    sconn = c.open(snap, "ro")
    try:
        sqc = deps.snapshot_quick_check(sconn)
        snap_fp = fingerprint(sconn)
        terms = pick_terms(sconn, info["layout"])
        row["search_before"] = search_probe(sconn, terms, info["layout"])  # outside any write lock
    finally:
        sconn.close()
    row["snapshot"].update(bytes=snap.stat().st_size, sha256=file_sha256(snap),
                           seconds=round(time.monotonic() - t0, 3), quick_check=sqc)
    # A trigram-malformed DB's snapshot reports the same FTS5 error; anything else is a bad snapshot.
    if sqc != row["before_quick_check"]:
        return {**row, "status": "failed_snapshot"}

    if not opts.apply:
        return dry_run_on_snapshot(snap, row, info, terms, c)
    return apply_on_original(db, row, info, terms, snap_fp, opts, deps)


def dry_run_on_snapshot(snap: Path, row: dict, info: dict, terms: list[str], c: Connector) -> dict:
    row["stage"] = "dry_run"
    conn = c.open(snap, "rw")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            t_cs = time.monotonic()  # same critical section apply runs under the write lock
            fp_before = fingerprint(conn)
            before = health(conn, in_txn=True)
            row["before"] = before
            if before["healthy"]:
                return {**row, "status": "would_skip_healthy"}
            t0 = time.monotonic()
            conn.execute(REBUILD_SQL)
            row["rebuild_seconds_on_snapshot"] = round(time.monotonic() - t0, 3)
            row["after"] = health(conn, in_txn=True)
            diff = compare(fp_before, fingerprint(conn))
            row["critical_section_seconds_on_snapshot"] = round(time.monotonic() - t_cs, 3)
            row["rows"] = fp_before
            row["search_after"] = search_probe(conn, terms, info["layout"])
            ok = row["after"]["healthy"] and diff is None
            return {**row, "status": "would_rebuild" if ok else "would_fail",
                    **({"error": diff} if diff else {})}
        finally:
            conn.execute("ROLLBACK")  # snapshot stays a byte-faithful backup
    finally:
        conn.close()


def apply_on_original(db: Path, row: dict, info: dict, terms: list[str], snap_fp: dict,
                      opts: Opts, deps: Deps) -> dict:
    assert deps.admission is not None
    c = opts.connector
    row["stage"] = "lock"
    with deps.admission(str(db), timeout_seconds=opts.lock_timeout) as granted:
        if not granted:
            return {**row, "status": "deferred_lock"}
        conn = c.open(db, "rw")
        try:
            row["stage"] = "begin"
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if not is_busy_error(exc):
                    raise
                return {**row, "status": "deferred_busy", "error": str(exc)}
            row["stage"] = "rebuild"
            t_lock = time.monotonic()
            try:
                fp_before = fingerprint(conn)
                t0 = time.monotonic()
                try:
                    conn.execute(REBUILD_SQL)
                except sqlite3.DatabaseError as exc:
                    if is_busy_error(exc):
                        raise
                    return {**row, "status": "failed_rebuild", "error": str(exc)}
                row["rebuild_seconds"] = round(time.monotonic() - t0, 3)
                row["stage"] = "verify"
                after = health(conn, in_txn=True)
                row["after"] = after
                fp_after = fingerprint(conn)
                problem = compare(fp_before, fp_after)
                if not after["healthy"]:
                    problem = problem or f"not healthy after rebuild: {after}"
                if deps.in_txn_verify is not None:
                    problem = problem or deps.in_txn_verify(fp_before, fp_after)
                if problem:
                    return {**row, "status": "failed_verify", "error": problem}
                row["stage"] = "commit"
                conn.execute("COMMIT")
                row["committed"] = True
                row["write_lock_seconds"] = round(time.monotonic() - t_lock, 3)
            finally:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
        finally:
            conn.close()
    row["rows"] = fp_before
    row["drift_since_snapshot"] = compare(snap_fp, fp_before)  # informational: live writes

    row["stage"] = "post_commit"
    if deps.after_commit is not None:
        deps.after_commit(db)
    conn = c.open(db, "rw")
    try:
        try:
            post = health(conn, in_txn=False)
        except sqlite3.OperationalError as exc:
            if not is_busy_error(exc):
                raise
            # in-transaction verification already passed; the runbook rescan covers this DB
            return {**row, "status": "rebuilt", "post_commit": f"skipped_busy: {exc}"}
        row["post_commit"] = post
        conn.execute("BEGIN")
        try:
            row["search_after"] = search_probe(conn, terms, info["layout"])
        finally:
            conn.execute("ROLLBACK")
    finally:
        conn.close()
    problem = None if post["healthy"] else f"post-commit health: {post}"
    if deps.post_commit_verify is not None:
        problem = problem or deps.post_commit_verify(db)
    if problem:
        # No online auto-restore: backing the snapshot over a live DB drops concurrent writes.
        return {**row, "status": "failed_needs_manual", "error": problem}
    return {**row, "status": "rebuilt"}


# ── list / ledger / main ───────────────────────────────────────────────────────────────────────

def read_list(path: Path) -> tuple[list[str], list[Path]]:
    headers, dbs, seen = [], [], set()
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            headers.append(line)
            continue
        p = json.loads(line)["path"] if line.startswith("{") else line
        if p not in seen:
            seen.add(p)
            dbs.append(Path(p))
    return headers, dbs


def ledger_problem(ledger: Path, list_path: Path, dbs: list[Path], snapshot_dir: Path) -> str | None:
    """Refuse a ledger path that aliases an input, a DB (or its -wal/-shm/lock) or lives among
    snapshots; an existing ledger must already be JSONL."""
    real = os.path.realpath(ledger)
    protected = {os.path.realpath(list_path)}
    for db in dbs:
        for suffix in ("", "-wal", "-shm", "-journal", ".fts_rebuild.lock"):
            protected.add(os.path.realpath(str(db) + suffix))
    if real in protected:
        return f"ledger {ledger} aliases an input or database file"
    if real.endswith(".db") or ".snapshot." in os.path.basename(real):
        return f"ledger {ledger} looks like a database/snapshot"
    if os.path.exists(real):
        with open(real, "rb") as fh:
            head = fh.read(4096)
        if head.startswith(b"SQLite format 3"):
            return f"ledger {ledger} is a SQLite database"
        first = head.split(b"\n", 1)[0].strip()
        if first:
            try:
                parsed = json.loads(first)
            except ValueError:
                parsed = None
            if not isinstance(parsed, dict):
                return f"existing ledger {ledger} is not JSONL"
    return None


class Ledger:
    def __init__(self, path: Path):
        self.path = path

    def write(self, rec: dict[str, Any]) -> None:
        rec = {"ts": dt.datetime.now().astimezone().isoformat(), **rec}
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(line, flush=True)


def build_parser() -> argparse.ArgumentParser:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--list", required=True, type=Path, help="A13 baseline file (# header + JSON lines with path)")
    ap.add_argument("--snapshot-dir", type=Path, default=Path(f"/home/hermes/backups/fts5-rebuild-{stamp}"))
    ap.add_argument("--ledger", type=Path, help="JSONL ledger (default <snapshot-dir>/ledger.jsonl)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--apply", action="store_true")
    ap.add_argument("--core-release", default=DEFAULT_RELEASE, help="approved core release dir (--apply)")
    ap.add_argument("--require-sqlite", default=DEFAULT_SQLITE, help="approved sqlite version (--apply)")
    ap.add_argument("--cjk-so", type=Path, help="cjk_unicode61 extension (default: core's fts5_cjk_so_path)")
    ap.add_argument("--io-device", default="vda")
    ap.add_argument("--io-sample-seconds", type=float, default=5.0)
    ap.add_argument("--write-ms-threshold", type=float, default=100.0)
    ap.add_argument("--resume-consecutive", type=int, default=3)
    ap.add_argument("--pause-cap-seconds", type=float, default=1800.0)
    ap.add_argument("--sync-unit", default="hermes-feishu-sync.service", help="'' disables the check")
    ap.add_argument("--busy-timeout-ms", type=int, default=10_000)
    ap.add_argument("--lock-timeout", type=float, default=10.0)
    ap.add_argument("--backup-pace-seconds", type=float, default=0.05)
    ap.add_argument("--backup-timeout-seconds", type=float, default=1800.0)
    return ap


def refuse(msg: str) -> int:
    print(f"refusing to run: {msg}", file=sys.stderr)
    return EXIT_USAGE


def main(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    args = build_parser().parse_args(argv)
    apply = bool(args.apply)
    mode = "apply" if apply else "dry_run"
    if deps is None:
        admission, core_file = load_core_admission()
        if apply:
            reason = validate_interpreter(core_file if admission else None, args.core_release,
                                          args.require_sqlite)
            if reason:
                print(f"refusing --apply: {reason} (not the approved core venv interpreter)", file=sys.stderr)
                return EXIT_USAGE
        try:  # both modes read the disk heavily (snapshots): no IO guard, no run
            _read_stat(args.io_device)
            if args.sync_unit:
                _unit_state(args.sync_unit)
        except Exception as exc:  # noqa: BLE001 - fail closed
            return refuse(f"IO guard unavailable ({exc})")
        deps = Deps(admission=admission,
                    sampler=make_sampler(args.io_device, args.io_sample_seconds, args.sync_unit))
    headers, dbs = read_list(args.list)
    if not dbs:
        return refuse(f"{args.list} lists no databases")
    ledger_path = args.ledger or args.snapshot_dir / "ledger.jsonl"
    problem = ledger_problem(ledger_path, args.list, dbs, args.snapshot_dir)
    if problem:
        return refuse(problem)
    args.snapshot_dir.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(ledger_path)
    cjk_so = args.cjk_so or default_cjk_so()
    ledger.write({"kind": "run", "mode": mode, "list": str(args.list),
                  "list_headers": headers, "dbs": len(dbs), "python": sys.executable,
                  "python_realpath": os.path.realpath(sys.executable), "sys_prefix": sys.prefix,
                  "sqlite": sqlite3.sqlite_version, "core_admission": deps.admission is not None,
                  "cjk_so": str(cjk_so) if cjk_so else None, "snapshot_dir": str(args.snapshot_dir),
                  **({} if deps.admission else {"warning": "core admission lock unavailable (dry-run only)"})})
    opts = Opts(apply=apply, snapshot_dir=args.snapshot_dir,
                connector=Connector(cjk_so, args.busy_timeout_ms), lock_timeout=args.lock_timeout,
                backup_pace=args.backup_pace_seconds, backup_timeout=args.backup_timeout_seconds)
    gate = IOGate(deps.sampler, deps.clock, args.write_ms_threshold, args.resume_consecutive,
                  args.pause_cap_seconds, ledger.write)
    counts: dict[str, int] = {}
    code = EXIT_OK
    for i, db in enumerate(dbs):
        row: dict[str, Any] = {"db": str(db), "mode": mode, "committed": False}
        try:
            rec = process_db(db, opts, deps, gate, row)
        except IOAbort as exc:
            for rest in dbs[i:]:
                ledger.write({"db": str(rest), "status": "aborted_io", "error": str(exc)})
                counts["aborted_io"] = counts.get("aborted_io", 0) + 1
            code = EXIT_IO
            break
        except CjkUnavailable as exc:
            rec = {**row, "status": "failed_cjk_tokenizer", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - every per-DB error lands in the ledger
            err = f"{type(exc).__name__}: {exc}"
            if row.get("committed"):
                rec = {**row, "status": "failed_needs_manual", "error": err}
            elif is_busy_error(exc):
                rec = {**row, "status": "deferred_busy", "error": err}
            else:
                rec = {**row, "status": f"failed_{row.get('stage', 'unknown')}", "error": err}
        ledger.write(rec)
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
        if rec["status"] in DEFER_STATUSES:
            code = EXIT_DEFERRED
            continue
        if rec["status"] not in SUCCESS[mode]:
            code = EXIT_FAILED
            for rest in dbs[i + 1:]:
                ledger.write({"db": str(rest), "status": "not_started", "error": "run stopped"})
            break
    ledger.write({"kind": "summary", "counts": counts, "exit": code})
    return code


if __name__ == "__main__":
    sys.exit(main())
