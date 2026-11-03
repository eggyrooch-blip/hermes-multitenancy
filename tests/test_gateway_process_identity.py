"""Gateway PID/status identity survives the legacy MT cron environment scope."""
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("profile", ["multitenancy_router", "expert_krd", None])
def test_cron_env_churn_preserves_gateway_identity_and_heartbeat(monkeypatch, tmp_path, profile):
    from gateway import status
    from hermes_constants import (
        get_hermes_home, get_process_hermes_home,
        reset_hermes_home_override, set_hermes_home_override,
    )
    from hermes_multitenancy.cron.orchestrator import _cron_profile_context
    from hermes_multitenancy.gateway_ownership import _patch_gateway_process_identity_home

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    launch_home = tmp_path / ".hermes"
    if profile:
        launch_home = launch_home / "profiles" / profile
        monkeypatch.setenv("HERMES_HOME", str(launch_home))
        monkeypatch.setenv("HERMES_PROFILE", profile)
    else:
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT", "test_expert" if profile == "expert_krd" else "")
    # Fresh process identity for each case; restore the real module after it.
    monkeypatch.setattr(status, "_get_process_hermes_home", lambda: get_process_hermes_home())
    monkeypatch.setattr(status, "_runtime_status_state_path", None)
    monkeypatch.setattr(status, "_runtime_status_state", None)
    monkeypatch.setattr(status, "_get_code_identity_fields", lambda: {})
    monkeypatch.setattr(status, "_read_json_file", lambda path: None)
    monkeypatch.setattr(status, "_emit_runtime_status_transition", lambda *args: None)
    writes = []
    writer = SimpleNamespace(submit=lambda path, value: writes.append((path, value)) or len(writes))
    monkeypatch.setattr(status, "_get_runtime_status_writer", lambda: writer)

    _patch_gateway_process_identity_home()
    first_binding = status._get_process_hermes_home
    status.publish_runtime_status(gateway_state="running", platform="feishu", platform_state="connected")
    cron_jobs = SimpleNamespace(HERMES_DIR=launch_home, CRON_DIR=launch_home / "cron",
                               JOBS_FILE=launch_home / "cron/jobs.json", OUTPUT_DIR=launch_home / "cron/output")
    cron_scheduler = SimpleNamespace()
    for name in ("tenant_a", "tenant_b"):
        tenant_home = tmp_path / ".hermes/profiles" / name
        with _cron_profile_context(cron_jobs, cron_scheduler, tenant_home,
                                   tenant_home / "cron/jobs.json", threading.Lock()):
            assert get_process_hermes_home() == tenant_home
            # Re-discovery in a tenant scope must retain the first launch home.
            _patch_gateway_process_identity_home()
            assert status._get_process_hermes_home is first_binding
            token = set_hermes_home_override(tenant_home)
            try:
                assert get_hermes_home() == tenant_home
                assert status._get_pid_path() == launch_home / "gateway.pid"
                assert status._get_runtime_status_path() == launch_home / "gateway_state.json"
                status.publish_runtime_status(active_agents=1)
            finally:
                reset_hermes_home_override(token)
        status.publish_runtime_status()  # real heartbeat call, only writer isolated

    assert get_process_hermes_home() == launch_home
    assert len(writes) == 5
    for path, value in writes:
        assert path == launch_home / "gateway_state.json"
        assert value["hermes_home"] == str(launch_home)
        assert value["gateway_state"] == "running"
        assert value["platforms"]["feishu"]["state"] == "connected"
