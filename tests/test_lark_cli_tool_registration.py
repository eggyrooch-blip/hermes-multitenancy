import importlib
import json
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


def test_lark_cli_tool_accepts_api_argv_without_api_prefix(monkeypatch, tmp_path):
    from hermes_multitenancy import lark_cli_tool

    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    profile = tmp_path / "profile"
    workspace = profile / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("WORKSPACE", str(workspace))

    captured = {}

    class Completed:
        returncode = 0
        stdout = '{"code":0,"data":{"open_id":"ou_test"}}'
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fake_run)

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "api",
            "argv": ["GET", "/open-apis/authen/v1/user_info"],
            "identity": "user",
            "risk": "read",
            "reason": "smoke",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result["ok"] is True
    assert result["json"]["data"]["open_id"] == "ou_test"
    assert captured["command"][:3] == [str(binary), "api", "GET"]
    assert captured["command"][3] == "/open-apis/authen/v1/user_info"
