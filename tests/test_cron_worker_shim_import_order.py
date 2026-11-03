"""Cron shim exports must survive submodule-first circular imports."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


def _run_in_clean_process(code: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("first_module", ["run_broker_bridge", "execution"])
def test_submodule_first_import_resolves_and_caches_helpers(first_module):
    _run_in_clean_process(f"""
        import hermes_multitenancy.cron.{first_module}
        from hermes_multitenancy import cron_worker
        from hermes_multitenancy.cron import execution, run_broker_bridge

        for name, owner in (
            ("_cron_user_key", run_broker_bridge),
            ("_cron_job_timeout_seconds", execution),
        ):
            assert getattr(cron_worker, name) is getattr(owner, name), name
            assert vars(cron_worker)[name] is getattr(owner, name), name
    """)


def test_monkeypatch_overrides_cached_helper_through_cw_call_path():
    _run_in_clean_process("""
        import hermes_multitenancy.cron.run_broker_bridge as bridge
        from hermes_multitenancy import cron_worker
        from pathlib import Path

        assert cron_worker._cron_user_key is bridge._cron_user_key
        calls = []

        def fake(job, profile_name):
            calls.append((job["id"], profile_name))
            return "ou_fake"

        setattr(cron_worker, "_cron_user_key", fake)
        assert bridge._cw._cron_user_key is fake
        request = bridge._build_cron_run_request(
            {"id": "job-import-order", "owner_open_id": "ou_original",
             "owner_profile": "owner"},
            profile_home=Path("profiles/owner"),
            prompt="ping",
        )
        assert request.user_key == "ou_fake"
        assert calls == [("job-import-order", "owner")]
    """)


def test_live_state_reads_and_writes_stay_uncached():
    _run_in_clean_process("""
        from hermes_multitenancy import cron_worker

        for name, owner in cron_worker._LIVE_STATE_OWNERS.items():
            first, second = object(), object()
            setattr(owner, name, first)
            assert getattr(cron_worker, name) is first, name
            setattr(cron_worker, name, second)
            assert getattr(owner, name) is second, name
            assert getattr(cron_worker, name) is second, name
            assert name not in vars(cron_worker), name
    """)


def test_fallback_uses_submodule_order_and_rejects_missing_names():
    _run_in_clean_process("""
        from hermes_multitenancy import cron_worker

        first, last = cron_worker._CRON_SUBMODULES[0], cron_worker._CRON_SUBMODULES[-1]
        first._late_shim_probe = object()
        last._late_shim_probe = object()
        assert cron_worker._late_shim_probe is first._late_shim_probe
        assert vars(cron_worker)["_late_shim_probe"] is first._late_shim_probe

        try:
            cron_worker._missing_shim_probe
        except AttributeError:
            pass
        else:
            raise AssertionError("missing shim name did not raise AttributeError")
    """)
