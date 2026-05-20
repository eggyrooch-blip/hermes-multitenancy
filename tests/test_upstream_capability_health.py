from __future__ import annotations

import json
from pathlib import Path


def _write_yaml(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_upstream_capability_health_reports_secret_free_boundaries(tmp_path: Path):
    from hermes_multitenancy.upstream_health import upstream_capability_health

    shared = tmp_path / ".hermes"
    router = shared / "profiles" / "multitenancy_router"
    _write_yaml(
        shared / "config.yaml",
        """
skill_curator:
  status: PAUSED
skill_bundles:
  enabled: true
  default_bundle: org-default
provider:
  credential_pool:
    enabled: true
    api_key: should-not-leak
capability_discovery:
  browseshsource:
    enabled: false
  skills_hub:
    enabled: false
  xai_web_search:
    enabled: false
""",
    )
    _write_yaml(
        router / "config.yaml",
        """
kanban:
  dispatch_in_gateway: false
""",
    )

    report = upstream_capability_health(
        shared_home=shared,
        router_profile_home=router,
    )

    assert report["secret_free"] is True
    assert report["ready"] is True
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["lark_cli_registration"]["ok"] is True
    assert checks["lark_cli_registration"]["tool"] == "lark_cli"
    assert checks["lark_cli_registration"]["alias"] == "lark-cli"
    assert checks["cardkit_transport"]["ok"] is True
    assert checks["skill_slash_rewrite"]["ok"] is True
    assert checks["skill_curator"]["status"] == "paused"
    assert checks["skill_bundles"]["status"] == "observed"
    assert checks["credential_provider_boundary"]["owner"] == "multitenancy_vault"
    assert checks["kanban_gateway_boundary"]["ok"] is True
    assert checks["profile_capability_discovery"]["ok"] is True
    rendered = json.dumps(report, ensure_ascii=False)
    assert "should-not-leak" not in rendered
    assert "api_key" not in rendered


def test_upstream_capability_health_flags_router_kanban_dispatch(tmp_path: Path):
    from hermes_multitenancy.upstream_health import upstream_capability_health

    shared = tmp_path / ".hermes"
    router = shared / "profiles" / "multitenancy_router"
    _write_yaml(
        router / "config.yaml",
        """
kanban:
  dispatch_in_gateway: true
""",
    )

    report = upstream_capability_health(shared_home=shared, router_profile_home=router)

    checks = {check["name"]: check for check in report["checks"]}
    assert report["ready"] is False
    assert "kanban_gateway_boundary" in report["attention"]
    assert checks["kanban_gateway_boundary"]["ok"] is False
    assert checks["kanban_gateway_boundary"]["reason"] == "router profile must not run upstream Kanban dispatcher by default"


def test_upstream_capability_health_honors_curator_paused_runtime_state(tmp_path: Path):
    from hermes_multitenancy.upstream_health import upstream_capability_health

    shared = tmp_path / ".hermes"
    _write_yaml(shared / "config.yaml", "curator:\n  enabled: true\n")
    state = shared / "skills" / ".curator_state"
    state.parent.mkdir(parents=True)
    state.write_text('{"paused": true, "run_count": 0}\n', encoding="utf-8")

    report = upstream_capability_health(shared_home=shared)

    checks = {check["name"]: check for check in report["checks"]}
    assert checks["skill_curator"]["ok"] is True
    assert checks["skill_curator"]["status"] == "paused"
    assert checks["skill_curator"]["source"] == "runtime_state"


def test_lark_cli_canary_health_subcommand_outputs_report(capsys, tmp_path: Path):
    from hermes_multitenancy.lark_cli_canary import main

    shared = tmp_path / ".hermes"
    router = shared / "profiles" / "multitenancy_router"
    _write_yaml(router / "config.yaml", "kanban:\n  dispatch_in_gateway: false\n")

    code = main([
        "health",
        "--shared-home",
        str(shared),
        "--router-profile-home",
        str(router),
    ])
    output = capsys.readouterr().out

    assert code == 0
    parsed = json.loads(output)
    assert parsed["ready"] is True
    assert parsed["secret_free"] is True
    assert any(check["name"] == "skill_slash_rewrite" for check in parsed["checks"])
