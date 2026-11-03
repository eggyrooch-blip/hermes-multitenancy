"""Per-tenant curator sweep: eligibility, scope hygiene, fault isolation."""
from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from hermes_multitenancy import curator_sweep

NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)


def _profile(shared: Path, name: str, usage: dict | None = None) -> Path:
    home = shared / "profiles" / name
    (home / "skills").mkdir(parents=True)
    if usage is not None:
        (home / "skills" / ".usage.json").write_text(
            json.dumps(usage), encoding="utf-8"
        )
    return home


def _iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


# ── eligibility ────────────────────────────────────────────────────────────

def test_no_usage_sidecar_is_not_eligible(tmp_path: Path):
    home = _profile(tmp_path, "alice")
    assert curator_sweep.eligibility(home, now=NOW) == (False, "no_usage_sidecar")


def test_agent_created_skill_makes_profile_eligible(tmp_path: Path):
    home = _profile(
        tmp_path,
        "bob",
        {"lark-pitfalls": {"created_by": "agent", "last_used_at": _iso(400)}},
    )
    ok, reason = curator_sweep.eligibility(home, now=NOW)
    assert (ok, reason) == (True, "agent_created")


def test_recent_activity_makes_profile_eligible(tmp_path: Path):
    home = _profile(tmp_path, "carol", {"docx": {"last_used_at": _iso(3)}})
    ok, reason = curator_sweep.eligibility(home, now=NOW)
    assert ok and reason == "skill_activity_within_30d"


def test_cold_profile_is_not_eligible(tmp_path: Path):
    """The whole point of the gate: no agent-written skills, no recent touch."""
    home = _profile(tmp_path, "dave", {"docx": {"last_used_at": _iso(120)}})
    assert curator_sweep.eligibility(home, now=NOW) == (
        False,
        "no_agent_created_no_recent_activity",
    )


def test_unreadable_sidecar_is_not_eligible(tmp_path: Path):
    home = _profile(tmp_path, "erin")
    (home / "skills" / ".usage.json").write_text("{not json", encoding="utf-8")
    assert curator_sweep.eligibility(home, now=NOW) == (
        False,
        "unreadable_usage_sidecar",
    )


# ── scope hygiene ──────────────────────────────────────────────────────────

def _install_fake_curator(monkeypatch, *, should_run=True, boom=False, calls=None):
    calls = calls if calls is not None else []

    def _should_run_now():
        return should_run

    def _run_curator_review(**kwargs):
        calls.append(kwargs)
        if boom:
            raise RuntimeError("aux model unreachable")
        return {"summary_so_far": "no changes"}

    curator = ModuleType("agent.curator")
    curator.should_run_now = _should_run_now
    curator.run_curator_review = _run_curator_review
    curator.is_enabled = lambda: True
    curator.is_paused = lambda: False
    agent_pkg = sys.modules.get("agent") or ModuleType("agent")
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setattr(agent_pkg, "curator", curator, raising=False)
    monkeypatch.setitem(sys.modules, "agent.curator", curator)
    return calls


def test_scope_is_undone_even_when_the_review_explodes(monkeypatch, tmp_path: Path):
    """A leaked home override would curate the NEXT tenant against this one's home."""
    home = _profile(tmp_path, "frank", {"x": {"created_by": "agent"}})
    undone = []
    monkeypatch.setattr(
        curator_sweep, "_scope_profile", lambda p: (lambda: undone.append(p), True)
    )
    _install_fake_curator(monkeypatch, boom=True)

    row = curator_sweep.run_profile(home)

    assert undone == [home]
    assert "aux model unreachable" in row["error"]
    assert row["ran"] is False


def test_dry_run_never_touches_the_curator(monkeypatch, tmp_path: Path):
    """Even should_run_now() is off-limits — it SEEDS .curator_state on first sight."""
    home = _profile(tmp_path, "grace", {"x": {"created_by": "agent"}})
    scoped = []
    monkeypatch.setattr(
        curator_sweep,
        "_scope_profile",
        lambda p: scoped.append(p) or (lambda: None, True),
    )
    calls = _install_fake_curator(monkeypatch)
    gate_asked = []
    sys.modules["agent.curator"].should_run_now = lambda: gate_asked.append(1) or True

    row = curator_sweep.run_profile(home, dry_run=True)

    assert (calls, gate_asked, scoped) == ([], [], [])
    assert row["ran"] is False and row["skipped"] == "dry_run"
    assert not (home / "skills" / ".curator_state").exists()


def test_upstream_gate_is_respected(monkeypatch, tmp_path: Path):
    """paused / disabled / not-due profiles cost zero aux-model calls."""
    home = _profile(tmp_path, "heidi", {"x": {"created_by": "agent"}})
    monkeypatch.setattr(curator_sweep, "_scope_profile", lambda p: (lambda: None, True))
    calls = _install_fake_curator(monkeypatch, should_run=False)

    row = curator_sweep.run_profile(home)

    assert calls == []
    assert row["skipped"] == "curator_interval_not_due"


def test_review_runs_synchronously(monkeypatch, tmp_path: Path):
    """A oneshot sweep exits immediately; a backgrounded review would be killed."""
    home = _profile(tmp_path, "ivan", {"x": {"created_by": "agent"}})
    (home / "skills" / ".curator_state").write_text(
        json.dumps({"run_count": 3, "last_run_summary": "auto: no changes"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(curator_sweep, "_scope_profile", lambda p: (lambda: None, True))
    calls = _install_fake_curator(monkeypatch)

    row = curator_sweep.run_profile(home)

    assert calls == [{"synchronous": True}]
    assert row["ran"] is True
    assert row["run_count"] == 3
    assert row["summary"] == "auto: no changes"


# ── sweep ──────────────────────────────────────────────────────────────────

def test_sweep_skips_cold_profiles_and_survives_a_bad_one(monkeypatch, tmp_path: Path):
    _profile(tmp_path, "cold", {"docx": {"last_used_at": _iso(120)}})
    _profile(tmp_path, "hot1", {"x": {"created_by": "agent"}})
    _profile(tmp_path, "hot2", {"y": {"created_by": "agent"}})

    monkeypatch.setattr(curator_sweep, "_scope_profile", lambda p: (lambda: None, True))

    seen: list[str] = []

    def _fake_run(profile_home, *, dry_run=False):
        seen.append(profile_home.name)
        if profile_home.name == "hot1":
            return {"profile": "hot1", "ran": False, "error": "RuntimeError: boom"}
        return {"profile": profile_home.name, "ran": True}

    monkeypatch.setattr(curator_sweep, "run_profile", _fake_run)

    out = io.StringIO()
    totals = curator_sweep.sweep(shared_home=tmp_path, stream=out)

    # cold never reaches run_profile → zero aux-model spend on it
    assert seen == ["hot1", "hot2"]
    assert totals["scanned"] == 3
    assert totals["eligible"] == 2
    assert totals["ran"] == 1
    assert totals["errors"] == 1
    assert "scanned=3 eligible=2 ran=1 errors=1" in out.getvalue()


def test_sweep_writes_one_jsonl_row_per_profile(monkeypatch, tmp_path: Path):
    _profile(tmp_path, "cold", None)
    _profile(tmp_path, "hot", {"x": {"created_by": "agent"}})
    monkeypatch.setattr(curator_sweep, "_scope_profile", lambda p: (lambda: None, True))
    monkeypatch.setattr(
        curator_sweep,
        "run_profile",
        lambda home, dry_run=False: {"profile": home.name, "ran": True},
    )
    log = tmp_path / "sweep.jsonl"

    curator_sweep.sweep(shared_home=tmp_path, log_path=log, stream=io.StringIO())

    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [(r["profile"], r["eligible"]) for r in rows] == [
        ("cold", False),
        ("hot", True),
    ]


def test_shared_home_resolves_from_a_profile_scoped_env(monkeypatch, tmp_path: Path):
    """The gateway exports HERMES_HOME=<shared>/profiles/<p>; walk back up."""
    profile = tmp_path / "profiles" / "sunke"
    profile.mkdir(parents=True)
    monkeypatch.delenv("HERMES_SHARED_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    assert curator_sweep.resolve_shared_home() == tmp_path


def test_future_timestamp_is_not_activity(tmp_path: Path):
    """Clock skew or a corrupt sidecar must not buy a weekly aux-model pass."""
    home = _profile(
        tmp_path, "skew", {"docx": {"last_used_at": "2099-01-01T00:00:00+00:00"}}
    )
    assert curator_sweep.eligibility(home, now=NOW) == (
        False,
        "no_agent_created_no_recent_activity",
    )


def test_fail_closed_when_the_home_override_is_unavailable(monkeypatch, tmp_path: Path):
    """No override → the curator would run against the ambient home. Refuse."""
    home = _profile(tmp_path, "judy", {"x": {"created_by": "agent"}})
    monkeypatch.setattr(curator_sweep, "_scope_profile", lambda p: (lambda: None, False))
    calls = _install_fake_curator(monkeypatch)

    row = curator_sweep.run_profile(home)

    assert calls == []
    assert row["ran"] is False
    assert "scope_failed" in row["error"]


def test_gate_skip_names_which_gate_closed(monkeypatch, tmp_path: Path):
    home = _profile(tmp_path, "ken", {"x": {"created_by": "agent"}})
    monkeypatch.setattr(curator_sweep, "_scope_profile", lambda p: (lambda: None, True))
    _install_fake_curator(monkeypatch, should_run=False)
    curator = sys.modules["agent.curator"]
    curator.is_enabled = lambda: True
    curator.is_paused = lambda: True

    assert curator_sweep.run_profile(home)["skipped"] == "curator_paused"

    curator.is_paused = lambda: False
    assert curator_sweep.run_profile(home)["skipped"] == "curator_interval_not_due"


def test_every_logged_row_carries_ran_and_duration(monkeypatch, tmp_path: Path):
    """Done line: 逐行记录 ran / skipped+原因 — including the ineligible majority."""
    _profile(tmp_path, "cold", None)
    _profile(tmp_path, "hot", {"x": {"created_by": "agent"}})
    monkeypatch.setattr(curator_sweep, "_scope_profile", lambda p: (lambda: None, True))
    monkeypatch.setattr(
        curator_sweep,
        "run_profile",
        lambda home, dry_run=False: {
            "profile": home.name,
            "ran": True,
            "duration_seconds": 1.0,
        },
    )
    log = tmp_path / "sweep.jsonl"

    curator_sweep.sweep(shared_home=tmp_path, log_path=log, stream=io.StringIO())

    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert all("ran" in r and "duration_seconds" in r for r in rows)
    cold = next(r for r in rows if r["profile"] == "cold")
    assert cold["skipped"] == "no_usage_sidecar"


@pytest.mark.parametrize(
    "argv",
    [
        ["--activity-days", "0"],
        ["--activity-days", "-1"],
        ["--limit", "0"],
        ["--limit", "-5"],
    ],
)
def test_out_of_range_arguments_are_rejected(argv):
    """A cron typo like --activity-days -1 would mark nearly everything eligible."""
    with pytest.raises(SystemExit) as exc:
        curator_sweep.main(argv)
    assert exc.value.code == 2


def _install_fake_hermes_constants(monkeypatch):
    """hermes_constants ships with hermes-agent, not this repo — stub it."""
    mod = ModuleType("hermes_constants")
    mod.set_hermes_home_override = lambda home: "tok"
    mod.reset_hermes_home_override = lambda tok: None
    monkeypatch.setitem(sys.modules, "hermes_constants", mod)


def _fake_loader_states(*attrs):
    """Mimic router._scope_profile_skill_loader's (module, attr, old, had) rows."""
    return [(None, a, None, False) for a in attrs]


@pytest.mark.parametrize(
    "mutated",
    [(), ("SKILLS_DIR",), ("_skill_commands",)],
    ids=["loader-skipped", "dir-only", "commands-only"],
)
def test_partial_skill_loader_scope_is_not_scoped(monkeypatch, tmp_path: Path, mutated):
    """Home override without the SKILLS_DIR redirect = curating the last tenant."""
    from hermes_multitenancy import router

    _install_fake_hermes_constants(monkeypatch)
    monkeypatch.setattr(
        router, "_scope_profile_skill_loader", lambda home: _fake_loader_states(*mutated)
    )
    monkeypatch.setattr(router, "_restore_profile_skill_loader", lambda states: None)

    undo, scoped = curator_sweep._scope_profile(tmp_path / "profiles" / "leo")
    undo()

    assert scoped is False


def test_both_halves_scoped_is_scoped(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import router

    _install_fake_hermes_constants(monkeypatch)
    monkeypatch.setattr(
        router,
        "_scope_profile_skill_loader",
        lambda home: _fake_loader_states("SKILLS_DIR", "_skill_commands"),
    )
    monkeypatch.setattr(router, "_restore_profile_skill_loader", lambda states: None)

    undo, scoped = curator_sweep._scope_profile(tmp_path / "profiles" / "mia")
    undo()

    assert scoped is True
