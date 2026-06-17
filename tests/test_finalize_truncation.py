from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_multitenancy.agent_real import (
    _finalize_aiagent_result,
    _is_output_truncation_error,
    _TRUNCATION_NOTICE,
    _PARTIAL_FAILURE_NOTICE,
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


def test_invalid_tool_call_partial_raises_not_notice():
    # REGRESSION: core returns partial=True for a hallucinated tool name (#1).
    # This is NOT "回复太长" — it must raise so the legacy fallback retries,
    # never surface the truncation notice. (Fails before the partial-notice fix.)
    res = {
        "final_response": None,
        "partial": True,
        "completed": False,
        "error": "Model generated invalid tool call: frobnicate_widget",
    }
    with pytest.raises(RuntimeError, match="AIAgent turn failed"):
        _finalize_aiagent_result(res)


def test_partial_with_salvageable_text_keeps_content_and_appends_notice():
    # Genuine answer-length truncation that still produced partial text: the user
    # must get the partial answer AND the continue hint — never the notice alone.
    res = {
        "final_response": "这是回答的前半部分",
        "partial": True,
        "error": "Response truncated due to output length limit",
    }
    out = _finalize_aiagent_result(res)
    assert "这是回答的前半部分" in out
    assert _TRUNCATION_NOTICE in out


def test_failed_true_with_text_keeps_content_not_notice_only():
    # codex review finding #3: a length-truncated turn can carry rolled-back text
    # AND failed=True. Content must be preserved (text + hint), never dropped to
    # the notice alone.
    res = {
        "final_response": "已经生成的一大段回答",
        "failed": True,
        "partial": True,
        "error": "Response truncated due to output length limit",
    }
    out = _finalize_aiagent_result(res)
    assert "已经生成的一大段回答" in out
    assert _TRUNCATION_NOTICE in out


def test_failed_true_with_text_non_truncation_returns_text():
    # Even a non-truncation failure must not throw away usable answer text.
    res = {"final_response": "有用的部分答案", "failed": True, "error": "some tool hiccup"}
    assert _finalize_aiagent_result(res) == "有用的部分答案"


@pytest.mark.asyncio
async def test_streaming_failure_after_partial_content_does_not_duplicate(monkeypatch, tmp_path):
    """If content already streamed and the subprocess then raises, we must NOT
    re-run the legacy stream (that duplicates the visible answer). Append an
    honest failure hint and stop."""
    from hermes_multitenancy import agent_real

    async def fake_stream(event, profile_home):
        yield ("content", "已经显示给用户的一段")
        raise RuntimeError("AIAgent subprocess failed: AIAgent turn failed: invalid tool call")

    def legacy_must_not_run(*_a, **_k):
        raise AssertionError("legacy _stream_loop must not re-stream after partial content")

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", fake_stream, raising=False)
    monkeypatch.setattr(agent_real, "_stream_loop", legacy_must_not_run)

    chunks = [c async for c in agent_real.stream_run_agent(_event(), tmp_path)]
    texts = [t for k, t in chunks if k == "content"]
    assert "已经显示给用户的一段" in texts
    assert any(_PARTIAL_FAILURE_NOTICE in str(t) for t in texts)
    # no duplicate of the already-shown content
    assert texts.count("已经显示给用户的一段") == 1


@pytest.mark.asyncio
async def test_streaming_failure_before_any_content_falls_back_to_legacy(monkeypatch, tmp_path):
    """If nothing streamed yet and the subprocess raises, fall back to the legacy
    stream so the user still gets an answer (no duplication risk)."""
    from hermes_multitenancy import agent_real

    async def fake_stream(event, profile_home):
        raise RuntimeError("AIAgent subprocess failed: AIAgent turn failed: invalid tool call")
        yield  # pragma: no cover (make it an async generator)

    async def fake_legacy(event, profile_home, *, messages=None):
        yield ("content", "legacy 兜底回答")

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", fake_stream, raising=False)
    monkeypatch.setattr(agent_real, "_stream_loop", fake_legacy)

    chunks = [c async for c in agent_real.stream_run_agent(_event(), tmp_path)]
    assert ("content", "legacy 兜底回答") in chunks


def test_is_output_truncation_error_matching():
    assert _is_output_truncation_error("Response truncated due to output length limit")
    assert _is_output_truncation_error("First response truncated due to output length limit")
    assert not _is_output_truncation_error("litellm.BadRequestError: 400")
    assert not _is_output_truncation_error("")
    assert not _is_output_truncation_error(None)
    # 'truncated' alone (e.g. a different truncation) must not match without 'output length'
    assert not _is_output_truncation_error("context truncated")
