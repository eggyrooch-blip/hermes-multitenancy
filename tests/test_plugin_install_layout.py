from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


def test_root_directory_plugin_shim_registers_hook(monkeypatch, tmp_path):
    """`hermes plugins install owner/repo` loads the repository root."""
    repo_root = Path(__file__).resolve().parents[1]
    module_name = "hermes_plugins_test.multitenancy"
    saved_top_level = {
        name: module
        for name, module in list(sys.modules.items())
        if name == "hermes_multitenancy" or name.startswith("hermes_multitenancy.")
    }
    for name in saved_top_level:
        sys.modules.pop(name, None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "plugin_smoke"))

    parent = types.ModuleType("hermes_plugins_test")
    parent.__path__ = []
    parent.__package__ = "hermes_plugins_test"
    sys.modules[parent.__name__] = parent

    spec = importlib.util.spec_from_file_location(
        module_name,
        repo_root / "__init__.py",
        submodule_search_locations=[str(repo_root)],
    )
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(repo_root)]
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        hooks = []

        class FakeCtx:
            def register_hook(self, name, callback):
                hooks.append((name, callback))

        module.register(FakeCtx())
        assert [name for name, _callback in hooks] == ["pre_gateway_dispatch"]
        assert callable(module.on_pre_gateway_dispatch)
    finally:
        for name in list(sys.modules):
            if name == "hermes_plugins_test" or name.startswith("hermes_plugins_test."):
                sys.modules.pop(name, None)
        for name, saved_module in saved_top_level.items():
            sys.modules[name] = saved_module


def test_card_modules_import_when_loaded_under_plugin_package():
    """Card helpers must not depend on top-level ``hermes_multitenancy`` imports."""
    repo_root = Path(__file__).resolve().parents[1]
    module_name = "hermes_plugins_test.multitenancy"

    saved_top_level = sys.modules.get("hermes_multitenancy", ...)
    parent = types.ModuleType("hermes_plugins_test")
    parent.__path__ = []
    parent.__package__ = "hermes_plugins_test"
    sys.modules[parent.__name__] = parent
    sys.modules["hermes_multitenancy"] = None

    spec = importlib.util.spec_from_file_location(
        module_name,
        repo_root / "__init__.py",
        submodule_search_locations=[str(repo_root)],
    )
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(repo_root)]
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        card_module = __import__(
            f"{module_name}.hermes_multitenancy.feishu_auth_cards",
            fromlist=["build_auth_card"],
        )
        assert callable(card_module.build_auth_card)
    finally:
        for name in list(sys.modules):
            if name == "hermes_plugins_test" or name.startswith("hermes_plugins_test."):
                sys.modules.pop(name, None)
        if saved_top_level is ...:
            sys.modules.pop("hermes_multitenancy", None)
        else:
            sys.modules["hermes_multitenancy"] = saved_top_level
