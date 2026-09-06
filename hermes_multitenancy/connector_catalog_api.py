"""Owner-safe WebUI routes for the frozen catalog and custom remote MCPs."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import socket
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from .connector_custom_catalog import ConnectorCatalog, CustomConnectorStore
from .connector_custom_runtime import CustomConnectorRuntime
from .connector_catalog_oauth import CatalogOAuthBroker
from .connector_cli_runtime import (
    catalog_cli_spec,
    cli_status,
    logout_cli,
    prepare_cli_runtime,
    start_cli_auth,
    stop_cli_auth,
)
from .connector_stdio_runtime import catalog_stdio_spec, prepare_stdio_runtime


_catalog: ConnectorCatalog | None = None
_oauth_brokers: dict[Path, CatalogOAuthBroker] = {}
_cli_auth_sessions: dict[str, tuple[asyncio.subprocess.Process, Path, str, float]] = {}
_cli_auth_lock = asyncio.Lock()
_CLI_AUTH_TTL_SECONDS = 600
_logger = logging.getLogger(__name__)


async def _stop_cli_session(connector_id: str) -> None:
    async with _cli_auth_lock:
        session = _cli_auth_sessions.pop(connector_id, None)
        if session:
            await stop_cli_auth(*session[:3])


async def _prune_cli_sessions(*, now: float | None = None) -> None:
    cutoff = time.monotonic() if now is None else now
    # ponytail: one global lock is enough at current auth volume; shard only if measured contention appears.
    async with _cli_auth_lock:
        for connector_id, session in tuple(_cli_auth_sessions.items()):
            if session[3] <= cutoff:
                _cli_auth_sessions.pop(connector_id, None)
                await stop_cli_auth(*session[:3])


def _prune_cli_env_files(runtime_base: Path, *, now: float | None = None) -> None:
    cutoff = time.time() if now is None else now
    for path in (runtime_base.parent / "connector-cli-env").glob("cli-*.env"):
        try:
            if cutoff - path.stat().st_mtime >= _CLI_AUTH_TTL_SECONDS:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def _get_catalog() -> ConnectorCatalog:
    global _catalog
    if _catalog is None:
        _catalog = ConnectorCatalog.bundled()
    return _catalog


def _oauth_row_for_request(row: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    recovery = row.get("remote_recovery") or {}
    field = str(recovery.get("endpoint_field") or "")
    fields = body.get("fields") or {}
    if not field:
        if fields:
            raise ValueError("connector OAuth fields are not supported")
        return row
    if not isinstance(fields, dict) or set(fields) != {field}:
        raise ValueError("connector endpoint field is required")
    endpoint = str(fields[field]).strip()
    if not endpoint or len(endpoint) > 2048 or "\r" in endpoint or "\n" in endpoint:
        raise ValueError("connector endpoint is invalid")
    parsed = urlsplit(endpoint)
    host = str(parsed.hostname or "").casefold()
    suffix = str(recovery.get("endpoint_host_suffix") or "").casefold()
    prefix = str(recovery.get("endpoint_path_prefix") or "")
    domain = suffix.lstrip(".")
    path_root = prefix.rstrip("/")
    host_allowed = host.endswith(suffix) if suffix.startswith("-") else (
        host == domain or host.endswith(f".{domain}")
    )
    if (
        parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}
        or not domain or not host_allowed
        or not path_root or (parsed.path != path_root and not parsed.path.startswith(f"{path_root}/"))
        or parsed.query or parsed.fragment
    ):
        raise ValueError("connector endpoint is outside the official tenant domain")
    return {**row, "endpoint": endpoint}


async def _verify_catalog_connection(db_path: Path, profile_name: str, subject_id: str, connector_id: str) -> int:
    runtime = CustomConnectorRuntime(db_path)
    tools = await runtime.list_connector_tools(profile_name, subject_id, connector_id)
    if not tools:
        raise RuntimeError("remote MCP exposes no approved read-only tools")
    probe = next((tool for tool in tools if not (tool.get("inputSchema") or {}).get("required")), None)
    if probe is None:
        raise RuntimeError("remote MCP exposes no zero-input read-only verification tool")
    await runtime.call_tool(profile_name, subject_id, str(probe["name"]), {})
    return len(tools)


def _get_oauth_broker(db_path: Path) -> CatalogOAuthBroker:
    path = db_path.resolve()
    if path not in _oauth_brokers:
        _oauth_brokers[path] = CatalogOAuthBroker(
            path,
            resolver=socket.getaddrinfo,
            verify=_verify_catalog_connection,
        )
    return _oauth_brokers[path]


def _catalog_action(row: dict[str, Any], installed: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    verdict = str(row.get("final_verdict") or "")
    row_key = str(row["row_key"])
    transport = re.sub(r"[-_ ]", "", str(row.get("transport") or "").casefold())
    installation_name = "catalog-" + hashlib.sha256(row_key.encode()).hexdigest()[:24]
    if installed and installation_name in installed:
        installation = installed[installation_name]
        return {
            "kind": "revoke", "label": "Disconnect", "available": True,
            "installation_name": installation_name, "connector_id": installation["connector_id"],
            "status": {"active": "authorized", "ready": "ready"}.get(installation["state"], "error"),
        }
    if verdict in {"incompatible", "rejected"}:
        return {
            "kind": "blocked",
            "label": "Incompatible" if verdict == "incompatible" else "Blocked",
            "available": False,
            "status": "catalog",
        }
    if row_key == "workbuddy:feishu":
        return {
            "kind": "authorize", "label": "Authorize", "available": True,
            "installation_name": installation_name, "fields": [],
            "auth_flow": "feishu_device_flow", "status": "needs_auth",
        }
    try:
        catalog_cli_spec(row.get("cli_manifest") or {})
    except ValueError:
        pass
    else:
        return {
            "kind": "authorize_cli", "label": "Authorize", "available": True,
            "installation_name": installation_name, "fields": [], "status": "needs_auth",
        }
    recovery = row.get("remote_recovery") or {}
    if recovery.get("state") == "resolved":
        fields = list(recovery.get("fields") or [])
        oauth = recovery.get("auth_flow") == "mcp_oauth"
        return {
            "kind": "authorize" if fields or oauth else "connect",
            "label": "Authorize" if fields or oauth else "Connect",
            "available": True,
            "installation_name": installation_name,
            "transport": "sse" if recovery.get("transport") == "sse" else "streamableHttp",
            "endpoint": recovery["endpoint"],
            "fields": fields,
            **({"auth_flow": "mcp_oauth"} if oauth else {}),
            "status": "needs_auth" if fields or oauth else "catalog",
        }
    if verdict == "pass" and row.get("endpoint") and transport in {"http", "streamablehttp", "sse"}:
        return {
            "kind": "connect",
            "label": "Connect",
            "available": True,
            "installation_name": installation_name,
            "transport": "sse" if transport == "sse" else "streamableHttp",
            "status": "catalog",
        }
    schema = row.get("credential_schema") or {}
    if (
        verdict == "needs_auth"
        and row.get("endpoint")
        and transport in {"http", "streamablehttp", "sse"}
        and schema.get("auth_flow") == "mcp_oauth"
    ):
        return {
            "kind": "authorize",
            "label": "Authorize",
            "available": True,
            "installation_name": installation_name,
            "transport": "sse" if transport == "sse" else "streamableHttp",
            "auth_flow": "mcp_oauth",
            "status": "needs_auth",
        }
    if transport == "stdio":
        try:
            spec = catalog_stdio_spec(row.get("runtime_manifest") or {})
        except ValueError:
            pass
        else:
            return {
                "kind": "install_sandbox",
                "label": "Connect",
                "available": True,
                "installation_name": installation_name,
                "fields": spec["fields"],
                "status": "catalog",
            }
    if (
        verdict == "needs_auth"
        and row.get("endpoint")
        and transport in {"http", "streamablehttp", "sse"}
        and schema.get("auth_flow") in {"manual_token", "manual_fields"}
        and schema.get("fields")
        and int(schema.get("invalid_field_count") or 0) == 0
    ):
        return {
            "kind": "authorize",
            "label": "Authorize",
            "available": True,
            "installation_name": installation_name,
            "transport": "sse" if transport == "sse" else "streamableHttp",
            "fields": list(schema["fields"]),
            "status": "needs_auth",
        }
    kind, label = {
        "needs_auth": ("authorize", "Authorization required"),
        "needs_sandbox": ("install_sandbox", "Sandbox required"),
        "incompatible": ("blocked", "Incompatible"),
        "rejected": ("blocked", "Blocked"),
    }.get(verdict, ("blocked", "Unavailable"))
    return {"kind": kind, "label": label, "available": False, "status": "catalog"}


def _public_catalog_row(
    row: dict[str, Any],
    installed: dict[str, dict[str, str]] | None = None,
    *,
    feishu_ready: bool = False,
) -> dict[str, Any]:
    icon = row.get("icon") or {}
    row_key = str(row["row_key"])
    public = {
        key: row.get(key)
        for key in (
            "row_key", "canonical_key", "catalog_id", "name", "description", "product",
            "transport", "endpoint", "download_count", "download_count_status", "final_verdict",
            "reason_code", "tool_count", "credential_schema", "next_action", "risks",
        )
    }
    transport = re.sub(r"[-_ ]", "", str(row.get("transport") or "").casefold())
    if transport not in {"http", "streamablehttp", "sse"}:
        public["endpoint"] = None
    action = _catalog_action(row, installed)
    if row_key == "workbuddy:feishu" and feishu_ready:
        action = {**action, "kind": "revoke", "label": "Disconnect", "status": "ready"}
    return public | {
        "action": action,
        "icon": {
            "url": f"/api/run-broker/connector-catalog/icon?row_key={quote(row_key, safe='')}",
            "status": icon.get("status"),
            "source": icon.get("source"),
            "redistribution_status": icon.get("redistribution_status"),
        }
    }


def _public_canonical_row(row: dict[str, Any]) -> dict[str, Any]:
    verdicts = row.get("verdicts") or {}
    blocked = sum(int(verdicts.get(key) or 0) for key in ("incompatible", "rejected")) == int(
        row.get("source_row_count") or 0
    )
    return {
        "canonical_key": row.get("canonical_key"),
        "products": row.get("products") or [],
        "source_row_count": row.get("source_row_count"),
        "verdicts": verdicts,
        "reason_code": "all_sources_blocked" if blocked else "source_selection_required",
        "next_action": ("Review source rejection reasons" if blocked else "Choose one source before connecting"),
        "action": {
            "kind": "blocked" if blocked else "choose_source",
            "label": "Blocked" if blocked else "View sources",
            "available": not blocked,
            "status": "catalog",
        },
    }


def register_routes(
    app: Any,
    *,
    authorize: Callable[[Any], bool],
    owner_tenant: Callable[..., tuple[str, str]],
    shared_home: Callable[[], Path],
) -> None:
    from aiohttp import web

    def store() -> CustomConnectorStore:
        return CustomConnectorStore(shared_home() / "multitenancy.db")

    async def catalog(request):
        if not authorize(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, subject_id = owner_tenant(request)
            bundled = _get_catalog()
            canonical = str(request.query.get("view") or "source").casefold() == "canonical"
            def owner_state():
                current = store()
                try:
                    installed = {
                        row["name"]: {"connector_id": row["connector_id"], "state": row["state"]}
                        for row in current.list_installations(profile_name, subject_id)
                    }
                finally:
                    current.close()
                try:
                    from .feishu_uat_auth import credential_status

                    status = credential_status(
                        profile_name=profile_name,
                        open_id=subject_id,
                        shared_home=shared_home(),
                    )
                    lark = status.get("lark_cli") or {}
                    feishu_ready = bool(
                        status.get("status") == "valid"
                        and status.get("runtime_available")
                        and lark.get("available")
                        and lark.get("default_identity") == "user"
                    )
                except Exception:
                    feishu_ready = False
                return installed, feishu_ready

            installed, feishu_ready = await asyncio.to_thread(owner_state)
            rows = ([_public_canonical_row(row) for row in bundled.list_canonical()]
                    if canonical else [
                        _public_catalog_row(row, installed, feishu_ready=feishu_ready)
                        for row in bundled.list_rows()
                    ])
            return web.json_response({
                "profile_name": profile_name,
                "subject_id": subject_id,
                "view": "canonical" if canonical else "source",
                "source_count": len(bundled.list_rows()),
                "canonical_count": len(bundled.list_canonical()),
                "connectors": rows,
            })
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)

    async def icon(request):
        if not authorize(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            return web.FileResponse(_get_catalog().icon_path(str(request.query.get("row_key") or "")))
        except (KeyError, OSError):
            return web.json_response({"error": "connector icon unavailable"}, status=404)

    async def list_custom(request):
        if not authorize(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, subject_id = owner_tenant(request)
            def read_rows():
                current = store()
                try:
                    return current.list_installations(profile_name, subject_id)
                finally:
                    current.close()

            rows = await asyncio.to_thread(read_rows)
            return web.json_response({"profile_name": profile_name, "subject_id": subject_id, "connectors": rows})
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)

    async def import_custom(request):
        if not authorize(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("connector import body must be an object")
            profile_name, subject_id = owner_tenant(request, body, require_write=True)
            config = body.get("config")
            def write_rows():
                current = store()
                try:
                    return current.import_config(profile_name, subject_id, config)
                finally:
                    current.close()

            rows = await asyncio.to_thread(write_rows)
            return web.json_response(
                {"profile_name": profile_name, "subject_id": subject_id, "connectors": rows}, status=201
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            _logger.exception("custom connector import failed type=%s", type(exc).__name__)
            return web.json_response({"error": "custom connector import unavailable"}, status=500)

    async def connect_catalog(request):
        if not authorize(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) not in ({"row_key"}, {"row_key", "fields"}):
                raise ValueError("connector connect body must contain row_key and optional fields")
            profile_name, subject_id = owner_tenant(request, body, require_write=True)
            try:
                row = _get_catalog().get(str(body["row_key"]))
            except KeyError:
                return web.json_response({"error": "connector catalog row not found"}, status=404)
            if row["row_key"] == "workbuddy:feishu":
                from .feishu_uat_auth import credential_status, revoke_uat_credential

                status = await asyncio.to_thread(
                    credential_status,
                    profile_name=profile_name,
                    open_id=subject_id,
                    shared_home=shared_home(),
                )
                lark = status.get("lark_cli") or {}
                if (
                    status.get("status") == "valid"
                    and status.get("runtime_available")
                    and lark.get("available")
                    and lark.get("default_identity") == "user"
                ):
                    removed = await asyncio.to_thread(
                        revoke_uat_credential,
                        profile_name=profile_name,
                        open_id=subject_id,
                        shared_home=shared_home(),
                    )
                    return web.json_response({"ok": removed, "status": "revoked"})
            def installed_for_owner():
                current = store()
                try:
                    return {
                        item["name"]: {"connector_id": item["connector_id"], "state": item["state"]}
                        for item in current.list_installations(profile_name, subject_id)
                    }
                finally:
                    current.close()

            action = _catalog_action(row, await asyncio.to_thread(installed_for_owner))
            if action["kind"] == "revoke":
                return web.json_response({"error": "connector is already connected"}, status=409)
            if not action["available"]:
                return web.json_response({"error": str(row.get("next_action") or action["label"])}, status=409)
            if action.get("auth_flow") == "mcp_oauth":
                origin = str(os.environ.get("HERMES_MCP_PUBLIC_ORIGIN") or "").strip().rstrip("/")
                if not origin.startswith("https://"):
                    return web.json_response({"error": "catalog OAuth public origin is unavailable"}, status=503)
                oauth_row = _oauth_row_for_request(row, body)
                started = await _get_oauth_broker(shared_home() / "multitenancy.db").start(
                    profile_name,
                    subject_id,
                    oauth_row,
                    redirect_uri=f"{origin}/api/auth/skill-credentials/catalog/oauth/callback",
                )
                return web.json_response({
                    "profile_name": profile_name,
                    "subject_id": subject_id,
                    **started,
                }, status=202)
            if action["kind"] == "authorize_cli":
                runtime_base = shared_home() / "connector-runtimes"
                await _prune_cli_sessions()
                await asyncio.to_thread(_prune_cli_env_files, runtime_base)
                await prepare_cli_runtime(runtime_base, row["cli_manifest"])

                def install_cli():
                    current = store()
                    try:
                        existing = next((item for item in current.list_installations(profile_name, subject_id)
                                         if item["name"] == action["installation_name"]), None)
                        return existing or current.install_catalog_cli(
                            profile_name,
                            subject_id,
                            name=action["installation_name"],
                            row_key=row["row_key"],
                            runtime_manifest=row["cli_manifest"],
                        )
                    finally:
                        current.close()

                connector = await asyncio.to_thread(install_cli)
                if await cli_status(runtime_base, connector["connector_id"], row["cli_manifest"]):
                    def ready_cli():
                        current = store()
                        try:
                            return current.set_state(profile_name, subject_id, connector["connector_id"], "ready")
                        finally:
                            current.close()
                    return web.json_response({
                        "profile_name": profile_name,
                        "subject_id": subject_id,
                        "connector": await asyncio.to_thread(ready_cli),
                    }, status=201)
                async with _cli_auth_lock:
                    old = _cli_auth_sessions.pop(connector["connector_id"], None)
                    if old:
                        await stop_cli_auth(*old[:3])
                    authorization_url, process, env_path, unit = await start_cli_auth(
                        runtime_base, connector["connector_id"], row["cli_manifest"]
                    )
                    _cli_auth_sessions[connector["connector_id"]] = (
                        process, env_path, unit, time.monotonic() + _CLI_AUTH_TTL_SECONDS
                    )
                return web.json_response({
                    "profile_name": profile_name,
                    "subject_id": subject_id,
                    "connector_id": connector["connector_id"],
                    "authorization_url": authorization_url,
                    "status": "authorizing",
                }, status=202)
            if action["kind"] == "install_sandbox":
                await prepare_stdio_runtime(
                    shared_home() / "connector-runtimes",
                    row["runtime_manifest"],
                )

            def connect_row():
                current = store()
                try:
                    existing = next((item for item in current.list_installations(profile_name, subject_id)
                                     if item["name"] == action["installation_name"]), None)
                    if existing:
                        return existing, False
                    if action["kind"] == "install_sandbox":
                        fields = body.get("fields") or {}
                        if not isinstance(fields, dict):
                            raise ValueError("connector credential fields are required")
                        created = current.install_catalog_stdio(
                            profile_name,
                            subject_id,
                            name=action["installation_name"],
                            row_key=row["row_key"],
                            runtime_manifest=row["runtime_manifest"],
                            fields=fields,
                        )
                    elif action["kind"] == "authorize":
                        fields = body.get("fields")
                        if not isinstance(fields, dict):
                            raise ValueError("connector credential fields are required")
                        created = current.install_catalog(
                            profile_name,
                            subject_id,
                            name=action["installation_name"],
                            transport=action["transport"],
                            endpoint=str(action.get("endpoint") or row["endpoint"]),
                            credential_schema=row["credential_schema"],
                            fields=fields,
                        )
                    else:
                        created = current.import_config(profile_name, subject_id, {"mcpServers": {
                                action["installation_name"]: {
                                    "type": action["transport"],
                                    "url": action.get("endpoint") or row["endpoint"],
                                },
                            }})[0]
                    return created, True
                finally:
                    current.close()

            connector, created = await asyncio.to_thread(connect_row)
            if created:
                try:
                    await _verify_catalog_connection(
                        shared_home() / "multitenancy.db", profile_name, subject_id, connector["connector_id"]
                    )
                except Exception:
                    def rollback():
                        current = store()
                        try:
                            current.delete(profile_name, subject_id, connector["connector_id"])
                        finally:
                            current.close()
                    await asyncio.to_thread(rollback)
                    raise

                def mark_ready():
                    current = store()
                    try:
                        current.set_state(profile_name, subject_id, connector["connector_id"], "ready")
                        return current.list_installations(profile_name, subject_id)
                    finally:
                        current.close()
                rows = await asyncio.to_thread(mark_ready)
            else:
                def current_rows():
                    current = store()
                    try:
                        return current.list_installations(profile_name, subject_id)
                    finally:
                        current.close()
                rows = await asyncio.to_thread(current_rows)
            return web.json_response(
                {"profile_name": profile_name, "subject_id": subject_id, "connectors": rows}, status=201
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            _logger.exception("catalog connector connect failed type=%s", type(exc).__name__)
            return web.json_response({"error": "catalog connector runtime verification failed"}, status=502)

    async def catalog_status(request):
        if not authorize(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"row_key"}:
                raise ValueError("connector status body must contain row_key")
            profile_name, subject_id = owner_tenant(request, body)
            row = _get_catalog().get(str(body["row_key"]))
            action = _catalog_action(row)
            if action.get("kind") != "authorize_cli":
                raise ValueError("connector does not support CLI authorization status")
            installation_name = action["installation_name"]

            def read_cli():
                current = store()
                try:
                    connector = next((item for item in current.list_installations(profile_name, subject_id)
                                      if item["name"] == installation_name), None)
                    if connector is None or connector["transport"] != "cli":
                        raise PermissionError("connector installation unavailable for current owner")
                    return connector, current.get_runtime(profile_name, subject_id, connector["connector_id"])
                finally:
                    current.close()

            connector, runtime = await asyncio.to_thread(read_cli)
            ready = await cli_status(
                shared_home() / "connector-runtimes",
                connector["connector_id"],
                runtime["runtime_manifest"],
            )
            if ready:
                await _stop_cli_session(connector["connector_id"])
                def mark_ready():
                    current = store()
                    try:
                        return current.set_state(profile_name, subject_id, connector["connector_id"], "ready")
                    finally:
                        current.close()
                connector = await asyncio.to_thread(mark_ready)
            return web.json_response({
                "profile_name": profile_name,
                "subject_id": subject_id,
                "connector": connector,
                "ready": ready,
            })
        except KeyError:
            return web.json_response({"error": "connector catalog row not found"}, status=404)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            _logger.exception("catalog connector status failed type=%s", type(exc).__name__)
            return web.json_response({"error": "catalog connector status unavailable"}, status=502)

    async def delete_custom(request):
        if not authorize(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, subject_id = owner_tenant(request, require_write=True)
            connector_id = str(request.match_info.get("connector_id") or "")

            def read_runtime():
                current = store()
                try:
                    return current.get_runtime(profile_name, subject_id, connector_id)
                finally:
                    current.close()

            runtime = await asyncio.to_thread(read_runtime)
            if runtime["transport"] == "cli":
                await _stop_cli_session(connector_id)
                await logout_cli(
                    shared_home() / "connector-runtimes",
                    connector_id,
                    runtime["runtime_manifest"],
                )

            def remove_row():
                current = store()
                try:
                    return current.delete(profile_name, subject_id, connector_id)
                finally:
                    current.close()

            await asyncio.to_thread(remove_row)
            return web.json_response({"ok": True})
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def complete_catalog_oauth(request):
        if not authorize(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"state", "code"}:
                raise ValueError("catalog OAuth callback must contain state and code")
            state, code = str(body["state"]), str(body["code"])
            if not state or len(state) > 512 or not code or len(code) > 8192:
                raise ValueError("invalid catalog OAuth callback")
            connector = await _get_oauth_broker(shared_home() / "multitenancy.db").complete(state, code)
            return web.json_response({"ok": True, "connector": connector})
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            _logger.exception("catalog OAuth callback failed type=%s", type(exc).__name__)
            return web.json_response({"error": "catalog OAuth callback failed"}, status=502)

    app.router.add_get("/api/run-broker/connector-catalog", catalog)
    app.router.add_get("/api/run-broker/connector-catalog/icon", icon)
    app.router.add_post("/api/run-broker/connector-catalog/connect", connect_catalog)
    app.router.add_post("/api/run-broker/connector-catalog/status", catalog_status)
    app.router.add_post("/api/run-broker/connector-catalog/oauth/callback", complete_catalog_oauth)
    app.router.add_get("/api/run-broker/custom-connectors", list_custom)
    app.router.add_post("/api/run-broker/custom-connectors/import", import_custom)
    app.router.add_delete("/api/run-broker/custom-connectors/{connector_id}", delete_custom)
