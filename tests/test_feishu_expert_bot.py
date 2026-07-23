from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest


FIXED_EXPERT = "keep-resource-delivery"


def _event(
    text: str = "hi",
    *,
    sender_open_id: str = "ou_expert_app",
    union_id: str | None = "on_delivery_user",
    chat_id: str = "oc_dm",
    metadata: dict | None = None,
) -> SimpleNamespace:
    source = SimpleNamespace(
        chat_id=chat_id,
        user_id=sender_open_id,
        user_id_alt=union_id,
        chat_type="p2p",
        platform=SimpleNamespace(value="feishu"),
    )
    raw_sender_id = {"open_id": sender_open_id}
    if union_id is not None:
        raw_sender_id["union_id"] = union_id
    return SimpleNamespace(
        text=text,
        source=source,
        raw_event={
            "event": {"sender": {"sender_id": raw_sender_id}},
            "metadata": dict(metadata or {}),
        },
    )


@pytest.fixture
def table():
    from hermes_multitenancy.routing import RoutingTable

    t = RoutingTable(":memory:")
    yield t
    t.close()


def _sync_user(table, *, user_id: str, profile_name: str, open_id: str, union_id: str) -> None:
    table.upsert(
        user_id=user_id,
        profile_name=profile_name,
        open_id=open_id,
        union_id=union_id,
        provenance="sync",
    )


def test_fixed_context_resolves_by_union_and_rewrites_identity_and_metadata(
    table, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_multitenancy import agent_real, expert_bot_route

    profile_home = tmp_path / "profiles" / "alice"
    _sync_user(
        table,
        user_id="u_alice",
        profile_name="alice",
        open_id="ou_main_bot_alice",
        union_id="on_delivery_user",
    )
    monkeypatch.setattr(expert_bot_route, "role_override_block_for", lambda *_a, **_kw: "**Role Override**")

    event = _event(
        sender_open_id="ou_expert_app_alice",
        metadata={
            "expert_id": "forged",
            "model": "evil/model",
            "provider": "evil",
            "source": "client",
            "ingest_secret_dir": "/tmp/secret",
            "ingest_secrets": [{"name": "x"}],
        },
    )
    result = expert_bot_route.resolve_fixed_expert_context(
        event,
        routing_table=table,
        profile_home_resolver=lambda name: profile_home,
        expert_id=FIXED_EXPERT,
    )

    assert isinstance(result, expert_bot_route.FixedExpertContext)
    assert result.profile_name == "alice"
    assert result.profile_home == profile_home
    assert result.canonical_open_id == "ou_main_bot_alice"

    expert_bot_route.apply_fixed_expert_context(event, result)

    assert event.sender_open_id == "ou_main_bot_alice"
    metadata = agent_real._event_metadata(event)
    assert metadata["expert_id"] == FIXED_EXPERT
    assert metadata["fixed_expert_open_id"] == "ou_main_bot_alice"
    assert metadata["fixed_expert_union_id"] == "on_delivery_user"
    assert "model" not in metadata
    assert "provider" not in metadata
    assert "source" not in metadata
    assert "ingest_secret_dir" not in metadata
    assert "ingest_secrets" not in metadata
    assert agent_real._expert_id_for_event(event) == FIXED_EXPERT


def test_fixed_context_rejects_duplicate_sync_union_rows(table, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes_multitenancy import expert_bot_route

    _sync_user(table, user_id="u1", profile_name="alice", open_id="ou_a", union_id="on_dup")
    _sync_user(table, user_id="u2", profile_name="bob", open_id="ou_b", union_id="on_dup")
    monkeypatch.setattr(expert_bot_route, "role_override_block_for", lambda *_a, **_kw: "**Role Override**")

    result = expert_bot_route.resolve_fixed_expert_context(
        _event(union_id="on_dup"),
        routing_table=table,
        profile_home_resolver=lambda name: tmp_path / name,
        expert_id=FIXED_EXPERT,
    )

    assert isinstance(result, expert_bot_route.FixedExpertRejection)
    assert result.reason == "duplicate_union_id"


@pytest.mark.parametrize("union_id", [None, "on_missing", "on_auto_only"])
def test_fixed_context_rejects_missing_unknown_and_non_sync_union_without_fallback(
    table, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, union_id: str | None
) -> None:
    from hermes_multitenancy import expert_bot_route

    table.upsert(
        user_id="u_auto",
        profile_name="auto_profile",
        open_id="ou_auto",
        union_id="on_auto_only",
        provenance="auto",
    )
    monkeypatch.setattr(expert_bot_route, "role_override_block_for", lambda *_a, **_kw: "**Role Override**")

    result = expert_bot_route.resolve_fixed_expert_context(
        _event(sender_open_id="ou_expert_app", union_id=union_id),
        routing_table=table,
        profile_home_resolver=lambda name: tmp_path / name,
        expert_id=FIXED_EXPERT,
    )

    assert isinstance(result, expert_bot_route.FixedExpertRejection)
    assert result.reason in {"missing_union_id", "unknown_union_id"}
    assert table.lookup_by_open_id("ou_expert_app") is None
    assert table.lookup_users_by_union_id("on_auto_only") == []


def test_fixed_context_hard_rejects_audience_miss_and_accepts_audience_hit(
    table, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_multitenancy import expert_bot_route

    _sync_user(
        table,
        user_id="u_alice",
        profile_name="alice",
        open_id="ou_main_bot_alice",
        union_id="on_delivery_user",
    )
    event = _event()

    monkeypatch.setattr(expert_bot_route, "role_override_block_for", lambda *_a, **_kw: None)
    denied = expert_bot_route.resolve_fixed_expert_context(
        event,
        routing_table=table,
        profile_home_resolver=lambda name: tmp_path / "profiles" / name,
        expert_id=FIXED_EXPERT,
    )
    assert isinstance(denied, expert_bot_route.FixedExpertRejection)
    assert denied.reason == "audience_denied"

    monkeypatch.setattr(expert_bot_route, "role_override_block_for", lambda *_a, **_kw: "**Role Override** delivery")
    allowed = expert_bot_route.resolve_fixed_expert_context(
        event,
        routing_table=table,
        profile_home_resolver=lambda name: tmp_path / "profiles" / name,
        expert_id=FIXED_EXPERT,
    )
    assert isinstance(allowed, expert_bot_route.FixedExpertContext)
    assert allowed.role_override_block == "**Role Override** delivery"


@pytest.mark.asyncio
async def test_fixed_expert_slash_gate_allows_auth_via_canonical_but_denies_approve(
    table, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_multitenancy import expert_bot_route, router as router_mod

    router_mod._user_inflight_tasks.clear()
    router_mod.override_routing_table(":memory:")
    router_table = router_mod._get_routing_table()
    assert router_table is not None
    _sync_user(
        router_table,
        user_id="u_alice",
        profile_name="alice",
        open_id="ou_main_bot_alice",
        union_id="on_delivery_user",
    )
    profile_home = tmp_path / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT", FIXED_EXPERT)
    monkeypatch.setattr(router_mod, "_profile_name_to_home", lambda name: profile_home)
    monkeypatch.setattr(expert_bot_route, "role_override_block_for", lambda *_a, **_kw: "**Role Override**")

    # /auth must REACH the auth handler (not be denied) AND arrive with the
    # CANONICAL mapped identity (union_id on_delivery_user -> profile alice,
    # open_id ou_main_bot_alice), NOT the expert-app-domain sender ou_expert_app.
    auth_calls: list[dict] = []

    async def _capture_auth(**kwargs):
        auth_calls.append(kwargs)

    monkeypatch.setattr(router_mod, "_handle_auth_command", _capture_auth)

    sends: list[str] = []

    class Adapter:
        async def send(self, _chat_id, message, *, reply_to=None, metadata=None):
            sends.append(message)

    gateway = SimpleNamespace(adapters={"feishu": Adapter()})
    await router_mod.handle_async(event=_event("/auth"), gateway=gateway)
    await router_mod.handle_async(event=_event("/approve"), gateway=gateway)
    await router_mod.handle_async(event=_event("/status"), gateway=gateway)

    # /auth reached the handler exactly once, bound to the canonical profile/open_id.
    assert len(auth_calls) == 1
    assert auth_calls[0]["profile_name"] == "alice"
    assert auth_calls[0]["sender"] == "ou_main_bot_alice"
    assert auth_calls[0]["sender"] != "ou_expert_app"
    # /auth was NOT denied (no "不支持 /auth" message; /auth only appears in the
    # available-commands list of OTHER deny messages).
    assert not any("不支持 /auth" in message for message in sends)
    # /approve is still denied; /status still allowed.
    assert any("不支持 /approve" in message for message in sends)
    assert any("profile: alice" in message for message in sends)
    router_mod.override_routing_table(None)


@pytest.mark.asyncio
async def test_fixed_expert_slash_gate_denies_quick_exec_and_plugin_without_agent(
    table, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_multitenancy import expert_bot_route, router as router_mod

    router_mod._user_inflight_tasks.clear()
    router_mod.override_routing_table(":memory:")
    router_table = router_mod._get_routing_table()
    assert router_table is not None
    _sync_user(
        router_table,
        user_id="u_alice",
        profile_name="alice",
        open_id="ou_main_bot_alice",
        union_id="on_delivery_user",
    )
    profile_home = tmp_path / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT", FIXED_EXPERT)
    monkeypatch.setattr(router_mod, "_profile_name_to_home", lambda name: profile_home)
    monkeypatch.setattr(expert_bot_route, "role_override_block_for", lambda *_a, **_kw: "**Role Override**")
    monkeypatch.setattr(
        router_mod,
        "_get_pool",
        lambda: (_ for _ in ()).throw(AssertionError("agent must not be invoked")),
    )

    sends: list[str] = []

    class Adapter:
        async def send(self, _chat_id, message, *, reply_to=None, metadata=None):
            sends.append(message)

    gateway = SimpleNamespace(adapters={"feishu": Adapter()})
    await router_mod.handle_async(event=_event("/exec ls"), gateway=gateway)
    await router_mod.handle_async(event=_event("/plugin run"), gateway=gateway)
    await router_mod.handle_async(event=_event("/status"), gateway=gateway)

    assert any("只读专家入口" in message and "/exec" in message for message in sends)
    assert any("只读专家入口" in message and "/plugin" in message for message in sends)
    assert any("profile: alice" in message for message in sends)
    router_mod.override_routing_table(None)


def test_readonly_toolsets_and_env_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes_multitenancy import agent_real

    monkeypatch.setenv("HERMES_MULTITENANCY_FEISHU_EXPERT_READONLY", "1")

    assert "HERMES_MULTITENANCY_FEISHU_EXPERT_READONLY" in agent_real._SUBPROCESS_ENV_ALLOWLIST
    assert agent_real._resolve_enabled_toolsets(
        {
            "platform_toolsets": {
                "webui": ["delegation", "execute_code", "file", "lark-cli", "terminal"]
            }
        },
        "webui",
        platform_tools_resolver=lambda *_a, **_kw: ["terminal", "web"],
    ) == ["file", "lark-cli", "web"]

    disabled = agent_real._resolve_disabled_toolsets({"agent": {"disabled_toolsets": ["browser"]}})
    assert disabled == ["browser", "delegation", "execute_code", "terminal"]


def _tool_json(payload: str) -> dict:
    data = json.loads(payload)
    assert isinstance(data, dict)
    return data


def test_readonly_lark_cli_denies_mutations_before_binary_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes_multitenancy import lark_cli_tool

    monkeypatch.setenv("HERMES_MULTITENANCY_FEISHU_EXPERT_READONLY", "1")
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", "/definitely/not/spawned")

    for argv in (
        ["api", "POST", "/open-apis/im/v1/messages"],
        ["PATCH", "/open-apis/contact/v3/users/ou_x"],
        ["DELETE", "/open-apis/drive/v1/files/f"],
    ):
        out = _tool_json(
            lark_cli_tool._handle_lark_cli_execute(
                {"mode": "api", "argv": argv, "risk": "read", "reason": "test"}
            )
        )
        assert out.get("ok", False) is False
        assert "read-only" in out["error"]

    shortcut = _tool_json(
        lark_cli_tool._handle_lark_cli_execute(
            {
                "mode": "shortcut",
                "argv": ["im", "+messages-send", "--chat-id", "oc_x"],
                "risk": "read",
                "reason": "test",
            }
        )
    )
    assert shortcut.get("ok", False) is False
    assert "read-only" in shortcut["error"]


def test_readonly_lark_cli_denies_unlisted_gets_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_multitenancy import lark_cli_tool

    binary = tmp_path / "lark-cli"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'argv': sys.argv[1:]}))\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    profile_home = tmp_path / "profile"
    workspace = profile_home / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HERMES_MULTITENANCY_FEISHU_EXPERT_READONLY", "1")
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_PROFILE", str(profile_home))
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", "ou_user")
    monkeypatch.setenv("LARKSUITE_CLI_AUTH_PROXY", "http://127.0.0.1:16384")
    monkeypatch.setenv("LARKSUITE_CLI_PROXY_KEY", "per-run-proxy-key")
    monkeypatch.setenv("LARKSUITE_CLI_APP_ID", "cli_public")

    for argv in (
        ["GET", "/open-apis/admin/v1/dangerous-read"],
        ["GET", "/open-apis/approval/v4/instances"],
    ):
        out = _tool_json(
            lark_cli_tool._handle_lark_cli_execute(
                {"mode": "api", "argv": argv, "risk": "read", "reason": "test"}
            )
        )
        assert out.get("ok", False) is False
        assert "read-only" in out["error"]

    allowed = _tool_json(
        lark_cli_tool._handle_lark_cli_execute(
            {
                "mode": "api",
                "argv": ["GET", "/open-apis/im/v1/messages"],
                "identity": "user",
                "risk": "read",
                "reason": "test",
            }
        )
    )
    assert allowed["ok"] is True


def test_readonly_lark_cli_allows_get_read_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_multitenancy import lark_cli_tool

    binary = tmp_path / "lark-cli"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'argv': sys.argv[1:]}))\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    profile_home = tmp_path / "profile"
    workspace = profile_home / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HERMES_MULTITENANCY_FEISHU_EXPERT_READONLY", "1")
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_PROFILE", str(profile_home))
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("LARKSUITE_CLI_AUTH_PROXY", "http://127.0.0.1:16384")
    monkeypatch.setenv("LARKSUITE_CLI_PROXY_KEY", "per-run-proxy-key")
    monkeypatch.setenv("LARKSUITE_CLI_APP_ID", "cli_public")

    out = _tool_json(
        lark_cli_tool._handle_lark_cli_execute(
            {
                "mode": "api",
                "argv": ["GET", "/open-apis/contact/v3/users"],
                "risk": "read",
                "reason": "test",
            }
        )
    )

    assert out["ok"] is True
    assert out["json"]["argv"][:3] == ["api", "GET", "/open-apis/contact/v3/users"]


def test_readonly_lark_cli_allows_bitable_read_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_multitenancy import lark_cli_tool

    binary = tmp_path / "lark-cli"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'argv': sys.argv[1:]}))\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    profile_home = tmp_path / "profile"
    workspace = profile_home / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HERMES_MULTITENANCY_FEISHU_EXPERT_READONLY", "1")
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_PROFILE", str(profile_home))
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("LARKSUITE_CLI_AUTH_PROXY", "http://127.0.0.1:16384")
    monkeypatch.setenv("LARKSUITE_CLI_PROXY_KEY", "per-run-proxy-key")
    monkeypatch.setenv("LARKSUITE_CLI_APP_ID", "cli_public")

    out = _tool_json(
        lark_cli_tool._handle_lark_cli_execute(
            {
                "mode": "api",
                "argv": ["GET", "/open-apis/bitable/v1/apps/app_token/tables"],
                "risk": "read",
                "reason": "test",
            }
        )
    )

    assert out["ok"] is True
    assert out["json"]["argv"][:3] == ["api", "GET", "/open-apis/bitable/v1/apps/app_token/tables"]


def test_readonly_kep_shim_denies_writes_and_allows_status(tmp_path: Path) -> None:
    from hermes_multitenancy.kep_cli_guard import install_kep_cli_shim

    real_bin = tmp_path / "real" / "hades-cli"
    real_bin.parent.mkdir(parents=True)
    real_bin.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    real_bin.chmod(0o755)
    auth_bin = tmp_path / "real" / "kep-auth"
    auth_bin.write_text("#!/bin/sh\nprintf 'header.payload.signature\\n'\n", encoding="utf-8")
    auth_bin.chmod(0o755)
    identity_url = "data:application/json," + urllib.parse.quote(json.dumps({
        "errorCode": 0,
        "ok": True,
        "data": {"payload": {"name": "alice", "exp": 4_102_444_800}},
    }))
    shim_dir = tmp_path / "shim"
    [wrapper] = install_kep_cli_shim(
        shim_dir,
        real_bins={"hades-cli": str(real_bin)},
        identity_urls={"online": identity_url, "pre": identity_url},
    )

    env = os.environ.copy()
    env["HERMES_MULTITENANCY_FEISHU_EXPERT_READONLY"] = "1"
    env["KEP_PROFILE"] = "alice"
    env["HERMES_KEP_CLI_REAL_BIN_KEP_AUTH"] = str(auth_bin)

    denied = subprocess.run(
        [str(wrapper), "create", "--name", "x"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert denied.returncode == 126
    assert "read-only" in denied.stderr

    allowed = subprocess.run(
        [str(wrapper), "status"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert allowed.returncode == 0
    assert json.loads(allowed.stdout) == ["--profile", "alice", "status"]


def test_env_unset_regression_paths_are_inert(
    table, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_multitenancy import agent_real, expert_bot_route, router as router_mod

    monkeypatch.delenv("HERMES_MULTITENANCY_FIXED_EXPERT", raising=False)
    monkeypatch.delenv("HERMES_MULTITENANCY_FEISHU_EXPERT_READONLY", raising=False)
    assert expert_bot_route.fixed_expert_id_from_env() == ""

    _sync_user(table, user_id="u1", profile_name="alice", open_id="ou_a", union_id="on_a")
    assert [row.open_id for row in table.lookup_users_by_union_id("on_a")] == ["ou_a"]

    router_mod.override_routing_table(":memory:")
    router_table = router_mod._get_routing_table()
    assert router_table is not None
    _sync_user(router_table, user_id="u2", profile_name="bob", open_id="ou_b", union_id="on_b")
    monkeypatch.setattr(router_mod, "_profile_name_to_home", lambda name: tmp_path / name)
    assert router_mod._resolve_route("missing", alt_id="on_b") == ("bob", tmp_path / "bob")

    assert agent_real._resolve_enabled_toolsets(
        {"platform_toolsets": {"webui": ["lark-cli"]}},
        "webui",
        platform_tools_resolver=None,
    ) == ["file", "lark-cli", "terminal", "web"]
    assert agent_real._resolve_disabled_toolsets({"agent": {"disabled_toolsets": ["browser"]}}) == ["browser"]
    router_mod.override_routing_table(None)
