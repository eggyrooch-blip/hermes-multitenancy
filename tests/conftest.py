"""Test isolation: reset module-level singletons before/after every test.

Without this, module-level RoutingTable / RuntimePool / SessionStore
default to disk paths (~/.hermes/multitenancy.db etc) and accumulate state
across tests — causing flaky test_session_memory_accumulates_across_turns
when an earlier test writes the same DB.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _enable_security_audit(monkeypatch):
    """Security audit now defaults OFF in prod (so a default-off deploy is
    byte-identical — no /var/log JSONL side-effect). The test suite asserts
    audit CONTENT (redaction/hashing/event presence), so it opts audit ON here.
    The dedicated default-off behavior test overrides this env explicitly."""
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_ENABLED", "1")


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
