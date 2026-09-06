import ast
import importlib
import inspect
import json
import sys
import types
from pathlib import Path

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
    properties = registrations[-1]["schema"]["parameters"]["properties"]
    assert properties["recursive_read"]["type"] == "boolean"
    assert properties["recursive_read_limit"]["maximum"] == 1000
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
    assert (
        result["failure_subsystem"],
        result["error_code"],
        result["retryable"],
    ) == ("identity", "FEISHU_IDENTITY_UNBOUND", False)
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


@pytest.mark.parametrize(
    ("stdout", "stderr", "returncode", "expected"),
    [
        (
            '{"code":99991668,"msg":"redacted fixture"}',
            "",
            0,
            ("credential", "FEISHU_AUTH_REAUTH_REQUIRED", False),
        ),
        (
            '{"code":99991672,"msg":"redacted fixture"}',
            "",
            1,
            ("permission", "FEISHU_PERMISSION_DENIED", False),
        ),
        (
            '{"code":230020,"msg":"redacted fixture"}',
            "",
            1,
            ("lark_api", "FEISHU_RATE_LIMITED", True),
        ),
        (
            '{"code":123456,"msg":"redacted fixture"}',
            "",
            1,
            ("lark_api", "FEISHU_BUSINESS_ERROR", False),
        ),
        (
            "",
            "unrecognized failure",
            1,
            ("lark_api", "FEISHU_UNKNOWN", False),
        ),
        (
            "",
            "credential identity mismatch",
            1,
            ("identity", "FEISHU_IDENTITY_MISMATCH", False),
        ),
    ],
)
def test_lark_cli_tool_returns_structured_failure_taxonomy(
    monkeypatch,
    tmp_path,
    stdout,
    stderr,
    returncode,
    expected,
):
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

    class Completed:
        pass

    completed = Completed()
    completed.returncode = returncode
    completed.stdout = stdout
    completed.stderr = stderr
    monkeypatch.setattr(lark_cli_tool.subprocess, "run", lambda *_args, **_kwargs: completed)

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "api",
            "argv": ["GET", "/open-apis/authen/v1/user_info"],
            "identity": "user",
            "risk": "read",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result["ok"] is False
    assert (
        result["failure_subsystem"],
        result["error_code"],
        result["retryable"],
    ) == expected
    machine_fields = json.dumps(
        {
            "failure_subsystem": result["failure_subsystem"],
            "error_code": result["error_code"],
            "retryable": result["retryable"],
        }
    )
    assert "token" not in machine_fields.lower()
    assert "open_id" not in machine_fields.lower()


def test_lark_cli_tool_timeout_has_retryable_transport_taxonomy(monkeypatch, tmp_path):
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
    monkeypatch.setattr(
        lark_cli_tool.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            lark_cli_tool.subprocess.TimeoutExpired("lark-cli", 60)
        ),
    )

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "api",
            "argv": ["GET", "/open-apis/authen/v1/user_info"],
            "identity": "user",
            "risk": "read",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert (
        result["failure_subsystem"],
        result["error_code"],
        result["retryable"],
    ) == ("transport", "FEISHU_DEPENDENCY_TIMEOUT", True)


def test_strict_unsupported_write_is_blocked_before_connector(monkeypatch, tmp_path):
    from hermes_multitenancy import lark_cli_tool
    from hermes_multitenancy.operation_checkpoint import OperationCheckpointStore

    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    profile = tmp_path / "profiles" / "alice"
    workspace = profile / "workspace"
    state_home = profile / "state"
    workspace.mkdir(parents=True)
    state_home.mkdir()
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_PROFILE", "alice")
    monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", "ou_alice")
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")

    calls = 0

    def timeout(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise lark_cli_tool.subprocess.TimeoutExpired("lark-cli", 60)

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", timeout)
    args = {
        "mode": "api",
        "argv": ["POST", "/open-apis/bitable/v1/apps", "--data", '{"name":"weekly"}'],
        "identity": "user",
        "risk": "write",
        "reason": "create weekly base",
    }

    first_raw = lark_cli_tool._handle_lark_cli_execute(
        args,
        task_id="task-1",
        session_id="session-1",
        tool_call_id="call-1",
    )
    first = first_raw if isinstance(first_raw, dict) else json.loads(first_raw)
    assert first["ok"] is False
    assert first["error_code"] == "FEISHU_OPERATION_NOT_RESUMABLE"
    assert "_hermes_operation" not in first
    assert calls == 0


def test_strict_lark_cli_write_uses_actual_command_not_model_risk_and_fails_closed(
    monkeypatch,
    tmp_path,
):
    from hermes_multitenancy import lark_cli_tool

    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    profile = tmp_path / "profiles" / "alice"
    workspace = profile / "workspace"
    state_home = profile / "state"
    workspace.mkdir(parents=True)
    state_home.mkdir()
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_PROFILE", "alice")
    monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", "ou_alice")
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")

    calls = 0

    def should_not_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("unsupported write reached lark-cli")

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", should_not_run)
    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "api",
            "argv": ["POST", "/open-apis/bitable/v1/apps", "--data", '{"name":"weekly"}'],
            "identity": "user",
            "risk": "read",
            "reason": "create weekly base",
        },
        task_id="task-1",
        session_id="session-1",
        tool_call_id="call-1",
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result["ok"] is False
    assert result["error_code"] == "FEISHU_OPERATION_NOT_RESUMABLE"
    assert result["retryable"] is False
    assert calls == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["docs", "+fetch", "--document-id", "doc_test"],
        ["calendar", "+agenda"],
        ["drive", "+preview", "--file-token", "file_test"],
        ["minutes", "+detail", "--minute-token", "minute_test"],
        ["mail", "+watch", "--mailbox", "me"],
        ["wiki", "+node-list", "--space-id", "space_test"],
        ["base", "+record-list", "--app-token", "app_test", "--table-id", "table_test"],
        ["base", "+record-search", "--app-token", "app_test", "--table-id", "table_test"],
        ["base", "+data-query", "--app-token", "app_test", "--query", "{}"],
    ],
)
def test_strict_registered_read_shortcuts_are_classified_from_real_command(argv):
    from hermes_multitenancy import lark_cli_tool

    assert lark_cli_tool._strict_operation_kind("shortcut", argv) == "read"


def test_strict_read_shortcuts_match_installed_connector_risk():
    from hermes_multitenancy import lark_cli_tool

    binary = lark_cli_tool.shutil.which("lark-cli")
    if not binary:
        pytest.skip("installed lark-cli is required for the connector risk integration check")
    help_env = {
        key: value
        for key, value in lark_cli_tool.os.environ.items()
        if key not in {"LARKSUITE_CLI_AUTH_PROXY", "LARKSUITE_CLI_PROXY_KEY"}
    }
    mismatches = []
    for domain, shortcut in sorted(lark_cli_tool._STRICT_READ_SHORTCUTS):
        completed = lark_cli_tool.subprocess.run(
            [binary, domain, shortcut, "--help"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
            env=help_env,
        )
        output = f"{completed.stdout}\n{completed.stderr}"
        declared = next(
            (line.partition(":")[2].strip() for line in output.splitlines() if line.startswith("Risk:")),
            "",
        )
        if completed.returncode != 0 or declared != "read":
            mismatches.append((domain, shortcut, completed.returncode, declared))

    assert mismatches == []


@pytest.mark.parametrize(
    ("domain", "resource", "method"),
    [
        ("approval", "instances", "initiated"),
        ("approval", "tasks", "query"),
        ("calendar", "calendars", "primary"),
        ("calendar", "events", "instance_view"),
        ("calendar", "events", "search_event"),
        ("contact", "user_profiles", "batch_query"),
        ("drive", "metas", "batch_query"),
        ("im", "messages", "read_users"),
        ("im", "reactions", "batch_query"),
        ("mail", "user_mailboxes", "accessible_mailboxes"),
        ("mail", "user_mailboxes", "profile"),
        ("task", "sections", "tasks"),
        ("task", "tasklists", "tasks"),
        ("wiki", "spaces", "get_node"),
    ],
)
def test_strict_registered_schema_reads_use_exact_typed_allowlist(domain, resource, method):
    from hermes_multitenancy import lark_cli_tool

    argv = [domain, resource, method, "--page-size", "1"]
    assert lark_cli_tool._strict_operation_kind("schema", argv) == "read"
    prepared, intent, error = lark_cli_tool._prepare_resumable_write(
        env={},
        mode="schema",
        argv=argv,
        session_id="session-1",
        tool_call_id="call-1",
    )
    assert prepared == argv
    assert intent is None
    assert error is None


@pytest.mark.parametrize(
    ("mode", "argv"),
    [
        ("shortcut", ["base", "+record-share-link-create", "--app-token", "app_test"]),
        ("schema", ["mail", "user_mailbox.event", "subscription"]),
    ],
)
def test_strict_write_endpoints_cannot_bypass_checkpoint_as_reads(mode, argv):
    from hermes_multitenancy import lark_cli_tool

    assert lark_cli_tool._strict_operation_kind(mode, argv) != "read"
    _argv, intent, error = lark_cli_tool._prepare_resumable_write(
        env={},
        mode=mode,
        argv=argv,
        session_id="session-1",
        tool_call_id="call-1",
    )
    assert intent is None
    assert json.loads(error)["error_code"] == "FEISHU_OPERATION_NOT_RESUMABLE"


@pytest.mark.parametrize(
    "argv",
    [
        ["approval", "instances", "get"],
        ["calendar", "calendars", "list"],
        ["drive", "files", "list"],
        ["im", "chats", "get"],
        ["mail", "user_mailbox.messages", "list"],
        ["task", "tasks", "get"],
        ["wiki", "spaces", "list"],
    ],
)
def test_exact_schema_allowlist_preserves_existing_conventional_read_methods(argv):
    from hermes_multitenancy import lark_cli_tool

    prepared, intent, error = lark_cli_tool._prepare_resumable_write(
        env={},
        mode="schema",
        argv=argv,
        session_id="session-1",
        tool_call_id="call-1",
    )
    assert (prepared, intent, error) == (argv, None, None)


def test_strict_unknown_schema_method_does_not_trust_read_like_name():
    from hermes_multitenancy import lark_cli_tool

    argv = ["calendar", "invented_resource", "get"]
    assert lark_cli_tool._strict_operation_kind("schema", argv) == "unknown"
    _argv, _intent, error = lark_cli_tool._prepare_resumable_write(
        env={},
        mode="schema",
        argv=argv,
        session_id="session-1",
        tool_call_id="call-1",
    )
    assert json.loads(error)["error_code"] == "FEISHU_OPERATION_NOT_RESUMABLE"


def test_strict_unknown_shortcut_does_not_trust_model_read_risk():
    from hermes_multitenancy import lark_cli_tool

    argv = ["docs", "+invented-fetchish-command", "--document-id", "doc_test"]
    assert lark_cli_tool._strict_operation_kind("shortcut", argv) == "unknown"
    _argv, _intent, error = lark_cli_tool._prepare_resumable_write(
        env={},
        mode="shortcut",
        argv=argv,
        session_id="session-1",
        tool_call_id="call-1",
    )
    assert json.loads(error)["error_code"] == "FEISHU_OPERATION_NOT_RESUMABLE"


def test_strict_message_resource_download_is_a_write_and_unknown_stays_denied():
    from hermes_multitenancy import lark_cli_tool

    resource_download = [
        "im",
        "+messages-resources-download",
        "--message-id",
        "om_fixture",
        "--file-key",
        "img_fixture",
        "--type",
        "image",
    ]
    assert lark_cli_tool._strict_operation_kind("shortcut", resource_download) == "write"
    _argv, intent, error = lark_cli_tool._prepare_resumable_write(
        env={},
        mode="shortcut",
        argv=resource_download,
        session_id="session-1",
        tool_call_id="call-1",
    )
    assert intent is None
    assert json.loads(error)["error_code"] == "FEISHU_OPERATION_NOT_RESUMABLE"

    _argv, _intent, error = lark_cli_tool._prepare_resumable_write(
        env={},
        mode="shortcut",
        argv=["docs", "+future-fetchish-command"],
        session_id="session-1",
        tool_call_id="call-1",
    )
    assert json.loads(error)["error_code"] == "FEISHU_OPERATION_NOT_RESUMABLE"


def test_strict_schema_image_upload_is_explicitly_write_and_denied_before_connector():
    from hermes_multitenancy import lark_cli_tool

    argv = ["im", "images", "create", "--image", "fixture.png"]
    assert lark_cli_tool._strict_operation_kind("schema", argv) == "write"
    _argv, intent, error = lark_cli_tool._prepare_resumable_write(
        env={},
        mode="schema",
        argv=argv,
        session_id="session-1",
        tool_call_id="call-1",
    )
    assert intent is None
    assert json.loads(error)["error_code"] == "FEISHU_OPERATION_NOT_RESUMABLE"


@pytest.mark.parametrize(
    "argv",
    [
        ["im", "+messages-send", "--user-id", "ou_target", "--text", "hello"],
        ["im", "+messages-send", "--chat-id", "oc_owned", "--markdown", "**hello**"],
        ["im", "+messages-send", "--chat-id", "oc_owned", "--image", "img_fixture"],
        ["im", "+messages-reply", "--message-id", "om_parent", "--file", "./report.pdf"],
    ],
)
def test_strict_im_write_without_exact_readback_descriptor_is_blocked_before_connector(argv):
    from hermes_multitenancy import lark_cli_tool

    _prepared, intent, error = lark_cli_tool._prepare_resumable_write(
        env={},
        mode="shortcut",
        argv=argv,
        session_id="session-1",
        tool_call_id="call-1",
    )
    assert intent is None
    assert json.loads(error)["error_code"] == "FEISHU_OPERATION_NOT_RESUMABLE"


@pytest.mark.parametrize("verb", ["batch", "status", "download", "info", "mget"])
def test_strict_unknown_shortcut_does_not_trust_read_like_verb(verb):
    from hermes_multitenancy import lark_cli_tool

    argv = ["base", f"+future-{verb}"]
    assert lark_cli_tool._strict_operation_kind("shortcut", argv) == "unknown"


@pytest.mark.parametrize("missing", ["actor", "call_identity"])
def test_durable_write_identity_failures_return_typed_error(monkeypatch, tmp_path, missing):
    from hermes_multitenancy import lark_cli_tool

    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    env = {
        "HERMES_PROFILE": "alice",
        "HERMES_FEISHU_USER_OPEN_ID": "ou_alice",
        "XDG_STATE_HOME": str(tmp_path),
    }
    if missing == "actor":
        env.pop("HERMES_PROFILE")
    receipt, error = lark_cli_tool._begin_lark_cli_operation(
        env=env,
        mode="shortcut",
        argv=["im", "+messages-send"],
        identity="user",
        risk="write",
        task_id="task-1",
        intent_key=None if missing == "call_identity" else "call:session-1:call-1",
    )

    assert receipt is None
    assert json.loads(error)["error_code"] == "FEISHU_IDENTITY_UNBOUND"


def test_strict_im_send_timeout_blocks_ordinary_relaunch_without_deterministic_readback(
    monkeypatch,
    tmp_path,
):
    from hermes_multitenancy import lark_cli_tool
    from hermes_multitenancy.operation_checkpoint import OperationCheckpointStore

    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    profile = tmp_path / "profiles" / "alice"
    workspace = profile / "workspace"
    state_home = profile / "state"
    workspace.mkdir(parents=True)
    state_home.mkdir()
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_PROFILE", "alice")
    monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", "ou_alice")
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    monkeypatch.setenv("HERMES_TRUSTED_FEISHU_TOOL_SCOPE", "feishu:user")
    monkeypatch.setenv("HERMES_TRUSTED_FEISHU_CHAT_TYPE", "p2p")
    monkeypatch.setenv("HERMES_TRUSTED_FEISHU_CHAT_FENCE", "a" * 64)

    commands = []

    class Completed:
        returncode = 0
        stdout = '{"code":0,"data":{"message_id":"om_once"}}'
        stderr = ""

    def connector(command, **_kwargs):
        commands.append(command)
        if len(commands) == 1:
            raise lark_cli_tool.subprocess.TimeoutExpired("lark-cli", 60)
        return Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", connector)
    args = {
        "mode": "shortcut",
        "argv": ["im", "+messages-send", "--chat-id", "oc_owned", "--text", "hello"],
        "identity": "user",
        "risk": "read",
        "reason": "send one message",
    }
    context = {
        "task_id": "task-1",
        "session_id": "session-1",
        "tool_call_id": "call-1",
    }

    first_raw = lark_cli_tool._handle_lark_cli_execute(args, **context)
    first = json.loads(first_raw)
    assert first["_hermes_operation"]["state"] == "uncertain"
    lark_cli_tool.post_lark_cli_operation(tool_name="lark_cli", result=first_raw, **context)

    second = json.loads(lark_cli_tool._handle_lark_cli_execute(args, **context))
    assert second["ok"] is False
    assert second["error_code"] == "FEISHU_OPERATION_OUTCOME_UNCERTAIN"
    assert second["retryable"] is False
    assert len(commands) == 1

    index = commands[0].index("--idempotency-key")
    key = commands[0][index + 1]
    assert len(key) <= 50
    assert key not in json.dumps(second)

    store = OperationCheckpointStore(state_home / "operation-checkpoints.db")
    row = store.get(
        second["_hermes_operation"]["operation_id"],
        profile_name="alice",
        subject="ou_alice",
    )
    store.close()
    assert row["state"] == "uncertain"
    assert row["tool_scope"] == "feishu:user"
    assert row["chat_type"] == "p2p"
    assert row["chat_fence"] == "a" * 64


def test_strict_im_send_requires_typed_message_receipt_before_confirming(
    monkeypatch,
    tmp_path,
):
    from hermes_multitenancy import lark_cli_tool

    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    profile = tmp_path / "profiles" / "alice"
    workspace = profile / "workspace"
    state_home = profile / "state"
    workspace.mkdir(parents=True)
    state_home.mkdir()
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_PROFILE", "alice")
    monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", "ou_alice")
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")

    class MissingReceipt:
        returncode = 0
        stdout = '{"code":0,"data":{}}'
        stderr = ""

    monkeypatch.setattr(
        lark_cli_tool.subprocess,
        "run",
        lambda *_args, **_kwargs: MissingReceipt(),
    )
    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["im", "+messages-send", "--chat-id", "oc_owned", "--text", "hello"],
            "identity": "user",
            "risk": "write",
            "reason": "send one message",
        },
        task_id="task-1",
        session_id="session-1",
        tool_call_id="call-1",
    )
    result = json.loads(raw)

    assert result["ok"] is False
    assert result["error_code"] == "FEISHU_OPERATION_OUTCOME_UNCERTAIN"
    assert result["retryable"] is False
    assert result["_hermes_operation"]["state"] == "uncertain"


@pytest.mark.parametrize(
    ("argv", "read_message"),
    [
        (
            ["im", "+messages-send", "--chat-id", "oc_owned", "--text", "hello"],
            {
                "message_id": "om_once",
                "chat_id": "oc_owned",
                "msg_type": "text",
                "content": '{"text":"hello"}',
            },
        ),
        (
            ["im", "+messages-reply", "--message-id", "om_parent", "--text", "hello"],
            {
                "message_id": "om_once",
                "chat_id": "oc_owned",
                "parent_id": "om_parent",
                "root_id": "om_parent",
                "msg_type": "text",
                "content": '{"text":"hello"}',
            },
        ),
    ],
)
def test_strict_im_write_confirms_only_after_actor_bound_mget_matches(
    monkeypatch,
    tmp_path,
    argv,
    read_message,
):
    from hermes_multitenancy import lark_cli_tool

    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    profile = tmp_path / "profiles" / "alice"
    workspace = profile / "workspace"
    state_home = profile / "state"
    workspace.mkdir(parents=True)
    state_home.mkdir()
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_PROFILE", "alice")
    monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", "ou_alice")
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")

    commands = []

    def connector(command, **kwargs):
        commands.append((command, kwargs["env"]))
        if command[1:4] == ["api", "GET", "/open-apis/im/v1/messages/mget"]:
            return types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"code": 0, "data": {"items": [read_message]}}),
                stderr="",
            )
        return types.SimpleNamespace(
            returncode=0,
            stdout='{"code":0,"data":{"message_id":"om_once","chat_id":"oc_owned"}}',
            stderr="",
        )

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", connector)
    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": argv,
            "identity": "user",
            "risk": "read",
            "reason": "write one message",
        },
        task_id="task-1",
        session_id="session-1",
        tool_call_id="call-1",
    )
    result = json.loads(raw)

    assert result["ok"] is True
    assert result["_hermes_operation"] == {
        "operation_id": result["_hermes_operation"]["operation_id"],
        "state": "confirmed",
        "step": "execute",
        "result_ref": "om_once",
    }
    assert "content_fp" not in json.dumps(result)
    assert len(commands) == 2
    read_command, read_env = commands[1]
    assert read_command[1:4] == ["api", "GET", "/open-apis/im/v1/messages/mget"]
    assert json.loads(read_command[read_command.index("--params") + 1]) == {
        "message_ids": "om_once"
    }
    assert read_command[-2:] == ["--as", "user"]
    assert read_env["HERMES_PROFILE"] == "alice"
    assert read_env["HERMES_FEISHU_USER_OPEN_ID"] == "ou_alice"
    assert read_env["LARKSUITE_CLI_AUTH_PROXY"] == "http://127.0.0.1:16384"


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("message_id", "om_other"),
        ("chat_id", "oc_other"),
        ("msg_type", "post"),
        ("content", '{"text":"other"}'),
    ],
)
def test_strict_im_write_readback_mismatch_stays_uncertain_without_rewrite(
    monkeypatch,
    tmp_path,
    field,
    wrong,
):
    from hermes_multitenancy import lark_cli_tool

    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    profile = tmp_path / "profiles" / "alice"
    workspace = profile / "workspace"
    state_home = profile / "state"
    workspace.mkdir(parents=True)
    state_home.mkdir()
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_PROFILE", "alice")
    monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", "ou_alice")
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")

    read_message = {
        "message_id": "om_once",
        "chat_id": "oc_owned",
        "msg_type": "text",
        "content": '{"text":"hello"}',
        field: wrong,
    }
    commands = []

    def connector(command, **_kwargs):
        commands.append(command)
        if command[1:4] == ["api", "GET", "/open-apis/im/v1/messages/mget"]:
            return types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"code": 0, "data": {"items": [read_message]}}),
                stderr="",
            )
        return types.SimpleNamespace(
            returncode=0,
            stdout='{"code":0,"data":{"message_id":"om_once","chat_id":"oc_owned"}}',
            stderr="",
        )

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", connector)
    args = {
        "mode": "shortcut",
        "argv": ["im", "+messages-send", "--chat-id", "oc_owned", "--text", "hello"],
        "identity": "user",
        "risk": "write",
        "reason": "send one message",
    }
    context = {"task_id": "task-1", "session_id": "session-1", "tool_call_id": "call-1"}
    first_raw = lark_cli_tool._handle_lark_cli_execute(args, **context)
    first = json.loads(first_raw)
    lark_cli_tool.post_lark_cli_operation(tool_name="lark_cli", result=first_raw)
    second = json.loads(lark_cli_tool._handle_lark_cli_execute(args, **context))

    assert first["ok"] is False
    assert first["error_code"] == "FEISHU_OPERATION_READBACK_MISMATCH"
    assert first["_hermes_operation"]["state"] == "uncertain"
    assert second["error_code"] == "FEISHU_OPERATION_OUTCOME_UNCERTAIN"
    assert len(commands) == 2


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("timeout", "FEISHU_OPERATION_READBACK_UNAVAILABLE"),
        ("permission", "FEISHU_OPERATION_READBACK_UNAVAILABLE"),
        ("empty", "FEISHU_OPERATION_READBACK_MISMATCH"),
    ],
)
def test_strict_im_write_readback_dependency_failures_never_confirm(monkeypatch, kind, expected):
    from hermes_multitenancy import lark_cli_tool

    def connector(*_args, **_kwargs):
        if kind == "timeout":
            raise lark_cli_tool.subprocess.TimeoutExpired("lark-cli", 60)
        payload = (
            {"code": 230020, "msg": "permission denied"}
            if kind == "permission"
            else {"code": 0, "data": {"items": []}}
        )
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", connector)
    assert lark_cli_tool._readback_resumable_message(
        binary="/managed/lark-cli-authsidecar",
        env={"HERMES_PROFILE": "alice", "HERMES_FEISHU_USER_OPEN_ID": "ou_alice"},
        cwd=None,
        timeout=60,
        identity="user",
        argv=["im", "+messages-send", "--chat-id", "oc_owned", "--text", "hello"],
        message_id="om_once",
    ) == expected


def test_strict_im_write_readback_keeps_two_actor_sessions_cross_match_zero(monkeypatch, tmp_path):
    from hermes_multitenancy import lark_cli_tool

    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    readbacks = []

    def connector(command, **kwargs):
        profile_name = kwargs["env"]["HERMES_PROFILE"]
        message_id = f"om_{profile_name}"
        if command[1:4] == ["api", "GET", "/open-apis/im/v1/messages/mget"]:
            params = json.loads(command[command.index("--params") + 1])
            readbacks.append((profile_name, params["message_ids"]))
            return types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "code": 0,
                    "data": {"items": [{
                        "message_id": message_id,
                        "chat_id": "oc_owned",
                        "msg_type": "text",
                        "content": '{"text":"hello"}',
                    }]},
                }),
                stderr="",
            )
        return types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"code": 0, "data": {"message_id": message_id}}),
            stderr="",
        )

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", connector)
    operation_ids = []
    for profile_name in ("alice", "bob"):
        profile = tmp_path / "profiles" / profile_name
        workspace = profile / "workspace"
        workspace.mkdir(parents=True)
        monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
        monkeypatch.setenv("HERMES_HOME", str(profile))
        monkeypatch.setenv("HERMES_PROFILE", profile_name)
        monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", f"ou_{profile_name}")
        monkeypatch.setenv("WORKSPACE", str(workspace))
        monkeypatch.setenv("XDG_STATE_HOME", str(profile / "state"))
        monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
        result = json.loads(lark_cli_tool._handle_lark_cli_execute(
            {
                "mode": "shortcut",
                "argv": ["im", "+messages-send", "--chat-id", "oc_owned", "--text", "hello"],
                "identity": "user",
                "risk": "write",
                "reason": "send one message",
            },
            task_id=f"task-{profile_name}",
            session_id=f"session-{profile_name}",
            tool_call_id="call-1",
        ))
        assert result["_hermes_operation"]["state"] == "confirmed"
        operation_ids.append(result["_hermes_operation"]["operation_id"])

    assert len(set(operation_ids)) == 2
    assert readbacks == [("alice", "om_alice"), ("bob", "om_bob")]


def test_strict_distinct_tool_calls_can_repeat_same_im_write(monkeypatch, tmp_path):
    from hermes_multitenancy import lark_cli_tool

    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    profile = tmp_path / "profiles" / "alice"
    workspace = profile / "workspace"
    state_home = profile / "state"
    workspace.mkdir(parents=True)
    state_home.mkdir()
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_PROFILE", "alice")
    monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", "ou_alice")
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")

    calls = 0

    def succeed(command, **_kwargs):
        nonlocal calls
        calls += 1
        if command[1:4] == ["api", "GET", "/open-apis/im/v1/messages/mget"]:
            return types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "code": 0,
                    "data": {"items": [{
                        "message_id": "om_once",
                        "chat_id": "oc_owned",
                        "msg_type": "text",
                        "content": '{"text":"hello"}',
                    }]},
                }),
                stderr="",
            )
        return types.SimpleNamespace(
            returncode=0,
            stdout='{"code":0,"data":{"message_id":"om_once"}}',
            stderr="",
        )

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", succeed)
    args = {
        "mode": "shortcut",
        "argv": ["im", "+messages-send", "--chat-id", "oc_owned", "--text", "hello"],
        "identity": "user",
        "risk": "write",
        "reason": "send one message",
    }

    first_raw = lark_cli_tool._handle_lark_cli_execute(
        args,
        task_id="task-2",
        session_id="session-2",
        tool_call_id="call-1",
    )
    first = json.loads(first_raw)
    assert first["_hermes_operation"]["state"] == "confirmed"
    lark_cli_tool.post_lark_cli_operation(tool_name="lark_cli", args=args, result=first_raw)
    assert "_hermes_operation" not in json.loads(
        lark_cli_tool.transform_lark_cli_operation_result(
            tool_name="lark_cli", args=args, result=first_raw
        )
    )

    second = json.loads(
        lark_cli_tool._handle_lark_cli_execute(
            args,
            task_id="task-2",
            session_id="session-2",
            tool_call_id="call-2",
        )
    )
    assert second["ok"] is True
    assert second["_hermes_operation"]["state"] == "confirmed"
    assert calls == 4


def test_operation_result_transform_fails_closed_when_sanitizing_fails():
    from hermes_multitenancy import lark_cli_tool

    result = {"ok": True, "value": object(), "_hermes_operation": {"state": "confirmed"}}
    transformed = json.loads(
        lark_cli_tool.transform_lark_cli_operation_result(tool_name="lark_cli", result=result)
    )

    assert transformed == {
        "ok": False,
        "error": "lark-cli result unavailable",
        "error_code": "FEISHU_OPERATION_RESULT_UNAVAILABLE",
        "retryable": False,
    }


def test_model_tools_host_seam_blocks_uncertain_step_relaunch_and_strips_operation_receipt(monkeypatch, tmp_path):
    from hermes_cli import plugins
    monkeypatch.setattr(plugins, "discover_plugins", lambda: None)
    import model_tools

    model_tools_tree = ast.parse(inspect.getsource(model_tools))
    dispatch_calls = [
        node
        for node in ast.walk(model_tools_tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "registry"
            and node.func.attr == "dispatch"
        )
    ]
    if not dispatch_calls or not all(
        any(keyword.arg == "tool_call_id" for keyword in node.keywords)
        for node in dispatch_calls
    ):
        pytest.skip("installed Agent does not provide the approved tool_call_id host contract")
    from hermes_multitenancy import lark_cli_tool
    from hermes_multitenancy.operation_checkpoint import OperationCheckpointStore

    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    profile = tmp_path / "profiles" / "alice"
    workspace = profile / "workspace"
    state_home = profile / "state"
    workspace.mkdir(parents=True)
    state_home.mkdir()
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_PROFILE", "alice")
    monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", "ou_alice")
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")

    calls = 0

    class Completed:
        returncode = 0
        stdout = '{"code":0,"data":{"message_id":"om_once"}}'
        stderr = ""

    def connector(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise lark_cli_tool.subprocess.TimeoutExpired("lark-cli", 60)
        return Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", connector)
    manager = plugins.get_plugin_manager()
    old_hooks = manager._hooks
    manager._hooks = {
        "post_tool_call": [lark_cli_tool.post_lark_cli_operation],
        "transform_tool_result": [lark_cli_tool.transform_lark_cli_operation_result],
    }
    args = {
        "mode": "shortcut",
        "argv": ["im", "+messages-send", "--chat-id", "oc_owned", "--text", "hello"],
        "identity": "user",
        "risk": "write",
        "reason": "send one message",
    }
    try:
        context = {
            "task_id": "task-3",
            "session_id": "session-3",
            "tool_call_id": "call-3",
        }
        first = json.loads(model_tools.handle_function_call("lark_cli", args, **context))
        second = json.loads(model_tools.handle_function_call("lark_cli", args, **context))
    finally:
        manager._hooks = old_hooks

    assert "_hermes_operation" not in first
    assert "_hermes_operation" not in second
    assert first["error_code"] == "FEISHU_DEPENDENCY_TIMEOUT"
    assert second["ok"] is False
    assert second["error_code"] == "FEISHU_OPERATION_OUTCOME_UNCERTAIN"
    assert second["retryable"] is False
    assert calls == 1
    store = OperationCheckpointStore(state_home / "operation-checkpoints.db")
    rows = store._conn.execute(
        "SELECT state FROM multitenancy_operation_checkpoints"
    ).fetchall()
    store.close()
    assert [row["state"] for row in rows] == ["uncertain"]


@pytest.mark.parametrize(
    ("runtime_setup", "expected"),
    [
        ("outside", ("permission", "FEISHU_PERMISSION_DENIED", False)),
        ("workspace_escape", ("permission", "FEISHU_PERMISSION_DENIED", False)),
        ("broker_missing", ("transport", "FEISHU_DEPENDENCY_UNAVAILABLE", False)),
    ],
)
def test_lark_cli_tool_classifies_profile_runtime_refusals(
    monkeypatch,
    tmp_path,
    runtime_setup,
    expected,
):
    from hermes_multitenancy import lark_cli_tool

    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    profile = tmp_path / "profile"
    workspace = profile / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("WORKSPACE", str(workspace))
    if runtime_setup == "outside":
        monkeypatch.delenv("HERMES_HOME")
        monkeypatch.delenv("WORKSPACE")
    elif runtime_setup == "workspace_escape":
        escaped = tmp_path / "outside"
        escaped.mkdir()
        monkeypatch.setenv("WORKSPACE", str(escaped))
    else:
        monkeypatch.delenv("LARKSUITE_CLI_AUTH_PROXY")
        monkeypatch.delenv("LARKSUITE_CLI_PROXY_KEY")
    monkeypatch.setattr(
        lark_cli_tool.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("runtime refusal must happen before lark-cli")
        ),
    )

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "api",
            "argv": ["GET", "/open-apis/authen/v1/user_info"],
            "identity": "user",
            "risk": "read",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert (
        result["failure_subsystem"],
        result["error_code"],
        result["retryable"],
    ) == expected


def test_lark_cli_bound_profile_wrong_write_identity_is_mismatch(monkeypatch, tmp_path):
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
    monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", "ou_fixture")
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "bot")
    monkeypatch.setattr(
        lark_cli_tool.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("identity mismatch must happen before lark-cli")
        ),
    )

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "api",
            "argv": ["POST", "/open-apis/calendar/v4/calendars"],
            "identity": "auto",
            "risk": "write",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert (
        result["failure_subsystem"],
        result["error_code"],
        result["retryable"],
    ) == ("identity", "FEISHU_IDENTITY_MISMATCH", False)


def test_lark_cli_tool_fields_match_classifier_for_same_fixture(monkeypatch, tmp_path):
    from hermes_multitenancy import lark_cli_tool
    from hermes_multitenancy.connector_failure_classifier import classify_connector_failure

    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    profile = tmp_path / "profile"
    workspace = profile / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("WORKSPACE", str(workspace))

    class Completed:
        returncode = 1
        stdout = '{"code":230020,"msg":"redacted fixture"}'
        stderr = ""

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", lambda *_args, **_kwargs: Completed())
    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "api",
            "argv": ["GET", "/open-apis/authen/v1/user_info"],
            "identity": "user",
            "risk": "read",
        }
    )
    tool_fields = raw if isinstance(raw, dict) else json.loads(raw)
    classified = classify_connector_failure(
        "lark-cli",
        exit_code=1,
        business_payload={"code": 230020, "msg": "redacted fixture"},
    )

    keys = ("failure_subsystem", "error_code", "retryable")
    assert {key: tool_fields[key] for key in keys} == {
        key: classified[key]
        for key in keys
    }


def test_lark_cli_successful_retry_text_does_not_mark_write_retryable(monkeypatch, tmp_path):
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
        stdout = '{"code":0,"data":{"calendar_id":"fixture"}}'
        stderr = "HTTP 429 rate limited; auto-retried and succeeded"

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", lambda *_args, **_kwargs: Completed())
    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "api",
            "argv": ["POST", "/open-apis/calendar/v4/calendars"],
            "identity": "user",
            "risk": "write",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result["ok"] is True
    assert result["error_code"] is None
    assert result["failure_subsystem"] is None
    assert result["retryable"] is False
