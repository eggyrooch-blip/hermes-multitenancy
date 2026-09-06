"""Immutable connector releases and exact-owner installation pins."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS multitenancy_connector_releases (
    connector_id TEXT NOT NULL,
    version TEXT NOT NULL,
    digest TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (connector_id, version)
);
CREATE TABLE IF NOT EXISTS multitenancy_connector_installations (
    profile_name TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    connector_id TEXT NOT NULL,
    credential_provider TEXT NOT NULL,
    credential_kind TEXT NOT NULL,
    current_version TEXT NOT NULL,
    previous_version TEXT,
    staged_version TEXT,
    state TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (profile_name, subject_id, connector_id)
);
"""


def _clean(label: str, value: str) -> str:
    text = str(value or "").strip()
    if not _ID.fullmatch(text):
        raise ValueError(f"invalid {label}")
    return text


def _manifest_json(manifest: dict[str, Any]) -> str:
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if not str(manifest.get("compatibility") or "").strip():
        raise ValueError("release compatibility is required")
    for field in ("required_scopes", "capabilities"):
        values = manifest.get(field)
        if not isinstance(values, list) or any(not isinstance(item, str) or not item for item in values):
            raise ValueError(f"manifest {field} must be a string list")
    components = manifest.get("components")
    if not isinstance(components, list) or not components:
        raise ValueError("release components are required")
    for component in components:
        if not isinstance(component, dict) or component.get("kind") not in {"remote_mcp", "skill", "cli", "connector"}:
            raise ValueError("unsupported release component")
        _clean("component id", component.get("id"))
        if component["kind"] == "remote_mcp":
            parsed = urlsplit(str(component.get("endpoint") or ""))
            loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or (parsed.scheme == "http" and not loopback)
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("remote MCP endpoint is unsafe")
            continue
        if not _VERSION.fullmatch(str(component.get("version") or "")) or not _DIGEST.fullmatch(str(component.get("digest") or "")):
            raise ValueError("local release components must be pinned by version and digest")
        if any(key in component for key in ("command", "args", "env", "headers")):
            raise ValueError("arbitrary commands are not allowed in release manifests")


class ConnectorReleaseStore:
    def __init__(self, db_path: Path | str) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        path.chmod(0o600)

    def close(self) -> None:
        self._conn.close()

    def publish(self, connector_id: str, version: str, digest: str, manifest: dict[str, Any]) -> None:
        connector_id = _clean("connector_id", connector_id)
        if not _VERSION.fullmatch(str(version or "")):
            raise ValueError("invalid release version")
        raw = _manifest_json(manifest)
        _validate_manifest(manifest)
        expected = hashlib.sha256(raw.encode()).hexdigest()
        if not _DIGEST.fullmatch(str(digest or "")) or digest != expected:
            raise ValueError("release digest does not match canonical manifest")
        existing = self._conn.execute(
            "SELECT digest, manifest_json FROM multitenancy_connector_releases WHERE connector_id=? AND version=?",
            (connector_id, version),
        ).fetchone()
        if existing:
            if existing["digest"] != digest or existing["manifest_json"] != raw:
                raise ValueError("connector releases are immutable")
            return
        self._conn.execute(
            "INSERT INTO multitenancy_connector_releases VALUES (?, ?, ?, ?, ?)",
            (connector_id, version, digest, raw, int(time.time() * 1000)),
        )
        self._conn.commit()

    def install(
        self,
        *,
        profile_name: str,
        subject_id: str,
        connector_id: str,
        version: str,
        credential_provider: str,
        credential_kind: str,
    ) -> dict[str, Any]:
        values = tuple(_clean(label, value) for label, value in (
            ("profile_name", profile_name),
            ("subject_id", subject_id),
            ("connector_id", connector_id),
            ("credential_provider", credential_provider),
            ("credential_kind", credential_kind),
        ))
        release = self._release(values[2], version)
        state = "active" if self._credential_has_scopes(values[0], values[1], values[3], values[4], release) else "needs_auth"
        self._conn.execute(
            """
            INSERT INTO multitenancy_connector_installations
                (profile_name, subject_id, connector_id, credential_provider, credential_kind,
                 current_version, previous_version, staged_version, state, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            ON CONFLICT(profile_name, subject_id, connector_id) DO UPDATE SET
                credential_provider=excluded.credential_provider,
                credential_kind=excluded.credential_kind,
                current_version=excluded.current_version,
                previous_version=NULL,
                staged_version=NULL,
                state=excluded.state,
                updated_at=excluded.updated_at
            """,
            (*values, version, state, int(time.time() * 1000)),
        )
        self._conn.commit()
        return self.get(values[0], values[1], values[2])

    def stage(self, profile_name: str, subject_id: str, connector_id: str, version: str) -> dict[str, Any]:
        profile_name, subject_id, connector_id = (
            _clean("profile_name", profile_name),
            _clean("subject_id", subject_id),
            _clean("connector_id", connector_id),
        )
        self._release(connector_id, version)
        cursor = self._conn.execute(
            """UPDATE multitenancy_connector_installations SET staged_version=?, updated_at=?
               WHERE profile_name=? AND subject_id=? AND connector_id=?""",
            (version, int(time.time() * 1000), profile_name, subject_id, connector_id),
        )
        if cursor.rowcount != 1:
            raise KeyError("connector installation not found")
        self._conn.commit()
        return self.get(profile_name, subject_id, connector_id)

    def promote(self, profile_name: str, subject_id: str, connector_id: str, *, canary_ok: bool) -> dict[str, Any]:
        if not canary_ok:
            raise PermissionError("connector canary did not pass")
        row = self._installation(profile_name, subject_id, connector_id)
        if not row["staged_version"]:
            raise ValueError("no staged connector release")
        release = self._release(row["connector_id"], row["staged_version"])
        if not self._credential_has_scopes(
            row["profile_name"], row["subject_id"], row["credential_provider"], row["credential_kind"], release
        ):
            self._conn.execute(
                """UPDATE multitenancy_connector_installations SET state='needs_auth', updated_at=?
                   WHERE profile_name=? AND subject_id=? AND connector_id=?""",
                (int(time.time() * 1000), row["profile_name"], row["subject_id"], row["connector_id"]),
            )
        else:
            self._conn.execute(
                """UPDATE multitenancy_connector_installations
                   SET previous_version=current_version, current_version=staged_version,
                       staged_version=NULL, state='active', updated_at=?
                   WHERE profile_name=? AND subject_id=? AND connector_id=?""",
                (int(time.time() * 1000), row["profile_name"], row["subject_id"], row["connector_id"]),
            )
        self._conn.commit()
        return self.get(row["profile_name"], row["subject_id"], row["connector_id"])

    def rollback(self, profile_name: str, subject_id: str, connector_id: str) -> dict[str, Any]:
        row = self._installation(profile_name, subject_id, connector_id)
        if not row["previous_version"]:
            raise ValueError("no connector release to roll back")
        release = self._release(row["connector_id"], row["previous_version"])
        state = "active" if self._credential_has_scopes(
            row["profile_name"], row["subject_id"], row["credential_provider"], row["credential_kind"], release
        ) else "needs_auth"
        self._conn.execute(
            """UPDATE multitenancy_connector_installations
               SET current_version=previous_version, previous_version=current_version,
                   staged_version=NULL, state=?, updated_at=?
               WHERE profile_name=? AND subject_id=? AND connector_id=?""",
            (state, int(time.time() * 1000), row["profile_name"], row["subject_id"], row["connector_id"]),
        )
        self._conn.commit()
        return self.get(row["profile_name"], row["subject_id"], row["connector_id"])

    def get(self, profile_name: str, subject_id: str, connector_id: str) -> dict[str, Any]:
        return dict(self._installation(profile_name, subject_id, connector_id))

    def _installation(self, profile_name: str, subject_id: str, connector_id: str) -> sqlite3.Row:
        keys = (
            _clean("profile_name", profile_name),
            _clean("subject_id", subject_id),
            _clean("connector_id", connector_id),
        )
        row = self._conn.execute(
            """SELECT profile_name, subject_id, connector_id, credential_provider, credential_kind,
                      current_version, previous_version, staged_version, state, updated_at
               FROM multitenancy_connector_installations
               WHERE profile_name=? AND subject_id=? AND connector_id=?""",
            keys,
        ).fetchone()
        if row is None:
            raise KeyError("connector installation not found")
        return row

    def _release(self, connector_id: str, version: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT manifest_json FROM multitenancy_connector_releases WHERE connector_id=? AND version=?",
            (_clean("connector_id", connector_id), str(version)),
        ).fetchone()
        if row is None:
            raise KeyError("connector release not found")
        return json.loads(row["manifest_json"])

    def _credential_has_scopes(
        self,
        profile_name: str,
        subject_id: str,
        provider: str,
        kind: str,
        release: dict[str, Any],
    ) -> bool:
        row = self._conn.execute(
            """SELECT scopes_json, expires_at FROM multitenancy_credentials
               WHERE profile_name=? AND subject_id=? AND provider=? AND secret_kind=? AND active=1""",
            (profile_name, subject_id, provider, kind),
        ).fetchone()
        if row is None or (row["expires_at"] is not None and int(row["expires_at"]) <= int(time.time() * 1000)):
            return False
        granted = set(json.loads(row["scopes_json"]))
        return set(release["required_scopes"]).issubset(granted)
