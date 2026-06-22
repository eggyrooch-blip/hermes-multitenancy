from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any

_FEISHU_MODULE_NAMES = (
    "gateway.platforms.feishu",
    "plugins.platforms.feishu.adapter",
)


def load_feishu_module() -> ModuleType:
    last_error: Exception | None = None
    for module_name in _FEISHU_MODULE_NAMES:
        try:
            return import_module(module_name)
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise ModuleNotFoundError("No Feishu adapter module candidates configured")


def load_feishu_adapter() -> Any:
    return getattr(load_feishu_module(), "FeishuAdapter")


def load_normalize_feishu_message() -> Any:
    return getattr(load_feishu_module(), "normalize_feishu_message")
