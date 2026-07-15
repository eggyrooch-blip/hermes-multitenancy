"""Trusted employee identity mapping for LiteLLM-billed Hermes runs."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Optional
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .routing import DEFAULT_DB_PATH, RoutingTable
from .run_broker import RunRejected
from .run_models import RunRequest
from .token_usage_uploader import make_owner_resolver


_TRUE = frozenset({"1", "true", "yes", "on"})
_EMPLOYEE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_LITELLM_USER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
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
        raise RunRejected("ai-gateway returned a mismatched employee identity")
    if email_local.lower() != employee_user_id.lower():
        raise RunRejected("ai-gateway returned a mismatched employee email")
    if not _LITELLM_USER_ID_RE.fullmatch(litellm_user_id):
        raise RunRejected("ai-gateway returned an invalid LiteLLM user id")
    return BillingIdentity(employee_user_id, email, litellm_user_id)


def _ensure_user_over_http(employee_user_id: str) -> dict[str, Any]:
    endpoint = os.environ.get("HERMES_LITELLM_IDENTITY_ENSURE_URL", "").strip()
    token = os.environ.get("AI_GATEWAY_INTERNAL_API_TOKEN", "").strip()
    if not endpoint or not token:
        raise RunRejected("employee billing identity service is not configured")
    body = json.dumps({"user_id": employee_user_id}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        timeout = max(0.1, min(float(os.environ.get("HERMES_LITELLM_IDENTITY_TIMEOUT", "3")), 10.0))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(65537)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
        raise RunRejected("employee billing identity service is unavailable") from exc
    if len(raw) > 65536:
        raise RunRejected("employee billing identity service returned an invalid response")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunRejected("employee billing identity service returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RunRejected("employee billing identity service returned an invalid response")
    return payload


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
                ensure_user=_ensure_user_over_http,
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
