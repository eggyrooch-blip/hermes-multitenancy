from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_multitenancy.agent_real import (
    _finalize_aiagent_result,
    _is_output_truncation_error,
    _TRUNCATION_NOTICE,
)


def _event():
    return SimpleNamespace(text="hello", message_id="om_x",
                           source=SimpleNamespace(platform="feishu", chat_id="oc_x", user_id="ou_x"))


@pytest.mark.asyncio
async def test_streaming_truncation_appends_notice_after_partial_content(monkeypatch, tmp_path):
    """codex review: when partial content already streamed, the truncation notice
    (done payload) must still reach the user as a trailing delta, not be dropped."""
    from hermes_multitenancy import agent_real

    async def fake_stream(event, profile_home):
        yield ("content", "前半段回答……")           # partial content streamed into the card
        yield ("done", _TRUNCATION_NOTICE)            # core truncated -> finalize returned the notice

    async def fail_subprocess(*_a, **_k):
        raise AssertionError("should consume the event-stream subprocess")

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", fake_stream, raising=False)
    monkeypatch.setattr(agent_real, "_run_aiagent_subprocess", fail_subprocess)

    chunks = [c async for c in agent_real.stream_run_agent(_event(), tmp_path)]
    # partial content + the appended truncation notice (the user gets the hint)
    assert ("content", "前半段回答……") in chunks
    assert any(kind == "content" and _TRUNCATION_NOTICE in str(text) for kind, text in chunks)


@pytest.mark.asyncio
async def test_streaming_normal_completion_does_not_append_notice(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real

    async def fake_stream(event, profile_home):
        yield ("content", "完整回答")
        yield ("done", "完整回答")  # normal: done == streamed content

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", fake_stream, raising=False)
    chunks = [c async for c in agent_real.stream_run_agent(_event(), tmp_path)]
    # no truncation notice on a normal turn; no duplicate of the answer
    assert all(_TRUNCATION_NOTICE not in str(t) for _, t in chunks)
    assert [t for k, t in chunks if k == "content"] == ["完整回答"]


def test_success_returns_final_response():
    assert _finalize_aiagent_result({"final_response": "hello"}) == "hello"


def test_empty_but_successful_returns_empty_string():
    # final_response "" with no failed flag is a genuine empty success.
    assert _finalize_aiagent_result({"final_response": ""}) == ""


def test_partial_truncation_returns_notice_not_raise():
    # core rollback case: partial=True, final_response=None, completed=False
    res = {
        "final_response": None,
        "partial": True,
        "error": "Response truncated due to output length limit",
    }
    assert _finalize_aiagent_result(res) == _TRUNCATION_NOTICE


def test_first_message_truncation_failed_returns_notice():
    # first-message truncation: failed=True but the error is truncation -> graceful, not raise
    res = {
        "final_response": None,
        "failed": True,
        "error": "First response truncated due to output length limit",
    }
    assert _finalize_aiagent_result(res) == _TRUNCATION_NOTICE


def test_genuine_failure_still_raises():
    # provider HTTP 400 etc -> must still raise so the model-unavailable fallback fires
    res = {"final_response": None, "failed": True, "error": "litellm.BadRequestError: 400"}
    with pytest.raises(RuntimeError, match="AIAgent turn failed"):
        _finalize_aiagent_result(res)


def test_none_final_response_without_truncation_still_raises():
    res = {"final_response": None, "error": "agent crashed somewhere"}
    with pytest.raises(RuntimeError):
        _finalize_aiagent_result(res)


def test_is_output_truncation_error_matching():
    assert _is_output_truncation_error("Response truncated due to output length limit")
    assert _is_output_truncation_error("First response truncated due to output length limit")
    assert not _is_output_truncation_error("litellm.BadRequestError: 400")
    assert not _is_output_truncation_error("")
    assert not _is_output_truncation_error(None)
    # 'truncated' alone (e.g. a different truncation) must not match without 'output length'
    assert not _is_output_truncation_error("context truncated")
