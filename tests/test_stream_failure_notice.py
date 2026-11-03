"""LiteLLM billing failures must surface a user-ready notice, not the generic
"中途出错" text (jiaomeijia 2026-08-10: 9 users over the $120 monthly cap all saw
the generic card and re-sent / filed tickets)."""
from __future__ import annotations

from hermes_multitenancy.router.streaming import (
    _BUDGET_EXCEEDED_NOTICE,
    _RATE_LIMIT_NOTICE,
    _stream_failure_content,
)
from hermes_multitenancy.agent_real import _PARTIAL_FAILURE_NOTICE


def _budget_exc() -> BaseException:
    # Real prod shape: _core re-wraps the finalize error, original 429 in __cause__.
    inner = RuntimeError(
        "AIAgent turn failed: HTTP 429: Budget has been exceeded! "
        "Current cost: 120.1246096361895, Max budget: 120.0"
    )
    outer = RuntimeError("streaming recovery exhausted")
    outer.__cause__ = inner
    return outer


def test_budget_exceeded_shows_quota_notice() -> None:
    out = _stream_failure_content("", None, exc=_budget_exc())
    assert out == _BUDGET_EXCEEDED_NOTICE
    assert _PARTIAL_FAILURE_NOTICE not in out


def test_plain_429_shows_rate_limit_notice() -> None:
    exc = RuntimeError("HTTP 429: Too many requests, slow down")
    assert _stream_failure_content("", None, exc=exc) == _RATE_LIMIT_NOTICE


def test_generic_exception_keeps_legacy_notice() -> None:
    exc = RuntimeError("tool crashed: boom")
    assert _stream_failure_content("", None, exc=exc) == _PARTIAL_FAILURE_NOTICE


def test_no_exc_keeps_legacy_notice() -> None:
    assert _stream_failure_content("", None) == _PARTIAL_FAILURE_NOTICE


def test_partial_text_gets_notice_appended_once() -> None:
    out = _stream_failure_content("已经写了一半的回答", None, exc=_budget_exc())
    assert out.startswith("已经写了一半的回答")
    assert out.endswith(_BUDGET_EXCEEDED_NOTICE)
    # idempotent: feeding the result back must not duplicate the notice
    again = _stream_failure_content(out, None, exc=_budget_exc())
    assert again.count(_BUDGET_EXCEEDED_NOTICE) == 1


def test_core_chinese_budget_message_without_cause() -> None:
    # _core raises _billing_failure_message copy; if a refactor drops `from exc`
    # the English 429 vanishes from the chain — the Chinese copy must still hit.
    exc = RuntimeError("该员工本月 LiteLLM 额度已用尽，请联系管理员调整额度或等待下月重置。")
    assert _stream_failure_content("", None, exc=exc) == _BUDGET_EXCEEDED_NOTICE


def test_core_chinese_budget_message_with_english_cause() -> None:
    exc = RuntimeError("该员工本月 LiteLLM 额度已用尽，请联系管理员调整额度或等待下月重置。")
    exc.__cause__ = RuntimeError(
        "AIAgent turn failed: HTTP 429: Budget has been exceeded! "
        "Current cost: 120.12, Max budget: 120.0"
    )
    assert _stream_failure_content("", None, exc=exc) == _BUDGET_EXCEEDED_NOTICE


def test_core_chinese_rate_limit_message_without_cause() -> None:
    exc = RuntimeError("LiteLLM 当前请求过于频繁，请稍后再试。")
    assert _stream_failure_content("", None, exc=exc) == _RATE_LIMIT_NOTICE


def test_self_referential_context_chain_terminates() -> None:
    exc = RuntimeError("outer")
    exc.__context__ = exc  # pathological loop must not hang
    assert _stream_failure_content("", None, exc=exc) == _PARTIAL_FAILURE_NOTICE


def _errored_card(content: str) -> dict:
    import time

    from hermes_multitenancy.card.builder import _render_message_card

    state = {
        "status": "",
        "reasoning": "",
        "content": content,
        "tools": [],
        "started_at": time.monotonic(),
        "finalized": True,
        "aborted": False,
        "errored": True,
    }
    return _render_message_card(state)


def test_budget_card_header_is_orange_quota() -> None:
    header = _errored_card(_BUDGET_EXCEEDED_NOTICE)["header"]
    assert header["template"] == "orange"
    assert header["title"]["content"] == "额度已用完"


def test_rate_limit_card_header_is_orange() -> None:
    header = _errored_card(_RATE_LIMIT_NOTICE)["header"]
    assert header["template"] == "orange"
    assert header["title"]["content"] == "请求过于频繁"


def test_generic_error_card_header_stays_red() -> None:
    header = _errored_card("partial answer\n\n" + _PARTIAL_FAILURE_NOTICE)["header"]
    assert header["template"] == "red"
    assert header["title"]["content"] == "执行出错"


def test_partial_plus_budget_notice_still_gets_quota_header() -> None:
    header = _errored_card("写了一半的回答\n\n" + _BUDGET_EXCEEDED_NOTICE)["header"]
    assert header["template"] == "orange"
    assert header["title"]["content"] == "额度已用完"


def test_non_errored_card_has_no_header() -> None:
    import time

    from hermes_multitenancy.card.builder import _render_message_card

    card = _render_message_card(
        {
            "status": "",
            "reasoning": "",
            "content": "正常回答",
            "tools": [],
            "started_at": time.monotonic(),
            "finalized": True,
            "aborted": False,
            "errored": False,
        }
    )
    assert "header" not in card


def test_stalled_tool_names_the_tool_not_generic_notice() -> None:
    from hermes_multitenancy.agent_real.streaming import AiagentToolStallTimeout

    inner = AiagentToolStallTimeout(300, tool_call_id="c1", tool_name="terminal", elapsed_s=305)
    outer = RuntimeError("streaming recovery exhausted")
    outer.__cause__ = inner
    out = _stream_failure_content("", None, exc=outer)
    assert out == inner.user_notice
    assert "terminal" in out and "305" in out
    assert _PARTIAL_FAILURE_NOTICE not in out
