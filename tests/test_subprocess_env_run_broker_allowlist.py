"""RunBroker auth reaches the sandboxed agent as a RUN-SCOPED token, never the master key.

History. The original guard here (2026-06-10 prod incident) asserted the opposite:
the shared master ``HERMES_RUN_BROKER_KEY`` had to be allowlisted into the child,
because otherwise the sandboxed ``cronjob(action=run)`` tool sent no Bearer and
the cron trigger 401'd. That fix was correct about the symptom and wrong about
the credential: the justification written into the allowlist — "server enforces
per-profile scope via X-Hermes-Profile / X-Hermes-User-Key" — was false. Those
headers are caller-asserted, so every agent shell held a credential that could
act as any colleague (2026-08-04 security review; probed in prod, GET 200 on a
peer's jobs).

The child now receives a per-run token whose (profile, open_id) binding is held
server-side, so the 2026-06-10 guarantee (child CAN authenticate) is preserved by
``tests/test_aiagent_subprocess.py::test_sandbox_child_gets_run_scoped_broker_token_not_master_key``
while the master key stays out of tenant reach.
"""
from __future__ import annotations

import pytest

from hermes_multitenancy import agent_real


@pytest.mark.parametrize(
    "key",
    [
        "HERMES_RUN_BROKER_URL",
        "HERMES_MULTITENANCY_RUN_BROKER_URL",
    ],
)
def test_run_broker_urls_are_allowlisted(key):
    """The child still needs to know WHERE the broker is."""
    assert key in agent_real._SUBPROCESS_ENV_ALLOWLIST


@pytest.mark.parametrize(
    "key",
    [
        "HERMES_RUN_BROKER_KEY",
        "HERMES_MULTITENANCY_RUN_BROKER_KEY",
    ],
)
def test_master_run_broker_keys_are_not_inheritable(key):
    """The bearer is minted per run, never inherited from the gateway env."""
    assert key not in agent_real._SUBPROCESS_ENV_ALLOWLIST


def test_build_subprocess_env_never_carries_master_run_broker_key(tmp_path, monkeypatch):
    """The parent's master key must not survive into the child subprocess env.

    Unconditional backstop: this must hold with strict context OFF too, since
    that flag defaults to off and `startup_guard` does not require it (codex
    review RBOS-STRICT-OPTIONAL).
    """
    profile_home = tmp_path / "profiles" / "p_test"
    profile_home.mkdir(parents=True)
    approval_dir = tmp_path / "approvals"
    approval_dir.mkdir()

    monkeypatch.delenv("HERMES_MULTITENANCY_STRICT_CONTEXT", raising=False)
    monkeypatch.setenv("HERMES_RUN_BROKER_KEY", "test-broker-key-123")
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "test-broker-key-123")
    monkeypatch.setenv("HERMES_RUN_BROKER_URL", "http://127.0.0.1:8766")
    # Secrets NOT in the allowlist must still be stripped (boundary intact).
    monkeypatch.setenv("SOME_RANDOM_SECRET", "must-not-leak")

    env = agent_real._build_subprocess_env(profile_home, approval_dir=approval_dir)

    assert "HERMES_RUN_BROKER_KEY" not in env
    assert "HERMES_MULTITENANCY_RUN_BROKER_KEY" not in env
    # The child still learns where the broker lives.
    assert env.get("HERMES_RUN_BROKER_URL") == "http://127.0.0.1:8766"
    assert "SOME_RANDOM_SECRET" not in env
