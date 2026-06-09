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
from hermes_multitenancy.card.builder import _format_metrics_line, _render_done_footer, _render_message_card
from hermes_multitenancy.card.footer_config import get_show_metrics
from hermes_multitenancy.card.state import _new_state


def _state_with(**fields):
    state = _new_state()
    state.update(fields)
    return state


def _render_footer(state, *, now: float, show_metrics: str = "0"):
    with (
        patch("hermes_multitenancy.card.builder.time.monotonic", return_value=now),
        patch.dict(os.environ, {"FOOTER_SHOW_METRICS": show_metrics}, clear=False),
    ):
        return _render_done_footer(state)


def test_footer_without_metrics_defaults_to_zh_cn_but_keeps_en_us():
    state = _state_with(finalized=True, started_at=1.0)
    footer = _render_footer(state, now=12.3, show_metrics="0")
    assert footer["zh_cn"] == "已完成 · 耗时 11.3s"
    assert footer["en_us"] == "Completed · Elapsed 11.3s"


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
    footer = _render_footer(state, now=12.3, show_metrics="1")
    assert footer["zh_cn"].splitlines() == [
        "已完成 · 耗时 11.3s",
        "tokens ↑123 ↓456 · cache 78% · ctx 12% · model claude-opus-4-7",
    ]
    assert footer["en_us"].splitlines() == [
        "Completed · Elapsed 11.3s",
        "tokens ↑123 ↓456 · cache 78% · ctx 12% · model claude-opus-4-7",
    ]


def test_footer_with_partial_metrics_shows_only_set_fields():
    state = _state_with(finalized=True, started_at=1.0, tokens_in=100)
    footer = _render_footer(state, now=12.3, show_metrics="true")
    assert footer["zh_cn"].splitlines() == ["已完成 · 耗时 11.3s", "tokens ↑100"]
    assert footer["en_us"].splitlines() == ["Completed · Elapsed 11.3s", "tokens ↑100"]


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


def test_aborted_footer_uses_bilingual_stopped_labels():
    state = _state_with(
        finalized=True,
        aborted=True,
        started_at=1.0,
        tokens_in=5,
    )
    footer = _render_footer(state, now=12.3, show_metrics="1")
    assert footer["zh_cn"].splitlines() == ["已停止 · 耗时 11.3s", "tokens ↑5"]
    assert footer["en_us"].splitlines() == ["Stopped · Elapsed 11.3s", "tokens ↑5"]


def test_footer_elapsed_uses_minutes_and_seconds_when_over_one_minute():
    state = _state_with(finalized=True, started_at=1.0)
    footer = _render_footer(state, now=66.2, show_metrics="0")
    assert footer["zh_cn"] == "已完成 · 耗时 1m 5s"
    assert footer["en_us"] == "Completed · Elapsed 1m 5s"


def test_footer_metrics_line_is_absent_when_flag_on_but_no_metric_fields():
    state = _state_with(finalized=True, started_at=1.0)
    footer = _render_footer(state, now=12.3, show_metrics="1")
    assert footer["zh_cn"] == "已完成 · 耗时 11.3s"
    assert footer["en_us"] == "Completed · Elapsed 11.3s"
    assert "\n" not in footer["zh_cn"]
    assert "\n" not in footer["en_us"]


def test_footer_with_model_name_only_renders_model_line_without_token_arrows():
    state = _state_with(finalized=True, started_at=1.0, model_name="claude-opus-4-7")
    footer = _render_footer(state, now=12.3, show_metrics="1")
    assert footer["zh_cn"].splitlines() == ["已完成 · 耗时 11.3s", "model claude-opus-4-7"]
    assert footer["en_us"].splitlines() == ["Completed · Elapsed 11.3s", "model claude-opus-4-7"]
    assert "↑" not in footer["zh_cn"]
    assert "↓" not in footer["zh_cn"]


def test_render_message_card_footer_uses_i18n_content():
    state = _state_with(finalized=True, started_at=1.0)
    with patch("hermes_multitenancy.card.builder.time.monotonic", return_value=12.3):
        card = _render_message_card(state)
    footer = card["elements"][-1]
    assert footer["tag"] == "markdown"
    assert footer["content"] == "已完成 · 耗时 11.3s"
    assert footer["i18n_content"] == {
        "zh_cn": "已完成 · 耗时 11.3s",
        "en_us": "Completed · Elapsed 11.3s",
    }
    assert footer["text_size"] == "notation"


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
