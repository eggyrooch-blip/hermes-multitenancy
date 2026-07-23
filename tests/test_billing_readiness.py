from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_multitenancy.billing_readiness import (
    BillingReadinessError,
    _consume_nonces,
    _run_live_recheck,
    cohort_hash,
    verify_artifact,
    verify_enabled_environment,
)


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _artifact(bindings: dict, *, nonce: str = "opaque", issued_at: int = 100) -> dict:
    artifact = {
        "version": 1,
        "nonce": nonce,
        "issued_at": issued_at,
        "expires_at": issued_at + 1200,
        "input_digest": bindings["input_digest"],
        "inventory_digest": "inventory",
        "employee_count": 1,
        "counts": {"READY_EXISTING": 1},
        "cohort_count": 1,
        "cohort_counts": {"READY_EXISTING": 1},
        "cohort_hash": cohort_hash("employee-a"),
        "policy_digest": "policy",
        "code_sha": "code",
        "contract_major": "1",
        "routing_watermark": bindings["routing_watermark"],
        "org_sha": bindings["org_sha"],
    }
    key = hmac.new(
        b"secret",
        b"hermes-billing-readiness-artifact:v1",
        hashlib.sha256,
    ).digest()
    artifact["signature"] = hmac.new(
        key,
        json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()
    return artifact


def _resign(artifact: dict) -> None:
    artifact.pop("signature", None)
    key = hmac.new(
        b"secret",
        b"hermes-billing-readiness-artifact:v1",
        hashlib.sha256,
    ).digest()
    artifact["signature"] = hmac.new(
        key,
        json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()


def _environment(tmp_path, monkeypatch) -> tuple[dict[str, str], dict]:
    employees = [{
        "employee_id": "employee-a",
        "email": "employee-a@keep.com",
        "cohort": True,
    }]
    employee_input = tmp_path / "employees.json"
    employee_input.write_text(json.dumps({"employees": employees}), encoding="utf-8")
    os.chmod(employee_input, 0o600)

    user_rows = [{
        "user_id": "employee-a",
        "profile_name": "employee-a",
        "open_id": "ou_employee_a",
        "union_id": "on_employee_a",
        "version": 1,
        "updated_at": 1,
    }]
    db_path = tmp_path / "multitenancy.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE multitenancy_routing ("
        "user_id TEXT PRIMARY KEY, profile_name TEXT, open_id TEXT, union_id TEXT, "
        "version INTEGER, updated_at INTEGER, active INTEGER, kind TEXT, "
        "provenance TEXT, chat_id TEXT, owner_open_id TEXT)"
    )
    connection.execute(
        "INSERT INTO multitenancy_routing "
        "(user_id, profile_name, open_id, union_id, version, updated_at, active, "
        "kind, provenance) VALUES (?, ?, ?, ?, ?, ?, 1, 'user', 'sync')",
        (
            "employee-a",
            "employee-a",
            "ou_employee_a",
            "on_employee_a",
            1,
            1,
        ),
    )
    connection.commit()
    connection.close()

    org_dir = tmp_path / "org"
    org_dir.mkdir()
    org_path = org_dir / "org-1.json"
    org_path.write_text(json.dumps({
        "employees": {
            "employee-a": {"user_id": "employee-a", "open_id": "ou_employee_a"}
        }
    }), encoding="utf-8")
    bindings = {
        "input_digest": _digest(employees),
        "routing_watermark": _digest({"groups": [], "users": user_rows}),
        "org_sha": hashlib.sha256(org_path.read_bytes()).hexdigest(),
    }

    first = tmp_path / "first.json"
    first.write_text(json.dumps(_artifact(bindings, nonce="first")), encoding="utf-8")
    os.chmod(first, 0o600)
    live_dir = tmp_path / "live"
    live_dir.mkdir(mode=0o700)
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir(mode=0o700)
    replay_path = replay_dir / "consumed"
    replay_path.touch(mode=0o600)
    os.chmod(replay_dir, 0o500)
    env = {
        "HERMES_LITELLM_BILLING_ENABLED": "true",
        "HERMES_LITELLM_BILLING_PAYER_IDS": "employee-a",
        "HERMES_LITELLM_BILLING_READINESS_ARTIFACT": str(first),
        "HERMES_LITELLM_BILLING_REPLAY_STORE": str(replay_path),
        "HERMES_AI_GATEWAY_BROKER_TOKEN": "secret",
        "HERMES_LITELLM_BILLING_POLICY_DIGEST": "policy",
        "HERMES_LITELLM_BILLING_CODE_SHA": "code",
        "HERMES_LITELLM_BILLING_CONTRACT_MAJOR": "1",
        "HERMES_LITELLM_BILLING_ROUTING_WATERMARK": bindings["routing_watermark"],
        "HERMES_LITELLM_BILLING_ORG_SHA": bindings["org_sha"],
        "HERMES_AI_GATEWAY_READINESS_CLI": "/usr/bin/true",
        "HERMES_LITELLM_BILLING_EMPLOYEE_INPUT": str(employee_input),
        "HERMES_LITELLM_BILLING_LIVE_RECHECK_DIR": str(live_dir),
        "HERMES_MULTITENANCY_DB": str(db_path),
        "HERMES_ORG_SNAPSHOT_DIR": str(org_dir),
        "UNRELATED_SERVICE_SECRET": "must-not-reach-child",
    }
    monkeypatch.setattr("hermes_multitenancy.billing_readiness.time.time", lambda: 200)
    return env, bindings


def _verification(bindings: dict) -> dict:
    return {
        "secret": "secret",
        "now": 200,
        "expected_cohort_hash": cohort_hash("employee-a"),
        "expected_policy_digest": "policy",
        "expected_code_sha": "code",
        "expected_contract_major": "1",
        "expected_routing_watermark": bindings["routing_watermark"],
        "expected_org_sha": bindings["org_sha"],
        "expected_input_digest": bindings["input_digest"],
        "expected_employee_count": 1,
    }


def test_readiness_artifact_accepts_bound_fresh_snapshot(tmp_path, monkeypatch):
    _env, bindings = _environment(tmp_path, monkeypatch)
    verify_artifact(_artifact(bindings), **_verification(bindings))


@pytest.mark.parametrize("field", ["signature", "cohort_hash", "policy_digest", "code_sha"])
def test_readiness_artifact_drift_fails_closed(field, tmp_path, monkeypatch):
    _env, bindings = _environment(tmp_path, monkeypatch)
    artifact = _artifact(bindings)
    artifact[field] = "bad"
    with pytest.raises(BillingReadinessError):
        verify_artifact(artifact, **_verification(bindings))


def test_readiness_artifact_rejects_unknown_misadmission_status(
    tmp_path, monkeypatch
):
    _env, bindings = _environment(tmp_path, monkeypatch)
    artifact = _artifact(bindings)
    artifact["counts"] = {
        "READY_EXISTING": 0,
        "NONCOHORT_MISADMITTED": 1,
    }
    _resign(artifact)
    with pytest.raises(BillingReadinessError, match="coverage_invalid"):
        verify_artifact(artifact, **_verification(bindings))


@pytest.mark.parametrize("raw", ["employee-a,employee-a", "employee-a,Employee-A", "*", ""])
def test_canary_cohort_rejects_duplicates_and_wildcards(raw):
    with pytest.raises(BillingReadinessError, match="cohort_invalid"):
        cohort_hash(raw)


def test_enabled_environment_requires_artifact_and_service_secret(monkeypatch):
    monkeypatch.setenv("HERMES_LITELLM_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", "employee-a")
    with pytest.raises(BillingReadinessError, match="local_universe|missing"):
        verify_enabled_environment()


def test_enabled_environment_binds_local_universe_and_consumes_nonces(
    tmp_path, monkeypatch
):
    env, bindings = _environment(tmp_path, monkeypatch)
    challenges = iter(("livechallenge1", "livechallenge2"))
    monkeypatch.setattr(
        "hermes_multitenancy.billing_readiness.secrets.token_hex",
        lambda _size: next(challenges),
    )

    def write_live(argv, **kwargs):
        output = argv[argv.index("--output") + 1]
        nonce = argv[argv.index("--nonce") + 1]
        assert argv[argv.index("--routing-watermark") + 1] == bindings["routing_watermark"]
        assert argv[argv.index("--org-sha") + 1] == bindings["org_sha"]
        assert "UNRELATED_SERVICE_SECRET" not in kwargs["env"]
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(_artifact(bindings, nonce=nonce, issued_at=200), handle)
        os.chmod(output, 0o600)

    monkeypatch.setattr(
        "hermes_multitenancy.billing_readiness.subprocess.run", write_live
    )
    verify_enabled_environment(env)
    with pytest.raises(BillingReadinessError, match="already_consumed"):
        verify_enabled_environment(env)


def test_enabled_environment_rejects_local_routing_drift(tmp_path, monkeypatch):
    env, _bindings = _environment(tmp_path, monkeypatch)
    connection = sqlite3.connect(env["HERMES_MULTITENANCY_DB"])
    connection.execute(
        "UPDATE multitenancy_routing SET profile_name = 'changed' "
        "WHERE user_id = 'employee-a'"
    )
    connection.commit()
    connection.close()
    with pytest.raises(BillingReadinessError, match="local_binding_drift"):
        verify_enabled_environment(env)


def test_enabled_environment_rejects_unknown_cohort_payer(tmp_path, monkeypatch):
    env, _bindings = _environment(tmp_path, monkeypatch)
    env["HERMES_LITELLM_BILLING_PAYER_IDS"] = "employee-b"
    with pytest.raises(BillingReadinessError, match="local_universe_drift"):
        verify_enabled_environment(env)


def test_enabled_environment_rejects_inventory_drift(tmp_path, monkeypatch):
    env, bindings = _environment(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "hermes_multitenancy.billing_readiness.secrets.token_hex",
        lambda _size: "livechallenge",
    )

    def write_drifted_live(argv, **_kwargs):
        output = argv[argv.index("--output") + 1]
        artifact = _artifact(bindings, nonce="livechallenge", issued_at=200)
        artifact["inventory_digest"] = "drift"
        _resign(artifact)
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(artifact, handle)
        os.chmod(output, 0o600)

    monkeypatch.setattr(
        "hermes_multitenancy.billing_readiness.subprocess.run",
        write_drifted_live,
    )
    with pytest.raises(BillingReadinessError, match="recheck_drift"):
        verify_enabled_environment(env)


def test_enabled_environment_rejects_local_drift_during_live_recheck(
    tmp_path, monkeypatch
):
    env, bindings = _environment(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "hermes_multitenancy.billing_readiness.secrets.token_hex",
        lambda _size: "livechallenge",
    )

    def mutate_local_routing(argv, **_kwargs):
        connection = sqlite3.connect(env["HERMES_MULTITENANCY_DB"])
        connection.execute(
            "UPDATE multitenancy_routing SET updated_at = 2 "
            "WHERE user_id = 'employee-a'"
        )
        connection.commit()
        connection.close()
        output = argv[argv.index("--output") + 1]
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(
                _artifact(bindings, nonce="livechallenge", issued_at=200),
                handle,
            )
        os.chmod(output, 0o600)

    monkeypatch.setattr(
        "hermes_multitenancy.billing_readiness.subprocess.run",
        mutate_local_routing,
    )
    with pytest.raises(BillingReadinessError, match="local_universe_drift"):
        verify_enabled_environment(env)


def test_live_recheck_noop_cannot_reuse_preexisting_artifact(tmp_path, monkeypatch):
    env, _bindings = _environment(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "hermes_multitenancy.billing_readiness.secrets.token_hex",
        lambda _size: "freshchallenge",
    )
    monkeypatch.setattr(
        "hermes_multitenancy.billing_readiness.subprocess.run",
        lambda *args, **kwargs: None,
    )
    with pytest.raises(BillingReadinessError, match="unreadable"):
        verify_enabled_environment(env)


def test_nonce_store_is_pinned_and_requires_a_nonwritable_parent(tmp_path):
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir(mode=0o700)
    store = replay_dir / "consumed"
    store.touch(mode=0o600)

    with pytest.raises(BillingReadinessError, match="permissions_invalid"):
        _consume_nonces(str(store), ("nonce-a",), 100)

    os.chmod(replay_dir, 0o500)
    try:
        _consume_nonces(str(store), ("nonce-a",), 100)
        with pytest.raises(BillingReadinessError, match="already_consumed"):
            _consume_nonces(str(store), ("nonce-a",), 101)
        assert json.loads(store.read_text().strip()) == {
            "consumed_at": 100,
            "nonce": "nonce-a",
        }
    finally:
        os.chmod(replay_dir, 0o700)


def test_nonce_store_rejects_symlink_leaf(tmp_path):
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.touch(mode=0o600)
    link = replay_dir / "consumed"
    link.symlink_to(target)
    os.chmod(replay_dir, 0o500)
    try:
        with pytest.raises(BillingReadinessError, match="unavailable"):
            _consume_nonces(str(link), ("nonce-a",), 100)
        assert target.read_bytes() == b""
    finally:
        os.chmod(replay_dir, 0o700)


def test_nonce_store_parent_blocks_replacement_during_lock(tmp_path, monkeypatch):
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir(mode=0o700)
    store = replay_dir / "consumed"
    store.touch(mode=0o600)
    attacker = replay_dir / "attacker"
    attacker.write_text('{"nonce":"attacker"}\n', encoding="utf-8")
    os.chmod(replay_dir, 0o500)
    real_flock = __import__("fcntl").flock

    def attempt_swap(fd, operation):
        with pytest.raises(PermissionError):
            os.replace(attacker, store)
        return real_flock(fd, operation)

    monkeypatch.setattr(
        "hermes_multitenancy.billing_readiness.fcntl.flock",
        attempt_swap,
    )
    try:
        _consume_nonces(str(store), ("nonce-a",), 100)
        assert json.loads(store.read_text().strip())["nonce"] == "nonce-a"
    finally:
        os.chmod(replay_dir, 0o700)


def test_live_recheck_executes_the_opened_inode_during_path_swap(
    tmp_path, monkeypatch
):
    cli_dir = tmp_path / "cli"
    cli_dir.mkdir(mode=0o755)
    cli = cli_dir / "readiness"
    cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(cli, 0o755)
    employee_input = tmp_path / "employees.json"
    employee_input.write_text("{}", encoding="utf-8")
    os.chmod(employee_input, 0o600)
    output_dir = tmp_path / "output"
    output_dir.mkdir(mode=0o700)
    env = {
        "HERMES_AI_GATEWAY_READINESS_CLI": str(cli),
        "HERMES_LITELLM_BILLING_EMPLOYEE_INPUT": str(employee_input),
        "HERMES_LITELLM_BILLING_LIVE_RECHECK_DIR": str(output_dir),
        "HERMES_LITELLM_BILLING_PAYER_IDS": "employee-a",
        "HERMES_LITELLM_BILLING_POLICY_DIGEST": "policy",
        "HERMES_LITELLM_BILLING_CODE_SHA": "code",
        "HERMES_LITELLM_BILLING_CONTRACT_MAJOR": "1",
    }
    real_fstat = os.fstat

    def root_owned(fd):
        value = real_fstat(fd)
        return SimpleNamespace(st_mode=value.st_mode, st_uid=0)

    monkeypatch.setattr(
        "hermes_multitenancy.billing_readiness.os.fstat",
        root_owned,
    )
    real_is_dir = Path.is_dir
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda self: True
        if str(self) == "/proc/self/fd"
        else real_is_dir(self),
    )

    def swap_path(argv, **kwargs):
        fd = kwargs["pass_fds"][0]
        assert argv[0] == f"/proc/self/fd/{fd}"
        assert os.pread(fd, 64, 0).startswith(b"#!/bin/sh")
        replacement = cli_dir / "replacement"
        replacement.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        os.replace(replacement, cli)
        assert os.pread(fd, 64, 0).startswith(b"#!/bin/sh\nexit 0")

    monkeypatch.setattr(
        "hermes_multitenancy.billing_readiness.subprocess.run",
        swap_path,
    )
    _run_live_recheck(
        env,
        {"routing_watermark": "routing", "org_sha": "org"},
    )
