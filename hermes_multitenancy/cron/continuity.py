"""Owner-bound continuity using the upstream profile-local notepad store."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


def scoped_job(job: dict, profile_home: Path) -> tuple[dict, str | None]:
    refs = job.get("context_from") or []
    refs = [refs] if isinstance(refs, str) else refs
    enabled = bool(refs) or job.get("continuity") is True
    if not enabled:
        return job, None
    job_id = str(job.get("id") or "").strip()
    if not job_id or not isinstance(refs, list) or any(
        str(ref).strip().lower() != "self" and ref != job_id for ref in refs
    ):
        raise ValueError("cron continuity requires this job's own context")
    home = profile_home.resolve()
    profile = str(job.get("owner_profile") or "").strip()
    owner = str(job.get("owner_open_id") or "").strip()
    if home.name != profile or home.parent.name != "profiles" or not owner.startswith("ou_"):
        raise ValueError("cron continuity owner/profile binding is unavailable")
    # A persisted job field is a claim, not authority. Require exactly one live
    # route for this profile and never infer ownership from prompt/session env.
    try:
        with sqlite3.connect(f"file:{home.parent.parent / 'multitenancy.db'}?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT open_id, owner_open_id, kind FROM multitenancy_routing "
                "WHERE profile_name=? AND active=1", (profile,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError("cron continuity route is unavailable") from exc
    if len(rows) != 1 or rows[0][2] != "user" or (rows[0][1] or rows[0][0]) != owner:
        raise ValueError("cron continuity owner route is unavailable or ambiguous")
    from cron import notepad
    if notepad._current_notepad_file().resolve() != home / "cron" / "notepad.db":
        raise ValueError("cron continuity notepad profile mismatch")
    scope = "mt-" + hashlib.sha256(json.dumps([owner, str(home), job_id]).encode()).hexdigest()
    # The upstream renderer swallows storage errors; admission must not.
    notepad.get_note(scope, "previous_success")
    # Only the prompt's scratchpad identity changes; RunRequest/delivery/session
    # identity remains the original job. Never import unbound legacy output.
    return {**job, "id": scope, "context_from": []}, scope


def remember_success(scope: str | None, content: str, *, completed: bool = True) -> None:
    text = str(content or "").strip()
    if not scope or not completed or not text or "[SILENT]" in text.upper():
        return
    from cron import notepad
    # One bounded previous result; no accumulating transcript or new storage.
    text = text.encode("utf-8")[:8000].decode("utf-8", errors="ignore")
    notepad.set_note(scope, "previous_success", text)
