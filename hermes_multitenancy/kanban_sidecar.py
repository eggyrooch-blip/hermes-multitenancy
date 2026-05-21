"""Multitenancy guardrail adapter for upstream Hermes Kanban dispatch.

This module does not start Kanban from the gateway.  It exposes an explicit
sidecar entrypoint that can plan or run one upstream dispatcher pass after the
multitenancy profile boundary has been checked.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import importlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .run_broker import RunBroker
from .run_models import RunRequest


CONFIG_NAME = "kanban-sidecar.yaml"
ROUTER_PROFILE = "multitenancy_router"
_SECRET_KEY_PARTS = ("secret", "token", "password", "api_key", "credential")
_BOARD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class KanbanApiError(ValueError):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class KanbanSidecarConfig:
    enabled: bool = False
    board: str = "default"
    tenant: str | None = None
    sidecar_profile: str | None = None
    allowed_profiles: tuple[str, ...] = ()
    max_spawn: int | None = None
    max_in_progress: int | None = None
    execute: bool = False
    run_broker: bool = True
    allowed_task_profiles: tuple[str, ...] = ()
    profile_user_keys: dict[str, str] | None = None
    delivery_mode: str = "feishu"


def plan_kanban_sidecar(
    *,
    shared_home: str | Path | None = None,
    current_profile: str | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    """Return a secret-free Kanban sidecar plan or one-pass execution result."""

    resolved_shared_home, resolved_profile = _resolve_runtime(shared_home, current_profile)
    config = load_config(resolved_shared_home)
    problems = _guard_problems(config, resolved_profile)

    base = _base_result(
        config,
        shared_home=resolved_shared_home,
        current_profile=resolved_profile,
        problems=problems,
    )

    if not config.enabled:
        base.update(
            {
                "status": "disabled",
                "reason": f"{CONFIG_NAME} missing or enabled=false",
                "would_execute": False,
                "will_execute": False,
            }
        )
        return base

    if problems:
        base.update(
            {
                "status": "blocked",
                "would_execute": False,
                "will_execute": False,
            }
        )
        return base

    will_execute = bool(execute and config.execute)
    if will_execute and not _profile_allowed(config, resolved_profile):
        base.update(
            {
                "status": "blocked",
                "would_execute": False,
                "will_execute": False,
                "problems": [
                    *problems,
                    f"profile {resolved_profile!r} is not in Kanban sidecar allowlist",
                ],
            }
        )
        return base

    dispatch_result = _dispatch_once(config, dry_run=not will_execute)
    base.update(_summarize_dispatch(dispatch_result))
    base.update(
        {
            "status": "executed" if will_execute else "dry_run",
            "would_execute": True,
            "will_execute": will_execute,
        }
    )
    return base


def load_config(shared_home: str | Path) -> KanbanSidecarConfig:
    path = Path(shared_home) / CONFIG_NAME
    if not path.exists():
        return KanbanSidecarConfig()
    raw = _load_yaml_like(path)
    if not isinstance(raw, dict):
        return KanbanSidecarConfig()
    return KanbanSidecarConfig(
        enabled=_bool(raw.get("enabled")),
        board=str(raw.get("board") or "default").strip() or "default",
        tenant=_optional_str(raw.get("tenant")),
        sidecar_profile=_optional_str(raw.get("sidecar_profile")),
        allowed_profiles=tuple(
            str(item).strip()
            for item in _as_list(raw.get("allowed_profiles"))
            if str(item).strip()
        ),
        max_spawn=_optional_int(raw.get("max_spawn")),
        max_in_progress=_optional_int(raw.get("max_in_progress")),
        execute=_bool(raw.get("execute")),
        run_broker=_bool(raw.get("run_broker", True)),
        allowed_task_profiles=tuple(
            str(item).strip()
            for item in _as_list(raw.get("allowed_task_profiles"))
            if str(item).strip()
        ),
        profile_user_keys={
            str(key).strip(): str(value).strip()
            for key, value in (raw.get("profile_user_keys") or {}).items()
            if str(key).strip() and str(value).strip()
        } if isinstance(raw.get("profile_user_keys"), dict) else None,
        delivery_mode=str(raw.get("delivery_mode") or "feishu").strip() or "feishu",
    )


def list_owner_assignees(*, owner_open_id: str, board: str = "default") -> list[dict[str, Any]]:
    """Return only Kanban assignees backed by the asserted owner's routes."""
    _owned_rows, owned_profiles, _owner_root = _owner_scope(owner_open_id)
    tasks = _list_owner_tasks(owner_open_id=owner_open_id, board=board)
    counts: dict[str, dict[str, int]] = {}
    for task in tasks:
        assignee = str(_get(task, "assignee", "") or "").strip()
        if not assignee:
            continue
        status = str(_get(task, "status", "") or "").strip() or "unknown"
        counts.setdefault(assignee, {})
        counts[assignee][status] = counts[assignee].get(status, 0) + 1

    result = []
    for row in _owned_rows:
        name = str(getattr(row, "profile_name", "") or "").strip()
        if not name or name not in owned_profiles:
            continue
        result.append(
            {
                "name": name,
                "display_label": getattr(row, "display_label", None) or name,
                "agent_id": getattr(row, "agent_id", None),
                "kind": getattr(row, "kind", None),
                "on_disk": True,
                "counts": counts.get(name, {}),
            }
        )
    return result


def list_owner_tasks(
    *,
    owner_open_id: str,
    board: str = "default",
    status: str | None = None,
    assignee: str | None = None,
    tenant: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """List Kanban tasks visible to the asserted owner."""
    tasks = _list_owner_tasks(
        owner_open_id=owner_open_id,
        board=board,
        status=status,
        assignee=assignee,
        tenant=tenant,
        include_archived=include_archived,
    )
    return [_task_to_dict(task) for task in tasks]


def create_owner_task(
    *,
    owner_open_id: str,
    payload: dict[str, Any],
    board: str = "default",
) -> dict[str, Any]:
    """Create a Kanban task under the asserted owner boundary."""
    _rows, owned_profiles, owner_root = _owner_scope(owner_open_id)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise KanbanApiError("title is required", status=400)
    assignee = _optional_text(payload.get("assignee"))
    if assignee is not None and assignee not in owned_profiles:
        raise KanbanApiError(f"assignee {assignee!r} is not accessible for asserted owner", status=403)
    priority = payload.get("priority", 0)
    if priority in (None, ""):
        priority = 0
    if not isinstance(priority, int):
        raise KanbanApiError("priority must be an integer", status=400)
    normalized_board = normalize_board_slug(board)
    kanban_db = importlib.import_module("hermes_cli.kanban_db")
    conn = kanban_db.connect(board=normalized_board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title=title,
            body=_optional_text(payload.get("body")),
            assignee=assignee,
            created_by=str(owner_root.profile_name),
            tenant=owner_open_id,
            priority=priority,
            board=normalized_board,
        )
        task = kanban_db.get_task(conn, task_id)
    finally:
        _close_conn(conn)
    return _task_to_dict(task) if task is not None else {"id": task_id}


def owner_kanban_stats(*, owner_open_id: str, board: str = "default") -> dict[str, Any]:
    tasks = _list_owner_tasks(owner_open_id=owner_open_id, board=board)
    by_status: dict[str, int] = {}
    by_assignee: dict[str, int] = {}
    for task in tasks:
        status = str(_get(task, "status", "") or "").strip() or "unknown"
        by_status[status] = by_status.get(status, 0) + 1
        assignee = str(_get(task, "assignee", "") or "").strip()
        if assignee:
            by_assignee[assignee] = by_assignee.get(assignee, 0) + 1
    return {"by_status": by_status, "by_assignee": by_assignee, "total": len(tasks)}


def owner_kanban_capabilities() -> dict[str, Any]:
    capabilities = [
        {"key": "explicitBoard", "status": "supported", "canonicalRoute": None, "canonicalCommand": "--board", "requiresBoard": True},
        {"key": "boardsList", "status": "supported", "canonicalRoute": "/boards", "canonicalCommand": "boards list", "requiresBoard": False},
        {"key": "boardCreate", "status": "missing", "reason": "Chat-plane Kanban does not expose board creation", "canonicalRoute": "/boards", "canonicalCommand": "boards create", "requiresBoard": False},
        {"key": "boardArchive", "status": "missing", "reason": "Chat-plane Kanban does not expose board archive/delete", "canonicalRoute": "/boards/{slug}", "canonicalCommand": "boards rm", "requiresBoard": False},
        {"key": "taskCrudLite", "status": "supported", "canonicalRoute": "/tasks", "canonicalCommand": "list/create", "requiresBoard": True},
        {"key": "commentsWrite", "status": "missing", "reason": "Chat-plane Kanban comment writes remain disabled until owner-scoped sidecar support exists", "canonicalRoute": "/tasks/{task_id}/comments", "canonicalCommand": "comment", "requiresBoard": True},
        {"key": "taskLog", "status": "missing", "reason": "Chat-plane Kanban logs remain disabled until owner-scoped artifact/log support exists", "canonicalRoute": "/tasks/{task_id}/log", "canonicalCommand": "log", "requiresBoard": True},
        {"key": "diagnostics", "status": "missing", "reason": "Chat-plane Kanban diagnostics remain disabled until owner-scoped sidecar support exists", "canonicalRoute": "/diagnostics", "canonicalCommand": "diagnostics", "requiresBoard": True},
        {"key": "reclaim", "status": "missing", "reason": "Chat-plane Kanban reclaim remains disabled until owner-scoped sidecar support exists", "canonicalRoute": "/tasks/{task_id}/reclaim", "canonicalCommand": "reclaim", "requiresBoard": True},
        {"key": "reassign", "status": "missing", "reason": "Chat-plane Kanban reassign remains disabled until owner-scoped sidecar support exists", "canonicalRoute": "/tasks/{task_id}/reassign", "canonicalCommand": "reassign", "requiresBoard": True},
        {"key": "specify", "status": "missing", "reason": "Chat-plane Kanban specify remains disabled until owner-scoped sidecar support exists", "canonicalRoute": "/tasks/{task_id}/specify", "canonicalCommand": "specify", "requiresBoard": True},
        {"key": "dispatch", "status": "supported", "canonicalRoute": "/dispatch", "canonicalCommand": "dispatch via RunBroker", "requiresBoard": True},
        {"key": "links", "status": "missing", "reason": "Chat-plane Kanban links remain disabled until owner-scoped sidecar support exists", "canonicalRoute": "/links", "canonicalCommand": "link/unlink", "requiresBoard": True},
        {"key": "bulk", "status": "missing", "reason": "Chat-plane Kanban bulk writes remain disabled until owner-scoped sidecar support exists", "canonicalRoute": "/tasks/bulk", "canonicalCommand": "bulk", "requiresBoard": True},
        {"key": "events", "status": "missing", "reason": "Disabled in chat-plane because WebSocket upgrade cannot enforce per-openid owner filtering", "canonicalRoute": "/events", "canonicalCommand": "watch", "requiresBoard": True},
    ]
    supports = {item["key"]: item["status"] == "supported" for item in capabilities}
    missing = [item["key"] for item in capabilities if item["status"] != "supported"]
    return {"source": "hermes-multitenancy-run-broker", "supports": supports, "missing": missing, "capabilities": capabilities}


def list_owner_boards(*, owner_open_id: str, include_archived: bool = False) -> list[dict[str, Any]]:
    kanban_db = importlib.import_module("hermes_cli.kanban_db")
    boards = list(kanban_db.list_boards(include_archived=include_archived))
    result = []
    for board in boards:
        slug = normalize_board_slug(str(board.get("slug") or "default"))
        stats = owner_kanban_stats(owner_open_id=owner_open_id, board=slug)
        if slug != "default" and stats["total"] == 0:
            continue
        safe = dict(board)
        safe["counts"] = stats["by_status"]
        safe["total"] = stats["total"]
        result.append(safe)
    if not result:
        result.append(
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
        )
    return result


def dispatch_owner_kanban(
    *,
    owner_open_id: str,
    board: str = "default",
    dry_run: bool = True,
    max_spawn: int | None = None,
    max_in_progress: int | None = None,
) -> dict[str, Any]:
    """Run an owner-bounded dispatch pass through the sidecar guardrails."""
    _rows, owned_profiles, _owner_root = _owner_scope(owner_open_id)
    normalized_board = normalize_board_slug(board)
    all_tasks = _list_all_tasks(board=normalized_board, include_archived=True)
    foreign = [
        str(_get(task, "id", "") or "")
        for task in all_tasks
        if not _task_visible_to_owner(task, owned_profiles=owned_profiles, owner_open_id=owner_open_id)
    ]
    if foreign:
        raise KanbanApiError("Kanban dispatch is disabled while this board contains tasks owned by other owners", status=403)

    config = KanbanSidecarConfig(
        enabled=True,
        board=normalized_board,
        tenant=owner_open_id,
        allowed_task_profiles=tuple(sorted(owned_profiles)),
        profile_user_keys={profile: owner_open_id for profile in owned_profiles},
        max_spawn=max_spawn,
        max_in_progress=max_in_progress,
        execute=True,
        run_broker=True,
        delivery_mode="feishu",
    )
    result = _dispatch_once(config, dry_run=bool(dry_run))
    summary = _summarize_dispatch(result)
    return {
        "status": "dry_run" if dry_run else "executed",
        "board": normalized_board,
        "tenant": owner_open_id,
        "run_broker": True,
        "allowed_task_profiles": sorted(owned_profiles),
        **summary,
    }


def normalize_board_slug(board: str | None) -> str:
    if board is None:
        return "default"
    slug = str(board).strip().lower()
    if not slug or not _BOARD_SLUG_RE.match(slug):
        raise KanbanApiError("invalid board slug", status=400)
    return slug


def _owner_scope(owner_open_id: str) -> tuple[list[Any], set[str], Any]:
    owner = str(owner_open_id or "").strip()
    if not owner:
        raise KanbanApiError("owner identity required (X-Hermes-Owner-Open-Id)", status=403)

    from . import router as router_mod

    table = router_mod._get_routing_table()
    if table is None:
        raise KanbanApiError("trusted owner header requires routing table verification", status=403)
    owner_root = table.resolve_owner_root(owner)
    if owner_root is None:
        raise KanbanApiError(f"asserted owner {owner!r} has no sync-root profile", status=403)

    rows: list[Any] = [owner_root]
    seen = {str(owner_root.profile_name)}
    for row in table.list_by_owner(owner):
        profile_name = str(getattr(row, "profile_name", "") or "").strip()
        if profile_name and profile_name not in seen:
            rows.append(row)
            seen.add(profile_name)
    return rows, seen, owner_root


def _list_owner_tasks(
    *,
    owner_open_id: str,
    board: str,
    status: str | None = None,
    assignee: str | None = None,
    tenant: str | None = None,
    include_archived: bool = False,
) -> list[Any]:
    _rows, owned_profiles, _owner_root = _owner_scope(owner_open_id)
    if assignee is not None and assignee not in owned_profiles:
        return []
    if tenant is not None and tenant != owner_open_id:
        return []
    tasks = _list_all_tasks(
        board=board,
        status=status,
        assignee=assignee,
        tenant=tenant,
        include_archived=include_archived,
    )
    return [
        task
        for task in tasks
        if _task_visible_to_owner(task, owned_profiles=owned_profiles, owner_open_id=owner_open_id)
    ]


def _list_all_tasks(
    *,
    board: str,
    status: str | None = None,
    assignee: str | None = None,
    tenant: str | None = None,
    include_archived: bool = False,
) -> list[Any]:
    normalized_board = normalize_board_slug(board)
    kanban_db = importlib.import_module("hermes_cli.kanban_db")
    conn = kanban_db.connect(board=normalized_board)
    try:
        return list(
            kanban_db.list_tasks(
                conn,
                status=status,
                assignee=assignee,
                tenant=tenant,
                include_archived=include_archived,
            )
        )
    finally:
        _close_conn(conn)


def _task_visible_to_owner(
    task: Any,
    *,
    owned_profiles: set[str],
    owner_open_id: str,
) -> bool:
    created_by = str(_get(task, "created_by", "") or "").strip()
    assignee = str(_get(task, "assignee", "") or "").strip()
    tenant = str(_get(task, "tenant", "") or "").strip()

    # Treat conflicting attribution as foreign instead of relying on a single
    # positive signal. Old Kanban rows can be hand-edited or created by older
    # code paths; dispatch must refuse mixed-owner boards before RunBroker
    # admission, not discover an alien assignee at execution time.
    if tenant and tenant != owner_open_id:
        return False
    if assignee and assignee not in owned_profiles:
        return False
    if created_by and created_by not in owned_profiles and created_by != owner_open_id:
        return False

    return (
        bool(created_by)
        or bool(assignee)
        or tenant == owner_open_id
    )


def _task_to_dict(task: Any) -> dict[str, Any]:
    if task is None:
        return {}
    if isinstance(task, dict):
        raw = dict(task)
    elif hasattr(task, "__dict__"):
        raw = {
            key: value
            for key, value in vars(task).items()
            if not key.startswith("_") and _is_json_scalar_or_list(value)
        }
    else:
        raw = {"id": str(task)}
    fields = [
        "id",
        "title",
        "body",
        "assignee",
        "status",
        "priority",
        "created_by",
        "created_at",
        "started_at",
        "completed_at",
        "workspace_kind",
        "workspace_path",
        "tenant",
        "result",
        "skills",
    ]
    return {key: raw.get(key) for key in fields if key in raw}


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_json_scalar_or_list(value: Any) -> bool:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return True
    if isinstance(value, list):
        return all(_is_json_scalar_or_list(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_scalar_or_list(item) for key, item in value.items())
    return False


def _close_conn(conn: Any) -> None:
    try:
        conn.close()
    except Exception:
        pass


def _resolve_runtime(
    shared_home: str | Path | None,
    current_profile: str | None,
) -> tuple[Path, str | None]:
    if shared_home is not None:
        return Path(shared_home).expanduser().resolve(), current_profile

    hermes_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser().resolve()
    profile = current_profile or os.environ.get("HERMES_PROFILE")
    if hermes_home.parent.name == "profiles":
        profile = profile or hermes_home.name
        return hermes_home.parent.parent, profile
    return hermes_home, profile


def _base_result(
    config: KanbanSidecarConfig,
    *,
    shared_home: Path,
    current_profile: str | None,
    problems: list[str],
) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "status": "unknown",
        "shared_home": str(shared_home),
        "board": config.board,
        "tenant": config.tenant,
        "current_profile": current_profile,
        "sidecar_profile": config.sidecar_profile,
        "allowed_profiles": list(config.allowed_profiles),
        "execute_configured": config.execute,
        "max_spawn": config.max_spawn,
        "max_in_progress": config.max_in_progress,
        "run_broker": config.run_broker,
        "allowed_task_profiles": list(config.allowed_task_profiles),
        "delivery_mode": config.delivery_mode,
        "problems": problems,
        "summary": {},
        "spawned": [],
        "secret_free": True,
    }


def _guard_problems(config: KanbanSidecarConfig, current_profile: str | None) -> list[str]:
    problems: list[str] = []
    if current_profile == ROUTER_PROFILE:
        problems.append("router profile must not run Kanban sidecar dispatch")
    if config.sidecar_profile == ROUTER_PROFILE:
        problems.append("router profile must not be configured as Kanban sidecar profile")
    if ROUTER_PROFILE in config.allowed_profiles:
        problems.append("router profile must not be allowlisted for Kanban sidecar dispatch")
    return problems


def _profile_allowed(config: KanbanSidecarConfig, current_profile: str | None) -> bool:
    if current_profile is None:
        return False
    if config.sidecar_profile and current_profile != config.sidecar_profile:
        return False
    if config.allowed_profiles and current_profile not in config.allowed_profiles:
        return False
    return True


def _dispatch_once(config: KanbanSidecarConfig, *, dry_run: bool) -> Any:
    kanban_db = importlib.import_module("hermes_cli.kanban_db")
    kwargs: dict[str, Any] = {
        "board": config.board,
        "dry_run": dry_run,
        "max_spawn": config.max_spawn,
        "max_in_progress": config.max_in_progress,
    }
    if config.run_broker and not dry_run:
        kwargs["spawn_fn"] = build_run_broker_spawn(config, kanban_db=kanban_db)

    dispatch_once = kanban_db.dispatch_once
    try:
        sig = inspect.signature(dispatch_once)
        needs_conn = "conn" in sig.parameters
    except (TypeError, ValueError):
        needs_conn = False
    if not needs_conn:
        return dispatch_once(**kwargs)

    conn = kanban_db.connect(board=config.board)
    try:
        return dispatch_once(conn, **kwargs)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def build_run_request_for_task(
    task: Any,
    *,
    config: KanbanSidecarConfig,
    workspace: str,
) -> RunRequest:
    assignee = str(getattr(task, "assignee", "") or "").strip()
    if not assignee:
        raise ValueError(f"kanban task {getattr(task, 'id', '<unknown>')} has no assignee")
    if assignee == ROUTER_PROFILE:
        raise ValueError("router profile must not execute Kanban tasks")
    if config.allowed_task_profiles and assignee not in config.allowed_task_profiles:
        raise ValueError(f"kanban task assignee {assignee!r} is not allowlisted")

    task_id = str(getattr(task, "id", "") or "").strip()
    title = str(getattr(task, "title", "") or "").strip()
    body = str(getattr(task, "body", "") or "").strip()
    user_key = _task_user_key(task, config)
    content = f"Kanban task {task_id}: {title}".strip()
    if body:
        content += f"\n\n{body}"

    metadata = {
        "kanban_task_id": task_id,
        "kanban_board": config.board,
        "kanban_tenant": getattr(task, "tenant", None),
        "kanban_run_id": getattr(task, "current_run_id", None),
        "kanban_workspace": str(workspace),
        "kanban_assignee": assignee,
        "allowed_task_profiles": list(config.allowed_task_profiles),
    }
    return RunRequest(
        channel="kanban",
        profile_name=assignee,
        user_key=user_key,
        content=content,
        chat_id=f"kanban:{config.board}",
        session_id=f"kanban:{config.board}:{task_id}",
        message_id=task_id,
        idempotency_key=f"kanban:{config.board}:{task_id}:{getattr(task, 'current_run_id', '')}",
        delivery_mode=config.delivery_mode,
        credential_subject=user_key,
        requires_host_tools=True,
        metadata=metadata,
    )


def build_run_broker_spawn(
    config: KanbanSidecarConfig,
    *,
    kanban_db: Any,
    dispatch_agent: Any | None = None,
    sandbox_available: Any | None = None,
):
    """Return an upstream Kanban ``spawn_fn`` that executes via RunBroker."""

    async def default_dispatch(request: RunRequest) -> str:
        from types import SimpleNamespace
        from .agent_real import real_run_agent

        shared_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
        if shared_home.name != request.profile_name and (shared_home / "profiles").exists():
            profile_home = shared_home / "profiles" / request.profile_name
        elif shared_home.parent.name == "profiles":
            profile_home = shared_home.parent / request.profile_name
        else:
            profile_home = shared_home / "profiles" / request.profile_name
        event = SimpleNamespace(
            text=request.content,
            message_id=request.message_id,
            source=SimpleNamespace(
                platform=SimpleNamespace(value="kanban"),
                chat_id=request.chat_id or "",
                chat_name="Kanban",
                chat_type="kanban",
                user_id=request.user_key,
                user_name=request.user_key,
                user_id_alt=request.user_key,
                message_id=request.message_id,
            ),
        )
        return await real_run_agent(event, profile_home)

    async def run_request(request: RunRequest) -> str:
        broker = RunBroker(
            dispatch_agent=dispatch_agent or default_dispatch,
            sandbox_available=sandbox_available,
        )
        result = await broker.run(request)
        return result.content

    def spawn(task: Any, workspace: str, *, board: str | None = None) -> None:
        request = build_run_request_for_task(task, config=config, workspace=workspace)
        content = asyncio.run(run_request(request))
        conn = kanban_db.connect(board=board or config.board)
        try:
            kanban_db.complete_task(
                conn,
                request.metadata["kanban_task_id"],
                result=content,
                summary=_first_line(content),
                metadata={
                    "channel": request.channel,
                    "profile_name": request.profile_name,
                    "user_key": request.user_key,
                    "delivery_mode": request.delivery_mode,
                    "workspace": workspace,
                },
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return None

    return spawn


def _task_user_key(task: Any, config: KanbanSidecarConfig) -> str:
    assignee = str(getattr(task, "assignee", "") or "").strip()
    profile_user_keys = config.profile_user_keys or {}
    user_key = str(profile_user_keys.get(assignee) or "").strip()
    if not user_key:
        user_key = str(getattr(task, "created_by", "") or getattr(task, "tenant", "") or "").strip()
    if not user_key:
        raise ValueError(f"kanban task {getattr(task, 'id', '<unknown>')} has no user_key/created_by")
    return user_key


def _first_line(text: str) -> str:
    return str(text or "").strip().splitlines()[0][:400] if str(text or "").strip() else ""


def _summarize_dispatch(result: Any) -> dict[str, Any]:
    fields = {
        "reclaimed": _get(result, "reclaimed", []),
        "promoted": _get(result, "promoted", []),
        "spawned": _get(result, "spawned", []),
        "skipped_unassigned": _get(result, "skipped_unassigned", []),
        "skipped_nonspawnable": _get(result, "skipped_nonspawnable", []),
        "crashed": _get(result, "crashed", []),
        "auto_blocked": _get(result, "auto_blocked", []),
        "timed_out": _get(result, "timed_out", []),
        "stale": _get(result, "stale", []),
        "respawn_guarded": _get(result, "respawn_guarded", []),
    }
    safe_fields = {key: _safe_value(value) for key, value in fields.items()}
    summary = {
        f"{key}_count": _count(value)
        for key, value in safe_fields.items()
    }
    return {
        **safe_fields,
        "summary": summary,
    }


def _get(value: Any, key: str, default: Any) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (str, bytes, dict)):
        return 1
    try:
        return len(value)
    except TypeError:
        return int(value) if isinstance(value, int) else 1


def _safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.lower() for part in _SECRET_KEY_PARTS):
                safe[key_text] = "<redacted>"
            else:
                safe[key_text] = _safe_value(item)
        return safe
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _load_yaml_like(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except Exception:
        return _parse_simple_yaml(text)


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_list_key: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- ") and current_list_key:
            data.setdefault(current_list_key, []).append(_scalar(line[2:].strip()))
            continue
        if ":" not in line:
            current_list_key = None
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not value:
            data[key] = []
            current_list_key = key
        else:
            data[key] = _scalar(value)
            current_list_key = None
    return data


def _scalar(value: str) -> Any:
    value = value.strip().strip("\"'")
    lower = value.lower()
    if lower in {"true", "yes", "on"}:
        return True
    if lower in {"false", "no", "off"}:
        return False
    if lower in {"null", "none", "~"}:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or run one multitenancy Kanban sidecar pass")
    parser.add_argument("--shared-home", type=Path, default=None)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    result = plan_kanban_sidecar(
        shared_home=args.shared_home,
        current_profile=args.profile,
        execute=args.execute,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] not in {"blocked"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
