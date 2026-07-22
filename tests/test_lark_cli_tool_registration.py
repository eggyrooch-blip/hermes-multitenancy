import importlib
import json
import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _broker_env(monkeypatch):
    monkeypatch.setenv("LARKSUITE_CLI_AUTH_PROXY", "http://127.0.0.1:16384")
    monkeypatch.setenv("LARKSUITE_CLI_PROXY_KEY", "per-run-proxy-key")
    monkeypatch.setenv("LARKSUITE_CLI_APP_ID", "cli_public")


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
    assert registrations[-1]["max_result_size_chars"] == 30_000
    identity_description = registrations[-1]["schema"]["parameters"]["properties"]["identity"]["description"]
    assert "owner-mapped Feishu group message sends" in identity_description
    assert "non-message APIs are refused" in identity_description

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


def test_lark_cli_tool_allows_personal_bot_im_send_to_owner_mapped_chat(
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
    monkeypatch.setenv("HERMES_FEISHU_BOT_ALLOWED_CHAT_IDS", "oc_allowed")
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "user")

    captured = {}

    class Completed:
        returncode = 0
        stdout = '{"code":0,"data":{"message_id":"om_allowed"}}'
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fake_run)

    body = {
        "receive_id": "oc_allowed",
        "msg_type": "text",
        "content": json.dumps({"text": "hello"}, ensure_ascii=False),
    }
    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "api",
            "argv": [
                "POST",
                "/open-apis/im/v1/messages",
                "--params",
                json.dumps({"receive_id_type": "chat_id"}, ensure_ascii=False),
                "--data",
                json.dumps(body, ensure_ascii=False),
            ],
            "identity": "bot",
            "risk": "write",
            "reason": "send owner mapped group message as Hermes bot",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result["ok"] is True
    assert result["identity"] == "bot"
    assert captured["command"][-2:] == ["--as", "bot"]
    assert captured["kwargs"]["env"]["HERMES_FEISHU_BOT_ALLOWED_CHAT_IDS"] == "oc_allowed"


def test_lark_cli_tool_allows_personal_bot_im_send_with_query_string_path(
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
    monkeypatch.setenv("HERMES_FEISHU_BOT_ALLOWED_CHAT_IDS", "oc_allowed")
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "user")

    captured = {}

    class Completed:
        returncode = 0
        stdout = '{"code":0,"data":{"message_id":"om_allowed"}}'
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fake_run)

    body = {"receive_id": "oc_allowed", "msg_type": "text"}
    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "api",
            "argv": [
                "POST",
                "/open-apis/im/v1/messages?receive_id_type=chat_id",
                "--data",
                json.dumps(body, ensure_ascii=False),
            ],
            "identity": "bot",
            "risk": "write",
            "reason": "send owner mapped group message as Hermes bot",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result["ok"] is True
    assert result["identity"] == "bot"
    assert captured["command"][3] == "/open-apis/im/v1/messages?receive_id_type=chat_id"
    assert captured["command"][-2:] == ["--as", "bot"]


@pytest.mark.parametrize("mode", ["shortcut", "schema"])
def test_lark_cli_tool_allows_personal_bot_im_image_upload_for_owner_mapped_groups(
    monkeypatch,
    tmp_path,
    mode,
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
    monkeypatch.setenv("HERMES_FEISHU_BOT_ALLOWED_CHAT_IDS", "oc_allowed")
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "user")

    captured = {}

    class Completed:
        returncode = 0
        stdout = '{"code":0,"data":{"image_key":"img_allowed"}}'
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fake_run)

    argv = [
        "im",
        "images",
        "create",
        "--data",
        json.dumps({"image_type": "message"}, ensure_ascii=False),
        "--file",
        "image=battle_report_tech.jpg",
    ]
    if mode == "schema":
        argv[3:3] = ["--as", "bot"]

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": mode,
            "argv": argv,
            "identity": "bot",
            "risk": "write",
            "reason": "upload image for an owner-mapped Hermes bot group card",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result["ok"] is True
    assert result["identity"] == "bot"
    assert captured["command"][:4] == [str(binary), "im", "images", "create"]
    assert captured["command"][-2:] == ["--as", "bot"]
    assert captured["kwargs"]["env"]["HERMES_FEISHU_BOT_ALLOWED_CHAT_IDS"] == "oc_allowed"


def test_lark_cli_tool_defers_personal_bot_im_send_to_broker(
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
    monkeypatch.setenv("HERMES_FEISHU_BOT_ALLOWED_CHAT_IDS", "oc_allowed")
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "user")
    # Broker proxy is wired: deferral to the authoritative live-routing gate applies.
    monkeypatch.setenv("LARKSUITE_CLI_AUTH_PROXY", "http://127.0.0.1:1/")
    monkeypatch.setenv("LARKSUITE_CLI_PROXY_KEY", "k")

    class Completed:
        returncode = 0
        stdout = '{"code":0,"data":{"message_id":"om_other"}}'
        stderr = ""

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", lambda *_args, **_kwargs: Completed())

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["im", "+messages-send", "--chat-id", "oc_other", "--text", "hello"],
            "identity": "bot",
            "risk": "write",
            "reason": "send non-owned group message as Hermes bot",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    # The child preflight now DEFERS bot IM sends to the broker — the authoritative
    # gate that live-re-checks routing (see test_bot_group_livecheck). The sandboxed
    # child can't read routing, so it must not hard-reject here (that broke sending
    # to a sender's freshly-created own group until the next turn). In prod an
    # unmapped send is still refused, by the broker proxy (403).
    assert result.get("ok") is True
    assert "limited to owner mapped group chats" not in (result.get("error") or "")


def test_lark_cli_tool_defers_personal_bot_im_send_to_broker_even_when_risk_read(
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
    monkeypatch.setenv("HERMES_FEISHU_BOT_ALLOWED_CHAT_IDS", "oc_allowed")
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "user")
    # Broker proxy is wired: deferral to the authoritative live-routing gate applies.
    monkeypatch.setenv("LARKSUITE_CLI_AUTH_PROXY", "http://127.0.0.1:1/")
    monkeypatch.setenv("LARKSUITE_CLI_PROXY_KEY", "k")

    class Completed:
        returncode = 0
        stdout = '{"code":0,"data":{"message_id":"om_other"}}'
        stderr = ""

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", lambda *_args, **_kwargs: Completed())

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["im", "+messages-send", "--chat-id", "oc_other", "--text", "hello"],
            "identity": "bot",
            "risk": "read",
            "reason": "send non-owned group message as Hermes bot",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    # The child preflight now DEFERS bot IM sends to the broker — the authoritative
    # gate that live-re-checks routing (see test_bot_group_livecheck). The sandboxed
    # child can't read routing, so it must not hard-reject here (that broke sending
    # to a sender's freshly-created own group until the next turn). In prod an
    # unmapped send is still refused, by the broker proxy (403).
    assert result.get("ok") is True
    assert "limited to owner mapped group chats" not in (result.get("error") or "")


def test_lark_cli_tool_refuses_personal_bot_im_send_when_broker_proxy_absent(
    monkeypatch,
    tmp_path,
):
    # No broker proxy wired -> there is no authoritative gate to defer to, so the
    # child preflight must keep refusing an unmapped bot IM send rather than let it
    # through unauthorized (codex review round 3).
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
    monkeypatch.setenv("HERMES_FEISHU_BOT_ALLOWED_CHAT_IDS", "oc_allowed")
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "user")
    monkeypatch.delenv("LARKSUITE_CLI_AUTH_PROXY", raising=False)
    monkeypatch.delenv("LARKSUITE_CLI_PROXY_KEY", raising=False)

    class Completed:
        returncode = 0
        stdout = '{"code":0,"data":{"message_id":"om_other"}}'
        stderr = ""

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", lambda *_args, **_kwargs: Completed())

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["im", "+messages-send", "--chat-id", "oc_other", "--text", "hello"],
            "identity": "bot",
            "risk": "write",
            "reason": "send non-owned group message as Hermes bot",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result.get("ok") is not True
    assert result.get("error") == "lark-cli auth broker is unavailable in the current profile runtime"


def test_lark_cli_tool_refuses_personal_bot_im_send_no_broker_even_when_risk_read(
    monkeypatch,
    tmp_path,
):
    # A bot IM send mis-declared as risk="read" with no broker proxy must STILL be
    # refused — the send is a write regardless of the caller-declared risk, and
    # there is no authoritative gate to defer to (codex review round 4).
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
    monkeypatch.setenv("HERMES_FEISHU_BOT_ALLOWED_CHAT_IDS", "oc_allowed")
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "user")
    monkeypatch.delenv("LARKSUITE_CLI_AUTH_PROXY", raising=False)
    monkeypatch.delenv("LARKSUITE_CLI_PROXY_KEY", raising=False)

    class Completed:
        returncode = 0
        stdout = '{"code":0,"data":{"message_id":"om_other"}}'
        stderr = ""

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", lambda *_args, **_kwargs: Completed())

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["im", "+messages-send", "--chat-id", "oc_other", "--text", "hello"],
            "identity": "bot",
            "risk": "read",
            "reason": "send non-owned group message as Hermes bot",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result.get("ok") is not True
    assert result.get("error") == "lark-cli auth broker is unavailable in the current profile runtime"


def test_lark_cli_tool_keeps_user_coerced_read_when_requested_identity_is_bot(
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
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "user")

    captured = {}

    class Completed:
        returncode = 0
        stdout = '{"code":0,"data":{"open_id":"ou_owner"}}'
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
            "identity": "bot",
            "risk": "read",
            "reason": "read current Feishu user info",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result["ok"] is True
    assert result["identity"] == "user"
    assert captured["command"][-2:] == ["--as", "user"]


def test_lark_cli_tool_rejects_personal_bot_non_message_write_even_when_default_is_user(
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
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "user")

    def fail_run(*_args, **_kwargs):
        raise AssertionError("personal bot non-message write must fail before spawning lark-cli")

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fail_run)

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "api",
            "argv": [
                "POST",
                "/open-apis/calendar/v4/calendars",
                "--data",
                json.dumps({"summary": "Hermes"}, ensure_ascii=False),
            ],
            "identity": "bot",
            "risk": "write",
            "reason": "create calendar as Hermes bot",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    # NON-message bot writes (e.g. a calendar API POST) are still hard-refused in the
    # child preflight — only IM message sends to a chat defer to the broker. So this
    # must fail before spawning lark-cli.
    assert result.get("ok") is not True
    assert "personal profile bot identity is limited to owner mapped group chats" in result["error"]


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
    (profile / "group_profile.json").write_text('{"kind":"group","chat_id":"oc_group"}', encoding="utf-8")
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


def test_lark_cli_tool_rejects_personal_im_read_without_bound_user_identity(
    monkeypatch,
    tmp_path,
):
    from hermes_multitenancy import lark_cli_tool

    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    profile = tmp_path / "bob"
    workspace = profile / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "bot")
    monkeypatch.delenv("HERMES_FEISHU_USER_OPEN_ID", raising=False)

    def fail_run(*_args, **_kwargs):
        raise AssertionError("personal IM read must fail before spawning lark-cli")

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fail_run)

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["messages", "list", "--limit", "20"],
            "identity": "user",
            "risk": "read",
            "reason": "查看用户最近的飞书消息",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result.get("ok") is not True
    assert "飞书个人消息读取需要先完成本人授权" in result["error"]
    assert "/feishu_auth" in result["error"]
    assert "WebUI" in result["error"]
    assert "refusing bot/app fallback" not in result["error"]


def test_lark_cli_tool_rejects_personal_im_read_with_bot_identity_even_when_sender_exists(
    monkeypatch,
    tmp_path,
):
    from hermes_multitenancy import lark_cli_tool

    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    profile = tmp_path / "bob"
    workspace = profile / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", "ou_bob")
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "bot")

    def fail_run(*_args, **_kwargs):
        raise AssertionError("personal IM bot read must fail before spawning lark-cli")

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fail_run)

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["im", "+chat-list", "--page-size", "20"],
            "identity": "bot",
            "risk": "read",
            "reason": "查看最近群聊",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result.get("ok") is not True
    assert "飞书个人消息读取需要先完成本人授权" in result["error"]


def test_lark_cli_tool_rejects_personal_im_read_even_when_risk_is_mislabeled(
    monkeypatch,
    tmp_path,
):
    from hermes_multitenancy import lark_cli_tool

    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    profile = tmp_path / "bob"
    workspace = profile / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", "ou_bob")
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "bot")

    def fail_run(*_args, **_kwargs):
        raise AssertionError("mislabeled personal IM bot read must fail before spawning lark-cli")

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fail_run)

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["im", "+chat-list", "--page-size", "20"],
            "identity": "bot",
            "risk": "export",
            "reason": "mislabeled read",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result.get("ok") is not True
    assert "飞书个人消息读取需要先完成本人授权" in result["error"]


def test_lark_cli_tool_rejects_personal_post_message_search_api(
    monkeypatch,
    tmp_path,
):
    from hermes_multitenancy import lark_cli_tool

    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    profile = tmp_path / "bob"
    workspace = profile / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "bot")

    def fail_run(*_args, **_kwargs):
        raise AssertionError("personal IM search API must fail before spawning lark-cli")

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fail_run)

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "api",
            "argv": ["POST", "/open-apis/im/v1/messages/search", "--data", "{}"],
            "identity": "bot",
            "risk": "read",
            "reason": "search messages",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result.get("ok") is not True
    assert "飞书个人消息读取需要先完成本人授权" in result["error"]


def test_lark_cli_tool_allows_personal_im_read_with_bound_user_identity(
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
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "user")

    captured = {}

    class Completed:
        returncode = 0
        stdout = '{"ok":true,"identity":"user","data":{"items":[]}}'
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        return Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fake_run)

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["im", "+flag-list", "--page-size", "20"],
            "identity": "user",
            "risk": "read",
            "reason": "查看用户收藏消息",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result["ok"] is True
    assert result["identity"] == "user"
    assert captured["command"][-2:] == ["--as", "user"]


def test_lark_cli_tool_allows_group_profile_bot_im_read(
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
    (profile / "group_profile.json").write_text('{"kind":"group","chat_id":"oc_group"}', encoding="utf-8")
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "bot")

    captured = {}

    class Completed:
        returncode = 0
        stdout = '{"ok":true,"identity":"bot","data":{"items":[]}}'
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        return Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fake_run)

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["im", "+chat-messages-list", "--chat-id", "oc_group"],
            "identity": "bot",
            "risk": "read",
            "reason": "群 profile 查看当前群消息",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result["ok"] is True
    assert result["identity"] == "bot"
    assert captured["command"][-2:] == ["--as", "bot"]


def test_lark_cli_tool_rejects_group_profile_global_chat_list(
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
    (profile / "group_profile.json").write_text('{"kind":"group","chat_id":"oc_group"}', encoding="utf-8")
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "bot")

    def fail_run(*_args, **_kwargs):
        raise AssertionError("group profile global chat list must fail before spawning lark-cli")

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fail_run)

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["im", "+chat-list", "--page-size", "20"],
            "identity": "bot",
            "risk": "read",
            "reason": "列出 bot 所在群",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result.get("ok") is not True
    assert "group profile Feishu message read is limited to the current chat" in result["error"]


def test_lark_cli_tool_rejects_group_profile_other_chat_messages(
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
    (profile / "group_profile.json").write_text('{"kind":"group","chat_id":"oc_current"}', encoding="utf-8")
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "bot")

    def fail_run(*_args, **_kwargs):
        raise AssertionError("group profile other-chat read must fail before spawning lark-cli")

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fail_run)

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["im", "+chat-messages-list", "--chat-id", "oc_other"],
            "identity": "bot",
            "risk": "read",
            "reason": "读取其它群历史",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result.get("ok") is not True
    assert "group profile Feishu message read is limited to the current chat" in result["error"]


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
