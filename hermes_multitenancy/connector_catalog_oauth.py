"""Owner-bound upstream MCP OAuth for catalog remote connectors."""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

import httpx
from mcp.client.auth.oauth2 import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthMetadata, OAuthToken

from .connector_custom_catalog import CustomConnectorStore
from .connector_remote_probe import _latest_protocol_version, validate_remote_endpoint
from .credentials import CredentialStore


class _OAuthStorage:
    def __init__(self, db_path: Path, schema: dict[str, Any], profile: str, subject: str, key: str | bytes | None) -> None:
        self.db_path, self.schema, self.profile, self.subject, self.key = db_path, schema, profile, subject, key
        self.tokens: OAuthToken | None = None
        self.client: OAuthClientInformationFull | None = None
        self.metadata: OAuthMetadata | None = None
        self.expires_at: int | None = None
        store = CredentialStore(db_path, encryption_key=key)
        try:
            payload = store.get_secret_for_runtime(
                profile_name=profile,
                subject_id=subject,
                provider=str(schema["provider"]),
                secret_kind="oauth",
            )
        except PermissionError:
            payload = {}
            status = {}
        else:
            status = store.get_status(
                profile_name=profile,
                subject_id=subject,
                provider=str(schema["provider"]),
                secret_kind="oauth",
            )
        finally:
            store.close()
        if payload.get("owner_profile") == profile and payload.get("owner_subject") == subject:
            if payload.get("tokens"):
                self.tokens = OAuthToken.model_validate(payload["tokens"])
            if payload.get("client"):
                self.client = OAuthClientInformationFull.model_validate(payload["client"])
            if payload.get("metadata"):
                self.metadata = OAuthMetadata.model_validate(payload["metadata"])
            self.expires_at = int(status["expires_at"]) if status.get("expires_at") else None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        if not tokens.refresh_token and self.tokens and self.tokens.refresh_token:
            tokens = tokens.model_copy(update={"refresh_token": self.tokens.refresh_token})
        self.tokens = tokens
        self._commit()

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client = client_info
        if self.tokens:
            self._commit()

    def set_metadata(self, metadata: OAuthMetadata) -> None:
        self.metadata = metadata
        self._commit()

    def _commit(self) -> None:
        if not self.tokens or not self.client:
            return
        store = CredentialStore(self.db_path, encryption_key=self.key)
        try:
            store.put_credential(
                profile_name=self.profile,
                subject_id=self.subject,
                provider=str(self.schema["provider"]),
                secret_kind="oauth",
                payload={
                    "owner_profile": self.profile,
                    "owner_subject": self.subject,
                    "fields": {},
                    "tokens": self.tokens.model_dump(mode="json", exclude_none=True),
                    "client": self.client.model_dump(mode="json", exclude_none=True),
                    "metadata": self.metadata.model_dump(mode="json", exclude_none=True) if self.metadata else None,
                },
                scopes=(self.tokens.scope or "").split(),
                expires_at=(int(time.time() * 1000) + self.tokens.expires_in * 1000)
                if self.tokens.expires_in else None,
            )
        finally:
            store.close()


@dataclass
class _Pending:
    callback: asyncio.Future[tuple[str, str | None]]
    redirect: asyncio.Future[str]
    task: asyncio.Task[dict[str, Any]] | None = None
    expiry: asyncio.Task[None] | None = None
    state: str = ""
    consumed: bool = False


class CatalogOAuthBroker:
    def __init__(
        self,
        db_path: Path | str,
        *,
        encryption_key: str | bytes | None = None,
        resolver: Callable[..., list[tuple[Any, ...]]],
        transport: httpx.AsyncBaseTransport | None = None,
        verify: Callable[[Path, str, str, str], Awaitable[int]],
        flow_timeout: float = 300,
    ) -> None:
        self.db_path = Path(db_path)
        self.encryption_key = encryption_key
        self.resolver = resolver
        self.transport = transport
        self.verify = verify
        self.flow_timeout = flow_timeout
        self.pending: dict[str, _Pending] = {}

    async def _expire(self, state: str, pending: _Pending) -> None:
        await asyncio.sleep(self.flow_timeout)
        if self.pending.get(state) is pending:
            self.pending.pop(state, None)
            pending.callback.cancel()
            if pending.task:
                pending.task.cancel()

    async def start(
        self, profile: str, subject: str, row: dict[str, Any], *, redirect_uri: str
    ) -> dict[str, str]:
        schema = row.get("credential_schema") or {}
        if schema.get("auth_flow") != "mcp_oauth" or schema.get("secret_kind") != "oauth":
            raise ValueError("catalog connector does not use MCP OAuth")
        endpoint = validate_remote_endpoint(str(row.get("endpoint") or ""), resolver=self.resolver).url
        redirect = urlparse(redirect_uri)
        if redirect.scheme != "https" or not redirect.netloc:
            raise ValueError("catalog OAuth redirect URI must be HTTPS")

        loop = asyncio.get_running_loop()
        pending = _Pending(loop.create_future(), loop.create_future())
        storage = _OAuthStorage(self.db_path, schema, profile, subject, self.encryption_key)

        async def redirect_handler(url: str) -> None:
            state = (parse_qs(urlparse(url).query).get("state") or [""])[0]
            if not state or state in self.pending:
                raise PermissionError("catalog OAuth state unavailable")
            pending.state = state
            self.pending[state] = pending
            pending.expiry = asyncio.create_task(self._expire(state, pending))
            pending.redirect.set_result(url)

        async def callback_handler() -> tuple[str, str | None]:
            return await pending.callback

        provider = OAuthClientProvider(
            endpoint,
            OAuthClientMetadata(
                redirect_uris=[redirect_uri],
                token_endpoint_auth_method="none",
                client_name="Hermes Connectors",
                software_id="hermes-connectors",
                software_version="1",
            ),
            storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
            timeout=self.flow_timeout,
        )
        pending.task = asyncio.create_task(self._complete_flow(profile, subject, row, endpoint, provider, storage))
        done, _ = await asyncio.wait({pending.redirect, pending.task}, return_when=asyncio.FIRST_COMPLETED)
        if pending.task in done and not pending.redirect.done():
            await pending.task
            raise RuntimeError("catalog OAuth did not provide an authorization URL")
        return {"authorization_url": pending.redirect.result()}

    async def complete(self, state: str, code: str) -> dict[str, Any]:
        pending = self.pending.get(str(state))
        if pending is None or pending.consumed or not code:
            raise PermissionError("catalog OAuth callback unavailable")
        pending.consumed = True
        if pending.expiry:
            pending.expiry.cancel()
        pending.callback.set_result((str(code), str(state)))
        try:
            if pending.task is None:
                raise RuntimeError("catalog OAuth task unavailable")
            return await asyncio.wait_for(asyncio.shield(pending.task), timeout=60)
        finally:
            self.pending.pop(str(state), None)

    async def _complete_flow(
        self,
        profile: str,
        subject: str,
        row: dict[str, Any],
        endpoint: str,
        provider: OAuthClientProvider,
        storage: _OAuthStorage,
    ) -> dict[str, Any]:
        schema = row["credential_schema"]

        async def validate_request(request: httpx.Request) -> None:
            validate_remote_endpoint(str(request.url), resolver=self.resolver)

        try:
            async with httpx.AsyncClient(
                auth=provider,
                transport=self.transport,
                timeout=30,
                follow_redirects=False,
                trust_env=False,
                event_hooks={"request": [validate_request]},
            ) as client:
                response = await client.post(
                    endpoint,
                    headers={"MCP-Protocol-Version": _latest_protocol_version()},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": _latest_protocol_version(),
                            "capabilities": {},
                            "clientInfo": {"name": "hermes-connectors", "version": "1"},
                        },
                    },
                )
                response.raise_for_status()

            if provider.context.oauth_metadata is None:
                raise RuntimeError("catalog OAuth metadata unavailable")
            storage.set_metadata(provider.context.oauth_metadata)

            name = "catalog-" + hashlib.sha256(str(row["row_key"]).encode()).hexdigest()[:24]
            store = CustomConnectorStore(
                self.db_path,
                encryption_key=self.encryption_key,
                resolver=self.resolver,
            )
            try:
                connector = store.install_catalog_oauth(
                    profile,
                    subject,
                    name=name,
                    transport=str(row["transport"]),
                    endpoint=endpoint,
                    credential_schema=schema,
                )
            finally:
                store.close()
            try:
                await self.verify(self.db_path, profile, subject, connector["connector_id"])
                store = CustomConnectorStore(
                    self.db_path,
                    encryption_key=self.encryption_key,
                    resolver=self.resolver,
                )
                try:
                    return store.set_state(profile, subject, connector["connector_id"], "ready")
                finally:
                    store.close()
            except Exception:
                store = CustomConnectorStore(
                    self.db_path,
                    encryption_key=self.encryption_key,
                    resolver=self.resolver,
                )
                try:
                    store.delete(profile, subject, connector["connector_id"])
                finally:
                    store.close()
                raise
        except Exception:
            vault = CredentialStore(self.db_path, encryption_key=self.encryption_key)
            try:
                vault.delete_credential(
                    profile_name=profile,
                    subject_id=subject,
                    provider=str(schema["provider"]),
                    secret_kind="oauth",
                )
            finally:
                vault.close()
            raise


async def refresh_catalog_oauth(
    db_path: Path | str,
    profile: str,
    subject: str,
    provider_name: str,
    endpoint: str,
    *,
    encryption_key: str | bytes | None = None,
    resolver: Callable[..., list[tuple[Any, ...]]],
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Refresh one expired owner-bound catalog token without starting browser auth."""
    storage = _OAuthStorage(
        Path(db_path), {"provider": provider_name}, profile, subject, encryption_key
    )
    if storage.expires_at and storage.expires_at > int(time.time() * 1000) + 60_000:
        return
    if not storage.tokens or not storage.tokens.refresh_token or not storage.client or not storage.metadata:
        raise PermissionError("connector OAuth credential requires authorization")

    endpoint = validate_remote_endpoint(endpoint, resolver=resolver).url
    oauth = OAuthClientProvider(
        endpoint,
        OAuthClientMetadata(
            redirect_uris=storage.client.redirect_uris,
            token_endpoint_auth_method=storage.client.token_endpoint_auth_method,
        ),
        storage,
        timeout=30,
    )
    oauth.context.current_tokens = storage.tokens
    oauth.context.client_info = storage.client
    oauth.context.oauth_metadata = storage.metadata
    oauth.context.protocol_version = _latest_protocol_version()
    request = await oauth._refresh_token()
    validate_remote_endpoint(str(request.url), resolver=resolver)
    async with httpx.AsyncClient(
        transport=transport, timeout=30, follow_redirects=False, trust_env=False
    ) as client:
        response = await client.send(request)
    if not await oauth._handle_refresh_response(response):
        raise PermissionError("connector OAuth refresh failed")
