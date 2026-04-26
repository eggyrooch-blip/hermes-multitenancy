"""Test isolation: reset module-level singletons before/after every test.

Without this, module-level RoutingTable / RuntimePool / SessionStore
default to disk paths (~/.hermes/multitenancy.db etc) and accumulate state
across tests — causing flaky test_session_memory_accumulates_across_turns
when an earlier test writes the same DB.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_router_singletons():
    """Reset router-level singletons before and after each test."""
    from hermes_multitenancy import router

    # Pre: clean slate (covers tests that don't explicitly set up state)
    router.override_routing_table(":memory:")
    router.override_pool(None)
    router.override_session_store(":memory:")
    router._session_history.clear()
    router._session_loaded.clear()
    router._user_inflight_tasks.clear()

    yield

    # Post: same clean teardown
    router.override_routing_table(":memory:")
    router.override_pool(None)
    router.override_session_store(":memory:")
    router._session_history.clear()
    router._session_loaded.clear()
    router._user_inflight_tasks.clear()
