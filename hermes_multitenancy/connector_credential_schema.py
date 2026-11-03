"""Catalog credential schemas backed by the existing owner-scoped vault."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .connector_catalog_conformance import read_catalog
from .credentials import CredentialStore


_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NO_AUTH = {"", "none", "none_or_embedded", "macos privacy permissions"}


def _auth_flow(mode: str) -> tuple[str, str]:
    value = mode.casefold()
    if "oauth" in value or value in {"server-side", "mcp", "vendor_account_scoped_cloud_broker"}:
        return "mcp_oauth", "oauth"
    if "token" in value:
        return "manual_token", "token"
    return "manual_fields", "config"


def credential_schemas(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
    remote_results: str | Path | None = None,
) -> list[dict[str, Any]]:
    discovered_auth: set[str] = set()
    if remote_results:
        for line in Path(remote_results).read_text(encoding="utf-8").splitlines():
            result = json.loads(line)
            if result.get("verdict") == "needs_auth":
                discovered_auth.add(str(result.get("row_key") or ""))

    schemas: list[dict[str, Any]] = []
    for row in read_catalog(path, expected_sha256=expected_sha256):
        row_key = f"{str(row['product']).strip().casefold()}:{str(row['catalog_id']).strip().casefold()}"
        mode = str(row.get("auth_mode") or "").strip()
        if mode.casefold() in _NO_AUTH and row_key not in discovered_auth:
            continue
        if not mode:
            mode = "mcp_oauth"
        flow, secret_kind = _auth_flow(mode)
        raw_fields = [str(value).strip() for value in (row.get("credential_key_names") or [])]
        fields = sorted({value for value in raw_fields if _FIELD.fullmatch(value)})
        if flow == "manual_token" and not fields:
            fields = ["TOKEN"]
        provider = f"connector:{hashlib.sha256(row_key.encode()).hexdigest()[:16]}"
        schemas.append(
            {
                "row_key": row_key,
                "provider": provider,
                "secret_kind": secret_kind,
                "auth_flow": flow,
                "fields": fields,
                "invalid_field_count": len(raw_fields) - len(fields),
                "storage": "multitenancy_credentials",
                "binding": ["profile_name", "subject_id", "provider", "secret_kind"],
                "status_without_credential": "needs_auth",
            }
        )
    return schemas


def store_connector_credential(
    store: CredentialStore,
    schema: dict[str, Any],
    *,
    profile_name: str,
    subject_id: str,
    fields: dict[str, str],
    expires_at: int | None = None,
) -> None:
    expected = set(schema.get("fields") or [])
    if set(fields) != expected or any(not str(value) for value in fields.values()):
        raise ValueError("credential fields do not match schema")
    store.put_credential(
        profile_name=profile_name,
        subject_id=subject_id,
        provider=str(schema["provider"]),
        secret_kind=str(schema["secret_kind"]),
        payload={
            "owner_profile": profile_name,
            "owner_subject": subject_id,
            "fields": dict(fields),
        },
        expires_at=expires_at,
    )


def resolve_connector_credential(
    store: CredentialStore,
    schema: dict[str, Any],
    *,
    profile_name: str,
    subject_id: str,
) -> dict[str, str]:
    provider = str(schema["provider"])
    secret_kind = str(schema["secret_kind"])
    status = store.get_status(
        profile_name=profile_name,
        subject_id=subject_id,
        provider=provider,
        secret_kind=secret_kind,
    )
    if status["status"] != "valid":
        raise PermissionError("connector credential is unavailable")
    payload = store.get_secret_for_runtime(
        profile_name=profile_name,
        subject_id=subject_id,
        provider=provider,
        secret_kind=secret_kind,
    )
    if payload.get("owner_profile") != profile_name or payload.get("owner_subject") != subject_id:
        raise PermissionError("connector credential binding mismatch")
    fields = payload.get("fields")
    if not isinstance(fields, dict) or set(fields) != set(schema.get("fields") or []):
        raise PermissionError("connector credential schema mismatch")
    return {str(key): str(value) for key, value in fields.items()}


def revoke_connector_credential(
    store: CredentialStore,
    schema: dict[str, Any],
    *,
    profile_name: str,
    subject_id: str,
) -> bool:
    return store.delete_credential(
        profile_name=profile_name,
        subject_id=subject_id,
        provider=str(schema["provider"]),
        secret_kind=str(schema["secret_kind"]),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--expect-sha256")
    parser.add_argument("--remote-results", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    schemas = credential_schemas(
        args.catalog,
        expected_sha256=args.expect_sha256,
        remote_results=args.remote_results,
    )
    args.output.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in schemas),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
