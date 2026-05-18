import importlib
import sys
import types


def test_lark_cli_tool_registers_with_hermes_registry_when_available(monkeypatch):
    calls = []

    class FakeRegistry:
        def register_toolset_alias(self, alias, toolset):
            calls.append(("alias", alias, toolset))

        def register(self, **kwargs):
            calls.append(("register", kwargs))

    tools_pkg = types.ModuleType("tools")
    registry_mod = types.ModuleType("tools.registry")
    registry_mod.registry = FakeRegistry()
    registry_mod.tool_error = lambda message, **kwargs: {"ok": False, "error": message, **kwargs}
    registry_mod.tool_result = lambda **kwargs: {"ok": True, **kwargs}

    with monkeypatch.context() as m:
        m.setitem(sys.modules, "tools", tools_pkg)
        m.setitem(sys.modules, "tools.registry", registry_mod)
        import hermes_multitenancy.lark_cli_tool as lark_cli_tool

        importlib.reload(lark_cli_tool)

    assert ("alias", "lark-cli", "lark_cli") in calls
    registrations = [item[1] for item in calls if item[0] == "register"]
    assert registrations
    assert registrations[-1]["name"] == "lark_cli"
    assert registrations[-1]["toolset"] == "lark_cli"
    assert registrations[-1]["schema"]["name"] == "lark_cli"

    import hermes_multitenancy.lark_cli_tool as lark_cli_tool

    importlib.reload(lark_cli_tool)
