"""Dead-air fix: a failed AIAgent turn must surface a fallback, never silence.

Regression for the bug where _run_with_aiagent flattened core's
{failed:True, final_response:None} result to "" — discarding the failure
signal so real_run_agent's legacy fallback and the router's
"⚠️ 模型暂时不可用" message could never fire, leaving the Feishu user with
complete silence (observed: up to 264s of no reply).
"""
import asyncio

import pytest

import hermes_multitenancy.agent_real as ar


# -- the unit my change actually lives in ----------------------------------

def test_finalize_raises_on_failed_turn():
    with pytest.raises(RuntimeError, match="HTTP 400"):
        ar._finalize_aiagent_result(
            {"failed": True, "final_response": None, "error": "HTTP 400"}
        )


def test_finalize_raises_on_missing_response():
    with pytest.raises(RuntimeError):
        ar._finalize_aiagent_result({"final_response": None})


def test_finalize_raises_on_none_result():
    with pytest.raises(RuntimeError):
        ar._finalize_aiagent_result(None)


def test_finalize_returns_text_on_success():
    assert ar._finalize_aiagent_result({"final_response": "hello"}) == "hello"


def test_finalize_empty_success_is_unchanged():
    # A genuine empty-but-successful turn must NOT start raising.
    assert ar._finalize_aiagent_result({"final_response": "", "failed": False}) == ""


# -- the user-observable propagation chain the fix re-enables ---------------

class _Evt:
    text = "hi"


def test_failed_turn_falls_back_not_silent(monkeypatch, tmp_path):
    """Failed subprocess turn → real_run_agent falls back to legacy (non-empty),
    instead of returning '' (dead air)."""
    async def boom(event, profile_home, messages=None):
        raise RuntimeError("AIAgent subprocess failed: AIAgent turn failed: HTTP 400")

    async def legacy_ok(event, profile_home, messages=None):
        return "legacy fallback answer"

    monkeypatch.setattr(ar, "_run_aiagent_subprocess", boom)
    monkeypatch.setattr(ar, "_legacy_real_run_agent", legacy_ok)
    out = asyncio.run(ar.real_run_agent(_Evt(), tmp_path))
    assert out == "legacy fallback answer"
    assert out  # never empty → no dead air


def test_total_failure_raises_for_router_fallback(monkeypatch, tmp_path):
    """When the legacy path also fails, real_run_agent raises so the router's
    '⚠️ 模型暂时不可用' fallback fires (instead of silently returning '')."""
    async def boom(event, profile_home, messages=None):
        raise RuntimeError("AIAgent turn failed: HTTP 400")

    async def legacy_boom(event, profile_home, messages=None):
        raise RuntimeError("all providers failed")

    monkeypatch.setattr(ar, "_run_aiagent_subprocess", boom)
    monkeypatch.setattr(ar, "_legacy_real_run_agent", legacy_boom)
    with pytest.raises(RuntimeError):
        asyncio.run(ar.real_run_agent(_Evt(), tmp_path))
