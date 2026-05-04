"""Phase 3 — real LLM integration test (calls live API).

Run only when explicitly opted in:
    pytest tests/test_real_llm_integration.py -v

Marked ``integration`` so default ``pytest tests/`` skips them by default
(see pytest.ini for the marker filter).
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.integration

DEFAULT_PROFILE_HOME = Path(
    os.environ.get("HERMES_INTEGRATION_PROFILE_HOME", str(Path.home() / ".hermes"))
).expanduser()


def _has_credentials() -> bool:
    """Skip these tests if the environment has no usable LLM credentials.

    Mirrors the providers in ``agent_real._PROVIDER_ENV_KEYS`` — keep in sync.
    """
    from dotenv import dotenv_values
    from hermes_multitenancy.agent_real import _PROVIDER_ENV_KEYS

    env = dotenv_values(DEFAULT_PROFILE_HOME / ".env")
    candidates = {name for names in _PROVIDER_ENV_KEYS.values() for name in names}
    return any(env.get(k) or os.environ.get(k) for k in candidates)


@pytest.mark.skipif(not DEFAULT_PROFILE_HOME.exists(), reason="default profile not present")
@pytest.mark.skipif(not _has_credentials(), reason="no LLM API keys available")
@pytest.mark.asyncio
async def test_real_run_agent_against_default_profile():
    """Smoke test: real_run_agent uses default profile config + creds and gets a real reply."""
    from hermes_multitenancy.agent_real import real_run_agent

    event = SimpleNamespace(text="Reply with exactly the single token: pong")
    response = await real_run_agent(event, DEFAULT_PROFILE_HOME)
    assert isinstance(response, str)
    assert response.strip(), "live LLM response was empty/whitespace"
    # Don't assert exact text — different LLMs may add formatting. Just prove
    # we got a non-empty string back from a real network call.


@pytest.mark.skipif(not DEFAULT_PROFILE_HOME.exists(), reason="default profile not present")
@pytest.mark.skipif(not _has_credentials(), reason="no LLM API keys available")
@pytest.mark.asyncio
async def test_profile_runtime_with_real_agent():
    """End-to-end: ProfileRuntime → real_run_agent → live LLM, with HERMES_HOME switched."""
    from hermes_multitenancy.runtime import ProfileRuntime
    from hermes_multitenancy.agent_real import real_run_agent

    rt = ProfileRuntime(profile_home=DEFAULT_PROFILE_HOME, run_agent_fn=real_run_agent)
    event = SimpleNamespace(text="Reply with exactly: tenant-test-ok")
    response = await rt.dispatch(event)
    assert isinstance(response, str)
    assert response.strip(), "live LLM response was empty/whitespace"


@pytest.mark.skipif(not DEFAULT_PROFILE_HOME.exists(), reason="default profile not present")
@pytest.mark.skipif(not _has_credentials(), reason="no LLM API keys available")
@pytest.mark.asyncio
async def test_pool_dispatches_real_agent_through_default_profile():
    """Pool → ProfileRuntime → real_run_agent stack works against live API."""
    from hermes_multitenancy.pool import RuntimePool
    from hermes_multitenancy.runtime import ProfileRuntime
    from hermes_multitenancy.agent_real import real_run_agent

    def factory(profile_name, profile_home):
        return ProfileRuntime(profile_home=profile_home, run_agent_fn=real_run_agent)

    pool = RuntimePool(runtime_factory=factory)
    event = SimpleNamespace(text="say 'hello' in one word")
    response = await pool.dispatch("default", DEFAULT_PROFILE_HOME, event)
    assert isinstance(response, str)
    assert response.strip(), "live LLM response was empty/whitespace"
    assert "default" in pool.loaded_profiles()
