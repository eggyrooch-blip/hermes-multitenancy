"""Trusted employee identity mapping for LiteLLM-billed Hermes runs."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Optional
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlparse

from .routing import DEFAULT_DB_PATH, RoutingTable
from .run_broker import RunRejected
from .run_models import RunRequest
from .token_usage_uploader import make_owner_resolver


_TRUE = frozenset({"1", "true", "yes", "on"})
_EMPLOYEE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_LITELLM_USER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_MAX_ADMIN_RESPONSE_BYTES = 1024 * 1024
_RESERVED_METADATA = frozenset({
    "litellm_billing_user_id",
    "litellm_billing_employee_user_id",
    "litellm_billing_email",
    "litellm_billing_base_url",
})


@dataclass(frozen=True)
class BillingIdentity:
    employee_user_id: str
    email: str
    litellm_user_id: str


class BillingIdentityStore:
    """Small durable mapping stored beside the existing routing data."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.db_path = str(db_path or DEFAULT_DB_PATH)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS multitenancy_billing_identities (
                employee_user_id TEXT PRIMARY KEY NOT NULL,
                email TEXT NOT NULL,
                litellm_user_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        self._conn.commit()

    def get(self, employee_user_id: str) -> Optional[BillingIdentity]:
        with self._lock:
            row = self._conn.execute(
                "SELECT employee_user_id, email, litellm_user_id "
                "FROM multitenancy_billing_identities WHERE employee_user_id = ?",
                (employee_user_id,),
            ).fetchone()
        if row is None:
            return None
        return BillingIdentity(**dict(row))

    def put(self, identity: BillingIdentity) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO multitenancy_billing_identities (
                    employee_user_id, email, litellm_user_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(employee_user_id) DO UPDATE SET
                    email = excluded.email,
                    litellm_user_id = excluded.litellm_user_id,
                    updated_at = excluded.updated_at
                """,
                (
                    identity.employee_user_id,
                    identity.email,
                    identity.litellm_user_id,
                    now,
                    now,
                ),
            )
            self._conn.commit()


EnsureUser = Callable[[str], dict[str, Any]]


class BillingIdentityPreparer:
    def __init__(
        self,
        *,
        routing: Any,
        store: Any,
        ensure_user: EnsureUser,
        billing_base_url: str,
    ) -> None:
        self._routing = routing
        self._store = store
        self._ensure_user = ensure_user
        self._billing_base_url = billing_base_url.strip().rstrip("/")
        self._routing_lock = threading.Lock()
        self._resolve_owner = make_owner_resolver(
            self._group_owner,
            self._profile_owner,
        )

    def prepare(self, request: RunRequest) -> RunRequest:
        metadata = {
            key: value
            for key, value in dict(request.metadata or {}).items()
            if key not in _RESERVED_METADATA
        }
        with self._routing_lock:
            owner_open_id = self._resolve_owner({
                "chat_type": metadata.get("chat_type"),
                "chat_id": request.chat_id,
                "sender_open_id": (
                    metadata.get("sender_open_id")
                    if request.channel == "feishu"
                    else ""
                ),
                "profile": request.profile_name,
            })
            employee_user_id = self._employee_user_id(owner_open_id)
        if not employee_user_id:
            raise RunRejected("employee billing identity could not be resolved")

        identity = self._store.get(employee_user_id)
        if identity is None:
            identity = _identity_from_ensure_response(
                employee_user_id,
                self._ensure_user(employee_user_id),
            )
            self._store.put(identity)

        metadata.update({
            "litellm_billing_user_id": identity.litellm_user_id,
            "litellm_billing_employee_user_id": identity.employee_user_id,
            "litellm_billing_email": identity.email,
            "litellm_billing_base_url": self._billing_base_url,
        })
        return replace(request, metadata=metadata)

    def _group_owner(self, chat_id: str) -> Optional[str]:
        row = self._routing.lookup_by_chat_id(chat_id)
        return str(getattr(row, "owner_open_id", "") or "") or None

    def _profile_owner(self, profile_name: str) -> Optional[str]:
        row = self._routing.lookup_by_profile_name(profile_name)
        if row is None:
            return None
        return str(
            getattr(row, "owner_open_id", None)
            or getattr(row, "open_id", None)
            or ""
        ) or None

    def _employee_user_id(self, owner_open_id: Optional[str]) -> Optional[str]:
        if not owner_open_id:
            return None
        for getter_name in ("resolve_owner_root", "lookup_by_open_id"):
            row = getattr(self._routing, getter_name)(owner_open_id)
            user_id = str(getattr(row, "user_id", "") or "").strip() if row else ""
            if _EMPLOYEE_ID_RE.fullmatch(user_id) and not user_id.startswith("ou_"):
                return user_id
        return None


def _identity_from_ensure_response(
    employee_user_id: str,
    payload: dict[str, Any],
) -> BillingIdentity:
    returned_user_id = str(payload.get("user_id") or "").strip()
    email = str(payload.get("email") or "").strip()
    litellm_user_id = str(payload.get("litellm_user_id") or "").strip()
    email_local, separator, _domain = email.partition("@")
    if returned_user_id != employee_user_id or not separator:
        raise RunRejected("LiteLLM returned a mismatched employee identity")
    if email_local.lower() != employee_user_id.lower():
        raise RunRejected("LiteLLM returned a mismatched employee email")
    if not _LITELLM_USER_ID_RE.fullmatch(litellm_user_id):
        raise RunRejected("LiteLLM returned an invalid user id")
    return BillingIdentity(employee_user_id, email, litellm_user_id)


class _LiteLLMAdminError(RuntimeError):
    def __init__(self, status: int) -> None:
        super().__init__(f"LiteLLM management API returned HTTP {status}")
        self.status = status


def _admin_base_url() -> str:
    raw = (
        os.environ.get("HERMES_LITELLM_ADMIN_BASE_URL", "").strip()
        or os.environ.get("HERMES_LITELLM_BILLING_BASE_URL", "").strip()
    ).rstrip("/")
    for suffix in ("/v1", "/anthropic", "/v1beta"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RunRejected("LiteLLM management API is not configured")
    return raw


def _admin_timeout() -> float:
    try:
        return max(
            0.1,
            min(float(os.environ.get("HERMES_LITELLM_ADMIN_TIMEOUT", "5")), 30.0),
        )
    except ValueError as exc:
        raise RunRejected("LiteLLM management timeout is invalid") from exc


def _admin_request(
    method: str,
    path: str,
    *,
    body: Optional[dict[str, Any]] = None,
) -> Any:
    token = os.environ.get("HERMES_LITELLM_ADMIN_KEY", "").strip()
    if not token:
        raise RunRejected("LiteLLM management API is not configured")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{_admin_base_url()}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=_admin_timeout()) as response:
            raw = response.read(_MAX_ADMIN_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise _LiteLLMAdminError(int(exc.code)) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RunRejected("LiteLLM management API is unavailable") from exc
    if len(raw) > _MAX_ADMIN_RESPONSE_BYTES:
        raise RunRejected("LiteLLM management API returned an invalid response")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunRejected("LiteLLM management API returned invalid JSON") from exc


def _latest_org_snapshot(db_path: str) -> Optional[dict[str, Any]]:
    directory = Path(
        os.environ.get("HERMES_ORG_SNAPSHOT_DIR", "").strip()
        or Path(db_path).expanduser().parent / "org-snapshots"
    ).expanduser()
    try:
        files = list(directory.glob("org-*.json"))
        newest = max(files, key=lambda path: path.stat().st_mtime)
        payload = json.loads(newest.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _team_alias_for_employee(employee_user_id: str, db_path: str) -> str:
    fallback = (
        os.environ.get("HERMES_LITELLM_DEFAULT_TEAM_ALIAS", "FD").strip()
        or "FD"
    )
    snapshot = _latest_org_snapshot(db_path)
    if snapshot is None:
        return fallback
    employees = snapshot.get("employees")
    departments = snapshot.get("departments")
    if not isinstance(employees, dict) or not isinstance(departments, list):
        return fallback
    employee = next(
        (
            value
            for key, value in employees.items()
            if isinstance(value, dict)
            and (
                str(key) == employee_user_id
                or str(value.get("user_id") or "") == employee_user_id
            )
        ),
        None,
    )
    if employee is None:
        return fallback
    by_id = {
        str(dept.get("dept_id")): dept
        for dept in departments
        if isinstance(dept, dict) and dept.get("dept_id")
    }
    path: list[str] = []
    dept_id = str(employee.get("dept_id") or "")
    visited: set[str] = set()
    while dept_id and dept_id not in visited:
        visited.add(dept_id)
        dept = by_id.get(dept_id)
        if dept is None:
            break
        name = str(dept.get("name") or "").strip()
        if name:
            path.insert(0, name)
        parent_id = str(dept.get("parent_id") or "")
        if not parent_id or parent_id == "0":
            break
        dept_id = parent_id
    if not path:
        return fallback
    first_level = path[0]
    cooperator = os.environ.get(
        "HERMES_LITELLM_COOPERATOR_DEPT_NAME", "合作商"
    ).strip()
    if first_level != cooperator:
        return first_level
    separator = os.environ.get("HERMES_LITELLM_COOPERATOR_SEPARATOR", "-")
    vendor = path[-1].split(separator, 1)[0].strip() if separator else path[-1]
    return vendor or fallback


def _team_from_payload(payload: Any, alias: str) -> Optional[str]:
    teams = (
        payload
        if isinstance(payload, list)
        else payload.get("teams", []) if isinstance(payload, dict) else []
    )
    for team in teams:
        if isinstance(team, dict) and team.get("team_alias") == alias:
            team_id = str(team.get("team_id") or "").strip()
            if team_id:
                return team_id
    return None


def _team_id_for_new_user(employee_user_id: str, db_path: str) -> str:
    fallback_id = os.environ.get("HERMES_LITELLM_DEFAULT_TEAM_ID", "").strip()
    if not fallback_id:
        raise RunRejected("LiteLLM default team is not configured")
    fallback_alias = os.environ.get(
        "HERMES_LITELLM_DEFAULT_TEAM_ALIAS", "FD"
    ).strip() or "FD"
    alias = _team_alias_for_employee(employee_user_id, db_path)
    if alias == fallback_alias:
        return fallback_id
    try:
        found = _team_from_payload(_admin_request("GET", "/team/list"), alias)
        if found:
            return found
        created = _admin_request(
            "POST",
            "/team/new",
            body={"team_alias": alias, "models": ["all-proxy-models"]},
        )
        team_id = str(created.get("team_id") or "").strip() if isinstance(created, dict) else ""
        if team_id:
            return team_id
        raise RunRejected("LiteLLM team could not be resolved")
    except _LiteLLMAdminError as exc:
        if exc.status == 409:
            try:
                found = _team_from_payload(_admin_request("GET", "/team/list"), alias)
                if found:
                    return found
            except (_LiteLLMAdminError, RunRejected) as retry_exc:
                raise RunRejected("LiteLLM team could not be resolved") from retry_exc
        raise RunRejected("LiteLLM team could not be resolved") from exc


def _find_user_by_email(email: str) -> Optional[dict[str, Any]]:
    page_size = 100
    for page in range(1, 102):
        query = urllib.parse.urlencode({
            "user_email": email,
            "page": page,
            "page_size": page_size,
        })
        payload = _admin_request("GET", f"/user/list?{query}")
        users = (
            payload
            if isinstance(payload, list)
            else payload.get("users", []) if isinstance(payload, dict) else []
        )
        for user in users:
            if (
                isinstance(user, dict)
                and str(user.get("user_email") or "").strip().lower()
                == email.lower()
            ):
                return user
        total_pages = payload.get("total_pages") if isinstance(payload, dict) else None
        if len(users) < page_size or (
            isinstance(total_pages, int) and page >= total_pages
        ):
            return None
    raise RunRejected("LiteLLM user list exceeded the supported page limit")


def _get_user_by_id(user_id: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"user_id": user_id})
    payload = _admin_request("GET", f"/user/info?{query}")
    user = payload.get("user_info", payload) if isinstance(payload, dict) else None
    if not isinstance(user, dict):
        raise RunRejected("LiteLLM user could not be resolved")
    return user


def _ensure_user_over_http(
    employee_user_id: str,
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    domain = (
        os.environ.get("HERMES_LITELLM_EMPLOYEE_EMAIL_DOMAIN", "keep.com")
        .strip()
        .lower()
    )
    if not domain or "@" in domain or "/" in domain:
        raise RunRejected("LiteLLM employee email domain is invalid")
    email = f"{employee_user_id}@{domain}"
    path = str(db_path or DEFAULT_DB_PATH)
    try:
        user = _find_user_by_email(email)
        created = user is None
        if user is None:
            team_id = _team_id_for_new_user(employee_user_id, path)
            try:
                user = _admin_request(
                    "POST",
                    "/user/new",
                    body={
                        "user_email": email,
                        "user_alias": employee_user_id,
                        "user_role": "internal_user",
                        "auto_create_key": False,
                        "teams": [team_id],
                        "send_invite_email": True,
                    },
                )
            except _LiteLLMAdminError as exc:
                if exc.status != 409:
                    raise
                user = _find_user_by_email(email)
                created = False
        if not isinstance(user, dict):
            raise RunRejected("LiteLLM user could not be resolved")
        litellm_user_id = str(user.get("user_id") or "").strip()
        if not _LITELLM_USER_ID_RE.fullmatch(litellm_user_id):
            raise RunRejected("LiteLLM returned an invalid user id")
        full_user = _get_user_by_id(litellm_user_id)
        metadata_value = full_user.get("metadata") or {}
        if not isinstance(metadata_value, dict):
            raise RunRejected("LiteLLM returned invalid user metadata")
        metadata = dict(metadata_value)
        metadata["hermes_billing_active"] = True
        _admin_request(
            "POST",
            "/user/update",
            body={
                "user_id": litellm_user_id,
                "max_budget": None,
                "budget_duration": None,
                "metadata": metadata,
            },
        )
    except _LiteLLMAdminError as exc:
        raise RunRejected("LiteLLM management API is unavailable") from exc
    return {
        "user_id": employee_user_id,
        "email": email,
        "litellm_user_id": litellm_user_id,
        "created": created,
    }


def _billing_enabled() -> bool:
    return os.environ.get("HERMES_LITELLM_BILLING_ENABLED", "").strip().lower() in _TRUE


_DEFAULT_LOCK = threading.Lock()
_DEFAULT_PREPARER: Optional[BillingIdentityPreparer] = None


def _default_preparer() -> BillingIdentityPreparer:
    global _DEFAULT_PREPARER
    with _DEFAULT_LOCK:
        if _DEFAULT_PREPARER is None:
            billing_base_url = os.environ.get("HERMES_LITELLM_BILLING_BASE_URL", "").strip()
            if not billing_base_url:
                raise RunRejected("LiteLLM billing gateway is not configured")
            db_path = os.environ.get("HERMES_MULTITENANCY_DB", "").strip() or str(DEFAULT_DB_PATH)
            _DEFAULT_PREPARER = BillingIdentityPreparer(
                routing=RoutingTable(db_path),
                store=BillingIdentityStore(db_path),
                ensure_user=lambda employee_user_id: _ensure_user_over_http(
                    employee_user_id,
                    db_path=db_path,
                ),
                billing_base_url=billing_base_url,
            )
        return _DEFAULT_PREPARER


async def prepare_billing_request(request: RunRequest) -> RunRequest:
    """Strip spoofable fields, then resolve/create identity when enabled."""
    if not _billing_enabled():
        clean = {
            key: value
            for key, value in dict(request.metadata or {}).items()
            if key not in _RESERVED_METADATA
        }
        return request if clean == request.metadata else replace(request, metadata=clean)
    return await asyncio.to_thread(_default_preparer().prepare, request)


def request_overrides_for_endpoint(
    metadata: dict[str, Any],
    destination_base_url: str,
) -> dict[str, Any]:
    """Return trusted headers only for an allowed path on the billing origin."""
    user_id = str(metadata.get("litellm_billing_user_id") or "").strip()
    billing_base_url = str(metadata.get("litellm_billing_base_url") or "").strip()
    if not _LITELLM_USER_ID_RE.fullmatch(user_id):
        return {}
    if not _allowed_billing_endpoint(billing_base_url, destination_base_url):
        return {}
    return {
        "extra_headers": {
            "X-Hermes-User-Id": user_id,
            "X-Hermes-Source": "hermes",
        }
    }


def _allowed_billing_endpoint(left: str, right: str) -> bool:
    try:
        a = urlparse(left)
        b = urlparse(right)
    except (TypeError, ValueError):
        return False
    same_origin = bool(
        a.scheme
        and a.netloc
        and a.scheme.lower() == b.scheme.lower()
        and a.netloc.lower() == b.netloc.lower()
    )
    if not same_origin:
        return False
    configured = os.environ.get(
        "HERMES_LITELLM_BILLING_ALLOWED_PATHS", "/v1,/anthropic"
    )
    allowed_paths = {
        path.strip().rstrip("/") or "/"
        for path in configured.split(",")
        if path.strip().startswith("/")
    }
    allowed_paths.add(a.path.rstrip("/") or "/")
    return (b.path.rstrip("/") or "/") in allowed_paths
