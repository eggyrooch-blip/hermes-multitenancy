import json
import socket
from pathlib import Path

import pytest


def _public_dns(_host, port, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def _private_dns(_host, port, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]


def test_bundled_catalog_is_complete_and_icons_are_confined():
    from hermes_multitenancy.connector_custom_catalog import ConnectorCatalog
    from hermes_multitenancy.connector_catalog_api import _catalog_action

    catalog = ConnectorCatalog.bundled()
    assert len(catalog.list_rows()) == 642
    assert len(catalog.list_canonical()) == 330
    assert all(_catalog_action(row)["available"] for row in catalog.list_rows())
    assert not {
        row["final_verdict"] for row in catalog.list_rows()
    } & {"incompatible", "rejected"}
    assert all(not ({"incompatible", "rejected"} & set(row["verdicts"])) for row in catalog.list_canonical())
    assert all(
        _catalog_action(row)["available"]
        for row in catalog.list_rows()
        if row["final_verdict"] == "needs_auth"
    )
    assert all(catalog.icon_path(row["row_key"]).is_file() for row in catalog.list_rows())
    with pytest.raises(KeyError):
        catalog.icon_path("../../etc/passwd")


def test_feishu_catalog_card_reports_ready_only_from_the_registered_owner_broker():
    from hermes_multitenancy.connector_catalog_api import _public_catalog_row
    from hermes_multitenancy.connector_custom_catalog import ConnectorCatalog

    row = ConnectorCatalog.bundled().get("workbuddy:feishu")
    pending = _public_catalog_row(row, feishu_ready=False)
    ready = _public_catalog_row(row, feishu_ready=True)
    assert pending["action"]["auth_flow"] == "feishu_device_flow"
    assert pending["action"]["status"] == "needs_auth"
    assert ready["action"]["kind"] == "revoke"
    assert ready["action"]["status"] == "ready"


def test_catalog_oauth_tenant_endpoint_is_label_and_path_segment_anchored():
    from hermes_multitenancy.connector_catalog_api import _oauth_row_for_request

    row = {
        "endpoint": "https://adapter.invalid/mcp",
        "remote_recovery": {
            "endpoint_field": "MCP_SERVER_URL",
            "endpoint_host_suffix": "atlassian.net",
            "endpoint_path_prefix": "/mcp",
        },
    }
    assert _oauth_row_for_request(row, {
        "fields": {"MCP_SERVER_URL": "https://tenant.atlassian.net/mcp/"},
    })["endpoint"] == "https://tenant.atlassian.net/mcp/"
    for endpoint in ("https://evil-atlassian.net/mcp", "https://tenant.atlassian.net/mcpevil"):
        with pytest.raises(ValueError, match="official tenant domain"):
            _oauth_row_for_request(row, {"fields": {"MCP_SERVER_URL": endpoint}})


def test_catalog_verification_calls_one_zero_input_readonly_tool(monkeypatch, tmp_path: Path):
    import asyncio

    from hermes_multitenancy import connector_catalog_api

    calls = []

    class Runtime:
        def __init__(self, _path):
            pass

        async def list_connector_tools(self, profile, subject, connector):
            return [
                {"name": f"{connector}__needs_arg", "inputSchema": {"type": "object", "required": ["q"]}},
                {"name": f"{connector}__health", "inputSchema": {"type": "object"}},
            ]

        async def call_tool(self, profile, subject, name, arguments):
            calls.append((profile, subject, name, arguments))
            return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(connector_catalog_api, "CustomConnectorRuntime", Runtime)
    assert asyncio.run(connector_catalog_api._verify_catalog_connection(
        tmp_path / "db", "alice", "subject-alice", "connector-1"
    )) == 2
    assert calls == [("alice", "subject-alice", "connector-1__health", {})]


def test_feishu_revoke_deletes_only_the_exact_routed_owner(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.feishu_uat_auth import revoke_uat_credential
    from hermes_multitenancy.routing import RoutingTable

    shared = tmp_path / "shared"
    db = shared / "multitenancy.db"
    table = RoutingTable(db)
    table.upsert(user_id="alice", profile_name="alice-profile", open_id="ou_alice", provenance="sync")
    table.upsert(user_id="bob", profile_name="bob-profile", open_id="ou_bob", provenance="sync")
    table.close()
    monkeypatch.setenv("HERMES_CREDENTIAL_KEY", "test-key")
    store = CredentialStore(db, encryption_key="test-key")
    for profile, subject in (("alice-profile", "ou_alice"), ("bob-profile", "ou_bob")):
        store.put_credential(
            profile_name=profile, subject_id=subject, provider="feishu", secret_kind="uat",
            payload={"access_token": "secret", "refresh_token": "refresh"},
        )
    store.close()
    legacy = shared / "profiles" / "alice-profile" / "feishu_uat" / "ou_alice.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"access_token":"legacy"}', encoding="utf-8")

    assert revoke_uat_credential(
        profile_name="alice-profile", open_id="ou_alice", shared_home=shared
    ) is True
    assert not legacy.exists()
    store = CredentialStore(db, encryption_key="test-key")
    assert store.get_status(
        profile_name="alice-profile", subject_id="ou_alice", provider="feishu", secret_kind="uat"
    )["status"] == "missing"
    assert store.get_status(
        profile_name="bob-profile", subject_id="ou_bob", provider="feishu", secret_kind="uat"
    )["status"] == "valid"
    store.close()
    with pytest.raises(Exception, match="not bound"):
        revoke_uat_credential(profile_name="bob-profile", open_id="ou_alice", shared_home=shared)


def test_cli_authorization_session_expires_and_removes_its_env(monkeypatch, tmp_path: Path):
    import asyncio

    from hermes_multitenancy import connector_catalog_api

    stopped = []

    async def stop(process, env_path, unit):
        stopped.append((process, env_path, unit))

    monkeypatch.setattr(connector_catalog_api, "stop_cli_auth", stop)
    connector_catalog_api._cli_auth_sessions.clear()
    connector_catalog_api._cli_auth_sessions["connector-1"] = (
        object(), tmp_path / "owner.env", "unit-1", 10.0,
    )
    asyncio.run(connector_catalog_api._prune_cli_sessions(now=10.0))
    assert connector_catalog_api._cli_auth_sessions == {}
    assert len(stopped) == 1 and stopped[0][1:] == (tmp_path / "owner.env", "unit-1")


def test_bundled_stdio_manifests_preserve_each_official_package_identity():
    from collections import Counter

    from hermes_multitenancy.connector_custom_catalog import ConnectorCatalog

    rows = [row for row in ConnectorCatalog.bundled().list_rows() if row["transport"] == "stdio"]
    assert len(rows) == 482
    assert all(row.get("runtime_manifest") for row in rows)
    assert Counter(row["runtime_manifest"]["state"] for row in rows) == {
        "direct": 363,
        "repository_only": 119,
    }

    edgeone = next(row for row in rows if row["row_key"] == "workbuddy:edgeone-pages")
    assert edgeone["runtime_manifest"]["command"] == "npx"
    assert edgeone["runtime_manifest"]["args"] == [
        "edgeone-pages-mcp-fullstack@latest", "--region", "china"
    ]

    firecrawl = next(
        row for row in rows if row["row_key"] == "trae solo cn:mendableai.firecrawl-mcp-server"
    )
    assert firecrawl["runtime_manifest"]["command"] == "npx"
    assert firecrawl["runtime_manifest"]["args"] == ["-y", "firecrawl-mcp"]
    assert "your-api-key" not in json.dumps(firecrawl)

    ida = next(row for row in rows if row["row_key"] == "trae solo cn:byted-mcp.ida-pro-mcp")
    assert ida["runtime_manifest"]["state"] == "direct"
    assert ida["runtime_manifest"]["source_url"] == "https://github.com/mrexodia/ida-pro-mcp"
    assert ida["runtime_manifest"]["package_resolution"]["dependency_lock"]["state"] == "resolved"


def test_bundled_npm_resolutions_pin_version_integrity_and_executable():
    from collections import Counter

    path = Path(__file__).parents[1] / "hermes_multitenancy" / "connector_catalog_data" / "stdio_npm_resolutions.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 171
    assert Counter(row["state"] for row in rows) == {
        "resolved": 166, "git_resolved": 4, "package_unavailable": 1,
    }
    resolved = [row for row in rows if row["state"] == "resolved"]
    assert all(row["version"] and "latest" not in row["version"] for row in resolved)
    assert all(row["integrity"].startswith("sha512-") for row in resolved)
    assert all(row["bin"] for row in resolved)
    assert all(
        len(row["commit"]) == 40 and row["dependency_lock"]["state"] == "resolved"
        for row in rows if row["state"] == "git_resolved"
    )

    trello = next(row for row in rows if row["row_key"] == "trae solo cn:delorenj.mcp-server-trello")
    assert trello["package"] == "@delorenj/mcp-server-trello"
    assert trello["version"] == "1.8.1"
    desktop = next(
        row for row in rows if row["row_key"] == "trae solo cn:wonderwhy-er.claudecomputercommander"
    )
    assert desktop["package"] == "@wonderwhy-er/desktop-commander"
    locks_path = Path(__file__).parents[1] / "hermes_multitenancy" / "connector_catalog_data" / "stdio_npm_locks.jsonl"
    locks = [json.loads(line) for line in locks_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(locks) == 117
    assert sum(lock["state"] == "resolved" for lock in locks) == 117
    assert all(
        lock["package_lock_sha256"] and '"integrity"' in lock["package_lock"]
        for lock in locks if lock["state"] == "resolved"
    )


def test_resolved_safe_npm_catalog_rows_are_connectable_and_owner_bound(tmp_path: Path):
    from hermes_multitenancy.connector_catalog_api import _catalog_action
    from hermes_multitenancy.connector_custom_catalog import ConnectorCatalog, CustomConnectorStore

    rows = ConnectorCatalog.bundled().list_rows()
    actions = [(row, _catalog_action(row)) for row in rows]
    installable = [
        (row, action) for row, action in actions
        if action["kind"] == "install_sandbox" and action["available"]
    ]
    assert len(installable) == 360
    assert all(action["available"] for _row, action in installable)

    row, action = next(
        (row, action) for row, action in installable
        if row["row_key"] == "trae solo cn:byted-mcp-volcengine.brave_search"
    )
    assert action["fields"] == ["BRAVE_API_KEY"]
    python_row, python_action = next(
        (row, action) for row, action in installable
        if row["row_key"] == "trae solo cn:byted-mcp-volcengine.time"
    )
    assert python_row["runtime_manifest"]["package_resolution"]["dependency_lock"]["state"] == "resolved"
    assert python_action["fields"] == []
    assert any(row["row_key"] == "trae solo cn:mendableai.firecrawl-mcp-server" for row, _ in installable)
    bigquery = next(
        row for row, _action in installable
        if row["row_key"] == "trae solo cn:ergut.mcp-bigquery-server"
    )
    assert bigquery["runtime_manifest"]["configs"][-1] == {
        "key": "GOOGLE_SERVICE_ACCOUNT_JSON",
        "path": "credentials/google-service-account.json",
        "required": True,
        "secret": True,
        "type": "file",
    }
    for row_key in (
        "trae solo cn:byted-mcp-volcengine.google_calendar",
        "trae solo cn:byted-mcp-volcengine.google_tasks",
    ):
        google = next(row for row, _action in installable if row["row_key"] == row_key)
        assert google["runtime_manifest"]["configs"] == [{
            "key": "GOOGLE_OAUTH_CREDENTIALS_JSON",
            "path": "credentials/google-oauth.json",
            "required": True,
            "secret": True,
            "type": "file",
        }]
        assert google["runtime_manifest"]["static_env"]["GOOGLE_OAUTH_CREDENTIALS"] == (
            "/home/connector/credentials/google-oauth.json"
        )

    db = tmp_path / "multitenancy.db"
    store = CustomConnectorStore(db, encryption_key="test-key", resolver=_public_dns)
    installed = store.install_catalog_stdio(
        "alice",
        "subject-alice",
        name=action["installation_name"],
        row_key=row["row_key"],
        runtime_manifest=row["runtime_manifest"],
        fields={"BRAVE_API_KEY": "owner-secret"},
    )
    assert installed["transport"] == "stdio"
    assert installed["credential_fields"] == ["BRAVE_API_KEY"]
    assert "runtime_manifest" not in installed
    assert "owner-secret" not in db.read_text(errors="ignore")
    assert store.list_installations("bob", "subject-bob") == []
    with pytest.raises(PermissionError):
        store.get_runtime("bob", "subject-bob", installed["connector_id"])
    runtime = store.get_runtime("alice", "subject-alice", installed["connector_id"])
    assert runtime["environment"] == {"BRAVE_API_KEY": "owner-secret"}
    assert runtime["runtime_manifest"]["package_resolution"]["version"]
    assert "owner-secret" not in json.dumps(runtime["runtime_manifest"])
    store.delete("alice", "subject-alice", installed["connector_id"])
    with pytest.raises(PermissionError):
        store.get_runtime("alice", "subject-alice", installed["connector_id"])
    store.close()


def test_workbuddy_cli_packages_are_pinned_and_require_owner_isolated_auth(tmp_path: Path):
    import asyncio

    from hermes_multitenancy.connector_catalog_api import _catalog_action
    from hermes_multitenancy.connector_cli_runtime import catalog_cli_spec, prepare_cli_runtime
    from hermes_multitenancy.connector_custom_catalog import ConnectorCatalog, CustomConnectorStore

    catalog = ConnectorCatalog.bundled()
    admitted = [row for row in catalog.list_rows() if _catalog_action(row)["kind"] == "authorize_cli"]
    assert len(admitted) == 21
    assert all(
        spec["resolution"].get("state") in {"embedded", "pinned_archive"}
        or spec["resolution"]["dependency_lock"]["state"] == "resolved"
        for row in admitted if (spec := catalog_cli_spec(row["cli_manifest"]))
    )
    feishu = catalog.get("workbuddy:feishu")
    assert _catalog_action(feishu)["auth_flow"] == "feishu_device_flow"
    with pytest.raises(ValueError):
        catalog_cli_spec(feishu["cli_manifest"])

    embedded = catalog.get("workbuddy:tc-chengxin")
    assert _catalog_action(embedded)["kind"] == "authorize_cli"
    assert catalog_cli_spec(embedded["cli_manifest"])["resolution"]["state"] == "embedded"
    runtime_root = asyncio.run(prepare_cli_runtime(tmp_path / "runtimes", embedded["cli_manifest"]))
    assert (runtime_root / "node_modules/@tongcheng/tc-chengxin-cli/bin/cli.js").is_file()
    assert catalog_cli_spec(catalog.get("workbuddy:77ircloud")["cli_manifest"])["resolution"]["state"] == "pinned_archive"
    assert catalog_cli_spec(catalog.get("workbuddy:wps-knowledgebase")["cli_manifest"])["resolution"]["state"] == "pinned_archive"

    row = catalog.get("workbuddy:tmeet")
    store = CustomConnectorStore(tmp_path / "catalog.db", encryption_key="test-key")
    installed = store.install_catalog_cli(
        "alice", "subject-alice", name="catalog-tmeet", row_key=row["row_key"],
        runtime_manifest=row["cli_manifest"],
    )
    runtime = store.get_runtime("alice", "subject-alice", installed["connector_id"])
    assert runtime["transport"] == "cli"
    assert runtime["runtime_manifest"]["package_resolution"]["version"] == "1.0.15"
    with pytest.raises(PermissionError):
        store.get_runtime("bob", "subject-bob", installed["connector_id"])
    store.close()


def test_public_replacements_recover_axiom_mem0_glean_neo4j_and_markitdown():
    from hermes_multitenancy.connector_catalog_api import _catalog_action
    from hermes_multitenancy.connector_custom_catalog import ConnectorCatalog

    catalog = ConnectorCatalog.bundled()
    axiom = catalog.get("trae solo cn:byted-mcp-volcengine.axiom")
    mem0 = catalog.get("trae solo cn:byted-mcp-volcengine.mem0-mcp")
    glean = catalog.get("trae solo cn:byted-mcp-volcengine.glean")
    neo4j = catalog.get("trae solo cn:byted-mcp-volcengine.neo4j")
    markitdown = catalog.get("trae solo cn:byted-mcp-volcengine.markitdown")

    assert _catalog_action(axiom)["kind"] == "authorize"
    assert _catalog_action(mem0)["kind"] == "authorize"
    assert _catalog_action(glean)["fields"] == ["GLEAN_MCP_URL"]
    assert _catalog_action(neo4j)["kind"] == "install_sandbox"
    assert _catalog_action(markitdown)["kind"] == "install_sandbox"
    assert neo4j["runtime_manifest"]["static_env"]["NEO4J_READ_ONLY"] == "true"
    assert _catalog_action(catalog.get("trae solo cn:byted-mcp-volcengine.meta_human"))["kind"] == "connect"
    assert _catalog_action(catalog.get("trae solo cn:byted-mcp-volcengine.kxss_mcp"))["fields"] == ["auth"]


def test_dynamic_public_endpoint_template_is_owner_bound(tmp_path: Path):
    from hermes_multitenancy.connector_catalog_api import _catalog_action
    from hermes_multitenancy.connector_custom_catalog import ConnectorCatalog, CustomConnectorStore

    row = ConnectorCatalog.bundled().get("workbuddy:h3yun-connector")
    action = _catalog_action(row)
    assert action["fields"] == ["H3YUN_API_BASE_URL", "H3YUN_TOKEN"]

    store = CustomConnectorStore(tmp_path / "catalog.db", encryption_key="test-key", resolver=_public_dns)
    installed = store.install_catalog(
        "alice", "subject-alice", name=action["installation_name"],
        transport=action["transport"], endpoint=action["endpoint"],
        credential_schema=row["credential_schema"],
        fields={"H3YUN_API_BASE_URL": "https://tenant.example.com", "H3YUN_TOKEN": "secret"},
    )
    runtime = store.get_runtime("alice", "subject-alice", installed["connector_id"])
    assert runtime["endpoint"] == "https://tenant.example.com/v1/agent/mcp"
    assert runtime["headers"] == {"Authorization": "Bearer secret"}
    with pytest.raises(PermissionError):
        store.get_runtime("bob", "subject-bob", installed["connector_id"])
    store.close()

    baidu = ConnectorCatalog.bundled().get("workbuddy:baidu-netdisk")
    baidu_action = _catalog_action(baidu)
    assert baidu_action["fields"] == ["BAIDU_NETDISK_ACCESS_TOKEN"]
    assert baidu_action["endpoint"] == "https://mcp-pan.baidu.com/sse"

    showapi = ConnectorCatalog.bundled().get("trae solo cn:byted-mcp-volcengine.qrcode_mcp")
    showapi_action = _catalog_action(showapi)
    store = CustomConnectorStore(tmp_path / "showapi.db", encryption_key="test-key", resolver=_public_dns)
    installed = store.install_catalog(
        "alice", "subject-alice", name=showapi_action["installation_name"],
        transport=showapi_action["transport"], endpoint=showapi_action["endpoint"],
        credential_schema=showapi["credential_schema"], fields={"SHOWAPI_APP_KEY": "owner-key"},
    )
    assert "owner-key" not in (tmp_path / "showapi.db").read_text(errors="ignore")
    assert store.get_runtime("alice", "subject-alice", installed["connector_id"])["endpoint"].endswith("/owner-key")
    store.close()


def test_official_readme_remote_recoveries_are_connectable_without_persisting_query_secrets(tmp_path: Path):
    from collections import Counter

    from hermes_multitenancy.connector_catalog_api import _catalog_action
    from hermes_multitenancy.connector_custom_catalog import ConnectorCatalog, CustomConnectorStore

    rows = ConnectorCatalog.bundled().list_rows()
    recovered = [row for row in rows if row.get("remote_recovery")]
    assert len(recovered) == 133
    assert Counter(bool(row["remote_recovery"]["fields"]) for row in recovered) == {True: 125, False: 8}
    row = next(
        item for item in recovered
        if item["row_key"] == "trae solo cn:byted-mcp-volcengine.3rd_party_mcp_chanjing"
    )
    action = _catalog_action(row)
    assert action["fields"] == ["appId", "appSecret"]
    assert action["endpoint"] == "https://mcp-service.chanjing.cc/sse"

    db = tmp_path / "multitenancy.db"
    store = CustomConnectorStore(db, encryption_key="test-key", resolver=_public_dns)
    installed = store.install_catalog(
        "alice", "subject-alice",
        name=action["installation_name"],
        transport=action["transport"],
        endpoint=action["endpoint"],
        credential_schema=row["credential_schema"],
        fields={"appId": "owner-id", "appSecret": "owner-secret"},
    )
    assert "owner-id" not in db.read_text(errors="ignore")
    assert "owner-secret" not in db.read_text(errors="ignore")
    runtime = store.get_runtime("alice", "subject-alice", installed["connector_id"])
    assert runtime["headers"] == {}
    assert runtime["endpoint"] == "https://mcp-service.chanjing.cc/sse?appId=owner-id&appSecret=owner-secret"
    with pytest.raises(PermissionError):
        store.get_runtime("bob", "subject-bob", installed["connector_id"])
    store.close()


def test_bundled_uvx_resolutions_pin_git_commits_or_pypi_hashes():
    from collections import Counter

    path = Path(__file__).parents[1] / "hermes_multitenancy" / "connector_catalog_data" / "stdio_python_resolutions.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 190
    assert Counter(row["state"] for row in rows) == {"git_resolved": 136, "pypi_resolved": 54}
    assert all(
        len(row["commit"]) == 40 and row["commit"].isalnum()
        for row in rows if row["state"] == "git_resolved"
    )
    assert all(
        row["version"] and any(file["sha256"] for file in row["artifacts"])
        for row in rows if row["state"] == "pypi_resolved"
    )
    assert {
        row["commit"] for row in rows
        if row["state"] == "git_resolved" and "github.com/volcengine/mcp-server" in row["repository"]
    } == {
        "3be81f20ff8566a462cd4a5608a0c371ec69419e",
        "ee80639f61aa1d4386793d45a395d57139afe2f4",
    }

    locks_path = Path(__file__).parents[1] / "hermes_multitenancy" / "connector_catalog_data" / "stdio_python_locks.jsonl"
    locks = [json.loads(line) for line in locks_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(locks) == 54
    assert sum(lock["state"] == "resolved" for lock in locks) == 54
    assert all(
        lock["requirements_sha256"] and "--hash=sha256:" in lock["requirements"]
        for lock in locks if lock["state"] == "resolved"
    )
    git_locks_path = (
        Path(__file__).parents[1] / "hermes_multitenancy" / "connector_catalog_data"
        / "stdio_python_git_locks.jsonl"
    )
    git_locks = [json.loads(line) for line in git_locks_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(git_locks) == 77
    assert sum(lock["state"] == "resolved" for lock in git_locks) == 77
    assert all(
        lock["source_archive_sha256"] and lock["python_runtime"]["executable"] == "/usr/bin/python3.12"
        and "--hash=sha256:" in lock["requirements"]
        for lock in git_locks if lock["state"] == "resolved"
    )


def test_custom_remote_import_is_owner_scoped_and_never_stores_plaintext_secret(tmp_path: Path):
    from hermes_multitenancy.connector_custom_catalog import CustomConnectorStore

    db = tmp_path / "multitenancy.db"
    store = CustomConnectorStore(db, encryption_key="test-key", resolver=_public_dns)
    config = {
        "mcpServers": {
            "demo": {
                "type": "streamableHttp",
                "url": "https://mcp.example.com/mcp",
                "headers": {"Authorization": "Bearer owner-secret"},
            }
        }
    }
    row = store.import_config("alice", "subject-alice", json.dumps(config))[0]
    assert row["transport"] == "streamable_http"
    assert row["state"] == "active"
    assert row["credential_fields"] == ["Authorization"]
    assert "headers" not in row and "owner-secret" not in db.read_text(errors="ignore")
    assert [item["name"] for item in store.list_installations("alice", "subject-alice")] == ["demo"]
    assert store.list_installations("bob", "subject-bob") == []
    with pytest.raises(PermissionError):
        store.get_runtime("bob", "subject-bob", row["connector_id"])
    runtime = store.get_runtime("alice", "subject-alice", row["connector_id"])
    assert runtime["headers"] == {"Authorization": "Bearer owner-secret"}
    store.close()


def test_custom_import_rejects_processes_and_private_endpoints_before_persisting(tmp_path: Path):
    from hermes_multitenancy.connector_custom_catalog import CustomConnectorStore

    db = tmp_path / "multitenancy.db"
    store = CustomConnectorStore(db, encryption_key="test-key", resolver=_public_dns)
    with pytest.raises(ValueError, match="command"):
        store.import_config(
            "alice",
            "subject-alice",
            '{"mcpServers":{"bad":{"command":"npx","args":["pkg@latest"]}}}',
        )
    store.close()

    private = CustomConnectorStore(db, encryption_key="test-key", resolver=_private_dns)
    with pytest.raises(ValueError, match="public"):
        private.import_config(
            "alice",
            "subject-alice",
            "mcpServers:\n  bad:\n    type: sse\n    url: https://internal.example/sse\n",
        )
    assert private.list_installations("alice", "subject-alice") == []
    private.close()


def test_webui_catalog_and_custom_installations_use_trusted_owner(monkeypatch, tmp_path: Path):
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import connector_catalog_api
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.connector_custom_catalog import CustomConnectorStore
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    routes = tmp_path / "routing.db"
    table = RoutingTable(routes)
    table.upsert(user_id="alice", profile_name="alice-profile", open_id="ou_alice", provenance="sync")
    table.upsert(user_id="bob", profile_name="bob-profile", open_id="ou_bob", provenance="sync")
    table.close()
    router_mod.override_routing_table(routes)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(tmp_path / "shared"))
    monkeypatch.setenv("HERMES_CREDENTIAL_KEY", "test-key")
    monkeypatch.setenv("HERMES_MCP_PUBLIC_ORIGIN", "https://hermes.example")
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    original = CustomConnectorStore
    monkeypatch.setattr(
        connector_catalog_api,
        "CustomConnectorStore",
        lambda path: original(path, encryption_key="test-key", resolver=_public_dns),
    )
    verified = []

    async def verify(_db_path, profile_name, subject_id, connector_id):
        verified.append((profile_name, subject_id, connector_id))
        return 1

    monkeypatch.setattr(connector_catalog_api, "_verify_catalog_connection", verify)
    prepared = []

    async def prepare_runtime(path, manifest):
        fingerprint = manifest["package_resolution"]["resolution_fingerprint"]
        prepared.append((path, fingerprint))
        return path / fingerprint

    monkeypatch.setattr(connector_catalog_api, "prepare_stdio_runtime", prepare_runtime)
    cli_calls = []

    async def prepare_cli(path, manifest):
        cli_calls.append(("prepare", path, manifest["row_key"]))
        return path

    cli_status_results = iter((False, True))

    async def status_cli(path, connector_id, manifest):
        cli_calls.append(("status", path, connector_id, manifest["row_key"]))
        return next(cli_status_results)

    class FakeProcess:
        returncode = None

    async def start_cli(path, connector_id, manifest):
        cli_calls.append(("start", path, connector_id, manifest["row_key"]))
        return "https://meeting.tencent.com/authorize", FakeProcess(), tmp_path / "cli.env", "unit"

    async def stop_cli(*_args):
        cli_calls.append(("stop",))

    async def logout_cli(*_args):
        cli_calls.append(("logout",))

    monkeypatch.setattr(connector_catalog_api, "prepare_cli_runtime", prepare_cli)
    monkeypatch.setattr(connector_catalog_api, "cli_status", status_cli)
    monkeypatch.setattr(connector_catalog_api, "start_cli_auth", start_cli)
    monkeypatch.setattr(connector_catalog_api, "stop_cli_auth", stop_cli)
    monkeypatch.setattr(connector_catalog_api, "logout_cli", logout_cli)
    oauth_calls = []

    class FakeOAuthBroker:
        async def start(self, profile, subject, row, *, redirect_uri):
            oauth_calls.append(("start", profile, subject, row["row_key"], redirect_uri, row["endpoint"]))
            return {"authorization_url": "https://auth.example/authorize?state=oauth-state"}

        async def complete(self, state, code):
            oauth_calls.append(("complete", state, code))
            return {"connector_id": "custom-aaaaaaaaaaaaaaaaaaaaaaaa", "state": "ready"}

    oauth_broker = FakeOAuthBroker()
    monkeypatch.setattr(connector_catalog_api, "_get_oauth_broker", lambda _path: oauth_broker)

    async def run():
        client = TestClient(TestServer(create_run_broker_app(mark_seen=lambda _r: True, sandbox_available=lambda: True)))
        await client.start_server()
        try:
            alice = {"X-Hermes-Owner-Open-Id": "ou_alice"}
            bob = {"X-Hermes-Owner-Open-Id": "ou_bob"}
            catalog = await client.get("/api/run-broker/connector-catalog", headers=alice)
            catalog_body = await catalog.json()
            assert catalog.status == 200 and catalog_body["source_count"] == 642
            assert len(catalog_body["connectors"]) == 642
            assert all(row["action"]["kind"] for row in catalog_body["connectors"])
            assert all("next_action" in row for row in catalog_body["connectors"])
            canonical_body = await (await client.get(
                "/api/run-broker/connector-catalog?view=canonical", headers=alice
            )).json()
            assert len(canonical_body["connectors"]) == 330
            assert all(row["action"]["kind"] for row in canonical_body["connectors"])
            assert all(row["action"]["available"] is False for row in canonical_body["connectors"]
                       if set(row["verdicts"]) <= {"incompatible", "rejected"})
            assert "path" not in catalog_body["connectors"][0]["icon"]
            assert all(
                row.get("endpoint") is None
                for row in catalog_body["connectors"]
                if str(row.get("transport") or "").casefold() == "stdio"
            )
            icon = await client.get(catalog_body["connectors"][0]["icon"]["url"])
            assert icon.status == 200 and (await icon.read())
            assert (await client.get("/api/run-broker/connector-catalog/icon?row_key=../multitenancy.db")).status == 404
            assert (await client.get("/api/run-broker/connector-catalog/icon?row_key=/etc/hosts")).status == 404
            assert (await client.post("/api/run-broker/custom-connectors/import", headers=alice, json=[])).status == 400

            cli = next(row for row in catalog_body["connectors"] if row["row_key"] == "workbuddy:tmeet")
            assert cli["action"]["kind"] == "authorize_cli"
            cli_started = await client.post(
                "/api/run-broker/connector-catalog/connect", headers=alice,
                json={"row_key": cli["row_key"]},
            )
            cli_started_body = await cli_started.json()
            assert cli_started.status == 202
            assert cli_started_body["authorization_url"].startswith("https://meeting.tencent.com/")
            assert (await client.post(
                "/api/run-broker/connector-catalog/status", headers=bob,
                json={"row_key": cli["row_key"]},
            )).status == 403
            cli_ready = await client.post(
                "/api/run-broker/connector-catalog/status", headers=alice,
                json={"row_key": cli["row_key"]},
            )
            cli_ready_body = await cli_ready.json()
            assert cli_ready.status == 200 and cli_ready_body["ready"] is True
            assert cli_ready_body["connector"]["state"] == "ready"
            assert (await client.delete(
                f'/api/run-broker/custom-connectors/{cli_ready_body["connector"]["connector_id"]}',
                headers=alice,
            )).status == 200
            assert ("stop",) in cli_calls and ("logout",) in cli_calls

            sandboxed = next(
                row for row in catalog_body["connectors"]
                if row["row_key"] == "trae solo cn:byted-mcp-volcengine.brave_search"
            )
            assert sandboxed["action"]["kind"] == "install_sandbox"
            sandbox_connected = await client.post(
                "/api/run-broker/connector-catalog/connect",
                headers=alice,
                json={"row_key": sandboxed["row_key"], "fields": {"BRAVE_API_KEY": "sandbox-secret"}},
            )
            sandbox_body = await sandbox_connected.json()
            assert sandbox_connected.status == 201, sandbox_body
            sandbox_installation = next(
                item for item in sandbox_body["connectors"]
                if item["name"] == sandboxed["action"]["installation_name"]
            )
            assert sandbox_installation["transport"] == "stdio"
            assert prepared and prepared[0][0] == tmp_path / "shared" / "connector-runtimes"
            assert "sandbox-secret" not in json.dumps(sandbox_body)
            assert "sandbox-secret" not in (tmp_path / "shared" / "multitenancy.db").read_text(errors="ignore")
            assert (await client.delete(
                f'/api/run-broker/custom-connectors/{sandbox_installation["connector_id"]}', headers=alice
            )).status == 200

            ready = next(row for row in catalog_body["connectors"] if row["action"].get("kind") == "connect")
            connected = await client.post(
                "/api/run-broker/connector-catalog/connect", headers=alice, json={"row_key": ready["row_key"]}
            )
            connected_body = await connected.json()
            assert connected.status == 201
            assert connected_body["connectors"][0]["name"] == ready["action"]["installation_name"]
            assert connected_body["connectors"][0]["state"] == "ready"
            assert verified[-1] == (
                "alice-profile", "ou_alice", connected_body["connectors"][0]["connector_id"]
            )
            assert len(verified) == 2
            refreshed_catalog = await (await client.get(
                "/api/run-broker/connector-catalog", headers=alice
            )).json()
            refreshed = next(row for row in refreshed_catalog["connectors"] if row["row_key"] == ready["row_key"])
            assert refreshed["action"]["status"] == "ready"
            assert refreshed["action"]["connector_id"] == connected_body["connectors"][0]["connector_id"]
            duplicate = await client.post(
                "/api/run-broker/connector-catalog/connect", headers=alice, json={"row_key": ready["row_key"]}
            )
            assert duplicate.status == 409
            assert (await (await client.get("/api/run-broker/custom-connectors", headers=alice)).json())["connectors"]
            assert (await (await client.get("/api/run-broker/custom-connectors", headers=bob)).json())["connectors"] == []
            oauth = next(
                row for row in catalog_body["connectors"]
                if (row.get("credential_schema") or {}).get("auth_flow") == "mcp_oauth"
                and row["final_verdict"] == "needs_auth"
            )
            oauth_start = await client.post(
                "/api/run-broker/connector-catalog/connect", headers=alice, json={"row_key": oauth["row_key"]}
            )
            assert oauth_start.status == 202
            assert (await oauth_start.json())["authorization_url"].startswith("https://auth.example/")
            assert oauth_calls[0][:5] == (
                "start", "alice-profile", "ou_alice", oauth["row_key"],
                "https://hermes.example/api/auth/skill-credentials/catalog/oauth/callback",
            )
            oauth_complete = await client.post(
                "/api/run-broker/connector-catalog/oauth/callback",
                json={"state": "oauth-state", "code": "oauth-code"},
            )
            assert oauth_complete.status == 200
            assert oauth_calls[1] == ("complete", "oauth-state", "oauth-code")

            glean = next(row for row in catalog_body["connectors"]
                         if row["row_key"] == "trae solo cn:byted-mcp-volcengine.glean")
            glean_start = await client.post(
                "/api/run-broker/connector-catalog/connect", headers=alice,
                json={"row_key": glean["row_key"], "fields": {
                    "GLEAN_MCP_URL": "https://acme-be.glean.com/mcp/research",
                }},
            )
            assert glean_start.status == 202
            assert oauth_calls[-1][-1] == "https://acme-be.glean.com/mcp/research"
            assert (await client.post(
                "/api/run-broker/connector-catalog/connect", headers=alice,
                json={"row_key": glean["row_key"], "fields": {
                    "GLEAN_MCP_URL": "https://evil.example/mcp/research",
                }},
            )).status == 400

            manual = next(
                row for row in catalog_body["connectors"]
                if (row.get("credential_schema") or {}).get("fields") == ["Authorization"]
                and row["final_verdict"] == "needs_auth"
            )
            authorized = await client.post(
                "/api/run-broker/connector-catalog/connect",
                headers=alice,
                json={"row_key": manual["row_key"], "fields": {"Authorization": "Bearer catalog-secret"}},
            )
            authorized_body = await authorized.json()
            assert authorized.status == 201, authorized_body
            manual_installation = next(
                item for item in authorized_body["connectors"]
                if item["name"] == manual["action"]["installation_name"]
            )
            assert manual_installation["state"] == "ready"
            assert "catalog-secret" not in json.dumps(authorized_body)
            assert "catalog-secret" not in (tmp_path / "shared" / "multitenancy.db").read_text(errors="ignore")
            assert (await (await client.get(
                "/api/run-broker/custom-connectors", headers=bob
            )).json())["connectors"] == []
            assert (await client.delete(
                f'/api/run-broker/custom-connectors/{manual_installation["connector_id"]}',
                headers=alice,
            )).status == 200
            catalog_connector_id = connected_body["connectors"][0]["connector_id"]
            assert (await client.delete(
                f"/api/run-broker/custom-connectors/{catalog_connector_id}", headers=alice
            )).status == 200

            async def fail_verify(*_args):
                raise RuntimeError("tools/list failed")

            monkeypatch.setattr(connector_catalog_api, "_verify_catalog_connection", fail_verify)
            failed = await client.post(
                "/api/run-broker/connector-catalog/connect", headers=alice, json={"row_key": ready["row_key"]}
            )
            assert failed.status == 502
            assert (await (await client.get(
                "/api/run-broker/custom-connectors", headers=alice
            )).json())["connectors"] == []

            spoofed = await client.post(
                "/api/run-broker/custom-connectors/import",
                headers=alice,
                json={
                    "profile_name": "bob-profile",
                    "config": {"mcpServers": {"demo": {"url": "https://mcp.example.com/mcp"}}},
                },
            )
            assert spoofed.status == 403
            created = await client.post(
                "/api/run-broker/custom-connectors/import",
                headers=alice,
                json={"config": {"mcpServers": {"demo": {
                    "url": "https://mcp.example.com/mcp",
                    "headers": {"Authorization": "Bearer response-secret"},
                }}}},
            )
            created_body = await created.json()
            assert created.status == 201 and created_body["profile_name"] == "alice-profile"
            assert "response-secret" not in json.dumps(created_body)
            connector_id = created_body["connectors"][0]["connector_id"]
            listed_body = await (await client.get("/api/run-broker/custom-connectors", headers=alice)).json()
            assert listed_body["connectors"]
            assert "response-secret" not in json.dumps(listed_body)
            assert (await (await client.get("/api/run-broker/custom-connectors", headers=bob)).json())["connectors"] == []
            denied = await client.delete(f"/api/run-broker/custom-connectors/{connector_id}", headers=bob)
            assert denied.status == 403
            removed = await client.delete(f"/api/run-broker/custom-connectors/{connector_id}", headers=alice)
            assert removed.status == 200
        finally:
            await client.close()
            router_mod.override_routing_table(None)

    asyncio.run(run())


def test_custom_runtime_exposes_only_owner_readonly_tools_and_revocation_is_immediate(tmp_path: Path):
    import asyncio

    from hermes_multitenancy.connector_custom_catalog import CustomConnectorStore
    from hermes_multitenancy.connector_custom_runtime import CustomConnectorRuntime

    db = tmp_path / "multitenancy.db"
    store = CustomConnectorStore(db, encryption_key="test-key", resolver=_public_dns)
    connector_id = store.import_config(
        "alice", "subject-alice", {"mcpServers": {"demo": {"url": "https://mcp.example.com/mcp"}}}
    )[0]["connector_id"]
    store.close()
    calls = []

    async def exchange(runtime, method, arguments):
        calls.append((runtime["connector_id"], method, arguments))
        if method == "tools/list":
            return {
                "tools": [
                    {"name": "read", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}},
                    {"name": "write", "inputSchema": {"type": "object"}},
                ]
            }
        return {"content": [{"type": "text", "text": "ok"}]}

    runtime = CustomConnectorRuntime(db, encryption_key="test-key", exchange=exchange)

    async def run():
        direct = await runtime.list_connector_tools("alice", "subject-alice", connector_id)
        assert [tool["name"] for tool in direct] == [f"{connector_id}__read"]
        tools = await runtime.list_tools("alice", "subject-alice")
        assert [tool["name"] for tool in tools] == [f"{connector_id}__read"]
        assert await runtime.list_tools("bob", "subject-bob") == []
        with pytest.raises(PermissionError, match="approved"):
            await runtime.call_tool("alice", "subject-alice", f"{connector_id}__write", {})
        result = await runtime.call_tool("alice", "subject-alice", f"{connector_id}__read", {"x": 1})
        assert result["content"][0]["text"] == "ok"
        owner = CustomConnectorStore(db, encryption_key="test-key", resolver=_public_dns)
        owner.delete("alice", "subject-alice", connector_id)
        owner.close()
        with pytest.raises(PermissionError, match="unavailable"):
            await runtime.call_tool("alice", "subject-alice", f"{connector_id}__read", {})

    asyncio.run(run())
    assert not any(method == "tools/call" and args.get("name") == "write" for _, method, args in calls)


def test_custom_runtime_routes_stdio_through_the_owner_runtime(tmp_path: Path):
    import asyncio

    from hermes_multitenancy.connector_catalog_api import _catalog_action
    from hermes_multitenancy.connector_custom_catalog import ConnectorCatalog, CustomConnectorStore
    from hermes_multitenancy.connector_custom_runtime import CustomConnectorRuntime

    row = next(
        item for item in ConnectorCatalog.bundled().list_rows()
        if item["row_key"] == "trae solo cn:byted-mcp-volcengine.brave_search"
    )
    action = _catalog_action(row)
    db = tmp_path / "multitenancy.db"
    store = CustomConnectorStore(db, encryption_key="test-key", resolver=_public_dns)
    connector_id = store.install_catalog_stdio(
        "alice",
        "subject-alice",
        name=action["installation_name"],
        row_key=row["row_key"],
        runtime_manifest=row["runtime_manifest"],
        fields={"BRAVE_API_KEY": "owner-secret"},
    )["connector_id"]
    store.close()
    calls = []

    async def stdio_exchange(runtime, method, arguments):
        calls.append((runtime, method, arguments))
        if method == "tools/list":
            return {"tools": [{
                "name": "search", "inputSchema": {"type": "object"},
                "annotations": {"readOnlyHint": True},
            }]}
        return {"content": [{"type": "text", "text": "ok"}]}

    runtime = CustomConnectorRuntime(
        db,
        encryption_key="test-key",
        exchange=lambda *_args: (_ for _ in ()).throw(AssertionError("remote exchange used")),
        stdio_exchange=stdio_exchange,
    )

    async def run():
        tools = await runtime.list_connector_tools("alice", "subject-alice", connector_id)
        assert [tool["name"] for tool in tools] == [f"{connector_id}__search"]
        result = await runtime.call_tool(
            "alice", "subject-alice", f"{connector_id}__search", {"q": "MCP"}
        )
        assert result["content"][0]["text"] == "ok"

    asyncio.run(run())
    assert all(call[0]["environment"] == {"BRAVE_API_KEY": "owner-secret"} for call in calls)
    assert [call[1] for call in calls] == ["tools/list", "tools/list", "tools/call"]


def test_custom_runtime_reads_chunked_multiline_sse_json():
    import asyncio

    from hermes_multitenancy.connector_custom_runtime import _response_json

    class ChunkedContent:
        def __init__(self):
            self.parts = iter([b'data: {"jsonrpc":"2.0",\n', b'data: "result":{"tools":[]}}\n\n', b''])

        async def read(self, _size):
            return next(self.parts)

    response = type("Response", (), {
        "content": ChunkedContent(),
        "headers": {"Content-Type": "text/event-stream; charset=utf-8"},
    })()
    assert asyncio.run(_response_json(response)) == {"jsonrpc": "2.0", "result": {"tools": []}}


def test_catalog_oauth_broker_completes_pkce_into_the_owner_vault(tmp_path: Path):
    import asyncio
    from urllib.parse import parse_qs, urlparse

    import httpx

    from hermes_multitenancy.connector_catalog_oauth import CatalogOAuthBroker
    from hermes_multitenancy.connector_custom_catalog import ConnectorCatalog, CustomConnectorStore
    from hermes_multitenancy.credentials import CredentialStore

    row = next(item for item in ConnectorCatalog.bundled().list_rows() if item["row_key"] == "workbuddy:linear-mcp")
    requests = []

    def upstream(request: httpx.Request):
        requests.append(request)
        url = str(request.url)
        if request.method == "GET" and "oauth-protected-resource" in url:
            return httpx.Response(200, json={
                "resource": row["endpoint"],
                "authorization_servers": ["https://auth.linear.example"],
            })
        if request.method == "GET" and ".well-known" in url:
            return httpx.Response(200, json={
                "issuer": "https://auth.linear.example",
                "authorization_endpoint": "https://auth.linear.example/authorize",
                "token_endpoint": "https://auth.linear.example/token",
                "registration_endpoint": "https://auth.linear.example/register",
                "code_challenge_methods_supported": ["S256"],
            })
        if url == "https://auth.linear.example/register":
            return httpx.Response(201, json={
                "client_id": "hermes-test-client",
                "redirect_uris": ["https://hermes.example/api/auth/skill-credentials/catalog/oauth/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            })
        if url == "https://auth.linear.example/token":
            if b"grant_type=refresh_token" in request.content:
                assert b"refresh_token=owner-refresh-token" in request.content
                return httpx.Response(200, json={
                    "access_token": "owner-refreshed-access-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                })
            assert b"code_verifier=" in request.content and b"code=oauth-code" in request.content
            return httpx.Response(200, json={
                "access_token": "owner-access-token",
                "refresh_token": "owner-refresh-token",
                "token_type": "Bearer",
                "expires_in": 3600,
            })
        if request.headers.get("Authorization") == "Bearer owner-access-token":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "linear", "version": "1"}}})
        return httpx.Response(401, headers={
            "WWW-Authenticate": f'Bearer resource_metadata="{row["endpoint"].rsplit("/", 1)[0]}/.well-known/oauth-protected-resource"',
        })

    verified = []

    async def verify(_db, profile, subject, connector_id):
        verified.append((profile, subject, connector_id))
        return 1

    async def run():
        db = tmp_path / "multitenancy.db"
        broker = CatalogOAuthBroker(
            db,
            encryption_key="test-key",
            resolver=_public_dns,
            transport=httpx.MockTransport(upstream),
            verify=verify,
        )
        started = await broker.start(
            "alice", "subject-alice", row,
            redirect_uri="https://hermes.example/api/auth/skill-credentials/catalog/oauth/callback",
        )
        auth = urlparse(started["authorization_url"])
        params = parse_qs(auth.query)
        assert auth.netloc == "auth.linear.example"
        assert params["code_challenge_method"] == ["S256"] and params["state"]
        completed = await broker.complete(params["state"][0], "oauth-code")
        assert completed["state"] == "ready"
        assert verified == [("alice", "subject-alice", completed["connector_id"])]
        with pytest.raises(PermissionError, match="unavailable"):
            await broker.complete(params["state"][0], "replay")

        store = CustomConnectorStore(db, encryption_key="test-key", resolver=_public_dns)
        assert store.list_installations("bob", "subject-bob") == []
        assert store.get_runtime("alice", "subject-alice", completed["connector_id"])["headers"] == {
            "Authorization": "Bearer owner-access-token"
        }
        store.close()

        import sqlite3
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE multitenancy_credentials SET expires_at=1 "
            "WHERE profile_name='alice' AND subject_id='subject-alice' AND secret_kind='oauth'"
        )
        conn.commit()
        conn.close()

        async def exchange(runtime, method, _arguments):
            assert runtime["headers"] == {"Authorization": "Bearer owner-refreshed-access-token"}
            assert method == "tools/list"
            return {"tools": [{
                "name": "read",
                "inputSchema": {"type": "object"},
                "annotations": {"readOnlyHint": True},
            }]}

        from hermes_multitenancy.connector_custom_runtime import CustomConnectorRuntime
        runtime = CustomConnectorRuntime(
            db,
            encryption_key="test-key",
            exchange=exchange,
            oauth_transport=httpx.MockTransport(upstream),
            resolver=_public_dns,
        )
        tools = await runtime.list_connector_tools("alice", "subject-alice", completed["connector_id"])
        assert [tool["name"] for tool in tools] == [f'{completed["connector_id"]}__read']

        vault = CredentialStore(db, encryption_key="test-key")
        refreshed = vault.get_secret_for_runtime(
            profile_name="alice", subject_id="subject-alice",
            provider=row["credential_schema"]["provider"], secret_kind="oauth",
        )
        vault.close()
        assert refreshed["tokens"]["refresh_token"] == "owner-refresh-token"
        assert "owner-access-token" not in db.read_text(errors="ignore")
        assert "owner-refresh-token" not in db.read_text(errors="ignore")

    asyncio.run(run())


def test_catalog_oauth_broker_expires_abandoned_callback(tmp_path: Path):
    import asyncio
    from urllib.parse import parse_qs, urlparse

    import httpx

    from hermes_multitenancy.connector_catalog_oauth import CatalogOAuthBroker
    from hermes_multitenancy.connector_custom_catalog import ConnectorCatalog

    row = next(item for item in ConnectorCatalog.bundled().list_rows() if item["row_key"] == "workbuddy:linear-mcp")

    def upstream(request: httpx.Request):
        url = str(request.url)
        if request.method == "GET" and "oauth-protected-resource" in url:
            return httpx.Response(200, json={
                "resource": row["endpoint"],
                "authorization_servers": ["https://auth.linear.example"],
            })
        if request.method == "GET" and ".well-known" in url:
            return httpx.Response(200, json={
                "issuer": "https://auth.linear.example",
                "authorization_endpoint": "https://auth.linear.example/authorize",
                "token_endpoint": "https://auth.linear.example/token",
                "registration_endpoint": "https://auth.linear.example/register",
            })
        if url == "https://auth.linear.example/register":
            return httpx.Response(201, json={
                "client_id": "hermes-test-client",
                "redirect_uris": ["https://hermes.example/oauth/callback"],
                "token_endpoint_auth_method": "none",
            })
        return httpx.Response(401, headers={
            "WWW-Authenticate": f'Bearer resource_metadata="{row["endpoint"].rsplit("/", 1)[0]}/.well-known/oauth-protected-resource"',
        })

    async def run():
        broker = CatalogOAuthBroker(
            tmp_path / "multitenancy.db",
            encryption_key="test-key",
            resolver=_public_dns,
            transport=httpx.MockTransport(upstream),
            verify=lambda *_args: None,
            flow_timeout=0.01,
        )
        started = await broker.start("alice", "subject-alice", row, redirect_uri="https://hermes.example/oauth/callback")
        state = parse_qs(urlparse(started["authorization_url"]).query)["state"][0]
        await asyncio.sleep(0.03)
        assert broker.pending == {}
        with pytest.raises(PermissionError, match="unavailable"):
            await broker.complete(state, "late-code")

    asyncio.run(run())
