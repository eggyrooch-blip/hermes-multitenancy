from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import secrets
import sqlite3
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
    path.chmod(0o600)
    return path


def _run(
    tmp_path: Path, *, employees, routes, accounts, keys, tamper_layer=None,
    fingerprint_mode=0o600, envelope_mode=0o600,
):
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
    for path in paths.values():
        path.chmod(envelope_mode)
    public_key = tmp_path / "account-ledger-public.pem"
    fingerprint_key = tmp_path / "account-ledger-fingerprint.key"
    public_key.write_bytes(_PRIVATE_KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ))
    public_key.chmod(0o644)
    fingerprint_key.write_bytes(b"test-only-fingerprint-secret")
    fingerprint_key.chmod(fingerprint_mode)
    from hermes_multitenancy import account_ledger
    old_path = account_ledger._PUBLIC_KEY_PATH
    old_fp_path = account_ledger._FINGERPRINT_KEY_PATH
    old_trust_root = account_ledger._TRUST_ROOT
    old_argv = sys.argv
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        account_ledger._PUBLIC_KEY_PATH = public_key
        account_ledger._FINGERPRINT_KEY_PATH = fingerprint_key
        account_ledger._TRUST_ROOT = tmp_path
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
        account_ledger._TRUST_ROOT = old_trust_root
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
        "route_count": 2,
        "account_count": 2,
        "current_key_count": 2,
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


def test_world_readable_signed_envelope_fails_closed(tmp_path):
    result = _run(
        tmp_path,
        employees=[{"employee_id": "alice", "profile": "profile-a", "open_id": "ou_a"}],
        routes=[],
        accounts=[],
        keys=[],
        envelope_mode=0o644,
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


class _InventoryResponse:
    def __init__(self, payload):
        self.status = 200
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit):
        return self._payload[:limit]


def _run_exporter(
    tmp_path, monkeypatch, *, users=None, keys=None, database_padding_bytes=0,
    signer_mode=0o600, signer_parent_mode=None, mismatched_public_key=False,
    base_url="https://litellm.sre.gotokeep.com", users_second_read=None,
    mutate_org_during_inventory=False, database_swap_back=False,
):
    from hermes_multitenancy import account_ledger, account_ledger_export

    private_key = Ed25519PrivateKey.generate()
    signer_parent = tmp_path
    if signer_parent_mode is not None:
        signer_parent = tmp_path / "signer-parent"
        signer_parent.mkdir()
        signer_parent.chmod(signer_parent_mode)
    private_path = signer_parent / "signing-private.pem"
    private_path.write_bytes(private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    private_path.chmod(signer_mode)
    public_path = tmp_path / "public.pem"
    public_source = (
        Ed25519PrivateKey.generate().public_key()
        if mismatched_public_key
        else private_key.public_key()
    )
    public_path.write_bytes(public_source.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    public_path.chmod(0o644)
    fingerprint_path = tmp_path / "fingerprint.key"
    fingerprint_path.write_bytes(b"test-only-fingerprint-secret")
    fingerprint_path.chmod(0o600)
    admin_key_path = tmp_path / "litellm-readonly.key"
    admin_key_path.write_text("test-admin-key", encoding="utf-8")
    admin_key_path.chmod(0o600)

    org_path = tmp_path / "org-frozen.json"
    org_path.write_text(json.dumps({
        "version": 2,
        "employees": {
            "sensitive-employee": {
                "user_id": "sensitive-employee",
                "profile_name": "private-profile",
                "open_id": "ou_private",
            }
        },
    }), encoding="utf-8")
    org_path.chmod(0o600)

    db_path = tmp_path / "multitenancy.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE multitenancy_routing ("
            "user_id TEXT, profile_name TEXT, open_id TEXT, active INTEGER, "
            "kind TEXT, provenance TEXT)"
        )
        connection.execute(
            "INSERT INTO multitenancy_routing VALUES "
            "('sensitive-employee','private-profile','ou_private',1,'user','sync')"
        )
        connection.execute(
            "CREATE TABLE multitenancy_billing_identities ("
            "employee_user_id TEXT, profile_name TEXT, email TEXT, "
            "litellm_user_id TEXT, key_id TEXT, expires_at INTEGER, "
            "migration_state TEXT)"
        )
        connection.execute(
            "INSERT INTO multitenancy_billing_identities VALUES "
            "('sensitive-employee','private-profile','sensitive@keep.com',"
            "'ll-private','key-private',4102444800000,'enforced')"
        )
        if database_padding_bytes:
            connection.execute("CREATE TABLE unrelated_large_table (payload BLOB)")
            connection.execute(
                "INSERT INTO unrelated_large_table VALUES (zeroblob(?))",
                (database_padding_bytes,),
            )
    db_path.chmod(0o600)

    if users is None:
        users = [{
            "user_id": "ll-private",
            "user_email": "sensitive@keep.com",
            "metadata": {"hermes_employee_id": "sensitive-employee"},
        }]
    if keys is None:
        keys = [{
            "token": "key-private",
            "user_id": "ll-private",
            "key_alias": "hermes-sensitive",
            "expires": 4102444800000,
            "blocked": False,
            "metadata": {
                "purpose": "hermes_runtime",
                "hermes_employee_id": "sensitive-employee",
            },
        }]
    calls = []
    user_reads = 0

    def get_only(request, timeout):
        nonlocal user_reads
        calls.append((request.method, request.full_url, timeout))
        assert request.get_header("Authorization") == "Bearer test-admin-key"
        if mutate_org_during_inventory and len(calls) == 1:
            org_path.write_text(org_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        if "/user/list?" in request.full_url:
            user_reads += 1
            return _InventoryResponse({
                "users": (
                    users_second_read
                    if user_reads > 1 and users_second_read is not None
                    else users
                ),
                "total_pages": 1,
            })
        if "/key/list?" in request.full_url:
            return _InventoryResponse({
                "keys": keys,
                "total_pages": 1,
            })
        raise AssertionError("unexpected inventory endpoint")

    monkeypatch.setattr(account_ledger_export, "_SIGNING_KEY_PATH", private_path)
    monkeypatch.setattr(account_ledger, "_PUBLIC_KEY_PATH", public_path)
    monkeypatch.setattr(account_ledger, "_FINGERPRINT_KEY_PATH", fingerprint_path)
    monkeypatch.setattr(account_ledger, "_TRUST_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(account_ledger_export, "_URL_OPEN", get_only)
    if database_swap_back:
        replacement = tmp_path / "replacement.db"
        replacement.write_bytes(db_path.read_bytes())
        replacement.chmod(0o600)
        original = tmp_path / "original.db"
        used = tmp_path / "used-replacement.db"
        real_connect = account_ledger_export.sqlite3.connect
        swapped = False

        def connect_with_swap_back(database_arg, *args, **kwargs):
            nonlocal swapped
            if not swapped and kwargs.get("uri") and "mode=ro" in str(database_arg):
                os.replace(db_path, original)
                os.replace(replacement, db_path)
                connection = real_connect(database_arg, *args, **kwargs)
                connection.execute("PRAGMA schema_version").fetchone()
                os.replace(db_path, used)
                os.replace(original, db_path)
                swapped = True
                return connection
            return real_connect(database_arg, *args, **kwargs)

        monkeypatch.setattr(account_ledger_export.sqlite3, "connect", connect_with_swap_back)
    output_dir = tmp_path / "published"
    returncode = account_ledger_export.main([
        "--org-snapshot", str(org_path),
        "--database", str(db_path),
        "--litellm-base-url", base_url,
        "--litellm-admin-key-file", str(admin_key_path),
        "--output-dir", str(output_dir),
    ])
    return returncode, output_dir, calls


def test_production_exporter_writes_one_verified_private_snapshot(
    tmp_path, monkeypatch, capsys
):
    returncode, output_dir, calls = _run_exporter(tmp_path, monkeypatch)

    captured = capsys.readouterr()
    assert returncode == 0, captured
    assert {method for method, _url, _timeout in calls} == {"GET"}
    assert len(calls) == 4
    report = json.loads(captured.out)
    assert report["status"] == "PASS"
    assert report["employee_count"] == 1
    assert report["route_count"] == 1
    assert report["account_count"] == 1
    assert report["current_key_count"] == 1
    assert all(
        raw not in captured.out + captured.err
        for raw in (
            "sensitive-employee", "private-profile", "ou_private",
            "sensitive@keep.com", "ll-private", "key-private", "test-admin-key",
        )
    )
    envelopes = [output_dir / f"{layer}.json" for layer in ("employees", "routes", "accounts", "keys")]
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in envelopes)
    values = [json.loads(path.read_text(encoding="utf-8")) for path in envelopes]
    assert len({value["snapshot_id"] for value in values}) == 1
    assert {value["audience"] for value in values} == {"hermes-1"}
    assert all(value["expires_at_ms"] - value["issued_at_ms"] == 1_800_000 for value in values)


def test_production_exporter_does_not_pass_a_disabled_upstream_account(
    tmp_path, monkeypatch, capsys
):
    returncode, output_dir, _calls = _run_exporter(
        tmp_path,
        monkeypatch,
        users=[{
            "user_id": "ll-private",
            "user_email": "sensitive@keep.com",
            "metadata": {
                "hermes_employee_id": "sensitive-employee",
                "hermes_billing_disabled": True,
            },
        }],
    )

    captured = capsys.readouterr()
    assert returncode == 1
    report = json.loads(captured.out)
    assert report["status"] == "FAIL"
    assert report["account_count"] == 0
    assert report["missing_account"] == 1
    assert output_dir.is_dir()
    assert "sensitive-employee" not in captured.out + captured.err


def test_production_exporter_rejects_ambiguous_exact_email_before_publish(
    tmp_path, monkeypatch, capsys
):
    returncode, output_dir, calls = _run_exporter(
        tmp_path,
        monkeypatch,
        users=[
            {
                "user_id": "ll-private",
                "user_email": "sensitive@keep.com",
                "metadata": {"hermes_employee_id": "sensitive-employee"},
            },
            {
                "user_id": "ll-collision",
                "user_email": "SENSITIVE@keep.com",
                "metadata": {"hermes_employee_id": "another-employee"},
            },
        ],
    )

    captured = capsys.readouterr()
    assert returncode == 2
    assert json.loads(captured.out) == {"status": "ERROR", "error": "invalid_input"}
    assert not output_dir.exists()
    assert {method for method, _url, _timeout in calls} == {"GET"}
    assert all(
        raw not in captured.out + captured.err
        for raw in ("sensitive-employee", "another-employee", "sensitive@keep.com")
    )


def test_production_exporter_freezes_large_realistic_sqlite_into_root_staging(
    tmp_path, monkeypatch, capsys
):
    returncode, output_dir, _calls = _run_exporter(
        tmp_path,
        monkeypatch,
        database_padding_bytes=24 * 1024 * 1024,
    )

    captured = capsys.readouterr()
    assert returncode == 0, captured
    assert json.loads(captured.out)["status"] == "PASS"
    assert output_dir.is_dir()


def test_production_exporter_rejects_world_readable_signer_before_http(
    tmp_path, monkeypatch, capsys
):
    returncode, output_dir, calls = _run_exporter(
        tmp_path,
        monkeypatch,
        signer_mode=0o644,
    )

    captured = capsys.readouterr()
    assert returncode == 2
    assert json.loads(captured.out) == {"status": "ERROR", "error": "invalid_input"}
    assert calls == []
    assert not output_dir.exists()


def test_production_exporter_rejects_signer_trust_root_mismatch_before_http(
    tmp_path, monkeypatch, capsys
):
    returncode, output_dir, calls = _run_exporter(
        tmp_path,
        monkeypatch,
        mismatched_public_key=True,
    )

    captured = capsys.readouterr()
    assert returncode == 2
    assert json.loads(captured.out) == {"status": "ERROR", "error": "invalid_input"}
    assert calls == []
    assert not output_dir.exists()


def test_frozen_org_reader_rejects_path_swap_after_open(tmp_path, monkeypatch):
    from hermes_multitenancy import account_ledger_export

    org_path = tmp_path / "org-frozen.json"
    org_path.write_text('{"employees":{}}', encoding="utf-8")
    org_path.chmod(0o600)
    replacement = tmp_path / "replacement.json"
    replacement.write_text('{"employees":{"changed":{}}}', encoding="utf-8")
    replacement.chmod(0o600)
    real_stat = account_ledger_export.os.stat
    swapped = False

    def swap_before_locator_check(path, *args, **kwargs):
        nonlocal swapped
        if Path(path) == org_path and not swapped:
            swapped = True
            account_ledger_export.os.replace(replacement, org_path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(account_ledger_export.os, "stat", swap_before_locator_check)
    try:
        account_ledger_export._read_file(org_path, max_bytes=1024)
    except account_ledger_export.ExportError as exc:
        assert str(exc) == "input_changed"
    else:
        raise AssertionError("path-swapped snapshot must fail closed")


def test_production_exporter_rejects_unapproved_https_origin_before_http(
    tmp_path, monkeypatch, capsys
):
    returncode, output_dir, calls = _run_exporter(
        tmp_path,
        monkeypatch,
        base_url="https://attacker.invalid",
    )

    captured = capsys.readouterr()
    assert returncode == 2
    assert json.loads(captured.out) == {"status": "ERROR", "error": "invalid_input"}
    assert calls == []
    assert not output_dir.exists()


def test_production_exporter_rejects_signer_under_writable_ancestor_before_http(
    tmp_path, monkeypatch, capsys
):
    returncode, output_dir, calls = _run_exporter(
        tmp_path,
        monkeypatch,
        signer_parent_mode=0o777,
    )

    captured = capsys.readouterr()
    assert returncode == 2
    assert json.loads(captured.out) == {"status": "ERROR", "error": "invalid_input"}
    assert calls == []
    assert not output_dir.exists()


def test_production_exporter_rejects_sqlite_swap_back_before_http(
    tmp_path, monkeypatch, capsys
):
    returncode, output_dir, calls = _run_exporter(
        tmp_path,
        monkeypatch,
        database_swap_back=True,
    )

    captured = capsys.readouterr()
    assert returncode == 2
    assert json.loads(captured.out) == {"status": "ERROR", "error": "invalid_input"}
    assert calls == []
    assert not output_dir.exists()


def test_production_exporter_rejects_cross_source_change_before_publish(
    tmp_path, monkeypatch, capsys
):
    returncode, output_dir, _calls = _run_exporter(
        tmp_path,
        monkeypatch,
        mutate_org_during_inventory=True,
    )

    captured = capsys.readouterr()
    assert returncode == 2
    assert json.loads(captured.out) == {"status": "ERROR", "error": "invalid_input"}
    assert not output_dir.exists()


def test_production_exporter_rejects_changing_upstream_inventory_before_publish(
    tmp_path, monkeypatch, capsys
):
    returncode, output_dir, calls = _run_exporter(
        tmp_path,
        monkeypatch,
        users_second_read=[],
    )

    captured = capsys.readouterr()
    assert returncode == 2
    assert json.loads(captured.out) == {"status": "ERROR", "error": "invalid_input"}
    assert len(calls) >= 3
    assert not output_dir.exists()


def test_production_unit_uses_dedicated_root_runtime_with_atomic_install_contract():
    unit = Path("deploy/hermes-account-ledger-export.service").read_text(encoding="utf-8")
    runbook = Path("docs/account-ledger-production-runbook.md").read_text(encoding="utf-8")

    exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert exec_start.startswith(
        "ExecStart=/opt/hermes/account-ledger-export/venv/bin/python "
    )
    assert "/home/hermes" not in exec_start
    assert "atomic symlink switch" in runbook
    assert "cryptography>=43.0" in runbook
    assert "cryptography.__file__" in runbook
    assert "root:root" in runbook and "group/world-writable=0" in runbook


def test_root_runtime_install_never_invokes_ambient_python_or_uv():
    runbook = Path("docs/account-ledger-production-runbook.md").read_text(encoding="utf-8")
    root_install = runbook.split("<!-- root-install:start -->", 1)[1].split(
        "<!-- root-install:end -->", 1
    )[0]

    assert "$UV_BIN" not in root_install
    assert not re.search(r"(?<![/\w])python3(?:\s|$)", root_install)
    assert "/usr/bin/python3.11" in root_install
    assert "--require-hashes" in root_install
    assert "EXPECTED_ARTIFACT_SHA256" in root_install
    assert "EXPECTED_LOCK_SHA256" in root_install
    assert "hermes_multitenancy.account_ledger_export.__file__" in root_install
    assert "bundle/deploy/hermes-account-ledger-export.service" in runbook
    assert " deploy/hermes-account-ledger-export.service " not in runbook
