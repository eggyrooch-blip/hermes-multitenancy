"""Read-only, privacy-safe reconciliation of employee billing snapshots."""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
import stat
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .billing_identity import _is_canonical_employee_id


_PUBLIC_KEY_PATH = Path("/etc/hermes/account-ledger-public.pem")
_FINGERPRINT_KEY_PATH = Path("/etc/hermes/account-ledger-fingerprint.key")
_AUDIENCE = "hermes-1"
_SNAPSHOT_ID = re.compile(r"[0-9a-f]{32}")


class InvalidLedgerInput(ValueError):
    pass


def _load(
    path: Path, layer: str, public_key: Ed25519PublicKey
) -> tuple[list[dict[str, Any]], str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
        raise InvalidLedgerInput
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidLedgerInput from exc
    if not isinstance(value, dict) or set(value) != {
        "version", "audience", "snapshot_id", "issued_at_ms", "expires_at_ms", "rows", "signature"
    }:
        raise InvalidLedgerInput
    rows = value["rows"]
    try:
        issued = int(value["issued_at_ms"])
        expires = int(value["expires_at_ms"])
    except (TypeError, ValueError) as exc:
        raise InvalidLedgerInput from exc
    now = int(time.time() * 1000)
    if (
        value["version"] != 1
        or value["audience"] != _AUDIENCE
        or not _SNAPSHOT_ID.fullmatch(str(value["snapshot_id"]))
        or not isinstance(rows, list)
        or any(not isinstance(row, dict) for row in rows)
        or issued <= 0
        or expires <= issued
        or expires - issued > 1_800_000
        or now < issued
        or now >= expires
    ):
        raise InvalidLedgerInput
    unsigned = {name: item for name, item in value.items() if name != "signature"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    try:
        signature = base64.b64decode(str(value["signature"]), validate=True)
        public_key.verify(signature, f"hermes-account-ledger:{layer}:v1\n".encode() + canonical)
    except (ValueError, InvalidSignature):
        raise InvalidLedgerInput
    return rows, str(value["snapshot_id"])


def _subject(row: dict[str, Any]) -> str:
    value = str(row.get("employee_id") or "").strip()
    if not _is_canonical_employee_id(value):
        raise InvalidLedgerInput
    return value.casefold()


def audit(
    employees: list[dict[str, Any]],
    routes: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    keys: list[dict[str, Any]],
    fingerprint_key: bytes,
) -> dict[str, Any]:
    if len(fingerprint_key) < 16:
        raise InvalidLedgerInput
    employee_ids = [_subject(row) for row in employees]
    if not employee_ids or len(employee_ids) != len(set(employee_ids)):
        raise InvalidLedgerInput
    if any(
        not str(row.get("profile") or "").strip()
        or not str(row.get("open_id") or "").startswith("ou_")
        for row in employees
    ):
        raise InvalidLedgerInput
    if (
        len({str(row["profile"]) for row in employees}) != len(employees)
        or len({str(row["open_id"]) for row in employees}) != len(employees)
    ):
        raise InvalidLedgerInput
    expected = set(employee_ids)
    roster = {employee_id: row for employee_id, row in zip(employee_ids, employees)}

    if any(type(row.get("active")) is not bool for row in routes + accounts + keys):
        raise InvalidLedgerInput

    now = int(time.time() * 1000)
    findings: set[tuple[str, str]] = set()
    route_rows = []
    for row in routes:
        subject = _subject(row)
        if (
            row["active"]
            and row.get("kind") == "user"
            and row.get("provenance") == "sync"
            and str(row.get("profile") or "").strip()
            and str(row.get("open_id") or "").startswith("ou_")
        ):
            route_rows.append(row)
        else:
            findings.add(("route_not_active_trusted", subject))

    account_rows = []
    for row in accounts:
        subject = _subject(row)
        if (
            row["active"]
            and row.get("migration_state") == "enforced"
            and str(row.get("profile") or "").strip()
            and str(row.get("litellm_user_id") or "").strip()
        ):
            account_rows.append(row)
        else:
            findings.add(("account_not_active_enforced", subject))

    key_rows = []
    for row in keys:
        subject = _subject(row)
        try:
            expires_at = int(row.get("expires_at"))
        except (TypeError, ValueError):
            raise InvalidLedgerInput
        if (
            row["active"]
            and expires_at > now
            and str(row.get("profile") or "").strip()
            and str(row.get("litellm_user_id") or "").strip()
            and str(row.get("key_id") or "").strip()
        ):
            key_rows.append(row)
        else:
            findings.add(("key_not_current", subject))
    active_layers = (route_rows, account_rows, key_rows)

    by_layer: list[dict[str, list[dict[str, Any]]]] = []
    for rows in active_layers:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[_subject(row)].append(row)
        by_layer.append(grouped)
    route_by, account_by, key_by = by_layer

    for subject in expected:
        for rows, missing_kind, duplicate_kind in (
            (route_by.get(subject, ()), "missing_route", "duplicate_route"),
            (account_by.get(subject, ()), "missing_account", "duplicate_account"),
            (key_by.get(subject, ()), "missing_current_key", "duplicate_current_key"),
        ):
            if not rows:
                findings.add((missing_kind, subject))
            elif len(rows) > 1:
                findings.add((duplicate_kind, subject))
        if len(route_by.get(subject, ())) == 1 and (
            str(route_by[subject][0].get("open_id") or "") != str(roster[subject]["open_id"])
            or str(route_by[subject][0].get("profile") or "") != str(roster[subject]["profile"])
        ):
            findings.add(("roster_route_mismatch", subject))
        if len(account_by.get(subject, ())) == len(key_by.get(subject, ())) == 1:
            account_id = str(account_by[subject][0].get("litellm_user_id") or "")
            key_account_id = str(key_by[subject][0].get("litellm_user_id") or "")
            account_profile = str(account_by[subject][0].get("profile") or "")
            key_profile = str(key_by[subject][0].get("profile") or "")
            if (
                not account_id
                or account_id != key_account_id
                or account_profile != key_profile
            ):
                findings.add(("key_account_mismatch", subject))
            if len(route_by.get(subject, ())) == 1:
                route_profile = str(route_by[subject][0].get("profile") or "")
                if route_profile != account_profile or route_profile != key_profile:
                    findings.add(("key_account_mismatch", subject))

    for layer, field, kind in (
        (route_rows, "profile", "shared_profile"),
        (route_rows, "open_id", "shared_open_id"),
        (account_rows, "litellm_user_id", "shared_account"),
        (key_rows, "key_id", "shared_key"),
    ):
        owners: dict[str, set[str]] = defaultdict(set)
        for row in layer:
            value = str(row.get(field) or "").strip()
            if not value:
                findings.add((f"missing_{field}", _subject(row)))
            else:
                owners[value].add(_subject(row))
        for subjects in owners.values():
            if len(subjects) > 1:
                findings.update((kind, subject) for subject in subjects)

    unexpected = {_subject(row) for row in routes + accounts + keys} - expected
    findings.update(("unexpected_subject", subject) for subject in unexpected)
    cross_kinds = {"roster_route_mismatch", "key_account_mismatch", "shared_profile", "shared_open_id", "shared_account", "shared_key"}
    report = {
        "status": "PASS",
        "employee_count": len(expected),
        "missing_route": sum(not route_by.get(subject) for subject in expected),
        "missing_account": sum(not account_by.get(subject) for subject in expected),
        "missing_current_key": sum(not key_by.get(subject) for subject in expected),
        "duplicate_route": sum(len(route_by.get(subject, ())) > 1 for subject in expected),
        "duplicate_account": sum(len(account_by.get(subject, ())) > 1 for subject in expected),
        "duplicate_current_key": sum(len(key_by.get(subject, ())) > 1 for subject in expected),
        "cross_identity": sum(kind in cross_kinds for kind, _ in findings),
        "unexpected_subject": len(unexpected),
        "findings": [
            {
                "kind": kind,
                "subject_fp": hmac.new(
                    fingerprint_key,
                    b"hermes-account-ledger:subject:v1\n" + subject.encode(),
                    hashlib.sha256,
                ).hexdigest(),
            }
            for kind, subject in sorted(findings)
        ],
    }
    if report["findings"]:
        report["status"] = "FAIL"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit frozen employee billing snapshots")
    for name in ("employees", "routes", "accounts", "keys"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    try:
        if _PUBLIC_KEY_PATH.is_symlink() or not _PUBLIC_KEY_PATH.is_file():
            raise InvalidLedgerInput
        if (
            _FINGERPRINT_KEY_PATH.is_symlink()
            or not _FINGERPRINT_KEY_PATH.is_file()
            or _FINGERPRINT_KEY_PATH.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            raise InvalidLedgerInput
        fingerprint_key = _FINGERPRINT_KEY_PATH.read_bytes()
        if len(fingerprint_key) < 16 or len(fingerprint_key) > 4096:
            raise InvalidLedgerInput
        public_key = serialization.load_pem_public_key(_PUBLIC_KEY_PATH.read_bytes())
        if not isinstance(public_key, Ed25519PublicKey):
            raise InvalidLedgerInput
        loaded = [
            _load(getattr(args, layer), layer, public_key)
            for layer in ("employees", "routes", "accounts", "keys")
        ]
        if len({snapshot_id for _rows, snapshot_id in loaded}) != 1:
            raise InvalidLedgerInput
        report = audit(*(rows for rows, _snapshot_id in loaded), fingerprint_key)
    except (InvalidLedgerInput, OSError, ValueError, TypeError):
        print(json.dumps({"status": "ERROR", "error": "invalid_input"}))
        return 2
    print(json.dumps(report, sort_keys=False, separators=(",", ":")))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
