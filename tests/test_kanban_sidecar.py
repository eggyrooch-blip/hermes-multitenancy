from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace


def _write_config(shared_home: Path, body: str) -> None:
    shared_home.mkdir(parents=True, exist_ok=True)
    (shared_home / "kanban-sidecar.yaml").write_text(body.strip() + "\n", encoding="utf-8")


def _install_fake_kanban_db(monkeypatch, dispatch_once):
    hermes_cli = types.ModuleType("hermes_cli")
    kanban_db = types.ModuleType("hermes_cli.kanban_db")
    kanban_db.dispatch_once = dispatch_once
    hermes_cli.kanban_db = kanban_db
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", kanban_db)


def test_kanban_sidecar_is_disabled_by_default_without_importing_upstream(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import kanban_sidecar

    imports: list[str] = []

    def fail_import(name: str):
        imports.append(name)
        raise AssertionError("disabled sidecar should not import upstream kanban")

    monkeypatch.setattr(kanban_sidecar.importlib, "import_module", fail_import)

    result = kanban_sidecar.plan_kanban_sidecar(
        shared_home=tmp_path,
        current_profile="kanban_orchestrator",
    )

    assert result["enabled"] is False
    assert result["will_execute"] is False
    assert result["would_execute"] is False
    assert result["status"] == "disabled"
    assert result["secret_free"] is True
    assert imports == []


def test_kanban_sidecar_rejects_router_profile(tmp_path: Path):
    from hermes_multitenancy import kanban_sidecar

    _write_config(
        tmp_path,
        """
        enabled: true
        board: ops
        sidecar_profile: multitenancy_router
        allowed_profiles:
          - multitenancy_router
        execute: true
        """,
    )

    result = kanban_sidecar.plan_kanban_sidecar(
        shared_home=tmp_path,
        current_profile="multitenancy_router",
        execute=True,
    )

    assert result["status"] == "blocked"
    assert result["will_execute"] is False
    assert any("router" in problem.lower() for problem in result["problems"])


def test_kanban_sidecar_dry_run_summarizes_upstream_dispatch(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import kanban_sidecar

    calls: list[dict] = []

    def dispatch_once(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            reclaimed=1,
            promoted=2,
            spawned=["task-a", {"id": "task-b", "assignee": "worker_b"}],
            skipped_unassigned=["task-c"],
            skipped_nonspawnable=["task-d"],
            crashed=[],
            auto_blocked=[],
            timed_out=[],
            stale=[],
            respawn_guarded=[],
        )

    _install_fake_kanban_db(monkeypatch, dispatch_once)
    _write_config(
        tmp_path,
        """
        enabled: true
        board: ops
        tenant: tenant-a
        sidecar_profile: kanban_orchestrator
        allowed_profiles:
          - kanban_orchestrator
          - worker_a
        max_spawn: 3
        max_in_progress: 5
        execute: false
        """,
    )

    result = kanban_sidecar.plan_kanban_sidecar(
        shared_home=tmp_path,
        current_profile="kanban_orchestrator",
    )

    assert result["status"] == "dry_run"
    assert result["enabled"] is True
    assert result["will_execute"] is False
    assert result["would_execute"] is True
    assert result["board"] == "ops"
    assert result["tenant"] == "tenant-a"
    assert result["summary"]["spawned_count"] == 2
    assert result["summary"]["skipped_unassigned_count"] == 1
    assert result["summary"]["skipped_nonspawnable_count"] == 1
    assert result["spawned"][0] == "task-a"
    assert calls == [
        {
            "board": "ops",
            "dry_run": True,
            "max_spawn": 3,
            "max_in_progress": 5,
        }
    ]


def test_kanban_sidecar_executes_only_when_explicitly_enabled(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import kanban_sidecar

    calls: list[dict] = []

    def dispatch_once(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            reclaimed=0,
            promoted=0,
            spawned=[{"id": "task-live", "assignee": "worker_a"}],
            skipped_unassigned=[],
            skipped_nonspawnable=[],
            crashed=[],
            auto_blocked=[],
            timed_out=[],
            stale=[],
            respawn_guarded=[],
        )

    _install_fake_kanban_db(monkeypatch, dispatch_once)
    _write_config(
        tmp_path,
        """
        enabled: true
        board: ops
        sidecar_profile: kanban_orchestrator
        allowed_profiles:
          - kanban_orchestrator
        execute: true
        """,
    )

    dry = kanban_sidecar.plan_kanban_sidecar(
        shared_home=tmp_path,
        current_profile="kanban_orchestrator",
    )
    live = kanban_sidecar.plan_kanban_sidecar(
        shared_home=tmp_path,
        current_profile="kanban_orchestrator",
        execute=True,
    )

    assert dry["status"] == "dry_run"
    assert dry["will_execute"] is False
    assert live["status"] == "executed"
    assert live["will_execute"] is True
    assert live["summary"]["spawned_count"] == 1
    assert calls[0] == {
        "board": "ops",
        "dry_run": True,
        "max_spawn": None,
        "max_in_progress": None,
    }
    assert calls[1]["board"] == "ops"
    assert calls[1]["dry_run"] is False
    assert calls[1]["max_spawn"] is None
    assert calls[1]["max_in_progress"] is None
    assert callable(calls[1]["spawn_fn"])


def test_kanban_task_builds_profile_scoped_run_request(tmp_path: Path):
    from hermes_multitenancy import kanban_sidecar

    task = SimpleNamespace(
        id="task-123",
        title="Prepare weekly report",
        body="Summarize this week's incidents",
        assignee="worker_a",
        tenant="tenant-a",
        created_by="ou_creator",
        current_run_id=42,
    )
    config = kanban_sidecar.KanbanSidecarConfig(
        enabled=True,
        board="ops",
        sidecar_profile="kanban_orchestrator",
        allowed_task_profiles=("worker_a",),
        profile_user_keys={"worker_a": "ou_worker_a"},
        delivery_mode="feishu",
    )

    request = kanban_sidecar.build_run_request_for_task(
        task,
        config=config,
        workspace="/tmp/workspace/task-123",
    )

    assert request.channel == "kanban"
    assert request.profile_name == "worker_a"
    assert request.user_key == "ou_worker_a"
    assert request.credential_subject == "ou_worker_a"
    assert request.session_id == "kanban:ops:task-123"
    assert request.message_id == "task-123"
    assert request.delivery_mode == "feishu"
    assert request.requires_host_tools is True
    assert "Prepare weekly report" in request.content
    assert request.metadata == {
        "kanban_task_id": "task-123",
        "kanban_board": "ops",
        "kanban_tenant": "tenant-a",
        "kanban_run_id": 42,
        "kanban_workspace": "/tmp/workspace/task-123",
        "kanban_assignee": "worker_a",
        "allowed_task_profiles": ["worker_a"],
    }


def test_kanban_sidecar_run_broker_spawn_completes_task(monkeypatch):
    from hermes_multitenancy import kanban_sidecar

    completed: list[dict] = []
    dispatched = []

    class FakeKanbanDb:
        @staticmethod
        def connect(*, board=None):
            assert board == "ops"
            return SimpleNamespace(close=lambda: None)

        @staticmethod
        def complete_task(conn, task_id, *, result=None, summary=None, metadata=None):
            completed.append({
                "task_id": task_id,
                "result": result,
                "summary": summary,
                "metadata": metadata,
            })
            return True

    async def dispatch_agent(request):
        dispatched.append(request)
        return "KANBAN_RUNBROKER_OK"

    config = kanban_sidecar.KanbanSidecarConfig(
        enabled=True,
        board="ops",
        sidecar_profile="kanban_orchestrator",
        allowed_task_profiles=("worker_a",),
        profile_user_keys={"worker_a": "ou_worker_a"},
        delivery_mode="webui",
    )
    spawn = kanban_sidecar.build_run_broker_spawn(
        config,
        kanban_db=FakeKanbanDb,
        dispatch_agent=dispatch_agent,
        sandbox_available=lambda: True,
    )
    task = SimpleNamespace(
        id="task-live",
        title="Do the work",
        body="Return the marker",
        assignee="worker_a",
        tenant=None,
        created_by="ou_creator",
        current_run_id=7,
    )

    pid = spawn(task, "/tmp/workspace/task-live", board="ops")

    assert pid is None
    assert len(dispatched) == 1
    assert dispatched[0].channel == "kanban"
    assert dispatched[0].profile_name == "worker_a"
    assert dispatched[0].delivery_mode == "webui"
    assert completed == [
        {
            "task_id": "task-live",
            "result": "KANBAN_RUNBROKER_OK",
            "summary": "KANBAN_RUNBROKER_OK",
            "metadata": {
                "channel": "kanban",
                "profile_name": "worker_a",
                "user_key": "ou_worker_a",
                "delivery_mode": "webui",
                "workspace": "/tmp/workspace/task-live",
            },
        }
    ]
