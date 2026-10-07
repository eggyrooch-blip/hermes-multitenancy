from __future__ import annotations

import io
from pathlib import Path

import pytest
import yaml

from hermes_multitenancy import startup_guard


def _fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    package = tmp_path / "hermes_multitenancy"
    package.mkdir()
    (package / "ok.py").write_text("VALUE = 1\n")
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["multitenancy"]}}))
    env = {
        "HERMES_HOME": str(profile),
        "FEISHU_APP_ID": "present",
        "HERMES_MULTITENANCY_CREDENTIAL_KEY": "present",
        "HERMES_MULTITENANCY_RUN_BROKER_KEY": "present",
        "HERMES_MULTITENANCY_RUN_BROKER_SERVER": "1",
    }
    return package, profile, env


def test_preflight_accepts_complete_isolation_boundary(monkeypatch, tmp_path):
    package, profile, env = _fixture(tmp_path)
    monkeypatch.setattr(startup_guard, "_import_boundaries", lambda: None)

    startup_guard.validate_startup(env=env, package_dir=package, profile_home=profile)


def test_preflight_rejects_unreadable_plugin_source(monkeypatch, tmp_path):
    package, profile, env = _fixture(tmp_path)
    blocked = package / "blocked.py"
    blocked.write_text("VALUE = 2\n")
    original = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        if path == blocked:
            raise PermissionError("blocked")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(startup_guard, "_import_boundaries", lambda: None)

    with pytest.raises(startup_guard.StartupGuardError):
        startup_guard.validate_startup(env=env, package_dir=package, profile_home=profile)


def test_preflight_rejects_disabled_plugin(tmp_path):
    package, profile, env = _fixture(tmp_path)
    (profile / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": []}}))

    with pytest.raises(startup_guard.StartupGuardError):
        startup_guard.validate_startup(env=env, package_dir=package, profile_home=profile)


def test_authenticated_broker_health_is_required(monkeypatch):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(
        startup_guard,
        "urlopen",
        lambda request, timeout: Response(b'{"ok":true,"service":"hermes-multitenancy-run-broker"}'),
    )

    startup_guard.wait_run_broker(env={"HERMES_MULTITENANCY_RUN_BROKER_KEY": "present"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 20.0), ("", 20.0), ("300", 300.0), (" 45.5 ", 45.5)],
)
def test_wait_broker_deadline_comes_from_unit_environment(monkeypatch, raw, expected):
    env = {"HERMES_MULTITENANCY_RUN_BROKER_KEY": "present"}
    if raw is not None:
        env["HERMES_MULTITENANCY_RUN_BROKER_WAIT_SECONDS"] = raw
    clock = {"now": 1000.0}
    deadlines = []

    def urlopen(request, timeout):
        deadlines.append(clock["now"])
        raise OSError("broker not up yet")

    def sleep(seconds):
        clock["now"] += 50.0

    monkeypatch.setattr(startup_guard, "urlopen", urlopen)
    monkeypatch.setattr(startup_guard.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(startup_guard.time, "sleep", sleep)
    assert startup_guard.run_broker_wait_seconds(env) == expected
    with pytest.raises(startup_guard.StartupGuardError, match="run_broker_unhealthy"):
        startup_guard.wait_run_broker(env=env)
    # Probes stop at the first attempt at/after start + configured wait.
    assert deadlines[-1] - 1000.0 >= expected
    assert deadlines[-1] - 1000.0 < expected + 50.0


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "nan", "inf", "3601"])
def test_wait_broker_rejects_invalid_wait_seconds(raw):
    env = {
        "HERMES_MULTITENANCY_RUN_BROKER_KEY": "present",
        "HERMES_MULTITENANCY_RUN_BROKER_WAIT_SECONDS": raw,
    }
    with pytest.raises(startup_guard.StartupGuardError, match="run_broker_wait_invalid"):
        startup_guard.wait_run_broker(env=env)


def test_required_dropin_waits_for_slow_production_broker():
    """05-multitenancy-required.conf replaces the temporary 99-tmp-wait-broker drop-in."""
    template = Path(__file__).resolve().parents[1] / "deploy" / "hermes-gateway-multitenancy-required.conf"
    rendered = template.read_text().replace("@PYTHON@", "/venv/bin/python")
    lines = [line.strip() for line in rendered.splitlines() if line.strip() and not line.startswith("#")]
    assert lines[0] == "[Service]"
    assert "Environment=HERMES_MULTITENANCY_RUN_BROKER_WAIT_SECONDS=300" in lines
    assert "TimeoutStartSec=400" in lines
    assert "ExecStartPost=/venv/bin/python -m hermes_multitenancy.startup_guard wait-broker" in lines
    wait = float(next(l for l in lines if l.startswith("Environment=HERMES_MULTITENANCY_RUN_BROKER_WAIT_SECONDS=")).split("=")[-1])
    assert startup_guard.run_broker_wait_seconds({"HERMES_MULTITENANCY_RUN_BROKER_WAIT_SECONDS": str(int(wait))}) == 300.0
    assert wait < 400


@pytest.mark.parametrize("cohort", ["", "*", "employee_a,*"])
def test_preflight_rejects_enabled_billing_without_finite_canary(
    monkeypatch, tmp_path, cohort
):
    package, profile, env = _fixture(tmp_path)
    env.update(
        {
            "HERMES_LITELLM_BILLING_ENABLED": "true",
            "HERMES_LITELLM_BILLING_PAYER_IDS": cohort,
        }
    )
    monkeypatch.setattr(startup_guard, "_import_boundaries", lambda: None)

    with pytest.raises(startup_guard.StartupGuardError, match="billing_canary"):
        startup_guard.validate_startup(
            env=env, package_dir=package, profile_home=profile
        )


def test_preflight_accepts_enabled_billing_with_finite_canary_and_no_readiness_env(
    monkeypatch, tmp_path
):
    """billing-drop-release-gate: since billing-degrade-not-refuse (option C),
    an unavailable readiness artifact degrades at runtime instead of refusing
    service, so validate_startup no longer requires ANY
    HERMES_LITELLM_BILLING_READINESS_ARTIFACT/replay-store/policy-digest/etc
    env to admit an enabled cohort. Negative control: if the
    verify_enabled_environment ceremony call is restored in
    _validate_billing_cohort, this test goes red with
    StartupGuardError("readiness_local_universe_unavailable") — the exact code
    observed when the mutation was run, not a guess — because none of that
    ceremony env is set here.
    """
    package, profile, env = _fixture(tmp_path)
    env.update(
        {
            "HERMES_LITELLM_BILLING_ENABLED": "true",
            "HERMES_LITELLM_BILLING_PAYER_IDS": "employee_a,employee_b",
        }
    )
    monkeypatch.setattr(startup_guard, "_import_boundaries", lambda: None)

    startup_guard.validate_startup(env=env, package_dir=package, profile_home=profile)


def test_preflight_ignores_billing_cohort_when_billing_disabled(monkeypatch, tmp_path):
    package, profile, env = _fixture(tmp_path)
    env.update(
        {
            "HERMES_LITELLM_BILLING_ENABLED": "false",
            "HERMES_LITELLM_BILLING_PAYER_IDS": "",
        }
    )
    monkeypatch.setattr(startup_guard, "_import_boundaries", lambda: None)

    startup_guard.validate_startup(env=env, package_dir=package, profile_home=profile)
