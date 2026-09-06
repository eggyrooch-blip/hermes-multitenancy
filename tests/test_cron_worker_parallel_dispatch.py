import concurrent.futures
import contextlib
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
        self.futures: list[concurrent.futures.Future] = []

    def submit(self, fn, *args, **kwargs):
        future: concurrent.futures.Future = concurrent.futures.Future()
        self.submissions.append((fn, args, kwargs))
        self.futures.append(future)
        return future


class ImmediateExecutor:
    def submit(self, fn, *args, **kwargs):
        future: concurrent.futures.Future = concurrent.futures.Future()
        future.set_result(
            {
                "success": True,
                "output": "full output",
                "final_response": "visible body",
                "error": None,
            }
        )
        return future


class FailingExecutor:
    def submit(self, *_args, **_kwargs):
        raise RuntimeError("executor down")


def _profile(root: Path, name: str) -> Path:
    profile = root / name
    (profile / "cron").mkdir(parents=True)
    (profile / "cron" / "jobs.json").write_text("[]", encoding="utf-8")
    return profile


def test_cross_profile_scan_submits_later_profile_before_slow_finish(tmp_path, monkeypatch):
    profiles_root = tmp_path / "profiles"
    _profile(profiles_root, "a_slow")
    _profile(profiles_root, "b_fast")
    scheduler = DueScheduler(
        {
            "a_slow": [{"id": "slow", "name": "slow"}],
            "b_fast": [{"id": "fast", "name": "fast"}],
        }
    )
    executor = RecordingExecutor()
    in_flight: set[tuple[str, str]] = set()
    monkeypatch.setattr(cron_worker, "backfill_cron_owner_context_for_profile", lambda _p: None)

    submitted = cron_worker._scan_and_submit_due_profile_jobs(
        FakeCronJobs(),
        scheduler,
        profiles_root,
        {"a_slow", "b_fast"},
        adapters=None,
        loop=SimpleNamespace(is_running=lambda: False),
        patch_lock=threading.Lock(),
        executor=executor,
        in_flight=in_flight,
        runner=lambda _profile_home, _job: None,
    )

    assert submitted == 2
    assert [(args[0].name, args[1]["id"]) for _fn, args, _kwargs in executor.submissions] == [
        ("a_slow", "slow"),
        ("b_fast", "fast"),
    ]
    assert in_flight == {("a_slow", "slow"), ("b_fast", "fast")}
    assert scheduler.advanced == [("a_slow", "slow"), ("b_fast", "fast")]


def test_cross_profile_scan_skips_duplicate_in_flight_job(tmp_path, monkeypatch):
    profiles_root = tmp_path / "profiles"
    _profile(profiles_root, "a_slow")
    _profile(profiles_root, "b_fast")
    scheduler = DueScheduler(
        {
            "a_slow": [{"id": "slow", "name": "slow"}],
            "b_fast": [{"id": "fast", "name": "fast"}],
        }
    )
    executor = RecordingExecutor()
    in_flight = {("a_slow", "slow")}
    monkeypatch.setattr(cron_worker, "backfill_cron_owner_context_for_profile", lambda _p: None)

    submitted = cron_worker._scan_and_submit_due_profile_jobs(
        FakeCronJobs(),
        scheduler,
        profiles_root,
        {"a_slow", "b_fast"},
        adapters=None,
        loop=SimpleNamespace(is_running=lambda: False),
        patch_lock=threading.Lock(),
        executor=executor,
        in_flight=in_flight,
        runner=lambda _profile_home, _job: None,
    )

    assert submitted == 1
    assert [(args[0].name, args[1]["id"]) for _fn, args, _kwargs in executor.submissions] == [
        ("b_fast", "fast")
    ]
    assert in_flight == {("a_slow", "slow"), ("b_fast", "fast")}
    assert scheduler.advanced == [("b_fast", "fast")]


def test_cross_profile_scan_respects_unavailable_profile_tick_lock(tmp_path, monkeypatch):
    profiles_root = tmp_path / "profiles"
    _profile(profiles_root, "locked")
    scheduler = DueScheduler({"locked": [{"id": "job1", "name": "locked"}]})
    executor = RecordingExecutor()
    in_flight: set[tuple[str, str]] = set()
    monkeypatch.setattr(cron_worker, "backfill_cron_owner_context_for_profile", lambda _p: None)
    monkeypatch.setattr(cron_worker, "_acquire_cron_tick_file_lock", lambda *_args: None)

    submitted = cron_worker._scan_and_submit_due_profile_jobs(
        FakeCronJobs(),
        scheduler,
        profiles_root,
        {"locked"},
        adapters=None,
        loop=SimpleNamespace(is_running=lambda: False),
        patch_lock=threading.Lock(),
        executor=executor,
        in_flight=in_flight,
        runner=lambda _profile_home, _job: None,
    )

    assert submitted == 0
    assert executor.submissions == []
    assert in_flight == set()
    assert scheduler.advanced == []


class FakeTickLock:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


class ClaimFinalizeScheduler(DueScheduler):
    SILENT_MARKER = "[SILENT]"

    def __init__(self, due_by_profile: dict[str, list[dict]]) -> None:
        super().__init__(due_by_profile)
        self.finalized: list[tuple[str, str]] = []

    def save_job_output(self, job_id: str, output: str) -> Path:
        self.finalized.append(("save", job_id))
        return Path(os.environ["HERMES_HOME"]) / "cron" / "output" / job_id / "out.md"

    def _deliver_result(self, job: dict, content: str, *, adapters=None, loop=None):
        self.finalized.append(("deliver", job["id"]))
        return None

    def mark_job_run(self, job_id: str, success: bool, error: str | None, *, delivery_error=None):
        self.finalized.append(("mark", job_id))

    def _summarize_cron_failure_for_delivery(self, job: dict, error: str | None) -> str:
        return f"failed: {error}"


def test_profile_tick_lock_is_held_until_submitted_job_finalizes(tmp_path, monkeypatch):
    profiles_root = tmp_path / "profiles"
    _profile(profiles_root, "owner")
    scheduler = ClaimFinalizeScheduler({"owner": [{"id": "job1", "name": "Job"}]})
    executor = RecordingExecutor()
    tick_lock = FakeTickLock()
    monkeypatch.setattr(cron_worker, "backfill_cron_owner_context_for_profile", lambda _p: None)
    monkeypatch.setattr(cron_worker, "_acquire_cron_tick_file_lock", lambda *_args: tick_lock)

    submitted = cron_worker._scan_and_submit_due_profile_jobs(
        FakeCronJobs(),
        scheduler,
        profiles_root,
        {"owner"},
        adapters=None,
        loop=SimpleNamespace(is_running=lambda: False),
        patch_lock=threading.Lock(),
        executor=executor,
        in_flight=set(),
        runner=lambda _profile_home, _job: None,
    )

    assert submitted == 1
    assert tick_lock.released is False

    executor.futures[0].set_result(
        {
            "success": True,
            "output": "full output",
            "final_response": "visible body",
            "error": None,
        }
    )

    assert tick_lock.released is True
    assert scheduler.finalized == [("save", "job1"), ("deliver", "job1"), ("mark", "job1")]


def test_completed_future_callback_does_not_reenter_profile_patch_lock(tmp_path, monkeypatch):
    profiles_root = tmp_path / "profiles"
    _profile(profiles_root, "owner")
    scheduler = ClaimFinalizeScheduler({"owner": [{"id": "job1", "name": "Job"}]})
    tick_lock = FakeTickLock()
    result: dict[str, object] = {}
    monkeypatch.setattr(cron_worker, "backfill_cron_owner_context_for_profile", lambda _p: None)
    monkeypatch.setattr(cron_worker, "_acquire_cron_tick_file_lock", lambda *_args: tick_lock)

    def run_scan() -> None:
        result["submitted"] = cron_worker._scan_and_submit_due_profile_jobs(
            FakeCronJobs(),
            scheduler,
            profiles_root,
            {"owner"},
            adapters=None,
            loop=SimpleNamespace(is_running=lambda: False),
            patch_lock=threading.Lock(),
            executor=ImmediateExecutor(),
            in_flight=set(),
            runner=lambda _profile_home, _job: None,
        )

    scan_thread = threading.Thread(target=run_scan, daemon=True)
    scan_thread.start()
    scan_thread.join(timeout=30)

    assert scan_thread.is_alive() is False, "scan deadlocked while registering completed future callback"
    assert result["submitted"] == 1
    assert tick_lock.released is True
    assert scheduler.finalized == [("save", "job1"), ("deliver", "job1"), ("mark", "job1")]


class SubmitFailureScheduler(DueScheduler):
    SILENT_MARKER = "[SILENT]"

    def __init__(self, due_by_profile: dict[str, list[dict]]) -> None:
        super().__init__(due_by_profile)
        self.calls: list[tuple] = []

    def save_job_output(self, job_id: str, output: str) -> Path:
        self.calls.append(("save", job_id, output))
        return Path(os.environ["HERMES_HOME"]) / "cron" / "output" / job_id / "out.md"

    def _deliver_result(self, job: dict, content: str, *, adapters=None, loop=None):
        self.calls.append(("deliver", job["id"], content))
        return None

    def mark_job_run(self, job_id: str, success: bool, error: str | None, *, delivery_error=None):
        self.calls.append(("mark", job_id, success, error, delivery_error))

    def _summarize_cron_failure_for_delivery(self, job: dict, error: str | None) -> str:
        return f"failed: {error}"


def test_submit_failure_marks_job_failed_and_releases_claim(tmp_path, monkeypatch):
    profiles_root = tmp_path / "profiles"
    _profile(profiles_root, "owner")
    scheduler = SubmitFailureScheduler({"owner": [{"id": "job1", "name": "Job"}]})
    tick_lock = FakeTickLock()
    in_flight: set[tuple[str, str]] = set()
    monkeypatch.setattr(cron_worker, "backfill_cron_owner_context_for_profile", lambda _p: None)
    monkeypatch.setattr(cron_worker, "_acquire_cron_tick_file_lock", lambda *_args: tick_lock)

    submitted = cron_worker._scan_and_submit_due_profile_jobs(
        FakeCronJobs(),
        scheduler,
        profiles_root,
        {"owner"},
        adapters=None,
        loop=SimpleNamespace(is_running=lambda: False),
        patch_lock=threading.Lock(),
        executor=FailingExecutor(),
        in_flight=in_flight,
        runner=lambda _profile_home, _job: None,
    )

    assert submitted == 0
    assert in_flight == set()
    assert tick_lock.released is True
    assert scheduler.advanced == [("owner", "job1")]
    assert scheduler.calls[0][0] == "save"
    assert "executor down" in scheduler.calls[0][2]
    assert scheduler.calls[1] == ("deliver", "job1", "failed: cron submit failed: executor down")
    assert scheduler.calls[2] == (
        "mark",
        "job1",
        False,
        "cron submit failed: executor down",
        None,
    )


def test_cron_job_body_explicitly_uses_runbroker_when_enabled(monkeypatch):
    scheduler = SimpleNamespace(
        run_job=lambda _job: (_ for _ in ()).throw(AssertionError("core run_job should not be used"))
    )
    monkeypatch.setattr(cron_worker, "_l4_check_needs_reauth_and_defer", lambda _job: None)
    monkeypatch.setattr(cron_worker, "_cron_run_broker_enabled", lambda: True)
    monkeypatch.setattr(
        cron_worker,
        "_run_job_through_broker",
        lambda job, sched: (True, f"output:{job['id']}", "visible", None),
    )

    assert cron_worker._run_cron_job_body({"id": "job1"}, scheduler) == (
        True,
        "output:job1",
        "visible",
        None,
    )


def test_cron_job_body_can_fall_back_to_core_run_job_when_broker_disabled(monkeypatch):
    scheduler = SimpleNamespace(
        run_job=lambda job: (True, f"core:{job['id']}", "core-visible", None)
    )
    monkeypatch.setattr(cron_worker, "_l4_check_needs_reauth_and_defer", lambda _job: None)
    monkeypatch.setattr(cron_worker, "_cron_run_broker_enabled", lambda: False)
    monkeypatch.setattr(
        cron_worker,
        "_run_job_through_broker",
        lambda _job, _sched: (_ for _ in ()).throw(AssertionError("broker should not be used")),
    )

    assert cron_worker._run_cron_job_body({"id": "job1"}, scheduler) == (
        True,
        "core:job1",
        "core-visible",
        None,
    )


def test_cron_job_body_with_cleanup_sweeps_mcp_orphans(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        cron_worker,
        "_run_cron_job_body",
        lambda _job, _scheduler: (True, "output", "visible", None),
    )
    monkeypatch.setattr(cron_worker, "_sweep_cron_mcp_orphans", lambda: calls.append("sweep"))

    assert cron_worker._run_cron_job_body_with_cleanup({"id": "job1"}, object()) == (
        True,
        "output",
        "visible",
        None,
    )
    assert calls == ["sweep"]


def test_cron_job_body_with_cleanup_sweeps_mcp_orphans_after_error(monkeypatch):
    calls: list[str] = []

    def fail(_job, _scheduler):
        raise RuntimeError("job exploded")

    monkeypatch.setattr(cron_worker, "_run_cron_job_body", fail)
    monkeypatch.setattr(cron_worker, "_sweep_cron_mcp_orphans", lambda: calls.append("sweep"))

    with pytest.raises(RuntimeError, match="job exploded"):
        cron_worker._run_cron_job_body_with_cleanup({"id": "job1"}, object())
    assert calls == ["sweep"]


def test_cron_subprocess_runtime_pool_uses_real_runner_for_visible_response(tmp_path, monkeypatch):
    from dataclasses import replace
    from hermes_multitenancy import agent_real, billing_identity, router

    async def prepare(request):
        return replace(
            request,
            metadata={**dict(request.metadata or {}), "billing_prepared": True},
        )

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)

    async def fake_real_run_agent(event, profile_home, **_kwargs):
        assert profile_home == tmp_path / "profiles" / "sunke"
        assert "Do not respond with [SILENT]" in event.text
        assert event.raw_event["metadata"]["billing_prepared"] is True
        return "REAL_VISIBLE_CRON_BODY"

    monkeypatch.setattr(agent_real, "real_run_agent", fake_real_run_agent)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "sunke"))
    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    router.override_pool(None)

    try:
        cron_worker._install_cron_subprocess_runtime_pool()
        scheduler = SimpleNamespace(
            _build_job_prompt=lambda job, prerun_script=None: job["prompt"],
        )

        success, output, final_response, error = cron_worker._run_job_through_broker(
            {
                "id": "job1",
                "name": "Daily cron",
                "prompt": "Return a detailed cron result.",
                "deliver": "feishu",
                "owner_profile": "sunke",
                "owner_open_id": "ou_sunke",
            },
            scheduler,
        )
    finally:
        router.override_pool(None)

    assert (success, final_response, error) == (True, "REAL_VISIBLE_CRON_BODY", None)
    assert "REAL_VISIBLE_CRON_BODY" in output
    assert "本次没有发现需要提醒" not in final_response


def test_run_job_for_profile_current_process_installs_runtime_pool_before_body(
    tmp_path,
    monkeypatch,
):
    from hermes_multitenancy import router
    from hermes_multitenancy.pool import RuntimePool
    from hermes_multitenancy.runtime import ProfileRuntime

    profile = _profile(tmp_path / "profiles", "sunke")
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
    cron_scheduler._build_job_prompt = lambda job, prerun_script=None: job["prompt"]
    cron_pkg.jobs = cron_jobs
    cron_pkg.scheduler = cron_scheduler
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)
    monkeypatch.setitem(sys.modules, "cron.scheduler", cron_scheduler)
    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.setenv("HERMES_HOME", "/outer/home")
    calls: list[tuple[str, str]] = []
    router.override_pool(None)

    async def fake_runner(_event, _profile_home, **_kwargs):
        return "REAL_VISIBLE_FROM_CURRENT_PROCESS"

    def install_runtime_pool():
        calls.append(("install", os.environ["HERMES_HOME"]))
        router.override_pool(
            RuntimePool(
                runtime_factory=lambda _profile_name, home: ProfileRuntime(
                    profile_home=home,
                    run_agent_fn=fake_runner,
                )
            )
        )

    monkeypatch.setattr(
        cron_worker,
        "_install_cron_subprocess_runtime_pool",
        install_runtime_pool,
    )

    try:
        result = cron_worker._run_job_for_profile_current_process(
            profile,
            {
                "id": "job1",
                "name": "Daily cron",
                "prompt": "Return a detailed cron result.",
                "deliver": "feishu",
                "owner_profile": "sunke",
                "owner_open_id": "ou_sunke",
            },
        )
    finally:
        router.override_pool(None)

    assert calls == [("install", str(profile.resolve()))]
    assert result["success"] is True
    assert result["final_response"] == "REAL_VISIBLE_FROM_CURRENT_PROCESS"
    assert "REAL_VISIBLE_FROM_CURRENT_PROCESS" in result["output"]
    assert os.environ["HERMES_HOME"] == "/outer/home"


class FinalizeScheduler:
    SILENT_MARKER = "[SILENT]"

    def __init__(self) -> None:
        self._hermes_home = Path("/original/hermes")
        self._LOCK_DIR = self._hermes_home / "cron"
        self._LOCK_FILE = self._LOCK_DIR / ".tick.lock"
        self.calls: list[tuple] = []

    def save_job_output(self, job_id: str, output: str) -> Path:
        self.calls.append(("save", Path(os.environ["HERMES_HOME"]).name, job_id, output))
        return Path(os.environ["HERMES_HOME"]) / "cron" / "output" / job_id / "out.md"

    def _deliver_result(self, job: dict, content: str, *, adapters=None, loop=None):
        self.calls.append(("deliver", Path(os.environ["HERMES_HOME"]).name, job["id"], content))
        return None

    def mark_job_run(self, job_id: str, success: bool, error: str | None, *, delivery_error=None):
        self.calls.append(
            (
                "mark",
                Path(os.environ["HERMES_HOME"]).name,
                job_id,
                success,
                error,
                delivery_error,
            )
        )

    def _summarize_cron_failure_for_delivery(self, job: dict, error: str | None) -> str:
        return f"failed: {error}"


class TerminalCronJobs(FakeCronJobs):
    def __init__(self, jobs: list[dict]) -> None:
        super().__init__()
        self._jobs_file_lock = threading.Lock()
        self.jobs = [dict(job) for job in jobs]

    def load_jobs(self) -> list[dict]:
        return [dict(job) for job in self.jobs]

    def save_jobs(self, jobs: list[dict]) -> None:
        self.jobs = [dict(job) for job in jobs]


def test_finalize_records_delivery_error_terminal_and_alert_for_unconfirmed_send(
    caplog, monkeypatch,
):
    """A missing Feishu receipt is terminally observable, never a quiet success."""
    cron_jobs = TerminalCronJobs(
        [{"id": "job1", "name": "Daily", "last_delivery_message_id": "om_stale"}]
    )
    scheduler = FinalizeScheduler()
    monkeypatch.setenv("HERMES_HOME", "/profiles/owner")
    scheduler._deliver_result = lambda *_args, **_kwargs: (
        "feishu live adapter delivery unconfirmed"
    )

    ok = cron_worker._finalize_claimed_cron_job_current_context(
        cron_jobs,
        scheduler,
        {"id": "job1", "name": "Daily"},
        {
            "success": True,
            "output": "full output",
            "final_response": "visible body",
            "error": None,
        },
        adapters={"feishu": object()},
        loop=SimpleNamespace(is_running=lambda: True),
    )

    assert ok is True
    assert scheduler.calls[-1][-1] == "feishu live adapter delivery unconfirmed"
    assert cron_jobs.jobs[0]["last_end_reason"] == "delivery_error"
    assert cron_jobs.jobs[0].get("last_delivery_message_id") is None
    assert "cron_delivery_alert job=job1 end_reason=delivery_error" in caplog.text


def test_record_cron_terminal_preserves_concurrent_job_creation():
    """Terminal read/modify/write shares core's lock with create_job."""
    cron_jobs = TerminalCronJobs([{"id": "job1", "name": "Daily"}])
    scheduler = FinalizeScheduler()
    loaded = threading.Event()
    release_load = threading.Event()
    original_load = cron_jobs.load_jobs

    def paused_load():
        jobs = original_load()
        loaded.set()
        assert release_load.wait(timeout=1)
        return jobs

    cron_jobs.load_jobs = paused_load
    terminal = threading.Thread(
        target=cron_worker._record_cron_terminal,
        args=(cron_jobs, scheduler, "job1"),
        kwargs={"end_reason": "completed", "receipt_message_id": "om_1"},
    )

    def create_job():
        with cron_jobs._jobs_file_lock:
            cron_jobs.jobs.append({"id": "job2", "name": "New"})

    terminal.start()
    assert loaded.wait(timeout=1)
    creator = threading.Thread(target=create_job)
    creator.start()
    release_load.set()
    terminal.join(timeout=1)
    creator.join(timeout=1)

    assert terminal.is_alive() is False
    assert creator.is_alive() is False
    assert [job["id"] for job in cron_jobs.jobs] == ["job1", "job2"]
    assert cron_jobs.jobs[0]["last_delivery_message_id"] == "om_1"


def test_record_cron_terminal_warns_when_job_disappeared(caplog):
    cron_jobs = TerminalCronJobs([])

    assert cron_worker._record_cron_terminal(
        cron_jobs,
        FinalizeScheduler(),
        "missing",
        end_reason="delivery_error",
    ) is False
    assert "cron terminal job missing job=missing" in caplog.text


def test_finalize_records_confirmed_receipt_with_completed_terminal(monkeypatch):
    cron_jobs = TerminalCronJobs([{"id": "job1", "name": "Daily"}])
    scheduler = FinalizeScheduler()
    monkeypatch.setenv("HERMES_HOME", "/profiles/owner")

    def deliver(job, _content, **_kwargs):
        job["_hermes_delivery_message_id"] = "om_confirmed"
        return None

    deliver._hermes_multitenancy_patched = True
    scheduler._deliver_result = deliver
    ok = cron_worker._finalize_claimed_cron_job_current_context(
        cron_jobs,
        scheduler,
        {"id": "job1", "name": "Daily"},
        {
            "success": True,
            "output": "full output",
            "final_response": "visible body",
            "error": None,
        },
        adapters={"feishu": object()},
        loop=SimpleNamespace(is_running=lambda: True),
    )

    assert ok is True
    assert cron_jobs.jobs[0]["last_end_reason"] == "completed"
    assert cron_jobs.jobs[0]["last_delivery_message_id"] == "om_confirmed"


def test_finalize_preserves_confirmed_feishu_receipt_when_other_leg_fails(monkeypatch):
    """A mixed partial failure stays terminally visible without losing send identity."""
    cron_jobs = TerminalCronJobs([{"id": "job1", "name": "Daily"}])
    scheduler = FinalizeScheduler()
    monkeypatch.setenv("HERMES_HOME", "/profiles/owner")
    delivery_calls = []

    def deliver(job, _content, **_kwargs):
        delivery_calls.append(job["id"])
        job["_hermes_feishu_receipt_expected"] = True
        job["_hermes_delivery_message_id"] = "om_confirmed"
        return "slack delivery failed"

    deliver._hermes_multitenancy_patched = True
    scheduler._deliver_result = deliver

    ok = cron_worker._finalize_claimed_cron_job_current_context(
        cron_jobs,
        scheduler,
        {"id": "job1", "name": "Daily", "deliver": ["feishu", "slack"]},
        {
            "success": True,
            "output": "full output",
            "final_response": "visible body",
            "error": None,
        },
        adapters={"feishu": object()},
        loop=SimpleNamespace(is_running=lambda: True),
    )

    assert ok is True
    assert delivery_calls == ["job1"]
    assert scheduler.calls[-1][-1] == "slack delivery failed"
    assert cron_jobs.jobs[0]["last_end_reason"] == "delivery_error"
    assert cron_jobs.jobs[0]["last_delivery_message_id"] == "om_confirmed"


@pytest.mark.parametrize("deliver_value", ["feishu", "all", ["feishu", "slack"]])
def test_finalize_feishu_delivery_without_receipt_is_terminal_error(
    monkeypatch, deliver_value,
):
    cron_jobs = TerminalCronJobs([{"id": "job1", "name": "Daily"}])
    scheduler = FinalizeScheduler()
    scheduler._resolve_delivery_targets = lambda _job: [
        {"platform": "feishu", "chat_id": "ou_owner"},
        {"platform": "slack", "chat_id": "channel"},
    ]
    monkeypatch.setenv("HERMES_HOME", "/profiles/owner")

    ok = cron_worker._finalize_claimed_cron_job_current_context(
        cron_jobs,
        scheduler,
        {"id": "job1", "name": "Daily", "deliver": deliver_value},
        {
            "success": True,
            "output": "full output",
            "final_response": "visible body",
            "error": None,
        },
        adapters={"feishu": object()},
        loop=SimpleNamespace(is_running=lambda: True),
    )

    assert ok is True
    assert cron_jobs.jobs[0]["last_end_reason"] == "delivery_error"
    assert scheduler.calls[-1][-1] == "feishu confirmed delivery patch unavailable"
    assert not any(call[0] == "deliver" for call in scheduler.calls)
    assert cron_jobs.jobs[0].get("last_delivery_message_id") is None


def test_finalize_unpatched_resolution_failure_stops_before_core(monkeypatch):
    cron_jobs = TerminalCronJobs([{"id": "job1", "name": "Daily"}])
    scheduler = FinalizeScheduler()
    scheduler._resolve_delivery_targets = lambda _job: (_ for _ in ()).throw(
        RuntimeError("transient resolver failure")
    )
    monkeypatch.setenv("HERMES_HOME", "/profiles/owner")

    ok = cron_worker._finalize_claimed_cron_job_current_context(
        cron_jobs,
        scheduler,
        {"id": "job1", "name": "Daily", "deliver": "all"},
        {
            "success": True,
            "output": "full output",
            "final_response": "visible body",
            "error": None,
        },
        adapters={"feishu": object()},
        loop=SimpleNamespace(is_running=lambda: True),
    )

    assert ok is True
    assert cron_jobs.jobs[0]["last_end_reason"] == "delivery_error"
    assert scheduler.calls[-1][-1] == "cron delivery target resolution unavailable"
    assert not any(call[0] == "deliver" for call in scheduler.calls)


def test_finalize_runs_parent_side_delivery_under_original_profile_context(tmp_path, monkeypatch):
    profile = _profile(tmp_path / "profiles", "songtingting")
    cron_jobs = FakeCronJobs()
    scheduler = FinalizeScheduler()
    scheduler._resolve_delivery_targets = lambda _job: [
        {"platform": "slack", "chat_id": "channel"}
    ]
    monkeypatch.setenv("HERMES_HOME", "/outer/home")

    ok = cron_worker._finalize_claimed_cron_job(
        cron_jobs,
        scheduler,
        profile,
        profile / "cron" / "jobs.json",
        {"id": "job1", "name": "Daily"},
        {
            "success": True,
            "output": "full output",
            "final_response": "visible body",
            "error": None,
        },
        adapters={"feishu": object()},
        loop=SimpleNamespace(is_running=lambda: True),
        patch_lock=threading.Lock(),
        verbose=True,
    )

    assert ok is True
    assert scheduler.calls == [
        ("save", "songtingting", "job1", "full output"),
        ("deliver", "songtingting", "job1", "visible body"),
        ("mark", "songtingting", "job1", True, None, None),
    ]
    assert cron_jobs.HERMES_DIR == Path("/original/hermes")
    assert cron_jobs.JOBS_FILE == Path("/original/hermes/cron/jobs.json")
    assert os.environ["HERMES_HOME"] == "/outer/home"


class ClaimStore:
    """cron.jobs stand-in whose load/save round-trip a real jobs.json.

    The release path has to survive the actual file, not an in-memory dict —
    that file is what the OWNING gateway's next tick reads.
    """

    def __init__(self, jobs_file: Path, jobs: list[dict]) -> None:
        self.HERMES_DIR = jobs_file.parent.parent
        self.CRON_DIR = jobs_file.parent
        self.JOBS_FILE = jobs_file
        self.OUTPUT_DIR = self.CRON_DIR / "output"
        self._lock_depth = 0
        self.unlocked_saves = 0
        self._write(jobs)

    @contextlib.contextmanager
    def _jobs_lock(self):
        self._lock_depth += 1
        try:
            yield
        finally:
            self._lock_depth -= 1

    def _write(self, jobs: list[dict]) -> None:
        self.JOBS_FILE.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")

    def load_jobs(self) -> list[dict]:
        return json.loads(self.JOBS_FILE.read_text(encoding="utf-8"))["jobs"]

    def save_jobs(self, jobs: list[dict]) -> None:
        if self._lock_depth == 0:
            self.unlocked_saves += 1
        self._write(jobs)

    def stored(self, job_id: str) -> dict:
        return next(job for job in self.load_jobs() if job["id"] == job_id)


class ClaimingDueScheduler(DueScheduler):
    """Mirrors core get_due_jobs(): stamps a run_claim on every due one-shot
    BEFORE the caller gets any chance to filter it by ownership."""

    CLAIM = {"at": "2026-08-01T10:00:00+08:00", "by": "scanning-gateway"}

    def __init__(self, store: ClaimStore) -> None:
        super().__init__({})
        self.store = store

    def get_due_jobs(self) -> list[dict]:
        with self.store._jobs_lock():
            jobs = self.store.load_jobs()
            for job in jobs:
                if job.get("schedule", {}).get("kind") == "once":
                    job["run_claim"] = dict(self.CLAIM)
            self.store.save_jobs(jobs)
            return [dict(job) for job in jobs]


def _scan_with_store(store, scheduler, profile, executor):
    return cron_worker._scan_and_submit_due_profile_jobs(
        store,
        scheduler,
        profile.parent,
        {profile.name},
        adapters=None,
        loop=SimpleNamespace(is_running=lambda: False),
        patch_lock=threading.Lock(),
        executor=executor,
        in_flight=set(),
        runner=lambda _profile_home, _job: None,
    )


def _router_gateway(monkeypatch) -> None:
    monkeypatch.setattr(cron_worker, "backfill_cron_owner_context_for_profile", lambda _p: None)
    monkeypatch.setattr(cron_worker, "_acquire_cron_tick_file_lock", lambda *_args: FakeTickLock())
    monkeypatch.delenv("HERMES_MULTITENANCY_FIXED_EXPERT", raising=False)
    monkeypatch.delenv("HERMES_MULTITENANCY_FIXED_EXPERT_APP_ID", raising=False)


def test_rejected_claimed_oneshot_has_its_run_claim_released(tmp_path, monkeypatch):
    """A one-shot this gateway does not own must go back to the pool NOW.

    Core claims it during the due scan; if we drop it still claimed, core skips
    it for the whole claim TTL (1800s) and the owning gateway never fires it.
    """
    profile = _profile(tmp_path / "profiles", "alice")
    store = ClaimStore(
        profile / "cron" / "jobs.json",
        [
            {
                "id": "expert_once",
                "name": "Expert reminder",
                "source_app": "cli_expert",
                "schedule": {"kind": "once"},
            }
        ],
    )
    scheduler = ClaimingDueScheduler(store)
    executor = RecordingExecutor()
    _router_gateway(monkeypatch)

    submitted = _scan_with_store(store, scheduler, profile, executor)

    assert submitted == 0
    assert executor.submissions == []
    assert store.stored("expert_once")["run_claim"] is None
    assert store.unlocked_saves == 0


def test_accepted_claimed_oneshot_keeps_its_run_claim(tmp_path, monkeypatch):
    """The claim on a job we DO run is the cross-process in-flight guard —
    releasing it here would let a second gateway re-dispatch the same run."""
    profile = _profile(tmp_path / "profiles", "alice")
    store = ClaimStore(
        profile / "cron" / "jobs.json",
        [{"id": "own_once", "name": "Mine", "schedule": {"kind": "once"}}],
    )
    scheduler = ClaimingDueScheduler(store)
    executor = RecordingExecutor()
    _router_gateway(monkeypatch)

    submitted = _scan_with_store(store, scheduler, profile, executor)

    assert submitted == 1
    assert store.stored("own_once")["run_claim"] == ClaimingDueScheduler.CLAIM


def test_release_leaves_a_claim_another_process_has_taken_over(tmp_path, monkeypatch):
    profile = _profile(tmp_path / "profiles", "alice")
    store = ClaimStore(
        profile / "cron" / "jobs.json",
        [
            {
                "id": "expert_once",
                "schedule": {"kind": "once"},
                "run_claim": {"at": "2026-08-01T10:05:00+08:00", "by": "other-gateway"},
            }
        ],
    )
    stale = {
        "id": "expert_once",
        "schedule": {"kind": "once"},
        "run_claim": {"at": "2026-08-01T10:00:00+08:00", "by": "us"},
    }

    assert cron_worker._release_cron_run_claim(store, DueScheduler({}), stale) is False
    assert store.stored("expert_once")["run_claim"] == {
        "at": "2026-08-01T10:05:00+08:00",
        "by": "other-gateway",
    }


def test_multitenancy_cron_worker_count_defaults_to_four_and_honors_env(monkeypatch):
    monkeypatch.delenv("HERMES_MULTITENANCY_CRON_WORKERS", raising=False)
    assert cron_worker._multitenancy_cron_worker_count() == 4

    monkeypatch.setenv("HERMES_MULTITENANCY_CRON_WORKERS", "2")
    assert cron_worker._multitenancy_cron_worker_count() == 2

    monkeypatch.setenv("HERMES_MULTITENANCY_CRON_WORKERS", "0")
    assert cron_worker._multitenancy_cron_worker_count() == 1

    monkeypatch.setenv("HERMES_MULTITENANCY_CRON_WORKERS", "not-a-number")
    assert cron_worker._multitenancy_cron_worker_count() == 4
