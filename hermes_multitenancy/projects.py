"""Profile-scoped Hermes Projects without modifying upstream hermes-agent."""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from .run_models import resolve_profile_workspace


class ProjectError(ValueError):
    status = 400


class ProjectInvalid(ProjectError):
    pass


class ProjectNotFound(ProjectError):
    status = 404


class ProjectConflict(ProjectError):
    status = 409


def _clean_text(value: Any, *, name: str, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ProjectInvalid(f"{name} must be a string")
    text = value.strip()
    if required and not text:
        raise ProjectInvalid(f"{name} is required")
    if len(text) > limit:
        raise ProjectInvalid(f"{name} is too long")
    return text


def _actor_hash(actor_subject: str) -> str:
    actor = str(actor_subject or "").strip()
    if not actor:
        raise ProjectInvalid("verified owner identity is required")
    return hashlib.sha256(actor.encode("utf-8")).hexdigest()


def _instruction_fingerprint(instructions: str) -> str:
    return hashlib.sha256(instructions.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProjectContext:
    session_id: str
    project_id: str | None
    project_name: str | None
    description: str
    workspace: str | None
    instructions: str
    instructions_fingerprint: str
    skip_memory: bool
    disabled_toolsets: tuple[str, ...]

    def receipt(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "workspace": self.workspace,
            "instructions_fingerprint": self.instructions_fingerprint,
            "memory_enabled": not self.skip_memory,
            "session_search_enabled": "session_search" not in self.disabled_toolsets,
            "folder_bound": bool(self.workspace) if self.project_id else None,
        }


class ProjectStore:
    def __init__(self, profile_home: Path):
        self.profile_home = Path(profile_home)
        self.profile_home.mkdir(parents=True, exist_ok=True)
        self.db_path = self.profile_home / "projects.db"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    owner_hash TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    instructions TEXT NOT NULL DEFAULT '',
                    icon TEXT NOT NULL DEFAULT '',
                    color TEXT NOT NULL DEFAULT '',
                    primary_folder TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_projects_owner_status
                    ON projects(owner_hash, status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS session_projects (
                    session_id TEXT PRIMARY KEY,
                    owner_hash TEXT NOT NULL,
                    project_id TEXT,
                    project_name TEXT,
                    description TEXT NOT NULL DEFAULT '',
                    workspace TEXT,
                    instructions TEXT NOT NULL DEFAULT '',
                    instructions_fingerprint TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id)
                );
                CREATE INDEX IF NOT EXISTS idx_session_projects_owner_project
                    ON session_projects(owner_hash, project_id, created_at DESC);
                """
            )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(session_projects)")
            }
            if "description" not in columns:
                conn.execute(
                    "ALTER TABLE session_projects ADD COLUMN description TEXT NOT NULL DEFAULT ''"
                )

    def _normalize_folder(self, value: Any) -> str | None:
        if value in {None, ""}:
            return None
        folder = _clean_text(value, name="primary_folder", limit=512, required=True)
        try:
            normalized, _ = resolve_profile_workspace(self.profile_home, folder)
        except ValueError as exc:
            raise ProjectInvalid("invalid workspace") from exc
        if normalized is None:
            raise ProjectInvalid("primary_folder must be below the workspace root")
        return normalized

    @staticmethod
    def _project(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "instructions": row["instructions"],
            "icon": row["icon"],
            "color": row["color"],
            "primary_folder": row["primary_folder"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_project(self, actor_subject: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ProjectInvalid("request body must be an object")
        owner = _actor_hash(actor_subject)
        name = _clean_text(payload.get("name"), name="name", limit=80, required=True)
        description = _clean_text(payload.get("description"), name="description", limit=2000)
        instructions = _clean_text(payload.get("instructions"), name="instructions", limit=8000)
        icon = _clean_text(payload.get("icon"), name="icon", limit=32)
        color = _clean_text(payload.get("color"), name="color", limit=32)
        folder = self._normalize_folder(payload.get("primary_folder"))
        now = int(time.time() * 1000)
        project_id = f"prj_{secrets.token_urlsafe(12)}"
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO projects "
                "(id, owner_hash, name, description, instructions, icon, color, primary_folder, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (project_id, owner, name, description, instructions, icon, color, folder, now, now),
            )
        return self.get_project(actor_subject, project_id)

    def list_projects(self, actor_subject: str, *, include_archived: bool = False) -> list[dict[str, Any]]:
        owner = _actor_hash(actor_subject)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM projects WHERE owner_hash=? "
                + ("" if include_archived else "AND status='active' ")
                + "ORDER BY updated_at DESC, id",
                (owner,),
            ).fetchall()
        return [self._project(row) for row in rows]

    def get_project(self, actor_subject: str, project_id: str) -> dict[str, Any]:
        owner = _actor_hash(actor_subject)
        project_id = _clean_text(project_id, name="project_id", limit=128, required=True)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE id=? AND owner_hash=?", (project_id, owner)
            ).fetchone()
        if row is None:
            raise ProjectNotFound("project not found")
        return self._project(row)

    def update_project(self, actor_subject: str, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ProjectInvalid("request body must be an object")
        current = self.get_project(actor_subject, project_id)
        if current["status"] != "active":
            raise ProjectConflict("project is archived")
        allowed = {"name", "description", "instructions", "icon", "color", "primary_folder"}
        unknown = set(payload) - allowed
        if unknown:
            raise ProjectInvalid(f"unsupported fields: {', '.join(sorted(unknown))}")
        values = dict(current)
        if "name" in payload:
            values["name"] = _clean_text(payload["name"], name="name", limit=80, required=True)
        for key, limit in (("description", 2000), ("instructions", 8000), ("icon", 32), ("color", 32)):
            if key in payload:
                values[key] = _clean_text(payload[key], name=key, limit=limit)
        if "primary_folder" in payload:
            values["primary_folder"] = self._normalize_folder(payload["primary_folder"])
        now = int(time.time() * 1000)
        owner = _actor_hash(actor_subject)
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET name=?, description=?, instructions=?, icon=?, color=?, primary_folder=?, updated_at=? "
                "WHERE id=? AND owner_hash=? AND status='active'",
                (
                    values["name"], values["description"], values["instructions"], values["icon"],
                    values["color"], values["primary_folder"], now, project_id, owner,
                ),
            )
        return self.get_project(actor_subject, project_id)

    def archive_project(self, actor_subject: str, project_id: str) -> dict[str, Any]:
        self.get_project(actor_subject, project_id)
        owner = _actor_hash(actor_subject)
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET status='archived', updated_at=? WHERE id=? AND owner_hash=?",
                (int(time.time() * 1000), project_id, owner),
            )
        return self.get_project(actor_subject, project_id)

    def _context_from_row(self, row: sqlite3.Row) -> ProjectContext:
        project_id = row["project_id"]
        workspace = row["workspace"]
        disabled = {"memory", "session_search"} if project_id else set()
        if project_id and not workspace:
            disabled.update({"code_execution", "delegation", "file", "terminal"})
        return ProjectContext(
            session_id=row["session_id"],
            project_id=project_id,
            project_name=row["project_name"],
            description=row["description"],
            workspace=workspace,
            instructions=row["instructions"],
            instructions_fingerprint=row["instructions_fingerprint"],
            skip_memory=bool(project_id),
            disabled_toolsets=tuple(sorted(disabled)),
        )

    def _validated_context_from_row(self, row: sqlite3.Row) -> ProjectContext:
        if row["project_id"] and row["project_status"] != "active":
            raise ProjectConflict("project is archived")
        if row["workspace"]:
            try:
                normalized, _ = resolve_profile_workspace(self.profile_home, row["workspace"])
            except ValueError as exc:
                raise ProjectConflict("project workspace is unavailable") from exc
            if normalized != row["workspace"]:
                raise ProjectConflict("project workspace is unavailable")
        return self._context_from_row(row)

    def bind_session(
        self,
        *,
        actor_subject: str,
        session_id: str,
        requested_project_id: str | None,
        requested_supplied: bool,
        requested_workspace: str | None,
    ) -> ProjectContext:
        owner = _actor_hash(actor_subject)
        session_id = _clean_text(session_id, name="session_id", limit=256, required=True)
        project_id = None
        if requested_supplied:
            project_id = _clean_text(
                requested_project_id, name="project_id", limit=128, required=True
            )
        requested_normalized = None
        if requested_workspace not in {None, ""}:
            try:
                requested_normalized, _ = resolve_profile_workspace(
                    self.profile_home, requested_workspace
                )
            except ValueError as exc:
                raise ProjectInvalid("invalid workspace") from exc

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT sp.*, p.status AS project_status FROM session_projects sp "
                "LEFT JOIN projects p ON p.id=sp.project_id WHERE sp.session_id=?",
                (session_id,),
            ).fetchone()
            if existing is not None:
                if existing["owner_hash"] != owner:
                    raise ProjectNotFound("session project not found")
                if requested_supplied and project_id != existing["project_id"]:
                    raise ProjectConflict("session is already bound to a different project")
                if requested_normalized is not None and requested_normalized != existing["workspace"]:
                    raise ProjectConflict("session is already bound to a different workspace")
                conn.commit()
                return self._validated_context_from_row(existing)

            project = None
            if project_id:
                project = conn.execute(
                    "SELECT * FROM projects WHERE id=? AND owner_hash=?", (project_id, owner)
                ).fetchone()
                if project is None:
                    raise ProjectNotFound("project not found")
                if project["status"] != "active":
                    raise ProjectConflict("project is archived")
                workspace = project["primary_folder"]
                if requested_normalized is not None and requested_normalized != workspace:
                    raise ProjectConflict("requested workspace does not match project")
                if workspace:
                    try:
                        normalized, _ = resolve_profile_workspace(self.profile_home, workspace)
                    except ValueError as exc:
                        raise ProjectConflict("project workspace is unavailable") from exc
                    if normalized != workspace:
                        raise ProjectConflict("project workspace is unavailable")
                name = project["name"]
                description = str(project["description"] or "")
                instructions = project["instructions"]
            else:
                workspace = requested_normalized
                name = None
                description = ""
                instructions = ""
            fingerprint = _instruction_fingerprint(instructions)
            conn.execute(
                "INSERT INTO session_projects "
                "(session_id, owner_hash, project_id, project_name, description, workspace, instructions, instructions_fingerprint, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, owner, project_id, name, description, workspace, instructions, fingerprint, int(time.time() * 1000)),
            )
            row = conn.execute(
                "SELECT sp.*, p.status AS project_status FROM session_projects sp "
                "LEFT JOIN projects p ON p.id=sp.project_id WHERE sp.session_id=?",
                (session_id,),
            ).fetchone()
            conn.commit()
        assert row is not None
        return self._context_from_row(row)

    def get_session_context(self, actor_subject: str, session_id: str) -> ProjectContext:
        owner = _actor_hash(actor_subject)
        session_id = _clean_text(session_id, name="session_id", limit=256, required=True)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT sp.*, p.status AS project_status FROM session_projects sp "
                "LEFT JOIN projects p ON p.id=sp.project_id "
                "WHERE sp.session_id=? AND sp.owner_hash=?",
                (session_id, owner),
            ).fetchone()
        if row is None:
            raise ProjectNotFound("session project not found")
        return self._validated_context_from_row(row)

    def list_project_sessions(self, actor_subject: str, project_id: str) -> list[dict[str, Any]]:
        self.get_project(actor_subject, project_id)
        owner = _actor_hash(actor_subject)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id, created_at FROM session_projects "
                "WHERE owner_hash=? AND project_id=? ORDER BY created_at DESC",
                (owner, project_id),
            ).fetchall()
        return [{"session_id": row["session_id"], "created_at": row["created_at"]} for row in rows]


def register_routes(
    app: web.Application,
    *,
    authorize: Callable[[Any], bool],
    owner_tenant: Callable[..., tuple[str, str]],
    profile_home: Callable[[str], Path],
) -> None:
    def _identity(request: Any, payload: dict[str, Any] | None = None, *, write: bool = False):
        if not authorize(request):
            raise web.HTTPUnauthorized(text="unauthorized")
        try:
            profile, actor = owner_tenant(request, payload, require_write=write)
        except PermissionError as exc:
            raise web.HTTPForbidden(text="project access denied") from exc
        if not profile or not actor:
            raise web.HTTPForbidden(text="verified owner identity is required")
        return ProjectStore(profile_home(profile)), actor

    async def _payload(request: Any) -> dict[str, Any]:
        try:
            value = await request.json()
        except Exception as exc:
            raise ProjectInvalid("invalid json") from exc
        if not isinstance(value, dict):
            raise ProjectInvalid("request body must be an object")
        return value

    def _error(exc: ProjectError) -> web.Response:
        return web.json_response({"error": str(exc)}, status=exc.status)

    async def list_projects(request: Any):
        try:
            store, actor = _identity(request)
            items = await asyncio.to_thread(
                store.list_projects,
                actor,
                include_archived=request.query.get("include_archived", "").lower() in {"1", "true"},
            )
            return web.json_response({"items": items})
        except ProjectError as exc:
            return _error(exc)

    async def create_project(request: Any):
        try:
            payload = await _payload(request)
            store, actor = _identity(request, payload, write=True)
            return web.json_response(
                {"project": await asyncio.to_thread(store.create_project, actor, payload)}, status=201
            )
        except ProjectError as exc:
            return _error(exc)

    async def get_project(request: Any):
        try:
            store, actor = _identity(request)
            project = await asyncio.to_thread(store.get_project, actor, request.match_info["project_id"])
            return web.json_response({"project": project})
        except ProjectError as exc:
            return _error(exc)

    async def update_project(request: Any):
        try:
            payload = await _payload(request)
            store, actor = _identity(request, payload, write=True)
            project = await asyncio.to_thread(
                store.update_project, actor, request.match_info["project_id"], payload
            )
            return web.json_response({"project": project})
        except ProjectError as exc:
            return _error(exc)

    async def archive_project(request: Any):
        try:
            store, actor = _identity(request, {}, write=True)
            project = await asyncio.to_thread(
                store.archive_project, actor, request.match_info["project_id"]
            )
            return web.json_response({"project": project})
        except ProjectError as exc:
            return _error(exc)

    async def project_sessions(request: Any):
        try:
            store, actor = _identity(request)
            items = await asyncio.to_thread(
                store.list_project_sessions, actor, request.match_info["project_id"]
            )
            return web.json_response({"items": items})
        except ProjectError as exc:
            return _error(exc)

    async def session_context(request: Any):
        try:
            store, actor = _identity(request)
            context = await asyncio.to_thread(
                store.get_session_context, actor, request.match_info["session_id"]
            )
            return web.json_response({"receipt": context.receipt()})
        except ProjectError as exc:
            return _error(exc)

    app.router.add_get("/api/run-broker/projects", list_projects)
    app.router.add_post("/api/run-broker/projects", create_project)
    app.router.add_get("/api/run-broker/projects/{project_id}", get_project)
    app.router.add_patch("/api/run-broker/projects/{project_id}", update_project)
    app.router.add_delete("/api/run-broker/projects/{project_id}", archive_project)
    app.router.add_get("/api/run-broker/projects/{project_id}/sessions", project_sessions)
    app.router.add_get("/api/run-broker/sessions/{session_id}/project", session_context)
