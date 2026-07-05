from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType
from typing import Any

# Newer cores load the bundled feishu platform plugin through
# ``hermes_cli/plugins.py``, which imports the plugin directory under this
# SYNTHETIC package name via ``spec_from_file_location`` — producing a module
# object DISTINCT from ``plugins.platforms.feishu.adapter`` even though both
# come from the same source file. The synthetic module exists only in
# ``sys.modules`` (it cannot be imported by name), and it is the one the
# gateway actually instantiates — class patches must land on it, else they
# silently no-op (root cause of bot-added inviter capture never firing).
_PLUGIN_LOADER_MODULE_NAME = "hermes_plugins.feishu_platform.adapter"

_FEISHU_MODULE_NAMES = (
    "gateway.platforms.feishu",
    "plugins.platforms.feishu.adapter",
)


def _is_missing_candidate_module(exc: ModuleNotFoundError, module_name: str) -> bool:
    missing_name = exc.name
    if missing_name is None:
        message = str(exc)
        return message == module_name or message == f"No module named '{module_name}'"
    return missing_name == module_name or module_name.startswith(f"{missing_name}.")


def is_missing_feishu_module_error(exc: BaseException) -> bool:
    if not isinstance(exc, ModuleNotFoundError):
        return False
    missing_name = exc.name
    if missing_name is None:
        message = str(exc)
        return any(
            message == module_name or message == f"No module named '{module_name}'"
            for module_name in _FEISHU_MODULE_NAMES
        )
    return any(
        missing_name == module_name or module_name.startswith(f"{missing_name}.")
        for module_name in _FEISHU_MODULE_NAMES
    )


def log_feishu_adapter_load_error(logger: Any, message: str, exc: BaseException) -> None:
    if is_missing_feishu_module_error(exc):
        logger.info(message)
    else:
        logger.warning("%s: %s", message, exc, exc_info=True)


def load_feishu_module() -> ModuleType:
    # The plugin-loader's synthetic module wins when present: it is the
    # adapter the gateway actually runs. (For the legacy names below a plain
    # ``import_module`` already returns the cached ``sys.modules`` entry, so
    # only the synthetic name needs an explicit lookup.) Guarded on the
    # ``FeishuAdapter`` attr so a half-initialized module never wins.
    module = sys.modules.get(_PLUGIN_LOADER_MODULE_NAME)
    if module is not None and getattr(module, "FeishuAdapter", None) is not None:
        return module
    last_error: Exception | None = None
    for module_name in _FEISHU_MODULE_NAMES:
        try:
            return import_module(module_name)
        except ModuleNotFoundError as exc:
            if not _is_missing_candidate_module(exc, module_name):
                raise
            last_error = exc
    if last_error is not None:
        raise last_error
    raise ModuleNotFoundError("No Feishu adapter module candidates configured")


def load_feishu_adapter() -> Any:
    return getattr(load_feishu_module(), "FeishuAdapter")


def load_normalize_feishu_message() -> Any:
    return getattr(load_feishu_module(), "normalize_feishu_message")
