"""Short-lived MCP client credentials bound to a trusted Hermes principal."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .trusted_runtime_principal import TrustedRuntimePrincipal


_TOKEN_PREFIX = "hmc_"
_MAX_TTL_SECONDS = 3600
_ALLOWED_SCOPES = frozenset({"mcp:tools"})
_TOKEN_RETENTION_SECONDS = 24 * 3600


class ClientTokenStore:
    """Persist only token hashes; resolve them back to one exact principal."""

    def __init__(self, db_path: Path | str, *, issuer: str, resource: str) -> None:
        self.db_path = Path(db_path)
        self.issuer = _http_url(issuer, "issuer")
        self.resource = _http_url(resource, "resource")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS multitenancy_mcp_client_tokens (
                token_sha256 TEXT PRIMARY KEY NOT NULL,
                client_id TEXT NOT NULL,
                profile_name TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                scopes_json TEXT NOT NULL,
                audience TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                revoked_at INTEGER,
                created_at INTEGER NOT NULL
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def mint(
        self,
        *,
        principal: TrustedRuntimePrincipal,
        client_id: str,
        scopes: list[str],
        ttl_seconds: int = 300,
    ) -> str:
        if not isinstance(principal, TrustedRuntimePrincipal) or not principal.is_authentic():
            raise PermissionError("trusted principal is required")
        client = str(client_id or "").strip()
        clean_scopes = sorted({str(scope).strip() for scope in scopes if str(scope).strip()})
        if not client or len(client) > 200 or not clean_scopes:
            raise ValueError("client id and scopes are required")
        if not set(clean_scopes).issubset(_ALLOWED_SCOPES):
            raise ValueError("unsupported client scope")
        if type(ttl_seconds) is not int or not 30 <= ttl_seconds <= _MAX_TTL_SECONDS:
            raise ValueError("client token ttl is out of range")
        return self._mint_bound(
            profile_name=principal.profile_name,
            subject_id=principal.actor_subject,
            client_id=client,
            scopes=clean_scopes,
            ttl_seconds=ttl_seconds,
        )

    def _mint_bound(
        self,
        *,
        profile_name: str,
        subject_id: str,
        client_id: str,
        scopes: list[str],
        ttl_seconds: int,
    ) -> str:
        if not scopes or not set(scopes).issubset(_ALLOWED_SCOPES):
            raise ValueError("unsupported client scope")
        now = int(time.time())
        token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            cutoff = now - _TOKEN_RETENTION_SECONDS
            self._conn.execute(
                "DELETE FROM multitenancy_mcp_client_tokens "
                "WHERE expires_at < ? OR (revoked_at IS NOT NULL AND revoked_at < ?)",
                (cutoff, cutoff),
            )
            self._conn.execute(
                """
                INSERT INTO multitenancy_mcp_client_tokens (
                    token_sha256, client_id, profile_name, subject_id,
                    scopes_json, audience, expires_at, revoked_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    _token_hash(token),
                    client_id,
                    profile_name,
                    subject_id,
                    json.dumps(scopes, separators=(",", ":")),
                    self.resource,
                    now + ttl_seconds,
                    now,
                ),
            )
            self._conn.commit()
        return token

    async def verify_token(self, token: str) -> AccessToken | None:
        value = str(token or "").strip()
        if not value.startswith(_TOKEN_PREFIX) or len(value) > 128:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT client_id, profile_name, subject_id, scopes_json,
                       audience, expires_at, revoked_at
                FROM multitenancy_mcp_client_tokens
                WHERE token_sha256 = ?
                """,
                (_token_hash(value),),
            ).fetchone()
        if (
            row is None
            or row["revoked_at"] is not None
            or int(row["expires_at"]) <= int(time.time())
            or str(row["audience"]) != self.resource
        ):
            return None
        try:
            scopes = json.loads(str(row["scopes_json"]))
        except (TypeError, ValueError):
            return None
        if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
            return None
        return AccessToken(
            token=value,
            client_id=str(row["client_id"]),
            scopes=scopes,
            expires_at=int(row["expires_at"]),
            resource=self.resource,
            subject=str(row["subject_id"]),
            claims={"iss": self.issuer, "profile": str(row["profile_name"])},
        )

    def revoke(self, token: str) -> bool:
        value = str(token or "").strip()
        if not value.startswith(_TOKEN_PREFIX) or len(value) > 128:
            return False
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE multitenancy_mcp_client_tokens
                SET revoked_at = ?
                WHERE token_sha256 = ? AND revoked_at IS NULL
                """,
                (int(time.time()), _token_hash(value)),
            )
            self._conn.commit()
            return cursor.rowcount == 1


class HermesOAuthProvider:
    """Public-client OAuth provider; browser approval supplies the trusted owner."""

    def __init__(self, token_store: ClientTokenStore, *, approval_url: str | None = None) -> None:
        self.token_store = token_store
        self.issuer = token_store.issuer
        self.resource = token_store.resource
        self.approval_url = _approval_url(approval_url) if approval_url else None
        self._lock = token_store._lock
        self._conn = token_store._conn
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS multitenancy_mcp_oauth_clients (
                client_id TEXT PRIMARY KEY NOT NULL,
                client_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS multitenancy_mcp_oauth_grants (
                kind TEXT NOT NULL,
                key_sha256 TEXT PRIMARY KEY NOT NULL,
                client_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                consumed_at INTEGER,
                created_at INTEGER NOT NULL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        # The caller owns ClientTokenStore and closes the shared SQLite connection.
        return None

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT client_json FROM multitenancy_mcp_oauth_clients WHERE client_id=?",
                (str(client_id or ""),),
            ).fetchone()
        if row is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate_json(str(row["client_json"]))
        except ValueError:
            return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Desktop MCP clients are public PKCE clients. Keeping a DCR client secret
        # would require a second secret vault and adds no security for local apps.
        client_info.token_endpoint_auth_method = "none"
        client_info.client_secret = None
        client_info.client_secret_expires_at = None
        client_id = str(client_info.client_id or "").strip()
        if not client_id:
            raise ValueError("client id is required")
        with self._lock:
            self._conn.execute(
                "INSERT INTO multitenancy_mcp_oauth_clients (client_id, client_json, created_at) VALUES (?, ?, ?)",
                (client_id, client_info.model_dump_json(), int(time.time())),
            )
            self._conn.commit()

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if str(params.resource or "").rstrip("/") != self.resource.rstrip("/"):
            from mcp.server.auth.provider import AuthorizeError

            raise AuthorizeError("invalid_request", "resource does not match this MCP server")
        request_id = "hma_" + secrets.token_urlsafe(32)
        now = int(time.time())
        payload = {
            "state": params.state,
            "scopes": params.scopes or [],
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource,
        }
        self._put_grant("pending", request_id, str(client.client_id), payload, now + 300)
        approval_url = self.approval_url or f"{self.issuer}/oauth/approve"
        return f"{approval_url}?{urlencode({'request_id': request_id})}"

    def pending_request(self, request_id: str) -> dict[str, object] | None:
        """Return display-only consent metadata without consuming the request."""
        row = self._load_grant("pending", str(request_id or ""))
        if row is None:
            return None
        try:
            payload = json.loads(str(row["payload_json"]))
            with self._lock:
                client_row = self._conn.execute(
                    "SELECT client_json FROM multitenancy_mcp_oauth_clients WHERE client_id=?",
                    (str(row["client_id"]),),
                ).fetchone()
            client = OAuthClientInformationFull.model_validate_json(str(client_row["client_json"]))
            redirect = urlsplit(str(payload["redirect_uri"]))
            redirect_origin = (
                f"{redirect.scheme}://{redirect.netloc}" if redirect.netloc else f"{redirect.scheme}:"
            )
            return {
                "client_id": str(row["client_id"]),
                "client_name": str(client.client_name or row["client_id"]),
                "redirect_origin": redirect_origin,
                "scopes": list(payload.get("scopes") or []),
            }
        except (KeyError, TypeError, ValueError):
            return None

    def approve(self, request_id: str, principal: TrustedRuntimePrincipal) -> str:
        if not isinstance(principal, TrustedRuntimePrincipal) or not principal.is_authentic():
            raise PermissionError("trusted principal is required")
        pending = self._consume_grant("pending", request_id)
        if pending is None:
            raise PermissionError("authorization request is unknown or expired")
        payload = json.loads(str(pending["payload_json"]))
        code = "hmc_code_" + secrets.token_urlsafe(32)
        code_payload = {
            **payload,
            "profile_name": principal.profile_name,
            "subject_id": principal.actor_subject,
        }
        self._put_grant("code", code, str(pending["client_id"]), code_payload, int(time.time()) + 120)
        return construct_redirect_uri(
            str(payload["redirect_uri"]),
            code=code,
            state=payload.get("state"),
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = self._load_grant("code", authorization_code, str(client.client_id))
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        return AuthorizationCode(
            code=authorization_code,
            scopes=list(payload["scopes"]),
            expires_at=float(row["expires_at"]),
            client_id=str(row["client_id"]),
            code_challenge=str(payload["code_challenge"]),
            redirect_uri=payload["redirect_uri"],
            redirect_uri_provided_explicitly=bool(payload["redirect_uri_provided_explicitly"]),
            resource=payload["resource"],
            subject=str(payload["subject_id"]),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        row = self._consume_grant("code", authorization_code.code, str(client.client_id))
        if row is None:
            raise TokenError("invalid_grant", "authorization code is already used")
        payload = json.loads(str(row["payload_json"]))
        return self._issue_pair(
            client_id=str(client.client_id),
            profile_name=str(payload["profile_name"]),
            subject_id=str(payload["subject_id"]),
            scopes=list(payload["scopes"]),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = self._load_grant("refresh", refresh_token, str(client.client_id))
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        return RefreshToken(
            token=refresh_token,
            client_id=str(row["client_id"]),
            scopes=list(payload["scopes"]),
            expires_at=int(row["expires_at"]),
            subject=str(payload["subject_id"]),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        row = self._consume_grant("refresh", refresh_token.token, str(client.client_id))
        if row is None:
            raise TokenError("invalid_grant", "refresh token is already used")
        payload = json.loads(str(row["payload_json"]))
        if not set(scopes).issubset(set(payload["scopes"])):
            raise TokenError("invalid_scope", "refresh cannot expand scopes")
        return self._issue_pair(
            client_id=str(client.client_id),
            profile_name=str(payload["profile_name"]),
            subject_id=str(payload["subject_id"]),
            scopes=scopes,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        return await self.token_store.verify_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if self.token_store.revoke(token.token):
            return
        self._consume_grant("refresh", token.token)

    def _issue_pair(
        self, *, client_id: str, profile_name: str, subject_id: str, scopes: list[str]
    ) -> OAuthToken:
        try:
            access = self.token_store._mint_bound(
                profile_name=profile_name,
                subject_id=subject_id,
                client_id=client_id,
                scopes=scopes,
                ttl_seconds=300,
            )
        except ValueError as exc:
            raise TokenError("invalid_scope", "authorization grant has unsupported scopes") from exc
        refresh = "hmc_refresh_" + secrets.token_urlsafe(32)
        self._put_grant(
            "refresh",
            refresh,
            client_id,
            {
                "profile_name": profile_name,
                "subject_id": subject_id,
                "scopes": scopes,
            },
            int(time.time()) + 14 * 24 * 3600,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=300,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    def _put_grant(
        self, kind: str, raw_key: str, client_id: str, payload: dict, expires_at: int
    ) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO multitenancy_mcp_oauth_grants (
                    kind, key_sha256, client_id, payload_json,
                    expires_at, consumed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    kind,
                    _token_hash(raw_key),
                    client_id,
                    json.dumps(payload, separators=(",", ":")),
                    expires_at,
                    now,
                ),
            )
            self._conn.commit()

    def _load_grant(self, kind: str, raw_key: str, client_id: str = ""):
        sql = (
            "SELECT * FROM multitenancy_mcp_oauth_grants "
            "WHERE kind=? AND key_sha256=? AND consumed_at IS NULL AND expires_at>?"
        )
        values: list[object] = [kind, _token_hash(raw_key), int(time.time())]
        if client_id:
            sql += " AND client_id=?"
            values.append(client_id)
        with self._lock:
            return self._conn.execute(sql, values).fetchone()

    def _consume_grant(self, kind: str, raw_key: str, client_id: str = ""):
        with self._lock:
            row = self._load_grant(kind, raw_key, client_id)
            if row is None:
                return None
            cursor = self._conn.execute(
                """
                UPDATE multitenancy_mcp_oauth_grants SET consumed_at=?
                WHERE kind=? AND key_sha256=? AND consumed_at IS NULL
                """,
                (int(time.time()), kind, _token_hash(raw_key)),
            )
            self._conn.commit()
            return row if cursor.rowcount == 1 else None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _http_url(value: str, field: str) -> str:
    clean = str(value or "").strip().rstrip("/")
    parsed = urlsplit(clean)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError(f"invalid {field} URL")
    return clean


def _approval_url(value: str) -> str:
    clean = str(value or "").strip().rstrip("/")
    parsed = urlsplit(clean)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or (parsed.fragment and not parsed.fragment.startswith("/"))
    ):
        raise ValueError("invalid approval URL")
    return clean
