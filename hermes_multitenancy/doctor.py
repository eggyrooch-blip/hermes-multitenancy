"""Strict startup doctor for the multitenancy plugin."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

import yaml

from . import runtime
from .routing import DEFAULT_DB_PATH, RoutingTable
from .upstream_health import (
    _lark_cli_registration_check,
    _required_sandbox_check,
    upstream_capability_health,
)


# Required strict gate rows. Under --strict, any false row in this set returns
# exit 1 so startup scripts can fail closed before admitting gateway traffic.
REQUIRED_CHECK_NAMES = frozenset(
    {
        "plugin_enabled",
        "pre_gateway_dispatch_hook",
        "strict_context",
        "auto_provision_disabled",
        "sandbox_required_available",
        "credential_broker_available",
        "child_env_no_vault_key",
        "profile_dirs_0700",
        "db_not_world_readable",
        "lark_cli_authsidecar",
        "route_table_active_users",
        "deleted_user_cannot_route",
    }
)


def _row(name: str, ok: bool, status: str, **fields: Any) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "status": status, **fields}


def _unavailable(name: str, exc: Exception) -> dict[str, Any]:
    return _row(name, False, "unavailable", reason=exc.__class__.__name__)


def _guarded(name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return fn()
    except Exception as exc:
        return _unavailable(name, exc)


def _plugin_enabled_check() -> dict[str, Any]:
    import hermes_multitenancy

    register = getattr(hermes_multitenancy, "register", None)
    return _row(
        "plugin_enabled",
        callable(register),
        "available" if callable(register) else "missing_register",
    )


def _pre_gateway_dispatch_hook_check() -> dict[str, Any]:
    manifest_path = Path(__file__).with_name("plugin.yaml")
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        hooks = manifest.get("provides_hooks") or []
        ok = "pre_gateway_dispatch" in hooks
        return _row(
            "pre_gateway_dispatch_hook",
            ok,
            "declared" if ok else "missing",
            source="plugin.yaml",
        )
    except Exception:
        from .router import on_pre_gateway_dispatch

        ok = callable(on_pre_gateway_dispatch)
        return _row(
            "pre_gateway_dispatch_hook",
            ok,
            "callable" if ok else "missing",
            source="router",
        )


def _strict_context_check() -> dict[str, Any]:
    enabled = runtime.strict_context_enabled()
    return _row(
        "strict_context",
        enabled,
        "enabled" if enabled else "disabled",
        reason="HERMES_MULTITENANCY_STRICT_CONTEXT is off" if not enabled else "strict context enabled",
    )


def _auto_provision_disabled_check() -> dict[str, Any]:
    from . import router

    enabled = router._auto_provision_enabled()
    return _row(
        "auto_provision_disabled",
        not enabled,
        "disabled" if not enabled else "enabled",
        reason="auto provisioning must be disabled under strict context"
        if enabled
        else "auto provisioning disabled",
    )


def _sandbox_required_available_check() -> dict[str, Any]:
    check = dict(_required_sandbox_check())
    check["name"] = "sandbox_required_available"
    check["required_when"] = "HERMES_MULTITENANCY_REQUIRE_SANDBOX"
    return check


def _credential_broker_available_check() -> dict[str, Any]:
    from . import credential_broker

    mint_lease = getattr(credential_broker, "mint_lease", None)
    verify_lease = getattr(credential_broker, "verify_lease", None)
    lease_signing_secret = getattr(credential_broker, "lease_signing_secret", None)
    if not (callable(mint_lease) and callable(verify_lease) and callable(lease_signing_secret)):
        return _row(
            "credential_broker_available",
            False,
            "incomplete",
            reason="credential broker symbols missing",
        )
    lease_signing_secret()
    return _row(
        "credential_broker_available",
        True,
        "available",
        reason="lease signing secret derivable",
    )


def _child_env_no_vault_key_check() -> dict[str, Any]:
    from . import agent_real

    keys = ("HERMES_MULTITENANCY_CREDENTIAL_KEY", "HERMES_CREDENTIAL_KEY")
    original = {key: os.environ.get(key) for key in (*keys, "HERMES_MULTITENANCY_STRICT_CONTEXT")}
    try:
        os.environ["HERMES_MULTITENANCY_STRICT_CONTEXT"] = "1"
        os.environ["HERMES_MULTITENANCY_CREDENTIAL_KEY"] = "doctor-dummy-new-key"
        os.environ["HERMES_CREDENTIAL_KEY"] = "doctor-dummy-legacy-key"
        with tempfile.TemporaryDirectory(prefix="hermes-mt-doctor-") as raw:
            root = Path(raw)
            profile_home = root / "profiles" / "doctor"
            approval_dir = profile_home / "approvals"
            approval_dir.mkdir(parents=True, mode=0o700)
            env = agent_real._build_subprocess_env(profile_home, approval_dir=approval_dir)
        leaked = [key for key in keys if key in env]
        return _row(
            "child_env_no_vault_key",
            not leaked,
            "stripped" if not leaked else "leaked",
            reason="vault keys absent from subprocess env" if not leaked else ",".join(leaked),
        )
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _profile_dirs_0700_check(*, shared_home: Path) -> dict[str, Any]:
    profiles_dir = shared_home.expanduser() / "profiles"
    if not profiles_dir.exists():
        return _row(
            "profile_dirs_0700",
            True,
            "not_checked",
            reason="profiles directory not found",
        )
    bad: list[str] = []
    checked = 0
    for path in profiles_dir.iterdir():
        if not path.is_dir():
            continue
        checked += 1
        if path.stat().st_mode & 0o777 != 0o700:
            bad.append(path.name)
    return _row(
        "profile_dirs_0700",
        not bad,
        "checked" if not bad else "bad_mode",
        checked=checked,
        reason="profile directories are 0700" if not bad else ",".join(sorted(bad)),
    )


def _db_not_world_readable_check(*, db_path: Path) -> dict[str, Any]:
    db_path = db_path.expanduser()
    if not db_path.exists():
        return _row(
            "db_not_world_readable",
            True,
            "not_found",
            reason="routing database not found",
        )
    world_readable = bool(db_path.stat().st_mode & 0o004)
    return _row(
        "db_not_world_readable",
        not world_readable,
        "protected" if not world_readable else "world_readable",
        reason="database is not world-readable" if not world_readable else "database is world-readable",
    )


def _lark_cli_authsidecar_check() -> dict[str, Any]:
    check = dict(_lark_cli_registration_check())
    check["name"] = "lark_cli_authsidecar"
    return check


def _route_table_active_users_check(*, db_path: Path) -> dict[str, Any]:
    table = RoutingTable(db_path)
    try:
        active_users = table.count_active(kind="user")
    finally:
        table.close()
    return _row(
        "route_table_active_users",
        active_users > 0,
        "active" if active_users > 0 else "empty",
        active_users=active_users,
        reason="active user routes found" if active_users > 0 else "no active user routes",
    )


def _deleted_user_cannot_route_check() -> dict[str, Any]:
    table = RoutingTable(":memory:")
    try:
        table.upsert(
            user_id="user-deleted",
            profile_name="deleted_profile",
            open_id="ou_deleted_user",
            union_id="on_deleted_user",
            provenance="sync",
        )
        deleted = table.soft_delete("user-deleted")
        context = table.resolve_context("ou_deleted_user")
    finally:
        table.close()
    ok = deleted and context is None
    return _row(
        "deleted_user_cannot_route",
        ok,
        "fail_closed" if ok else "routable",
        reason="soft-deleted user does not resolve" if ok else "soft-deleted user resolved",
    )


def _strict_checks(*, shared_home: Path, db_path: Path) -> list[dict[str, Any]]:
    return [
        _guarded("plugin_enabled", _plugin_enabled_check),
        _guarded("pre_gateway_dispatch_hook", _pre_gateway_dispatch_hook_check),
        _guarded("strict_context", _strict_context_check),
        _guarded("auto_provision_disabled", _auto_provision_disabled_check),
        _guarded("sandbox_required_available", _sandbox_required_available_check),
        _guarded("credential_broker_available", _credential_broker_available_check),
        _guarded("child_env_no_vault_key", _child_env_no_vault_key_check),
        _guarded("profile_dirs_0700", lambda: _profile_dirs_0700_check(shared_home=shared_home)),
        _guarded("db_not_world_readable", lambda: _db_not_world_readable_check(db_path=db_path)),
        _guarded("lark_cli_authsidecar", _lark_cli_authsidecar_check),
        _guarded("route_table_active_users", lambda: _route_table_active_users_check(db_path=db_path)),
        _guarded("deleted_user_cannot_route", _deleted_user_cannot_route_check),
    ]


def build_report(*, shared_home: Path, db_path: Path, strict: bool) -> dict[str, Any]:
    shared_home = shared_home.expanduser()
    db_path = db_path.expanduser()
    upstream = upstream_capability_health(shared_home=shared_home)
    checks = _strict_checks(shared_home=shared_home, db_path=db_path)
    failed_required = [
        str(check["name"])
        for check in checks
        if check.get("name") in REQUIRED_CHECK_NAMES and not check.get("ok")
    ]
    return {
        "strict": bool(strict),
        "ok": not failed_required,
        "checks": checks,
        "upstream": upstream,
        "failed_required": failed_required,
    }


def _default_shared_home() -> Path:
    raw = os.environ.get("HERMES_SHARED_HOME") or ""
    if raw.strip():
        return Path(raw).expanduser()
    return Path.home() / ".hermes"


def _print_human(report: dict[str, Any]) -> None:
    status = "PASS" if report.get("ok") else "FAIL"
    print(f"hermes-multitenancy doctor: {status} strict={bool(report.get('strict'))}")
    failed = report.get("failed_required") or []
    if failed:
        print("failed_required: " + ", ".join(str(item) for item in failed))
    print("checks:")
    for check in report.get("checks") or []:
        mark = "PASS" if check.get("ok") else "FAIL"
        name = str(check.get("name") or "")
        row_status = str(check.get("status") or "")
        reason = str(check.get("reason") or "")
        suffix = f" - {reason}" if reason else ""
        print(f"  {mark:4} {name:32} {row_status}{suffix}")
    upstream = report.get("upstream") or {}
    upstream_status = "PASS" if upstream.get("ready") else "FAIL"
    attention = upstream.get("attention") or []
    print(f"upstream: {upstream_status}")
    if attention:
        print("upstream_attention: " + ", ".join(str(item) for item in attention))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the hermes multitenancy doctor.")
    parser.add_argument("--strict", action="store_true", help="Return 1 when a required check fails.")
    parser.add_argument("--json", action="store_true", help="Print the full report as JSON.")
    parser.add_argument("--shared-home", type=Path, default=_default_shared_home())
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

    report = build_report(shared_home=args.shared_home, db_path=args.db_path, strict=args.strict)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        _print_human(report)
    if args.strict and report["failed_required"]:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
