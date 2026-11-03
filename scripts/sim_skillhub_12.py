#!/usr/bin/env python3
"""Phase A end-to-end SIM: SkillHub installer worker against 12 REAL ldap profiles.

Drives `run_worker` (the real worker entry point) against a realistic
`skill.install_approved` event with an injected downloader (no network), then a
`skill.status_changed` event, asserting:

  (a) canonical release dir exists with SKILL.md
  (b) all 12 profiles/<ldap>/skills/<skill> are symlinks resolving to canonical
  (c) each profile has .hermes-managed.json with credential=kep-cli + origin=aidock-skillhub
  (d) each profile has skills/.keephub/lock.json == {"installed": {<skill>: {...}}}
  (e) the event row flips to status='installed'

Then re-runs the worker (idempotent => all 12 'skipped'), then bumps the version
and asserts the symlinks REPOINT.

Standalone — creates everything under a self-managed temp dir and cleans it up.
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

from hermes_multitenancy import skillhub_events
from hermes_multitenancy.skillhub_installer import run_worker

# The 12 REAL ldap user_ids (Phase A audience).
LDAPS = [
    "dengwenhui",
    "xiongshuangquan",
    "huangxiaofeng",
    "helong",
    "jinleilei",
    "wangchengming",
    "jiangzailong",
    "gengyujie",
    "ningkaixin",
    "shepeiyao",
    "liuchuanhao",
    "zhaoxijun",
]

SKILL_CODE = "kep-feishu-document-reading"

_FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        _FAILURES.append(label)


def seed_routing_db(shared_home: Path) -> None:
    """Seed multitenancy_routing with the 12 real ldap rows (mirrors prod schema)."""
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
            "INSERT INTO multitenancy_routing("
            "  user_id, profile_name, open_id, active, synced_at, created_at, updated_at, kind"
            ") VALUES (?, ?, ?, 1, 0, 0, 0, 'user')",
            # user_id == profile_name == <ldap>; dummy open_id
            [(ldap, ldap, f"ou_dummy_{ldap}") for ldap in LDAPS],
        )
        conn.commit()


def make_profile_dirs(shared_home: Path) -> Path:
    profiles_root = shared_home / "profiles"
    for ldap in LDAPS:
        (profiles_root / ldap / "skills").mkdir(parents=True, exist_ok=True)
    return profiles_root


def build_real_shape_zip(version: str, *, wrapper_dir: str) -> bytes:
    """In-memory zip mirroring the REAL package shape.

    Top dir '<wrapper_dir>/' containing SKILL.md, scripts/aidock_platform_hook.py,
    _meta.json.
    """
    buffer = io.BytesIO()
    root = f"{wrapper_dir.strip('/')}/"
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            f"{root}SKILL.md",
            f"---\nname: {SKILL_CODE}\nversion: {version}\n---\n# {SKILL_CODE}\n",
        )
        archive.writestr(
            f"{root}scripts/aidock_platform_hook.py",
            "def handle(event):\n    return event\n",
        )
        archive.writestr(
            f"{root}_meta.json",
            json.dumps({"skill_code": SKILL_CODE, "version": version}),
        )
    return buffer.getvalue()


def install_approved_event(version: str, release_id: str, download_url: str) -> dict:
    """Real event shape: skill.install_approved, auth_type='auth', 12 profile_ids."""
    return {
        "event_type": "skill.install_approved",
        "skill_code": SKILL_CODE,
        "release_id": release_id,
        "version": version,
        "download_url": download_url,
        "checksum_sha256": None,
        "skill_status": "active",
        "audience": {
            "auth_type": "auth",
            "users": [{"profile_id": ldap} for ldap in LDAPS],
        },
    }


def status_changed_event(version: str, release_id: str, download_url: str) -> dict:
    ev = install_approved_event(version, release_id, download_url)
    ev["event_type"] = "skill.status_changed"
    ev["skill_status"] = "active"
    return ev


def queue_event(store: "skillhub_events.SkillhubEventStore", payload: dict) -> str:
    raw = json.dumps(payload)
    event = skillhub_events.normalize_event(payload, raw_body=raw)
    status, duplicate = store.record(event, raw_payload=raw, signature_verified=False)
    assert duplicate is False, "unexpected duplicate on queue"
    assert status in {"queued", "queued_unknown_type"}, f"unexpected queue status {status}"
    return event["event_id"]


def assert_full_install(
    *,
    shared_home: Path,
    profiles_root: Path,
    version: str,
    release_id: str,
    wrapper_dir: str | None,
    phase: str,
) -> None:
    """Assertions (a)-(d) for a given installed version."""
    base = shared_home / "_managed" / "aidock-skillhub" / SKILL_CODE / version
    canonical = base if wrapper_dir is None else base / wrapper_dir

    # (a) canonical exists
    check((canonical / "SKILL.md").is_file(), f"[{phase}] (a) canonical SKILL.md at {canonical}")

    symlink_ok = 0
    marker_ok = 0
    lock_ok = 0
    for ldap in LDAPS:
        skill_dir = profiles_root / ldap / "skills" / SKILL_CODE
        # (b) symlink resolving to canonical
        if skill_dir.is_symlink() and skill_dir.resolve() == canonical.resolve():
            symlink_ok += 1

        # (c) .hermes-managed.json marker
        managed_path = profiles_root / ldap / "skills" / ".hermes-managed.json"
        try:
            managed = json.loads(managed_path.read_text(encoding="utf-8"))
            entry = managed["skills"][SKILL_CODE]
            if entry.get("credential") == "kep-cli" and entry.get("origin") == "aidock-skillhub" \
                    and entry.get("version") == version:
                marker_ok += 1
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            pass

        # (d) .keephub/lock.json
        lock_path = profiles_root / ldap / "skills" / ".keephub" / "lock.json"
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            expected = {
                "installed": {
                    SKILL_CODE: {"version": version, "release_id": release_id}
                }
            }
            if lock == expected:
                lock_ok += 1
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    check(symlink_ok == 12, f"[{phase}] (b) all 12 symlinks resolve to canonical ({symlink_ok}/12)")
    check(marker_ok == 12, f"[{phase}] (c) all 12 markers credential=kep-cli origin=aidock-skillhub ({marker_ok}/12)")
    check(lock_ok == 12, f"[{phase}] (d) all 12 keephub lock.json exact match ({lock_ok}/12)")


def main() -> int:
    tmp_root = Path(tempfile.mkdtemp(prefix="sim-skillhub-12-"))
    print(f"SIM root: {tmp_root}")
    try:
        shared_home = tmp_root / ".hermes"
        profiles_root = make_profile_dirs(shared_home)
        seed_routing_db(shared_home)
        store = skillhub_events.SkillhubEventStore(tmp_root / "events.db")

        v1 = "1.0.0"
        v1_zip = build_real_shape_zip(v1, wrapper_dir=SKILL_CODE)
        url = "https://aidock.invalid/kep-feishu-document-reading-1.0.0.zip"

        # ---- PHASE 1: fresh install via run_worker ----
        print("\n[PHASE 1] fresh install_approved -> run_worker (12 profiles)")
        ev1_id = queue_event(store, install_approved_event(v1, "rel_v1", url))
        s1 = run_worker(
            store=store,
            shared_home=shared_home,
            profiles_root=profiles_root,
            downloader=lambda _u: v1_zip,
        )
        print(f"  worker summary: {s1}")
        check(s1 == {"processed": 1, "installed": 1, "failed": 0, "skipped": 0},
              "[PHASE1] worker summary processed=1 installed=1")
        assert_full_install(
            shared_home=shared_home, profiles_root=profiles_root,
            version=v1, release_id="rel_v1", wrapper_dir=SKILL_CODE, phase="PHASE1",
        )
        # (e) event row flipped to status='installed'
        row1 = store.get(ev1_id)
        check(row1 is not None and row1["status"] == "installed",
              f"[PHASE1] (e) event row status='installed' (got {row1['status'] if row1 else None})")
        res1 = json.loads(row1["results_json"]) if row1 else {}
        installed_users = sum(1 for u in res1.get("users", {}).values() if u.get("status") == "installed")
        check(installed_users == 12, f"[PHASE1] results.users all 12 installed ({installed_users}/12)")

        # ---- PHASE 2: idempotent re-run (status_changed, same version) ----
        # A genuinely NEW event row (distinct release_id => distinct synthesized
        # event_id) re-targeting the SAME already-installed version. The canonical
        # is unchanged and every profile already points at it, so the worker must
        # report each of the 12 per-user statuses as 'skipped' (no churn).
        print("\n[PHASE 2] re-run same version (status_changed, new event row) -> all 12 skipped")
        ev2_id = queue_event(store, status_changed_event(v1, "rel_v1_rerun", url))
        s2 = run_worker(
            store=store,
            shared_home=shared_home,
            profiles_root=profiles_root,
            downloader=lambda _u: v1_zip,
        )
        print(f"  worker summary: {s2}")
        # event itself is processed+marked installed; per-user statuses must all be 'skipped'
        check(s2 == {"processed": 1, "installed": 1, "failed": 0, "skipped": 0},
              "[PHASE2] worker processed the re-run event")
        row2 = store.get(ev2_id)
        check(row2 is not None and row2["status"] == "installed",
              f"[PHASE2] re-run event row status='installed' (got {row2['status'] if row2 else None})")
        res2 = json.loads(row2["results_json"]) if row2 and row2.get("results_json") else {}
        skipped_users = sum(1 for u in res2.get("users", {}).values() if u.get("status") == "skipped")
        check(skipped_users == 12, f"[PHASE2] all 12 per-user statuses == 'skipped' ({skipped_users}/12)")

        # ---- PHASE 3: version bump 1.0.1 -> symlinks REPOINT ----
        print("\n[PHASE 3] version bump 1.0.1 -> symlinks repoint")
        v2 = "1.0.1"
        wrapper_v2 = f"{SKILL_CODE}-{v2}"
        v2_zip = build_real_shape_zip(v2, wrapper_dir=wrapper_v2)
        url2 = "https://aidock.invalid/kep-feishu-document-reading-1.0.1.zip"
        ev3_id = queue_event(store, install_approved_event(v2, "rel_v2", url2))
        s3 = run_worker(
            store=store,
            shared_home=shared_home,
            profiles_root=profiles_root,
            downloader=lambda _u: v2_zip,
        )
        print(f"  worker summary: {s3}")
        check(s3 == {"processed": 1, "installed": 1, "failed": 0, "skipped": 0},
              "[PHASE3] worker processed the bump event")

        canonical_v2 = shared_home / "_managed" / "aidock-skillhub" / SKILL_CODE / v2 / wrapper_v2
        repoint_ok = 0
        for ldap in LDAPS:
            skill_dir = profiles_root / ldap / "skills" / SKILL_CODE
            if skill_dir.is_symlink() and skill_dir.resolve() == canonical_v2.resolve():
                repoint_ok += 1
        check(repoint_ok == 12, f"[PHASE3] all 12 symlinks REPOINT to v1.0.1 canonical ({repoint_ok}/12)")
        assert_full_install(
            shared_home=shared_home, profiles_root=profiles_root,
            version=v2, release_id="rel_v2", wrapper_dir=wrapper_v2, phase="PHASE3",
        )
        row3 = store.get(ev3_id)
        # per-user status should be 'repointed'
        res3 = json.loads(row3["results_json"]) if row3 else {}
        repointed_users = sum(1 for u in res3.get("users", {}).values() if u.get("status") == "repointed")
        check(repointed_users == 12, f"[PHASE3] all 12 per-user statuses == 'repointed' ({repointed_users}/12)")

        store.close()

        print("\n==== SIM RESULT ====")
        if _FAILURES:
            print(f"FAILED ({len(_FAILURES)} assertion(s)):")
            for f in _FAILURES:
                print(f"  - {f}")
            return 1
        print("ALL ASSERTIONS PASSED: 12-profile install + markers + idempotency + version-repoint")
        return 0
    finally:
        import shutil
        shutil.rmtree(tmp_root, ignore_errors=True)
        print(f"cleaned up {tmp_root}")


if __name__ == "__main__":
    sys.exit(main())
