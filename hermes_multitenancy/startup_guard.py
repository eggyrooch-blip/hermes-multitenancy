"""Production startup checks owned by the multitenancy plugin."""

from __future__ import annotations

import importlib
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Mapping
from urllib.request import Request, urlopen

import yaml


class StartupGuardError(RuntimeError):
    pass


_REQUIRED_ENV = (
    "FEISHU_APP_ID",
    "HERMES_MULTITENANCY_CREDENTIAL_KEY",
    "HERMES_MULTITENANCY_RUN_BROKER_KEY",
)
_BOUNDARY_MODULES = (
    "hermes_multitenancy.router",
    "hermes_multitenancy.lark_cli_auth_broker",
    "hermes_multitenancy.lark_cli_tool",
    "hermes_multitenancy.webui_broker_server",
)


def _compile_package(package_dir: Path) -> None:
    for source in sorted(package_dir.rglob("*.py")):
        try:
            compile(source.read_bytes(), str(source), "exec")
        except Exception as exc:
            raise StartupGuardError("plugin_source_unreadable") from exc


def _import_boundaries() -> None:
    for module in _BOUNDARY_MODULES:
        importlib.import_module(module)


def _validate_billing_cohort(env: Mapping[str, str]) -> None:
    enabled = str(env.get("HERMES_LITELLM_BILLING_ENABLED", "")).strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return
    raw = str(env.get("HERMES_LITELLM_BILLING_PAYER_IDS", "")).strip()
    selected = {item.strip() for item in raw.split(",") if item.strip()}
    if not selected or "*" in selected:
        raise StartupGuardError("billing_canary_cohort_invalid")
    # ponytail: the signed readiness-artifact ceremony (verify_enabled_environment)
    # used to gate startup here too. sunke picked option C on 2026-08-06
    # (billing-degrade-not-refuse): unavailable credentials now degrade to the
    # shared key + an alert instead of refusing service, so the ceremony this
    # startup gate existed to protect against has no failure mode left to
    # prevent. Ceremony code lives on in billing_readiness.py for shadow/CLI
    # use; it is just not wired into the startup path anymore. See
    # .ftask/billing-degrade-not-refuse/SPEC.md.


def validate_startup(
    *,
    env: Mapping[str, str] | None = None,
    package_dir: Path | None = None,
    profile_home: Path | None = None,
) -> None:
    env = os.environ if env is None else env
    home_raw = str(profile_home or env.get("HERMES_HOME", "")).strip()
    if not home_raw:
        raise StartupGuardError("profile_home_missing")

    config = yaml.safe_load((Path(home_raw).expanduser() / "config.yaml").read_text()) or {}
    enabled = (config.get("plugins") or {}).get("enabled") if isinstance(config, dict) else None
    if not isinstance(enabled, list) or "multitenancy" not in enabled:
        raise StartupGuardError("plugin_not_enabled")
    if str(env.get("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "")).strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise StartupGuardError("run_broker_disabled")
    if any(not str(env.get(name, "")).strip() for name in _REQUIRED_ENV):
        raise StartupGuardError("isolation_environment_missing")
    _validate_billing_cohort(env)

    package_dir = package_dir or Path(__file__).resolve().parent
    _compile_package(package_dir)
    try:
        compatibility = importlib.import_module("hermes_cli.plugin_compat")
    except ModuleNotFoundError as exc:
        if exc.name != "hermes_cli.plugin_compat":
            raise
    else:
        if compatibility.scan_plugin(package_dir.parent):
            raise StartupGuardError("plugin_removed_core_imports")
    _import_boundaries()


RUN_BROKER_WAIT_ENV = "HERMES_MULTITENANCY_RUN_BROKER_WAIT_SECONDS"
DEFAULT_RUN_BROKER_WAIT_SECONDS = 20.0
MAX_RUN_BROKER_WAIT_SECONDS = 3600.0


def run_broker_wait_seconds(env: Mapping[str, str] | None = None) -> float:
    """How long ``wait-broker`` waits; the unit sets it for slow production disks.

    Unset/empty keeps the 20s default. Anything that is not a finite number in
    (0, 3600] is a configuration error, not a silent fallback.
    """
    env = os.environ if env is None else env
    raw = str(env.get(RUN_BROKER_WAIT_ENV, "")).strip()
    if not raw:
        return DEFAULT_RUN_BROKER_WAIT_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise StartupGuardError("run_broker_wait_invalid") from exc
    if not math.isfinite(value) or not 0 < value <= MAX_RUN_BROKER_WAIT_SECONDS:
        raise StartupGuardError("run_broker_wait_invalid")
    return value


def wait_run_broker(
    *,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float | None = None,
) -> None:
    env = os.environ if env is None else env
    if timeout_seconds is None:
        timeout_seconds = run_broker_wait_seconds(env)
    key = str(env.get("HERMES_MULTITENANCY_RUN_BROKER_KEY", "")).strip()
    host = str(env.get("HERMES_MULTITENANCY_RUN_BROKER_HOST", "127.0.0.1")).strip() or "127.0.0.1"
    try:
        port = int(str(env.get("HERMES_MULTITENANCY_RUN_BROKER_PORT", "8766")))
    except ValueError as exc:
        raise StartupGuardError("run_broker_port_invalid") from exc
    if not key or host not in {"127.0.0.1", "::1", "localhost"} or not 1 <= port <= 65535:
        raise StartupGuardError("run_broker_boundary_invalid")

    deadline = time.monotonic() + timeout_seconds
    request = Request(
        f"http://{host}:{port}/api/run-broker/health",
        headers={"Authorization": f"Bearer {key}"},
    )
    while True:
        try:
            with urlopen(request, timeout=1.0) as response:
                payload = json.loads(response.read())
            if payload.get("ok") is True and payload.get("service") == "hermes-multitenancy-run-broker":
                return
        except Exception:
            pass
        if time.monotonic() >= deadline:
            raise StartupGuardError("run_broker_unhealthy")
        time.sleep(0.2)


def run_gateway() -> None:
    """Refuse to connect unless this process installed the tenant dispatch hook."""
    validate_startup()
    from hermes_cli.plugins import discover_plugins, get_plugin_manager

    discover_plugins()
    manager = get_plugin_manager()
    plugin = manager._plugins.get("multitenancy")
    if plugin is None or not plugin.enabled or plugin.error:
        raise StartupGuardError("plugin_not_loaded")
    package = importlib.import_module(plugin.module.register.__module__)
    dispatch = getattr(package, "_dispatch_with_worker_init", None)
    if dispatch is None or dispatch not in manager.iter_hook_callbacks("pre_gateway_dispatch"):
        raise StartupGuardError("tenant_dispatch_hook_missing")
    wait_run_broker()
    from hermes_cli.main import main as core_main

    sys.argv = [sys.argv[0], "gateway", "run"]
    core_main()


def main(argv: list[str] | None = None) -> int:
    command = (argv or sys.argv[1:] or ["preflight"])[0]
    try:
        if command == "gateway":
            run_gateway()
        elif command == "preflight":
            validate_startup()
        elif command == "wait-broker":
            wait_run_broker()
        else:
            raise StartupGuardError("unknown_command")
    except BaseException as exc:
        print(f"multitenancy startup guard failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"multitenancy startup guard passed: {command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
