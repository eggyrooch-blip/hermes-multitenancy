"""Regression: cron deliveries must render as interactive Feishu cards.

Historically the cron worker delivered scheduled-task output via
``adapter.send`` as ``msg_type="text"``, which flattens markdown (bullets,
bold, links) — unlike normal replies that stream as interactive CardKit cards.
``_build_cron_card`` + ``_send_cron_card_via_live_adapter`` render a simple
interactive card instead, gated by ``cron.card_response`` (default on) with a
plain-text fallback so a card failure never drops the delivery.

These tests fail without the card path (delivery stays plain text).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from hermes_multitenancy import cron_worker
from hermes_multitenancy.cron_worker import (
    _build_cron_card,
    _cron_card_response_enabled,
    _deliver_cron_feishu_via_live_adapter,
    _send_cron_card_via_live_adapter,
)

_MARKDOWN_BODY = "Here is your update:\n- **bold** item\n- [link](https://example.com)"


# --------------------------------------------------------------------------- #
# _build_cron_card                                                            #
# --------------------------------------------------------------------------- #
def test_build_cron_card_renders_markdown_card():
    job = {"id": "2515da283456", "name": "双周会前一天提醒"}
    card, media = _build_cron_card(job, _MARKDOWN_BODY)

    assert card is not None
    # Header carries the task name (interactive card, not flattened text).
    assert card["header"]["title"]["content"].endswith("双周会前一天提醒")
    # Body is a markdown element that preserves the markdown verbatim.
    body_elements = [e for e in card["elements"] if e.get("tag") == "markdown"]
    assert any("**bold**" in e["content"] for e in body_elements)
    assert any("[link](https://example.com)" in e["content"] for e in body_elements)
    # A "stop this job" footer is present (wrap_response default on).
    assert any("stop reminder 双周会前一天提醒" in e["content"] for e in body_elements)
    assert media == []


def test_build_cron_card_empty_body_returns_none():
    card, media = _build_cron_card({"id": "x", "name": "n"}, "   \n  ")
    assert card is None  # caller falls back to plain text
    assert media == []


def test_card_response_enabled_defaults_true():
    # cron.config is unavailable in the test env → must default ON.
    assert _cron_card_response_enabled() is True


# --------------------------------------------------------------------------- #
# _send_cron_card_via_live_adapter                                            #
# --------------------------------------------------------------------------- #
class _FakeFuture:
    def __init__(self, result):
        self._result = result

    def result(self, timeout=None):
        return self._result

    def cancel(self):  # pragma: no cover - not exercised on success
        pass


def test_send_cron_card_uses_interactive_msg_type(monkeypatch):
    captured = {}

    def fake_adapter_send(**kwargs):
        captured.update(kwargs)
        return "coro-sentinel"

    adapter = SimpleNamespace(_feishu_send_with_retry=fake_adapter_send)

    def fake_schedule(coro, loop):
        assert coro == "coro-sentinel"
        return _FakeFuture(SimpleNamespace(success=True, message_id="om_1")), None

    monkeypatch.setattr(cron_worker, "_schedule_on_gateway_loop", fake_schedule)

    card = {"elements": [{"tag": "markdown", "content": "hi"}]}
    err = _send_cron_card_via_live_adapter(adapter, "ou_abc", card, None, object())

    assert err is None
    assert captured["msg_type"] == "interactive"
    assert json.loads(captured["payload"]) == card


def test_send_cron_card_returns_error_on_failure(monkeypatch):
    adapter = SimpleNamespace(_feishu_send_with_retry=lambda **k: "coro")
    monkeypatch.setattr(
        cron_worker,
        "_schedule_on_gateway_loop",
        lambda coro, loop: (_FakeFuture(SimpleNamespace(success=False, error="boom")), None),
    )
    err = _send_cron_card_via_live_adapter(adapter, "ou_abc", {"elements": []}, None, object())
    assert err is not None and "boom" in err


# --------------------------------------------------------------------------- #
# delivery: prefers card, falls back to text                                  #
# --------------------------------------------------------------------------- #
def _fake_scheduler(target):
    return SimpleNamespace(_resolve_delivery_targets=lambda job: [target])


def _running_loop():
    return SimpleNamespace(is_running=lambda: True)


def test_delivery_prefers_card_over_text(monkeypatch):
    target = {"platform": "feishu", "chat_id": "ou_abc"}
    adapter = SimpleNamespace(_feishu_send_with_retry=lambda **k: None, send=lambda *a, **k: None)

    monkeypatch.setattr(cron_worker, "_adapter_for_platform", lambda adapters, name: adapter)
    monkeypatch.setattr(cron_worker, "_cron_card_response_enabled", lambda: True)

    calls = {"card": 0, "text": 0}
    monkeypatch.setattr(
        cron_worker,
        "_send_cron_card_via_live_adapter",
        lambda *a, **k: calls.__setitem__("card", calls["card"] + 1) or None,
    )

    def fail_if_text(coro, loop):
        calls["text"] += 1
        return None, "text path should not run when card succeeds"

    monkeypatch.setattr(cron_worker, "_schedule_on_gateway_loop", fail_if_text)

    err = _deliver_cron_feishu_via_live_adapter(
        _fake_scheduler(target),
        {"id": "j1", "name": "task"},
        _MARKDOWN_BODY,
        adapters={"feishu": adapter},
        loop=_running_loop(),
    )
    assert err is None
    assert calls["card"] == 1
    assert calls["text"] == 0  # card success → text path skipped


def test_delivery_falls_back_to_text_when_card_fails(monkeypatch):
    target = {"platform": "feishu", "chat_id": "ou_abc"}
    adapter = SimpleNamespace(_feishu_send_with_retry=lambda **k: None, send=lambda *a, **k: "text-coro")

    monkeypatch.setattr(cron_worker, "_adapter_for_platform", lambda adapters, name: adapter)
    monkeypatch.setattr(cron_worker, "_cron_card_response_enabled", lambda: True)
    monkeypatch.setattr(
        cron_worker, "_send_cron_card_via_live_adapter", lambda *a, **k: "card boom"
    )

    text_calls = {"n": 0}

    def fake_schedule(coro, loop):
        text_calls["n"] += 1
        assert coro == "text-coro"  # the plain-text send was attempted
        return _FakeFuture(SimpleNamespace(success=True)), None

    monkeypatch.setattr(cron_worker, "_schedule_on_gateway_loop", fake_schedule)

    err = _deliver_cron_feishu_via_live_adapter(
        _fake_scheduler(target),
        {"id": "j1", "name": "task"},
        _MARKDOWN_BODY,
        adapters={"feishu": adapter},
        loop=_running_loop(),
    )
    assert err is None
    assert text_calls["n"] == 1  # fell back to plain text after card failure
