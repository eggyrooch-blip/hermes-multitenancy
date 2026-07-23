"""Fail-closed verification of the AI Gateway readiness artifact."""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import subprocess
import time
from typing import Any, Mapping


class BillingReadinessError(RuntimeError):
    pass


_SIGNING_DOMAIN = b"hermes-billing-readiness-artifact:v1"
_ALLOWED_STATUSES = frozenset({
    "READY_EXISTING",
    "ADOPTABLE_EMAIL_ONLY",
    "MISSING_ACCOUNT",
    "NEEDS_TEAM",
    "NEEDS_MEMBERSHIP",
    "NEEDS_HERMES_KEY",
    "DRIFT_OR_CONFLICT",
})
_LIVE_RECHECK_ENV = frozenset({
    "HERMES_AI_GATEWAY_BROKER_TOKEN",
    "LITELLM_BASE_URL",
    "LITELLM_MASTER_KEY",
    "PATH",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
})
_REQUIRED_FIELDS = frozenset({
    "version",
    "nonce",
    "issued_at",
    "expires_at",
    "input_digest",
    "inventory_digest",
    "cohort_hash",
    "policy_digest",
    "code_sha",
    "contract_major",
    "routing_watermark",
    "org_sha",
    "counts",
    "employee_count",
    "cohort_counts",
    "cohort_count",
    "signature",
})


def _canonical_payload(artifact: Mapping[str, Any]) -> bytes:
    payload = {key: value for key, value in artifact.items() if key != "signature"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _cohort_ids(raw: str) -> list[str]:
    selected = [item.strip() for item in raw.split(",") if item.strip()]
    folded = [item.casefold() for item in selected]
    if (
        not selected
        or "*" in selected
        or len(folded) != len(set(folded))
        or any(len(item) > 128 or any(char.isspace() for char in item) for item in selected)
    ):
        raise BillingReadinessError("billing_canary_cohort_invalid")
    return sorted(selected)


def cohort_hash(raw: str) -> str:
    payload = json.dumps(_cohort_ids(raw), separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _signature(secret: str, artifact: Mapping[str, Any]) -> str:
    signing_key = hmac.new(secret.encode(), _SIGNING_DOMAIN, hashlib.sha256).digest()
    return hmac.new(signing_key, _canonical_payload(artifact), hashlib.sha256).hexdigest()


def _coverage_is_exact(counts: Any, total: int) -> bool:
    if not isinstance(counts, Mapping) or not set(counts).issubset(_ALLOWED_STATUSES):
        return False
    try:
        values = [int(value) for value in counts.values()]
    except (TypeError, ValueError):
        return False
    return all(value >= 0 for value in values) and sum(values) == total


def verify_artifact(
    artifact: Mapping[str, Any],
    *,
    secret: str,
    now: int | None = None,
    expected_cohort_hash: str = "",
    expected_policy_digest: str = "",
    expected_code_sha: str = "",
    expected_contract_major: str = "",
    expected_routing_watermark: str = "",
    expected_org_sha: str = "",
    expected_input_digest: str = "",
    expected_employee_count: int = 0,
) -> None:
    if _REQUIRED_FIELDS - set(artifact):
        raise BillingReadinessError("readiness_artifact_schema_invalid")
    if not all(
        str(value).strip()
        for value in (
            secret,
            expected_cohort_hash,
            expected_policy_digest,
            expected_code_sha,
            expected_contract_major,
            expected_routing_watermark,
            expected_org_sha,
            expected_input_digest,
            artifact.get("input_digest"),
            artifact.get("inventory_digest"),
        )
    ):
        raise BillingReadinessError("readiness_artifact_binding_missing")
    if int(artifact.get("version") or 0) != 1:
        raise BillingReadinessError("readiness_artifact_version_invalid")
    issued = int(artifact.get("issued_at") or 0)
    expires = int(artifact.get("expires_at") or 0)
    current = int(time.time()) if now is None else int(now)
    if issued <= 0 or expires <= issued or expires - issued > 1800 or current < issued or current >= expires:
        raise BillingReadinessError("readiness_artifact_expired")
    signature = str(artifact.get("signature") or "")
    expected = _signature(secret, artifact)
    if not signature or not hmac.compare_digest(signature, expected):
        raise BillingReadinessError("readiness_artifact_signature_invalid")
    for field, expected_value in (
        ("cohort_hash", expected_cohort_hash),
        ("policy_digest", expected_policy_digest),
        ("code_sha", expected_code_sha),
        ("contract_major", expected_contract_major),
        ("routing_watermark", expected_routing_watermark),
        ("org_sha", expected_org_sha),
        ("input_digest", expected_input_digest),
    ):
        if str(artifact.get(field) or "") != expected_value:
            raise BillingReadinessError(f"readiness_artifact_{field}_drift")
    employee_count = int(artifact.get("employee_count") or 0)
    counts = artifact.get("counts")
    if (
        employee_count <= 0
        or employee_count != expected_employee_count
        or not _coverage_is_exact(counts, employee_count)
    ):
        raise BillingReadinessError("readiness_artifact_coverage_invalid")
    cohort_count = int(artifact.get("cohort_count") or 0)
    cohort_counts = artifact.get("cohort_counts")
    if (
        cohort_count <= 0
        or not _coverage_is_exact(cohort_counts, cohort_count)
    ):
        raise BillingReadinessError("readiness_artifact_cohort_coverage_invalid")
    if int(cohort_counts.get("READY_EXISTING") or 0) != cohort_count:
        raise BillingReadinessError("readiness_artifact_cohort_not_ready")
    nonce = str(artifact.get("nonce") or "")
    if not nonce:
        raise BillingReadinessError("readiness_artifact_nonce_missing")


def _read_artifact(path: str) -> dict[str, Any]:
    try:
        artifact_path = Path(path)
        if artifact_path.is_symlink() or artifact_path.stat().st_mode & 0o077:
            raise BillingReadinessError("readiness_artifact_permissions_invalid")
        value = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise BillingReadinessError("readiness_artifact_unreadable") from exc
    if not isinstance(value, dict):
        raise BillingReadinessError("readiness_artifact_schema_invalid")
    return value


def _consume_nonces(path: str, nonces: tuple[str, ...], now: int) -> None:
    replay_path = Path(path)
    parent_fd = store_fd = None
    try:
        parent_fd = os.open(
            replay_path.parent,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        parent_stat = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_mode & 0o222:
            raise BillingReadinessError(
                "readiness_replay_store_permissions_invalid"
            )
        store_fd = os.open(
            replay_path.name,
            os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        store_stat = os.fstat(store_fd)
        if (
            not stat.S_ISREG(store_stat.st_mode)
            or store_stat.st_uid != os.geteuid()
            or store_stat.st_mode & 0o077
        ):
            raise BillingReadinessError(
                "readiness_replay_store_permissions_invalid"
            )
        with os.fdopen(store_fd, "r+", encoding="utf-8") as handle:
            store_fd = None
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            content = handle.read(4 * 1024 * 1024 + 1)
            if len(content) > 4 * 1024 * 1024:
                raise BillingReadinessError("readiness_replay_store_unavailable")
            consumed = {
                str(json.loads(line)["nonce"])
                for line in content.splitlines()
                if line.strip()
            }
            if consumed.intersection(nonces):
                raise BillingReadinessError("readiness_artifact_already_consumed")
            handle.seek(0, os.SEEK_END)
            for nonce in nonces:
                handle.write(json.dumps(
                    {"nonce": nonce, "consumed_at": now},
                    sort_keys=True,
                    separators=(",", ":"),
                ) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BillingReadinessError:
        raise
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BillingReadinessError("readiness_replay_store_unavailable") from exc
    finally:
        if store_fd is not None:
            os.close(store_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _sha256_json(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _local_bindings(env: Mapping[str, str]) -> dict[str, Any]:
    input_path = Path(
        str(env.get("HERMES_LITELLM_BILLING_EMPLOYEE_INPUT", "")).strip()
    )
    db_path = Path(str(env.get("HERMES_MULTITENANCY_DB", "")).strip())
    org_dir = Path(str(env.get("HERMES_ORG_SNAPSHOT_DIR", "")).strip())
    if (
        input_path.is_symlink()
        or not input_path.is_file()
        or input_path.stat().st_mode & 0o077
        or db_path.is_symlink()
        or not db_path.is_file()
        or org_dir.is_symlink()
        or not org_dir.is_dir()
    ):
        raise BillingReadinessError("readiness_local_universe_unavailable")
    try:
        raw_input = json.loads(input_path.read_text(encoding="utf-8"))
        rows = raw_input.get("employees") if isinstance(raw_input, Mapping) else raw_input
        if not isinstance(rows, list) or not rows:
            raise BillingReadinessError("readiness_employee_input_invalid")
        employees: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_emails: set[str] = set()
        for row in rows:
            item = dict(row) if isinstance(row, Mapping) else {}
            employee_id = str(
                item.get("employee_id") or item.get("user_id") or ""
            ).strip()
            email = str(
                item.get("email") or item.get("enterprise_email") or ""
            ).strip().casefold()
            if (
                not employee_id
                or not email
                or employee_id.casefold() in seen_ids
                or email in seen_emails
            ):
                raise BillingReadinessError("readiness_employee_input_invalid")
            seen_ids.add(employee_id.casefold())
            seen_emails.add(email)
            employees.append({
                "employee_id": employee_id,
                "email": email,
                "cohort": bool(item.get("cohort")),
            })
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            user_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT user_id, profile_name, open_id, union_id, version, updated_at "
                    "FROM multitenancy_routing WHERE active = 1 AND kind = 'user' "
                    "AND provenance = 'sync' ORDER BY user_id"
                ).fetchall()
            ]
            group_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT user_id, profile_name, chat_id, owner_open_id, "
                    "version, updated_at FROM multitenancy_routing "
                    "WHERE active = 1 AND kind = 'group' ORDER BY chat_id"
                ).fetchall()
            ]
        if not user_rows or any(
            not row["user_id"] or not row["profile_name"] or not row["open_id"]
            for row in user_rows
        ):
            raise BillingReadinessError("readiness_routing_universe_invalid")
        root_open_ids = {str(row["open_id"]) for row in user_rows}
        if any(
            not row["chat_id"]
            or not row["profile_name"]
            or str(row["owner_open_id"] or "") not in root_open_ids
            for row in group_rows
        ):
            raise BillingReadinessError("readiness_group_owner_invalid")
        org_path = max(
            org_dir.glob("org-*.json"),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )
        if org_path.is_symlink() or not org_path.is_file():
            raise BillingReadinessError("readiness_org_snapshot_invalid")
        org_bytes = org_path.read_bytes()
        org = json.loads(org_bytes)
        org_rows = org.get("employees") if isinstance(org, Mapping) else None
        if not isinstance(org_rows, Mapping) or not org_rows:
            raise BillingReadinessError("readiness_org_snapshot_invalid")
        domain = str(
            env.get("HERMES_LITELLM_EMPLOYEE_EMAIL_DOMAIN", "keep.com")
        ).strip().casefold()
        if not domain or "@" in domain or "/" in domain:
            raise BillingReadinessError("readiness_employee_email_domain_invalid")
        org_employees = {
            str(value.get("user_id") or key).strip(): str(
                value.get("enterprise_email")
                or value.get("email")
                or f"{value.get('user_id') or key}@{domain}"
            ).strip().casefold()
            for key, value in org_rows.items()
            if isinstance(value, Mapping) and str(value.get("user_id") or key).strip()
        }
    except BillingReadinessError:
        raise
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        raise BillingReadinessError("readiness_local_universe_unavailable") from exc
    employee_ids = {row["employee_id"] for row in employees}
    route_ids = {str(row["user_id"]) for row in user_rows}
    employee_emails = {
        row["employee_id"]: row["email"] for row in employees
    }
    cohort_ids = set(_cohort_ids(
        str(env.get("HERMES_LITELLM_BILLING_PAYER_IDS", ""))
    ))
    input_cohort = {
        row["employee_id"] for row in employees if bool(row["cohort"])
    }
    if (
        len(org_employees) != len(org_rows)
        or employee_ids != route_ids
        or employee_ids != set(org_employees)
        or employee_emails != org_employees
        or cohort_ids != input_cohort
    ):
        raise BillingReadinessError("readiness_local_universe_drift")
    return {
        "input_digest": _sha256_json(employees),
        "employee_count": len(employees),
        "routing_watermark": _sha256_json({
            "groups": group_rows,
            "users": user_rows,
        }),
        "org_sha": hashlib.sha256(org_bytes).hexdigest(),
    }


def _run_live_recheck(
    env: Mapping[str, str],
    bindings: Mapping[str, Any],
) -> tuple[int, Path, str]:
    cli = Path(str(env.get("HERMES_AI_GATEWAY_READINESS_CLI", "")).strip())
    input_path = Path(
        str(env.get("HERMES_LITELLM_BILLING_EMPLOYEE_INPUT", "")).strip()
    )
    output_dir = Path(
        str(env.get("HERMES_LITELLM_BILLING_LIVE_RECHECK_DIR", "")).strip()
    )
    if (
        not cli.is_absolute()
        or input_path.is_symlink()
        or not input_path.is_file()
        or input_path.stat().st_mode & 0o077
        or output_dir.is_symlink()
        or not output_dir.is_dir()
        or output_dir.stat().st_mode & 0o077
    ):
        raise BillingReadinessError("readiness_live_recheck_command_invalid")
    challenge = secrets.token_hex(16)
    output_path = output_dir / f"readiness-{challenge}.json"
    if output_path.exists():
        raise BillingReadinessError("readiness_live_recheck_output_exists")
    expected_cohort = cohort_hash(
        str(env.get("HERMES_LITELLM_BILLING_PAYER_IDS", ""))
    )
    cli_fd = _open_trusted_cli(cli)
    try:
        argv = [
            f"/proc/self/fd/{cli_fd}",
            "broker-readiness-snapshot",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--cohort-hash",
            expected_cohort,
            "--policy-digest",
            str(env.get("HERMES_LITELLM_BILLING_POLICY_DIGEST", "")).strip(),
            "--code-sha",
            str(env.get("HERMES_LITELLM_BILLING_CODE_SHA", "")).strip(),
            "--nonce",
            challenge,
            "--contract-major",
            str(env.get("HERMES_LITELLM_BILLING_CONTRACT_MAJOR", "")).strip(),
            "--routing-watermark",
            str(bindings["routing_watermark"]),
            "--org-sha",
            str(bindings["org_sha"]),
        ]
        started = int(time.time())
        try:
            subprocess.run(
                argv,
                check=True,
                timeout=120,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={
                    key: value
                    for key, value in env.items()
                    if key in _LIVE_RECHECK_ENV
                },
                pass_fds=(cli_fd,),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BillingReadinessError("readiness_live_recheck_failed") from exc
        return started, output_path, challenge
    finally:
        os.close(cli_fd)


def _open_trusted_cli(cli: Path) -> int:
    """Pin one root-owned CLI after validating every directory in its path."""
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or not Path("/proc/self/fd").is_dir()
    ):
        raise BillingReadinessError("readiness_live_recheck_command_invalid")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current_fd: int | None = None
    try:
        before = os.lstat("/")
        current_fd = os.open("/", flags)
        after = os.fstat(current_fd)
        _verify_trusted_path_part(before, after, directory=True)
        for part in cli.parent.parts[1:]:
            before = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
            next_fd = os.open(part, flags, dir_fd=current_fd)
            try:
                after = os.fstat(next_fd)
                _verify_trusted_path_part(before, after, directory=True)
            except Exception:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd

        before = os.stat(cli.name, dir_fd=current_fd, follow_symlinks=False)
        cli_fd = os.open(cli.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current_fd)
        try:
            after = os.fstat(cli_fd)
            _verify_trusted_path_part(before, after, directory=False)
        except Exception:
            os.close(cli_fd)
            raise
        return cli_fd
    except (OSError, BillingReadinessError) as exc:
        if isinstance(exc, BillingReadinessError):
            raise
        raise BillingReadinessError(
            "readiness_live_recheck_command_invalid"
        ) from exc
    finally:
        if current_fd is not None:
            os.close(current_fd)


def _verify_trusted_path_part(
    before: os.stat_result,
    after: os.stat_result,
    *,
    directory: bool,
) -> None:
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or not expected_type(after.st_mode)
        or after.st_uid != 0
        or after.st_mode & 0o022
        or (not directory and not after.st_mode & 0o111)
    ):
        raise BillingReadinessError("readiness_live_recheck_command_invalid")


def verify_enabled_environment(env: Mapping[str, str] | None = None) -> None:
    env = os.environ if env is None else env
    enabled = str(env.get("HERMES_LITELLM_BILLING_ENABLED", "")).strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return
    path = str(env.get("HERMES_LITELLM_BILLING_READINESS_ARTIFACT", "")).strip()
    replay_path = str(
        env.get("HERMES_LITELLM_BILLING_REPLAY_STORE", "")
    ).strip()
    secret = str(env.get("HERMES_AI_GATEWAY_BROKER_TOKEN", "")).strip()
    policy_digest = str(
        env.get("HERMES_LITELLM_BILLING_POLICY_DIGEST", "")
    ).strip()
    code_sha = str(env.get("HERMES_LITELLM_BILLING_CODE_SHA", "")).strip()
    contract_major = str(
        env.get("HERMES_LITELLM_BILLING_CONTRACT_MAJOR", "")
    ).strip()
    routing_watermark = str(
        env.get("HERMES_LITELLM_BILLING_ROUTING_WATERMARK", "")
    ).strip()
    org_sha = str(env.get("HERMES_LITELLM_BILLING_ORG_SHA", "")).strip()
    expected_cohort = cohort_hash(
        str(env.get("HERMES_LITELLM_BILLING_PAYER_IDS", ""))
    )
    bindings = _local_bindings(env)
    if (
        routing_watermark != bindings["routing_watermark"]
        or org_sha != bindings["org_sha"]
    ):
        raise BillingReadinessError("readiness_local_binding_drift")
    if not all((
        path,
        replay_path,
        secret,
        policy_digest,
        code_sha,
        contract_major,
        routing_watermark,
        org_sha,
    )):
        raise BillingReadinessError("readiness_artifact_missing")
    first = _read_artifact(path)
    live_started, live_path, challenge = _run_live_recheck(env, bindings)
    if _local_bindings(env) != bindings:
        raise BillingReadinessError("readiness_local_universe_drift")
    live = _read_artifact(str(live_path))
    artifacts = (first, live)
    for artifact in artifacts:
        verify_artifact(
            artifact,
            secret=secret,
            expected_cohort_hash=expected_cohort,
            expected_policy_digest=policy_digest,
            expected_code_sha=code_sha,
            expected_contract_major=contract_major,
            expected_routing_watermark=routing_watermark,
            expected_org_sha=org_sha,
            expected_input_digest=str(bindings["input_digest"]),
            expected_employee_count=int(bindings["employee_count"]),
        )
    for field in (
        "input_digest",
        "inventory_digest",
        "cohort_hash",
        "policy_digest",
        "code_sha",
        "contract_major",
        "routing_watermark",
        "org_sha",
    ):
        if first.get(field) != live.get(field):
            raise BillingReadinessError("readiness_enable_recheck_drift")
    first_nonce = str(first["nonce"])
    live_nonce = str(live["nonce"])
    if (
        first_nonce == live_nonce
        or live_nonce != challenge
        or int(live["issued_at"]) < int(first["issued_at"])
        or int(live["issued_at"]) < live_started
    ):
        raise BillingReadinessError("readiness_enable_recheck_invalid")
    _consume_nonces(replay_path, (first_nonce, live_nonce), int(time.time()))
