"""Cold-start regression tests for plugin registration.

Hermes discovers plugins on a worker while the gateway runtime is importing on
the main thread.  Registration must therefore stay import-light; runtime
initialisation belongs to the first gateway dispatch, after discovery joins.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_register_does_not_import_gateway_runtime_before_first_dispatch() -> None:
    repo = Path(__file__).resolve().parents[1]
    script = r'''
import importlib.util
import json
import sys
from pathlib import Path

root = Path.cwd()
spec = importlib.util.spec_from_file_location(
    "cold_multitenancy_plugin",
    root / "__init__.py",
    submodule_search_locations=[str(root)],
)
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
# Hermes resolves every manifest-declared hook during discovery, before it
# calls register(). That lookup must remain just as import-light as register.
manifest_hook = plugin.on_pre_gateway_dispatch

class Ctx:
    def __init__(self):
        self.hooks = []
    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

ctx = Ctx()
plugin.register(ctx)
forbidden = sorted(
    name for name in sys.modules
    if name == "gateway.run" or name.endswith("hermes_multitenancy.router")
)
print(json.dumps({
    "hooks": [name for name, _ in ctx.hooks],
    "forbidden": forbidden,
    "same_callback": ctx.hooks[0][1] is manifest_hook,
}))
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )

    assert completed.stdout.strip() == (
        '{"hooks": ["pre_gateway_dispatch"], "forbidden": [], "same_callback": true}'
    )
