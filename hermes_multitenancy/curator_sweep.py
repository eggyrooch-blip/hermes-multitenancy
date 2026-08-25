"""Per-tenant curator sweep.

The gateway's housekeeping loop polls the curator hourly, but it calls it
against the *gateway process's own* ``HERMES_HOME`` — in this deployment that
is ``profiles/multitenancy_router``.  So in a multitenancy install no tenant
profile ever gets curated: the engine is there, nobody dispatches it per
tenant.  This module is that missing dispatcher.

It walks the tenant profiles, scopes Hermes to one profile at a time (home
override + skill-loader redirect, the same pair ``run_broker`` uses), and lets
the upstream curator decide under its own gates whether a pass is due.

Runs as a standalone oneshot process (systemd timer) — never inside the
gateway event loop.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

DEFAULT_ACTIVITY_DAYS = 30

# Fields skill_usage stamps on a usage record; any of them inside the window
# counts as "this profile is still touching its skills".
_ACTIVITY_FIELDS = ("last_used_at", "last_viewed_at", "last_patched_at", "created_at")


def resolve_shared_home() -> Path:
    """Return the shared Hermes home that owns ``profiles/``."""
    for key in ("HERMES_SHARED_HOME", "HERMES_HOME"):
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        home = Path(raw).expanduser()
        # HERMES_HOME may already point INTO a profile (<shared>/profiles/<p>).
        if home.parent.name == "profiles":
            return home.parent.parent
        return home
    return Path.home() / ".hermes"


def iter_profile_homes(shared_home: Path) -> Iterator[Path]:
    root = Path(shared_home) / "profiles"
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and not entry.name.startswith("."):
            yield entry


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def eligibility(
    profile_home: Path,
    *,
    activity_days: int = DEFAULT_ACTIVITY_DAYS,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Whether *profile_home* is worth spending a curator pass on.

    The consolidation pass forks a real aux-model agent (minutes, not
    milliseconds), so sweeping every profile unconditionally would burn one
    agent run per tenant to discover there is nothing to curate.  A profile
    earns a pass by having agent-written skills, or by having touched its
    skills recently.
    """
    usage_path = Path(profile_home) / "skills" / ".usage.json"
    if not usage_path.is_file():
        return False, "no_usage_sidecar"
    try:
        records = json.loads(usage_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "unreadable_usage_sidecar"
    if not isinstance(records, dict):
        return False, "unreadable_usage_sidecar"

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=activity_days)
    # A stamp from the future (clock skew, corrupt sidecar) would otherwise keep
    # a profile eligible forever and burn an aux-model pass every week.
    horizon = now + timedelta(hours=1)
    recent = False
    for record in records.values():
        if not isinstance(record, dict):
            continue
        # Same marker skill_usage writes for curator/background-review authorship.
        if record.get("created_by") == "agent" or record.get("agent_created") is True:
            return True, "agent_created"
        if recent:
            continue
        for field in _ACTIVITY_FIELDS:
            stamp = _parse_iso(record.get(field))
            if stamp is not None and cutoff <= stamp <= horizon:
                recent = True
                break
    if recent:
        return True, f"skill_activity_within_{activity_days}d"
    return False, "no_agent_created_no_recent_activity"


def _scope_profile(profile_home: Path) -> tuple[Any, bool]:
    """Point Hermes at *profile_home*; return ``(undo, scoped)``.

    ``scoped`` is False unless BOTH halves landed. Callers MUST treat that as
    fail-closed and skip the profile: half-scoped is the cross-tenant bug, not
    a degraded mode. The two mutations are not interchangeable — the home
    override is what ``get_hermes_home()`` reads, while the skill loader caches
    ``SKILLS_DIR`` at import time, so without redirecting it too the curator
    would curate whichever profile happened to import the module first.
    """
    undo: list = []
    scoped = False
    try:
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )
    except ImportError:
        pass
    else:
        token = set_hermes_home_override(profile_home)
        undo.append(lambda: reset_hermes_home_override(token))
        scoped = True

    try:
        from . import router
    except Exception:
        scoped = False
    else:
        states = router._scope_profile_skill_loader(profile_home)
        if states:
            undo.append(lambda: router._restore_profile_skill_loader(states))
        # `_scope_profile_skill_loader` is best-effort and silently skips a
        # failed import, so a non-empty `states` does NOT prove the loader
        # moved. Verify BOTH mutations landed, exactly as run_broker does —
        # a home override without the SKILLS_DIR redirect still resolves
        # skills against the previously loaded profile.
        mutated = {attr for (_m, attr, _old, _had) in states}
        if not ("SKILLS_DIR" in mutated and "_skill_commands" in mutated):
            scoped = False

    def _undo() -> None:
        # Unwind in reverse; one failure must not strand the others, otherwise
        # a leaked override would silently curate the next tenant's skills
        # against this tenant's home.
        for fn in reversed(undo):
            try:
                fn()
            except Exception:
                pass

    return _undo, scoped


def _state_evidence(profile_home: Path) -> dict[str, Any]:
    """Read back what the pass persisted — run_count is the proof it happened."""
    state_path = Path(profile_home) / "skills" / ".curator_state"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict):
        return {}
    return {
        "run_count": state.get("run_count"),
        "report_path": state.get("last_report_path"),
        "summary": str(state.get("last_run_summary") or "")[:400],
    }


def run_profile(profile_home: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Run one gated curator pass for *profile_home*. Never raises."""
    name = Path(profile_home).name
    started = time.monotonic()
    row: dict[str, Any] = {"profile": name, "ran": False}
    if dry_run:
        # Return BEFORE touching the curator at all: should_run_now() seeds
        # `.curator_state` the first time it observes a profile, so asking it
        # anything would leave a write behind on every never-curated tenant —
        # not what a preview is allowed to do.
        row["skipped"] = "dry_run"
        row["duration_seconds"] = round(time.monotonic() - started, 3)
        return row
    undo, scoped = _scope_profile(profile_home)
    try:
        if not scoped:
            row["error"] = "scope_failed: hermes home override unavailable"
            return row
        from agent import curator

        # NOT maybe_run_curator(): it delegates to run_curator_review() with
        # synchronous=False, so the LLM pass runs on a daemon thread — fine for
        # the long-lived gateway, fatal for a oneshot sweep that exits and
        # kills it mid-review. should_run_now() is the same gate set
        # (enabled / paused / interval, plus first-run seeding); the idle gate
        # maybe_run_curator adds is a no-op for us since a timer-launched
        # sweep is by definition not competing with a live turn.
        if not curator.should_run_now():
            # Name WHICH gate closed — "paused" is an operator decision worth
            # surfacing, "not due" is just the weekly cadence working.
            if not curator.is_enabled():
                row["skipped"] = "curator_disabled"
            elif curator.is_paused():
                row["skipped"] = "curator_paused"
            else:
                row["skipped"] = "curator_interval_not_due"
            return row
        curator.run_curator_review(synchronous=True)
        row["ran"] = True
        # run_curator_review only returns the pre-LLM snapshot; the pass's real
        # outcome is what it persisted, so read that back as the evidence line.
        row.update(_state_evidence(profile_home))
    except Exception as exc:  # one bad profile must not end the sweep
        row["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        undo()
        row["duration_seconds"] = round(time.monotonic() - started, 3)
    return row


def _log_path(shared_home: Path, now: datetime) -> Path:
    return (
        Path(shared_home)
        / "logs"
        / "curator-sweep"
        / f"{now.strftime('%Y%m%d')}.jsonl"
    )


def sweep(
    *,
    shared_home: Path | None = None,
    activity_days: int = DEFAULT_ACTIVITY_DAYS,
    dry_run: bool = False,
    only: str | None = None,
    limit: int | None = None,
    log_path: Path | None = None,
    stream=None,
) -> dict[str, Any]:
    """Walk tenant profiles, curate the eligible ones, return the totals."""
    stream = stream if stream is not None else sys.stdout
    shared = Path(shared_home) if shared_home else resolve_shared_home()
    now = datetime.now(timezone.utc)
    log_file = log_path if log_path is not None else _log_path(shared, now)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        log_file = None

    totals = {"scanned": 0, "eligible": 0, "ran": 0, "errors": 0}
    rows: list[dict[str, Any]] = []
    for profile_home in iter_profile_homes(shared):
        if only and profile_home.name != only:
            continue
        totals["scanned"] += 1
        ok, reason = eligibility(profile_home, activity_days=activity_days, now=now)
        row: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "profile": profile_home.name,
            "eligible": ok,
            "reason": reason,
            "ran": False,
        }
        if ok:
            totals["eligible"] += 1
            row.update(run_profile(profile_home, dry_run=dry_run))
        else:
            # Every logged row answers "ran or skipped, and why" — the
            # ineligible majority included, or an audit of the JSONL can't
            # tell "skipped on purpose" from "never looked at".
            row["skipped"] = reason
            row["duration_seconds"] = 0.0
        row.setdefault("duration_seconds", 0.0)
        if row.get("ran"):
            totals["ran"] += 1
        if row.get("error"):
            totals["errors"] += 1
        rows.append(row)
        print(
            f"{row['profile']} eligible={row['eligible']} reason={row['reason']}"
            + (" ran=1" if row.get("ran") else "")
            + (f" error={row['error']}" if row.get("error") else ""),
            file=stream,
        )
        if log_file is not None:
            try:
                with log_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError:
                log_file = None
        if limit is not None and totals["eligible"] >= limit:
            break

    summary = (
        f"scanned={totals['scanned']} eligible={totals['eligible']} "
        f"ran={totals['ran']} errors={totals['errors']}"
    )
    print(summary, file=stream)
    totals["rows"] = rows
    totals["summary"] = summary
    return totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes_multitenancy.curator_sweep",
        description="Run the Hermes curator once per eligible tenant profile.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the eligibility decision per profile; never invoke the curator.",
    )
    parser.add_argument(
        "--activity-days",
        type=int,
        default=DEFAULT_ACTIVITY_DAYS,
        help=f"Skill-activity window that makes a profile eligible (default {DEFAULT_ACTIVITY_DAYS}).",
    )
    parser.add_argument("--profile", dest="only", help="Sweep only this profile.")
    parser.add_argument(
        "--limit", type=int, help="Stop after this many eligible profiles."
    )
    args = parser.parse_args(argv)
    if args.activity_days < 1:
        parser.error("--activity-days must be >= 1")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")

    totals = sweep(
        activity_days=args.activity_days,
        dry_run=args.dry_run,
        only=args.only,
        limit=args.limit,
    )
    return 1 if totals["errors"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
