"""Feishu Contact v3 org sync for Hermes multitenancy.

This is the Python counterpart to the old OpenClaw ``feishu-sync.js`` pull
phase. It reads the existing Hermes Feishu app config, builds an org snapshot,
syncs Hermes profiles, then reconciles the multitenancy routing table.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from ..routing import DEFAULT_DB_PATH, RoutingTable

from .feishu_hr import UserSpec, apply_users

ORG_SYNC_BEGIN = "<!-- BEGIN HERMES MULTITENANCY ORG SYNC -->"
ORG_SYNC_END = "<!-- END HERMES MULTITENANCY ORG SYNC -->"
DEFAULT_PROFILE_SKILL_CONFIG_NAMES = (
    "profile-skill-defaults.yaml",
    "profile-skill-defaults.yml",
)
SKILL_DISTRIBUTION_CONFIG_NAMES = (
    "skill-distribution.yaml",
    "skill-distribution.yml",
)
SKILL_BUNDLES_CONFIG_NAMES = (
    "skill-bundles.yaml",
    "skill-bundles.yml",
)
MANAGED_SKILL_MANIFEST = ".hermes-managed.json"

_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Subdirectories created inside every profile home. Order is not significant.
#
# The block under "isolation pivot" backs the per-profile env redirect set by
# ``agent_real._build_subprocess_env``: when a skill subprocess sees HOME /
# XDG_CACHE_HOME / TMPDIR pointing inside the profile, these are the
# directories it lands in. Creating them at provision time (mode 0700, applied
# below in ``_sync_one_profile``) means new profiles start out hardened — no
# race where the child subprocess creates them world-readable on first spawn.
_PROFILE_DIRS = [
    "memories",
    "sessions",
    "skills",
    "skins",
    "logs",
    "plans",
    "workspace",
    "cron",
    # isolation pivot — see agent_real._build_subprocess_env
    "home",    # HOME redirect
    "cache",   # XDG_CACHE_HOME
    "config",  # XDG_CONFIG_HOME
    "state",   # XDG_STATE_HOME
    "data",    # XDG_DATA_HOME
    "tmp",     # TMPDIR
    # Profile-scoped secret storage for skills — see hermes_multitenancy.skill_storage
    "tokens",
    # Profile-scoped Feishu UAT (user_access_token) per-open_id JSON files.
    # Rebound at runtime by agent_real._configure_feishu_uat_home so each
    # profile sees only the tokens that belong to its tenant.
    "feishu_uat",
]
_SKILL_SECRET_FILE_NAMES = {".env", ".env.local", ".npmrc", ".netrc", "auth.json", "feishu_uat.json"}
_SKILL_SECRET_NAME_PARTS = ("token", "secret", "credential", "password", "passwd", "apikey", "api_key")
_SKILL_SOURCE_FILE_SUFFIXES = (
    ".py",
    ".pyi",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".rb",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".swift",
    ".php",
    ".pl",
    ".lua",
    ".md",
    ".txt",
)
_SKILL_LINKED_DEP_DIRS = {"node_modules"}
_SKILL_IGNORED_DIRS = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv"}
_SHARED_SKILL_SYMLINK_PREFIXES = ("lark-",)
_AUTO_DISTRIBUTE_SHARED_SKILL_PREFIXES = ("lark-",)
_CHILD_SHAREABLE_TOKEN_POLICIES = {
    "none",
    "no-token",
    "no_token",
    "tokenless",
    "broker",
    "brokered",
    "bot",
}
_RESERVED_PROFILE_NAMES = {
    "hermes", "default", "test", "tmp", "root", "sudo",
    "chat", "model", "gateway", "setup", "whatsapp", "login", "logout",
    "status", "cron", "doctor", "dump", "config", "pairing", "skills",
    "tools", "mcp", "sessions", "insights", "version", "update",
    "uninstall", "profile", "plugins", "honcho", "acp",
}


class FeishuOrgSyncError(RuntimeError):
    """Raised when the Feishu Contact API cannot be pulled successfully."""


@dataclass(frozen=True)
class Department:
    dept_id: str
    name: str
    parent_id: str = "0"
    leader_user_id: Optional[str] = None
    member_count: int = 0


@dataclass(frozen=True)
class DepartmentUser:
    open_id: str
    user_id: Optional[str] = None
    union_id: Optional[str] = None


@dataclass(frozen=True)
class Employee:
    open_id: str
    user_id: Optional[str]
    agent_id: str
    profile_name: str
    name: str
    dept_id: str
    dept_name: str
    leader_user_id: Optional[str]
    is_leader: bool = False
    union_id: Optional[str] = None
    subordinates: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OrgSnapshot:
    version: int
    timestamp: str
    departments: list[Department]
    employees: dict[str, Employee]
    stats: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for employee in data["employees"].values():
            employee["subordinates"] = list(employee.get("subordinates") or [])
        return data


class _VaultFeishuClient:
    """Minimal Contact API client for clean upstream installs without legacy tools."""

    def __init__(self, app_payload: dict[str, Any], *, timeout: float = 10.0) -> None:
        self._app_payload = app_payload
        self._timeout = timeout
        self._tenant_access_token: Optional[str] = None
        domain = str(app_payload.get("domain") or app_payload.get("FEISHU_DOMAIN") or "feishu").strip().lower()
        self._base_url = "https://open.larksuite.com" if domain == "larksuite" else "https://open.feishu.cn"

    @classmethod
    def for_current_home(cls, *, timeout: float = 10.0) -> "_VaultFeishuClient":
        from ..credentials import CredentialStore

        shared_home = _get_current_shared_home()
        store = CredentialStore(shared_home / "multitenancy.db")
        try:
            payload = store.get_secret_for_runtime(
                profile_name="__global__",
                subject_id="feishu_app",
                provider="feishu",
                secret_kind="app",
            )
        finally:
            store.close()
        return cls(payload, timeout=timeout)

    def do_request(
        self,
        method: str,
        uri: str,
        *,
        queries: list[tuple[str, str]] | None = None,
    ) -> tuple[int, str, dict[str, Any]]:
        token = self._get_tenant_access_token()
        url = f"{self._base_url}{uri}"
        if queries:
            url = f"{url}?{urllib.parse.urlencode(queries)}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method=method.upper(),
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310 - fixed Feishu host.
            raw = response.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise FeishuOrgSyncError(f"{method} {uri} returned invalid JSON") from exc
        return (
            int(payload.get("code") or 0),
            str(payload.get("msg") or ""),
            payload.get("data") if isinstance(payload.get("data"), dict) else {},
        )

    def _get_tenant_access_token(self) -> str:
        if not self._tenant_access_token:
            from ..lark_cli_auth_broker import _mint_tenant_access_token

            self._tenant_access_token = _mint_tenant_access_token(self._app_payload, timeout=self._timeout)
        return self._tenant_access_token


class FeishuContactClient:
    """Small Contact v3 adapter over Hermes' existing FeishuClient."""

    def __init__(
        self,
        client: Any,
        *,
        api_delay: float = 0.65,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._api_delay = api_delay
        self._sleep = sleep

    @classmethod
    def for_current_home(cls, *, api_delay: float = 0.65) -> "FeishuContactClient":
        try:
            from tools.feishu_oapi_client import FeishuClient  # type: ignore
        except ModuleNotFoundError as exc:
            if exc.name not in {"tools", "tools.feishu_oapi_client"}:
                raise
            return cls(_VaultFeishuClient.for_current_home(), api_delay=api_delay)
        return cls(FeishuClient.for_tenant(), api_delay=api_delay)

    def fetch_department_detail(self, dept_id: str) -> Optional[Department]:
        data = self._request(
            "GET",
            f"/open-apis/contact/v3/departments/{dept_id}",
            queries=[
                ("department_id_type", "open_department_id"),
                ("user_id_type", "user_id"),
            ],
        )
        raw = data.get("department") or {}
        if not raw:
            return None
        return _department_from_api(raw, parent_id=raw.get("parent_department_id") or "0")

    def fetch_department_children(self, parent_id: str) -> list[Department]:
        departments: list[Department] = []
        page_token = ""
        while True:
            queries = [
                ("page_size", "50"),
                ("department_id_type", "open_department_id"),
                ("user_id_type", "user_id"),
            ]
            if page_token:
                queries.append(("page_token", page_token))
            data = self._request(
                "GET",
                f"/open-apis/contact/v3/departments/{parent_id}/children",
                queries=queries,
            )
            departments.extend(
                _department_from_api(raw, parent_id=parent_id)
                for raw in (data.get("items") or [])
            )
            page_token = str(data.get("page_token") or "")
            if not page_token:
                return departments

    def fetch_department_tree(self, root_id: str = "0") -> list[Department]:
        departments: list[Department] = []
        queue = [root_id]
        while queue:
            parent_id = queue.pop(0)
            children = self.fetch_department_children(parent_id)
            departments.extend(children)
            queue.extend(dept.dept_id for dept in children)
        return departments

    def fetch_department_users(self, dept_id: str) -> list[DepartmentUser]:
        users: list[DepartmentUser] = []
        page_token = ""
        while True:
            queries = [
                ("department_id", dept_id),
                ("department_id_type", "open_department_id"),
                ("user_id_type", "user_id"),
                ("page_size", "50"),
            ]
            if page_token:
                queries.append(("page_token", page_token))
            data = self._request(
                "GET",
                "/open-apis/contact/v3/users/find_by_department",
                queries=queries,
            )
            users.extend(_department_user_from_api(raw) for raw in (data.get("items") or []))
            page_token = str(data.get("page_token") or "")
            if not page_token:
                return users

    def iter_department_user_records(self, dept_id: str) -> list[dict[str, Any]]:
        """Same find_by_department pull as fetch_department_users, but returns the
        RAW user objects (which carry email / enterprise_email that DepartmentUser
        drops). Additive — existing callers are untouched. Used by
        ``fetch_contact_directory`` to build an open_id -> {email, dept} map without
        a second auth path (reuses this client's tenant token)."""
        records: list[dict[str, Any]] = []
        page_token = ""
        while True:
            queries = [
                ("department_id", dept_id),
                ("department_id_type", "open_department_id"),
                ("user_id_type", "user_id"),
                ("page_size", "50"),
            ]
            if page_token:
                queries.append(("page_token", page_token))
            data = self._request(
                "GET",
                "/open-apis/contact/v3/users/find_by_department",
                queries=queries,
            )
            records.extend(raw for raw in (data.get("items") or []) if isinstance(raw, dict))
            page_token = str(data.get("page_token") or "")
            if not page_token:
                return records

    def _request(self, method: str, uri: str, *, queries: list[tuple[str, str]]) -> dict[str, Any]:
        if self._api_delay > 0:
            self._sleep(self._api_delay)
        code, msg, data = self._client.do_request(method, uri, queries=queries)
        if code != 0:
            raise FeishuOrgSyncError(f"{method} {uri} failed: code={code} msg={msg}")
        return data if isinstance(data, dict) else {}


def pull_feishu_org(
    dept_id: Optional[str] = None,
    *,
    client: Any = None,
    api_delay: float = 0.65,
) -> OrgSnapshot:
    """Pull Feishu departments/users and return an org snapshot."""
    contact = _coerce_contact_client(client, api_delay=api_delay)
    root_id = dept_id or "0"
    departments = contact.fetch_department_tree(root_id)
    target = contact.fetch_department_detail(root_id)
    if target and all(dept.dept_id != target.dept_id for dept in departments):
        departments.insert(0, target)

    dept_user_map = {
        dept.dept_id: contact.fetch_department_users(dept.dept_id)
        for dept in departments
    }
    return build_org_snapshot(departments, dept_user_map)


def fetch_contact_directory(
    dept_id: Optional[str] = None,
    *,
    client: Any = None,
    api_delay: float = 0.65,
) -> dict[str, dict[str, str]]:
    """Build ``open_id -> {"email", "dept", "name"}`` via the SAME feishu-sync
    contact client (tenant token from the vault), so callers that need to map a
    Feishu sender to an enterprise email/department reuse the established
    feishu-sync auth path instead of standing up their own (e.g. lark-cli).

    email prefers ``enterprise_email`` then ``email``; users without either are
    omitted (the caller decides how to handle an unresolved open_id). dept is the
    name of the department the user was listed under.
    """
    contact = _coerce_contact_client(client, api_delay=api_delay)
    root_id = dept_id or "0"
    departments = contact.fetch_department_tree(root_id)
    target = contact.fetch_department_detail(root_id)
    if target and all(dept.dept_id != target.dept_id for dept in departments):
        departments.insert(0, target)

    directory: dict[str, dict[str, str]] = {}
    for dept in departments:
        for raw in contact.iter_department_user_records(dept.dept_id):
            open_id = str(raw.get("open_id") or "")
            if not open_id or open_id in directory:
                continue
            email = str(raw.get("enterprise_email") or raw.get("email") or "")
            if not email:
                continue
            directory[open_id] = {
                "email": email,
                "dept": str(dept.name or "unknown"),
                "name": str(raw.get("name") or ""),
            }
    return directory


def build_org_snapshot(
    departments: list[Department],
    dept_user_map: dict[str, list[DepartmentUser]],
) -> OrgSnapshot:
    dept_by_id = {dept.dept_id: dept for dept in departments}
    employees: dict[str, Employee] = {}

    for dept in departments:
        for user in dept_user_map.get(dept.dept_id, []):
            if not user.open_id:
                continue
            agent_id = user.user_id or f"oid-{user.open_id[-8:]}"
            if agent_id in employees:
                continue
            employees[agent_id] = Employee(
                open_id=user.open_id,
                user_id=user.user_id,
                agent_id=agent_id,
                profile_name=profile_name_for_user_id(user.user_id) if user.user_id else f"feishu_oid_{user.open_id[-8:]}",
                name=user.user_id or agent_id,
                dept_id=dept.dept_id,
                dept_name=dept.name,
                leader_user_id=dept.leader_user_id,
                is_leader=False,
                union_id=user.union_id,
            )

    leader_ids = {dept.leader_user_id for dept in departments if dept.leader_user_id}
    enriched: dict[str, Employee] = {}
    for key, employee in employees.items():
        is_leader = bool(employee.user_id and employee.user_id in leader_ids)
        leader_user_id = employee.leader_user_id
        dept = dept_by_id.get(employee.dept_id)
        if is_leader and dept and employee.user_id == dept.leader_user_id:
            parent = dept_by_id.get(dept.parent_id)
            leader_user_id = parent.leader_user_id if parent else None
        enriched[key] = Employee(
            **{
                **asdict(employee),
                "leader_user_id": leader_user_id,
                "is_leader": is_leader,
                "subordinates": (),
            }
        )

    final: dict[str, Employee] = {}
    for key, employee in enriched.items():
        subordinates: tuple[str, ...] = ()
        if employee.is_leader and employee.user_id:
            subordinates = tuple(
                other.agent_id
                for other in enriched.values()
                if other.user_id
                and other.leader_user_id == employee.user_id
                and other.agent_id != employee.agent_id
            )
        final[key] = Employee(
            **{
                **asdict(employee),
                "subordinates": subordinates,
            }
        )

    stats = {
        "total_depts": len(departments),
        "total_employees": len(final),
        "leaders": sum(1 for employee in final.values() if employee.is_leader),
        "empty_user_id": sum(1 for employee in final.values() if not employee.user_id),
    }
    return OrgSnapshot(
        version=2,
        timestamp=datetime.now(timezone.utc).isoformat(),
        departments=departments,
        employees=final,
        stats=stats,
    )


def build_user_specs(snapshot: OrgSnapshot) -> list[UserSpec]:
    """Convert a snapshot to route specs. Users without Feishu user_id skip routing."""
    specs: list[UserSpec] = []
    for employee in snapshot.employees.values():
        if not employee.user_id:
            continue
        specs.append(
            UserSpec(
                user_id=employee.user_id,
                profile_name=employee.profile_name,
                open_id=employee.open_id,
                union_id=employee.union_id,
            )
        )
    return specs


def profile_name_for_user_id(user_id: str) -> str:
    raw = re.sub(r"[^a-z0-9_-]+", "_", user_id.lower()).strip("_-")
    if not raw or not raw[0].isalnum():
        raw = f"feishu_{raw or 'user'}"
    if raw in _RESERVED_PROFILE_NAMES:
        raw = f"feishu_{raw}"
    if len(raw) > 64:
        digest = hashlib.sha1(user_id.encode("utf-8")).hexdigest()[:8]
        raw = f"{raw[:55]}_{digest}"
    if not _PROFILE_ID_RE.match(raw):
        raise ValueError(f"Cannot derive a valid profile name from user_id={user_id!r}")
    return raw


def sync_profiles(
    snapshot: OrgSnapshot,
    *,
    dry_run: bool = False,
    profiles_root: Optional[Path] = None,
    source_home: Optional[Path] = None,
) -> dict[str, int]:
    """Create/update Hermes profiles for routed Feishu users."""
    root = profiles_root or (_get_default_hermes_root() / "profiles")
    shared_home = source_home or _get_current_hermes_home()
    stats = {"created": 0, "updated": 0, "kept": 0, "skipped": 0}

    for employee in snapshot.employees.values():
        if not employee.user_id:
            stats["skipped"] += 1
            continue
        profile_home = root / employee.profile_name
        action = _sync_one_profile(employee, profile_home, shared_home=shared_home, dry_run=dry_run)
        stats[action] += 1
    return stats


def sync_feishu_org(
    *,
    dept_id: Optional[str] = None,
    db_path: Optional[Path] = None,
    dry_run: bool = False,
    snapshot_out: Optional[Path] = None,
    client: Any = None,
    profiles_root: Optional[Path] = None,
    source_home: Optional[Path] = None,
    api_delay: float = 0.65,
    soft_delete_missing: Optional[bool] = None,
    materialize_credentials_after_sync: bool = True,
) -> dict[str, Any]:
    """Pull Feishu org data, sync profiles, and reconcile the routing table."""
    snapshot = pull_feishu_org(dept_id, client=client, api_delay=api_delay)
    users = build_user_specs(snapshot)
    delete_missing = (dept_id is None) if soft_delete_missing is None else soft_delete_missing
    profile_stats = sync_profiles(
        snapshot,
        dry_run=dry_run,
        profiles_root=profiles_root,
        source_home=source_home,
    )

    if dry_run:
        route_stats = _plan_routes_without_db_writes(db_path, users, soft_delete_missing=delete_missing)
    else:
        table = RoutingTable(db_path)
        try:
            route_stats = apply_users(table, users, soft_delete_missing=delete_missing)
        finally:
            table.close()

    snapshot_path = None
    if snapshot_out and not dry_run:
        snapshot_path = save_snapshot(snapshot, snapshot_out, dept_id=dept_id)

    credential_stats = None
    if materialize_credentials_after_sync and not dry_run:
        from ..credential_materializer import materialize_credentials

        credential_stats = materialize_credentials(
            shared_home=source_home,
            profiles_root=profiles_root,
            db_path=db_path,
            dry_run=False,
        )

    return {
        "dry_run": dry_run,
        "departments": snapshot.stats["total_depts"],
        "employees": snapshot.stats["total_employees"],
        "missing_user_id": snapshot.stats["empty_user_id"],
        "leaders": snapshot.stats["leaders"],
        "profiles_created": profile_stats["created"],
        "profiles_updated": profile_stats["updated"],
        "profiles_kept": profile_stats["kept"],
        "profiles_skipped": profile_stats["skipped"],
        "routes_upserted": route_stats["upserted"],
        "routes_soft_deleted": route_stats["soft_deleted"],
        "routes_kept": route_stats["kept"],
        "routes_soft_delete_enabled": delete_missing,
        "credential_materialization": credential_stats,
        "snapshot_path": str(snapshot_path) if snapshot_path else None,
    }


def save_snapshot(snapshot: OrgSnapshot, path: Path, *, dept_id: Optional[str] = None) -> Path:
    target = _snapshot_target(path, dept_id=dept_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def _migrate_feishu_uat_for_employee(
    profile_home: Path,
    shared_home: Path,
    open_id: str,
) -> bool:
    """Copy ``<shared>/feishu_uat/<open_id>.json`` into ``<profile>/feishu_uat/``.

    Bridges the read-side change (``agent_real._configure_feishu_uat_home``
    now points at the per-profile dir) against the still-shared write-side
    OAuth callback. Idempotent — only copies when the profile-local file is
    missing or older than the shared source.

    Returns ``True`` if a copy happened, ``False`` otherwise (no source,
    invalid open_id, or destination already current).
    """
    if not open_id or not str(open_id).startswith("ou_"):
        return False
    src = shared_home / "feishu_uat" / f"{open_id}.json"
    if not src.is_file():
        return False
    dst_dir = profile_home / "feishu_uat"
    dst_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(dst_dir, 0o700)
    except OSError:
        pass
    dst = dst_dir / f"{open_id}.json"
    if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
        _store_feishu_uat_json_in_vault(profile_home, shared_home, open_id, dst)
        return False
    import shutil
    shutil.copy2(src, dst)
    try:
        os.chmod(dst, 0o600)
    except OSError:
        pass
    _store_feishu_uat_json_in_vault(profile_home, shared_home, open_id, dst)
    return True


def _store_feishu_uat_json_in_vault(
    profile_home: Path,
    shared_home: Path,
    open_id: str,
    token_path: Path,
) -> None:
    """Best-effort mirror of refreshed Feishu UAT JSON into credential vault."""
    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("access_token"):
            return
        scopes = data.get("scopes") or data.get("scope") or []
        if isinstance(scopes, str):
            scopes = [part for part in scopes.replace(",", " ").split() if part]
        expires_at = data.get("expires_at")
        from ..credentials import CredentialStore

        store = CredentialStore(shared_home / "multitenancy.db")
        try:
            store.put_credential(
                profile_name=profile_home.name,
                subject_id=open_id,
                provider="feishu",
                secret_kind="uat",
                payload=data,
                scopes=scopes,
                expires_at=int(expires_at) if expires_at else None,
            )
        finally:
            store.close()
    except Exception:
        # Credential vault is optional for dry local tests and older installs. The
        # profile-local JSON copy remains the migration fallback.
        return


def _tighten_dir_mode(path: Path, mode: int) -> None:
    """Apply ``chmod mode`` to ``path``, swallowing OS errors.

    Best-effort: on filesystems that do not honor POSIX modes (network mounts,
    bind-mounted volumes in some Linux containers) the chmod is a no-op and we
    do not want sync to fail because of it. We log at debug level so the
    failure is still discoverable when investigating mode drift.
    """
    try:
        os.chmod(path, mode)
    except OSError:
        # No logger module imported at sync layer — use a quiet print only
        # under HERMES_MULTITENANCY_VERBOSE_PROVISION=1 to aid postmortems.
        if os.getenv("HERMES_MULTITENANCY_VERBOSE_PROVISION") == "1":
            print(f"[multitenancy:sync] chmod {oct(mode)} failed on {path}")


def _sync_one_profile(
    employee: Employee,
    profile_home: Path,
    *,
    shared_home: Path,
    dry_run: bool,
) -> str:
    existed = profile_home.exists()
    desired_soul = _desired_soul_text(
        (profile_home / "SOUL.md").read_text(encoding="utf-8") if (profile_home / "SOUL.md").exists() else "",
        employee,
    )
    soul_changed = not (profile_home / "SOUL.md").exists() or (
        (profile_home / "SOUL.md").read_text(encoding="utf-8") != desired_soul
    )
    config_missing = not (profile_home / "config.yaml").exists()
    config_needs_image_gen = False
    if not config_missing:
        from ..router import _profile_config_file_needs_default_image_gen

        config_needs_image_gen = _profile_config_file_needs_default_image_gen(profile_home / "config.yaml")

    if dry_run:
        if not existed:
            return "created"
        return "updated" if soul_changed or config_missing or config_needs_image_gen else "kept"

    profile_home.mkdir(parents=True, exist_ok=True)
    # Tighten profile_home to 0700 so other system users cannot enumerate the
    # tenant tree. We always re-apply (chmod is idempotent) so an externally
    # loosened directory gets corrected on the next sync pass.
    _tighten_dir_mode(profile_home, 0o700)
    for subdir in _PROFILE_DIRS:
        sub = profile_home / subdir
        sub.mkdir(parents=True, exist_ok=True)
        _tighten_dir_mode(sub, 0o700)

    config_changed = _ensure_profile_config(profile_home, shared_home=shared_home)
    soul_path = profile_home / "SOUL.md"
    if soul_changed:
        soul_path.write_text(desired_soul, encoding="utf-8")

    _ensure_shared_profile_file(profile_home, shared_home, "auth.json")
    env_changed = _ensure_profile_local_env(profile_home, shared_home)

    # Best-effort migration of the user's Feishu UAT token from the legacy
    # shared location into this profile's own feishu_uat/ dir. agent_real
    # rebinds FEISHU_UAT_DIR per-profile at runtime, so without this copy a
    # subprocess for a freshly-synced profile would not find the token even
    # though OAuth had captured it under the shared path.
    _migrate_feishu_uat_for_employee(profile_home, shared_home, employee.open_id)
    skills_changed = _sync_default_profile_skills(profile_home, shared_home, employee)

    if not existed:
        return "created"
    return "updated" if soul_changed or config_changed or env_changed or skills_changed else "kept"


def _sync_default_profile_skills(
    profile_home: Path,
    shared_home: Path,
    employee: Employee | None = None,
    upstream_profile_home: Path | None = None,
) -> bool:
    changed = False
    desired: dict[str, dict[str, Any]] = {}
    for spec in _profile_skill_specs(
        profile_home,
        shared_home,
        employee=employee,
        upstream_profile_home=upstream_profile_home,
    ):
        rel_path, src, metadata = _desired_skill_metadata(spec, shared_home=shared_home)
        if not src.is_dir():
            continue
        dst = profile_home / "skills" / rel_path
        if metadata.get("install_mode") == "symlink":
            if _skill_tree_has_secret_files(src):
                changed = _copy_skill_tree(src, dst) or changed
            else:
                changed = _link_shared_skill_tree(src, dst) or changed
        else:
            changed = _copy_skill_tree(src, dst) or changed
        desired[str(rel_path)] = metadata
    changed = _prune_removed_managed_skills(profile_home, desired) or changed
    changed = _write_managed_skill_manifest(profile_home, desired) or changed
    return changed


def plan_profile_skill_sync(
    profile_home: Path,
    shared_home: Path,
    employee: Employee | None = None,
    upstream_profile_home: Path | None = None,
) -> dict[str, Any]:
    """Plan profile skill sync without mutating the profile filesystem."""
    desired: dict[str, dict[str, Any]] = {}
    items: list[dict[str, Any]] = []
    for spec in _profile_skill_specs(
        profile_home,
        shared_home,
        employee=employee,
        upstream_profile_home=upstream_profile_home,
    ):
        rel_path, src, metadata = _desired_skill_metadata(spec, shared_home=shared_home)
        if not src.is_dir():
            continue
        desired[str(rel_path)] = metadata

    previous = _read_managed_skill_manifest(profile_home)
    for rel_raw, metadata in sorted(desired.items()):
        rel_path = _safe_skill_relative_path(rel_raw)
        if rel_path is None:
            continue
        previous_metadata = previous.get(rel_raw)
        change = "unchanged"
        if not isinstance(previous_metadata, dict):
            change = "upstream_added"
        elif _profile_skill_target_modified(profile_home / "skills" / rel_path, metadata):
            change = "profile_modified"
        elif _managed_skill_metadata_changed(previous_metadata, metadata):
            change = "upstream_updated"
        item = {
            "path": rel_raw,
            "change": change,
            "install_mode": metadata.get("install_mode"),
            "managed": True,
            "source_exists": True,
        }
        for key in (
            "requested_install_mode",
            "secret_guard",
            "version",
            "token_policy",
            "requires_token",
            "share_with_children",
            "inherited_from",
            "bundle",
        ):
            if metadata.get(key) is not None:
                item[key] = metadata[key]
        items.append(item)

    for rel_raw in sorted(set(previous) - set(desired)):
        if _safe_skill_relative_path(rel_raw) is None:
            continue
        items.append(
            {
                "path": rel_raw,
                "change": "multitenancy_removed",
                "managed": True,
                "source_exists": False,
            }
        )

    counts = {
        "upstream_added": 0,
        "upstream_updated": 0,
        "profile_modified": 0,
        "multitenancy_removed": 0,
        "unchanged": 0,
    }
    for item in items:
        counts[str(item["change"])] += 1
    return {
        "profile_home": str(profile_home),
        "shared_home": str(shared_home),
        "changed": any(item["change"] != "unchanged" for item in items),
        "counts": counts,
        "items": items,
        "secret_free": True,
    }


def _profile_skill_specs(
    profile_home: Path,
    shared_home: Path,
    *,
    employee: Employee | None = None,
    upstream_profile_home: Path | None = None,
) -> list[dict[str, Any]]:
    child_only = upstream_profile_home is not None and employee is None
    if employee is None:
        employee = Employee(
            open_id="",
            user_id=profile_home.name,
            agent_id=profile_home.name,
            profile_name=profile_home.name,
            name=profile_home.name,
            dept_id="",
            dept_name="",
            leader_user_id=None,
        )
    specs_by_path: dict[str, dict[str, Any]] = {}
    if not child_only:
        for spec in _default_profile_skill_specs(shared_home, employee):
            specs_by_path[str(spec["path"])] = spec
    for spec in _child_profile_skill_specs_from_upstream(
        upstream_profile_home, shared_home=shared_home
    ):
        specs_by_path[str(spec["path"])] = spec
    return sorted(specs_by_path.values(), key=lambda item: str(item["path"]))


def _desired_skill_metadata(
    spec: dict[str, Any],
    *,
    shared_home: Path,
) -> tuple[Path, Path, dict[str, Any]]:
    rel_path = spec["path"]
    install_mode = spec["install_mode"]
    src = spec.get("source_path") or (shared_home / "skills" / rel_path)
    actual_install_mode = install_mode
    metadata: dict[str, Any] = {
        "source": str(src),
        "install_mode": actual_install_mode,
    }
    if install_mode == "symlink" and src.is_dir() and _skill_tree_has_secret_files(src):
        actual_install_mode = "copy"
        metadata["install_mode"] = actual_install_mode
        metadata["requested_install_mode"] = install_mode
        metadata["secret_guard"] = "copy_filtered"
    for key in (
        "requested_install_mode",
        "secret_guard",
        "version",
        "token_policy",
        "requires_token",
        "share_with_children",
        "inherited_from",
        "bundle",
    ):
        if spec.get(key) is not None:
            metadata[key] = spec[key]
    return rel_path, src, metadata


def _default_profile_skill_specs(shared_home: Path, employee: Employee) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    # Baseline defaults (profile-skill-defaults.yaml) are ALWAYS included. Previously
    # the presence of skill-distribution.yaml took an exclusive `if` branch that never
    # called _default_profile_skill_paths, silently dropping every profile's baseline
    # default skills. MERGE instead of shadow: emit defaults first, then let
    # skill-distribution.yaml entries overlay them on path conflict (distribution
    # carries richer audience/metadata) via the last-wins dedup below. When only one
    # file exists behavior is unchanged: no defaults file -> _default_profile_skill_paths
    # returns []; no distribution file -> the overlay block is skipped.
    specs.extend(
        {
            "path": rel_path,
            "install_mode": "symlink" if _should_symlink_shared_skill(rel_path) else "copy",
            "share_with_children": False,
        }
        for rel_path in _default_profile_skill_paths(shared_home)
    )
    distribution = _skill_distribution_config_path(shared_home)
    if distribution is not None:
        raw = yaml.safe_load(distribution.read_text(encoding="utf-8")) or {}
        items = raw.get("skills") or []
        if not isinstance(items, list):
            raise ValueError("skill distribution config must contain skills: []")
        for item in items:
            spec = _coerce_skill_distribution_entry(item, employee, shared_home=shared_home)
            if spec is not None:
                specs.append(spec)
    specs.extend(_skill_bundle_profile_specs(shared_home, employee))
    specs_by_path = {str(spec["path"]): spec for spec in specs}
    for rel_path in _shared_lark_skill_paths(shared_home):
        specs_by_path.setdefault(
            str(rel_path),
            {
                "path": rel_path,
                "install_mode": "symlink",
                "share_with_children": True,
                "token_policy": "brokered",
                "requires_token": False,
            },
        )
    for rel_path in _shared_kep_cli_skill_paths(shared_home):
        # symlink -> pool refresh propagates instantly; credentials stay
        # per-profile via kep-auth (no brokered token semantics here).
        specs_by_path.setdefault(
            str(rel_path),
            {
                "path": rel_path,
                "install_mode": "symlink",
                "share_with_children": False,
            },
        )
    specs = list(specs_by_path.values())
    return sorted(specs, key=lambda item: str(item["path"]))


def _child_profile_skill_specs_from_upstream(
    upstream_profile_home: Path | None,
    *,
    shared_home: Path,
) -> list[dict[str, Any]]:
    if upstream_profile_home is None:
        return []
    upstream_manifest = _read_managed_skill_manifest(upstream_profile_home)
    specs: list[dict[str, Any]] = []
    for rel_raw, metadata in upstream_manifest.items():
        if not isinstance(metadata, dict):
            continue
        if metadata.get("share_with_children") is not True:
            continue
        rel_path = _safe_skill_relative_path(rel_raw)
        if rel_path is None:
            continue
        install_mode = str(metadata.get("install_mode") or "symlink").strip().lower()
        if install_mode not in {"symlink", "copy"}:
            continue
        source_path = _safe_skill_source_path(metadata.get("source"), shared_home=shared_home)
        spec: dict[str, Any] = {
            "path": rel_path,
            "install_mode": install_mode,
            "inherited_from": upstream_profile_home.name,
            "share_with_children": True,
        }
        if source_path is not None:
            spec["source_path"] = source_path
        for key in ("version", "token_policy", "requires_token", "bundle", "requested_install_mode", "secret_guard"):
            if metadata.get(key) is not None:
                spec[key] = metadata[key]
        specs.append(spec)
    return specs


def _skill_distribution_config_path(shared_home: Path) -> Optional[Path]:
    return next((shared_home / name for name in SKILL_DISTRIBUTION_CONFIG_NAMES if (shared_home / name).exists()), None)


def _skill_bundle_config_path(shared_home: Path) -> Optional[Path]:
    return next((shared_home / name for name in SKILL_BUNDLES_CONFIG_NAMES if (shared_home / name).exists()), None)


def _skill_bundle_profile_specs(shared_home: Path, employee: Employee) -> list[dict[str, Any]]:
    config_path = _skill_bundle_config_path(shared_home)
    if config_path is None:
        return []
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    root = raw.get("skill_bundles") if isinstance(raw, dict) else None
    if root is None and isinstance(raw, dict):
        root = raw
    if not isinstance(root, dict):
        raise ValueError("skill bundles config must contain skill_bundles: {}")
    bundles_raw = root.get("bundles") or {}
    if isinstance(bundles_raw, list):
        bundles = {
            str(item.get("name") or item.get("id") or "").strip(): item
            for item in bundles_raw
            if isinstance(item, dict)
        }
    elif isinstance(bundles_raw, dict):
        bundles = {str(name): value for name, value in bundles_raw.items()}
    else:
        raise ValueError("skill bundles config must contain bundles as mapping or list")

    default_bundle_names = _as_string_set(root.get("default"))
    specs: list[dict[str, Any]] = []
    for bundle_name, bundle_raw in sorted(bundles.items()):
        if not bundle_name or not isinstance(bundle_raw, dict):
            continue
        bundle_audience = bundle_raw.get("audience", "all")
        if not _skill_audience_matches(bundle_audience, employee):
            continue
        if default_bundle_names and bundle_name not in default_bundle_names and _skill_audience_is_global(bundle_audience):
            continue
        skills = bundle_raw.get("skills") or []
        if not isinstance(skills, list):
            raise ValueError(f"skill bundle {bundle_name!r} must contain skills: []")
        for item in skills:
            coerced = _coerce_bundle_skill_entry(
                item,
                employee,
                shared_home=shared_home,
                bundle_name=bundle_name,
                bundle=bundle_raw,
            )
            if coerced is not None:
                specs.append(coerced)
    return specs


def _coerce_bundle_skill_entry(
    item: Any,
    employee: Employee,
    *,
    shared_home: Path,
    bundle_name: str,
    bundle: dict[str, Any],
) -> Optional[dict[str, Any]]:
    if isinstance(item, str):
        merged: dict[str, Any] = {"path": item}
    elif isinstance(item, dict):
        merged = dict(item)
    else:
        return None
    for key in ("audience", "install_mode", "token_policy", "requires_token", "share_with_children", "source", "src", "version"):
        if key not in merged and bundle.get(key) is not None:
            merged[key] = bundle[key]
    spec = _coerce_skill_distribution_entry(merged, employee, shared_home=shared_home)
    if spec is None:
        return None
    spec["bundle"] = bundle_name
    return spec


def _coerce_skill_distribution_entry(
    item: Any,
    employee: Employee,
    *,
    shared_home: Path,
) -> Optional[dict[str, Any]]:
    if isinstance(item, str):
        rel_path = _safe_skill_relative_path(item)
        install_mode = "symlink"
        audience: Any = "all"
        source = None
        version = None
        token_policy = "unspecified"
        requires_token = None
        share_with_children = False
    elif isinstance(item, dict):
        rel_path = _safe_skill_relative_path(item.get("path") or item.get("skill"))
        install_mode = str(item.get("install_mode") or "symlink").strip().lower()
        audience = item.get("audience", "all")
        source = item.get("source") or item.get("src")
        version = item.get("version")
        requires_token = item.get("requires_token")
        token_policy = str(
            item.get("token_policy")
            or ("none" if requires_token is False else "unspecified")
        ).strip().lower()
        share_with_children = item.get("share_with_children")
        if share_with_children is None:
            share_with_children = _skill_is_child_shareable(token_policy, requires_token)
        else:
            share_with_children = bool(share_with_children)
    else:
        return None
    if rel_path is None:
        return None
    if install_mode not in {"symlink", "copy"}:
        raise ValueError(f"unsupported skill install_mode={install_mode!r}")
    if not _skill_audience_matches(audience, employee):
        return None
    spec: dict[str, Any] = {
        "path": rel_path,
        "install_mode": install_mode,
        "share_with_children": bool(share_with_children),
    }
    source_path = _safe_skill_source_path(source, shared_home=shared_home) if source else None
    if source_path is not None:
        spec["source_path"] = source_path
    if version is not None:
        spec["version"] = str(version)
    if token_policy != "unspecified":
        spec["token_policy"] = token_policy
    if requires_token is not None:
        spec["requires_token"] = bool(requires_token)
    return spec


def _skill_is_child_shareable(token_policy: str, requires_token: Any) -> bool:
    if requires_token is False:
        return True
    return token_policy in _CHILD_SHAREABLE_TOKEN_POLICIES


def _safe_skill_source_path(value: Any, *, shared_home: Path) -> Optional[Path]:
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        return path
    if any(part in ("", ".", "..") or part.startswith(".") for part in path.parts):
        return None
    return shared_home / path


def _skill_audience_matches(audience: Any, employee: Employee) -> bool:
    if audience is None:
        return True
    if isinstance(audience, str):
        return audience.strip().lower() in {"all", "*", "everyone", "__all__"}
    if isinstance(audience, list):
        return employee.profile_name in {str(item).strip() for item in audience}
    if not isinstance(audience, dict):
        return False

    profiles = _as_string_set(audience.get("profiles") or audience.get("profile"))
    if profiles and employee.profile_name in profiles:
        return True
    users = _as_string_set(audience.get("users") or audience.get("user_ids") or audience.get("user_id"))
    if users and employee.user_id and employee.user_id in users:
        return True
    departments = _as_string_set(audience.get("departments") or audience.get("department_ids") or audience.get("dept_ids"))
    if departments and (employee.dept_id in departments or employee.dept_name in departments):
        return True
    return False


def _skill_audience_is_global(audience: Any) -> bool:
    if audience is None:
        return True
    if isinstance(audience, str):
        return audience.strip().lower() in {"all", "*", "everyone", "__all__"}
    return False


def _as_string_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _default_profile_skill_paths(shared_home: Path) -> list[Path]:
    config_path = next((shared_home / name for name in DEFAULT_PROFILE_SKILL_CONFIG_NAMES if (shared_home / name).exists()), None)
    if config_path is None:
        return []
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    items = raw.get("skills") or []
    if not isinstance(items, list):
        raise ValueError("profile skill defaults config must contain skills: []")
    result: list[Path] = []
    for item in items:
        path = _safe_skill_relative_path(item)
        if path is not None:
            result.append(path)
    return sorted(set(result), key=lambda p: str(p))


def _shared_kep_cli_skill_paths(shared_home: Path) -> list[Path]:
    """Auto-distribute candidates for kep business-CLI skills: Keep/kep-*-cli.

    Mirrors the lark-* prefix auto-distribution (sunke 2026-07-02: kep skills
    are org-wide by decision) so a NEW system registered in the kep hub reaches
    every profile at the next org sync without a manual distribution edit.
    """
    keep_root = shared_home / "skills" / "Keep"
    if not keep_root.is_dir():
        return []
    result: list[Path] = []
    for item in keep_root.iterdir():
        if not (item.name.startswith("kep-") and item.name.endswith("-cli")):
            continue
        if (item.is_dir() or item.is_symlink()) and (item / "SKILL.md").is_file():
            result.append(Path("Keep") / item.name)
    return sorted(result, key=lambda p: str(p))


def _shared_lark_skill_paths(shared_home: Path) -> list[Path]:
    skills_root = shared_home / "skills"
    if not skills_root.is_dir():
        return []
    result: list[Path] = []
    for item in skills_root.iterdir():
        if not item.name.startswith(_AUTO_DISTRIBUTE_SHARED_SKILL_PREFIXES):
            continue
        if (item.is_dir() or item.is_symlink()) and (item / "SKILL.md").is_file():
            result.append(Path(item.name))
    return sorted(result, key=lambda p: str(p))


def _safe_skill_relative_path(value: Any) -> Optional[Path]:
    raw = str(value or "").strip()
    path = Path(raw)
    if not raw or path.is_absolute():
        return None
    if any(part in ("", ".", "..") or part.startswith(".") for part in path.parts):
        return None
    return path


def _copy_skill_tree(src: Path, dst: Path) -> bool:
    changed = False
    if dst.is_symlink() or (dst.exists() and not dst.is_dir()):
        dst.unlink()
        changed = True
    for root, dirs, files in os.walk(src):
        root_path = Path(root)
        root_rel = root_path.relative_to(src)
        if root_rel.parts:
            target_root = dst / root_rel
            if not target_root.exists():
                target_root.mkdir(parents=True, exist_ok=True)
                changed = True
            _tighten_dir_mode(target_root, 0o700)

        keep_dirs: list[str] = []
        for dirname in dirs:
            item = root_path / dirname
            rel = item.relative_to(src)
            target = dst / rel
            if item.is_symlink() or dirname in _SKILL_IGNORED_DIRS:
                continue
            if dirname in _SKILL_LINKED_DEP_DIRS:
                if not _same_symlink(target, item):
                    if target.exists() or target.is_symlink():
                        shutil.rmtree(target) if target.is_dir() and not target.is_symlink() else target.unlink()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(item, target_is_directory=True)
                    changed = True
                continue
            keep_dirs.append(dirname)
        dirs[:] = keep_dirs

        for filename in files:
            item = root_path / filename
            if item.is_symlink():
                continue
            rel = item.relative_to(src)
            if _is_secret_skill_file(rel):
                continue
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or item.read_bytes() != target.read_bytes():
                shutil.copy2(item, target)
                changed = True
    if src.exists() and not dst.exists():
        dst.mkdir(parents=True, exist_ok=True)
        changed = True
    if dst.exists():
        _tighten_dir_mode(dst, 0o700)
    return changed


def _skill_tree_has_secret_files(src: Path) -> bool:
    for root, dirs, files in os.walk(src):
        root_path = Path(root)
        keep_dirs: list[str] = []
        for dirname in dirs:
            item = root_path / dirname
            if item.is_symlink() or dirname in _SKILL_IGNORED_DIRS:
                continue
            keep_dirs.append(dirname)
        dirs[:] = keep_dirs
        for filename in files:
            item = root_path / filename
            if item.is_symlink():
                continue
            if _is_secret_skill_file(item.relative_to(src)):
                return True
    return False


def _profile_skill_target_modified(target: Path, metadata: dict[str, Any]) -> bool:
    source = Path(str(metadata.get("source") or ""))
    install_mode = str(metadata.get("install_mode") or "")
    if not source.is_dir():
        return True
    if install_mode == "symlink":
        return not _same_symlink(target, source)
    if not target.is_dir() or target.is_symlink():
        return True
    return _skill_tree_digest(source) != _skill_tree_digest(target)


def _managed_skill_metadata_changed(previous: dict[str, Any], desired: dict[str, Any]) -> bool:
    keys = {
        "source",
        "install_mode",
        "requested_install_mode",
        "secret_guard",
        "version",
        "token_policy",
        "requires_token",
        "share_with_children",
        "inherited_from",
    }
    return {key: previous.get(key) for key in keys} != {key: desired.get(key) for key in keys}


def _skill_tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return ""
    for item in sorted(root.rglob("*"), key=lambda path: str(path.relative_to(root))):
        rel = item.relative_to(root)
        if item.is_symlink() or any(part in _SKILL_IGNORED_DIRS for part in rel.parts):
            continue
        if item.is_dir():
            continue
        if _is_secret_skill_file(rel):
            continue
        digest.update(str(rel).encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _should_symlink_shared_skill(rel_path: Path) -> bool:
    return len(rel_path.parts) == 1 and rel_path.name.startswith(_SHARED_SKILL_SYMLINK_PREFIXES)


def _link_shared_skill_tree(src: Path, dst: Path) -> bool:
    if _same_symlink(dst, src):
        return False
    if dst.exists() or dst.is_symlink():
        shutil.rmtree(dst) if dst.is_dir() and not dst.is_symlink() else dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.symlink_to(src, target_is_directory=True)
    return True


def _prune_removed_managed_skills(profile_home: Path, desired: dict[str, dict[str, Any]]) -> bool:
    changed = False
    previous = _read_managed_skill_manifest(profile_home)
    for rel_raw in sorted(set(previous) - set(desired)):
        rel_path = _safe_skill_relative_path(rel_raw)
        if rel_path is None:
            continue
        target = profile_home / "skills" / rel_path
        if target.exists() or target.is_symlink():
            shutil.rmtree(target) if target.is_dir() and not target.is_symlink() else target.unlink()
            changed = True
    return changed


def _read_managed_skill_manifest(profile_home: Path) -> dict[str, Any]:
    path = profile_home / "skills" / MANAGED_SKILL_MANIFEST
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    skills = raw.get("skills") if isinstance(raw, dict) else None
    return skills if isinstance(skills, dict) else {}


def _write_managed_skill_manifest(profile_home: Path, desired: dict[str, dict[str, Any]]) -> bool:
    path = profile_home / "skills" / MANAGED_SKILL_MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        {
            "version": 1,
            "skills": desired,
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    before = path.read_text(encoding="utf-8") if path.exists() else None
    if before == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def _same_symlink(path: Path, target: Path) -> bool:
    if not path.is_symlink():
        return False
    try:
        return path.resolve(strict=True) == target.resolve(strict=True)
    except FileNotFoundError:
        return False


def _is_secret_skill_file(rel_path: Path) -> bool:
    name = rel_path.name.lower()
    if name in _SKILL_SECRET_FILE_NAMES:
        return True
    if name.endswith((".token", ".secret", ".key")):
        return True
    if name.endswith(_SKILL_SOURCE_FILE_SUFFIXES):
        return False
    return any(part in name for part in _SKILL_SECRET_NAME_PARTS)


def _ensure_profile_config(profile_home: Path, *, shared_home: Path) -> bool:
    config_path = profile_home / "config.yaml"
    before = config_path.read_text(encoding="utf-8") if config_path.exists() else None
    if config_path.exists():
        from ..router import _normalize_profile_config_file

        _normalize_profile_config_file(config_path, shared_home=shared_home)
    else:
        from ..router import _dump_profile_config, _profile_config_from_shared_home

        config_path.write_text(
            _dump_profile_config(_profile_config_from_shared_home(shared_home)),
            encoding="utf-8",
        )
    after = config_path.read_text(encoding="utf-8") if config_path.exists() else None
    return before != after


def _ensure_shared_profile_file(profile_home: Path, shared_home: Path, name: str) -> None:
    from ..router import _ensure_shared_profile_file as ensure

    ensure(profile_home, shared_home, name)


def _ensure_profile_local_env(profile_home: Path, shared_home: Path) -> bool:
    from ..router import ensure_profile_local_env as ensure

    return ensure(profile_home, shared_home)


def _desired_soul_text(existing: str, employee: Employee) -> str:
    block = _render_org_block(employee)
    if ORG_SYNC_BEGIN in existing and ORG_SYNC_END in existing:
        pattern = re.compile(
            re.escape(ORG_SYNC_BEGIN) + r".*?" + re.escape(ORG_SYNC_END),
            re.DOTALL,
        )
        return pattern.sub(block, existing)
    prefix = existing.rstrip()
    if not prefix:
        prefix = f"# {employee.profile_name}"
    return f"{prefix}\n\n{block}\n"


def _render_org_block(employee: Employee) -> str:
    role = "manager" if employee.is_leader else "employee"
    lines = [
        ORG_SYNC_BEGIN,
        "## Organization Sync",
        "",
        f"- profile_name: {employee.profile_name}",
        f"- user_id: {employee.user_id or ''}",
        f"- open_id: {employee.open_id}",
        f"- department: {employee.dept_name} ({employee.dept_id})",
        f"- role: {role}",
        f"- leader_user_id: {employee.leader_user_id or ''}",
    ]
    if employee.subordinates:
        lines.append(f"- direct_subordinates: {len(employee.subordinates)}")
        lines.extend(f"  - {item}" for item in employee.subordinates[:30])
    else:
        lines.append("- direct_subordinates: 0")
    lines.append(ORG_SYNC_END)
    return "\n".join(lines)


def _coerce_contact_client(client: Any, *, api_delay: float) -> Any:
    if client is None:
        return FeishuContactClient.for_current_home(api_delay=api_delay)
    if hasattr(client, "do_request"):
        return FeishuContactClient(client, api_delay=api_delay)
    return client


def _plan_routes_without_db_writes(
    db_path: Optional[Path],
    users: list[UserSpec],
    *,
    soft_delete_missing: bool,
) -> dict[str, int]:
    current = _read_current_routes(db_path)
    current_by_open_id = {row["open_id"]: row for row in current.values()}
    desired = {u.user_id: u for u in users}

    upserted = 0
    soft_deleted = 0
    kept = 0
    for user in desired.values():
        existing = current.get(user.user_id)
        if existing and (
            existing["profile_name"] == user.profile_name
            and existing["open_id"] == user.open_id
            and (existing["union_id"] or None) == user.union_id
        ):
            kept += 1
        else:
            conflicting = current_by_open_id.get(user.open_id)
            if conflicting and conflicting["user_id"] != user.user_id:
                soft_deleted += 1
                current.pop(conflicting["user_id"], None)
            upserted += 1
    if soft_delete_missing:
        soft_deleted += sum(1 for user_id in current if user_id not in desired)
    return {"upserted": upserted, "soft_deleted": soft_deleted, "kept": kept}


def _read_current_routes(db_path: Optional[Path]) -> dict[str, dict[str, Optional[str]]]:
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    if str(path) == ":memory:" or not path.exists():
        return {}
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return {}
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT user_id, profile_name, open_id, union_id"
            " FROM multitenancy_routing WHERE active = 1"
        )
        return {row["user_id"]: dict(row) for row in cur.fetchall()}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _department_from_api(raw: dict[str, Any], *, parent_id: str) -> Department:
    dept_id = str(raw.get("open_department_id") or raw.get("department_id") or raw.get("dept_id") or "")
    return Department(
        dept_id=dept_id,
        name=str(raw.get("name") or dept_id),
        parent_id=str(raw.get("parent_department_id") or parent_id or "0"),
        leader_user_id=raw.get("leader_user_id") or None,
        member_count=int(raw.get("member_count") or 0),
    )


def _department_user_from_api(raw: dict[str, Any]) -> DepartmentUser:
    return DepartmentUser(
        open_id=str(raw.get("open_id") or ""),
        user_id=raw.get("user_id") or None,
        union_id=raw.get("union_id") or None,
    )


def _get_current_hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:
        env_home = os.getenv("HERMES_HOME")
        return Path(env_home).expanduser() if env_home else Path.home() / ".hermes"


def _get_current_shared_home() -> Path:
    env_shared = os.getenv("HERMES_SHARED_HOME")
    if env_shared:
        return Path(env_shared).expanduser()
    current = _get_current_hermes_home()
    if current.parent.name == "profiles":
        return current.parent.parent
    return current


def _get_default_hermes_root() -> Path:
    try:
        from hermes_constants import get_default_hermes_root  # type: ignore

        return Path(get_default_hermes_root())
    except Exception:
        current = _get_current_hermes_home()
        if current.parent.name == "profiles":
            return current.parent.parent
        return current


def _snapshot_target(path: Path, *, dept_id: Optional[str]) -> Path:
    if path.suffix:
        return path
    suffix = f"-dept-{dept_id[-8:]}" if dept_id else ""
    name = f"org-{datetime.now(timezone.utc).date().isoformat()}{suffix}.json"
    return path / name


__all__ = [
    "Department",
    "DepartmentUser",
    "Employee",
    "FeishuContactClient",
    "FeishuOrgSyncError",
    "OrgSnapshot",
    "build_org_snapshot",
    "build_user_specs",
    "profile_name_for_user_id",
    "pull_feishu_org",
    "save_snapshot",
    "sync_feishu_org",
    "sync_profiles",
]
