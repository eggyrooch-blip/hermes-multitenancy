"""One owner-scoped GitHub MCP bundle; secrets never enter the agent child."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from .connectors.models import AuthAction, ConnectorStatus
from .credentials import CredentialStore


CONNECTOR_ID = "github-mcp"
PROVIDER = "github"
REMOTE_URL = "https://api.githubcopilot.com/mcp/"
READ_ONLY_TOOLS = frozenset({
    "get_me",
    "search_repositories",
    "search_code",
    "get_file_contents",
    "list_branches",
    "list_commits",
    "issue_read",
    "pull_request_read",
})
_MANAGED_MARKER = "<!-- hermes-managed: github-mcp v1 -->"
_SKILL = f"""---
name: github-mcp
description: Read GitHub repositories, code, issues and pull requests through the current user's connector.
requires_connectors: [github-mcp]
---
{_MANAGED_MARKER}

# GitHub MCP

Use the `hermes-connectors` MCP tools for GitHub research. Start with `get_me`
when identity matters. This bundle is read-only: never claim a write succeeded,
and never ask for or expose a PAT in chat.
"""
_SERVER_ARGS = ["-m", "hermes_multitenancy.connector_mcp_stdio"]
_SERVER_ENV = {
    "HERMES_RUN_BROKER_URL": "${HERMES_RUN_BROKER_URL}",
    "HERMES_MULTITENANCY_RUN_BROKER_URL": "${HERMES_MULTITENANCY_RUN_BROKER_URL}",
    "HERMES_RUN_BROKER_KEY": "${HERMES_RUN_BROKER_KEY}",
}


class ConnectorUnavailable(PermissionError):
    pass


def _profile_home(shared_home: Path | str, profile_name: str) -> Path:
    name = str(profile_name or "").strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\0" in name:
        raise ValueError("invalid profile_name")
    return Path(shared_home).resolve() / "profiles" / name


def _store(shared_home: Path | str) -> CredentialStore:
    return CredentialStore(Path(shared_home) / "multitenancy.db")


def _account_hint(login: str) -> str:
    login = str(login or "").strip()
    return login if len(login) <= 7 else f"{login[:4]}…{login[-3:]}"


def probe_github_user(token: str) -> dict[str, Any]:
    if not str(token or "").strip():
        raise ConnectorUnavailable("GitHub token is required")
    request = urllib.request.Request(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hermes-multitenancy",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise ConnectorUnavailable("GitHub token is invalid or unauthorized") from None
        raise ConnectorUnavailable("GitHub identity verification failed") from None
    except Exception:
        raise ConnectorUnavailable("GitHub identity verification is unavailable") from None
    if not isinstance(body, dict) or not body.get("id") or not body.get("login"):
        raise ConnectorUnavailable("GitHub identity response is incomplete")
    return body


def _clean_token(token: Any) -> str:
    value = str(token or "").strip()
    if not value:
        raise ConnectorUnavailable("GitHub token is required")
    if len(value) > 512 or not value.isprintable() or any(char.isspace() for char in value):
        raise ConnectorUnavailable("GitHub token is malformed")
    return value


def ensure_bundle(shared_home: Path | str, profile_name: str) -> None:
    profile = _profile_home(shared_home, profile_name)
    if not profile.is_dir():
        raise ConnectorUnavailable("profile home is unavailable")
    skill_path = profile / "skills" / CONNECTOR_ID / "SKILL.md"
    if skill_path.exists():
        if not skill_path.is_file() or _MANAGED_MARKER not in skill_path.read_text(encoding="utf-8"):
            raise ConnectorUnavailable("an unmanaged github-mcp skill already exists")
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_text(skill_path, _SKILL)

    config_path = profile / "config.yaml"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except Exception as exc:
        raise ConnectorUnavailable("profile config is invalid") from exc
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ConnectorUnavailable("profile config must be an object")
    servers = config.setdefault("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ConnectorUnavailable("profile mcp_servers config must be an object")
    expected = {"command": sys.executable, "args": _SERVER_ARGS, "env": _SERVER_ENV}
    existing = servers.get("hermes-connectors")
    managed_existing = (
        isinstance(existing, dict)
        and existing.get("command") in {"python3", sys.executable}
        and existing.get("args") == _SERVER_ARGS
        and existing.get("env") == _SERVER_ENV
    )
    if existing is not None and not managed_existing:
        raise ConnectorUnavailable("profile has a conflicting hermes-connectors MCP server")
    if existing != expected:
        servers["hermes-connectors"] = expected
        _atomic_text(config_path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def connect(
    shared_home: Path | str,
    profile_name: str,
    subject_id: str,
    token: str,
    *,
    probe: Callable[[str], dict[str, Any]] = probe_github_user,
) -> dict[str, Any]:
    token = _clean_token(token)
    identity = probe(token)
    github_id = str(identity.get("id") or "").strip()
    login = str(identity.get("login") or "").strip()
    if not github_id or not login:
        raise ConnectorUnavailable("GitHub identity response is incomplete")
    store = _store(shared_home)
    try:
        # put_credential executes INSERT before commit_if, so SQLite already holds
        # the writer transaction here; a concurrent second owner waits, then sees
        # this row. The identity check and final commit are therefore one unit.
        stored = store.put_credential(
            profile_name=profile_name,
            subject_id=subject_id,
            provider=PROVIDER,
            secret_kind="token",
            payload={"token": token, "github_id": github_id, "login": login},
            scopes=["read_only"],
            commit_if=lambda _connection: store.payload_value_is_unique(
                provider=PROVIDER,
                secret_kind="token",
                field="github_id",
                value=github_id,
                owner_profile=profile_name,
                owner_subject=subject_id,
            ),
        )
    finally:
        store.close()
    if not stored:
        raise ConnectorUnavailable("GitHub account is already bound to another user")
    try:
        ensure_bundle(shared_home, profile_name)
    except Exception:
        revoke(shared_home, profile_name, subject_id)
        raise
    return {"ok": True, "account_hint": _account_hint(login)}


def revoke(shared_home: Path | str, profile_name: str, subject_id: str) -> bool:
    store = _store(shared_home)
    try:
        return store.delete_credential(
            profile_name=profile_name,
            subject_id=subject_id,
            provider=PROVIDER,
            secret_kind="token",
        )
    finally:
        store.close()


def status(shared_home: Path | str, profile_name: str, subject_id: str) -> ConnectorStatus:
    installed = (_profile_home(shared_home, profile_name) / "skills" / CONNECTOR_ID / "SKILL.md").is_file()
    store = _store(shared_home)
    try:
        record = store.get_status(
            profile_name=profile_name,
            subject_id=subject_id,
            provider=PROVIDER,
            secret_kind="token",
        )
        state = "needs_auth"
        hint = None
        if record.get("status") == "valid" and record.get("has_payload"):
            payload = store.get_secret_for_runtime(
                profile_name=profile_name,
                subject_id=subject_id,
                provider=PROVIDER,
                secret_kind="token",
            )
            state = "authenticated"
            hint = _account_hint(str(payload.get("login") or "")) or None
    except Exception:
        state, hint = "error", None
    finally:
        store.close()
    return ConnectorStatus(
        id=CONNECTOR_ID,
        title="GitHub",
        provider=PROVIDER,
        installed=installed,
        status=state,  # type: ignore[arg-type]
        account_hint=hint,
        detail="官方远程 MCP · 配套 Skill · 默认只读 · 个人凭证",
        required_by=[CONNECTOR_ID] if installed else [],
        action=AuthAction(kind="manual", label="重新连接" if state == "authenticated" else "连接"),
        profile=profile_name,
        scope="profile",
        acting_identity="user",
        credential_owner=profile_name,
        runtime_policy_owner="run_broker",
        kind="external",
    )


async def _runtime_token(
    shared_home: Path | str,
    profile_name: str,
    subject_id: str,
    probe: Callable[[str], dict[str, Any]],
) -> str:
    store = _store(shared_home)
    try:
        try:
            payload = store.get_secret_for_runtime(
                profile_name=profile_name,
                subject_id=subject_id,
                provider=PROVIDER,
                secret_kind="token",
            )
        except PermissionError as exc:
            raise ConnectorUnavailable("credential not found for current owner") from exc
    finally:
        store.close()
    token = str(payload.get("token") or "")
    live = await asyncio.to_thread(probe, token)
    if str(live.get("id") or "") != str(payload.get("github_id") or ""):
        raise ConnectorUnavailable("GitHub credential owner changed")
    return token


async def list_tools(
    shared_home: Path | str,
    profile_name: str,
    subject_id: str,
    *,
    probe: Callable[[str], dict[str, Any]] = probe_github_user,
    remote_list: Callable[[str], Awaitable[list[dict[str, Any]]]] | None = None,
) -> list[dict[str, Any]]:
    token = await _runtime_token(shared_home, profile_name, subject_id, probe)
    tools = await (remote_list or _remote_list_tools)(token)
    return [tool for tool in tools if str(tool.get("name") or "") in READ_ONLY_TOOLS]


async def call_tool(
    shared_home: Path | str,
    profile_name: str,
    subject_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    probe: Callable[[str], dict[str, Any]] = probe_github_user,
    remote_call: Callable[[str, str, dict[str, Any]], Awaitable[Any]] | None = None,
) -> Any:
    if tool_name not in READ_ONLY_TOOLS:
        raise PermissionError(f"GitHub MCP tool {tool_name!r} is not allowlisted")
    token = await _runtime_token(shared_home, profile_name, subject_id, probe)
    return await (remote_call or _remote_call_tool)(token, tool_name, arguments)


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-MCP-Readonly": "true",
        "X-MCP-Toolsets": "context,repos,issues,pull_requests",
    }


async def _session(token: str):
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:
        raise ConnectorUnavailable("MCP SDK is not installed") from exc
    return ClientSession, streamable_http_client


async def _remote_list_tools(token: str) -> list[dict[str, Any]]:
    import httpx

    ClientSession, transport = await _session(token)
    async with httpx.AsyncClient(headers=_headers(token), timeout=120) as client:
        async with transport(REMOTE_URL, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return [
                    {
                        "name": tool.name,
                        "description": tool.description or "",
                        "inputSchema": tool.inputSchema,
                    }
                    for tool in result.tools
                ]


async def _remote_call_tool(token: str, tool_name: str, arguments: dict[str, Any]) -> Any:
    import httpx

    ClientSession, transport = await _session(token)
    async with httpx.AsyncClient(headers=_headers(token), timeout=120) as client:
        async with transport(REMOTE_URL, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                return result.model_dump(mode="json") if hasattr(result, "model_dump") else result
