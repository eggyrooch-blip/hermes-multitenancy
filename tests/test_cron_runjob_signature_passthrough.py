"""Regression: the multitenancy cron run_job monkeypatch must forward every
argument the core scheduler passes.

The core `cron.scheduler.run_job` grew a keyword-only `defer_agent_teardown`
list (used by the parallel pool to defer agent teardown). The multitenancy
wrapper hard-coded `def run_job(job: dict)`, so once the core started calling
`run_job(job, defer_agent_teardown=...)` every cron tick raised
`TypeError: run_job() got an unexpected keyword argument 'defer_agent_teardown'`
and no scheduled job delivered. The wrapper must accept and pass through
whatever the core hands it.
"""

import sys
from types import ModuleType

from hermes_multitenancy.cron import patches
from hermes_multitenancy import cron_worker as _cw


def _install_fake_scheduler(monkeypatch):
    calls = {}

    def original_run_job(job, *, defer_agent_teardown=None):
        calls["job"] = job
        calls["defer_agent_teardown"] = defer_agent_teardown
        return (True, "ok", "", None)

    fake_scheduler = ModuleType("cron.scheduler")
    fake_scheduler.run_job = original_run_job
    fake_cron = ModuleType("cron")
    fake_cron.scheduler = fake_scheduler
    monkeypatch.setitem(sys.modules, "cron", fake_cron)
    monkeypatch.setitem(sys.modules, "cron.scheduler", fake_scheduler)
    # Broker disabled → the wrapper delegates to the core run_job, which is the
    # path that must preserve defer_agent_teardown.
    monkeypatch.setattr(_cw, "_cron_run_broker_enabled", lambda: False, raising=False)
    monkeypatch.setattr(_cw, "_l4_check_needs_reauth_and_defer", lambda job: None, raising=False)
    return fake_scheduler, calls


def test_patched_run_job_forwards_defer_agent_teardown(monkeypatch):
    fake_scheduler, calls = _install_fake_scheduler(monkeypatch)

    patches._patch_cron_run_broker()

    deferred: list = []
    # Before the fix this raised TypeError: unexpected keyword argument.
    result = fake_scheduler.run_job({"id": "job1"}, defer_agent_teardown=deferred)

    assert result == (True, "ok", "", None)
    assert calls["defer_agent_teardown"] is deferred


def test_patched_run_job_still_works_without_extra_kwargs(monkeypatch):
    fake_scheduler, calls = _install_fake_scheduler(monkeypatch)

    patches._patch_cron_run_broker()

    result = fake_scheduler.run_job({"id": "job2"})

    assert result == (True, "ok", "", None)
    assert calls["defer_agent_teardown"] is None
