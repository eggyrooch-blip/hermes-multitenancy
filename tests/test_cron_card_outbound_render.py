"""Regression: Feishu cron output renders as a STREAMING card, not plain text.

Core's cron ``_deliver_result`` sends ``adapter.send(text)`` which Feishu shows
as flattened plain text (``##`` headings and ``| |`` tables appear literally).
Like openclaw-lark, we deliver Feishu cron output through the SAME streaming
CardKit surface a normal reply uses (start -> stream -> finalize), converting
headings->bold and tables->bullet key/values so they render. Any failure falls
through to core plain-text delivery (a delivery is never dropped).

These tests fail without the converter + streaming-card delivery path.
"""
from __future__ import annotations

from types import SimpleNamespace

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


# --------------------------------------------------------------------------- #
# markdown -> Feishu-card-renderable subset                                   #
# --------------------------------------------------------------------------- #
def test_heading_becomes_bold():
    out = _cardify_markdown_for_feishu("## 🚗 限行尾号\n正文")
    assert "**🚗 限行尾号**" in out
    assert "## " not in out


def test_two_col_table_becomes_key_value_bullets():
    out = _convert_md_tables_for_card("| 项目 | 详情 |\n|---|---|\n| 天气 | 晴 |\n| 气温 | 24°C |")
    assert "- **天气**：晴" in out
    assert "- **气温**：24°C" in out
    assert "|---|" not in out and "| 项目 |" not in out


def test_multi_col_table_renders_each_row():
    out = _convert_md_tables_for_card("| 排名 | 笔记 | 信号 |\n|---|---|---|\n| #1 | 高考 | 强 |")
    assert "**#1**" in out and "笔记: 高考" in out and "信号: 强" in out


def test_card_body_has_no_raw_markdown_and_has_title():
    body = _build_cron_card_body({"name": "北京天气"}, _WEATHER)
    assert "**⏰ 北京天气**" in body          # clean title, not "Cronjob Response:"
    assert "## " not in body                  # headings converted
    assert "|------|" not in body             # table separator gone
    assert "- **☀️ 天气**：**晴**" in body     # table row -> bullet
    assert "- ✅ 好天气" in body               # existing list preserved
    assert "stop reminder 北京天气" in body    # stop hint


# --------------------------------------------------------------------------- #
# streaming-card delivery driver                                              #
# --------------------------------------------------------------------------- #
class _Future:
    def __init__(self, result):
        self._r = result

    def result(self, timeout=None):
        return self._r

    def cancel(self):
        pass


def _streaming_adapter(calls):
    def start(**kwargs):
        calls.append(("start", kwargs))
        return "start-coro"

    def update(**kwargs):
        calls.append(("update", kwargs))
        return "update-coro"

    return SimpleNamespace(
        supports_streaming_card=lambda: True,
        start_streaming_card=start,
        update_streaming_card=update,
    )


def test_stream_cron_card_drives_start_then_finalize(monkeypatch):
    calls = []
    adapter = _streaming_adapter(calls)

    def fake_sched(coro, loop):
        if coro == "start-coro":
            return _Future(SimpleNamespace(success=True, message_id="om_1")), None
        return _Future(SimpleNamespace(success=True)), None

    monkeypatch.setattr(cron_worker, "_schedule_on_gateway_loop", fake_sched)
    err = _stream_cron_card_on_loop(adapter, "ou_x", "body", None, object())
    assert err is None
    kinds = [c[0] for c in calls]
    assert kinds == ["start", "update", "update"]  # start + stream + finalize
    assert calls[1][1]["finalize"] is False and calls[2][1]["finalize"] is True


def test_stream_cron_card_start_failure_returns_error(monkeypatch):
    adapter = _streaming_adapter([])
    monkeypatch.setattr(
        cron_worker,
        "_schedule_on_gateway_loop",
        lambda coro, loop: (_Future(SimpleNamespace(success=False, error="boom")), None),
    )
    err = _stream_cron_card_on_loop(adapter, "ou_x", "body", None, object())
    assert err is not None and "boom" in err


def test_stream_cron_card_never_raises_on_adapter_explosion():
    def boom(**k):
        raise RuntimeError("kaboom")

    adapter = SimpleNamespace(start_streaming_card=boom, supports_streaming_card=lambda: True)
    err = _stream_cron_card_on_loop(adapter, "ou_x", "body", None, object())
    assert err is not None and "kaboom" in err  # contained, not raised


def _running_loop():
    return SimpleNamespace(is_running=lambda: True)


def test_try_deliver_streams_for_all_feishu_target(monkeypatch):
    calls = []
    adapter = _streaming_adapter(calls)
    scheduler = SimpleNamespace(
        _resolve_delivery_targets=lambda job: [{"platform": "feishu", "chat_id": "ou_x"}]
    )
    monkeypatch.setattr(cron_worker, "_adapter_for_platform", lambda a, n: adapter)
    monkeypatch.setattr(
        cron_worker,
        "_schedule_on_gateway_loop",
        lambda coro, loop: (
            _Future(SimpleNamespace(success=True, message_id="om_1"))
            if coro == "start-coro"
            else _Future(SimpleNamespace(success=True)),
            None,
        ),
    )
    out = _try_deliver_cron_feishu_streaming_card(
        scheduler, {"name": "t"}, "## h\n- a\n- b", adapters={"feishu": adapter}, loop=_running_loop()
    )
    assert out is True
    assert any(c[0] == "start" for c in calls)


def test_try_deliver_skips_non_feishu_target(monkeypatch):
    scheduler = SimpleNamespace(
        _resolve_delivery_targets=lambda job: [{"platform": "telegram", "chat_id": "123"}]
    )
    out = _try_deliver_cron_feishu_streaming_card(
        scheduler, {"name": "t"}, "## h", adapters={"telegram": object()}, loop=_running_loop()
    )
    assert out is None  # falls through to core delivery


def test_try_deliver_skips_when_no_streaming_adapter(monkeypatch):
    scheduler = SimpleNamespace(
        _resolve_delivery_targets=lambda job: [{"platform": "feishu", "chat_id": "ou_x"}]
    )
    # adapter without start_streaming_card
    monkeypatch.setattr(cron_worker, "_adapter_for_platform", lambda a, n: SimpleNamespace())
    out = _try_deliver_cron_feishu_streaming_card(
        scheduler, {"name": "t"}, "## h", adapters={"feishu": object()}, loop=_running_loop()
    )
    assert out is None
