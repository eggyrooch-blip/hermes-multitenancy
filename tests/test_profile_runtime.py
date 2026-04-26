"""US-003 verification: ProfileRuntime monkey-patches HERMES_HOME during dispatch."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


def _force_reload_hermes_constants():
    """Drop the cached hermes_constants module so a fresh import reads env."""
    # We don't actually need to evict — get_hermes_home() reads env on every call.
    # This helper exists to make the contract explicit in tests.
    pass


@pytest.mark.asyncio
async def test_dispatch_switches_hermes_home_during_run():
    """Mid-dispatch, hermes_constants.get_hermes_home() must return profile_home."""
    sys.path.insert(0, "/Users/kite/.hermes/hermes-agent")
    try:
        import hermes_constants  # type: ignore
    finally:
        sys.path.pop(0)

    from hermes_multitenancy.runtime import ProfileRuntime

    with tempfile.TemporaryDirectory() as tmp:
        profile_home = Path(tmp) / "spike_profile_X"
        profile_home.mkdir()

        seen = {}

        async def runner(event, home):
            # Inside dispatch — assert the env var is switched
            seen["env_during"] = os.environ.get("HERMES_HOME")
            seen["constants_during"] = str(hermes_constants.get_hermes_home())
            return "ok"

        original_env = os.environ.get("HERMES_HOME")
        result = await ProfileRuntime(profile_home=profile_home, run_agent_fn=runner).dispatch(
            SimpleNamespace(text="test")
        )

        # 1. Dispatch returned the runner's value
        assert result == "ok"

        # 2. During dispatch, env var was the profile_home
        assert seen["env_during"] == str(profile_home)

        # 3. During dispatch, hermes_constants saw the same value (no caching)
        assert seen["constants_during"] == str(profile_home)

        # 4. After dispatch, env var is restored
        assert os.environ.get("HERMES_HOME") == original_env


@pytest.mark.asyncio
async def test_dispatch_restores_on_exception():
    """If runner raises, HERMES_HOME must still restore (try/finally)."""
    from hermes_multitenancy.runtime import ProfileRuntime

    with tempfile.TemporaryDirectory() as tmp:
        profile_home = Path(tmp)

        async def boom(event, home):
            raise RuntimeError("expected boom")

        original_env = os.environ.get("HERMES_HOME")

        with pytest.raises(RuntimeError, match="expected boom"):
            await ProfileRuntime(profile_home=profile_home, run_agent_fn=boom).dispatch(None)

        assert os.environ.get("HERMES_HOME") == original_env, "HERMES_HOME must be restored after exception"


@pytest.mark.asyncio
async def test_default_runner_returns_echo_with_profile_name():
    """Default runner is a Spike stub — echoes input prefixed with profile name."""
    from hermes_multitenancy.runtime import ProfileRuntime

    with tempfile.TemporaryDirectory() as tmp:
        profile_home = Path(tmp) / "alpha"
        profile_home.mkdir()
        result = await ProfileRuntime(profile_home=profile_home).dispatch(
            SimpleNamespace(text="hello")
        )
        assert "[profile=alpha]" in result
        assert "hello" in result
