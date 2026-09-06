"""Regression: cron deliveries must render as interactive Feishu cards.

Historically the cron worker delivered scheduled-task output via
``adapter.send`` as ``msg_type="text"``, which flattens markdown (bullets,
bold, links) — unlike normal replies that stream as interactive CardKit cards.
``_build_cron_card`` + ``_send_cron_card_via_live_adapter`` render a simple
interactive card instead, gated by ``cron.card_response`` (default on). An
unconfirmed card send fails closed so a fallback cannot double-send it.

These tests fail without the card path (delivery stays plain text).
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from hermes_multitenancy import cron_worker
from hermes_multitenancy.cron_worker import (
    _build_cron_card,
    _cron_card_response_enabled,
    _deliver_cron_feishu_via_live_adapter,
    _send_cron_card_via_live_adapter,
)

_MARKDOWN_BODY = "Here is your update:\n- **bold** item\n- [link](https://example.com)"


# --------------------------------------------------------------------------- #
# _build_cron_card                                                            #
# --------------------------------------------------------------------------- #
def test_build_cron_card_renders_markdown_card():
    """cron-card-style-restore: the deterministic delivery path must look like the
    old streaming card sunke had last week — bold ⏰ title IN the body (no blue
    header bar), cardified markdown, Chinese italic stop-hint (no English
    "To stop or manage" footer, no hr)."""
    job = {"id": "2515da283456", "name": "双周会前一天提醒"}
    card, media = _build_cron_card(job, _MARKDOWN_BODY)

    assert card is not None
    assert "header" not in card, "no blue header bar — title lives in the body"
    body_elements = [e for e in card["elements"] if e.get("tag") == "markdown"]
    assert len(body_elements) == 1
    body = body_elements[0]["content"]
    assert body.startswith("**⏰ 双周会前一天提醒**")
    assert "**bold**" in body
    assert "[link](https://example.com)" in body
    assert body.rstrip().endswith("_停止该任务：回复 “stop reminder 双周会前一天提醒”_")
    assert "To stop or manage" not in body
    assert not any(e.get("tag") == "hr" for e in card["elements"])
    assert media == []


def test_build_cron_card_cardifies_headings_and_tables():
    job = {"id": "2515da283456", "name": "早报"}
    content = "# 今日速览\n\n| 项目 | 值 |\n| --- | --- |\n| 限行 | 1和6 |"
    card, _ = _build_cron_card(job, content)

    body = card["elements"][0]["content"]
    assert "**今日速览**" in body, "headings become bold (cards don't render #)"
    assert "- **限行**：1和6" in body, "pipe tables become bullet key/values"
    assert "#" not in body.split("停止该任务")[0].replace("**", "")


def test_build_cron_card_wrap_response_off_sends_bare_body(monkeypatch):
    import hermes_multitenancy.cron.feishu_card as feishu_card_mod

    fake_cfg = SimpleNamespace(load_config=lambda: {"cron": {"wrap_response": False}})
    monkeypatch.setitem(sys.modules, "cron.config", fake_cfg)

    card, _ = _build_cron_card({"id": "x", "name": "早报"}, _MARKDOWN_BODY)
    body = card["elements"][0]["content"]
    assert "⏰" not in body and "停止该任务" not in body
    assert "**bold**" in body


def test_build_cron_card_empty_body_returns_none():
    card, media = _build_cron_card({"id": "x", "name": "n"}, "   \n  ")
    assert card is None  # caller falls back to plain text
    assert media == []


def test_build_cron_card_media_only_summary_uses_job_name(monkeypatch):
    fake_base = SimpleNamespace(
        BasePlatformAdapter=SimpleNamespace(
            extract_media=lambda _content: (["/tmp/a.png"], ""),
            filter_media_delivery_paths=lambda paths: paths,
        )
    )
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", fake_base)

    card, media = _build_cron_card({"id": "x", "name": "Daily media"}, "MEDIA:/tmp/a.png")

    assert card["config"]["summary"]["content"] == "Daily media"
    assert media == ["/tmp/a.png"]


def test_card_response_enabled_defaults_true():
    # cron.config is unavailable in the test env → must default ON.
    assert _cron_card_response_enabled() is True


# --------------------------------------------------------------------------- #
# _send_cron_card_via_live_adapter                                            #
# --------------------------------------------------------------------------- #
class _FakeFuture:
    def __init__(self, result):
        self._result = result

    def result(self, timeout=None):
        return self._result

    def cancel(self):  # pragma: no cover - not exercised on success
        pass


def test_send_cron_card_uses_interactive_msg_type(monkeypatch):
    captured = {}

    def fake_adapter_send(**kwargs):
        captured.update(kwargs)
        return "coro-sentinel"

    adapter = SimpleNamespace(_feishu_send_with_retry=fake_adapter_send)

    def fake_schedule(coro, loop):
        assert coro == "coro-sentinel"
        return _FakeFuture(SimpleNamespace(success=True, message_id="om_1")), None

    monkeypatch.setattr(cron_worker, "_schedule_on_gateway_loop", fake_schedule)

    card = {"elements": [{"tag": "markdown", "content": "hi"}]}
    err = _send_cron_card_via_live_adapter(adapter, "ou_abc", card, None, object())

    assert err is None
    assert captured["msg_type"] == "interactive"
    assert json.loads(captured["payload"]) == card


def test_send_cron_card_requires_nonblank_message_id_when_requested(monkeypatch):
    adapter = SimpleNamespace(_send_raw_message=lambda **k: "coro")
    monkeypatch.setattr(
        cron_worker,
        "_schedule_on_gateway_loop",
        lambda coro, loop: (_FakeFuture(SimpleNamespace(success=True, message_id="  ")), None),
    )

    err = _send_cron_card_via_live_adapter(
        adapter,
        "ou_abc",
        {"elements": []},
        None,
        object(),
        require_receipt=True,
    )

    assert err == "feishu card send missing message_id"


def test_send_cron_card_receipt_path_uses_one_raw_attempt(monkeypatch):
    calls = []
    receipt = {}
    adapter = SimpleNamespace(
        _send_raw_message=lambda **kwargs: calls.append(("raw", kwargs)) or "raw-coro",
        _feishu_send_with_retry=lambda **kwargs: calls.append(("retry", kwargs)) or "retry-coro",
    )
    monkeypatch.setattr(
        cron_worker,
        "_schedule_on_gateway_loop",
        lambda coro, loop: (_FakeFuture(SimpleNamespace(success=True, message_id="om_once")), None),
    )

    err = _send_cron_card_via_live_adapter(
        adapter,
        "oc_dm",
        {"elements": []},
        None,
        object(),
        require_receipt=True,
        receipt_out=receipt,
    )

    assert err is None
    assert [kind for kind, _kwargs in calls] == ["raw"]
    assert receipt == {"message_id": "om_once"}


def test_send_cron_card_detects_non_success_response(monkeypatch):
    # A raw Feishu response with code != 0 and no message_id must be detected
    # as a failure (via _finalize), NOT silently treated as success — else the
    # caller skips the plain-text fallback and the delivery is dropped.
    adapter = SimpleNamespace(_feishu_send_with_retry=lambda **k: "coro")
    monkeypatch.setattr(
        cron_worker,
        "_schedule_on_gateway_loop",
        lambda coro, loop: (_FakeFuture({"code": 230001, "msg": "invalid"}), None),
    )
    err = _send_cron_card_via_live_adapter(adapter, "ou_abc", {"elements": []}, None, object())
    assert err is not None  # failure surfaced so caller can fall back to text


class _RaiseFuture:
    def result(self, timeout=None):
        raise RuntimeError("coroutine blew up")

    def cancel(self):
        pass


def test_send_cron_card_contains_synchronous_adapter_raise(monkeypatch):
    # If _feishu_send_with_retry raises SYNCHRONOUSLY while building the coro,
    # the helper must return an error string, never propagate — a raise here
    # would skip the text fallback in the caller and drop the delivery.
    def boom(**kwargs):
        raise RuntimeError("sync adapter explosion")

    adapter = SimpleNamespace(_feishu_send_with_retry=boom)
    err = _send_cron_card_via_live_adapter(adapter, "ou_abc", {"elements": []}, None, object())
    assert err is not None and "sync adapter explosion" in err


def test_send_cron_card_contains_coroutine_raise(monkeypatch):
    # A non-timeout exception from future.result() must be contained too.
    adapter = SimpleNamespace(_feishu_send_with_retry=lambda **k: "coro")
    monkeypatch.setattr(
        cron_worker,
        "_schedule_on_gateway_loop",
        lambda coro, loop: (_RaiseFuture(), None),
    )
    err = _send_cron_card_via_live_adapter(adapter, "ou_abc", {"elements": []}, None, object())
    assert err is not None and "coroutine blew up" in err


# --------------------------------------------------------------------------- #
# delivery: prefers one provable card send                                   #
# --------------------------------------------------------------------------- #
def _fake_scheduler(target):
    return SimpleNamespace(_resolve_delivery_targets=lambda job: [target])


def _running_loop():
    return SimpleNamespace(is_running=lambda: True)


def test_delivery_prefers_card_over_text(monkeypatch):
    target = {"platform": "feishu", "chat_id": "ou_abc"}
    adapter = SimpleNamespace(_send_raw_message=lambda **k: None, send=lambda *a, **k: None)

    monkeypatch.setattr(cron_worker, "_adapter_for_platform", lambda adapters, name: adapter)
    monkeypatch.setattr(cron_worker, "_cron_card_response_enabled", lambda: True)

    calls = {"card": 0, "text": 0}
    monkeypatch.setattr(
        cron_worker,
        "_send_cron_card_via_live_adapter",
        lambda *a, **k: calls.__setitem__("card", calls["card"] + 1) or None,
    )

    def fail_if_text(coro, loop):
        calls["text"] += 1
        return None, "text path should not run when card succeeds"

    monkeypatch.setattr(cron_worker, "_schedule_on_gateway_loop", fail_if_text)

    err = _deliver_cron_feishu_via_live_adapter(
        _fake_scheduler(target),
        {"id": "j1", "name": "task"},
        _MARKDOWN_BODY,
        adapters={"feishu": adapter},
        loop=_running_loop(),
        require_receipt=True,
    )
    assert err is None
    assert calls["card"] == 1
    assert calls["text"] == 0  # card success → text path skipped


def test_delivery_does_not_double_send_when_card_result_is_unconfirmed(monkeypatch):
    target = {"platform": "feishu", "chat_id": "ou_abc"}
    adapter = SimpleNamespace(_send_raw_message=lambda **k: None, send=lambda *a, **k: "text-coro")

    monkeypatch.setattr(cron_worker, "_adapter_for_platform", lambda adapters, name: adapter)
    monkeypatch.setattr(cron_worker, "_cron_card_response_enabled", lambda: True)
    monkeypatch.setattr(
        cron_worker, "_send_cron_card_via_live_adapter", lambda *a, **k: "card boom"
    )

    text_calls = {"n": 0}

    def fake_schedule(coro, loop):
        text_calls["n"] += 1
        return _FakeFuture(SimpleNamespace(success=True, message_id="om_unexpected")), None

    monkeypatch.setattr(cron_worker, "_schedule_on_gateway_loop", fake_schedule)

    err = _deliver_cron_feishu_via_live_adapter(
        _fake_scheduler(target),
        {"id": "j1", "name": "task"},
        _MARKDOWN_BODY,
        adapters={"feishu": adapter},
        loop=_running_loop(),
        require_receipt=True,
    )
    assert err == "card boom"
    assert text_calls["n"] == 0


def test_legacy_delivery_still_falls_back_to_text_when_card_fails(monkeypatch):
    target = {"platform": "feishu", "chat_id": "ou_abc"}
    adapter = SimpleNamespace(_feishu_send_with_retry=lambda **k: None, send=lambda *a, **k: "text-coro")
    monkeypatch.setattr(cron_worker, "_adapter_for_platform", lambda adapters, name: adapter)
    monkeypatch.setattr(cron_worker, "_cron_card_response_enabled", lambda: True)
    monkeypatch.setattr(
        cron_worker, "_send_cron_card_via_live_adapter", lambda *a, **k: "card boom"
    )
    text_calls = []
    monkeypatch.setattr(
        cron_worker,
        "_schedule_on_gateway_loop",
        lambda coro, loop: text_calls.append(coro)
        or (_FakeFuture(SimpleNamespace(success=True)), None),
    )

    err = _deliver_cron_feishu_via_live_adapter(
        _fake_scheduler(target),
        {"id": "j1", "name": "task"},
        _MARKDOWN_BODY,
        adapters={"feishu": adapter},
        loop=_running_loop(),
    )

    assert err is None
    assert text_calls == ["text-coro"]


def test_delivery_requires_static_card_when_receipt_required(monkeypatch):
    target = {"platform": "feishu", "chat_id": "ou_abc"}
    adapter = SimpleNamespace(send=lambda *a, **k: "text-coro")
    monkeypatch.setattr(cron_worker, "_adapter_for_platform", lambda adapters, name: adapter)
    monkeypatch.setattr(cron_worker, "_cron_card_response_enabled", lambda: False)
    monkeypatch.setattr(
        cron_worker,
        "_schedule_on_gateway_loop",
        lambda coro, loop: (_FakeFuture(SimpleNamespace(success=True, message_id="   ")), None),
    )

    err = _deliver_cron_feishu_via_live_adapter(
        _fake_scheduler(target),
        {"id": "j1", "name": "task"},
        _MARKDOWN_BODY,
        adapters={"feishu": adapter},
        loop=_running_loop(),
        require_receipt=True,
    )

    assert err == "feishu confirmed card unavailable"


def test_receipt_delivery_builds_card_for_media_only_payload(monkeypatch):
    target = {"platform": "feishu", "chat_id": "ou_abc"}
    adapter = SimpleNamespace(_send_raw_message=lambda **k: None)
    calls = []
    monkeypatch.setattr(cron_worker, "_adapter_for_platform", lambda adapters, name: adapter)
    monkeypatch.setattr(cron_worker, "_cron_card_response_enabled", lambda: True)
    monkeypatch.setattr(
        cron_worker, "_cron_delivery_payload_for_adapter", lambda job, content: ("", [("/tmp/a.png", False)])
    )
    monkeypatch.setattr(
        cron_worker,
        "_build_cron_card",
        lambda job, content: ({"elements": []}, [("/tmp/a.png", False)]),
    )
    monkeypatch.setattr(
        cron_worker,
        "_send_cron_card_via_live_adapter",
        lambda *a, **k: calls.append("card") or k["receipt_out"].update(message_id="om_card"),
    )
    monkeypatch.setattr(
        cron_worker,
        "_send_media_files_via_live_adapter",
        lambda *a, **k: calls.append("media") or None,
    )
    receipt = {}

    err = _deliver_cron_feishu_via_live_adapter(
        _fake_scheduler(target),
        {"id": "j1", "name": "task"},
        "MEDIA:/tmp/a.png",
        adapters={"feishu": adapter},
        loop=_running_loop(),
        require_receipt=True,
        receipt_out=receipt,
    )

    assert err is None
    assert calls == ["card", "media"]
    assert receipt == {"message_id": "om_card"}


def test_media_provider_receipt_failure_is_consumed(monkeypatch):
    import types

    scheduler = types.ModuleType("cron.scheduler")
    scheduler._send_media_via_adapter = lambda *a, **k: SimpleNamespace(
        success=False,
        error_code="missing_receipt",
    )
    cron_pkg = types.ModuleType("cron")
    cron_pkg.scheduler = scheduler
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.setitem(sys.modules, "gateway.config", SimpleNamespace(Platform=lambda value: value))

    error = cron_worker._send_media_files_via_live_adapter(
        object(),
        "sink",
        [("media", False)],
        None,
        object(),
        {"id": "job"},
    )

    assert error == "cron media delivery unconfirmed (missing_receipt)"


def test_media_provider_missing_receipt_fails_closed(monkeypatch):
    import types

    scheduler = types.ModuleType("cron.scheduler")
    scheduler._send_media_via_adapter = lambda *a, **k: None
    cron_pkg = types.ModuleType("cron")
    cron_pkg.scheduler = scheduler
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.setitem(sys.modules, "gateway.config", SimpleNamespace(Platform=lambda value: value))

    error = cron_worker._send_media_files_via_live_adapter(
        object(),
        "sink",
        [("media", False)],
        None,
        object(),
        {"id": "job"},
    )

    assert error == "cron media delivery unconfirmed (unconfirmed)"
