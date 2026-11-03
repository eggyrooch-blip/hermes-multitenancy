import asyncio
import concurrent.futures
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _probe(user_id: int, login: str):
    return lambda _token: {"id": user_id, "login": login}


def test_github_bundle_connects_exact_owner_and_installs_secretless_mcp_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from hermes_multitenancy.github_mcp_connector import connect, status

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model:\n  default: openai/test\n", encoding="utf-8")

    result = connect(
        shared_home=tmp_path,
        profile_name="alice",
        subject_id="ou_alice",
        token="github_pat_super_secret",
        probe=_probe(101, "octo-alice"),
    )

    assert result == {"ok": True, "account_hint": "octo…ice"}
    assert status(tmp_path, "alice", "ou_alice").status == "authenticated"
    assert status(tmp_path, "alice", "ou_bob").status == "needs_auth"
    skill = (profile / "skills" / "github-mcp" / "SKILL.md").read_text(encoding="utf-8")
    assert "requires_connectors: [github-mcp]" in skill
    config = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    server = config["mcp_servers"]["hermes-connectors"]
    assert Path(server["command"]).resolve() == Path(sys.executable).resolve()
    assert server["args"] == ["-m", "hermes_multitenancy.connector_mcp_stdio"]
    assert server["env"] == {
        "HERMES_RUN_BROKER_URL": "${HERMES_RUN_BROKER_URL}",
        "HERMES_MULTITENANCY_RUN_BROKER_URL": "${HERMES_MULTITENANCY_RUN_BROKER_URL}",
        "HERMES_RUN_BROKER_KEY": "${HERMES_RUN_BROKER_KEY}",
    }
    assert "github_pat_super_secret" not in (profile / "config.yaml").read_text(encoding="utf-8")
    raw_db = (tmp_path / "multitenancy.db").read_bytes()
    assert b"github_pat_super_secret" not in raw_db
    subprocess.run(
        [server["command"], "-c", "import hermes_multitenancy._import_smoke"],
        check=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])},
    )
    subprocess.run(
        [
            server["command"],
            "-c",
            (
                "from hermes_cli.config import load_config; "
                "e=load_config()['mcp_servers']['hermes-connectors']['env']; "
                "assert e['HERMES_RUN_BROKER_URL']=='http://127.0.0.1:9991'; "
                "assert e['HERMES_RUN_BROKER_KEY']=='run-scope-only'"
            ),
        ],
        check=True,
        env={
            **os.environ,
            "HERMES_HOME": str(profile),
            "HERMES_RUN_BROKER_URL": "http://127.0.0.1:9991",
            "HERMES_RUN_BROKER_KEY": "run-scope-only",
        },
    )


def test_github_account_cannot_bind_to_a_second_multitenancy_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from hermes_multitenancy.github_mcp_connector import ConnectorUnavailable, connect, status

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    for profile in ("alice", "bob"):
        (tmp_path / "profiles" / profile).mkdir(parents=True)
    connect(tmp_path, "alice", "ou_alice", "github_pat_a", probe=_probe(101, "octo-alice"))

    with pytest.raises(ConnectorUnavailable, match="already bound"):
        connect(tmp_path, "bob", "ou_bob", "github_pat_same_account", probe=_probe(101, "octo-alice"))

    assert status(tmp_path, "alice", "ou_alice").status == "authenticated"
    assert status(tmp_path, "bob", "ou_bob").status == "needs_auth"


def test_concurrent_same_github_account_bind_commits_for_only_one_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from hermes_multitenancy.github_mcp_connector import ConnectorUnavailable, connect

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    for profile in ("alice", "bob"):
        (tmp_path / "profiles" / profile).mkdir(parents=True)

    def bind(owner: tuple[str, str, str]):
        profile, subject, token = owner
        try:
            connect(tmp_path, profile, subject, token, probe=_probe(101, "octo-alice"))
            return "stored"
        except ConnectorUnavailable as exc:
            return str(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(bind, [
            ("alice", "ou_alice", "github_pat_a"),
            ("bob", "ou_bob", "github_pat_b"),
        ]))

    assert results.count("stored") == 1
    assert sum("already bound" in result for result in results) == 1


@pytest.mark.parametrize("token", ["x" * 513, "github_pat_has space", "github_pat_line\nbreak"])
def test_github_token_shape_is_bounded_before_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str
):
    from hermes_multitenancy.github_mcp_connector import ConnectorUnavailable, connect

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    (tmp_path / "profiles" / "alice").mkdir(parents=True)
    called = []
    with pytest.raises(ConnectorUnavailable, match="malformed"):
        connect(tmp_path, "alice", "ou_alice", token, probe=lambda value: called.append(value) or {})
    assert called == []


def test_github_runtime_is_owner_exact_live_verified_and_allowlisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from hermes_multitenancy.github_mcp_connector import (
        ConnectorUnavailable,
        call_tool,
        connect,
        list_tools,
    )

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    (tmp_path / "profiles" / "alice").mkdir(parents=True)
    connect(tmp_path, "alice", "ou_alice", "github_pat_a", probe=_probe(101, "octo-alice"))

    upstream_calls = []

    async def remote_list(token: str):
        upstream_calls.append(("list", token))
        return [
            {"name": "get_me", "description": "current user", "inputSchema": {"type": "object"}},
            {"name": "create_issue", "description": "write", "inputSchema": {"type": "object"}},
        ]

    tools = asyncio.run(list_tools(
        tmp_path, "alice", "ou_alice", probe=_probe(101, "octo-alice"), remote_list=remote_list
    ))
    assert [tool["name"] for tool in tools] == ["get_me"]
    assert upstream_calls == [("list", "github_pat_a")]

    with pytest.raises(PermissionError, match="not allowlisted"):
        asyncio.run(call_tool(
            tmp_path, "alice", "ou_alice", "create_issue", {},
            probe=_probe(101, "octo-alice"), remote_call=lambda *_args: None,
        ))
    assert upstream_calls == [("list", "github_pat_a")]

    with pytest.raises(ConnectorUnavailable, match="credential not found"):
        asyncio.run(list_tools(
            tmp_path, "alice", "ou_bob", probe=_probe(202, "octo-bob"), remote_list=remote_list
        ))
    with pytest.raises(ConnectorUnavailable, match="owner changed"):
        asyncio.run(list_tools(
            tmp_path, "alice", "ou_alice", probe=_probe(202, "octo-mallory"), remote_list=remote_list
        ))
    assert upstream_calls == [("list", "github_pat_a")]


def test_github_revoke_deletes_only_the_exact_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from hermes_multitenancy.github_mcp_connector import connect, revoke, status

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    (tmp_path / "profiles" / "alice").mkdir(parents=True)
    connect(tmp_path, "alice", "ou_alice", "github_pat_a", probe=_probe(101, "octo-alice"))
    assert revoke(tmp_path, "alice", "ou_bob") is False
    assert status(tmp_path, "alice", "ou_alice").status == "authenticated"
    assert revoke(tmp_path, "alice", "ou_alice") is True
    assert status(tmp_path, "alice", "ou_alice").status == "needs_auth"


def test_github_mcp_data_plane_requires_and_uses_run_scoped_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy import github_mcp_connector as github
    from hermes_multitenancy import webui_broker_server as broker

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "master")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(tmp_path))
    seen = []

    async def fake_list(shared_home, profile_name, subject_id):
        seen.append((str(shared_home), profile_name, subject_id))
        return [{"name": "get_me", "description": "me", "inputSchema": {"type": "object"}}]

    monkeypatch.setattr(github, "list_tools", fake_list)
    upstream_calls = []

    async def unexpected_runtime(*_args, **_kwargs):
        upstream_calls.append("called")
        raise AssertionError("write tool reached credential/upstream path")

    monkeypatch.setattr(github, "_runtime_token", unexpected_runtime)
    broker.register_run_broker_scoped_token(
        token="run-a", profile_name="alice", open_id="ou_alice", run_id="run-1"
    )

    async def run():
        client = TestClient(TestServer(broker.create_run_broker_app()))
        await client.start_server()
        try:
            good = await client.get(
                "/api/run-broker/connectors/github-mcp/tools",
                headers={"Authorization": "Bearer run-a"},
            )
            master = await client.get(
                "/api/run-broker/connectors/github-mcp/tools",
                headers={"Authorization": "Bearer master"},
            )
            forged = await client.get(
                "/api/run-broker/connectors/github-mcp/tools?profile_name=bob",
                headers={"Authorization": "Bearer run-a"},
            )
            blocked_write = await client.post(
                "/api/run-broker/connectors/github-mcp/call",
                headers={"Authorization": "Bearer run-a"},
                json={"name": "create_issue", "arguments": {}},
            )
            return good.status, await good.json(), master.status, forged.status, blocked_write.status
        finally:
            await client.close()

    try:
        good_status, body, master_status, forged_status, blocked_write_status = asyncio.run(run())
    finally:
        broker.unregister_run_broker_scoped_token("run-a")

    assert good_status == 200
    assert body["tools"][0]["name"] == "get_me"
    assert seen == [(str(tmp_path), "alice", "ou_alice")]
    assert master_status == 403
    assert forged_status == 403
    assert blocked_write_status == 403
    assert upstream_calls == []


def test_github_credential_endpoint_uses_trusted_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy import github_mcp_connector as github
    from hermes_multitenancy import webui_broker_server as broker

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "master")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(tmp_path))
    monkeypatch.setattr(broker, "_resolve_owner_scoped_profile", lambda _request, payload: ("alice", None))
    seen = []
    monkeypatch.setattr(
        github,
        "connect",
        lambda shared, profile, subject, token: (
            seen.append(("connect", str(shared), profile, subject, token))
            or {"ok": True, "account_hint": "octo…ice"}
        ),
    )
    monkeypatch.setattr(
        github,
        "revoke",
        lambda shared, profile, subject: (
            seen.append(("revoke", str(shared), profile, subject)) or True
        ),
    )

    async def run():
        client = TestClient(TestServer(broker.create_run_broker_app()))
        await client.start_server()
        try:
            headers = {
                "Authorization": "Bearer master",
                "X-Hermes-Owner-Open-Id": "ou_alice",
            }
            connected = await client.post(
                "/api/run-broker/credentials/github",
                headers=headers,
                json={"token": "github_pat_x", "open_id": "ou_victim", "profile_name": "victim"},
            )
            revoked = await client.delete("/api/run-broker/credentials/github", headers=headers)
            no_owner = await client.post(
                "/api/run-broker/credentials/github",
                headers={"Authorization": "Bearer master"},
                json={"token": "github_pat_x", "open_id": "ou_victim"},
            )
            return connected.status, await connected.text(), revoked.status, no_owner.status
        finally:
            await client.close()

    connected_status, body, revoked_status, no_owner_status = asyncio.run(run())
    assert connected_status == 200 and revoked_status == 200
    assert "github_pat_x" not in body
    assert seen == [
        ("connect", str(tmp_path), "alice", "ou_alice", "github_pat_x"),
        ("revoke", str(tmp_path), "alice", "ou_alice"),
    ]
    assert no_owner_status == 403


def test_stdio_shim_exposes_broker_tools_without_a_github_secret_in_child_env():
    from aiohttp import web
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        async def tools(request):
            assert request.headers["Authorization"] == "Bearer run-only"
            return web.json_response({
                "tools": [{"name": "get_me", "description": "me", "inputSchema": {"type": "object"}}]
            })

        app = web.Application()
        app.router.add_get("/api/run-broker/connectors/github-mcp/tools", tools)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        env = {
            **os.environ,
            "PYTHONPATH": str(Path(__file__).parents[1]),
            "HERMES_RUN_BROKER_URL": f"http://127.0.0.1:{port}",
            "HERMES_RUN_BROKER_KEY": "run-only",
        }
        for key in list(env):
            if key.startswith("GITHUB_"):
                env.pop(key)
        try:
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "hermes_multitenancy.connector_mcp_stdio"],
                env=env,
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return [tool.name for tool in result.tools]
        finally:
            await runner.cleanup()

    assert asyncio.run(run()) == ["get_me"]


def test_mcp_oauth_approval_uses_only_the_trusted_webui_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from aiohttp.test_utils import TestClient, TestServer
    from mcp.server.auth.provider import AuthorizationParams
    from mcp.shared.auth import OAuthClientInformationFull

    from hermes_multitenancy import webui_broker_server as broker
    from hermes_multitenancy.connector_client_auth import ClientTokenStore, HermesOAuthProvider

    issuer = "http://127.0.0.1:8767"
    resource = f"{issuer}/mcp"
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "master")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_MCP_PUBLIC_ORIGIN", issuer)
    monkeypatch.setattr(broker, "_resolve_owner_scoped_profile", lambda _request, _payload: ("alice", None))

    token_store = ClientTokenStore(tmp_path / "multitenancy.db", issuer=issuer, resource=resource)
    provider = HermesOAuthProvider(token_store)
    client_info = OAuthClientInformationFull(
        client_id="cursor-local",
        redirect_uris=["http://127.0.0.1:7777/callback"],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="mcp:tools",
    )
    asyncio.run(provider.register_client(client_info))
    approval_url = asyncio.run(provider.authorize(
        client_info,
        AuthorizationParams(
            state="client-state",
            scopes=["mcp:tools"],
            code_challenge="challenge",
            redirect_uri="http://127.0.0.1:7777/callback",
            redirect_uri_provided_explicitly=True,
            resource=resource,
        ),
    ))
    request_id = approval_url.split("request_id=", 1)[1]
    assert provider.pending_request(request_id) == {
        "client_id": "cursor-local",
        "client_name": "cursor-local",
        "redirect_origin": "http://127.0.0.1:7777",
        "scopes": ["mcp:tools"],
    }
    assert provider._conn is token_store._conn
    provider.close()
    token_store.close()

    async def run():
        client = TestClient(TestServer(broker.create_run_broker_app()))
        await client.start_server()
        try:
            described = await client.get(
                "/api/run-broker/connectors/mcp-oauth/requests/" + request_id,
                headers={
                    "Authorization": "Bearer master",
                    "X-Hermes-Owner-Open-Id": "subject-alice",
                },
            )
            approved = await client.post(
                "/api/run-broker/connectors/mcp-oauth/approve",
                headers={
                    "Authorization": "Bearer master",
                    "X-Hermes-Owner-Open-Id": "subject-alice",
                },
                json={"request_id": request_id, "profile_name": "victim", "subject_id": "victim"},
            )
            denied = await client.post(
                "/api/run-broker/connectors/mcp-oauth/approve",
                headers={"Authorization": "Bearer master"},
                json={"request_id": request_id},
            )
            return described.status, await described.json(), approved.status, await approved.json(), denied.status
        finally:
            await client.close()

    described_status, described_body, status, body, denied_status = asyncio.run(run())
    assert described_status == 200
    assert described_body == {
        "client_id": "cursor-local",
        "client_name": "cursor-local",
        "redirect_origin": "http://127.0.0.1:7777",
        "scopes": ["mcp:tools"],
    }
    assert status == 200
    assert body["redirect_url"].startswith("http://127.0.0.1:7777/callback?")
    assert "client-state" in body["redirect_url"]
    assert denied_status == 403
