from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType
from typing import Any

# Newer cores load the bundled feishu platform plugin through
# ``hermes_cli/plugins.py``, which imports the plugin directory under a
# SYNTHETIC package name via ``spec_from_file_location`` — producing a module
# object DISTINCT from ``plugins.platforms.feishu.adapter`` even though both
# come from the same source file. The synthetic module exists only in
# ``sys.modules`` (it cannot be imported by name), and it is the one the
# gateway actually instantiates — class patches must land on it, else they
# silently no-op (root cause of bot-added inviter capture never firing).
#
# The ONE list of synthetic names, newest core first. Core 0.21.5 derives the
# name from ``manifest.key`` (``platforms/feishu`` → ``platforms__feishu``);
# core <= 0.21.4 used ``feishu_platform``. Missing the new name made
# ``load_live_feishu_module`` fail closed and the router exit at startup
# (local UAT 2026-10-07).
_PLUGIN_LOADER_MODULE_NAMES = (
    "hermes_plugins.platforms__feishu.adapter",
    "hermes_plugins.feishu_platform.adapter",
)

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


def is_executor_shutdown_error(exc: BaseException) -> bool:
    """True for the fatal ``RuntimeError`` raised once the process is tearing down.

    ``loop.run_in_executor`` / ``asyncio.to_thread`` raise this the moment the
    SDK executor (or the interpreter itself) has been shut down; both wordings
    ("after shutdown" and "after interpreter shutdown") share the same prefix.
    Nothing can be sent from this process again, so retrying is always futile —
    the send-retry loop must give up immediately instead of paying 1s + 2s of
    backoff per chat (prod 2026-07-30: 4-minute SIGTERM→SIGKILL hang).
    """
    return isinstance(exc, RuntimeError) and "cannot schedule new futures" in str(exc)


def _complete_adapter_module(module: Any) -> ModuleType | None:
    # Guarded on the ``FeishuAdapter`` attr so a half-initialized module never wins.
    if module is not None and getattr(module, "FeishuAdapter", None) is not None:
        return module
    return None


def _module_of_registry_entry(entry: Any) -> ModuleType | None:
    """The module behind a registered feishu platform entry.

    ``adapter_factory`` is the very class the gateway instantiates, so its
    module is authoritative whatever name the core gave it. Core 0.21.5 scopes
    entries per HERMES_HOME and gives every scope after the first a
    ``__home_<digest>`` suffixed module, so a bare candidate name already in
    ``sys.modules`` may belong to ANOTHER home — the current scope's entry must
    win over it.
    """
    factory = getattr(entry, "adapter_factory", None)
    module_name = getattr(factory, "__module__", None)
    if not isinstance(module_name, str):
        return None
    module = _complete_adapter_module(sys.modules.get(module_name))
    # A class factory must BE the module's ``FeishuAdapter``; anything else
    # means this module is not the one the entry instantiates.
    if module is not None and isinstance(factory, type) and module.FeishuAdapter is not factory:
        return None
    return module


def _loaded_synthetic_module() -> ModuleType | None:
    """The plugin loader's synthetic feishu adapter module, once it exists
    under any known name (newest core first)."""
    for module_name in _PLUGIN_LOADER_MODULE_NAMES:
        module = _complete_adapter_module(sys.modules.get(module_name))
        if module is not None:
            return module
    return None


def _materialize_deferred_feishu_platform() -> ModuleType | None:
    """Resolve the current scope's feishu registry entry (loading it if deferred).

    Since core v0190 bundled ``kind: platform`` plugins are DEFERRED
    (``hermes_cli/plugins.py`` → ``_register_deferred_platform``): discovery only
    hands ``platform_registry`` a loader, so the synthetic module does not exist
    until the first registry lookup. multitenancy ``register()`` runs before that
    — it used to miss, fall back, and import the same source file a second time,
    landing all 15 class patches on a clone the gateway never instantiates
    (prod 2026-07-25 → 07-30, silently).

    ``get()`` is the public API and runs the deferred loader; it only imports +
    registers (``check_fn``/``validate_config`` are reached from
    ``create_adapter`` alone), so a non-gateway context with no ``FEISHU_*`` env
    is safe. Fail-open: nothing here is worth breaking a patch install over —
    on any failure we are exactly where we were before, at the old fallback.

    Returns the module behind the current scope's registered entry, else None
    (no registry, no entry, or an entry without a resolvable adapter module —
    callers then probe the known synthetic names).
    """
    try:
        from gateway.platform_registry import platform_registry

        return _module_of_registry_entry(platform_registry.get("feishu"))
    except Exception:
        return None


def load_feishu_module() -> ModuleType:
    # The current scope's registry entry wins: its adapter class is the one the
    # gateway runs. Without a registry answer the plugin-loader's synthetic
    # module wins when present. (For the legacy names below a plain
    # ``import_module`` already returns the cached ``sys.modules`` entry, so
    # only the synthetic names need an explicit lookup.)
    module = _materialize_deferred_feishu_platform() or _loaded_synthetic_module()
    if module is not None:
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


def load_live_feishu_module() -> ModuleType:
    """Resolve the adapter class the gateway will instantiate, or fail closed.

    Current-scope registry entry first (see ``_module_of_registry_entry``);
    known synthetic names only when the registry has no usable entry.
    """
    try:
        from gateway.platform_registry import platform_registry
    except ModuleNotFoundError as exc:
        if not _is_missing_candidate_module(exc, "gateway.platform_registry"):
            raise
        return _loaded_synthetic_module() or load_feishu_module()

    module = _module_of_registry_entry(platform_registry.get("feishu")) or _loaded_synthetic_module()
    if module is None:
        raise RuntimeError("live Feishu adapter module did not materialize")
    return module


def load_feishu_adapter() -> Any:
    return getattr(load_feishu_module(), "FeishuAdapter")


def load_normalize_feishu_message() -> Any:
    return getattr(load_feishu_module(), "normalize_feishu_message")
