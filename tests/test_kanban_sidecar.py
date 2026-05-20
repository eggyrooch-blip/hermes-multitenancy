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
    assert calls == [
        {
            "board": "ops",
            "dry_run": True,
            "max_spawn": None,
            "max_in_progress": None,
        },
        {
            "board": "ops",
            "dry_run": False,
            "max_spawn": None,
            "max_in_progress": None,
        },
    ]
