from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from hermes_multitenancy import cron_worker


class FakeCronJobs:
    def __init__(self) -> None:
        self.HERMES_DIR = Path("/original/hermes")
        self.CRON_DIR = self.HERMES_DIR / "cron"
        self.JOBS_FILE = self.CRON_DIR / "jobs.json"
        self.OUTPUT_DIR = self.CRON_DIR / "output"


class DueScheduler:
    def __init__(self, due_by_profile: dict[str, list[dict]]) -> None:
        self.due_by_profile = due_by_profile
        self._hermes_home = Path("/original/hermes")
        self._LOCK_DIR = self._hermes_home / "cron"
        self._LOCK_FILE = self._LOCK_DIR / ".tick.lock"
        self.advanced: list[tuple[str, str]] = []

    def get_due_jobs(self) -> list[dict]:
        profile = Path(os.environ["HERMES_HOME"]).name
        return [dict(job) for job in self.due_by_profile.get(profile, [])]

    def advance_next_run(self, job_id: str) -> bool:
        self.advanced.append((Path(os.environ["HERMES_HOME"]).name, job_id))
        return True


class RecordingExecutor:
    def __init__(self) -> None:
        self.submissions: list[tuple[object, tuple, dict]] = []

    def submit(self, fn, *args, **kwargs):
        import concurrent.futures

        future: concurrent.futures.Future = concurrent.futures.Future()
        self.submissions.append((fn, args, kwargs))
        return future


def _profile(root: Path, name: str) -> Path:
    profile = root / name
    (profile / "cron").mkdir(parents=True)
    (profile / "cron" / "jobs.json").write_text("[]", encoding="utf-8")
    return profile


def _install_fake_cron_modules(monkeypatch, profile: Path) -> None:
    cron_pkg = ModuleType("cron")
    cron_jobs = ModuleType("cron.jobs")
    cron_scheduler = ModuleType("cron.scheduler")
    cron_jobs.HERMES_DIR = Path("/original/hermes")
    cron_jobs.CRON_DIR = cron_jobs.HERMES_DIR / "cron"
    cron_jobs.JOBS_FILE = cron_jobs.CRON_DIR / "jobs.json"
    cron_jobs.OUTPUT_DIR = cron_jobs.CRON_DIR / "output"
    cron_scheduler._hermes_home = Path("/original/hermes")
    cron_scheduler._LOCK_DIR = cron_scheduler._hermes_home / "cron"
    cron_scheduler._LOCK_FILE = cron_scheduler._LOCK_DIR / ".tick.lock"
    cron_pkg.jobs = cron_jobs
    cron_pkg.scheduler = cron_scheduler
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)
    monkeypatch.setitem(sys.modules, "cron.scheduler", cron_scheduler)
    monkeypatch.setenv("HERMES_HOME", str(profile))


def test_infer_cron_owner_context_tags_expert_jobs_and_regresses_without_fixed_expert(
    tmp_path, monkeypatch
):
    profile = _profile(tmp_path / "profiles", "alice")
    monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", "ou_owner")
    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT", "expert-123")
    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT_APP_ID", "cli_a123")

    tagged = cron_worker.infer_cron_owner_context({}, profile_home=profile)

    # expert_id is NOT persisted at create time: the create subprocess is
    # deliberately FIXED_EXPERT-free (subprocess allowlist), so it can only tag
    # the source_app LABEL + writable authorization. The trusted expert_id is
    # derived at execute time from the owning gateway env (see
    # _expert_id_for_cron_job), never from a spoofable job field.
    assert tagged == {
        "owner_open_id": "ou_owner",
        "owner_profile": "alice",
        "source_app": "cli_a123",
        "writable_authorized": True,
    }

    # Tagging is gated on the app-id label (what the create subprocess actually
    # sees), so the non-expert regression case must clear APP_ID, not FIXED_EXPERT.
    monkeypatch.delenv("HERMES_MULTITENANCY_FIXED_EXPERT", raising=False)
    monkeypatch.delenv("HERMES_MULTITENANCY_FIXED_EXPERT_APP_ID", raising=False)
    plain = cron_worker.infer_cron_owner_context({}, profile_home=profile)

    assert plain == {
        "owner_open_id": "ou_owner",
        "owner_profile": "alice",
    }


def test_job_partition_and_scan_submit_only_the_owning_gateway(tmp_path, monkeypatch):
    profile = _profile(tmp_path / "profiles", "alice")
    monkeypatch.setattr(cron_worker, "backfill_cron_owner_context_for_profile", lambda _p: None)
    monkeypatch.setenv("HERMES_HOME", str(profile))

    untagged = {"id": "plain"}
    tagged = {"id": "expert", "source_app": "cli_expert"}
    other = {"id": "other", "source_app": "cli_other"}

    monkeypatch.delenv("HERMES_MULTITENANCY_FIXED_EXPERT", raising=False)
    monkeypatch.delenv("HERMES_MULTITENANCY_FIXED_EXPERT_APP_ID", raising=False)
    assert cron_worker._job_belongs_to_this_gateway(untagged) is True
    assert cron_worker._job_belongs_to_this_gateway(tagged) is False

    router_scheduler = DueScheduler({"alice": [tagged]})
    router_executor = RecordingExecutor()
    router_submitted = cron_worker._scan_and_submit_due_profile_jobs(
        FakeCronJobs(),
        router_scheduler,
        profile.parent,
        {"alice"},
        adapters=None,
        loop=SimpleNamespace(is_running=lambda: False),
        patch_lock=threading.Lock(),
        executor=router_executor,
        in_flight=set(),
        runner=lambda _profile_home, _job: None,
    )
    assert router_submitted == 0
    assert router_scheduler.advanced == []
    assert router_executor.submissions == []

    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT", "expert-123")
    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT_APP_ID", "cli_expert")
    assert cron_worker._job_belongs_to_this_gateway(tagged) is True
    assert cron_worker._job_belongs_to_this_gateway(untagged) is False
    assert cron_worker._job_belongs_to_this_gateway(other) is False

    expert_scheduler = DueScheduler({"alice": [tagged]})
    expert_executor = RecordingExecutor()
    expert_submitted = cron_worker._scan_and_submit_due_profile_jobs(
        FakeCronJobs(),
        expert_scheduler,
        profile.parent,
        {"alice"},
        adapters=None,
        loop=SimpleNamespace(is_running=lambda: False),
        patch_lock=threading.Lock(),
        executor=expert_executor,
        in_flight=set(),
        runner=lambda _profile_home, _job: None,
    )
    assert expert_submitted == 1
    assert expert_scheduler.advanced == [("alice", "expert")]
    assert [(args[0].name, args[1]["id"]) for _fn, args, _kwargs in expert_executor.submissions] == [
        ("alice", "expert")
    ]


def test_build_cron_run_request_threads_expert_id_into_event_metadata(tmp_path, monkeypatch):
    from hermes_multitenancy import agent_real

    profile = _profile(tmp_path / "profiles", "alice")
    # expert_id is derived from the OWNING gateway's trusted env at execute time,
    # gated on the job's source_app label — a spoofed job["expert_id"] is ignored.
    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT", "expert-123")
    request = cron_worker._build_cron_run_request(
        {
            "id": "job1",
            "name": "Daily",
            "prompt": "hi",
            "deliver": "feishu",
            "owner_profile": "alice",
            "owner_open_id": "ou_owner",
            "source_app": "cli_expert",
            "expert_id": "spoofed-ignored",
        },
        profile_home=profile,
        prompt="run now",
    )

    assert request.metadata["expert_id"] == "expert-123"
    assert agent_real._expert_id_for_event(cron_worker._build_cron_event(request)) == "expert-123"


def test_webui_expert_job_revalidates_catalog_before_run_request(tmp_path, monkeypatch):
    from hermes_multitenancy import expert_overlay

    profile = _profile(tmp_path / "profiles", "alice")
    visible = {"value": True}
    monkeypatch.setattr(
        expert_overlay,
        "resolve_caller_departments",
        lambda profile_home, **kwargs: ["42"],
    )
    monkeypatch.setattr(
        expert_overlay,
        "resolve_expert",
        lambda profile_home, expert_id, **kwargs: (
            SimpleNamespace(expert_id=expert_id) if visible["value"] else None
        ),
    )
    dependency_checks = []
    monkeypatch.setattr(
        cron_worker,
        "_assert_webui_expert_cron_dependencies",
        lambda job, **kwargs: dependency_checks.append((job, kwargs)),
    )
    job = {
        "id": "job1",
        "name": "Daily",
        "deliver": "feishu",
        "owner_profile": "alice",
        "owner_open_id": "ou_owner",
        "expert_id": "expert-webui",
    }

    request = cron_worker._build_cron_run_request(job, profile_home=profile, prompt="run now")

    assert request.metadata["expert_id"] == "expert-webui"
    assert request.profile_name == "alice"
    assert request.user_key == "ou_owner"
    assert request.session_id == "cron:job1"
    assert request.delivery_mode == "feishu"
    assert dependency_checks[0][1]["profile_name"] == "alice"
    assert dependency_checks[0][1]["user_key"] == "ou_owner"

    visible["value"] = False
    with pytest.raises(ValueError, match="cron expert is no longer available"):
        cron_worker._build_cron_run_request(job, profile_home=profile, prompt="must not run")


def test_webui_expert_job_preflights_delivery_and_session_mirror(tmp_path, monkeypatch):
    from hermes_multitenancy import router

    profile = _profile(tmp_path / "profiles", "alice")
    reads = []
    store = SimpleNamespace(count=lambda profile_name, user_key: reads.append((profile_name, user_key)))
    monkeypatch.setattr(cron_worker, "_cron_delivery_identity_is_bound", lambda job, target: True)
    monkeypatch.setattr(router, "_history_key", lambda *a, **k: ("alice", "ou_owner"))
    monkeypatch.setattr(router, "_get_session_store", lambda: store)
    job = {
        "id": "job1",
        "deliver": "feishu",
        "owner_profile": "alice",
        "owner_open_id": "ou_owner",
        "expert_id": "expert-webui",
    }

    cron_worker._assert_webui_expert_cron_dependencies(
        job,
        profile_home=profile,
        profile_name="alice",
        user_key="ou_owner",
    )
    assert reads == [("alice", "ou_owner")]

    monkeypatch.setattr(cron_worker, "_cron_delivery_identity_is_bound", lambda job, target: False)
    with pytest.raises(ValueError, match="delivery identity"):
        cron_worker._assert_webui_expert_cron_dependencies(
            job,
            profile_home=profile,
            profile_name="alice",
            user_key="ou_owner",
        )

    monkeypatch.setattr(cron_worker, "_cron_delivery_identity_is_bound", lambda job, target: True)
    monkeypatch.setattr(router, "_get_session_store", lambda: None)
    with pytest.raises(ValueError, match="session mirror"):
        cron_worker._assert_webui_expert_cron_dependencies(
            job,
            profile_home=profile,
            profile_name="alice",
            user_key="ou_owner",
        )

    with pytest.raises(ValueError, match="must use feishu"):
        cron_worker._assert_webui_expert_cron_dependencies(
            {**job, "deliver": "local"},
            profile_home=profile,
            profile_name="alice",
            user_key="ou_owner",
        )


def test_webui_expert_job_never_falls_back_around_run_broker(monkeypatch):
    calls = []
    scheduler = SimpleNamespace(run_job=lambda job: calls.append(job))
    monkeypatch.setattr(cron_worker, "_l4_check_needs_reauth_and_defer", lambda _job: None)
    monkeypatch.setattr(cron_worker, "_cron_run_broker_enabled", lambda: False)

    result = cron_worker._run_cron_job_body({"id": "job1", "expert_id": "expert-webui"}, scheduler)

    assert result == (False, "", "", "scheduled expert execution requires RunBroker")
    assert calls == []


def test_run_job_for_profile_current_process_temporarily_disables_readonly_for_authorized_jobs(
    tmp_path, monkeypatch
):
    from hermes_multitenancy.expert_bot_route import feishu_expert_readonly_enabled

    profile = _profile(tmp_path / "profiles", "alice")
    _install_fake_cron_modules(monkeypatch, profile)
    monkeypatch.setenv("HERMES_MULTITENANCY_FEISHU_EXPERT_READONLY", "1")
    # A source_app job force-audits; give it a writable path so it isn't fail-closed.
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(cron_worker, "_install_cron_subprocess_runtime_pool", lambda: None)

    states: list[bool] = []

    def fake_run(job, _scheduler):
        states.append(feishu_expert_readonly_enabled())
        return True, f"output:{job['id']}", "visible", None

    monkeypatch.setattr(cron_worker, "_run_cron_job_body_with_cleanup", fake_run)

    # Expert-routed (source_app) AND create-time authorized → readonly dropped.
    writable = cron_worker._run_job_for_profile_current_process(
        profile,
        {"id": "job1", "name": "Daily", "source_app": "cli_expert", "writable_authorized": True},
    )
    assert writable["success"] is True
    assert states == [False]
    assert os.environ["HERMES_MULTITENANCY_FEISHU_EXPERT_READONLY"] == "1"

    # writable_authorized WITHOUT source_app (dirty/untagged) must NOT drop readonly.
    states.clear()
    untagged = cron_worker._run_job_for_profile_current_process(
        profile,
        {"id": "jobU", "name": "Daily", "writable_authorized": True},
    )
    assert untagged["success"] is True
    assert states == [True]
    assert os.environ["HERMES_MULTITENANCY_FEISHU_EXPERT_READONLY"] == "1"

    states.clear()
    readonly = cron_worker._run_job_for_profile_current_process(
        profile,
        {"id": "job2", "name": "Daily"},
    )
    assert readonly["success"] is True
    assert states == [True]
    assert os.environ["HERMES_MULTITENANCY_FEISHU_EXPERT_READONLY"] == "1"


def test_expert_gateway_scan_submits_source_out_job_and_delivery_targets_owner(tmp_path, monkeypatch):
    profile = _profile(tmp_path / "profiles", "alice")
    job = {
        "id": "job1",
        "name": "Daily",
        "owner_open_id": "ou_owner",
        "owner_profile": "alice",
        "source_app": "cli_expert",
        "expert_id": "expert-123",
    }

    monkeypatch.setattr(cron_worker, "backfill_cron_owner_context_for_profile", lambda _p: None)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT", "expert-123")
    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT_APP_ID", "cli_expert")

    scheduler = DueScheduler({"alice": [job]})
    executor = RecordingExecutor()
    submitted = cron_worker._scan_and_submit_due_profile_jobs(
        FakeCronJobs(),
        scheduler,
        profile.parent,
        {"alice"},
        adapters=None,
        loop=SimpleNamespace(is_running=lambda: False),
        patch_lock=threading.Lock(),
        executor=executor,
        in_flight=set(),
        runner=lambda _profile_home, _job: None,
    )

    assert submitted == 1
    assert scheduler.advanced == [("alice", "job1")]
    assert cron_worker._cron_deliver_target(job, "ou_owner") == {
        "platform": "feishu",
        "chat_id": "ou_owner",
        "thread_id": None,
    }


def test_scheduled_execution_appends_security_audit_with_hashed_owner(tmp_path, monkeypatch):
    profile = _profile(tmp_path / "profiles", "alice")
    audit_path = tmp_path / "audit.jsonl"
    _install_fake_cron_modules(monkeypatch, profile)
    # Gate explicitly OFF — the expert (source_app) writable audit is a required
    # compensating control and must still land via force= (SPEC guardrail: 每次
    # scheduled 执行落审计; operator cannot silence the control that authorizes it).
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_ENABLED", "0")
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))
    # Trusted expert_id is derived from the owning gateway env, gated on source_app.
    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT", "expert-123")
    monkeypatch.setattr(cron_worker, "_install_cron_subprocess_runtime_pool", lambda: None)
    monkeypatch.setattr(
        cron_worker,
        "_run_cron_job_body_with_cleanup",
        lambda job, _scheduler: (True, f"output:{job['id']}", "visible", None),
    )

    result = cron_worker._run_job_for_profile_current_process(
        profile,
        {
            "id": "job1",
            "name": "Daily",
            "owner_open_id": "ou_owner",
            "owner_profile": "alice",
            "source_app": "cli_expert",
            "expert_id": "spoofed-ignored",
            "writable_authorized": True,
        },
    )

    assert result["success"] is True
    [line] = audit_path.read_text(encoding="utf-8").splitlines()
    event = json.loads(line)
    assert event["event_type"] == "cron_scheduled_execution"
    assert event["expert_id"] == "expert-123"
    assert event["run_id"] == "job1"
    assert event["decision"] == "writable"
    assert event["open_id_hash"]
    assert "ou_owner" not in line


def test_expert_scheduled_execution_fails_closed_when_audit_cannot_be_recorded(tmp_path, monkeypatch):
    import hermes_multitenancy.security_audit as security_audit

    profile = _profile(tmp_path / "profiles", "alice")
    _install_fake_cron_modules(monkeypatch, profile)
    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT", "expert-123")
    monkeypatch.setattr(cron_worker, "_install_cron_subprocess_runtime_pool", lambda: None)

    ran: list[str] = []
    monkeypatch.setattr(
        cron_worker,
        "_run_cron_job_body_with_cleanup",
        lambda job, _sched: (ran.append(job["id"]), (True, "out", "vis", None))[1],
    )
    # audit write attempted but fails (disk full / unwritable path).
    monkeypatch.setattr(security_audit, "append_security_event", lambda **_kw: False)

    result = cron_worker._run_job_for_profile_current_process(
        profile,
        {
            "id": "job1",
            "name": "Daily",
            "owner_open_id": "ou_owner",
            "owner_profile": "alice",
            "source_app": "cli_expert",
            "writable_authorized": True,
        },
    )

    # Compensating control could not be recorded → writable body must NOT run.
    assert result["success"] is False
    assert "audit" in (result["error"] or "").lower()
    assert ran == []


def test_writable_authorized_strict_bool_rejects_dirty_data():
    from hermes_multitenancy.cron.execution import _writable_authorized

    assert _writable_authorized({"writable_authorized": True}) is True
    assert _writable_authorized({"writable_authorized": "1"}) is True
    assert _writable_authorized({"writable_authorized": "true"}) is True
    # Dirty / hand-edited persisted data must NOT authorize writes — Python
    # truthiness would wrongly treat the strings "0"/"false" as authorized.
    assert _writable_authorized({"writable_authorized": "0"}) is False
    assert _writable_authorized({"writable_authorized": "false"}) is False
    assert _writable_authorized({"writable_authorized": 0}) is False
    assert _writable_authorized({"writable_authorized": ""}) is False
    assert _writable_authorized({}) is False


def test_partition_is_env_only_and_fails_closed_without_app_id_env(tmp_path, monkeypatch):
    # The expert gateway partition matches ONLY the process env app-id (deploy
    # contract) — never a profile file, because the scan rebinds HERMES_HOME to
    # each scanned USER profile. A missing app-id env means "own nothing"
    # (fail-closed misconfig), never a profile-file lookup of the wrong app.
    profile = _profile(tmp_path / "profiles", "alice")
    uat = profile / "feishu_uat"
    uat.mkdir(parents=True, exist_ok=True)
    (uat / "app.json").write_text(json.dumps({"app_id": "cli_from_profile"}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT", "expert-123")
    monkeypatch.delenv("HERMES_MULTITENANCY_FIXED_EXPERT_APP_ID", raising=False)

    # No app-id env → owns nothing, even a job tagged with the profile file's app.
    assert cron_worker._job_belongs_to_this_gateway({"source_app": "cli_from_profile"}) is False

    # With the env set, it matches by env (not the profile file).
    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT_APP_ID", "cli_env")
    assert cron_worker._job_belongs_to_this_gateway({"source_app": "cli_env"}) is True
    assert cron_worker._job_belongs_to_this_gateway({"source_app": "cli_from_profile"}) is False


def test_with_cron_owner_context_never_stamps_source_labels_during_backfill(tmp_path, monkeypatch):
    # On an expert gateway (app-id env set), backfilling/scanning a legacy/user job
    # must NOT stamp source_app / writable_authorized — else ordinary cron silently
    # becomes expert writable cron and the router stops owning it. Source labels are
    # created ONLY by the create hook (the user's act of creating in the expert bot).
    profile = _profile(tmp_path / "profiles", "alice")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT", "expert-123")
    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT_APP_ID", "cli_expert")

    legacy = {"id": "legacy", "owner_open_id": "ou_owner", "owner_profile": "alice"}
    result = cron_worker.with_cron_owner_context(legacy, profile_home=profile)
    assert "source_app" not in result
    assert "writable_authorized" not in result
    # ... but the create hook itself DOES stamp (source labels come from create only).
    tagged = cron_worker.infer_cron_owner_context(legacy, profile_home=profile)
    assert tagged.get("source_app") == "cli_expert"
    assert tagged.get("writable_authorized") is True


def test_router_regression_without_fixed_expert_keeps_untagged_jobs_unchanged(tmp_path, monkeypatch):
    profile = _profile(tmp_path / "profiles", "alice")
    monkeypatch.setattr(cron_worker, "backfill_cron_owner_context_for_profile", lambda _p: None)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", "ou_owner")
    monkeypatch.delenv("HERMES_MULTITENANCY_FIXED_EXPERT", raising=False)
    monkeypatch.delenv("HERMES_MULTITENANCY_FIXED_EXPERT_APP_ID", raising=False)

    assert cron_worker.infer_cron_owner_context({}, profile_home=profile) == {
        "owner_open_id": "ou_owner",
        "owner_profile": "alice",
    }

    scheduler = DueScheduler({"alice": [{"id": "plain", "name": "plain"}]})
    executor = RecordingExecutor()
    submitted = cron_worker._scan_and_submit_due_profile_jobs(
        FakeCronJobs(),
        scheduler,
        profile.parent,
        {"alice"},
        adapters=None,
        loop=SimpleNamespace(is_running=lambda: False),
        patch_lock=threading.Lock(),
        executor=executor,
        in_flight=set(),
        runner=lambda _profile_home, _job: None,
    )

    assert submitted == 1
    assert scheduler.advanced == [("alice", "plain")]
    assert [(args[0].name, args[1]["id"]) for _fn, args, _kwargs in executor.submissions] == [
        ("alice", "plain")
    ]
