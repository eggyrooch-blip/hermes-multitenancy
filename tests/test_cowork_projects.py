from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest


def test_project_store_freezes_session_context_and_isolates_actor(tmp_path):
    from hermes_multitenancy.projects import ProjectConflict, ProjectNotFound, ProjectStore

    profile = tmp_path / "profiles" / "alice"
    (profile / "workspace" / "repo-a").mkdir(parents=True)
    store = ProjectStore(profile)

    project = store.create_project(
        "actor-a",
        {
            "name": "Alpha",
            "description": "Validate context, continuity, and artifacts.",
            "instructions": "Use Alpha rules",
            "primary_folder": "repo-a",
        },
    )
    assert store.list_projects("actor-a") == [project]
    assert store.list_projects("actor-b") == []
    with pytest.raises(ProjectNotFound):
        store.get_project("actor-b", project["id"])

    context = store.bind_session(
        actor_subject="actor-a",
        session_id="session-a",
        requested_project_id=project["id"],
        requested_supplied=True,
        requested_workspace=None,
    )
    assert context.project_id == project["id"]
    assert context.workspace == "repo-a"
    assert context.description == "Validate context, continuity, and artifacts."
    assert context.instructions == "Use Alpha rules"
    assert context.skip_memory is True
    assert context.disabled_toolsets == ("memory", "session_search")

    repeated = store.bind_session(
        actor_subject="actor-a",
        session_id="session-a",
        requested_project_id=None,
        requested_supplied=False,
        requested_workspace=None,
    )
    assert repeated == context
    with pytest.raises(ProjectConflict, match="different project"):
        store.bind_session(
            actor_subject="actor-a",
            session_id="session-a",
            requested_project_id="prj_other",
            requested_supplied=True,
            requested_workspace=None,
        )
    with pytest.raises(ProjectNotFound):
        store.get_session_context("actor-b", "session-a")

    store.update_project(
        "actor-a",
        project["id"],
        {"description": "new description", "instructions": "new instructions"},
    )
    assert store.get_session_context("actor-a", "session-a").description == context.description
    assert store.get_session_context("actor-a", "session-a").instructions == "Use Alpha rules"

    new_context = store.bind_session(
        actor_subject="actor-a",
        session_id="session-b",
        requested_project_id=project["id"],
        requested_supplied=True,
        requested_workspace=None,
    )
    assert new_context.description == "new description"
    assert new_context.instructions == "new instructions"

    outside = tmp_path / "outside"
    outside.mkdir()
    (profile / "workspace" / "repo-a").rmdir()
    (profile / "workspace" / "repo-a").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProjectConflict, match="workspace"):
        store.get_session_context("actor-a", "session-a")


def test_project_store_migrates_existing_session_description_column(tmp_path):
    import sqlite3

    from hermes_multitenancy.projects import ProjectStore

    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    db_path = profile / "projects.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE session_projects ("
            "session_id TEXT PRIMARY KEY, owner_hash TEXT NOT NULL, project_id TEXT, "
            "project_name TEXT, workspace TEXT, instructions TEXT NOT NULL DEFAULT '', "
            "instructions_fingerprint TEXT NOT NULL, created_at INTEGER NOT NULL)"
        )

    ProjectStore(profile)

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(session_projects)")}
    assert "description" in columns


def test_project_store_fails_closed_for_paths_archives_and_no_folder(tmp_path):
    from hermes_multitenancy.projects import ProjectConflict, ProjectInvalid, ProjectStore

    profile = tmp_path / "profiles" / "alice"
    workspace = profile / "workspace"
    (workspace / "repo-a").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    store = ProjectStore(profile)

    with pytest.raises(ProjectInvalid, match="invalid workspace"):
        store.create_project("actor-a", {"name": "Escape", "primary_folder": "escape"})
    with pytest.raises(ProjectInvalid, match="invalid workspace"):
        store.create_project("actor-a", {"name": "Absolute", "primary_folder": str(outside)})

    info = store.create_project("actor-a", {"name": "Info only"})
    context = store.bind_session(
        actor_subject="actor-a",
        session_id="session-info",
        requested_project_id=info["id"],
        requested_supplied=True,
        requested_workspace=None,
    )
    assert context.workspace is None
    assert context.disabled_toolsets == (
        "code_execution",
        "delegation",
        "file",
        "memory",
        "session_search",
        "terminal",
    )

    store.archive_project("actor-a", info["id"])
    with pytest.raises(ProjectConflict, match="archived"):
        store.bind_session(
            actor_subject="actor-a",
            session_id="session-info",
            requested_project_id=None,
            requested_supplied=False,
            requested_workspace=None,
        )
    with pytest.raises(ProjectConflict, match="archived"):
        store.get_session_context("actor-a", "session-info")


def test_no_project_session_freezes_existing_workspace_without_memory_changes(tmp_path):
    from hermes_multitenancy.projects import ProjectConflict, ProjectStore

    profile = tmp_path / "profiles" / "alice"
    (profile / "workspace" / "scratch").mkdir(parents=True)
    store = ProjectStore(profile)
    context = store.bind_session(
        actor_subject="actor-a",
        session_id="plain-session",
        requested_project_id=None,
        requested_supplied=False,
        requested_workspace="scratch",
    )
    assert context.project_id is None
    assert context.workspace == "scratch"
    assert context.skip_memory is False
    assert context.disabled_toolsets == ()
    (profile / "workspace" / "other").mkdir()
    with pytest.raises(ProjectConflict, match="workspace"):
        store.bind_session(
            actor_subject="actor-a",
            session_id="plain-session",
            requested_project_id=None,
            requested_supplied=False,
            requested_workspace="other",
        )


def test_context_receipt_redacts_actor_and_instruction_text(tmp_path):
    from hermes_multitenancy.projects import ProjectStore

    profile = tmp_path / "profiles" / "alice"
    (profile / "workspace" / "repo-a").mkdir(parents=True)
    store = ProjectStore(profile)
    project = store.create_project(
        "actor-secret",
        {"name": "Alpha", "instructions": "private instruction", "primary_folder": "repo-a"},
    )
    context = store.bind_session(
        actor_subject="actor-secret",
        session_id="session-a",
        requested_project_id=project["id"],
        requested_supplied=True,
        requested_workspace=None,
    )
    receipt = context.receipt()
    serialized = str(receipt)
    assert "actor-secret" not in serialized
    assert "private instruction" not in serialized
    assert str(profile) not in serialized
    assert receipt["project_id"] == project["id"]
    assert receipt["workspace"] == "repo-a"
    assert receipt["memory_enabled"] is False
    assert receipt["session_search_enabled"] is False


def test_concurrent_first_bind_freezes_exactly_one_project(tmp_path):
    from hermes_multitenancy.projects import ProjectConflict, ProjectStore

    profile = tmp_path / "profiles" / "alice"
    (profile / "workspace" / "a").mkdir(parents=True)
    (profile / "workspace" / "b").mkdir()
    store = ProjectStore(profile)
    projects = [
        store.create_project("actor-a", {"name": name, "primary_folder": name.lower()})
        for name in ("A", "B")
    ]
    barrier = Barrier(2)

    def bind(project_id: str):
        barrier.wait()
        try:
            return store.bind_session(
                actor_subject="actor-a",
                session_id="shared-session",
                requested_project_id=project_id,
                requested_supplied=True,
                requested_workspace=None,
            ).project_id
        except ProjectConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(bind, [project["id"] for project in projects]))
    assert results.count("conflict") == 1
    assert store.get_session_context("actor-a", "shared-session").project_id in {
        project["id"] for project in projects
    }
