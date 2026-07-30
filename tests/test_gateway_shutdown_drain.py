"""Regression: gateway SIGTERM teardown must not be held open by doomed sends.

prod 2026-07-30 18:00:11→18:04:11: gateway teardown itself finished in 0.91s but
the process hung for 4 minutes until systemd escalated to SIGKILL. Two
mechanisms held it:

1. ``FeishuAdapter._feishu_send_with_retry`` treats every exception as
   transient. Once the interpreter / SDK executor is shut down every send
   raises ``RuntimeError: cannot schedule new futures after …shutdown`` — fatal,
   never recoverable — so the loop burned 1s + 2s of backoff per chat and,
   serialized over the chats with live sessions, stretched into minutes.
2. The cron scheduler kept firing deliveries during teardown
   (``delivery error: Feishu send failed: cannot schedule new futures after
   interpreter shutdown``), each one paying the same retry tax.

These tests pin: zero retries for both wordings of the fatal error, unchanged
3-attempt backoff for ordinary transient errors, the two pre-existing escape
branches of the core loop (post-content-invalid raise, reply-withdrawn
fallback), and cron delivery skipping while the gateway drains.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import types
from typing import Any, Optional

import pytest

from hermes_multitenancy import cron_worker as _cw
from hermes_multitenancy import feishu_adapter_compat
from hermes_multitenancy.cron import patches

_FATAL_WORDINGS = (
    "cannot schedule new futures after shutdown",
    "cannot schedule new futures after interpreter shutdown",
)


class _FakeResponse:
    def __init__(self, code: int = 0) -> None:
        self.code = code


class _FakeFeishuAdapter:
    """Minimal stand-in for the surface the send-retry loop touches."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self.core_loop_calls = 0
        self._outcomes = list(outcomes)

    async def _feishu_send_with_retry(
        self,
        *,
        chat_id: str,
        msg_type: str,
        payload: str,
        reply_to: Optional[str],
        metadata: Optional[dict],
        **_extra: Any,
    ) -> Any:
        """Stand-in for the core loop, so delegation is observable."""
        self.core_loop_calls += 1
        return await self._send_raw_message(
            chat_id=chat_id,
            msg_type=msg_type,
            payload=payload,
            reply_to=reply_to,
            metadata=metadata,
        )

    async def _send_raw_message(
        self,
        *,
        chat_id: str,
        msg_type: str,
        payload: str,
        reply_to: Optional[str],
        metadata: Optional[dict],
    ) -> Any:
        self.calls.append({"chat_id": chat_id, "msg_type": msg_type, "reply_to": reply_to})
        outcome = self._outcomes.pop(0) if self._outcomes else _FakeResponse()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    @staticmethod
    def _response_succeeded(response: Any) -> bool:
        return getattr(response, "code", 0) == 0


def _install_fake_feishu_module(monkeypatch) -> types.ModuleType:
    module = types.ModuleType("plugins.platforms.feishu.adapter")
    module._FEISHU_SEND_ATTEMPTS = 3  # type: ignore[attr-defined]
    module._FEISHU_REPLY_FALLBACK_CODES = frozenset({230011, 231003})  # type: ignore[attr-defined]
    module._POST_CONTENT_INVALID_RE = re.compile(  # type: ignore[attr-defined]
        r"content format of the post type is incorrect", re.IGNORECASE
    )
    module.FeishuAdapter = type("FeishuAdapter", (_FakeFeishuAdapter,), {})  # type: ignore[attr-defined]

    for name in (
        "hermes_plugins.feishu_platform.adapter",
        "gateway.platforms.feishu",
        "plugins.platforms.feishu.adapter",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "plugins.platforms.feishu.adapter", module)

    def import_module(name: str) -> types.ModuleType:
        if name == "plugins.platforms.feishu.adapter":
            return module
        raise ModuleNotFoundError(f"No module named '{name}'", name=name)

    monkeypatch.setattr(feishu_adapter_compat, "import_module", import_module)
    return module


def _install_send_retry_patch(monkeypatch) -> tuple[types.ModuleType, list[int]]:
    module = _install_fake_feishu_module(monkeypatch)
    sleeps: list[int] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(patches.asyncio, "sleep", fake_sleep)
    patches._patch_feishu_send_retry_shutdown_fatal()
    assert getattr(
        module.FeishuAdapter._feishu_send_with_retry,
        "_hermes_multitenancy_send_retry_fatal_patched",
        False,
    ), "patch did not land on the runtime adapter class"
    return module, sleeps


async def _send(adapter: Any, **kwargs: Any) -> Any:
    payload = {
        "chat_id": "oc_chat",
        "msg_type": "text",
        "payload": '{"text": "hi"}',
        "reply_to": None,
        "metadata": None,
    }
    payload.update(kwargs)
    return await adapter._feishu_send_with_retry(**payload)


# --------------------------------------------------------------------------
# 1 + 2. Fatal executor-shutdown RuntimeError → zero retries, zero backoff
# --------------------------------------------------------------------------


@pytest.mark.parametrize("wording", _FATAL_WORDINGS)
def test_executor_shutdown_runtimeerror_is_not_retried(monkeypatch, caplog, wording) -> None:
    module, sleeps = _install_send_retry_patch(monkeypatch)
    fatal = RuntimeError(wording)
    adapter = module.FeishuAdapter([fatal, fatal, fatal])
    caplog.set_level(logging.INFO, logger=patches.logger.name)

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(_send(adapter))

    assert excinfo.value is fatal
    assert len(adapter.calls) == 1, "fatal shutdown error must not be retried"
    assert sleeps == [], "fatal shutdown error must not pay backoff"
    assert "retrying in" not in caplog.text


def test_is_executor_shutdown_error_classification() -> None:
    for wording in _FATAL_WORDINGS:
        assert feishu_adapter_compat.is_executor_shutdown_error(RuntimeError(wording))
    assert not feishu_adapter_compat.is_executor_shutdown_error(RuntimeError("timeout"))
    # Same text, wrong type → still transient (only the interpreter/executor
    # raises this as a RuntimeError).
    assert not feishu_adapter_compat.is_executor_shutdown_error(
        ValueError("cannot schedule new futures after shutdown")
    )


# --------------------------------------------------------------------------
# 3. Ordinary transient error → unchanged 3-attempt 1s/2s backoff
# --------------------------------------------------------------------------


def test_transient_error_still_retries_three_times(monkeypatch, caplog) -> None:
    module, sleeps = _install_send_retry_patch(monkeypatch)
    adapter = module.FeishuAdapter(
        [TimeoutError("read timeout"), TimeoutError("read timeout"), TimeoutError("read timeout")]
    )
    caplog.set_level(logging.WARNING, logger=patches.logger.name)

    with pytest.raises(TimeoutError):
        asyncio.run(_send(adapter))

    assert len(adapter.calls) == 3
    assert sleeps == [1, 2]
    assert caplog.text.count("retrying in") == 2


def test_transient_error_then_success(monkeypatch) -> None:
    module, sleeps = _install_send_retry_patch(monkeypatch)
    ok = _FakeResponse()
    adapter = module.FeishuAdapter([TimeoutError("read timeout"), ok])

    assert asyncio.run(_send(adapter)) is ok
    assert len(adapter.calls) == 2
    assert sleeps == [1]


# --------------------------------------------------------------------------
# Regression guards for the two pre-existing escape branches of the core loop
# --------------------------------------------------------------------------


def test_post_content_invalid_raises_without_retry(monkeypatch) -> None:
    module, sleeps = _install_send_retry_patch(monkeypatch)
    fatal = ValueError("content format of the post type is incorrect")
    adapter = module.FeishuAdapter([fatal, fatal, fatal])

    with pytest.raises(ValueError):
        asyncio.run(_send(adapter, msg_type="post"))

    assert len(adapter.calls) == 1
    assert sleeps == []


def test_withdrawn_reply_falls_back_to_new_message(monkeypatch) -> None:
    module, sleeps = _install_send_retry_patch(monkeypatch)
    withdrawn = _FakeResponse(code=230011)
    ok = _FakeResponse()
    adapter = module.FeishuAdapter([withdrawn, ok])

    assert asyncio.run(_send(adapter, reply_to="om_parent")) is ok
    assert [call["reply_to"] for call in adapter.calls] == ["om_parent", None]
    assert sleeps == []


def test_unexpected_call_shape_delegates_to_core_loop(monkeypatch, caplog) -> None:
    """The patch replaces a core loop; an unrecognised call shape must fall back
    to core rather than silently mirror a signature we no longer match
    (2026-07-21: a hard-coded cron run_job signature broke every tick)."""
    module, _sleeps = _install_send_retry_patch(monkeypatch)
    adapter = module.FeishuAdapter([_FakeResponse()])
    caplog.set_level(logging.WARNING, logger=patches.logger.name)

    asyncio.run(
        adapter._feishu_send_with_retry(
            chat_id="oc_chat",
            msg_type="text",
            payload="{}",
            reply_to=None,
            metadata=None,
            defer_teardown=True,  # a parameter a future core might add
        )
    )

    assert adapter.core_loop_calls == 1
    assert "unexpected _feishu_send_with_retry call shape" in caplog.text


# --------------------------------------------------------------------------
# 4. cron delivery skipped while the gateway drains
# --------------------------------------------------------------------------


class _FakeGateway:
    def __init__(self, draining: bool = False) -> None:
        self._draining = draining


def _install_fake_scheduler(monkeypatch) -> tuple[types.ModuleType, list[dict]]:
    delivered: list[dict] = []

    def original_deliver_result(job: dict, content: str, adapters: Any = None, loop: Any = None):
        delivered.append({"job": job, "content": content})
        return None

    fake_scheduler = types.ModuleType("cron.scheduler")
    fake_scheduler._deliver_result = original_deliver_result  # type: ignore[attr-defined]
    fake_cron = types.ModuleType("cron")
    fake_cron.scheduler = fake_scheduler  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cron", fake_cron)
    monkeypatch.setitem(sys.modules, "cron.scheduler", fake_scheduler)
    monkeypatch.setattr(_cw, "_cron_card_response_enabled", lambda: False, raising=False)
    monkeypatch.setattr(_cw, "_mirror_cron_delivery_to_owner", lambda job, content: None, raising=False)
    return fake_scheduler, delivered


def test_cron_delivery_skipped_during_gateway_shutdown(monkeypatch, caplog) -> None:
    fake_scheduler, delivered = _install_fake_scheduler(monkeypatch)
    monkeypatch.setattr(patches, "_gateway_ref", None, raising=False)
    gateway = _FakeGateway(draining=True)  # strong ref: the probe holds only a weakref
    patches._remember_gateway(gateway)
    patches._patch_cron_delivery_mirror()
    caplog.set_level(logging.INFO, logger=patches.logger.name)

    error = fake_scheduler._deliver_result({"id": "job-shutdown"}, "result text")

    assert delivered == [], "no send may be attempted while the gateway drains"
    assert error is not None and "shut" in error.lower()
    assert "cron delivery skipped" in caplog.text


def test_cron_delivery_unaffected_when_gateway_running(monkeypatch) -> None:
    fake_scheduler, delivered = _install_fake_scheduler(monkeypatch)
    monkeypatch.setattr(patches, "_gateway_ref", None, raising=False)
    gateway = _FakeGateway(draining=False)
    patches._remember_gateway(gateway)
    patches._patch_cron_delivery_mirror()

    assert fake_scheduler._deliver_result({"id": "job-live"}, "result text") is None
    assert [entry["job"]["id"] for entry in delivered] == ["job-live"]


def test_gateway_shutdown_probe_is_false_without_a_known_gateway(monkeypatch) -> None:
    monkeypatch.setattr(patches, "_gateway_ref", None, raising=False)
    assert patches.gateway_is_shutting_down() is False
