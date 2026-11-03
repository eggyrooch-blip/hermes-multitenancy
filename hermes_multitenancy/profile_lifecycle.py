"""Conservative, reversible lifecycle management for unreferenced profiles."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .credential_renewal_common import is_fixture_path
from .gateway_ownership import DEFAULT_ROUTER_PROFILE, router_profile_name


@dataclass
class QuarantineReport:
    run_id: str
    referenced: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)


def _live_pid(path: Path) -> bool | None:
    """True = alive, False = no process / no pid file, None = cannot tell.

    ``None`` must stop the run: an unreadable pid file, or a pid we are not
    allowed to signal (EPERM, gateway owned by another account), is not proof
    that the profile is unused.
    """
    if not path.exists():
        return False
    try:
        raw = path.read_text(encoding="utf-8").strip()
        try:
            pid = int(raw)
        except ValueError:
            payload = json.loads(raw)
            pid = int(payload["pid"])
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return None
    return True


# Same grammar as core ``hermes_constants.PROFILE_ID_RE``; anything else under
# profiles/ (``.deleted`` tombstones, stray dirs) is core bookkeeping, not a profile.
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _bot_chat_receivers(value: Any, known: set[str]) -> set[str]:
    """``bot-chat:<name>`` targets in a cron ``deliver`` / ``failure_deliver`` value."""
    parts: list[str] = []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                parts.extend(item.split(","))
    found: set[str] = set()
    for part in parts:
        raw = part.strip()
        if raw.lower().startswith("bot-chat:"):
            name = raw.split(":", 1)[1].strip().lower()
            if name in known:
                found.add(name)
    return found


def _cron_profile_names(value: Any, known: set[str]) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"profile", "profile_name", "target_profile"} and str(child) in known:
                found.add(str(child))
            if key in {"deliver", "failure_deliver"}:
                found.update(_bot_chat_receivers(child, known))
            found.update(_cron_profile_names(child, known))
    elif isinstance(value, list):
        for child in value:
            found.update(_cron_profile_names(child, known))
    return found


_REQUIRED_TABLES = frozenset({"multitenancy_routing", "multitenancy_sessions"})


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
            # routing/sessions are created by routing.py / sessions.py on every
            # real install; without them the database is empty, mid-restore or
            # foreign, and "no rows" would wrongly mark every profile orphaned.
            if not _REQUIRED_TABLES <= tables:
                return None
            for query in queries:
                table = query.split(" FROM ", 1)[1].split()[0]
                if table not in tables:
                    continue
                refs.update(str(row[0]) for row in conn.execute(query) if row[0])
    except sqlite3.Error:
        # An unreadable reference source is not proof that anything is orphaned.
        return None
    return refs


def _current_references(shared_home: Path, profiles: dict[str, Path]) -> set[str] | None:
    """Every profile name with a durable or live reference; None = fail closed."""
    known = set(profiles)
    refs = _db_references(shared_home)
    if refs is None:
        return None
    active_path = shared_home / "active_profile"
    if active_path.exists():
        try:
            active = active_path.read_text(encoding="utf-8").strip().lower()
        except (OSError, UnicodeDecodeError):
            return None
        if active in known:
            refs.add(active)
    # The router profile is infrastructure: it owns the Feishu websocket and has
    # no routing/session rows of its own. The operator shell may lack the env
    # override, so the default name is always kept too.
    refs.update({router_profile_name(), DEFAULT_ROUTER_PROFILE} & known)
    for name, profile in profiles.items():
        if (profile / ".keep").exists():
            refs.add(name)
            continue
        live = _live_pid(profile / "gateway.pid")
        if live is None:
            return None
        if live:
            refs.add(name)

    cron_paths = [shared_home / "cron" / "jobs.json"]
    cron_paths.extend(profile / "cron" / "jobs.json" for profile in profiles.values())
    for cron_path in cron_paths:
        if cron_path.is_file():
            try:
                refs.update(_cron_profile_names(json.loads(cron_path.read_text()), known))
            except (OSError, json.JSONDecodeError):
                return None
            if cron_path.parent.parent.name in known:
                refs.add(cron_path.parent.parent.name)
    return refs


def _scan_profiles(profiles_dir: Path) -> dict[str, Path]:
    return {
        entry.name: entry
        for entry in profiles_dir.iterdir()
        if entry.is_dir() and _PROFILE_ID_RE.match(entry.name) and not is_fixture_path(entry)
    }


def _services_running(shared_home: Path) -> bool:
    """A live (or unknowable) shared gateway means reference writers may be active."""
    return _live_pid(shared_home / "gateway.pid") is not False


def _plan(shared_home: Path, prefixes: tuple[str, ...] | None) -> tuple[QuarantineReport, dict[str, Path]]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = QuarantineReport(run_id=run_id)
    profiles_dir = shared_home / "profiles"
    if not profiles_dir.is_dir():
        return report, {}
    profiles = _scan_profiles(profiles_dir)
    refs = _current_references(shared_home, profiles)
    if refs is None:
        return report, profiles
    known = set(profiles)
    report.referenced = sorted(refs & known)
    candidates = sorted(known - refs)
    if prefixes is not None:
        candidates = [n for n in candidates if any(n.startswith(p) for p in prefixes)]
    report.candidates = candidates
    return report, profiles


def _apply(shared_home: Path, report: QuarantineReport, *, services_stopped: bool) -> QuarantineReport:
    """Move candidates only while no reference writer can run, re-checking first.

    Routing/session/cron writers (WebUI provisioning, gateway) share no lock with
    this tool, so an online move can race a freshly provisioned profile. Applying
    therefore requires the operator to declare services stopped AND the shared
    gateway pid to be provably dead; references are then recomputed right before
    each move so anything that appeared after the dry-run is kept.
    """
    if not report.candidates:
        return report
    if not services_stopped or _services_running(shared_home):
        raise RuntimeError(
            "refusing to quarantine while Hermes services may be running; "
            "stop gateway/WebUI and pass --services-stopped"
        )
    profiles_dir = shared_home / "profiles"
    destination = shared_home / "profile-quarantine" / report.run_id
    destination.mkdir(parents=True, exist_ok=False)
    for name in report.candidates:
        profiles = _scan_profiles(profiles_dir)
        if name not in profiles:
            continue
        refs = _current_references(shared_home, profiles)
        if refs is None:
            break
        if name in refs:
            continue
        shutil.move(str(profiles[name]), str(destination / name))
        report.quarantined.append(name)
    (destination / "manifest.json").write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def quarantine_orphan_profiles(
    shared_home: Path, *, apply: bool = False, services_stopped: bool = False
) -> QuarantineReport:
    """Plan or reversibly move profiles that have no durable/live reference.

    Default is dry-run. Any unreadable reference source makes the operation
    fail closed by returning no candidates.
    """
    shared_home = Path(shared_home).resolve()
    report, _ = _plan(shared_home, None)
    if not apply:
        return report
    return _apply(shared_home, report, services_stopped=services_stopped)


def quarantine_matching_orphans(
    shared_home: Path,
    *,
    prefixes: tuple[str, ...],
    apply: bool = False,
    services_stopped: bool = False,
) -> QuarantineReport:
    """Restrict a conservative orphan plan to operator-selected name prefixes."""
    shared_home = Path(shared_home).resolve()
    report, _ = _plan(shared_home, prefixes)
    if not apply:
        return report
    return _apply(shared_home, report, services_stopped=services_stopped)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit and reversibly quarantine orphan Hermes profiles")
    parser.add_argument("--home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--apply", action="store_true", help="move candidates into profile-quarantine")
    parser.add_argument(
        "--services-stopped",
        action="store_true",
        help="confirm gateway/WebUI are stopped; required with --apply",
    )
    parser.add_argument("--prefix", action="append", default=[], help="limit to profile name prefix")
    args = parser.parse_args(argv)
    if args.prefix:
        report = quarantine_matching_orphans(
            args.home,
            prefixes=tuple(args.prefix),
            apply=args.apply,
            services_stopped=args.services_stopped,
        )
    else:
        report = quarantine_orphan_profiles(
            args.home, apply=args.apply, services_stopped=args.services_stopped
        )
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
