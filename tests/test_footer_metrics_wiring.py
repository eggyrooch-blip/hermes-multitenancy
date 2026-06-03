from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import patch

from hermes_multitenancy.agent_real import (
    _build_aiagent_metrics_payload,
    _sanitize_metrics_event_payload,
)
from hermes_multitenancy.card import ensure_feishu_cardkit_streaming
from hermes_multitenancy.card.builder import _render_done_footer
from hermes_multitenancy.card.state import _states


class _MarkableAdapter:
    def __init__(self):
        self.sent: list = []
        self.card_sends: list = []

    def _patch_auth_card(self, message_id, card):
        self.sent.append({"message_id": message_id, "card": card})
        return True

    async def _feishu_send_with_retry(self, *, chat_id, msg_type, payload, reply_to=None, metadata=None):
        self.card_sends.append({"chat_id": chat_id, "payload": payload})
        return SimpleNamespace(success=True, message_id="msg-1")

    def _finalize_send_result(self, response, default_message):
        return SimpleNamespace(
            success=bool(getattr(response, "success", False)),
            message_id=getattr(response, "message_id", None),
        )


def _adapter_with_started_card():
    adapter = ensure_feishu_cardkit_streaming(_MarkableAdapter())
    asyncio.run(adapter.start_streaming_card(chat_id="chat-1"))
    return adapter


def test_build_aiagent_metrics_payload_maps_run_conversation_result():
    payload = _build_aiagent_metrics_payload(
        {
            "prompt_tokens": 400,
            "completion_tokens": 120,
            "cache_read_tokens": 100,
            "model": "claude-opus-4-7",
        }
    )

    assert payload == {
        "tokens_in": 400,
        "tokens_out": 120,
        "cache_hit_pct": 25.0,
        "context_pct": None,
        "model_name": "claude-opus-4-7",
    }


def test_sanitize_metrics_event_payload_drops_invalid_values():
    payload = _sanitize_metrics_event_payload(
        {
            "tokens_in": 321,
            "tokens_out": True,
            "cache_hit_pct": float("nan"),
            "context_pct": float("inf"),
            "model_name": "  claude-sonnet-4  ",
        }
    )

    assert payload == {
        "tokens_in": 321,
        "model_name": "claude-sonnet-4",
    }


def test_update_streaming_card_metrics_renders_footer_metrics():
    adapter = _adapter_with_started_card()
    payload = _build_aiagent_metrics_payload(
        {
            "prompt_tokens": 512,
            "completion_tokens": 128,
            "cache_read_tokens": 64,
            "model": "claude-opus-4-7",
        }
    )

    result = adapter.update_streaming_card_metrics(message_id="msg-1", **payload)

    assert getattr(result, "success", False) is True
    state = _states(adapter)["msg-1"]
    assert state["tokens_in"] == 512
    assert state["tokens_out"] == 128
    assert state["cache_hit_pct"] == 12.5
    assert state["context_pct"] is None
    assert state["model_name"] == "claude-opus-4-7"

    state["finalized"] = True
    state["started_at"] = 1.0
    with patch.dict(os.environ, {"FOOTER_SHOW_METRICS": "1"}, clear=False):
        footer = _render_done_footer(state)

    assert "tokens ↑512 ↓128" in footer
    assert "cache 12%" in footer
    assert "model claude-opus-4-7" in footer
