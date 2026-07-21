"""Regression: interactive form cards must skip CardKit and use the raw
interactive transport.

CardKit v1 ``card.create`` rejects schema-2.0 ``form`` components with
``code=11310 form's name is required`` even when the form carries a non-empty
``name`` (the confirm/fill card's form is built with ``name="push_fill_form"``
and still gets rejected). Live walkthrough 2026-07-21 saw every push-card
confirm card fail CardKit and fall back to raw. This locks in: a form-bearing
card never even attempts CardKit; a plain card still goes CardKit-first.
"""

import asyncio
from types import SimpleNamespace

from hermes_multitenancy import feishu_auth_cards as fac


_HEADER = {"title": {"tag": "plain_text", "content": "t"}, "template": "blue"}

# Form element carries a non-empty name on purpose — the fix must skip CardKit
# regardless, proving the bug is CardKit-rejects-forms, not a missing name.
FORM_CARD = {
    "schema": "2.0",
    "header": _HEADER,
    "body": {"elements": [
        {"tag": "markdown", "content": "请核对"},
        {"tag": "form", "name": "push_fill_form", "elements": [
            {"tag": "input", "name": "amount", "placeholder": {"tag": "plain_text", "content": "金额"}},
        ]},
    ]},
}

PLAIN_CARD = {
    "schema": "2.0",
    "header": _HEADER,
    "body": {"elements": [
        {"tag": "markdown", "content": "直接回复此卡即可填写"},
    ]},
}


class _FakeAdapter:
    def __init__(self, calls):
        self._calls = calls

    async def _feishu_send_with_retry(self, *, chat_id, msg_type, payload, reply_to, metadata):
        self._calls["raw"] += 1
        self._calls["last_payload"] = payload
        return SimpleNamespace(code=0)


def _patch(monkeypatch, calls):
    async def fake_create(adapter, card):
        calls["cardkit"] += 1
        return "card_xyz"

    monkeypatch.setattr(fac, "_can_use_cardkit", lambda adapter: True)
    monkeypatch.setattr(fac, "_create_cardkit_card", fake_create)
    monkeypatch.setattr(
        fac, "_finalize",
        lambda adapter, response, msg: SimpleNamespace(success=True, message_id="om_test"),
    )


def test_form_card_skips_cardkit_and_uses_raw(monkeypatch):
    calls = {"cardkit": 0, "raw": 0}
    _patch(monkeypatch, calls)
    adapter = _FakeAdapter(calls)

    result = asyncio.run(fac.send_auth_card(adapter=adapter, chat_id="oc_x", card=FORM_CARD))

    assert calls["cardkit"] == 0, "form card must NOT attempt CardKit card.create"
    assert calls["raw"] == 1, "form card must be sent via raw interactive"
    assert result and result["transport"] == "interactive"
    # The raw payload is the card JSON itself, not a card_id reference.
    assert "push_fill_form" in calls["last_payload"]


def test_plain_card_still_uses_cardkit(monkeypatch):
    calls = {"cardkit": 0, "raw": 0}
    _patch(monkeypatch, calls)
    adapter = _FakeAdapter(calls)

    result = asyncio.run(fac.send_auth_card(adapter=adapter, chat_id="oc_x", card=PLAIN_CARD))

    assert calls["cardkit"] == 1, "non-form card should still go CardKit-first"
    assert result and result["transport"] == "cardkit"


def test_card_has_form_detects_nested_form():
    assert fac._card_has_form(FORM_CARD) is True
    assert fac._card_has_form(PLAIN_CARD) is False
    assert fac._card_has_form({"body": {"elements": [{"tag": "column_set", "columns": [
        {"tag": "column", "elements": [{"tag": "form", "name": "x", "elements": []}]},
    ]}]}}) is True
