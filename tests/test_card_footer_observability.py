"""Phase 6 (card-footer-observability) — footer metrics rendering tests.

Confirms that:
- ``_render_done_footer`` only emits the metrics line when FOOTER_SHOW_METRICS
  env flag is truthy AND the state actually carries metric fields.
- ``_format_metrics_line`` formats only the fields that are populated
  (tokens / cache hit % / context % / model name).
- ``update_streaming_card_metrics`` merges metrics into per-message state
  without firing any API call.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_multitenancy.card import ensure_feishu_cardkit_streaming
from hermes_multitenancy.card.builder import _format_metrics_line, _render_done_footer
from hermes_multitenancy.card.footer_config import get_show_metrics
from hermes_multitenancy.card.state import _new_state


def _state_with(**fields):
    state = _new_state()
    state.update(fields)
    return state


def test_footer_without_metrics_is_unchanged_when_flag_off():
    state = _state_with(finalized=True, started_at=1.0)
    with patch.dict(os.environ, {"FOOTER_SHOW_METRICS": "0"}, clear=False):
        footer = _render_done_footer(state)
    assert footer.startswith("Done (")
    assert "tokens" not in footer
    assert "cache" not in footer
    assert "model" not in footer


def test_footer_with_full_metrics_shows_all_fields_when_flag_on():
    state = _state_with(
        finalized=True,
        started_at=1.0,
        tokens_in=123,
        tokens_out=456,
        cache_hit_pct=78,
        context_pct=12,
        model_name="claude-opus-4-7",
    )
    with patch.dict(os.environ, {"FOOTER_SHOW_METRICS": "1"}, clear=False):
        footer = _render_done_footer(state)
    assert "Done (" in footer
    assert "tokens ↑123 ↓456" in footer
    assert "cache 78%" in footer
    assert "ctx 12%" in footer
    assert "model claude-opus-4-7" in footer


def test_footer_with_partial_metrics_shows_only_set_fields():
    state = _state_with(finalized=True, started_at=1.0, tokens_in=100)
    with patch.dict(os.environ, {"FOOTER_SHOW_METRICS": "true"}, clear=False):
        footer = _render_done_footer(state)
    assert "tokens ↑100" in footer
    # Out-only side absent
    assert "↓" not in footer
    assert "cache" not in footer
    assert "model" not in footer


def test_footer_flag_recognizes_multiple_truthy_values():
    state = _state_with(tokens_in=1)
    for truthy in ("1", "true", "yes", "on", "TRUE", "Yes"):
        with patch.dict(os.environ, {"FOOTER_SHOW_METRICS": truthy}, clear=False):
            assert get_show_metrics() is True


def test_footer_flag_is_off_by_default():
    env = dict(os.environ)
    env.pop("FOOTER_SHOW_METRICS", None)
    with patch.dict(os.environ, env, clear=True):
        assert get_show_metrics() is False


def test_format_metrics_line_empty_when_no_fields():
    line = _format_metrics_line(_new_state())
    assert line == ""


def test_format_metrics_line_with_tokens_in_only():
    state = _state_with(tokens_in=10)
    line = _format_metrics_line(state)
    assert line == "tokens ↑10"


def test_aborted_footer_with_metrics_uses_aborted_label():
    state = _state_with(
        finalized=True,
        aborted=True,
        started_at=1.0,
        tokens_in=5,
    )
    with patch.dict(os.environ, {"FOOTER_SHOW_METRICS": "1"}, clear=False):
        footer = _render_done_footer(state)
    assert footer.startswith("Aborted (")
    assert "tokens ↑5" in footer


# ---------------------------------------------------------------------------
# update_streaming_card_metrics — installed method, sync merge into state
# ---------------------------------------------------------------------------


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


def test_update_metrics_merges_into_state_without_api_call():
    adapter = _adapter_with_started_card()
    before_sends = len(adapter.card_sends)
    result = adapter.update_streaming_card_metrics(
        message_id="msg-1",
        tokens_in=200,
        tokens_out=400,
        model_name="claude-opus-4-7",
    )
    assert getattr(result, "success", False) is True
    # No new API call fired by the metrics merge.
    assert len(adapter.card_sends) == before_sends
    # State carries the merged values.
    from hermes_multitenancy.card.state import _states

    state = _states(adapter)["msg-1"]
    assert state["tokens_in"] == 200
    assert state["tokens_out"] == 400
    assert state["model_name"] == "claude-opus-4-7"
    # Unset fields stay None.
    assert state["cache_hit_pct"] is None
    assert state["context_pct"] is None


def test_update_metrics_on_unknown_message_id_returns_failure():
    adapter = ensure_feishu_cardkit_streaming(_MarkableAdapter())
    result = adapter.update_streaming_card_metrics(message_id="nope", tokens_in=1)
    assert getattr(result, "success", True) is False


def test_update_metrics_respects_unavailable_guard():
    from hermes_multitenancy.card.unavailable_guard import UnavailableGuard, _RECALLED_CODE

    adapter = _adapter_with_started_card()
    UnavailableGuard.mark_unavailable(adapter, "msg-1", _RECALLED_CODE)
    result = adapter.update_streaming_card_metrics(message_id="msg-1", tokens_in=99)
    assert getattr(result, "success", True) is False
    assert "card unavailable" in (getattr(result, "error", "") or "")
