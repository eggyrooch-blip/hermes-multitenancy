from __future__ import annotations

import pytest

from hermes_multitenancy.agent_real import (
    _finalize_aiagent_result,
    _is_output_truncation_error,
    _TRUNCATION_NOTICE,
)


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
