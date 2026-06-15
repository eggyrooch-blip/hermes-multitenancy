from __future__ import annotations


def test_helpdesk_processors_attach_to_existing_dispatcher():
    """Injects helpdesk processors into an already-built dispatcher WITHOUT a new
    ws client, and without clobbering existing (IM) processors."""
    import pytest

    pytest.importorskip("lark_oapi")  # optional dep; present in the gateway runtime, maybe not in CI
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    from hermes_multitenancy import feishu_helpdesk_events as he

    # a dispatcher core already built, with a pre-existing customized event
    built = (
        EventDispatcherHandler.builder("", "")
        .register_p2_customized_event("drive.notice.comment_add_v1", lambda d: None)
        .build()
    )
    before = set(built._processorMap.keys())
    assert "p2.drive.notice.comment_add_v1" in before

    he._register_helpdesk_processors(built)
    after = set(built._processorMap.keys())

    assert "p2.helpdesk.ticket_message.created_v1" in after
    # ticket.created is intentionally NOT registered (handler doesn't act on it)
    assert "p2.helpdesk.ticket.created_v1" not in after
    assert before <= after  # existing processors preserved, nothing clobbered

    # idempotent: a second attach does not raise (keys already present are skipped)
    he._register_helpdesk_processors(built)


def test_handler_forwards_in_background(monkeypatch):
    """On event fire, the handler forwards the marshalled payload (fast-ack path)."""
    from hermes_multitenancy import feishu_helpdesk_events as he

    sent = {}
    monkeypatch.setattr(he, "_forward", lambda payload: sent.update(payload))
    # run submitted work synchronously for the test
    monkeypatch.setattr(he._executor, "submit", lambda fn, *a, **k: fn(*a, **k))

    handler = he._make_handler("helpdesk.ticket_message.created_v1")
    handler({"header": {"event_type": "helpdesk.ticket_message.created_v1"}, "event": {"ticket_id": "T1"}})
    assert sent.get("_hermes_event_type") == "helpdesk.ticket_message.created_v1"
    assert sent.get("event", {}).get("ticket_id") == "T1"


def test_prod_it_helpdesk_is_hard_denied():
    """The real company IT helpdesk id must be in the deny-weld set."""
    from hermes_multitenancy.feishu_helpdesk_event import DENY_HELPDESK_IDS
    assert "6909040876777979905" in DENY_HELPDESK_IDS


def test_allowlist_default_test_desk_and_prod_always_denied(monkeypatch):
    import importlib
    from hermes_multitenancy import feishu_helpdesk_event as m
    # prod IT desk must stay denied even if env tries to clear the deny list
    monkeypatch.setenv("HERMES_HELPDESK_DENY_IDS", "")
    importlib.reload(m)
    assert "6909040876777979905" in m.DENY_HELPDESK_IDS            # hardcoded, env can't remove
    assert "7651445701632691164" in m.ALLOWED_HELPDESK_IDS         # test desk allowlisted by default
    importlib.reload(m)  # restore default env state
