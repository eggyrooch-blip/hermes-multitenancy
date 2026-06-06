"""Regression: Feishu cron output renders as a STREAMING card, not plain text.

Core's cron ``_deliver_result`` sends ``adapter.send(text)`` which Feishu shows
as flattened plain text (``##`` headings and ``| |`` tables appear literally).
Like openclaw-lark, we deliver Feishu cron output through the SAME streaming
CardKit surface a normal reply uses (start -> stream -> finalize), converting
headings->bold and tables->bullet key/values so they render. Any failure falls
through to core plain-text delivery (a delivery is never dropped, never doubled).
"""
from __future__ import annotations

import sys
import types

from hermes_multitenancy import cron_worker
from hermes_multitenancy.cron_worker import (
    _build_cron_card_body,
    _cardify_markdown_for_feishu,
    _convert_md_tables_for_card,
    _stream_cron_card_on_loop,
    _try_deliver_cron_feishu_streaming_card,
)

_WEATHER = """## 🌤️ 天气预报
| 项目 | 详情 |
|------|------|
| ☀️ 天气 | **晴** |
| 🌡️ 气温 | 最高 24°C |

## 🗺️ 出行建议
- ✅ 好天气
- 👕 薄外套"""


def _inject_base(monkeypatch, *, raises=False, media=None):
    """Provide a fake gateway.platforms.base so _try can reach the media gate."""
    mod = types.ModuleType("gateway.platforms.base")

    class BasePlatformAdapter:
        @staticmethod
        def extract_media(content):
            if raises:
                raise RuntimeError("extract boom")
            return (media or []), content

        @staticmethod
        def filter_media_delivery_paths(m):
            return m

    mod.BasePlatformAdapter = BasePlatformAdapter
    for name in ("gateway", "gateway.platforms"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", mod)


# --------------------------------------------------------------------------- #
# markdown -> Feishu-card-renderable subset                                   #
# --------------------------------------------------------------------------- #
def test_heading_becomes_bold():
    out = _cardify_markdown_for_feishu("## 🚗 限行尾号\n正文")
    assert "**🚗 限行尾号**" in out and "## " not in out


def test_two_col_table_becomes_key_value_bullets():
    out = _convert_md_tables_for_card("| 项目 | 详情 |\n|---|---|\n| 天气 | 晴 |\n| 气温 | 24°C |")
    assert "- **天气**：晴" in out and "- **气温**：24°C" in out
    assert "|---|" not in out and "| 项目 |" not in out


def test_multi_col_table_renders_each_row():
    out = _convert_md_tables_for_card("| 排名 | 笔记 | 信号 |\n|---|---|---|\n| #1 | 高考 | 强 |")
    assert "**#1**" in out and "笔记: 高考" in out and "信号: 强" in out


def test_card_body_has_no_raw_markdown_and_has_title():
    body = _build_cron_card_body({"name": "北京天气"}, _WEATHER)
    assert "**⏰ 北京天气**" in body          # clean title, not "Cronjob Response:"
    assert "## " not in body and "|------|" not in body
    assert "- **☀️ 天气**：**晴**" in body
    assert "- ✅ 好天气" in body
    assert "stop reminder 北京天气" in body


# --------------------------------------------------------------------------- #
# _stream_cron_card_on_loop : never drop, never double-send                   #
# --------------------------------------------------------------------------- #
class _Future:
    def __init__(self, result):
        self._r = result

    def result(self, timeout=None):
        if isinstance(self._r, Exception):
            raise self._r
        return self._r

    def cancel(self):
        pass


def _adapter(calls):
    def start(**k):
        calls.append(("start", k))
        return "start-coro"

    def update(**k):
        calls.append(("update", k))
        return "update-coro"

    return types.SimpleNamespace(
        supports_streaming_card=lambda: True,
        start_streaming_card=start,
        update_streaming_card=update,
    )


def _ok(success=True, message_id="om_1"):
    return types.SimpleNamespace(success=success, message_id=message_id)


def test_stream_drives_start_stream_finalize(monkeypatch):
    calls = []
    adapter = _adapter(calls)
    monkeypatch.setattr(
        cron_worker,
        "_schedule_on_gateway_loop",
        lambda coro, loop: (_Future(_ok() if coro == "start-coro" else _ok(message_id=None)), None),
    )
    assert _stream_cron_card_on_loop(adapter, "ou_x", "body", None, object()) is None
    kinds = [c[0] for c in calls]
    assert kinds == ["start", "update", "update"]
    assert calls[1][1]["finalize"] is False and calls[2][1]["finalize"] is True


def test_stream_non_final_failure_returns_error(monkeypatch):
    # Body never shown -> must return an error so caller falls back to text.
    n = {"i": 0}

    def sched(coro, loop):
        n["i"] += 1
        if n["i"] == 1:
            return _Future(_ok()), None          # start ok
        if n["i"] == 2:
            return None, "sched boom"            # non-final update fails to schedule
        return _Future(_ok()), None

    monkeypatch.setattr(cron_worker, "_schedule_on_gateway_loop", sched)
    err = _stream_cron_card_on_loop(_adapter([]), "ou_x", "body", None, object())
    assert err is not None  # -> caller falls back to plain text


def test_stream_finalize_failure_is_tolerated(monkeypatch):
    # Body already streamed -> finalize failure must NOT trigger fallback (no dup).
    n = {"i": 0}

    def sched(coro, loop):
        n["i"] += 1
        if n["i"] == 1:
            return _Future(_ok()), None          # start
        if n["i"] == 2:
            return _Future(_ok(message_id=None)), None  # non-final stream ok
        return _Future(RuntimeError("finalize blew up")), None  # finalize raises

    monkeypatch.setattr(cron_worker, "_schedule_on_gateway_loop", sched)
    assert _stream_cron_card_on_loop(_adapter([]), "ou_x", "body", None, object()) is None


def test_stream_never_raises_on_adapter_explosion():
    def boom(**k):
        raise RuntimeError("kaboom")

    adapter = types.SimpleNamespace(start_streaming_card=boom, supports_streaming_card=lambda: True)
    err = _stream_cron_card_on_loop(adapter, "ou_x", "body", None, object())
    assert err is not None and "kaboom" in err


# --------------------------------------------------------------------------- #
# _try_deliver_cron_feishu_streaming_card : applicability gates               #
# --------------------------------------------------------------------------- #
def _running_loop():
    return types.SimpleNamespace(is_running=lambda: True)


def _scheduler(targets):
    return types.SimpleNamespace(_resolve_delivery_targets=lambda job: targets)


def test_try_streams_single_feishu_target(monkeypatch):
    _inject_base(monkeypatch)
    calls = []
    adapter = _adapter(calls)
    monkeypatch.setattr(cron_worker, "_adapter_for_platform", lambda a, n: adapter)
    monkeypatch.setattr(
        cron_worker,
        "_schedule_on_gateway_loop",
        lambda coro, loop: (_Future(_ok() if coro == "start-coro" else _ok(message_id=None)), None),
    )
    out = _try_deliver_cron_feishu_streaming_card(
        _scheduler([{"platform": "feishu", "chat_id": "ou_x"}]),
        {"name": "t"},
        "## h\n- a\n- b",
        adapters={"feishu": adapter},
        loop=_running_loop(),
    )
    assert out is True
    assert any(c[0] == "start" for c in calls)


def test_try_skips_non_feishu_target():
    out = _try_deliver_cron_feishu_streaming_card(
        _scheduler([{"platform": "telegram", "chat_id": "1"}]),
        {"name": "t"}, "## h", adapters={"telegram": object()}, loop=_running_loop(),
    )
    assert out is None


def test_try_skips_multi_target():
    # Two feishu targets -> must bail (avoid double-send on partial failure).
    out = _try_deliver_cron_feishu_streaming_card(
        _scheduler([{"platform": "feishu", "chat_id": "a"}, {"platform": "feishu", "chat_id": "b"}]),
        {"name": "t"}, "## h", adapters={"feishu": object()}, loop=_running_loop(),
    )
    assert out is None


def test_try_skips_when_no_streaming_adapter(monkeypatch):
    monkeypatch.setattr(cron_worker, "_adapter_for_platform", lambda a, n: types.SimpleNamespace())
    out = _try_deliver_cron_feishu_streaming_card(
        _scheduler([{"platform": "feishu", "chat_id": "ou_x"}]),
        {"name": "t"}, "## h", adapters={"feishu": object()}, loop=_running_loop(),
    )
    assert out is None


def test_try_fails_closed_on_media_extract_error(monkeypatch):
    _inject_base(monkeypatch, raises=True)
    adapter = _adapter([])
    monkeypatch.setattr(cron_worker, "_adapter_for_platform", lambda a, n: adapter)
    out = _try_deliver_cron_feishu_streaming_card(
        _scheduler([{"platform": "feishu", "chat_id": "ou_x"}]),
        {"name": "t"}, "## h", adapters={"feishu": adapter}, loop=_running_loop(),
    )
    assert out is None  # fail closed -> core handles (may have media)


def test_try_skips_when_media_present(monkeypatch):
    _inject_base(monkeypatch, media=[("/tmp/a.png", False)])
    adapter = _adapter([])
    monkeypatch.setattr(cron_worker, "_adapter_for_platform", lambda a, n: adapter)
    out = _try_deliver_cron_feishu_streaming_card(
        _scheduler([{"platform": "feishu", "chat_id": "ou_x"}]),
        {"name": "t"}, "## h\n![x](/tmp/a.png)", adapters={"feishu": adapter}, loop=_running_loop(),
    )
    assert out is None
