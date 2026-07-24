"""Regression: callback-capable forms use CardKit when it is available."""

import asyncio
import json
from types import SimpleNamespace

from hermes_multitenancy import feishu_auth_cards as fac


_HEADER = {"title": {"tag": "plain_text", "content": "t"}, "template": "blue"}

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


def test_form_card_uses_cardkit_without_raw_fallback(monkeypatch):
    calls = {"cardkit": 0, "raw": 0}
    _patch(monkeypatch, calls)
    adapter = _FakeAdapter(calls)

    result = asyncio.run(fac.send_auth_card(adapter=adapter, chat_id="oc_x", card=FORM_CARD))

    assert calls["cardkit"] == 1
    assert calls["raw"] == 1
    assert result and result["transport"] == "cardkit"
    assert json.loads(calls["last_payload"]) == {
        "type": "card",
        "data": {"card_id": "card_xyz"},
    }


def test_plain_card_still_uses_cardkit(monkeypatch):
    calls = {"cardkit": 0, "raw": 0}
    _patch(monkeypatch, calls)
    adapter = _FakeAdapter(calls)

    result = asyncio.run(fac.send_auth_card(adapter=adapter, chat_id="oc_x", card=PLAIN_CARD))

    assert calls["cardkit"] == 1, "non-form card should still go CardKit-first"
    assert result and result["transport"] == "cardkit"
