from __future__ import annotations

import base64
import contextlib
import io
import json
import re
import secrets
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


_PRIVATE_KEY = Ed25519PrivateKey.generate()


def _write(path: Path, layer: str, snapshot_id: str, rows: list[dict[str, object]]) -> Path:
    now = int(time.time() * 1000)
    payload = {
        "version": 1,
        "audience": "hermes-1",
        "snapshot_id": snapshot_id,
        "issued_at_ms": now,
        "expires_at_ms": now + 600_000,
        "rows": rows,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["signature"] = base64.b64encode(
        _PRIVATE_KEY.sign(f"hermes-account-ledger:{layer}:v1\n".encode() + canonical)
    ).decode()
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run(tmp_path: Path, *, employees, routes, accounts, keys, tamper_layer=None, fingerprint_mode=0o600):
    snapshot_id = secrets.token_hex(16)
    paths = {
        name: _write(tmp_path / f"{name}.json", name, snapshot_id, rows)
        for name, rows in {
            "employees": employees,
            "routes": routes,
            "accounts": accounts,
            "keys": keys,
        }.items()
    }
    if tamper_layer:
        payload = json.loads(paths[tamper_layer].read_text(encoding="utf-8"))
        payload["rows"].append({})
        paths[tamper_layer].write_text(json.dumps(payload), encoding="utf-8")
    public_key = tmp_path / "account-ledger-public.pem"
    fingerprint_key = tmp_path / "account-ledger-fingerprint.key"
    public_key.write_bytes(_PRIVATE_KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ))
    fingerprint_key.write_bytes(b"test-only-fingerprint-secret")
    fingerprint_key.chmod(fingerprint_mode)
    from hermes_multitenancy import account_ledger
    old_path = account_ledger._PUBLIC_KEY_PATH
    old_fp_path = account_ledger._FINGERPRINT_KEY_PATH
    old_argv = sys.argv
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        account_ledger._PUBLIC_KEY_PATH = public_key
        account_ledger._FINGERPRINT_KEY_PATH = fingerprint_key
        sys.argv = [
            "account-ledger", "--employees", str(paths["employees"]),
            "--routes", str(paths["routes"]), "--accounts", str(paths["accounts"]),
            "--keys", str(paths["keys"]),
        ]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            returncode = account_ledger.main()
    finally:
        account_ledger._PUBLIC_KEY_PATH = old_path
        account_ledger._FINGERPRINT_KEY_PATH = old_fp_path
        sys.argv = old_argv
    return SimpleNamespace(returncode=returncode, stdout=stdout.getvalue(), stderr=stderr.getvalue())


def test_complete_one_to_one_ledger_passes_without_raw_identity(tmp_path):
    result = _run(
        tmp_path,
        employees=[
            {"employee_id": "alice", "profile": "profile-a", "open_id": "ou_a"},
            {"employee_id": "bob", "profile": "profile-b", "open_id": "ou_b"},
        ],
        routes=[
            {"employee_id": "alice", "profile": "profile-a", "open_id": "ou_a", "active": True, "kind": "user", "provenance": "sync"},
            {"employee_id": "bob", "profile": "profile-b", "open_id": "ou_b", "active": True, "kind": "user", "provenance": "sync"},
        ],
        accounts=[
            {"employee_id": "alice", "profile": "profile-a", "litellm_user_id": "ll-a", "active": True, "migration_state": "enforced"},
            {"employee_id": "bob", "profile": "profile-b", "litellm_user_id": "ll-b", "active": True, "migration_state": "enforced"},
        ],
        keys=[
            {"employee_id": "alice", "profile": "profile-a", "litellm_user_id": "ll-a", "key_id": "key-a", "expires_at": 4102444800000, "active": True},
            {"employee_id": "bob", "profile": "profile-b", "litellm_user_id": "ll-b", "key_id": "key-b", "expires_at": 4102444800000, "active": True},
        ],
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "status": "PASS",
        "employee_count": 2,
        "missing_route": 0,
        "missing_account": 0,
        "missing_current_key": 0,
        "duplicate_route": 0,
        "duplicate_account": 0,
        "duplicate_current_key": 0,
        "cross_identity": 0,
        "unexpected_subject": 0,
        "findings": [],
    }
    assert all(value not in result.stdout for value in ("alice", "bob", "profile-a", "ll-a", "key-a"))


def test_non_boolean_active_flag_fails_closed_as_invalid_input(tmp_path):
    result = _run(
        tmp_path,
        employees=[{"employee_id": "sensitive-employee"}],
        routes=[{"employee_id": "sensitive-employee", "profile": "private-profile", "active": "true"}],
        accounts=[],
        keys=[],
    )

    assert result.returncode == 2
    assert json.loads(result.stdout) == {"status": "ERROR", "error": "invalid_input"}
    assert "sensitive-employee" not in result.stdout + result.stderr


def test_casefold_duplicate_employee_denominator_fails_closed(tmp_path):
    result = _run(
        tmp_path,
        employees=[{"employee_id": "Alice"}, {"employee_id": "alice"}],
        routes=[],
        accounts=[],
        keys=[],
    )

    assert result.returncode == 2
    assert json.loads(result.stdout) == {"status": "ERROR", "error": "invalid_input"}


def test_tampered_signed_snapshot_fails_closed(tmp_path):
    result = _run(
        tmp_path,
        employees=[{"employee_id": "alice", "profile": "profile-a", "open_id": "ou_a"}],
        routes=[],
        accounts=[],
        keys=[],
        tamper_layer="employees",
    )

    assert result.returncode == 2
    assert json.loads(result.stdout) == {"status": "ERROR", "error": "invalid_input"}


def test_world_readable_fingerprint_key_fails_closed(tmp_path):
    result = _run(
        tmp_path,
        employees=[{"employee_id": "alice", "profile": "profile-a", "open_id": "ou_a"}],
        routes=[],
        accounts=[],
        keys=[],
        fingerprint_mode=0o644,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout) == {"status": "ERROR", "error": "invalid_input"}


def test_missing_duplicate_and_cross_identity_are_counted_without_raw_values(tmp_path):
    result = _run(
        tmp_path,
        employees=[
            {"employee_id": "alice", "profile": "profile-a", "open_id": "ou_a"},
            {"employee_id": "bob", "profile": "profile-b", "open_id": "ou_b"},
        ],
        routes=[
            {"employee_id": "alice", "profile": "profile-a", "open_id": "ou_a", "active": True, "kind": "user", "provenance": "sync"},
            {"employee_id": "alice", "profile": "profile-a-duplicate", "open_id": "ou_a2", "active": True, "kind": "user", "provenance": "sync"},
            {"employee_id": "ghost", "profile": "profile-ghost", "open_id": "ou_g", "active": True, "kind": "user", "provenance": "sync"},
        ],
        accounts=[
            {"employee_id": "alice", "profile": "profile-a", "litellm_user_id": "ll-a", "active": True, "migration_state": "enforced"},
            {"employee_id": "bob", "profile": "profile-b", "litellm_user_id": "ll-b", "active": True, "migration_state": "enforced"},
        ],
        keys=[
            {"employee_id": "alice", "profile": "profile-a", "litellm_user_id": "ll-wrong", "key_id": "key-a", "expires_at": 4102444800000, "active": True},
        ],
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["status"] == "FAIL"
    assert report["missing_route"] == 1
    assert report["duplicate_route"] == 1
    assert report["missing_current_key"] == 1
    assert report["cross_identity"] == 1
    assert report["unexpected_subject"] == 1
    assert {item["kind"] for item in report["findings"]} >= {
        "duplicate_route", "missing_current_key", "key_account_mismatch", "unexpected_subject"
    }
    assert all(re.fullmatch(r"[0-9a-f]{64}", item["subject_fp"]) for item in report["findings"])
    assert all(value not in result.stdout for value in ("alice", "bob", "ghost", "ll-wrong", "key-a"))


def test_expired_key_and_untrusted_route_are_actionable_missing_rows(tmp_path):
    result = _run(
        tmp_path,
        employees=[{"employee_id": "alice", "profile": "profile-a", "open_id": "ou_a"}],
        routes=[{"employee_id": "alice", "profile": "profile-a", "open_id": "ou_a", "active": True, "kind": "synthetic", "provenance": "auto"}],
        accounts=[{"employee_id": "alice", "profile": "profile-a", "litellm_user_id": "ll-a", "active": True, "migration_state": "enforced"}],
        keys=[{"employee_id": "alice", "profile": "profile-a", "litellm_user_id": "ll-a", "key_id": "key-a", "expires_at": 946684800000, "active": True}],
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["missing_route"] == 1
    assert report["missing_current_key"] == 1
    assert {item["kind"] for item in report["findings"]} >= {
        "missing_route", "missing_current_key", "route_not_active_trusted", "key_not_current"
    }


def test_roster_open_id_swap_is_cross_identity_failure(tmp_path):
    result = _run(
        tmp_path,
        employees=[
            {"employee_id": "alice", "profile": "profile-a", "open_id": "ou_a"},
            {"employee_id": "bob", "profile": "profile-b", "open_id": "ou_b"},
        ],
        routes=[
            {"employee_id": "alice", "profile": "profile-a", "open_id": "ou_b", "active": True, "kind": "user", "provenance": "sync"},
            {"employee_id": "bob", "profile": "profile-b", "open_id": "ou_a", "active": True, "kind": "user", "provenance": "sync"},
        ],
        accounts=[
            {"employee_id": "alice", "profile": "profile-a", "litellm_user_id": "ll-a", "active": True, "migration_state": "enforced"},
            {"employee_id": "bob", "profile": "profile-b", "litellm_user_id": "ll-b", "active": True, "migration_state": "enforced"},
        ],
        keys=[
            {"employee_id": "alice", "profile": "profile-a", "litellm_user_id": "ll-a", "key_id": "key-a", "expires_at": 4102444800000, "active": True},
            {"employee_id": "bob", "profile": "profile-b", "litellm_user_id": "ll-b", "key_id": "key-b", "expires_at": 4102444800000, "active": True},
        ],
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["cross_identity"] == 2
    assert all(value not in result.stdout for value in ("alice", "bob", "ou_a", "ou_b"))


def test_employee_ids_are_casefolded_across_signed_layers(tmp_path):
    result = _run(
        tmp_path,
        employees=[{"employee_id": "alice", "profile": "profile-a", "open_id": "ou_a"}],
        routes=[{"employee_id": "Alice", "profile": "profile-a", "open_id": "ou_a", "active": True, "kind": "user", "provenance": "sync"}],
        accounts=[{"employee_id": "ALICE", "profile": "profile-a", "litellm_user_id": "ll-a", "active": True, "migration_state": "enforced"}],
        keys=[{"employee_id": "alice", "profile": "profile-a", "litellm_user_id": "ll-a", "key_id": "key-a", "expires_at": 4102444800000, "active": True}],
    )

    assert result.returncode == 0, result.stdout


def test_missing_route_does_not_inflate_cross_identity(tmp_path):
    result = _run(
        tmp_path,
        employees=[{"employee_id": "alice", "profile": "profile-a", "open_id": "ou_a"}],
        routes=[],
        accounts=[{"employee_id": "alice", "profile": "profile-a", "litellm_user_id": "ll-a", "active": True, "migration_state": "enforced"}],
        keys=[{"employee_id": "alice", "profile": "profile-a", "litellm_user_id": "ll-a", "key_id": "key-a", "expires_at": 4102444800000, "active": True}],
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["missing_route"] == 1
    assert report["cross_identity"] == 0
    assert {item["kind"] for item in report["findings"]} == {"missing_route"}
