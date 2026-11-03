"""Cache 防串号: the optional status cache must never cross profiles/users.

The registry's optional short-TTL cache is keyed by ``(profile, open_id,
scope)``. These tests prove (1) profile A's cached value never serves profile B,
(2) a within-ttl repeat for the same (profile, open_id) reuses the cache (reader
called once), and (3) the cache key is composed from profile + open_id + scope
so none of those axes can collide.

The underlying reader ``credential_hub._collect_credential_rows`` is replaced by
a profile-stamped stub with a call counter, so a cross-profile leak is directly
observable in the returned data and the count.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_connector_cache():
    from hermes_multitenancy.connectors import registry

    registry.clear_cache()
    yield
    registry.clear_cache()


def _stamped_reader():
    """A _collect_credential_rows replacement: stamps the profile + counts calls."""
    from hermes_multitenancy.credential_hub import CredentialRow

    calls = {"n": 0}

    def reader(*, profile_name, open_id, shared_home=None, home_dir=None):
        calls["n"] += 1
        return [
            CredentialRow(
                id="lark-cli",
                title="Lark-cli",
                provider="lark",
                installed=True,
                status="authenticated",
                account_hint=profile_name,  # stamp: proves which profile produced it
                detail=f"profile={profile_name};open_id={open_id}",
                action={"kind": "feishu_device_flow", "label": "重新授权"},
            )
        ]

    return reader, calls


def test_cache_does_not_serve_profile_a_value_to_profile_b(monkeypatch):
    from hermes_multitenancy import credential_hub
    from hermes_multitenancy.connectors import registry

    reader, _calls = _stamped_reader()
    monkeypatch.setattr(credential_hub, "_collect_credential_rows", reader)

    a = registry.collect_connector_statuses(profile_name="A", open_id="ua", use_cache=True)
    b = registry.collect_connector_statuses(profile_name="B", open_id="ub", use_cache=True)

    assert a[0].account_hint == "A"
    assert a[0].profile == "A"
    assert b[0].account_hint == "B"  # B never served A's cached row
    assert b[0].profile == "B"


def test_same_profile_open_id_within_ttl_uses_cache(monkeypatch):
    from hermes_multitenancy import credential_hub
    from hermes_multitenancy.connectors import registry

    reader, calls = _stamped_reader()
    monkeypatch.setattr(credential_hub, "_collect_credential_rows", reader)

    first = registry.collect_connector_statuses(profile_name="A", open_id="ua", use_cache=True)
    second = registry.collect_connector_statuses(profile_name="A", open_id="ua", use_cache=True)

    assert calls["n"] == 1  # reader hit only once; second call served from cache
    assert first[0].account_hint == "A"
    assert second[0].account_hint == "A"


def test_use_cache_false_never_serves_from_cache(monkeypatch):
    # use_cache=False always READS LIVE (never serves a cached value) — the reader
    # runs on every call. It does still refresh the cache afterwards (so a later
    # cached read isn't stale), but it never SERVES from it; that's the invariant the
    # post-auth poll relies on.
    from hermes_multitenancy import credential_hub
    from hermes_multitenancy.connectors import registry

    reader, calls = _stamped_reader()
    monkeypatch.setattr(credential_hub, "_collect_credential_rows", reader)

    registry.collect_connector_statuses(profile_name="A", open_id="ua", use_cache=False)
    registry.collect_connector_statuses(profile_name="A", open_id="ua", use_cache=False)
    assert calls["n"] == 2  # bypasses the cached read each time → reader hit every call


def test_cache_key_includes_profile_open_id_and_scope():
    from hermes_multitenancy.connectors import registry

    base = registry._cache_key("A", "ua")
    assert registry._cache_key("B", "ua") != base  # profile axis
    assert registry._cache_key("A", "ub") != base  # open_id axis
    assert registry._cache_key("A", "ua", "tenant") != registry._cache_key("A", "ua", "profile")
