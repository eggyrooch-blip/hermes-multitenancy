"""Frozen connector catalog and owner-scoped custom remote MCP definitions."""
from __future__ import annotations

import hashlib
import json
import re
import socket
import sqlite3
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

import yaml

from .connector_remote_probe import validate_remote_endpoint
from .credentials import CredentialStore


_DATA = Path(__file__).with_name("connector_catalog_data")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEADER = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_FORBIDDEN_HEADERS = {
    "accept", "connection", "content-length", "content-type", "host", "mcp-protocol-version",
    "mcp-session-id", "proxy-authorization", "transfer-encoding",
}


def _owner_https_adapter() -> dict[str, Any]:
    return {
        "state": "resolved",
        "endpoint": "https://adapter.invalid/mcp",
        "transport": "streamable_http",
        "fields": ["MCP_SERVER_URL", "MCP_AUTHORIZATION"],
        "field_targets": {
            "MCP_SERVER_URL": {"kind": "endpoint_base", "path": ""},
            "MCP_AUTHORIZATION": {"kind": "header", "name": "Authorization"},
        },
        "adapter_contract": "owner_supplied_https_mcp",
    }


_REMOTE_TEMPLATES = {
    "workbuddy:baidu-netdisk": {
        "state": "resolved",
        "endpoint": "https://mcp-pan.baidu.com/sse",
        "transport": "sse",
        "fields": ["BAIDU_NETDISK_ACCESS_TOKEN"],
        "field_targets": {
            "BAIDU_NETDISK_ACCESS_TOKEN": {"kind": "query", "name": "access_token"},
        },
    },
    "workbuddy:h3yun-connector": {
        "state": "resolved",
        "endpoint": "https://www.h3yun.com/v1/agent/mcp",
        "transport": "streamable_http",
        "fields": ["H3YUN_API_BASE_URL", "H3YUN_TOKEN"],
        "field_targets": {
            "H3YUN_API_BASE_URL": {"kind": "endpoint_base", "path": "/v1/agent/mcp"},
            "H3YUN_TOKEN": {"kind": "header", "name": "Authorization", "prefix": "Bearer "},
        },
    },
    **{
        row_key: _owner_https_adapter()
        for row_key in {
            "workbuddy:ctrip-wendao",
            "workbuddy:netease-mail",
            "workbuddy:mastergo-vibe-mcp",
            "workbuddy:emr-query",
            "workbuddy:seeyon-office-marketing-suite",
            "workbuddy:woscli",
            "workbuddy:mglc",
            "workbuddy:uupt",
            "trae solo cn:builtin-macos-connectors",
            "doubaowork:vendor-cloud-catalog",
        }
    },
}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS multitenancy_custom_connector_definitions (
    profile_name TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    connector_id TEXT NOT NULL,
    name TEXT NOT NULL,
    transport TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    credential_provider TEXT NOT NULL,
    credential_secret_kind TEXT NOT NULL DEFAULT 'headers',
    credential_fields_json TEXT NOT NULL,
    runtime_manifest_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (profile_name, subject_id, connector_id)
);
"""


def _clean_id(label: str, value: str) -> str:
    text = str(value or "").strip()
    if not _ID.fullmatch(text):
        raise ValueError(f"invalid {label}")
    return text


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class ConnectorCatalog:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._rows = _load_jsonl(self.root / "connectors.jsonl")
        stdio_manifests = {
            str(item["row_key"]): item
            for item in _load_jsonl(self.root / "stdio_manifests.jsonl")
        }
        if len(stdio_manifests) != 482:
            raise ValueError("bundled stdio manifest count mismatch")
        npm_resolutions = {
            str(item["row_key"]): item
            for item in _load_jsonl(self.root / "stdio_npm_resolutions.jsonl")
        }
        if len(npm_resolutions) != 171:
            raise ValueError("bundled npm resolution count mismatch")
        npm_locks = {
            f"{item['package']}\0{item['version']}": item
            for item in _load_jsonl(self.root / "stdio_npm_locks.jsonl")
        }
        if len(npm_locks) != 117:
            raise ValueError("bundled npm dependency lock count mismatch")
        python_resolutions = {
            str(item["row_key"]): item
            for item in _load_jsonl(self.root / "stdio_python_resolutions.jsonl")
        }
        if len(python_resolutions) != 190:
            raise ValueError("bundled Python resolution count mismatch")
        python_locks = {
            str(item["resolution_fingerprint"]): item
            for item in _load_jsonl(self.root / "stdio_python_locks.jsonl")
        }
        if len(python_locks) != 54:
            raise ValueError("bundled Python dependency lock count mismatch")
        python_git_locks = {
            str(item["pinned_source"]): item
            for item in _load_jsonl(self.root / "stdio_python_git_locks.jsonl")
        }
        if len(python_git_locks) != 77:
            raise ValueError("bundled Python Git lock count mismatch")
        remote_recoveries = {
            str(item["row_key"]): item
            for item in _load_jsonl(self.root / "stdio_remote_recoveries.jsonl")
        }
        if len(remote_recoveries) != 172:
            raise ValueError("bundled README recovery count mismatch")
        cli_manifests = {
            str(item["row_key"]): item
            for item in _load_jsonl(self.root / "workbuddy_cli_manifests.jsonl")
        }
        cli_resolutions = {
            str(item["row_key"]): item
            for item in _load_jsonl(self.root / "workbuddy_cli_npm_resolutions.jsonl")
        }
        cli_locks = {
            f"{item['package']}\0{item['version']}": item
            for item in _load_jsonl(self.root / "workbuddy_cli_npm_locks.jsonl")
        }
        if len(cli_manifests) != 27 or len(cli_resolutions) != 19 or len(cli_locks) != 19:
            raise ValueError("bundled WorkBuddy CLI manifest count mismatch")
        for row_key, manifest in cli_manifests.items():
            if manifest.get("state") != "npm_resolvable":
                continue
            resolution = cli_resolutions.get(row_key)
            if resolution is None:
                raise ValueError(f"bundled WorkBuddy CLI resolution missing: {row_key}")
            lock = cli_locks.get(f"{resolution['package']}\0{resolution['version']}")
            if lock is None:
                raise ValueError(f"bundled WorkBuddy CLI lock missing: {row_key}")
            resolution["dependency_lock"] = lock
            manifest["package_resolution"] = resolution
        for recovery in remote_recoveries.values():
            if recovery.get("state") == "stdio_resolved":
                stdio_manifests[str(recovery["row_key"])] = recovery["runtime_manifest"]
        for row in self._rows:
            cli_manifest = cli_manifests.get(str(row["row_key"]))
            if row.get("row_key") == "workbuddy:textin-xparse-ai":
                cli_manifest = cli_manifests.get("workbuddy:textin-xparse")
            if cli_manifest is not None:
                row["cli_manifest"] = cli_manifest
            if str(row.get("transport") or "").casefold() == "stdio":
                manifest = stdio_manifests.get(str(row["row_key"]))
                if manifest is None:
                    raise ValueError(f"bundled stdio manifest missing: {row['row_key']}")
                if manifest.get("command") == "npx":
                    resolution = npm_resolutions.get(str(row["row_key"]))
                    if resolution is None:
                        raise ValueError(f"bundled npm resolution missing: {row['row_key']}")
                    if resolution.get("state") == "resolved":
                        dependency_lock = npm_locks.get(
                            f"{resolution['package']}\0{resolution['version']}"
                        )
                        if dependency_lock is None:
                            raise ValueError(f"bundled npm dependency lock missing: {row['row_key']}")
                        resolution["dependency_lock"] = dependency_lock
                    manifest["package_resolution"] = resolution
                elif (
                    manifest.get("command") == "uvx"
                    or (python_resolutions.get(str(row["row_key"])) or {}).get("normalized_command") == "uvx"
                ):
                    resolution = python_resolutions.get(str(row["row_key"]))
                    if resolution is None:
                        raise ValueError(f"bundled Python resolution missing: {row['row_key']}")
                    if resolution.get("state") == "pypi_resolved":
                        dependency_lock = python_locks.get(str(resolution["resolution_fingerprint"]))
                        if dependency_lock is None:
                            raise ValueError(f"bundled Python dependency lock missing: {row['row_key']}")
                        resolution["dependency_lock"] = dependency_lock
                    elif resolution.get("state") == "git_resolved":
                        dependency_lock = python_git_locks.get(str(resolution["pinned_source"]))
                        if dependency_lock is None:
                            raise ValueError(f"bundled Python Git lock missing: {row['row_key']}")
                        resolution["dependency_lock"] = dependency_lock
                        if resolution.get("subdirectory") == "server/mcp_server_bytehouse":
                            resolution["runtime_args"] = []
                    manifest["command"] = "uvx"
                    manifest["package_resolution"] = resolution
                elif row["row_key"] == "trae solo cn:modelcontextprotocol.servers_time":
                    resolution = next(
                        item.copy() for item in python_resolutions.values()
                        if item.get("package") == "mcp-server-time"
                    )
                    resolution["dependency_lock"] = python_locks[resolution["resolution_fingerprint"]]
                    manifest["command"] = "uvx"
                    manifest["package_resolution"] = resolution
                row["runtime_manifest"] = manifest
            fields: list[str] = []
            recovery = _REMOTE_TEMPLATES.get(str(row["row_key"])) or remote_recoveries.get(str(row["row_key"]))
            if recovery and recovery.get("state") == "resolved":
                fields = list(recovery.get("fields") or [])
                row["remote_recovery"] = recovery
                if fields or recovery.get("auth_flow") == "mcp_oauth":
                    row_key = str(row["row_key"])
                    row["credential_schema"] = {
                        "row_key": row_key,
                        "provider": f"connector:{hashlib.sha256(row_key.encode()).hexdigest()[:16]}",
                        "secret_kind": "oauth" if recovery.get("auth_flow") == "mcp_oauth" else "config",
                        "auth_flow": recovery.get("auth_flow") or "manual_fields",
                        "fields": fields,
                        "field_targets": recovery.get("field_targets") or {},
                        "invalid_field_count": 0,
                        "storage": "multitenancy_credentials",
                        "binding": ["profile_name", "subject_id", "provider", "secret_kind"],
                        "status_without_credential": "needs_auth",
                    }
            schema = row.get("credential_schema")
            if row.get("final_verdict") == "needs_auth" and not schema:
                row_key = str(row["row_key"])
                schema = row["credential_schema"] = {
                    "row_key": row_key,
                    "provider": f"connector:{hashlib.sha256(row_key.encode()).hexdigest()[:16]}",
                    "secret_kind": "oauth",
                    "auth_flow": "mcp_oauth",
                    "fields": [],
                    "invalid_field_count": 0,
                    "storage": "multitenancy_credentials",
                    "binding": ["profile_name", "subject_id", "provider", "secret_kind"],
                    "status_without_credential": "needs_auth",
                }
            if schema and schema.get("auth_flow") in {"manual_token", "manual_fields"}:
                fields = [field for field in schema.get("fields") or [] if str(field).casefold() != "accept"]
                if row.get("row_key") == "workbuddy:fuma-ai-callout" and "access-token" not in fields:
                    fields.append("access-token")
                schema["fields"] = sorted(fields)
                schema["invalid_field_count"] = (
                    0 if row.get("row_key") == "workbuddy:fuma-ai-callout"
                    else max(0, int(schema.get("invalid_field_count") or 0))
                )
            effective_verdict = ""
            if str(row.get("row_key")) == "workbuddy:feishu":
                effective_verdict = "needs_auth"
            elif recovery and recovery.get("state") == "resolved":
                effective_verdict = "needs_auth" if fields or recovery.get("auth_flow") else "pass"
            elif cli_manifest is not None:
                try:
                    from .connector_cli_runtime import catalog_cli_spec

                    catalog_cli_spec(cli_manifest)
                    effective_verdict = "needs_auth"
                except ValueError:
                    pass
            elif str(row.get("transport") or "").casefold() == "stdio":
                try:
                    from .connector_stdio_runtime import catalog_stdio_spec

                    catalog_stdio_spec(row.get("runtime_manifest") or {})
                    effective_verdict = "needs_sandbox"
                except ValueError:
                    pass
            if effective_verdict:
                row["source_verdict"] = row.get("final_verdict")
                row["final_verdict"] = effective_verdict
                row["reason_code"] = "admitted_runtime"
                row["next_action"] = "Connect with the admitted owner-bound runtime"
        self._canonical = _load_jsonl(self.root / "canonical.jsonl")
        if len(self._rows) != 642 or len(self._canonical) != 330:
            raise ValueError("bundled connector catalog count mismatch")
        verdicts_by_canonical: dict[str, dict[str, int]] = {}
        for row in self._rows:
            key = str(row["canonical_key"])
            verdict = str(row["final_verdict"])
            counts = verdicts_by_canonical.setdefault(key, {})
            counts[verdict] = counts.get(verdict, 0) + 1
        for row in self._canonical:
            row["verdicts"] = verdicts_by_canonical[str(row["canonical_key"])]
        self._by_key = {str(row["row_key"]): row for row in self._rows}

    @classmethod
    def bundled(cls) -> "ConnectorCatalog":
        return cls(_DATA)

    def list_rows(self) -> list[dict[str, Any]]:
        return self._rows.copy()

    def list_canonical(self) -> list[dict[str, Any]]:
        return self._canonical.copy()

    def get(self, row_key: str) -> dict[str, Any]:
        row = self._by_key.get(str(row_key))
        if row is None:
            raise KeyError("connector catalog row not found")
        return row.copy()

    def icon_path(self, row_key: str) -> Path:
        row = self._by_key.get(str(row_key))
        if row is None:
            raise KeyError("connector catalog row not found")
        relative = Path(str((row.get("icon") or {}).get("path") or ""))
        path = (self.root / relative).resolve()
        icons = (self.root / "icons").resolve()
        if path.parent != icons or path.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp", ".svg"}:
            raise KeyError("connector icon unavailable")
        if hashlib.sha256(path.read_bytes()).hexdigest() != str((row.get("icon") or {}).get("sha256") or ""):
            raise KeyError("connector icon integrity mismatch")
        return path


def _transport(value: str) -> str:
    normalized = re.sub(r"[-_ ]", "", str(value or "").casefold())
    if normalized in {"http", "streamablehttp"}:
        return "streamable_http"
    if normalized == "sse":
        return "sse"
    raise ValueError("only Streamable HTTP and SSE transports are allowed")


def _headers(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError("headers must be an object with at most 32 fields")
    result: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        name, secret = str(raw_name).strip(), str(raw_value)
        if not _HEADER.fullmatch(name) or name.casefold() in _FORBIDDEN_HEADERS:
            raise ValueError("unsafe connector header name")
        if not secret or len(secret) > 8192 or "\r" in secret or "\n" in secret:
            raise ValueError("unsafe connector header value")
        result[name] = secret
    return result


def parse_mcp_servers(raw: str | dict[str, Any]) -> dict[str, dict[str, Any]]:
    if isinstance(raw, str):
        if not raw.strip() or len(raw.encode()) > 256 * 1024:
            raise ValueError("connector config is empty or too large")
        try:
            loaded = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ValueError("connector config must be valid JSON or YAML") from exc
    else:
        loaded = raw
    if not isinstance(loaded, dict):
        raise ValueError("connector config must be an object")
    servers = loaded.get("mcpServers", loaded.get("mcp_servers", loaded))
    if not isinstance(servers, dict) or not servers or len(servers) > 64:
        raise ValueError("mcpServers must contain 1 to 64 connectors")
    return servers


class CustomConnectorStore:
    def __init__(
        self,
        db_path: Path | str,
        *,
        encryption_key: str | bytes | None = None,
        resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._resolver = resolver
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(multitenancy_custom_connector_definitions)")}
        if "credential_secret_kind" not in columns:
            self._conn.execute(
                "ALTER TABLE multitenancy_custom_connector_definitions "
                "ADD COLUMN credential_secret_kind TEXT NOT NULL DEFAULT 'headers'"
            )
        if "runtime_manifest_json" not in columns:
            self._conn.execute(
                "ALTER TABLE multitenancy_custom_connector_definitions "
                "ADD COLUMN runtime_manifest_json TEXT NOT NULL DEFAULT '{}'"
            )
        self._conn.commit()
        self._db_path.chmod(0o600)
        self._credentials = CredentialStore(self._db_path, encryption_key=encryption_key)

    def close(self) -> None:
        self._credentials.close()
        self._conn.close()

    def import_config(self, profile_name: str, subject_id: str, raw: str | dict[str, Any]) -> list[dict[str, Any]]:
        profile_name = _clean_id("profile_name", profile_name)
        subject_id = _clean_id("subject_id", subject_id)
        prepared: list[tuple[str, str, str, dict[str, str]]] = []
        for raw_name, raw_spec in parse_mcp_servers(raw).items():
            name = _clean_id("connector name", str(raw_name))
            if not isinstance(raw_spec, dict):
                raise ValueError("each MCP connector must be an object")
            if "command" in raw_spec or "args" in raw_spec:
                raise ValueError("arbitrary command connectors are not allowed")
            endpoint = validate_remote_endpoint(str(raw_spec.get("url") or ""), resolver=self._resolver).url
            prepared.append((name, _transport(str(raw_spec.get("type") or "http")), endpoint, _headers(raw_spec.get("headers"))))

        rows = []
        for name, transport, endpoint, headers in prepared:
            connector_id = "custom-" + hashlib.sha256(f"{profile_name}\0{subject_id}\0{name}".encode()).hexdigest()[:24]
            provider = f"connector-{connector_id}"
            if headers:
                self._credentials.put_credential(
                    profile_name=profile_name,
                    subject_id=subject_id,
                    provider=provider,
                    secret_kind="headers",
                    payload={"owner_profile": profile_name, "owner_subject": subject_id, "fields": headers},
                )
            try:
                self._conn.execute(
                    """INSERT INTO multitenancy_custom_connector_definitions
                       (profile_name, subject_id, connector_id, name, transport, endpoint,
                        credential_provider, credential_secret_kind, credential_fields_json, state, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'headers', ?, 'active', ?)
                       ON CONFLICT(profile_name, subject_id, connector_id) DO UPDATE SET
                         name=excluded.name, transport=excluded.transport, endpoint=excluded.endpoint,
                         credential_provider=excluded.credential_provider,
                         credential_fields_json=excluded.credential_fields_json,
                         state='active', updated_at=excluded.updated_at""",
                    (profile_name, subject_id, connector_id, name, transport, endpoint, provider,
                     json.dumps(sorted(headers)), int(time.time() * 1000)),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                if headers:
                    self._credentials.delete_credential(
                        profile_name=profile_name, subject_id=subject_id, provider=provider, secret_kind="headers"
                    )
                raise
            rows.append(self.get(profile_name, subject_id, connector_id))
        return rows

    def install_catalog(
        self,
        profile_name: str,
        subject_id: str,
        *,
        name: str,
        transport: str,
        endpoint: str,
        credential_schema: dict[str, Any],
        fields: dict[str, str],
    ) -> dict[str, Any]:
        """Install one catalog remote using its exact owner-bound credential schema."""
        from .connector_credential_schema import store_connector_credential

        profile_name = _clean_id("profile_name", profile_name)
        subject_id = _clean_id("subject_id", subject_id)
        name = _clean_id("connector name", name)
        transport = _transport(transport)
        provider = str(credential_schema.get("provider") or "")
        secret_kind = str(credential_schema.get("secret_kind") or "")
        if not re.fullmatch(r"connector:[a-f0-9]{16}", provider):
            raise ValueError("invalid credential provider")
        if secret_kind not in {"token", "config"}:
            raise ValueError("invalid credential secret kind")
        expected = sorted(str(field) for field in credential_schema.get("fields") or [])
        values = {str(key): str(value) for key, value in fields.items()}
        if sorted(values) != expected:
            raise ValueError("credential fields do not match schema")
        if any(not value or len(value) > 8192 or "\r" in value or "\n" in value for value in values.values()):
            raise ValueError("unsafe connector credential value")
        targets = credential_schema.get("field_targets") or {
            field: {"kind": "header", "name": field} for field in expected
        }
        if set(targets) != set(expected):
            raise ValueError("credential field targets do not match schema")
        endpoint_fields = [
            field for field, target in targets.items() if target.get("kind") == "endpoint_base"
        ]
        path_fields = [
            field for field, target in targets.items() if target.get("kind") == "path_segment"
        ]
        if len(endpoint_fields) > 1:
            raise ValueError("connector endpoint template is ambiguous")
        if endpoint_fields:
            target = targets[endpoint_fields[0]]
            base = values[endpoint_fields[0]].rstrip("/")
            parsed = urllib.parse.urlsplit(base)
            if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
                raise ValueError("connector endpoint base must be public HTTPS")
            endpoint = base + str(target.get("path") or "")
        endpoint = validate_remote_endpoint(endpoint, resolver=self._resolver).url
        headers = {
            str(target["name"]): str(target.get("prefix") or "") + values[field]
            for field, target in targets.items()
            if target.get("kind") == "header"
        }
        _headers(headers)
        query_names = [
            str(target["name"]) for target in targets.values() if target.get("kind") == "query"
        ]
        if any(not re.fullmatch(r"[A-Za-z0-9._~-]{1,128}", name) for name in query_names):
            raise ValueError("unsafe connector query field name")
        if len(headers) + len(query_names) + len(endpoint_fields) + len(path_fields) != len(expected):
            raise ValueError("unsupported credential field target")
        schema = {**credential_schema, "provider": provider, "secret_kind": secret_kind}
        connector_id = "custom-" + hashlib.sha256(f"{profile_name}\0{subject_id}\0{name}".encode()).hexdigest()[:24]
        store_connector_credential(
            self._credentials,
            schema,
            profile_name=profile_name,
            subject_id=subject_id,
            fields=values,
        )
        try:
            self._conn.execute(
                """INSERT INTO multitenancy_custom_connector_definitions
                   (profile_name, subject_id, connector_id, name, transport, endpoint,
                    credential_provider, credential_secret_kind, credential_fields_json,
                    runtime_manifest_json, state, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                   ON CONFLICT(profile_name, subject_id, connector_id) DO UPDATE SET
                     transport=excluded.transport, endpoint=excluded.endpoint,
                     credential_provider=excluded.credential_provider,
                     credential_secret_kind=excluded.credential_secret_kind,
                     credential_fields_json=excluded.credential_fields_json,
                     runtime_manifest_json=excluded.runtime_manifest_json,
                     state='active', updated_at=excluded.updated_at""",
                (
                    profile_name, subject_id, connector_id, name, transport, endpoint,
                    provider, secret_kind, json.dumps(expected),
                    json.dumps({"field_targets": targets}, sort_keys=True, separators=(",", ":")),
                    int(time.time() * 1000),
                ),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            self._credentials.delete_credential(
                profile_name=profile_name,
                subject_id=subject_id,
                provider=provider,
                secret_kind=secret_kind,
            )
            raise
        return self.get(profile_name, subject_id, connector_id)

    def install_catalog_oauth(
        self,
        profile_name: str,
        subject_id: str,
        *,
        name: str,
        transport: str,
        endpoint: str,
        credential_schema: dict[str, Any],
    ) -> dict[str, Any]:
        profile_name = _clean_id("profile_name", profile_name)
        subject_id = _clean_id("subject_id", subject_id)
        name = _clean_id("connector name", name)
        endpoint = validate_remote_endpoint(endpoint, resolver=self._resolver).url
        transport = _transport(transport)
        provider = str(credential_schema.get("provider") or "")
        if not re.fullmatch(r"connector:[a-f0-9]{16}", provider) or credential_schema.get("secret_kind") != "oauth":
            raise ValueError("invalid OAuth credential schema")
        status = self._credentials.get_status(
            profile_name=profile_name,
            subject_id=subject_id,
            provider=provider,
            secret_kind="oauth",
        )
        if status["status"] != "valid":
            raise PermissionError("connector OAuth credential is unavailable")
        connector_id = "custom-" + hashlib.sha256(f"{profile_name}\0{subject_id}\0{name}".encode()).hexdigest()[:24]
        self._conn.execute(
            """INSERT INTO multitenancy_custom_connector_definitions
               (profile_name, subject_id, connector_id, name, transport, endpoint,
                credential_provider, credential_secret_kind, credential_fields_json, state, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'oauth', '[]', 'active', ?)
               ON CONFLICT(profile_name, subject_id, connector_id) DO UPDATE SET
                 transport=excluded.transport, endpoint=excluded.endpoint,
                 credential_provider=excluded.credential_provider,
                 credential_secret_kind='oauth', credential_fields_json='[]',
                 state='active', updated_at=excluded.updated_at""",
            (profile_name, subject_id, connector_id, name, transport, endpoint, provider, int(time.time() * 1000)),
        )
        self._conn.commit()
        return self.get(profile_name, subject_id, connector_id)

    def install_catalog_stdio(
        self,
        profile_name: str,
        subject_id: str,
        *,
        name: str,
        row_key: str,
        runtime_manifest: dict[str, Any],
        fields: dict[str, str],
    ) -> dict[str, Any]:
        """Install one immutable catalog npm MCP without accepting arbitrary commands."""
        from .connector_credential_schema import store_connector_credential
        from .connector_stdio_runtime import catalog_stdio_spec

        profile_name = _clean_id("profile_name", profile_name)
        subject_id = _clean_id("subject_id", subject_id)
        name = _clean_id("connector name", name)
        if str(runtime_manifest.get("row_key") or "") != row_key:
            raise ValueError("catalog stdio manifest identity mismatch")
        spec = catalog_stdio_spec(runtime_manifest)
        provider = f"connector:{hashlib.sha256(row_key.encode()).hexdigest()[:16]}"
        schema = {"provider": provider, "secret_kind": "config", "fields": spec["fields"]}
        if fields:
            store_connector_credential(
                self._credentials,
                schema,
                profile_name=profile_name,
                subject_id=subject_id,
                fields={str(key): str(value) for key, value in fields.items()},
            )
        elif spec["fields"]:
            raise ValueError("credential fields do not match schema")
        connector_id = "custom-" + hashlib.sha256(
            f"{profile_name}\0{subject_id}\0{name}".encode()
        ).hexdigest()[:24]
        try:
            self._conn.execute(
                """INSERT INTO multitenancy_custom_connector_definitions
                   (profile_name, subject_id, connector_id, name, transport, endpoint,
                    credential_provider, credential_secret_kind, credential_fields_json,
                    runtime_manifest_json, state, updated_at)
                   VALUES (?, ?, ?, ?, 'stdio', '', ?, 'config', ?, ?, 'active', ?)
                   ON CONFLICT(profile_name, subject_id, connector_id) DO UPDATE SET
                     transport='stdio', endpoint='', credential_provider=excluded.credential_provider,
                     credential_secret_kind='config',
                     credential_fields_json=excluded.credential_fields_json,
                     runtime_manifest_json=excluded.runtime_manifest_json,
                     state='active', updated_at=excluded.updated_at""",
                (
                    profile_name, subject_id, connector_id, name, provider,
                    json.dumps(sorted(fields)),
                    json.dumps(runtime_manifest, sort_keys=True, separators=(",", ":")),
                    int(time.time() * 1000),
                ),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            if fields:
                self._credentials.delete_credential(
                    profile_name=profile_name,
                    subject_id=subject_id,
                    provider=provider,
                    secret_kind="config",
                )
            raise
        return self.get(profile_name, subject_id, connector_id)

    def install_catalog_cli(
        self,
        profile_name: str,
        subject_id: str,
        *,
        name: str,
        row_key: str,
        runtime_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Install one admitted CLI lifecycle into its owner's isolated home."""
        from .connector_cli_runtime import catalog_cli_spec

        profile_name = _clean_id("profile_name", profile_name)
        subject_id = _clean_id("subject_id", subject_id)
        name = _clean_id("connector name", name)
        if row_key not in {str(runtime_manifest.get("row_key") or ""), "workbuddy:textin-xparse-ai"}:
            raise ValueError("catalog CLI manifest identity mismatch")
        catalog_cli_spec(runtime_manifest)
        connector_id = "custom-" + hashlib.sha256(
            f"{profile_name}\0{subject_id}\0{name}".encode()
        ).hexdigest()[:24]
        self._conn.execute(
            """INSERT INTO multitenancy_custom_connector_definitions
               (profile_name, subject_id, connector_id, name, transport, endpoint,
                credential_provider, credential_secret_kind, credential_fields_json,
                runtime_manifest_json, state, updated_at)
               VALUES (?, ?, ?, ?, 'cli', '', '', 'headers', '[]', ?, 'active', ?)
               ON CONFLICT(profile_name, subject_id, connector_id) DO UPDATE SET
                 transport='cli', endpoint='', credential_provider='',
                 credential_secret_kind='headers', credential_fields_json='[]',
                 runtime_manifest_json=excluded.runtime_manifest_json,
                 state='active', updated_at=excluded.updated_at""",
            (
                profile_name, subject_id, connector_id, name,
                json.dumps(runtime_manifest, sort_keys=True, separators=(",", ":")),
                int(time.time() * 1000),
            ),
        )
        self._conn.commit()
        return self.get(profile_name, subject_id, connector_id)

    def list_installations(self, profile_name: str, subject_id: str) -> list[dict[str, Any]]:
        keys = (_clean_id("profile_name", profile_name), _clean_id("subject_id", subject_id))
        rows = self._conn.execute(
            """SELECT * FROM multitenancy_custom_connector_definitions
               WHERE profile_name=? AND subject_id=? ORDER BY name, connector_id""",
            keys,
        ).fetchall()
        return [self._public(row) for row in rows]

    def get(self, profile_name: str, subject_id: str, connector_id: str) -> dict[str, Any]:
        row = self._row(profile_name, subject_id, connector_id)
        return self._public(row)

    def get_runtime(self, profile_name: str, subject_id: str, connector_id: str) -> dict[str, Any]:
        row = self._row(profile_name, subject_id, connector_id)
        fields = json.loads(row["credential_fields_json"])
        headers: dict[str, str] = {}
        environment: dict[str, str] = {}
        values: dict[str, str] = {}
        endpoint = str(row["endpoint"])
        oauth_expires_at: int | None = None
        if fields or row["credential_secret_kind"] == "oauth":
            payload = self._credentials.get_secret_for_runtime(
                profile_name=profile_name,
                subject_id=subject_id,
                provider=row["credential_provider"],
                secret_kind=row["credential_secret_kind"],
            )
            if payload.get("owner_profile") != profile_name or payload.get("owner_subject") != subject_id:
                raise PermissionError("connector credential binding mismatch")
            if row["credential_secret_kind"] == "oauth":
                status = self._credentials.get_status(
                    profile_name=profile_name,
                    subject_id=subject_id,
                    provider=row["credential_provider"],
                    secret_kind="oauth",
                )
                oauth_expires_at = status.get("expires_at")
                access_token = str(((payload.get("tokens") or {}).get("access_token") or ""))
                if not access_token:
                    raise PermissionError("connector OAuth credential is unavailable")
                headers = {"Authorization": f"Bearer {access_token}"}
            else:
                values = payload.get("fields") or {}
                if row["transport"] == "stdio":
                    environment = values
                else:
                    metadata = json.loads(row["runtime_manifest_json"] or "{}")
                    targets = metadata.get("field_targets") or {
                        field: {"kind": "header", "name": field} for field in fields
                    }
                    query: list[tuple[str, str]] = []
                    for field, value in values.items():
                        target = targets.get(field) or {}
                        if target.get("kind") == "query":
                            query.append((str(target["name"]), str(value)))
                        elif target.get("kind") == "header":
                            headers[str(target["name"])] = str(target.get("prefix") or "") + str(value)
                        elif target.get("kind") == "endpoint_base":
                            pass
                        elif target.get("kind") == "path_segment":
                            endpoint = endpoint.rstrip("/") + "/" + urllib.parse.quote(str(value), safe="")
                        else:
                            raise PermissionError("connector credential target mismatch")
                    if query:
                        parsed = urllib.parse.urlsplit(endpoint)
                        endpoint = urllib.parse.urlunsplit((
                            parsed.scheme, parsed.netloc, parsed.path,
                            urllib.parse.urlencode(urllib.parse.parse_qsl(parsed.query) + query), "",
                        ))
            resolved_fields = environment if row["transport"] == "stdio" else values
            if row["credential_secret_kind"] != "oauth" and sorted(resolved_fields) != fields:
                raise PermissionError("connector credential schema mismatch")
        runtime = {
            **self._public(row),
            "endpoint": endpoint,
            "headers": headers,
            "credential_provider": row["credential_provider"],
            "credential_secret_kind": row["credential_secret_kind"],
            "oauth_expires_at": oauth_expires_at,
        }
        if row["transport"] in {"stdio", "cli"}:
            manifest = json.loads(row["runtime_manifest_json"])
            if not manifest:
                raise PermissionError("connector runtime manifest is unavailable")
            runtime.update({"environment": environment, "runtime_manifest": manifest})
        return runtime

    def delete(self, profile_name: str, subject_id: str, connector_id: str) -> bool:
        row = self._row(profile_name, subject_id, connector_id)
        self._conn.execute(
            "DELETE FROM multitenancy_custom_connector_definitions WHERE profile_name=? AND subject_id=? AND connector_id=?",
            (profile_name, subject_id, connector_id),
        )
        self._conn.commit()
        if json.loads(row["credential_fields_json"]) or row["credential_secret_kind"] != "headers":
            self._credentials.delete_credential(
                profile_name=profile_name,
                subject_id=subject_id,
                provider=row["credential_provider"],
                secret_kind=row["credential_secret_kind"],
            )
        return True

    def set_state(self, profile_name: str, subject_id: str, connector_id: str, state: str) -> dict[str, Any]:
        if state not in {"active", "ready"}:
            raise ValueError("invalid connector state")
        self._row(profile_name, subject_id, connector_id)
        self._conn.execute(
            """UPDATE multitenancy_custom_connector_definitions SET state=?, updated_at=?
               WHERE profile_name=? AND subject_id=? AND connector_id=?""",
            (state, int(time.time() * 1000), profile_name, subject_id, connector_id),
        )
        self._conn.commit()
        return self.get(profile_name, subject_id, connector_id)

    def _row(self, profile_name: str, subject_id: str, connector_id: str) -> sqlite3.Row:
        keys = (_clean_id("profile_name", profile_name), _clean_id("subject_id", subject_id), _clean_id("connector_id", connector_id))
        row = self._conn.execute(
            """SELECT * FROM multitenancy_custom_connector_definitions
               WHERE profile_name=? AND subject_id=? AND connector_id=?""",
            keys,
        ).fetchone()
        if row is None:
            raise PermissionError("connector installation unavailable for current owner")
        return row

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "connector_id": row["connector_id"],
            "name": row["name"],
            "transport": row["transport"],
            "endpoint": row["endpoint"],
            "credential_fields": json.loads(row["credential_fields_json"]),
            "state": row["state"],
            "updated_at": row["updated_at"],
        }
