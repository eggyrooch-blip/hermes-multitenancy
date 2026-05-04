from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


def test_root_directory_plugin_shim_exposes_register():
    """`hermes plugins install owner/repo` loads the repository root."""
    repo_root = Path(__file__).resolve().parents[1]
    module_name = "hermes_plugins_test.multitenancy"

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
        assert callable(module.register)
        assert callable(module.on_pre_gateway_dispatch)
    finally:
        for name in list(sys.modules):
            if name == "hermes_plugins_test" or name.startswith("hermes_plugins_test."):
                sys.modules.pop(name, None)
