from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace


def _install_fake_kanban_db(monkeypatch, *, tasks=None, boards=None, created=None, dispatched=None):
    task_rows = list(tasks or [])
    board_rows = list(boards or [
        {
            "slug": "default",
            "name": "Default",
            "description": "",
            "icon": "kanban",
            "color": "#888888",
            "archived": False,
            "counts": {},
            "total": 0,
        }
    ])
    created_rows = created if created is not None else []
    dispatch_calls = dispatched if dispatched is not None else []

    hermes_cli = types.ModuleType("hermes_cli")
    kanban_db = types.ModuleType("hermes_cli.kanban_db")

    def connect(*, board=None):
        return SimpleNamespace(board=board or "default", close=lambda: None)

    def list_boards(*, include_archived=True):
        if include_archived:
            return list(board_rows)
        return [board for board in board_rows if not board.get("archived")]

    def list_tasks(conn, *, assignee=None, status=None, tenant=None, include_archived=False, **_kwargs):
        rows = [row for row in task_rows if include_archived or getattr(row, "status", None) != "archived"]
        if assignee is not None:
            rows = [row for row in rows if getattr(row, "assignee", None) == assignee]
        if status is not None:
            rows = [row for row in rows if getattr(row, "status", None) == status]
        if tenant is not None:
            rows = [row for row in rows if getattr(row, "tenant", None) == tenant]
        return rows

    def get_task(conn, task_id):
        return next((row for row in task_rows if row.id == task_id), None)

    def create_task(conn, **kwargs):
        created_rows.append(kwargs)
        task = SimpleNamespace(
            id="task-created",
            title=kwargs["title"],
            body=kwargs.get("body"),
            assignee=kwargs.get("assignee"),
            status="ready",
            priority=kwargs.get("priority", 0),
            created_by=kwargs.get("created_by"),
            created_at=1,
            started_at=None,
            completed_at=None,
            workspace_kind=kwargs.get("workspace_kind", "scratch"),
            workspace_path=None,
            tenant=kwargs.get("tenant"),
            result=None,
            skills=None,
        )
        task_rows.append(task)
        return task.id

    def dispatch_once(**kwargs):
        dispatch_calls.append(kwargs)
        return SimpleNamespace(spawned=[], reclaimed=[], promoted=[])

    kanban_db.connect = connect
    kanban_db.list_boards = list_boards
    kanban_db.list_tasks = list_tasks
    kanban_db.get_task = get_task
    kanban_db.create_task = create_task
    kanban_db.dispatch_once = dispatch_once
    hermes_cli.kanban_db = kanban_db
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", kanban_db)


def _seed_owner_routes(db_path):
    from hermes_multitenancy.routing import RoutingTable

    table = RoutingTable(db_path)
    table.upsert(
        user_id="u-owner",
        profile_name="owner_root",
        open_id="ou_owner",
        provenance="sync",
    )
    table.upsert_owned_agent(
        agent_id="webui:ou_owner:writer",
        profile_name="owner_writer",
        owner_open_id="ou_owner",
        display_label="Writer",
        upstream_profile="owner_root",
    )
    table.upsert(
        user_id="u-other",
        profile_name="other_root",
        open_id="ou_other",
        provenance="sync",
    )
    table.upsert_owned_agent(
        agent_id="webui:ou_other:agent",
        profile_name="other_agent",
        owner_open_id="ou_other",
        display_label="Other",
        upstream_profile="other_root",
    )
    table.close()


def test_webui_kanban_assignees_and_stats_are_owner_scoped(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    db_path = tmp_path / "routing.db"
    _seed_owner_routes(db_path)
    _install_fake_kanban_db(monkeypatch, tasks=[
        SimpleNamespace(id="t-owned", title="Owned", body=None, assignee="owner_writer", status="ready", priority=0, created_by="owner_root", created_at=1, started_at=None, completed_at=None, workspace_kind="scratch", workspace_path=None, tenant="ou_owner", result=None, skills=None),
        SimpleNamespace(id="t-foreign", title="Foreign", body=None, assignee="other_agent", status="ready", priority=0, created_by="other_root", created_at=1, started_at=None, completed_at=None, workspace_kind="scratch", workspace_path=None, tenant="ou_other", result=None, skills=None),
    ])

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app()
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                assignee_response = await client.get("/api/run-broker/kanban/assignees", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner",
                })
                assignee_body = await assignee_response.json()
                stats_response = await client.get("/api/run-broker/kanban/stats", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner",
                })
                stats_body = await stats_response.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)
        return assignee_response, assignee_body, stats_response, stats_body

    assignee_response, assignee_body, stats_response, stats_body = asyncio.run(runner())

    assert assignee_response.status == 200
    assert [item["name"] for item in assignee_body["assignees"]] == ["owner_root", "owner_writer"]
    assert assignee_body["assignees"][1]["counts"] == {"ready": 1}
    assert stats_response.status == 200
    assert stats_body["stats"] == {
        "by_status": {"ready": 1},
        "by_assignee": {"owner_writer": 1},
        "total": 1,
    }


def test_webui_kanban_inconsistent_task_attribution_is_foreign(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    db_path = tmp_path / "routing.db"
    _seed_owner_routes(db_path)
    dispatch_calls = []
    _install_fake_kanban_db(monkeypatch, tasks=[
        SimpleNamespace(id="t-owned", title="Owned", body=None, assignee="owner_writer", status="ready", priority=0, created_by="owner_root", created_at=1, started_at=None, completed_at=None, workspace_kind="scratch", workspace_path=None, tenant="ou_owner", result=None, skills=None),
        SimpleNamespace(id="t-conflict-tenant", title="Conflict tenant", body=None, assignee="owner_writer", status="ready", priority=0, created_by="owner_root", created_at=1, started_at=None, completed_at=None, workspace_kind="scratch", workspace_path=None, tenant="ou_other", result=None, skills=None),
        SimpleNamespace(id="t-conflict-assignee", title="Conflict assignee", body=None, assignee="other_agent", status="ready", priority=0, created_by="owner_root", created_at=1, started_at=None, completed_at=None, workspace_kind="scratch", workspace_path=None, tenant="ou_owner", result=None, skills=None),
    ], dispatched=dispatch_calls)

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app()
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                stats_response = await client.get("/api/run-broker/kanban/stats", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner",
                })
                stats_body = await stats_response.json()
                dispatch_response = await client.post("/api/run-broker/kanban/dispatch", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner",
                }, json={"dryRun": False})
                dispatch_body = await dispatch_response.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)
        return stats_response, stats_body, dispatch_response, dispatch_body

    stats_response, stats_body, dispatch_response, dispatch_body = asyncio.run(runner())

    assert stats_response.status == 200
    assert stats_body["stats"]["total"] == 1
    assert dispatch_response.status == 403
    assert "other owners" in dispatch_body["error"]
    assert dispatch_calls == []


def test_webui_kanban_create_stamps_owner_and_rejects_foreign_assignee(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    db_path = tmp_path / "routing.db"
    _seed_owner_routes(db_path)
    created = []
    _install_fake_kanban_db(monkeypatch, created=created)

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app()
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                rejected = await client.post("/api/run-broker/kanban/tasks", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner",
                }, json={
                    "title": "Bad",
                    "assignee": "other_agent",
                })
                rejected_body = await rejected.json()
                created_response = await client.post("/api/run-broker/kanban/tasks", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner",
                }, json={
                    "title": "Good",
                    "body": "from webui",
                    "assignee": "owner_writer",
                    "tenant": "ou_other",
                    "priority": 3,
                })
                created_body = await created_response.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)
        return rejected, rejected_body, created_response, created_body

    rejected, rejected_body, created_response, created_body = asyncio.run(runner())

    assert rejected.status == 403
    assert "assignee" in rejected_body["error"]
    assert created_response.status == 200
    assert created_body["task"]["tenant"] == "ou_owner"
    assert created_body["task"]["created_by"] == "owner_root"
    assert created == [{
        "title": "Good",
        "body": "from webui",
        "assignee": "owner_writer",
        "created_by": "owner_root",
        "tenant": "ou_owner",
        "priority": 3,
        "board": "default",
    }]


def test_webui_kanban_dispatch_refuses_mixed_owner_board(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    db_path = tmp_path / "routing.db"
    _seed_owner_routes(db_path)
    dispatch_calls = []
    _install_fake_kanban_db(monkeypatch, tasks=[
        SimpleNamespace(id="t-owned", title="Owned", body=None, assignee="owner_writer", status="ready", priority=0, created_by="owner_root", created_at=1, started_at=None, completed_at=None, workspace_kind="scratch", workspace_path=None, tenant="ou_owner", result=None, skills=None),
        SimpleNamespace(id="t-foreign", title="Foreign", body=None, assignee="other_agent", status="ready", priority=0, created_by="other_root", created_at=1, started_at=None, completed_at=None, workspace_kind="scratch", workspace_path=None, tenant="ou_other", result=None, skills=None),
    ], dispatched=dispatch_calls)

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app()
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post("/api/run-broker/kanban/dispatch", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner",
                }, json={"dryRun": False})
                body = await response.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)
        return response, body

    response, body = asyncio.run(runner())

    assert response.status == 403
    assert "other owners" in body["error"]
    assert dispatch_calls == []


def test_webui_kanban_dispatch_validates_control_inputs(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    db_path = tmp_path / "routing.db"
    _seed_owner_routes(db_path)
    dispatch_calls = []
    _install_fake_kanban_db(monkeypatch, dispatched=dispatch_calls)

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app()
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                bad_bool = await client.post("/api/run-broker/kanban/dispatch", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner",
                }, json={"dryRun": "false"})
                bad_bool_body = await bad_bool.json()
                bad_max = await client.post("/api/run-broker/kanban/dispatch", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner",
                }, json={"max": 101})
                bad_max_body = await bad_max.json()
                good = await client.post("/api/run-broker/kanban/dispatch", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner",
                }, json={"dryRun": True, "max": 1, "maxInProgress": 2})
                good_body = await good.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)
        return bad_bool, bad_bool_body, bad_max, bad_max_body, good, good_body

    bad_bool, bad_bool_body, bad_max, bad_max_body, good, good_body = asyncio.run(runner())

    assert bad_bool.status == 400
    assert bad_bool_body["error"] == "dryRun must be boolean"
    assert bad_max.status == 400
    assert bad_max_body["error"] == "max must be <= 100"
    assert good.status == 200
    assert good_body["result"]["status"] == "dry_run"
    assert dispatch_calls == [{
        "board": "default",
        "dry_run": True,
        "max_spawn": 1,
        "max_in_progress": 2,
    }]


def test_webui_kanban_capabilities_are_owner_scoped(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    db_path = tmp_path / "routing.db"
    _seed_owner_routes(db_path)
    _install_fake_kanban_db(monkeypatch)

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app()
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                missing_owner = await client.get("/api/run-broker/kanban/capabilities")
                missing_owner_body = await missing_owner.json()
                response = await client.get("/api/run-broker/kanban/capabilities", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner",
                })
                body = await response.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)
        return missing_owner, missing_owner_body, response, body

    missing_owner, missing_owner_body, response, body = asyncio.run(runner())

    assert missing_owner.status == 403
    assert "owner identity required" in missing_owner_body["error"]
    assert response.status == 200
    assert body["capabilities"]["source"] == "hermes-multitenancy-run-broker"
    assert body["capabilities"]["supports"]["dispatch"] is True
    assert body["capabilities"]["supports"]["events"] is False
