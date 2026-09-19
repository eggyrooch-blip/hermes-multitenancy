"""Conservative, reversible lifecycle management for unreferenced profiles."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .credential_renewal_common import is_fixture_path


@dataclass
class QuarantineReport:
    run_id: str
    referenced: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)


def _live_pid(path: Path) -> bool:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        try:
            pid = int(raw)
        except ValueError:
            payload = json.loads(raw)
            pid = int(payload["pid"])
        os.kill(pid, 0)
        return True
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _cron_profile_names(value: Any, known: set[str]) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"profile", "profile_name", "target_profile"} and str(child) in known:
                found.add(str(child))
            found.update(_cron_profile_names(child, known))
    elif isinstance(value, list):
        for child in value:
            found.update(_cron_profile_names(child, known))
    return found


def _db_references(shared_home: Path) -> set[str] | None:
    db_path = shared_home / "multitenancy.db"
    if not db_path.is_file():
        return None
    refs: set[str] = set()
    queries = (
        "SELECT profile_name FROM multitenancy_routing WHERE active = 1",
        "SELECT upstream_profile FROM multitenancy_routing WHERE active = 1 AND upstream_profile IS NOT NULL",
        "SELECT profile_name FROM multitenancy_channel_bindings WHERE active = 1",
        "SELECT DISTINCT profile_name FROM multitenancy_sessions",
    )
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            for query in queries:
                table = query.split(" FROM ", 1)[1].split()[0]
                if table not in tables:
                    continue
                refs.update(str(row[0]) for row in conn.execute(query) if row[0])
    except sqlite3.Error:
        # An unreadable reference source is not proof that anything is orphaned.
        return None
    return refs


def quarantine_orphan_profiles(shared_home: Path, *, apply: bool = False) -> QuarantineReport:
    """Plan or reversibly move profiles that have no durable/live reference.

    Default is dry-run. Any unreadable reference source makes the operation
    fail closed by returning no candidates.
    """
    shared_home = Path(shared_home).resolve()
    profiles_dir = shared_home / "profiles"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = QuarantineReport(run_id=run_id)
    if not profiles_dir.is_dir():
        return report

    profiles = {
        entry.name: entry
        for entry in profiles_dir.iterdir()
        if entry.is_dir() and not is_fixture_path(entry)
    }
    known = set(profiles)
    db_refs = _db_references(shared_home)
    if db_refs is None:
        return report
    refs = db_refs
    for name, profile in profiles.items():
        if (profile / ".keep").exists() or _live_pid(profile / "gateway.pid"):
            refs.add(name)

    cron_paths = [shared_home / "cron" / "jobs.json"]
    cron_paths.extend(profile / "cron" / "jobs.json" for profile in profiles.values())
    for cron_path in cron_paths:
        if cron_path.is_file():
            try:
                refs.update(_cron_profile_names(json.loads(cron_path.read_text()), known))
                if cron_path.parent.parent.name in known:
                    refs.add(cron_path.parent.parent.name)
            except (OSError, json.JSONDecodeError):
                return report

    report.referenced = sorted(refs & known)
    report.candidates = sorted(known - refs)
    if not apply or not report.candidates:
        return report

    destination = shared_home / "profile-quarantine" / run_id
    destination.mkdir(parents=True, exist_ok=False)
    for name in report.candidates:
        shutil.move(str(profiles[name]), str(destination / name))
        report.quarantined.append(name)
    (destination / "manifest.json").write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def quarantine_matching_orphans(
    shared_home: Path, *, prefixes: tuple[str, ...], apply: bool = False
) -> QuarantineReport:
    """Restrict a conservative orphan plan to operator-selected name prefixes."""
    report = quarantine_orphan_profiles(shared_home, apply=False)
    report.candidates = [
        name for name in report.candidates if any(name.startswith(prefix) for prefix in prefixes)
    ]
    if not apply or not report.candidates:
        return report
    profiles_dir = Path(shared_home).resolve() / "profiles"
    destination = Path(shared_home).resolve() / "profile-quarantine" / report.run_id
    destination.mkdir(parents=True, exist_ok=False)
    for name in report.candidates:
        shutil.move(str(profiles_dir / name), str(destination / name))
        report.quarantined.append(name)
    (destination / "manifest.json").write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit and reversibly quarantine orphan Hermes profiles")
    parser.add_argument("--home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--apply", action="store_true", help="move candidates into profile-quarantine")
    parser.add_argument("--prefix", action="append", default=[], help="limit to profile name prefix")
    args = parser.parse_args(argv)
    if args.prefix:
        report = quarantine_matching_orphans(
            args.home, prefixes=tuple(args.prefix), apply=args.apply
        )
    else:
        report = quarantine_orphan_profiles(args.home, apply=args.apply)
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
