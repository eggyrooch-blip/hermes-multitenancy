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


def test_lark_cli_tool_auto_identity_uses_profile_default_as_user(monkeypatch, tmp_path):
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
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "user")

    captured = {}

    class Completed:
        returncode = 0
        stdout = '{"code":0,"data":{"app_token":"base_token"}}'
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fake_run)

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["base", "+base-create", "--name", "identity-smoke"],
            "identity": "auto",
            "risk": "write",
            "reason": "smoke",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result["ok"] is True
    assert result["identity"] == "user"
    assert captured["command"][-2:] == ["--as", "user"]


def test_lark_cli_tool_does_not_add_default_identity_to_event_shortcut(monkeypatch, tmp_path):
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
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "user")

    captured = {}

    class Completed:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fake_run)

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["event", "status"],
            "identity": "auto",
            "risk": "read",
            "reason": "smoke",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result["ok"] is True
    assert "--as" not in captured["command"]


def test_lark_cli_tool_does_not_add_default_identity_to_help_commands(monkeypatch, tmp_path):
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
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "user")

    captured = {}

    class Completed:
        returncode = 0
        stdout = "slides help"
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fake_run)

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["slides", "--help"],
            "identity": "auto",
            "risk": "read",
            "reason": "help smoke",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result["ok"] is True
    assert captured["command"] == [str(binary), "slides", "--help"]


def test_lark_cli_tool_preserves_resource_tokens_in_structured_json(monkeypatch, tmp_path):
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
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "user")

    class Completed:
        returncode = 0
        stdout = json.dumps(
            {
                "ok": True,
                "identity": "user",
                "data": {
                    "base": {
                        "base_token": "basetok_123",
                        "folder_token": "fld_456",
                        "url": "https://example.feishu.cn/base/basetok_123",
                    }
                },
            }
        )
        stderr = ""

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", lambda *_args, **_kwargs: Completed())

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["base", "+base-create", "--name", "resource-token-smoke"],
            "identity": "auto",
            "risk": "write",
            "reason": "smoke",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result["ok"] is True
    assert result["json"]["identity"] == "user"
    assert result["json"]["data"]["base"]["base_token"] == "basetok_123"
    assert result["json"]["data"]["base"]["folder_token"] == "fld_456"


def test_lark_cli_tool_profile_default_user_overrides_model_bot_guess(monkeypatch, tmp_path):
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
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "user")

    captured = {}

    class Completed:
        returncode = 0
        stdout = '{"ok":true,"identity":"user"}'
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        return Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fake_run)

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["base", "+base-create", "--name", "identity-smoke"],
            "identity": "bot",
            "risk": "write",
            "reason": "model guessed bot",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result["ok"] is True
    assert result["identity"] == "user"
    assert captured["command"][-2:] == ["--as", "user"]


def test_lark_cli_tool_profile_default_user_overrides_explicit_argv_as_bot(monkeypatch, tmp_path):
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
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "user")

    captured = {}

    class Completed:
        returncode = 0
        stdout = '{"ok":true,"identity":"user"}'
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        return Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fake_run)

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["markdown", "+create", "--as", "bot", "--file", "./技术方案.md"],
            "identity": "auto",
            "risk": "write",
            "reason": "create document from markdown",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result["ok"] is True
    assert result["identity"] == "user"
    assert "--as=bot" not in captured["command"]
    assert "bot" not in captured["command"]
    assert captured["command"][-2:] == ["--as", "user"]


def test_lark_cli_tool_rejects_personal_write_when_sender_user_identity_is_not_bound(
    monkeypatch,
    tmp_path,
):
    from hermes_multitenancy import lark_cli_tool

    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    profile = tmp_path / "owner"
    workspace = profile / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", "ou_owner")
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "bot")

    def fail_run(*_args, **_kwargs):
        raise AssertionError("personal user write must fail before spawning lark-cli")

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fail_run)

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "api",
            "argv": ["POST", "/open-apis/calendar/v4/calendars/primary/events"],
            "identity": "auto",
            "risk": "write",
            "reason": "create calendar event",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result.get("ok") is not True
    assert "personal profile write requires bound Feishu user identity" in result["error"]


def test_lark_cli_tool_allows_group_profile_bot_write_with_sender_context(
    monkeypatch,
    tmp_path,
):
    from hermes_multitenancy import lark_cli_tool

    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    profile = tmp_path / "feishu_group_test"
    workspace = profile / "workspace"
    workspace.mkdir(parents=True)
    (profile / "group_profile.json").write_text('{"kind":"group"}', encoding="utf-8")
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", "ou_owner")
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "bot")

    captured = {}

    class Completed:
        returncode = 0
        stdout = '{"ok":true,"identity":"bot"}'
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        return Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fake_run)

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "api",
            "argv": ["POST", "/open-apis/calendar/v4/calendars/primary/events"],
            "identity": "auto",
            "risk": "write",
            "reason": "group bot calendar event",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result["ok"] is True
    assert result["identity"] == "bot"
    assert captured["command"][-2:] == ["--as", "bot"]


def test_lark_cli_tool_filters_non_business_update_notice(monkeypatch, tmp_path):
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
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "bot")

    class Completed:
        returncode = 0
        stdout = '{"ok":true,"identity":"bot","data":{"document_id":"doc_1"}}'
        stderr = "A new version of lark-cli is available: v0.0.0 -> 1.0.34. Run lark-cli update to upgrade.\\n"

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", lambda *_args, **_kwargs: Completed())

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["docs", "+create", "--api-version", "v2", "--content", "<title>x</title>"],
            "identity": "auto",
            "risk": "write",
            "reason": "smoke",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result["ok"] is True
    assert result["json"]["data"]["document_id"] == "doc_1"
    rendered = json.dumps(result, ensure_ascii=False)
    assert "new version" not in rendered
    assert "lark-cli update" not in rendered
    assert "1.0.34" not in rendered
