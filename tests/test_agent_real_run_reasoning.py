"""broker 模式 reasoning_effort 透传：metadata → agent_kwargs["reasoning_config"]。

WebUI 的档位选择器走 `metadata.reasoning_effort`（RunRequest 零 schema 变更），
一路到 `event.raw_event["metadata"]`；本文件钉住最后一跳的翻译规则。
"""
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from hermes_multitenancy.agent_real.run import _reasoning_config_for_event


# core 0.21.3 `hermes_constants.py:927-943` 的契约副本。hermes_constants 随
# hermes-agent 发布、不在本仓，CI 里不可导入 —— 与 `test_curator_sweep.py:303`
# 同样的 stub 手法。真核可导入时 `test_stub_matches_core_parser` 比对防漂移。
_VALID_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def _reference_parse_reasoning_effort(effort):
    if effort is None or effort is True:
        return None
    effort = str(effort).strip().lower()
    if effort in {"none", "false", "disabled"}:
        return {"enabled": False}
    if effort in _VALID_REASONING_EFFORTS:
        return {"enabled": True, "effort": effort}
    return None


@pytest.fixture
def stub_hermes_constants(monkeypatch):
    mod = ModuleType("hermes_constants")
    mod.VALID_REASONING_EFFORTS = _VALID_REASONING_EFFORTS
    mod.parse_reasoning_effort = _reference_parse_reasoning_effort
    monkeypatch.setitem(sys.modules, "hermes_constants", mod)
    return mod


def _event(metadata=None, *, raw_event=None):
    if raw_event is not None:
        return SimpleNamespace(raw_event=raw_event)
    raw: dict = {"channel": "webui", "session_id": "session-webui"}
    if metadata is not None:
        raw["metadata"] = metadata
    return SimpleNamespace(raw_event=raw)


def test_high_becomes_enabled_high(stub_hermes_constants):
    assert _reasoning_config_for_event(
        _event({"reasoning_effort": "high"})
    ) == {"enabled": True, "effort": "high"}


@pytest.mark.parametrize("level", _VALID_REASONING_EFFORTS)
def test_every_core_level_passes_through(stub_hermes_constants, level):
    assert _reasoning_config_for_event(
        _event({"reasoning_effort": level})
    ) == {"enabled": True, "effort": level}


@pytest.mark.parametrize("off", ["none", "false", "disabled", "None", " DISABLED "])
def test_off_words_disable_reasoning(stub_hermes_constants, off):
    """`reasoning_effort: none` 必须是「关」，不是「回落默认」。"""
    assert _reasoning_config_for_event(_event({"reasoning_effort": off})) == {"enabled": False}


def test_bogus_value_returns_none(stub_hermes_constants):
    assert _reasoning_config_for_event(_event({"reasoning_effort": "bogus"})) is None


@pytest.mark.parametrize(
    "metadata",
    [
        {},                                  # 有 metadata 但没这个键
        {"model": "glm-5.1"},                # 只有别的键
        {"reasoning_effort": ""},            # 空串 = 档位选择器的 default 档
        {"reasoning_effort": "   "},
        {"reasoning_effort": None},
    ],
)
def test_absent_or_empty_leaves_default(stub_hermes_constants, metadata):
    assert _reasoning_config_for_event(_event(metadata)) is None


def test_missing_metadata_key_leaves_default(stub_hermes_constants):
    assert _reasoning_config_for_event(_event()) is None


@pytest.mark.parametrize("raw_event", [None, "not-a-dict", {"metadata": "not-a-dict"}])
def test_malformed_event_leaves_default(stub_hermes_constants, raw_event):
    assert _reasoning_config_for_event(_event(raw_event=raw_event)) is None


def test_event_without_raw_event_attribute_leaves_default(stub_hermes_constants):
    assert _reasoning_config_for_event(object()) is None


def test_unimportable_core_does_not_raise(monkeypatch):
    """core 不在 PYTHONPATH 上时只能回落默认，绝不能炸掉整个 run。"""
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    assert _reasoning_config_for_event(_event({"reasoning_effort": "high"})) is None


def test_parser_exception_does_not_raise(monkeypatch):
    mod = ModuleType("hermes_constants")

    def _boom(_effort):
        raise RuntimeError("core blew up")

    mod.parse_reasoning_effort = _boom
    monkeypatch.setitem(sys.modules, "hermes_constants", mod)
    assert _reasoning_config_for_event(_event({"reasoning_effort": "high"})) is None


def test_shared_contract_matches_installed_core():
    """真 core 可导入时，比对 stub 与核在**两个版本共有**的档位上的返回，防契约漂移。

    刻意只比交集：测试 venv 钉的是 `hermes-agent>=0.14,<1.0` 解析出的 0.14.0
    （5 档 minimal/low/medium/high/xhigh，只认 "none" 表示关），而生产 broker 的
    PYTHONPATH 指向 core 0.21.3（7 档，多 max/ultra，另认 false/disabled）。
    helper 自己不持有词表、把词表判断整个交给运行时 core，所以两版都能跑；
    这条用例守的是共有部分不许变。
    """
    try:
        from hermes_constants import parse_reasoning_effort  # type: ignore
    except Exception:  # pragma: no cover - core 不在 PYTHONPATH 时跳过
        pytest.skip("hermes_constants ships with hermes-agent; not importable here")
    shared = ("minimal", "low", "medium", "high", "xhigh")
    for value in shared:
        assert parse_reasoning_effort(value) == {"enabled": True, "effort": value}, value
    assert parse_reasoning_effort("none") == {"enabled": False}
    assert parse_reasoning_effort("bogus") is None
    assert parse_reasoning_effort("") is None


def test_level_unknown_to_older_core_falls_back_not_crashes():
    """老 core（0.14，无 max/ultra）收到新档位词只回落默认，不抛。

    生产 broker 跑 0.21.3 认 max；但 MT 包的依赖下界是 0.14，混装时
    `metadata.reasoning_effort="max"` 必须安静回落到 profile 默认。
    """
    mod = ModuleType("hermes_constants")
    mod.VALID_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")

    def _core_0_14_parse(effort: str):
        if not effort or not effort.strip():
            return None
        effort = effort.strip().lower()
        if effort == "none":
            return {"enabled": False}
        if effort in mod.VALID_REASONING_EFFORTS:
            return {"enabled": True, "effort": effort}
        return None

    mod.parse_reasoning_effort = _core_0_14_parse
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "hermes_constants", mod)
        assert _reasoning_config_for_event(_event({"reasoning_effort": "max"})) is None
        assert _reasoning_config_for_event(
            _event({"reasoning_effort": "high"})
        ) == {"enabled": True, "effort": "high"}
        # 老 core 的 parse 对非 str 会 AttributeError —— helper 必须吞掉。
        assert _reasoning_config_for_event(_event({"reasoning_effort": True})) is None
