"""P1-7 — typed SessionScope: legacy parity (default) + strict isolation."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean_strict(monkeypatch):
    monkeypatch.delenv("HERMES_MULTITENANCY_STRICT_CONTEXT", raising=False)
    yield


# --------------------------------------------------------------------------- #
# fail-closed construction
# --------------------------------------------------------------------------- #

def test_scope_requires_channel():
    from hermes_multitenancy.session_scope import SessionScope

    with pytest.raises(ValueError, match="channel"):
        SessionScope(profile_name="p", channel="", user_key="ou_a")


def test_scope_requires_user_key():
    from hermes_multitenancy.session_scope import SessionScope

    with pytest.raises(ValueError, match="user_key"):
        SessionScope(profile_name="p", channel="feishu", user_key="")


def test_scope_is_frozen():
    from hermes_multitenancy.session_scope import build_session_scope

    s = build_session_scope(profile_name="p", user_key="ou_a")
    with pytest.raises(Exception):
        s.channel = "webui"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# default (strict off) → byte-identical to legacy keys
# --------------------------------------------------------------------------- #

def test_default_history_key_matches_legacy_tuple():
    from hermes_multitenancy.session_scope import build_session_scope

    s = build_session_scope(
        profile_name="feishu_alice",
        user_key="ou_alice",
        channel="feishu",
        chat_id="oc_x",
        thread_id="om_t",
        route_version=7,
    )
    # channel/chat/thread/version are ignored in default mode.
    assert s.history_key == ("feishu_alice", "ou_alice")


def test_default_inflight_key_matches_legacy_tuple():
    from hermes_multitenancy.session_scope import build_session_scope

    s = build_session_scope(
        profile_name="feishu_alice", user_key="ou_alice", chat_id="oc_x"
    )
    assert s.inflight_key == ("feishu_alice", "oc_x", "ou_alice")


def test_default_inflight_key_unrouted_and_unknown_fallbacks():
    from hermes_multitenancy.session_scope import build_session_scope

    s = build_session_scope(profile_name=None, user_key="ou_a", chat_id=None)
    assert s.inflight_key == ("_unrouted", "unknown", "ou_a")


def test_history_key_delegation_parity_with_router(monkeypatch):
    """The router's _history_key / _inflight_key must produce exactly the legacy
    tuples in default mode (proves the SessionScope wiring changed nothing)."""
    from hermes_multitenancy import router

    assert router._history_key("feishu_alice", "ou_alice", None) == (
        "feishu_alice",
        "ou_alice",
    )
    assert router._inflight_key("feishu_alice", "ou_alice", None, "oc_x") == (
        "feishu_alice",
        "oc_x",
        "ou_alice",
    )
    # sender falls back to alt when sender is empty/unknown (legacy semantics).
    assert router._history_key("p", "unknown", "u_alt") == ("p", "u_alt")


# --------------------------------------------------------------------------- #
# strict on → channel / chat / thread / route_version isolate sessions
# --------------------------------------------------------------------------- #

def test_strict_channels_do_not_cross(monkeypatch):
    from hermes_multitenancy.session_scope import build_session_scope

    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    keys = {
        build_session_scope(
            profile_name="feishu_alice", user_key="ou_alice", channel=ch
        ).history_key
        for ch in ("feishu", "webui", "cron")
    }
    assert len(keys) == 3  # DM vs webui vs cron never share a session


def test_strict_threads_do_not_cross(monkeypatch):
    from hermes_multitenancy.session_scope import build_session_scope

    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    t1 = build_session_scope(
        profile_name="feishu_g", user_key="ou_a", channel="feishu",
        chat_id="oc_g", thread_id="th1",
    )
    t2 = build_session_scope(
        profile_name="feishu_g", user_key="ou_a", channel="feishu",
        chat_id="oc_g", thread_id="th2",
    )
    assert t1.history_key != t2.history_key


def test_strict_route_version_invalidates_session(monkeypatch):
    from hermes_multitenancy.session_scope import build_session_scope

    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    v1 = build_session_scope(
        profile_name="p", user_key="ou_a", channel="feishu", route_version=1
    )
    v2 = build_session_scope(
        profile_name="p", user_key="ou_a", channel="feishu", route_version=2
    )
    assert v1.history_key != v2.history_key


def test_strict_history_key_still_two_tuple(monkeypatch):
    """SessionStore signature must not change — strict folds into the user-key
    dimension, never adds a tuple element."""
    from hermes_multitenancy.session_scope import build_session_scope

    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    s = build_session_scope(
        profile_name="p", user_key="ou_a", channel="feishu",
        chat_id="oc", thread_id="th", route_version=5,
    )
    assert len(s.history_key) == 2
    assert s.history_key[0] == "p"  # profile element unchanged


def test_strict_inflight_key_still_three_tuple(monkeypatch):
    from hermes_multitenancy.session_scope import build_session_scope

    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    s = build_session_scope(
        profile_name="p", user_key="ou_a", channel="webui",
        chat_id="oc", thread_id="th",
    )
    assert len(s.inflight_key) == 3
    assert s.inflight_key[0] == "p"
    assert s.inflight_key[1] == "oc"
