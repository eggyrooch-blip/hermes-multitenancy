"""Profile-scoped credential vault for hermes-multitenancy.

This module deliberately separates routing identity from credential storage:
``multitenancy_routing`` says which profile owns a user; this credential table
says whether that profile has a usable provider credential.  Model-visible
status APIs must never return the raw payload.  Runtime-only callers can ask
for a decrypted payload after supplying the exact profile/subject/provider/kind
tuple.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable


_SCHEMA = """
CREATE TABLE IF NOT EXISTS multitenancy_credentials (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name      TEXT NOT NULL,
    subject_id        TEXT NOT NULL,
    provider          TEXT NOT NULL,
    secret_kind       TEXT NOT NULL,
    scopes_json       TEXT NOT NULL DEFAULT '[]',
    scope_hash        TEXT NOT NULL DEFAULT '',
    expires_at        INTEGER,
    encrypted_payload TEXT NOT NULL,
    active            INTEGER NOT NULL DEFAULT 1,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    UNIQUE(profile_name, subject_id, provider, secret_kind)
);
CREATE INDEX IF NOT EXISTS idx_credentials_profile_subject
    ON multitenancy_credentials(profile_name, subject_id, provider, active);
"""


class CredentialStore:
    """SQLite-backed, profile-scoped credential vault.

    ``encryption_key`` is intentionally explicit in tests.  Production should
    provide ``HERMES_CREDENTIAL_KEY`` or ``HERMES_MULTITENANCY_CREDENTIAL_KEY``.
    The built-in sealing is an authenticated stream cipher based on HMAC-SHA256
    keystream blocks.  It keeps token strings out of SQLite pages without adding
    a runtime dependency; deployments with a KMS can swap this layer later while
    preserving the table and broker API.
    """

    def __init__(self, db_path: Path | str, *, encryption_key: str | bytes | None = None) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._key = _resolve_optional_key(encryption_key)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        # This database contains encrypted credential payloads and must not
        # inherit a service account's permissive umask when first created.
        Path(self.db_path).chmod(0o600)

    def put_credential(
        self,
        *,
        profile_name: str,
        subject_id: str,
        provider: str,
        secret_kind: str,
        payload: dict[str, Any],
        scopes: Iterable[str] | None = None,
        expires_at: int | None = None,
        commit_if: Callable[[sqlite3.Connection], bool] | None = None,
    ) -> bool:
        profile_name = _clean_id("profile_name", profile_name)
        subject_id = _clean_id("subject_id", subject_id)
        provider = _clean_id("provider", provider)
        secret_kind = _clean_id("secret_kind", secret_kind)
        scopes_list = _normalize_scopes(scopes)
        now = _now_ms()
        sealed = _seal_json(payload, _require_key(self._key))
        try:
            self._conn.execute(
                """
                INSERT INTO multitenancy_credentials
                    (profile_name, subject_id, provider, secret_kind, scopes_json,
                     scope_hash, expires_at, encrypted_payload, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(profile_name, subject_id, provider, secret_kind) DO UPDATE SET
                    scopes_json       = excluded.scopes_json,
                    scope_hash        = excluded.scope_hash,
                    expires_at        = excluded.expires_at,
                    encrypted_payload = excluded.encrypted_payload,
                    active            = 1,
                    updated_at        = excluded.updated_at
                """,
                (
                    profile_name,
                    subject_id,
                    provider,
                    secret_kind,
                    json.dumps(scopes_list, ensure_ascii=False, sort_keys=True),
                    _scope_hash(scopes_list),
                    expires_at,
                    sealed,
                    now,
                    now,
                ),
            )
            if commit_if is not None and not commit_if(self._conn):
                self._conn.rollback()
                return False
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    def get_status(
        self,
        *,
        profile_name: str,
        subject_id: str,
        provider: str,
        required_scopes: Iterable[str] | None = None,
        secret_kind: str = "uat",
    ) -> dict[str, Any]:
        """Return redacted credential status for exactly one profile subject."""
        row = self._get_row(
            profile_name=profile_name,
            subject_id=subject_id,
            provider=provider,
            secret_kind=secret_kind,
        )
        base = {
            "profile_name": str(profile_name),
            "subject_id": str(subject_id),
            "provider": str(provider),
            "secret_kind": str(secret_kind),
            "storage": "multitenancy_db",
            "has_payload": False,
            "scopes": [],
            "missing_scopes": [],
            "expires_at": None,
        }
        if row is None:
            return {**base, "status": "missing"}

        scopes = _loads_scopes(row["scopes_json"])
        required = _normalize_scopes(required_scopes)
        missing = [scope for scope in required if scope not in set(scopes)]
        expires_at = row["expires_at"]
        status = "valid"
        if expires_at is not None and int(expires_at) <= _now_ms():
            status = "expired"
        elif missing:
            status = "scope_missing"

        result = {
            **base,
            "status": status,
            "has_payload": bool(row["encrypted_payload"]),
            "scopes": scopes,
            "missing_scopes": missing,
            "expires_at": expires_at,
        }
        if self._key is not None:
            try:
                payload = _open_json(row["encrypted_payload"], self._key)
                refresh_expires_at = int(payload.get("refresh_expires_at"))
                if refresh_expires_at > 0:
                    result["refresh_expires_at"] = refresh_expires_at
                if payload.get("credential_version"):
                    result["credential_version"] = str(payload["credential_version"])
                if payload.get("status"):
                    result["credential_status"] = str(payload["status"])
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

        return result

    def get_secret_for_runtime(
        self,
        *,
        profile_name: str,
        subject_id: str,
        provider: str,
        secret_kind: str = "uat",
    ) -> dict[str, Any]:
        """Return decrypted payload for internal runtime use only."""
        payload, _updated_at = self.get_secret_for_runtime_with_updated_at(
            profile_name=profile_name,
            subject_id=subject_id,
            provider=provider,
            secret_kind=secret_kind,
        )
        return payload

    def get_secret_for_runtime_with_updated_at(
        self,
        *,
        profile_name: str,
        subject_id: str,
        provider: str,
        secret_kind: str = "uat",
    ) -> tuple[dict[str, Any], int]:
        """Return one decrypted runtime payload with its canonical write time."""
        row = self._get_row(
            profile_name=profile_name,
            subject_id=subject_id,
            provider=provider,
            secret_kind=secret_kind,
        )
        if row is None:
            raise PermissionError("credential not found for current profile/subject/provider")
        return (
            _open_json(row["encrypted_payload"], _require_key(self._key)),
            int(row["updated_at"]),
        )

    def close(self) -> None:
        self._conn.close()

    def payload_value_is_unique(
        self,
        *,
        provider: str,
        secret_kind: str,
        field: str,
        value: str,
        owner_profile: str,
        owner_subject: str,
    ) -> bool:
        """Check one encrypted upstream identity is not active for another owner."""
        provider = _clean_id("provider", provider)
        secret_kind = _clean_id("secret_kind", secret_kind)
        owner = (_clean_id("profile_name", owner_profile), _clean_id("subject_id", owner_subject))
        wanted = str(value)
        rows = self._conn.execute(
            """
            SELECT profile_name, subject_id, encrypted_payload
            FROM multitenancy_credentials
            WHERE provider = ? AND secret_kind = ? AND active = 1
            """,
            (provider, secret_kind),
        ).fetchall()
        key = _require_key(self._key)
        for row in rows:
            if (str(row["profile_name"]), str(row["subject_id"])) == owner:
                continue
            payload = _open_json(row["encrypted_payload"], key)
            if hmac.compare_digest(str(payload.get(field) or ""), wanted):
                return False
        return True

    def delete_credential(
        self,
        *,
        profile_name: str,
        subject_id: str,
        provider: str,
        secret_kind: str,
    ) -> bool:
        """Delete exactly one credential after its upstream generation is gone."""
        values = (
            _clean_id("profile_name", profile_name),
            _clean_id("subject_id", subject_id),
            _clean_id("provider", provider),
            _clean_id("secret_kind", secret_kind),
        )
        try:
            cursor = self._conn.execute(
                """
                DELETE FROM multitenancy_credentials
                WHERE profile_name = ? AND subject_id = ?
                  AND provider = ? AND secret_kind = ?
                """,
                values,
            )
            self._conn.commit()
            return bool(cursor.rowcount)
        except Exception:
            self._conn.rollback()
            raise

    def _get_row(
        self,
        *,
        profile_name: str,
        subject_id: str,
        provider: str,
        secret_kind: str,
    ) -> sqlite3.Row | None:
        cur = self._conn.execute(
            """
            SELECT * FROM multitenancy_credentials
            WHERE profile_name = ?
              AND subject_id = ?
              AND provider = ?
              AND secret_kind = ?
              AND active = 1
            LIMIT 1
            """,
            (
                _clean_id("profile_name", profile_name),
                _clean_id("subject_id", subject_id),
                _clean_id("provider", provider),
                _clean_id("secret_kind", secret_kind),
            ),
        )
        return cur.fetchone()


def _resolve_key(value: str | bytes | None) -> bytes:
    raw = value
    if raw is None:
        raw = (
            os.getenv("HERMES_MULTITENANCY_CREDENTIAL_KEY")
            or os.getenv("HERMES_CREDENTIAL_KEY")
            or ""
        )
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not raw:
        raise RuntimeError("credential encryption key is required")
    return hashlib.sha256(raw).digest()


def _resolve_optional_key(value: str | bytes | None) -> bytes | None:
    raw = value
    if raw is None:
        raw = (
            os.getenv("HERMES_MULTITENANCY_CREDENTIAL_KEY")
            or os.getenv("HERMES_CREDENTIAL_KEY")
            or ""
        )
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not raw:
        return None
    return hashlib.sha256(raw).digest()


def _require_key(key: bytes | None) -> bytes:
    if key is None:
        raise RuntimeError("credential encryption key is required")
    return key


def _clean_id(name: str, value: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    if "/" in value or "\\" in value or "\0" in value or ".." in value:
        raise ValueError(f"{name} contains illegal characters")
    return value


def _normalize_scopes(scopes: Iterable[str] | None) -> list[str]:
    return sorted({str(scope).strip() for scope in (scopes or []) if str(scope).strip()})


def _loads_scopes(value: str) -> list[str]:
    try:
        loaded = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    return _normalize_scopes(str(item) for item in loaded)


def _scope_hash(scopes: list[str]) -> str:
    joined = "\n".join(scopes).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()


def _seal_json(payload: dict[str, Any], key: bytes) -> str:
    plaintext = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    nonce = os.urandom(16)
    ciphertext = _xor_bytes(plaintext, _keystream(key, nonce, len(plaintext)))
    tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    return "v1:" + base64.urlsafe_b64encode(nonce + tag + ciphertext).decode("ascii")


def _open_json(sealed: str, key: bytes) -> dict[str, Any]:
    if not sealed.startswith("v1:"):
        raise ValueError("unsupported credential payload version")
    raw = base64.urlsafe_b64decode(sealed[3:].encode("ascii"))
    if len(raw) < 48:
        raise ValueError("credential payload is truncated")
    nonce = raw[:16]
    tag = raw[16:48]
    ciphertext = raw[48:]
    expected = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("credential payload authentication failed")
    plaintext = _xor_bytes(ciphertext, _keystream(key, nonce, len(ciphertext)))
    data = json.loads(plaintext.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("credential payload must be a JSON object")
    return data


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(out[:length])


def _xor_bytes(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right))


def _now_ms() -> int:
    return int(time.time() * 1000)
