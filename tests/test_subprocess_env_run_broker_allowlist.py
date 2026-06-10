"""RunBroker auth must reach the sandboxed agent via the env allowlist.

Regression guard: the sandbox env is a strict allowlist (parent gateway env is
NOT inherited). The cronjob(action=run) tool runs inside the sandbox and reads
HERMES_RUN_BROKER_KEY from its own env to authenticate to the router RunBroker;
if the key is not allowlisted the child sends no Bearer token and the cron
trigger fails with 401 (the 2026-06-10 prod incident).
"""
from __future__ import annotations

import pytest

from hermes_multitenancy import agent_real


@pytest.mark.parametrize(
    "key",
    [
        "HERMES_RUN_BROKER_KEY",
        "HERMES_MULTITENANCY_RUN_BROKER_KEY",
        "HERMES_RUN_BROKER_URL",
        "HERMES_MULTITENANCY_RUN_BROKER_URL",
    ],
)
def test_run_broker_keys_are_allowlisted(key):
    assert key in agent_real._SUBPROCESS_ENV_ALLOWLIST


def test_build_subprocess_env_passes_run_broker_key_from_parent(tmp_path, monkeypatch):
    """The allowlisted RunBroker key in the gateway env must carry into the
    child subprocess env so the sandboxed cron tool can authenticate."""
    profile_home = tmp_path / "profiles" / "p_test"
    profile_home.mkdir(parents=True)
    approval_dir = tmp_path / "approvals"
    approval_dir.mkdir()

    monkeypatch.setenv("HERMES_RUN_BROKER_KEY", "test-broker-key-123")
    # Secrets NOT in the allowlist must still be stripped (boundary intact).
    monkeypatch.setenv("SOME_RANDOM_SECRET", "must-not-leak")

    env = agent_real._build_subprocess_env(profile_home, approval_dir=approval_dir)

    assert env.get("HERMES_RUN_BROKER_KEY") == "test-broker-key-123"
    assert "SOME_RANDOM_SECRET" not in env
