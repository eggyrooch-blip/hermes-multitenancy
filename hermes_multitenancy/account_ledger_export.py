"""Root-only, GET-only producer for signed account-ledger snapshots."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
import tempfile
import time
from typing import Any, Callable
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import account_ledger
from .billing_employee_key import _NO_REDIRECT_OPENER
from .billing_identity import _is_canonical_employee_id


_SIGNING_KEY_PATH = Path("/etc/hermes/account-ledger-signing-private.pem")
_URL_OPEN: Callable[..., Any] = _NO_REDIRECT_OPENER.open
_LAYERS = ("employees", "routes", "accounts", "keys")
_SOURCE_FILES = ("org-before.json", "org-after.json", "database-before.db", "database-after.db")
_MAX_SOURCE_BYTES = 20 * 1024 * 1024
_MAX_DATABASE_BYTES = 4 * 1024 * 1024 * 1024
_MAX_HTTP_BYTES = 20 * 1024 * 1024
_MAX_CREDENTIAL_BYTES = 16 * 1024
_UNBOUND_PROFILE = "__unbound__"
_LITELLM_HOST = "litellm.sre.gotokeep.com"
_LITELLM_ORIGIN = f"https://{_LITELLM_HOST}"


class ExportError(RuntimeError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExportError("duplicate_json_key")
        result[key] = value
    return result


def _read_file(path: Path, *, max_bytes: int, secret: bool = False) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ExportError("input_unavailable") from exc
    try:
        before = os.fstat(fd)
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > max_bytes
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or (secret and (mode != 0o600 or before.st_uid != os.geteuid()))
        ):
            raise ExportError("input_permissions_invalid")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        locator = os.stat(path, follow_symlinks=False)
        if (
            len(raw) > max_bytes
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino) != (locator.st_dev, locator.st_ino)
        ):
            raise ExportError("input_changed")
        return raw
    finally:
        os.close(fd)


def _read_trusted_file(path: Path, *, max_bytes: int, secret: bool = False) -> bytes:
    try:
        return account_ledger._read_pinned(path, max_bytes=max_bytes, private=secret)
    except account_ledger.InvalidLedgerInput as exc:
        raise ExportError("input_permissions_invalid") from exc


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ExportError("invalid_json") from exc


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("metadata")
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        parsed = _json(value.encode())
        if isinstance(parsed, dict):
            return parsed
    return {}


def _subject(value: Any) -> str:
    result = str(value or "").strip()
    if not _is_canonical_employee_id(result):
        raise ExportError("employee_id_invalid")
    return result.casefold()


def _org_rows(path: Path) -> list[dict[str, Any]]:
    value = _json(_read_trusted_file(path, max_bytes=_MAX_SOURCE_BYTES, secret=True))
    employees = value.get("employees") if isinstance(value, dict) else None
    if not isinstance(employees, dict) or not employees:
        raise ExportError("org_snapshot_invalid")
    rows: list[dict[str, Any]] = []
    seen_profiles: set[str] = set()
    seen_open_ids: set[str] = set()
    for key, raw in employees.items():
        if not isinstance(raw, dict):
            raise ExportError("org_snapshot_invalid")
        employee_id = _subject(raw.get("user_id") or key)
        if employee_id != _subject(key):
            raise ExportError("org_snapshot_identity_mismatch")
        profile = str(raw.get("profile_name") or "").strip()
        open_id = str(raw.get("open_id") or "").strip()
        if (
            not profile
            or not open_id.startswith("ou_")
            or profile in seen_profiles
            or open_id in seen_open_ids
        ):
            raise ExportError("org_snapshot_identity_ambiguous")
        seen_profiles.add(profile)
        seen_open_ids.add(open_id)
        rows.append({"employee_id": employee_id, "profile": profile, "open_id": open_id})
    if len(rows) != len({_subject(row["employee_id"]) for row in rows}):
        raise ExportError("org_snapshot_identity_ambiguous")
    return sorted(rows, key=lambda row: row["employee_id"])


def _open_database_locator(path: Path) -> tuple[int, os.stat_result]:
    try:
        locator_fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ExportError("database_invalid") from exc
    before = os.fstat(locator_fd)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size <= 0
        or before.st_size > _MAX_DATABASE_BYTES
        or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        os.close(locator_fd)
        raise ExportError("database_invalid")
    return locator_fd, before


def _fd_root() -> Path:
    if sys.platform.startswith("linux") and Path("/proc/self/fd").is_dir():
        return Path("/proc/self/fd")
    if sys.platform == "darwin" and Path("/dev/fd").is_dir():
        return Path("/dev/fd")
    raise ExportError("database_platform_unsupported")


def _open_fd_numbers() -> set[int]:
    result: set[int] = set()
    for name in os.listdir(_fd_root()):
        if not name.isdigit():
            continue
        number = int(name)
        try:
            os.fstat(number)
        except OSError:
            continue
        result.add(number)
    return result


def _process_holds_inode(metadata: os.stat_result, *, exclude: set[int]) -> bool:
    for name in os.listdir(_fd_root()):
        if not name.isdigit() or int(name) in exclude:
            continue
        try:
            current = os.fstat(int(name))
        except OSError:
            continue
        if (current.st_dev, current.st_ino) == (metadata.st_dev, metadata.st_ino):
            return True
    return False


def _freeze_database(source: Path, destination: Path) -> None:
    locator_fd, before = _open_database_locator(source)
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        uri = "file:" + urllib.parse.quote(str(source.absolute()), safe="/") + "?mode=ro"
        existing_fds = _open_fd_numbers()
        source_connection = sqlite3.connect(uri, uri=True)
        if not _process_holds_inode(before, exclude=existing_fds | {locator_fd}):
            raise ExportError("input_changed")
        source_connection.execute("PRAGMA query_only=ON")
        destination_connection = sqlite3.connect(destination)
        source_connection.backup(destination_connection)
        result = destination_connection.execute("PRAGMA quick_check").fetchone()
        if result is None or result[0] != "ok":
            raise ExportError("database_invalid")
        destination_connection.commit()
        destination_connection.close()
        destination_connection = None
        destination.chmod(0o600)
        frozen_fd = os.open(destination, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(frozen_fd)
        finally:
            os.close(frozen_fd)
        after = os.stat(source, follow_symlinks=False)
        current = os.fstat(locator_fd)
        if (
            (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise ExportError("input_changed")
    except sqlite3.Error as exc:
        raise ExportError("database_invalid") from exc
    finally:
        if source_connection is not None:
            source_connection.close()
        if destination_connection is not None:
            destination_connection.close()
        os.close(locator_fd)


def _database_rows(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    account_ledger._assert_trusted_ancestors(path)
    locator_fd, before = _open_database_locator(path)
    connection: sqlite3.Connection | None = None
    try:
        pinned_path = _fd_root() / str(locator_fd)
        uri = "file:" + urllib.parse.quote(str(pinned_path), safe="/") + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        routes_raw = connection.execute(
            "SELECT user_id, profile_name, open_id, active, kind, provenance "
            "FROM multitenancy_routing WHERE kind = 'user' AND provenance = 'sync' "
            "ORDER BY user_id"
        ).fetchall()
        billing_raw = connection.execute(
            "SELECT employee_user_id, profile_name, email, litellm_user_id, "
            "key_id, expires_at, migration_state "
            "FROM multitenancy_billing_identities ORDER BY employee_user_id"
        ).fetchall()
        connection.commit()
    except sqlite3.Error as exc:
        raise ExportError("database_invalid") from exc
    finally:
        if connection is not None:
            connection.close()
        try:
            after = os.stat(path, follow_symlinks=False)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ExportError("input_changed")
        finally:
            os.close(locator_fd)

    routes: list[dict[str, Any]] = []
    for raw in routes_raw:
        active = raw["active"]
        if active not in (0, 1):
            raise ExportError("route_schema_invalid")
        routes.append({
            "employee_id": _subject(raw["user_id"]),
            "profile": str(raw["profile_name"] or "").strip(),
            "open_id": str(raw["open_id"] or "").strip(),
            "active": active == 1,
            "kind": str(raw["kind"] or ""),
            "provenance": str(raw["provenance"] or ""),
        })
    if any(not row["profile"] or not row["open_id"].startswith("ou_") for row in routes):
        raise ExportError("route_schema_invalid")
    for field in ("employee_id", "profile", "open_id"):
        if len({row[field] for row in routes}) != len(routes):
            raise ExportError("route_identity_ambiguous")

    bindings: list[dict[str, Any]] = []
    for raw in billing_raw:
        email = str(raw["email"] or "").strip().casefold()
        profile = str(raw["profile_name"] or "").strip()
        account_id = str(raw["litellm_user_id"] or "").strip()
        key_id = str(raw["key_id"] or "").strip()
        state = str(raw["migration_state"] or "").strip()
        if not profile or email.count("@") != 1 or state not in {"legacy", "enforced"}:
            raise ExportError("billing_identity_schema_invalid")
        bindings.append({
            "employee_id": _subject(raw["employee_user_id"]),
            "profile": profile,
            "email": email,
            "litellm_user_id": account_id,
            "key_id": key_id,
            "expires_at": int(raw["expires_at"] or 0),
            "migration_state": state,
        })
    if len({_subject(row["employee_id"]) for row in bindings}) != len(bindings):
        raise ExportError("billing_identity_ambiguous")
    for field in ("profile", "email", "litellm_user_id", "key_id"):
        values = [row[field] for row in bindings if row[field]]
        if len(set(values)) != len(values):
            raise ExportError("billing_identity_ambiguous")
    return routes, bindings


def _stable_digest(rows: list[dict[str, Any]]) -> tuple[int, str]:
    canonical_rows = sorted(
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for row in rows
    )
    payload = json.dumps(canonical_rows, separators=(",", ":")).encode()
    return len(rows), hashlib.sha256(payload).hexdigest()


def _validate_base_url(value: str) -> str:
    if any(char.isspace() or ord(char) < 0x20 for char in value):
        raise ExportError("litellm_endpoint_invalid")
    try:
        parsed = urllib.parse.urlparse(value)
        _ = parsed.port
    except ValueError as exc:
        raise ExportError("litellm_endpoint_invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != _LITELLM_HOST
        or (parsed.port or 443) != 443
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ExportError("litellm_endpoint_invalid")
    return _LITELLM_ORIGIN


def _get_page(base_url: str, token: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
    if path not in {"/user/list", "/key/list"}:
        raise ExportError("litellm_path_not_read_only")
    request = urllib.request.Request(
        base_url + path + "?" + urllib.parse.urlencode(params),
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with _URL_OPEN(request, timeout=30.0) as response:
            raw = response.read(_MAX_HTTP_BYTES + 1)
            status = int(getattr(response, "status", 200))
    except Exception as exc:
        raise ExportError("litellm_unavailable") from exc
    if status != 200 or len(raw) > _MAX_HTTP_BYTES:
        raise ExportError("litellm_response_invalid")
    value = _json(raw)
    if not isinstance(value, dict):
        raise ExportError("litellm_response_invalid")
    return value


def _inventory(base_url: str, token: str, path: str, key: str, size_name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_pages: set[str] = set()
    for page in range(1, 1001):
        params: dict[str, Any] = {"page": page, size_name: 100}
        if path == "/key/list":
            params["return_full_object"] = "true"
        value = _get_page(base_url, token, path, params)
        page_rows = value.get(key)
        if not isinstance(page_rows, list) or any(not isinstance(row, dict) for row in page_rows):
            raise ExportError("litellm_response_invalid")
        marker = json.dumps(page_rows, sort_keys=True, separators=(",", ":"), default=str)
        if marker in seen_pages:
            raise ExportError("litellm_pagination_invalid")
        seen_pages.add(marker)
        rows.extend(page_rows)
        total_pages = value.get("total_pages")
        if total_pages is not None:
            if not isinstance(total_pages, int) or total_pages < page:
                raise ExportError("litellm_pagination_invalid")
            if page == total_pages:
                return rows
        elif len(page_rows) < 100:
            return rows
    raise ExportError("litellm_pagination_invalid")


def _blocked(row: dict[str, Any]) -> bool:
    value = row.get("blocked", False)
    disabled = _metadata(row).get("hermes_billing_disabled", False)
    if type(value) is not bool or type(disabled) is not bool:
        raise ExportError("litellm_response_invalid")
    return value or disabled


def _expires_ms(value: Any) -> int:
    if isinstance(value, bool):
        raise ExportError("key_expiry_invalid")
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number > 10_000_000_000 else number * 1000
    text = str(value or "").strip()
    if not text:
        raise ExportError("key_expiry_invalid")
    if text.isdigit():
        return _expires_ms(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExportError("key_expiry_invalid") from exc
    if parsed.tzinfo is None:
        raise ExportError("key_expiry_invalid")
    return int(parsed.timestamp() * 1000)


def _upstream_rows(
    bindings: list[dict[str, Any]], users: list[dict[str, Any]], keys: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    user_by_id: dict[str, dict[str, Any]] = {}
    users_by_email: dict[str, list[dict[str, Any]]] = {}
    for user in users:
        user_id = str(user.get("user_id") or "").strip()
        email = str(user.get("user_email") or user.get("email") or "").strip().casefold()
        if not user_id or user_id in user_by_id:
            raise ExportError("account_identity_ambiguous")
        user_by_id[user_id] = user
        if email:
            users_by_email.setdefault(email, []).append(user)

    key_by_id: dict[str, dict[str, Any]] = {}
    for key in keys:
        key_id = str(key.get("token") or key.get("key") or "").strip()
        if not key_id or key_id in key_by_id:
            raise ExportError("key_identity_ambiguous")
        key_by_id[key_id] = key

    accounts: list[dict[str, Any]] = []
    key_rows: list[dict[str, Any]] = []
    bound_account_ids = {row["litellm_user_id"] for row in bindings if row["litellm_user_id"]}
    bound_key_ids = {row["key_id"] for row in bindings if row["key_id"]}
    binding_by_subject = {row["employee_id"]: row for row in bindings}

    for binding in bindings:
        subject = binding["employee_id"]
        account_id = binding["litellm_user_id"]
        user = user_by_id.get(account_id) if account_id else None
        if user is not None:
            email_matches = users_by_email.get(binding["email"], [])
            if len(email_matches) != 1 or email_matches[0] is not user:
                raise ExportError("account_identity_ambiguous")
            upstream_subject = str(_metadata(user).get("hermes_employee_id") or "").strip()
            if upstream_subject and _subject(upstream_subject) != subject:
                raise ExportError("account_identity_mismatch")
            accounts.append({
                "employee_id": subject,
                "profile": binding["profile"],
                "litellm_user_id": account_id,
                "active": not _blocked(user),
                "migration_state": binding["migration_state"],
            })
        key_id = binding["key_id"]
        key = key_by_id.get(key_id) if key_id else None
        if key is not None:
            key_account = str(key.get("user_id") or "").strip()
            upstream_subject = str(_metadata(key).get("hermes_employee_id") or "").strip()
            if key_account != account_id or (upstream_subject and _subject(upstream_subject) != subject):
                raise ExportError("key_identity_mismatch")
            key_rows.append({
                "employee_id": subject,
                "profile": binding["profile"],
                "litellm_user_id": key_account,
                "key_id": key_id,
                "expires_at": _expires_ms(key.get("expires")),
                "active": not _blocked(key),
            })

    for user_id, user in user_by_id.items():
        upstream_subject = str(_metadata(user).get("hermes_employee_id") or "").strip()
        if not upstream_subject or user_id in bound_account_ids:
            continue
        subject = _subject(upstream_subject)
        if subject in binding_by_subject:
            raise ExportError("account_identity_ambiguous")
        accounts.append({
            "employee_id": subject,
            "profile": _UNBOUND_PROFILE,
            "litellm_user_id": user_id,
            "active": not _blocked(user),
            "migration_state": "unbound",
        })

    for key_id, key in key_by_id.items():
        metadata = _metadata(key)
        upstream_subject = str(metadata.get("hermes_employee_id") or "").strip()
        if metadata.get("purpose") != "hermes_runtime" or not upstream_subject or key_id in bound_key_ids:
            continue
        subject = _subject(upstream_subject)
        if subject in binding_by_subject:
            raise ExportError("key_identity_ambiguous")
        key_rows.append({
            "employee_id": subject,
            "profile": _UNBOUND_PROFILE,
            "litellm_user_id": str(key.get("user_id") or "").strip(),
            "key_id": key_id,
            "expires_at": _expires_ms(key.get("expires")),
            "active": not _blocked(key),
        })
    return accounts, key_rows


def _sign(layer: str, rows: list[dict[str, Any]], private_key: Ed25519PrivateKey, snapshot_id: str, now_ms: int) -> bytes:
    payload = {
        "version": 1,
        "audience": "hermes-1",
        "snapshot_id": snapshot_id,
        "issued_at_ms": now_ms,
        "expires_at_ms": now_ms + 1_800_000,
        "rows": rows,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["signature"] = base64.b64encode(
        private_key.sign(f"hermes-account-ledger:{layer}:v1\n".encode() + canonical)
    ).decode()
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _write_file(directory: Path, name: str, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(directory / name, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _cleanup(directory: Path) -> None:
    for layer in _LAYERS:
        try:
            (directory / f"{layer}.json").unlink()
        except FileNotFoundError:
            pass
    sources = directory / ".sources"
    for name in _SOURCE_FILES:
        try:
            (sources / name).unlink()
        except FileNotFoundError:
            pass
    try:
        sources.rmdir()
    except FileNotFoundError:
        pass
    try:
        directory.rmdir()
    except FileNotFoundError:
        pass


def export_snapshot(
    *, org_path: Path, database: Path, base_url: str, admin_key_path: Path, output_dir: Path
) -> dict[str, Any]:
    if output_dir.exists() or output_dir.is_symlink():
        raise ExportError("output_exists")
    parent = output_dir.parent
    try:
        account_ledger._assert_trusted_ancestors(output_dir)
    except account_ledger.InvalidLedgerInput as exc:
        raise ExportError("output_parent_permissions_invalid") from exc
    parent_stat = parent.stat()
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.geteuid()
        or parent_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ExportError("output_parent_permissions_invalid")
    endpoint = _validate_base_url(base_url)
    private_raw = _read_trusted_file(
        _SIGNING_KEY_PATH, max_bytes=_MAX_CREDENTIAL_BYTES, secret=True
    )
    admin_token = _read_trusted_file(
        admin_key_path, max_bytes=_MAX_CREDENTIAL_BYTES, secret=True
    ).decode().strip()
    if not admin_token:
        raise ExportError("litellm_credential_invalid")
    try:
        private_key = serialization.load_pem_private_key(private_raw, password=None)
    except (TypeError, ValueError) as exc:
        raise ExportError("signer_invalid") from exc
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ExportError("signer_invalid")
    public_raw = _read_trusted_file(
        account_ledger._PUBLIC_KEY_PATH, max_bytes=_MAX_CREDENTIAL_BYTES
    )
    if private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ) != public_raw:
        raise ExportError("trust_root_mismatch")
    _read_trusted_file(account_ledger._FINGERPRINT_KEY_PATH, max_bytes=4096, secret=True)

    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=parent))
    staging.chmod(0o700)
    try:
        sources = staging / ".sources"
        sources.mkdir(mode=0o700)
        org_before = _read_file(org_path, max_bytes=_MAX_SOURCE_BYTES)
        _write_file(sources, "org-before.json", org_before)
        _freeze_database(database, sources / "database-before.db")
        employees = _org_rows(sources / "org-before.json")
        routes, bindings = _database_rows(sources / "database-before.db")

        users = _inventory(endpoint, admin_token, "/user/list", "users", "page_size")
        keys = _inventory(endpoint, admin_token, "/key/list", "keys", "size")
        users_after = _inventory(endpoint, admin_token, "/user/list", "users", "page_size")
        keys_after = _inventory(endpoint, admin_token, "/key/list", "keys", "size")

        org_after = _read_file(org_path, max_bytes=_MAX_SOURCE_BYTES)
        _write_file(sources, "org-after.json", org_after)
        _freeze_database(database, sources / "database-after.db")
        employees_after = _org_rows(sources / "org-after.json")
        routes_after, bindings_after = _database_rows(sources / "database-after.db")
        if (
            hashlib.sha256(org_before).digest() != hashlib.sha256(org_after).digest()
            or _stable_digest(employees) != _stable_digest(employees_after)
            or _stable_digest(routes) != _stable_digest(routes_after)
            or _stable_digest(bindings) != _stable_digest(bindings_after)
            or _stable_digest(users) != _stable_digest(users_after)
            or _stable_digest(keys) != _stable_digest(keys_after)
        ):
            raise ExportError("sources_changed")

        accounts, key_rows = _upstream_rows(bindings, users, keys)
        rows = {
            "employees": employees,
            "routes": routes,
            "accounts": accounts,
            "keys": key_rows,
        }
        snapshot_id = secrets.token_hex(16)
        now_ms = int(time.time() * 1000)
        for name in _SOURCE_FILES:
            (sources / name).unlink()
        sources.rmdir()
        for layer in _LAYERS:
            _write_file(staging, f"{layer}.json", _sign(layer, rows[layer], private_key, snapshot_id, now_ms))
        directory_fd = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        paths = {layer: staging / f"{layer}.json" for layer in _LAYERS}
        report = account_ledger.verify_snapshot(paths)
        os.rename(staging, output_dir)
        parent_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return report
    except Exception:
        _cleanup(staging)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-snapshot", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--litellm-base-url", required=True)
    parser.add_argument("--litellm-admin-key-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = export_snapshot(
            org_path=args.org_snapshot,
            database=args.database,
            base_url=args.litellm_base_url,
            admin_key_path=args.litellm_admin_key_file,
            output_dir=args.output_dir,
        )
    except (ExportError, OSError, ValueError, TypeError, UnicodeError):
        print(json.dumps({"status": "ERROR", "error": "invalid_input"}, separators=(",", ":")))
        return 2
    print(json.dumps(report, separators=(",", ":")))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
