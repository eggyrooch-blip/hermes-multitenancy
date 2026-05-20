from __future__ import annotations

import json
from pathlib import Path


def _write_config(shared_home: Path, body: str) -> None:
    shared_home.mkdir(parents=True, exist_ok=True)
    (shared_home / "discovery-policy.yaml").write_text(body.strip() + "\n", encoding="utf-8")


def test_discovery_policy_blocks_external_sources_and_x_search_by_default(tmp_path: Path):
    from hermes_multitenancy.discovery_policy import plan_profile_discovery

    plan = plan_profile_discovery(
        shared_home=tmp_path,
        profile_name="owner",
        requested_sources=["official", "browse-sh", "skills-sh", "github", "url", "well-known"],
        requested_toolsets=["x_search"],
        xai_credentials={"available": True, "source": "env", "api_key": "xai-secret-value"},
    )

    assert plan["enabled"] is False
    assert plan["secret_free"] is True
    assert plan["sources"]["official"]["allowed"] is True
    assert plan["sources"]["official"]["mode"] == "read_only"
    for source in ["browse-sh", "skills-sh", "github", "url", "well-known"]:
        assert plan["sources"][source]["allowed"] is False
        assert "policy" in plan["sources"][source]["reason"].lower()
    assert plan["toolsets"]["x_search"]["allowed"] is False
    assert "xai-secret-value" not in json.dumps(plan)


def test_discovery_policy_allows_browse_sh_for_matching_profile_only(tmp_path: Path):
    from hermes_multitenancy.discovery_policy import plan_profile_discovery

    _write_config(
        tmp_path,
        """
        enabled: true
        sources:
          browse-sh:
            action: allow
            audience:
              profiles:
                - research_bot
        """,
    )

    allowed = plan_profile_discovery(
        shared_home=tmp_path,
        profile_name="research_bot",
        requested_sources=["browse-sh"],
    )
    blocked = plan_profile_discovery(
        shared_home=tmp_path,
        profile_name="ops_bot",
        requested_sources=["browse-sh"],
    )

    assert allowed["sources"]["browse-sh"]["allowed"] is True
    assert allowed["sources"]["browse-sh"]["requires_audit"] is True
    assert allowed["sources"]["browse-sh"]["install_requires_secret_guard"] is True
    assert blocked["sources"]["browse-sh"]["allowed"] is False
    assert "audience" in blocked["sources"]["browse-sh"]["reason"].lower()


def test_discovery_policy_allows_x_search_by_department_without_leaking_secret(tmp_path: Path):
    from hermes_multitenancy.discovery_policy import plan_profile_discovery

    _write_config(
        tmp_path,
        """
        enabled: true
        toolsets:
          x_search:
            action: allow
            audience:
              departments:
                - research
        """,
    )

    plan = plan_profile_discovery(
        shared_home=tmp_path,
        profile_name="alice",
        user_key="ou_alice",
        departments=["research"],
        requested_toolsets=["x_search"],
        xai_credentials={
            "available": True,
            "source": "vault:__org__/xai/api_key",
            "api_key": "xai-live-secret",
            "refresh_token": "oauth-refresh-secret",
        },
    )

    decision = plan["toolsets"]["x_search"]
    assert decision["allowed"] is True
    assert decision["credential_available"] is True
    assert decision["credential_source"] == "present"
    serialized = json.dumps(plan, ensure_ascii=False)
    assert "xai-live-secret" not in serialized
    assert "oauth-refresh-secret" not in serialized
    assert "vault:__org__" not in serialized


def test_discovery_policy_audit_writes_redacted_jsonl(tmp_path: Path):
    from hermes_multitenancy.discovery_policy import plan_profile_discovery

    _write_config(
        tmp_path,
        """
        enabled: true
        audit:
          enabled: true
        sources:
          browse-sh:
            action: allow
            audience:
              users:
                - ou_alice
        toolsets:
          x_search:
            action: allow
            audience:
              users:
                - ou_alice
        """,
    )

    plan = plan_profile_discovery(
        shared_home=tmp_path,
        profile_name="alice",
        user_key="ou_alice",
        requested_sources=["browse-sh", "github"],
        requested_toolsets=["x_search"],
        xai_credentials={"available": True, "source": "env", "api_key": "secret-token"},
        audit=True,
    )

    audit_path = tmp_path / "audit" / "discovery-policy.jsonl"
    record = json.loads(audit_path.read_text(encoding="utf-8").strip())

    assert plan["audit"]["written"] is True
    assert record["profile_name"] == "alice"
    assert record["user_key"] == "ou_alice"
    assert record["sources"]["allowed"] == ["browse-sh"]
    assert record["sources"]["blocked"] == ["github"]
    assert record["toolsets"]["allowed"] == ["x_search"]
    assert record["secret_free"] is True
    assert "secret-token" not in json.dumps(record)


def test_resolve_enabled_toolsets_filters_x_search_when_policy_blocks(tmp_path: Path):
    from hermes_multitenancy import agent_real

    def resolver(_config, _platform, **_kwargs):
        return ["file", "web", "x_search"]

    result = agent_real._resolve_enabled_toolsets(
        {},
        "webui",
        platform_tools_resolver=resolver,
        profile_home=tmp_path / "profiles" / "alice",
        shared_home=tmp_path,
    )

    assert result == ["file", "web"]


def test_resolve_enabled_toolsets_keeps_x_search_when_policy_allows(tmp_path: Path):
    from hermes_multitenancy import agent_real

    _write_config(
        tmp_path,
        """
        enabled: true
        toolsets:
          x_search:
            action: allow
            audience:
              profiles:
                - alice
        """,
    )

    def resolver(_config, _platform, **_kwargs):
        return ["file", "web", "x_search"]

    result = agent_real._resolve_enabled_toolsets(
        {},
        "webui",
        platform_tools_resolver=resolver,
        profile_home=tmp_path / "profiles" / "alice",
        shared_home=tmp_path,
        xai_credentials={"available": True, "source": "env", "api_key": "secret"},
    )

    assert result == ["file", "web", "x_search"]
