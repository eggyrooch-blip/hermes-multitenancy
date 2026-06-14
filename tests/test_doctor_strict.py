from __future__ import annotations

from pathlib import Path

import pytest


def _active_route_db(path: Path) -> None:
    from hermes_multitenancy.routing import RoutingTable

    table = RoutingTable(path)
    try:
        table.upsert(
            user_id="user-1",
            profile_name="alice",
            open_id="ou_active_user",
            union_id="on_active_user",
            provenance="sync",
        )
    finally:
        table.close()
    path.chmod(0o600)


def _configured_home(tmp_path: Path) -> tuple[Path, Path]:
    shared_home = tmp_path / ".hermes"
    profiles = shared_home / "profiles"
    profiles.mkdir(parents=True, mode=0o700)
    profiles.chmod(0o700)
    db_path = shared_home / "multitenancy.db"
    _active_route_db(db_path)
    return shared_home, db_path


@pytest.fixture(autouse=True)
def _strict_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-vault-key")
    monkeypatch.delenv("HERMES_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("HERMES_MULTITENANCY_AUTO_PROVISION", raising=False)
    monkeypatch.delenv("HERMES_MULTITENANCY_DEV_MODE", raising=False)
    monkeypatch.delenv("HERMES_MULTITENANCY_REQUIRE_SANDBOX", raising=False)


def _checks(report: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(row["name"]): row for row in report["checks"]}  # type: ignore[index]


def test_build_report_passes_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_multitenancy import doctor
    from hermes_multitenancy.doctor import build_report

    shared_home, db_path = _configured_home(tmp_path)

    # A genuinely locked-down strict deployment: sandbox enforced + available
    # and the trusted authsidecar binary present. (Monkeypatched because the
    # real sandbox policy file / Go sidecar aren't in the unit-test sandbox.)
    monkeypatch.setenv("HERMES_MULTITENANCY_REQUIRE_SANDBOX", "1")
    monkeypatch.setattr(
        doctor,
        "_required_sandbox_check",
        lambda: {"name": "required_sandbox", "ok": True, "status": "available"},
    )
    sidecar = tmp_path / "lark-cli-authsidecar"
    sidecar.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(sidecar))

    report = build_report(shared_home=shared_home, db_path=db_path, strict=True)

    assert report["ok"] is True
    assert report["failed_required"] == []
    checks = _checks(report)
    assert checks["strict_context"]["ok"] is True
    assert checks["auto_provision_disabled"]["ok"] is True
    assert checks["route_table_active_users"]["ok"] is True
    assert checks["sandbox_required_available"]["ok"] is True
    assert checks["lark_cli_authsidecar"]["ok"] is True


def test_strict_fails_when_sandbox_not_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-review gap A: under strict context, REQUIRE_SANDBOX off is a fail-open
    hole the gate must catch (upstream returns 'skipped'/ok otherwise)."""
    from hermes_multitenancy.doctor import build_report

    shared_home, db_path = _configured_home(tmp_path)
    monkeypatch.delenv("HERMES_MULTITENANCY_REQUIRE_SANDBOX", raising=False)

    report = build_report(shared_home=shared_home, db_path=db_path, strict=True)

    row = _checks(report)["sandbox_required_available"]
    assert row["ok"] is False
    assert "sandbox_required_available" in report["failed_required"]


def test_strict_fails_when_authsidecar_binary_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-review gap B: tool-schema registration is not enough — the actual
    authsidecar binary must resolve, else the check is a false PASS."""
    from hermes_multitenancy.doctor import build_report

    shared_home, db_path = _configured_home(tmp_path)
    # No HERMES_LARK_CLI_BIN, and HERMES_BASE_HOME points at a dir with no
    # bin/lark-cli-authsidecar → resolver returns None (a generic lark-cli on
    # PATH must NOT satisfy the gate).
    monkeypatch.delenv("HERMES_LARK_CLI_BIN", raising=False)
    monkeypatch.setenv("HERMES_BASE_HOME", str(tmp_path / "empty-home"))

    report = build_report(shared_home=shared_home, db_path=db_path, strict=True)

    row = _checks(report)["lark_cli_authsidecar"]
    assert row["ok"] is False
    assert row["status"] == "binary_missing"
    assert "lark_cli_authsidecar" in report["failed_required"]


def test_strict_authsidecar_rejects_generic_lark_cli_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-review (round 3) gap: a generic `lark-cli` on PATH (e.g. npm package)
    must NOT satisfy the authsidecar gate — only the trusted Go sidecar does."""
    from hermes_multitenancy import doctor

    monkeypatch.delenv("HERMES_LARK_CLI_BIN", raising=False)
    monkeypatch.setenv("HERMES_BASE_HOME", str(tmp_path / "empty-home"))
    # Even if a generic lark-cli is on PATH, the strict resolver ignores it.
    assert doctor._resolve_authsidecar_binary() is None


def test_authsidecar_resolved_via_doctor_shared_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-review (round 4) gap: the resolver must honor the doctor's --shared-home,
    not just HERMES_BASE_HOME/~/.hermes."""
    from hermes_multitenancy import doctor

    monkeypatch.delenv("HERMES_LARK_CLI_BIN", raising=False)
    monkeypatch.setenv("HERMES_BASE_HOME", str(tmp_path / "elsewhere"))
    shared_home = tmp_path / "shared"
    (shared_home / "bin").mkdir(parents=True)
    sidecar = shared_home / "bin" / "lark-cli-authsidecar"
    sidecar.write_text("#!/bin/sh\n")

    assert doctor._resolve_authsidecar_binary(shared_home) == str(sidecar)
    row = doctor._lark_cli_authsidecar_check(shared_home=shared_home)
    assert row["ok"] is True
    assert row["binary"] == str(sidecar)


def test_non_strict_preserves_upstream_health_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-review (round 4) gap / SPEC regression guard: non-strict build_report
    must NOT flip ok=false from strict gate rows — it preserves upstream health."""
    from hermes_multitenancy.doctor import build_report

    shared_home, db_path = _configured_home(tmp_path)
    monkeypatch.delenv("HERMES_MULTITENANCY_STRICT_CONTEXT", raising=False)

    report = build_report(shared_home=shared_home, db_path=db_path, strict=False)

    # Strict rows still present (informational) but they must NOT drive ok.
    assert report["failed_required"] == []
    assert report["ok"] == bool(report["upstream"].get("ready", True))
    # Sanity: a strict run on the same posture WOULD fail (proves the rows exist).
    strict_report = build_report(shared_home=shared_home, db_path=db_path, strict=True)
    assert strict_report["failed_required"]  # strict still gates


def test_strict_route_table_rejects_auto_provisioned_only(
    tmp_path: Path,
) -> None:
    """Re-review (round 3) gap: SPEC says 'active SYNCED users' — an active
    auto-provisioned row must NOT satisfy the gate."""
    from hermes_multitenancy.doctor import _route_table_active_users_check
    from hermes_multitenancy.routing import RoutingTable

    db_path = tmp_path / "auto.db"
    table = RoutingTable(db_path)
    try:
        # Router auto-provision writes user_id == open_id; the routing migration
        # derives provenance='auto' from exactly that identity equality (the
        # passed provenance is normalized deterministically on open).
        table.upsert(
            user_id="ou_auto",
            profile_name="auto_profile",
            open_id="ou_auto",
        )
    finally:
        table.close()
    db_path.chmod(0o600)

    row = _route_table_active_users_check(db_path=db_path)
    assert row["ok"] is False  # auto-provisioned (user_id==open_id) isn't synced
    assert row["active_users"] == 0


def test_strict_exits_nonzero_when_required_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_multitenancy.doctor import build_report, main

    shared_home, db_path = _configured_home(tmp_path)
    monkeypatch.setenv("HERMES_MULTITENANCY_AUTO_PROVISION", "1")

    report = build_report(shared_home=shared_home, db_path=db_path, strict=True)

    assert report["ok"] is False
    assert "auto_provision_disabled" in report["failed_required"]
    assert main(["--strict", "--shared-home", str(shared_home), "--db-path", str(db_path)]) == 1


def test_child_env_no_vault_key_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.doctor import build_report

    shared_home, db_path = _configured_home(tmp_path)
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "dummy-new-key")
    monkeypatch.setenv("HERMES_CREDENTIAL_KEY", "dummy-legacy-key")

    report = build_report(shared_home=shared_home, db_path=db_path, strict=True)

    row = _checks(report)["child_env_no_vault_key"]
    assert row["ok"] is True

    profile_home = tmp_path / "profile"
    approval_dir = tmp_path / "approval"
    env = agent_real._build_subprocess_env(profile_home, approval_dir=approval_dir)
    assert "HERMES_MULTITENANCY_CREDENTIAL_KEY" not in env
    assert "HERMES_CREDENTIAL_KEY" not in env


def test_deleted_user_cannot_route_check(tmp_path: Path) -> None:
    from hermes_multitenancy.doctor import _deleted_user_cannot_route_check

    row = _deleted_user_cannot_route_check()

    assert row["ok"] is True
    assert row["status"] == "fail_closed"


def test_non_strict_returns_zero_even_with_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_multitenancy.doctor import main

    shared_home, db_path = _configured_home(tmp_path)
    monkeypatch.delenv("HERMES_MULTITENANCY_STRICT_CONTEXT", raising=False)

    assert main(["--shared-home", str(shared_home), "--db-path", str(db_path)]) == 0
