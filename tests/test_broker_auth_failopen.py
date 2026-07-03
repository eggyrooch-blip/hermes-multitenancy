"""Audit §4 fail-open hardening (2026-07-03): `_authorized` returned True for
EVERY request when no broker key was configured. The prod startup path already
refuses a keyless bind, but the auth decision should not trust that alone — a
keyless broker bound to a non-loopback host must fail CLOSED.

FAILS on pre-fix code (non-loopback + no key returned True → open to the network).
"""
from __future__ import annotations

import pytest

from hermes_multitenancy import webui_broker_server as b


class _FakeReq:
    def __init__(self, headers=None):
        self.headers = headers or {}


def test_no_key_loopback_stays_open(monkeypatch):
    monkeypatch.setattr(b, "_run_broker_key", lambda: "")
    for host in ("127.0.0.1", "::1", "localhost"):
        monkeypatch.setattr(b, "_run_broker_host", lambda h=host: h)
        assert b._authorized(_FakeReq()) is True  # dev convenience preserved


def test_no_key_nonloopback_fails_closed(monkeypatch):
    monkeypatch.setattr(b, "_run_broker_key", lambda: "")
    for host in ("0.0.0.0", "203.0.113.5", "broker.example"):  # non-loopback (docs-safe)
        monkeypatch.setattr(b, "_run_broker_host", lambda h=host: h)
        assert b._authorized(_FakeReq()) is False  # keyless off-box bind → CLOSED


def test_with_key_still_checks_bearer(monkeypatch):
    monkeypatch.setattr(b, "_run_broker_key", lambda: "secret")
    assert b._authorized(_FakeReq({"Authorization": "Bearer secret"})) is True
    assert b._authorized(_FakeReq({"Authorization": "Bearer wrong"})) is False
    assert b._authorized(_FakeReq({})) is False
