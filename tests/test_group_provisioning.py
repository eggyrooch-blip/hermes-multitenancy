"""Layer 2 tests — group route auto-provisioning + profile skeleton."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


def _read_yaml(path: Path) -> dict:
    import yaml

    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert isinstance(loaded, dict)
    return loaded


# -- Pure helpers ------------------------------------------------------------


def test_extract_chat_type_from_source():
    from hermes_multitenancy.router import _extract_chat_type

    event = SimpleNamespace(source=SimpleNamespace(chat_type="group"))
    assert _extract_chat_type(event) == "group"


def test_extract_chat_type_from_event_attr():
    from hermes_multitenancy.router import _extract_chat_type

    event = SimpleNamespace(chat_type="p2p", source=None)
    assert _extract_chat_type(event) == "p2p"


def test_extract_chat_type_missing_returns_empty():
    from hermes_multitenancy.router import _extract_chat_type

    event = SimpleNamespace(source=SimpleNamespace())
    assert _extract_chat_type(event) == ""


def test_is_group_chat_type_recognizes_known_values():
    from hermes_multitenancy.router import _is_group_chat_type

    assert _is_group_chat_type("group") is True
    assert _is_group_chat_type("topic") is True
    assert _is_group_chat_type("Group") is True  # case-insensitive
    assert _is_group_chat_type("p2p") is False
    assert _is_group_chat_type("") is False


def test_extract_chat_id_prefers_source():
    from hermes_multitenancy.router import _extract_chat_id

    event = SimpleNamespace(source=SimpleNamespace(chat_id="oc_abc"))
    assert _extract_chat_id(event) == "oc_abc"


def test_extract_chat_id_falls_back_to_event_attr():
    from hermes_multitenancy.router import _extract_chat_id

    event = SimpleNamespace(chat_id="oc_event", source=None)
    assert _extract_chat_id(event) == "oc_event"


def test_make_group_profile_name_is_deterministic():
    from hermes_multitenancy.router import _make_group_profile_name

    assert _make_group_profile_name("oc_abc123") == _make_group_profile_name("oc_abc123")
    # Different chat_ids produce different profile names
    assert _make_group_profile_name("oc_abc") != _make_group_profile_name("oc_def")


def test_make_group_profile_name_prefix_filesystem_safe():
    from hermes_multitenancy.router import _make_group_profile_name

    name = _make_group_profile_name("oc_AbC123-:&^%")
    assert name.startswith("feishu_group_")
    # Profile name must not contain any unsafe filesystem characters.
    for ch in name:
        assert ch.isalnum() or ch in "_-"


def test_is_group_profile_name():
    from hermes_multitenancy.router import is_group_profile_name

    assert is_group_profile_name("feishu_group_abc_1234") is True
    assert is_group_profile_name("feishu_owner") is False
    assert is_group_profile_name("") is False
    assert is_group_profile_name(None) is False


# -- register_chat_inviter --------------------------------------------------


def test_register_chat_inviter_stores_entry():
    from hermes_multitenancy import router as router_mod

    router_mod._chat_inviter_cache.clear()
    router_mod.register_chat_inviter(
        "oc_x", "ou_inviter", chat_name="IT 组", inviter_display="owner"
    )
    entry = router_mod._chat_inviter_cache["oc_x"]
    assert entry["inviter_open_id"] == "ou_inviter"
    assert entry["chat_name"] == "IT 组"
    assert entry["inviter_display"] == "owner"


def test_register_chat_inviter_rejects_non_open_id():
    from hermes_multitenancy import router as router_mod

    router_mod._chat_inviter_cache.clear()
    # tenant-local user_id format (no ou_ prefix) must be rejected
    router_mod.register_chat_inviter("oc_x", "g41a5b5g")
    assert "oc_x" not in router_mod._chat_inviter_cache


def test_register_chat_inviter_rejects_empty_chat_id():
    from hermes_multitenancy import router as router_mod

    router_mod._chat_inviter_cache.clear()
    router_mod.register_chat_inviter("", "ou_inviter")
    assert router_mod._chat_inviter_cache == {}


# -- _ensure_group_profile --------------------------------------------------


def test_ensure_group_profile_writes_marker_and_soul(tmp_path):
    from hermes_multitenancy.router import _ensure_group_profile

    profile_home = tmp_path / "profiles" / "feishu_group_x"
    _ensure_group_profile(
        profile_name="feishu_group_x",
        profile_home=profile_home,
        chat_id="oc_abc",
        owner_open_id="ou_inviter",
        display_label="owner-IT组",
    )
    # Profile dir exists with skeleton files
    assert profile_home.is_dir()
    assert (profile_home / "SOUL.md").exists()
    soul_text = (profile_home / "SOUL.md").read_text(encoding="utf-8")
    assert "oc_abc" in soul_text
    assert "ou_inviter" in soul_text
    assert "owner-IT组" in soul_text
    assert "lark_cli" in soul_text
    assert "bot identity" in soul_text
    assert "https://" in soul_text
    assert "short timeout" in soul_text
    config_text = (profile_home / "config.yaml").read_text(encoding="utf-8")
    assert "lark-cli" in config_text
    assert "toolsets_mode: explicit" in config_text
    assert "feishu: merge_default" in config_text
    assert "api_server:" in config_text
    assert "terminal" not in config_text
    assert "feishu_docx" not in config_text
    config = _read_yaml(profile_home / "config.yaml")
    assert config["image_gen"] == {"provider": "tencent-vod", "model": "gem-3.1"}
    # The group profile marker exists and forbids feishu_auth.
    marker = json.loads((profile_home / "group_profile.json").read_text("utf-8"))
    assert marker["kind"] == "group"
    assert marker["chat_id"] == "oc_abc"
    assert marker["owner_open_id"] == "ou_inviter"
    assert marker["feishu_auth_disabled"] is True
    # The feishu_uat/ directory exists but is empty (no per-user UAT here).
    uat_dir = profile_home / "feishu_uat"
    assert uat_dir.is_dir()
    assert list(uat_dir.iterdir()) == []


def test_ensure_group_profile_is_idempotent(tmp_path):
    from hermes_multitenancy.router import _ensure_group_profile

    profile_home = tmp_path / "profiles" / "feishu_group_x"
    _ensure_group_profile(
        profile_name="feishu_group_x",
        profile_home=profile_home,
        chat_id="oc_abc",
        owner_open_id="ou_inviter",
        display_label="owner-IT组",
    )
    soul_before = (profile_home / "SOUL.md").read_text("utf-8")
    # Calling again must not overwrite the SOUL or change the marker.
    _ensure_group_profile(
        profile_name="feishu_group_x",
        profile_home=profile_home,
        chat_id="oc_abc",
        owner_open_id="ou_inviter",
        display_label="owner-IT组-renamed",
    )
    soul_after = (profile_home / "SOUL.md").read_text("utf-8")
    assert soul_after == soul_before
    assert soul_after.count("Feishu/Lark capability rules:") == 1


def test_ensure_group_profile_extends_existing_lark_guidance_without_duplicate_heading(tmp_path):
    from hermes_multitenancy.router import _ensure_group_profile

    profile_home = tmp_path / "profiles" / "feishu_group_existing"
    profile_home.mkdir(parents=True)
    (profile_home / "SOUL.md").write_text(
        "\n".join(
            [
                "# Existing",
                "",
                "Feishu/Lark capability rules:",
                "- 飞书/Lark 的读写、导出和长尾 OpenAPI 能力优先通过 `lark_cli` 工具完成。",
                "- 如果需要调用飞书能力，必须等待真实工具结果；不要编造 message_id、文档链接或调用结果。",
                "- `lark_cli` 的 identity 使用 auto，profile runtime 会按个人/群聊边界选择 user 或 bot。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    _ensure_group_profile(
        profile_name="feishu_group_existing",
        profile_home=profile_home,
        chat_id="oc_existing",
        owner_open_id="ou_inviter",
        display_label="existing",
    )

    soul_text = (profile_home / "SOUL.md").read_text("utf-8")
    assert soul_text.count("Feishu/Lark capability rules:") == 1
    assert "credential unavailable" in soul_text
    assert "terminal" in soul_text
    assert "npx" in soul_text
    assert 'mode="script"' in soul_text
    assert "任何解释器或直接执行方式" in soul_text
    assert "不限文件类型或所在目录" in soul_text


def test_lark_cli_defaults_cover_webui_api_server_platform():
    from hermes_multitenancy.router import _apply_lark_cli_profile_defaults

    config = {"model": {"default": "openai/test"}}

    _apply_lark_cli_profile_defaults(config)

    assert config["platform_toolsets"]["feishu"] == ["lark-cli"]
    assert config["platform_toolsets"]["api_server"] == ["lark-cli"]
    assert config["platform_toolsets"]["webui"] == ["lark-cli"]
    assert "terminal" not in config["platform_toolsets"]["api_server"]
    assert config["multitenancy"]["toolsets_mode"] == "explicit"
    # feishu is merge_default (not explicit): explicit dropped the `skills`
    # toolset so lark-* skills never reached the agent manifest.
    assert config["multitenancy"]["platform_toolsets_mode"] == {
        "feishu": "merge_default",
        "api_server": "merge_default",
        "webui": "merge_default",
    }


def test_feishu_profile_keeps_skills_toolset_after_lark_cli_defaults(monkeypatch):
    """Regression: feishu pinned to ``explicit`` returned only ["lark-cli"]
    and silently dropped the ``skills`` toolset, so lark-* skill bodies
    symlinked into the profile never appeared in the agent manifest
    (user report: 技能里看不到 lark skills). With ``merge_default`` the
    profile keeps both lark-cli and the default ``skills`` capability.

    Fails if ``_apply_lark_cli_profile_defaults`` sets feishu mode back to
    ``explicit`` (the pre-fix behavior).
    """
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.router import _apply_lark_cli_profile_defaults

    monkeypatch.delenv("HERMES_MULTITENANCY_TOOLSETS_MODE", raising=False)

    config = {"model": {"default": "openai/test"}}
    _apply_lark_cli_profile_defaults(config)

    def fake_get_platform_tools(cfg, platform, *, include_default_mcp_servers=True):
        # Real feishu/api_server platform defaults include the `skills`
        # capability that surfaces the profile's skills/ directory.
        return {"skills", "file", "web"}

    resolved = agent_real._resolve_enabled_toolsets(
        config,
        "feishu",
        platform_tools_resolver=fake_get_platform_tools,
    )

    assert resolved is not None
    assert "lark-cli" in resolved
    assert "skills" in resolved  # dropped pre-fix under explicit mode


def test_ensure_group_profile_inherits_default_skills(tmp_path):
    from hermes_multitenancy.router import _ensure_group_profile

    shared_home = tmp_path
    keep_source = shared_home / "skills" / "Keep" / "keep-record"
    lark_source = shared_home / "skills" / "lark-base"
    keep_source.mkdir(parents=True)
    lark_source.mkdir(parents=True)
    (shared_home / "profile-skill-defaults.yaml").write_text(
        "skills:\n  - Keep/keep-record\n  - lark-base\n",
        encoding="utf-8",
    )
    (keep_source / "SKILL.md").write_text("# Keep Record\n", encoding="utf-8")
    (lark_source / "SKILL.md").write_text("# Lark Base\n", encoding="utf-8")
    profile_home = shared_home / "profiles" / "feishu_group_x"

    _ensure_group_profile(
        profile_name="feishu_group_x",
        profile_home=profile_home,
        chat_id="oc_abc",
        owner_open_id="ou_inviter",
        display_label="owner-IT组",
    )

    assert (profile_home / "skills" / "Keep" / "keep-record" / "SKILL.md").exists()
    lark_target = profile_home / "skills" / "lark-base"
    assert lark_target.is_symlink()
    assert lark_target.resolve() == lark_source.resolve()


def test_ensure_group_profile_receives_available_lark_skills(tmp_path):
    from hermes_multitenancy.router import _ensure_group_profile

    shared_home = tmp_path
    lark_slides = shared_home / "skills" / "lark-slides"
    lark_wiki = shared_home / "skills" / "lark-wiki"
    for source in (lark_slides, lark_wiki):
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text(f"# {source.name}\n", encoding="utf-8")
    profile_home = shared_home / "profiles" / "feishu_group_x"

    _ensure_group_profile(
        profile_name="feishu_group_x",
        profile_home=profile_home,
        chat_id="oc_abc",
        owner_open_id="ou_inviter",
        display_label="owner-IT组",
    )

    for name, source in {"lark-slides": lark_slides, "lark-wiki": lark_wiki}.items():
        target = profile_home / "skills" / name
        assert target.is_symlink()
        assert target.resolve() == source.resolve()


def test_group_profile_inherits_owner_child_shareable_skills_not_tokens(tmp_path):
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.router import _ensure_group_profile
    from hermes_multitenancy.sync.feishu_org import Employee, _sync_default_profile_skills

    shared_home = tmp_path
    shared_skill = shared_home / "skills" / "internal" / "readonly-report"
    private_skill = shared_home / "skills" / "internal" / "personal-oauth"
    shared_skill.mkdir(parents=True)
    private_skill.mkdir(parents=True)
    (shared_skill / "SKILL.md").write_text("# Readonly Report\n", encoding="utf-8")
    (private_skill / "SKILL.md").write_text("# Personal OAuth\n", encoding="utf-8")
    (shared_home / "skill-distribution.yaml").write_text(
        """
skills:
  - path: internal/readonly-report
    audience:
      departments: [od_sales]
    requires_token: false
  - path: internal/personal-oauth
    audience:
      departments: [od_sales]
    token_policy: user_oauth
""",
        encoding="utf-8",
    )
    owner_profile = shared_home / "profiles" / "alice"
    owner = Employee(
        open_id="ou_alice",
        user_id="alice",
        agent_id="alice",
        profile_name="alice",
        name="Alice",
        dept_id="od_sales",
        dept_name="Sales",
        leader_user_id=None,
    )
    _sync_default_profile_skills(owner_profile, shared_home, owner)
    (owner_profile / "tokens").mkdir(parents=True)
    (owner_profile / "tokens" / "personal-oauth.json").write_text("secret", encoding="utf-8")

    router_mod.override_routing_table(":memory:")
    try:
        table = router_mod._get_routing_table()
        assert table is not None
        table.upsert(
            user_id="alice",
            profile_name="alice",
            open_id="ou_alice",
            provenance="sync",
        )
        group_profile = shared_home / "profiles" / "feishu_group_sales"
        _ensure_group_profile(
            profile_name="feishu_group_sales",
            profile_home=group_profile,
            chat_id="oc_sales",
            owner_open_id="ou_alice",
            display_label="Alice-Sales",
        )
    finally:
        router_mod.override_routing_table(None)

    assert (group_profile / "skills" / "internal" / "readonly-report").is_symlink()
    assert not (group_profile / "skills" / "internal" / "personal-oauth").exists()
    assert list((group_profile / "tokens").glob("*")) == []
    assert list((group_profile / "feishu_uat").glob("*")) == []


def test_group_profile_keeps_default_skills_when_owner_route_exists(tmp_path):
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.router import _ensure_group_profile
    from hermes_multitenancy.sync.feishu_org import Employee, _sync_default_profile_skills

    shared_home = tmp_path
    default_skill = shared_home / "skills" / "Keep" / "kep-prd-analysis"
    owner_shared_skill = shared_home / "skills" / "internal" / "readonly-report"
    owner_private_skill = shared_home / "skills" / "internal" / "personal-oauth"
    for skill_dir in (default_skill, owner_shared_skill, owner_private_skill):
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {skill_dir.name}\n", encoding="utf-8")
    (shared_home / "skill-distribution.yaml").write_text(
        """
skills:
  - path: Keep/kep-prd-analysis
    audience: all
  - path: internal/readonly-report
    audience:
      departments: [od_sales]
    requires_token: false
  - path: internal/personal-oauth
    audience:
      departments: [od_sales]
    token_policy: user_oauth
""",
        encoding="utf-8",
    )
    owner_profile = shared_home / "profiles" / "alice"
    owner = Employee(
        open_id="ou_alice",
        user_id="alice",
        agent_id="alice",
        profile_name="alice",
        name="Alice",
        dept_id="od_sales",
        dept_name="Sales",
        leader_user_id=None,
    )
    _sync_default_profile_skills(owner_profile, shared_home, owner)

    router_mod.override_routing_table(":memory:")
    try:
        table = router_mod._get_routing_table()
        assert table is not None
        table.upsert(
            user_id="alice",
            profile_name="alice",
            open_id="ou_alice",
            provenance="sync",
        )
        group_profile = shared_home / "profiles" / "feishu_group_sales"
        _ensure_group_profile(
            profile_name="feishu_group_sales",
            profile_home=group_profile,
            chat_id="oc_sales",
            owner_open_id="ou_alice",
            display_label="Alice-Sales",
        )
    finally:
        router_mod.override_routing_table(None)

    assert (group_profile / "skills" / "Keep" / "kep-prd-analysis").is_symlink()
    assert (group_profile / "skills" / "internal" / "readonly-report").is_symlink()
    assert not (group_profile / "skills" / "internal" / "personal-oauth").exists()


# -- resolve_or_auto_provision_group_route ----------------------------------


@pytest.fixture
def isolated_router(tmp_path, monkeypatch):
    from hermes_multitenancy import router as router_mod

    router_mod._chat_inviter_cache.clear()
    router_mod.override_routing_table(":memory:")
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda name: tmp_path / "profiles" / name,
    )
    # Ensure auto-provision is enabled regardless of host env
    monkeypatch.setenv("HERMES_MULTITENANCY_AUTO_PROVISION", "1")
    yield router_mod, tmp_path
    router_mod.override_routing_table(None)
    router_mod._chat_inviter_cache.clear()


@pytest.mark.asyncio
async def test_resolve_returns_existing_group_route(isolated_router):
    router_mod, tmp_path = isolated_router
    table = router_mod._get_routing_table()
    table.upsert_group(
        chat_id="oc_existing",
        profile_name="feishu_group_existing",
        owner_open_id="ou_inviter",
        display_label="owner-IT",
    )
    profile_name, profile_home = await router_mod.resolve_or_auto_provision_group_route(
        chat_id="oc_existing", gateway=None
    )
    assert profile_name == "feishu_group_existing"
    assert profile_home == tmp_path / "profiles" / "feishu_group_existing"


@pytest.mark.asyncio
async def test_resolve_existing_group_route_normalizes_legacy_feishu_explicit_mode(
    isolated_router,
):
    """Existing group profiles created before the CardKit/file-IO work may
    still pin Feishu to only ["lark-cli"]. Route resolution must migrate them
    so group Feishu can use default file/web tools while lark_cli stays present.
    """
    router_mod, tmp_path = isolated_router
    table = router_mod._get_routing_table()
    profile_home = tmp_path / "profiles" / "feishu_group_legacy"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "\n".join(
            [
                "model:",
                "  default: openai/test-model",
                "platform_toolsets:",
                "  feishu:",
                "    - lark-cli",
                "multitenancy:",
                "  toolsets_mode: explicit",
                "  platform_toolsets_mode:",
                "    feishu: explicit",
                "    api_server: merge_default",
                "    webui: merge_default",
                "",
            ]
        ),
        encoding="utf-8",
    )
    table.upsert_group(
        chat_id="oc_existing",
        profile_name="feishu_group_legacy",
        owner_open_id="ou_inviter",
        display_label="owner-IT",
    )

    profile_name, returned_home = await router_mod.resolve_or_auto_provision_group_route(
        chat_id="oc_existing", gateway=None
    )

    assert profile_name == "feishu_group_legacy"
    assert returned_home == profile_home
    config_text = (profile_home / "config.yaml").read_text(encoding="utf-8")
    assert "feishu: merge_default" in config_text
    assert "feishu: explicit" not in config_text


@pytest.mark.asyncio
async def test_resolve_auto_provisions_when_cache_has_inviter(isolated_router):
    router_mod, tmp_path = isolated_router
    router_mod.register_chat_inviter(
        "oc_new", "ou_inviter", chat_name="IT 组", inviter_display="owner"
    )

    class _Adapter:
        async def get_chat_info(self, chat_id):
            return {"name": "IT 组"}

        async def get_user_name(self, open_id):
            return "owner"

    gateway = SimpleNamespace(adapters={"feishu": _Adapter()})
    profile_name, profile_home = await router_mod.resolve_or_auto_provision_group_route(
        chat_id="oc_new", gateway=gateway,
    )
    assert profile_name is not None and profile_name.startswith("feishu_group_")
    assert profile_home is not None and profile_home.is_dir()
    # The routing row exists with the inviter as owner.
    row = router_mod._get_routing_table().lookup_by_chat_id("oc_new")
    assert row is not None
    assert row.owner_open_id == "ou_inviter"
    assert row.display_label == "owner-IT 组"
    # Cache is evicted post-provision so a future re-add starts fresh.
    assert "oc_new" not in router_mod._chat_inviter_cache


@pytest.mark.asyncio
async def test_provision_survives_router_restart_via_db_pending_inviter(isolated_router):
    router_mod, _ = isolated_router
    router_mod.register_chat_inviter(
        "oc_restart", "ou_inviter", chat_name="IT", inviter_display="owner"
    )
    router_mod._chat_inviter_cache.clear()

    class _Adapter:
        async def get_chat_info(self, chat_id):
            return {"name": "IT"}

        async def get_user_name(self, open_id):
            return "owner"

    gateway = SimpleNamespace(adapters={"feishu": _Adapter()})
    profile_name, _profile_home = await router_mod.resolve_or_auto_provision_group_route(
        chat_id="oc_restart", gateway=gateway,
    )

    assert profile_name is not None
    row = router_mod._get_routing_table().lookup_by_chat_id("oc_restart")
    assert row is not None
    assert row.owner_open_id == "ou_inviter"


@pytest.mark.asyncio
async def test_provision_prefers_db_inviter_over_empty_cache(isolated_router):
    router_mod, _ = isolated_router
    table = router_mod._get_routing_table()
    table.put_pending_inviter("oc_dbonly", "ou_A")
    router_mod._chat_inviter_cache.clear()

    class _Adapter:
        async def get_chat_info(self, chat_id):
            return {"name": "DB Only"}

        async def get_user_name(self, open_id):
            return "Owner A"

    gateway = SimpleNamespace(adapters={"feishu": _Adapter()})
    profile_name, _profile_home = await router_mod.resolve_or_auto_provision_group_route(
        chat_id="oc_dbonly", gateway=gateway,
    )

    assert profile_name is not None
    row = router_mod._get_routing_table().lookup_by_chat_id("oc_dbonly")
    assert row is not None
    assert row.owner_open_id == "ou_A"


@pytest.mark.asyncio
async def test_expired_pending_inviter_is_pruned_and_not_used_as_owner(isolated_router):
    router_mod, _ = isolated_router
    table = router_mod._get_routing_table()
    table.put_pending_inviter("oc_expired", "ou_expired")
    table._conn.execute(
        "UPDATE multitenancy_pending_group_inviter SET created_at = ? WHERE chat_id = ?",
        (1, "oc_expired"),
    )
    table._conn.commit()
    deleted = table.prune_pending_inviters(
        now=router_mod._CHAT_INVITER_CACHE_TTL_S + 2,
        ttl_seconds=router_mod._CHAT_INVITER_CACHE_TTL_S,
    )
    router_mod._chat_inviter_cache.clear()

    class _Adapter:
        async def get_chat_info(self, chat_id):
            return {"name": "Expired"}

        async def get_user_name(self, open_id):
            return "Expired Owner"

    gateway = SimpleNamespace(adapters={"feishu": _Adapter()})
    profile_name, profile_home = await router_mod.resolve_or_auto_provision_group_route(
        chat_id="oc_expired", gateway=gateway,
    )

    assert deleted == 1
    assert table.get_pending_inviter("oc_expired") is None
    assert profile_name is None
    assert profile_home is None
    assert router_mod._get_routing_table().lookup_by_chat_id("oc_expired") is None


def test_bot_added_creates_durable_route_before_first_message(isolated_router):
    router_mod, _ = isolated_router
    table = router_mod._get_routing_table()

    router_mod.register_chat_inviter(
        "oc_durable", "ou_inviter", chat_name="IT", inviter_display="owner"
    )
    table._conn.execute(
        "UPDATE multitenancy_pending_group_inviter SET created_at = ? WHERE chat_id = ?",
        (1, "oc_durable"),
    )
    table._conn.commit()
    table.prune_pending_inviters(
        now=router_mod._CHAT_INVITER_CACHE_TTL_S + 2,
        ttl_seconds=router_mod._CHAT_INVITER_CACHE_TTL_S,
    )
    router_mod._chat_inviter_cache.clear()

    profile_name, profile_home = asyncio.run(
        router_mod.resolve_or_auto_provision_group_route(
            chat_id="oc_durable", gateway=None,
        )
    )

    assert profile_name is not None
    assert profile_home is not None and profile_home.is_dir()
    row = table.lookup_by_chat_id("oc_durable")
    assert row is not None
    assert row.owner_open_id == "ou_inviter"
    assert row.display_label == "owner-IT"


def test_register_chat_inviter_survives_unavailable_pending_store(monkeypatch):
    from hermes_multitenancy import router as router_mod

    router_mod._chat_inviter_cache.clear()
    monkeypatch.setattr(router_mod, "_get_routing_table", lambda: None)

    router_mod.register_chat_inviter("oc_x", "ou_inviter")

    assert "oc_x" in router_mod._chat_inviter_cache


@pytest.mark.asyncio
async def test_resolve_refuses_when_no_inviter_cached(isolated_router):
    """Per design: no implicit fallback to chat.owner_id. Refuse silently."""
    router_mod, _ = isolated_router

    class _Adapter:
        async def get_chat_info(self, chat_id):
            return {"name": "IT 组", "owner_id": "ou_someone"}

    gateway = SimpleNamespace(adapters={"feishu": _Adapter()})
    profile_name, profile_home = await router_mod.resolve_or_auto_provision_group_route(
        chat_id="oc_never_added", gateway=gateway,
    )
    assert profile_name is None
    assert profile_home is None
    # No routing row was created.
    assert router_mod._get_routing_table().lookup_by_chat_id("oc_never_added") is None


@pytest.mark.asyncio
async def test_resolve_provisions_group_with_inviter_when_user_auto_provision_disabled(
    isolated_router, monkeypatch
):
    router_mod, _ = isolated_router
    monkeypatch.setenv("HERMES_MULTITENANCY_AUTO_PROVISION", "0")
    router_mod.register_chat_inviter("oc_x", "ou_inviter")
    profile_name, profile_home = await router_mod.resolve_or_auto_provision_group_route(
        chat_id="oc_x", gateway=None,
    )
    assert profile_name is not None and profile_name.startswith("feishu_group_")
    assert profile_home is not None
    row = router_mod._get_routing_table().lookup_by_chat_id("oc_x")
    assert row is not None
    assert row.owner_open_id == "ou_inviter"


@pytest.mark.asyncio
async def test_resolve_fetches_chat_name_when_cache_missing_it(isolated_router):
    router_mod, _ = isolated_router
    router_mod.register_chat_inviter("oc_lookup", "ou_inviter")  # no chat_name/display

    class _Adapter:
        async def get_chat_info(self, chat_id):
            return {"name": "技术中台"}

        async def get_user_name(self, open_id):
            return "owner"

    gateway = SimpleNamespace(adapters={"feishu": _Adapter()})
    profile_name, _profile_home = await router_mod.resolve_or_auto_provision_group_route(
        chat_id="oc_lookup", gateway=gateway,
    )
    assert profile_name is not None
    row = router_mod._get_routing_table().lookup_by_chat_id("oc_lookup")
    assert row.display_label == "owner-技术中台"


# -- handle_async group-chat integration ------------------------------------


def _group_event(text: str = "@bot hi", chat_id: str = "oc_g1") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        source=SimpleNamespace(
            chat_id=chat_id,
            user_id="g_member",
            user_id_alt=None,
            user_name="member",
            chat_type="group",
            open_id="ou_member_who_at_mentioned",
            platform=SimpleNamespace(value="feishu"),
        ),
    )


@pytest.mark.asyncio
async def test_handle_async_routes_group_to_group_profile(isolated_router, monkeypatch):
    """A group-chat message must dispatch to the group profile, never to the
    @-mentioning user's profile."""
    router_mod, tmp_path = isolated_router

    # Pre-provision the group route
    router_mod._get_routing_table().upsert(
        user_id="inviter",
        profile_name="inviter",
        open_id="ou_inviter",
        provenance="sync",
    )
    router_mod.register_chat_inviter(
        "oc_g1", "ou_inviter", chat_name="IT 组", inviter_display="owner"
    )

    captured_homes = []

    async def capture_runner(event, home):
        captured_homes.append(home)
        return "ok"

    from hermes_multitenancy import runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, "_default_run_agent", capture_runner)

    sends = []

    class _Adapter:
        async def get_chat_info(self, chat_id):
            return {"name": "IT 组"}
        async def get_user_name(self, open_id):
            return "owner"
        async def send_typing(self, c): pass
        async def send(self, c, m, *, reply_to=None, metadata=None):
            sends.append((c, m))

    gateway = SimpleNamespace(adapters={"feishu": _Adapter()})
    await router_mod.handle_async(event=_group_event(), gateway=gateway)

    assert captured_homes, "runner should have been called"
    selected_home = captured_homes[0]
    assert selected_home.name.startswith("feishu_group_"), (
        f"expected group profile, got {selected_home.name}"
    )


@pytest.mark.asyncio
async def test_handle_async_rejects_feishu_auth_in_group(isolated_router, monkeypatch):
    router_mod, _ = isolated_router
    router_mod.register_chat_inviter(
        "oc_g_auth", "ou_inviter", chat_name="IT", inviter_display="owner"
    )

    runner_calls: list = []

    async def runner(event, home):
        runner_calls.append(home)
        return "ok"

    from hermes_multitenancy import runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, "_default_run_agent", runner)

    sends = []

    class _Adapter:
        async def get_chat_info(self, chat_id):
            return {"name": "IT"}
        async def get_user_name(self, open_id):
            return "owner"
        async def send_typing(self, c): pass
        async def send(self, c, m, *, reply_to=None, metadata=None):
            sends.append((c, m))

    gateway = SimpleNamespace(adapters={"feishu": _Adapter()})
    evt = _group_event(text="/feishu_auth", chat_id="oc_g_auth")
    await router_mod.handle_async(event=evt, gateway=gateway)

    # Runner must NOT have been invoked — the auth command was intercepted.
    assert runner_calls == []
    # Adapter received a polite rejection mentioning the limitation.
    assert any("群聊模式" in m for _c, m in sends), sends


@pytest.mark.asyncio
async def test_handle_async_group_without_inviter_sends_guidance(isolated_router, monkeypatch):
    router_mod, _ = isolated_router
    # No register_chat_inviter call — simulate restart-before-bot-added.

    runner_calls: list = []

    async def runner(event, home):
        runner_calls.append(home)
        return "ok"

    from hermes_multitenancy import runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, "_default_run_agent", runner)

    sends = []

    class _Adapter:
        async def send_typing(self, c): pass
        async def send(self, c, m, *, reply_to=None, metadata=None):
            sends.append((c, m))

    gateway = SimpleNamespace(adapters={"feishu": _Adapter()})
    await router_mod.handle_async(
        event=_group_event(chat_id="oc_orphan"), gateway=gateway
    )
    assert runner_calls == []
    assert any("移除我后" in m or "邀请人身份" in m for _c, m in sends), sends


@pytest.mark.asyncio
async def test_handle_async_p2p_unchanged_by_group_branch(isolated_router, monkeypatch):
    """P2P (private) chats must NOT pick up the group code path."""
    router_mod, tmp_path = isolated_router
    table = router_mod._get_routing_table()
    profile_dir = tmp_path / "profiles" / "feishu_solo"
    profile_dir.mkdir(parents=True)
    table.upsert(
        user_id="solo_user",
        profile_name="feishu_solo",
        open_id="ou_solo",
    )

    captured = []

    async def runner(event, home):
        captured.append(home)
        return "ok"

    from hermes_multitenancy import runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, "_default_run_agent", runner)

    class _Adapter:
        async def send_typing(self, c): pass
        async def send(self, c, m, *, reply_to=None, metadata=None): pass

    gateway = SimpleNamespace(adapters={"feishu": _Adapter()})

    event = SimpleNamespace(
        text="hi",
        source=SimpleNamespace(
            chat_id="p2p_chat",
            user_id="solo_user",
            user_id_alt=None,
            user_name="solo",
            chat_type="p2p",
            open_id="ou_solo",
            platform=SimpleNamespace(value="feishu"),
        ),
    )
    await router_mod.handle_async(event=event, gateway=gateway)
    assert captured == [profile_dir], (
        f"expected p2p sender route, got {captured}"
    )


# -- Adversarial coverage (independent code-review follow-ups) --------------

def test_blocked_group_command_normalizes_bypasses():
    """/feishu_auth gate must survive @bot suffix and zero-width padding."""
    from hermes_multitenancy.router import _is_blocked_group_command

    assert _is_blocked_group_command("feishu_auth") is True
    assert _is_blocked_group_command("feishu_auth@2号BOT_A") is True
    assert _is_blocked_group_command("FEISHU_AUTH") is True
    assert _is_blocked_group_command("feishu_auth​") is True  # zero-width
    assert _is_blocked_group_command("  feishu_auth  ") is True
    assert _is_blocked_group_command("feishu_logout") is True
    assert _is_blocked_group_command("feishu_reauth") is True
    # Non-auth commands stay allowed
    assert _is_blocked_group_command("status") is False
    assert _is_blocked_group_command("new") is False
    assert _is_blocked_group_command("") is False


def test_short_chat_id_collision_resistance():
    """Two chat_ids sharing the 12-char cosmetic prefix must still map to
    distinct profile names via the 64-bit sha1 suffix."""
    from hermes_multitenancy.router import _short_chat_id, _make_group_profile_name

    a = "oc_samprefix000000000000000000aaaa"
    b = "oc_samprefix000000000000000000bbbb"
    # Same 12-char alnum prefix after stripping 'oc'
    assert _short_chat_id(a)[:12] == _short_chat_id(b)[:12]
    # But distinct overall (suffix differs) → distinct profiles
    assert _short_chat_id(a) != _short_chat_id(b)
    assert _make_group_profile_name(a) != _make_group_profile_name(b)
    # 16 hex chars of sha1 in the suffix
    assert len(_short_chat_id(a).split("_")[-1]) == 16


def test_chat_inviter_cache_is_bounded():
    """Spamming bot-add across throwaway chats must not grow the cache
    without bound."""
    from hermes_multitenancy import router as router_mod

    with router_mod._chat_inviter_cache_lock:
        router_mod._chat_inviter_cache.clear()
    cap = router_mod._CHAT_INVITER_CACHE_MAX
    for i in range(cap + 50):
        router_mod.register_chat_inviter(f"oc_spam_{i}", f"ou_{i:040d}")
    with router_mod._chat_inviter_cache_lock:
        size = len(router_mod._chat_inviter_cache)
    assert size <= cap, f"cache grew past cap: {size} > {cap}"
    with router_mod._chat_inviter_cache_lock:
        router_mod._chat_inviter_cache.clear()


def test_chat_inviter_cache_ttl_expiry(monkeypatch):
    """Entries older than the TTL must not resolve."""
    from hermes_multitenancy import router as router_mod

    with router_mod._chat_inviter_cache_lock:
        router_mod._chat_inviter_cache.clear()
    router_mod.register_chat_inviter("oc_ttl", "ou_" + "a" * 40)
    assert router_mod._resolve_group_inviter_from_cache("oc_ttl") is not None
    # Jump time past the TTL
    real_time = router_mod.time.time
    monkeypatch.setattr(
        router_mod.time,
        "time",
        lambda: real_time() + router_mod._CHAT_INVITER_CACHE_TTL_S + 10,
    )
    assert router_mod._resolve_group_inviter_from_cache("oc_ttl") is None
    with router_mod._chat_inviter_cache_lock:
        router_mod._chat_inviter_cache.clear()


def test_group_env_excludes_non_model_secrets(tmp_path):
    """Group .env must carry only model provider keys + FEISHU_HOME_CHANNEL,
    never the credential-vault master key / gateway config / tool tokens."""
    from hermes_multitenancy.router import _write_group_profile_env

    shared = tmp_path
    (shared / ".env").write_text(
        "GLM_API_KEY=model-key-keep\n"
        "GLM_BASE_URL=https://api.example/keep\n"
        "HERMES_MULTITENANCY_CREDENTIAL_KEY=VAULT-MASTER-MUST-NOT-LEAK\n"
        "API_SERVER_PORT=8643\n"
        "PLOTLAKE_TOKEN=tool-token-must-not-leak\n"
        "GATEWAY_ALLOW_ALL_USERS=1\n"
        "# a comment line\n"
        "\n",
        encoding="utf-8",
    )
    profile_home = shared / "profiles" / "feishu_group_x"
    profile_home.mkdir(parents=True)

    _write_group_profile_env(profile_home, shared, "oc_secret_test")

    env_text = (profile_home / ".env").read_text(encoding="utf-8")
    # Model keys preserved
    assert "GLM_API_KEY=model-key-keep" in env_text
    assert "GLM_BASE_URL=https://api.example/keep" in env_text
    # Home channel override present
    assert "FEISHU_HOME_CHANNEL=oc_secret_test" in env_text
    # Sensitive / irrelevant lines must NOT be duplicated into the tenant dir
    assert "VAULT-MASTER-MUST-NOT-LEAK" not in env_text
    assert "HERMES_MULTITENANCY_CREDENTIAL_KEY" not in env_text
    assert "PLOTLAKE_TOKEN" not in env_text
    assert "API_SERVER_PORT" not in env_text
    assert "GATEWAY_ALLOW_ALL_USERS" not in env_text


def test_group_env_atomic_no_tmp_left_behind(tmp_path):
    """The atomic write must leave the final .env and no .env.tmp residue."""
    from hermes_multitenancy.router import _write_group_profile_env

    shared = tmp_path
    (shared / ".env").write_text("GLM_API_KEY=k\n", encoding="utf-8")
    profile_home = shared / "profiles" / "feishu_group_y"
    profile_home.mkdir(parents=True)
    _write_group_profile_env(profile_home, shared, "oc_atomic")
    assert (profile_home / ".env").exists()
    assert not (profile_home / ".env.tmp").exists()
    # Re-run is idempotent (replaces, doesn't append/dup)
    _write_group_profile_env(profile_home, shared, "oc_atomic")
    txt = (profile_home / ".env").read_text(encoding="utf-8")
    assert txt.count("FEISHU_HOME_CHANNEL=oc_atomic") == 1


def test_ensure_auto_profile_writes_profile_local_env(tmp_path):
    """Auto-provisioned user profiles must not symlink .env to shared secrets."""
    from hermes_multitenancy.router import _ensure_auto_profile

    shared = tmp_path / ".hermes"
    shared.mkdir()
    (shared / ".env").write_text("HERMES_MULTITENANCY_CREDENTIAL_KEY=shared-secret\n", encoding="utf-8")
    (shared / "auth.json").write_text("{}", encoding="utf-8")
    profile_home = shared / "profiles" / "songtingting"

    _ensure_auto_profile(
        "songtingting",
        profile_home,
        route_key="ou_songtingting",
        sender="ou_songtingting",
    )

    env_path = profile_home / ".env"
    assert env_path.exists()
    assert not env_path.is_symlink()
    assert "shared-secret" not in env_path.read_text(encoding="utf-8")
    config = _read_yaml(profile_home / "config.yaml")
    assert config["image_gen"] == {"provider": "tencent-vod", "model": "gem-3.1"}


def test_ensure_webui_agent_profile_defaults_to_vod_and_disables_feishu(tmp_path):
    """WebUI agent profiles should inherit default image_gen but never connect Feishu."""
    from hermes_multitenancy.router import _ensure_webui_agent_profile

    shared = tmp_path / ".hermes"
    shared.mkdir()
    (shared / "auth.json").write_text("{}", encoding="utf-8")
    profile_home = shared / "profiles" / "agent_city_poster"

    _ensure_webui_agent_profile(
        profile_name="agent_city_poster",
        profile_home=profile_home,
        owner_open_id="ou_owner",
        display_label="City Poster Agent",
        agent_id="agent_city_poster",
    )

    config = _read_yaml(profile_home / "config.yaml")
    assert config["image_gen"] == {"provider": "tencent-vod", "model": "gem-3.1"}
    assert config["platforms"]["feishu"] == {"enabled": False}


def test_repair_profile_local_env_backfills_shared_env_symlink(tmp_path):
    """Backfill replaces only .env symlinks that point at shared .env."""
    from hermes_multitenancy.router import repair_profile_local_envs

    shared = tmp_path / ".hermes"
    profiles = shared / "profiles"
    user = profiles / "songtingting"
    router = profiles / "multitenancy_router"
    kept = profiles / "feishu_group_x"
    created = profiles / "new_user"
    external = profiles / "external_env"
    external_target = tmp_path / "external.env"
    for path in (shared, profiles, user, router, kept, created, external):
        path.mkdir(parents=True, exist_ok=True)
    shared_env = shared / ".env"
    shared_env.write_text("HERMES_MULTITENANCY_CREDENTIAL_KEY=shared-secret\n", encoding="utf-8")
    external_target.write_text("EXTERNAL=keep\n", encoding="utf-8")
    (user / ".env").symlink_to(shared_env)
    (router / ".env").symlink_to(shared_env)
    (kept / ".env").write_text("FEISHU_HOME_CHANNEL=oc_keep\n", encoding="utf-8")
    (external / ".env").symlink_to(external_target)
    (user / "feishu_uat").mkdir()
    (user / "feishu_uat" / "ou_songtingting.json").write_text('{"refresh_token":"keep"}', encoding="utf-8")

    stats = repair_profile_local_envs(shared_home=shared)

    assert stats["repaired"] == 1
    assert stats["created"] == 1
    assert stats["kept"] == 1
    assert stats["skipped_service"] == 1
    assert stats["skipped_non_shared_symlink"] == 1
    assert (user / ".env").exists()
    assert not (user / ".env").is_symlink()
    assert (created / ".env").exists()
    assert not (created / ".env").is_symlink()
    assert "shared-secret" not in (user / ".env").read_text(encoding="utf-8")
    assert (router / ".env").is_symlink()
    assert (kept / ".env").read_text(encoding="utf-8") == "FEISHU_HOME_CHANNEL=oc_keep\n"
    assert (external / ".env").is_symlink()
    assert (user / "feishu_uat" / "ou_songtingting.json").read_text(encoding="utf-8") == '{"refresh_token":"keep"}'


def test_repair_profile_local_env_dry_run_reports_plan_without_writing(tmp_path):
    """Dry-run must not report applied repairs or mutate symlinks."""
    from hermes_multitenancy.router import repair_profile_local_envs

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "songtingting"
    missing = shared / "profiles" / "new_user"
    profile.mkdir(parents=True)
    missing.mkdir(parents=True)
    shared_env = shared / ".env"
    shared_env.write_text("SECRET=shared\n", encoding="utf-8")
    (profile / ".env").symlink_to(shared_env)

    stats = repair_profile_local_envs(shared_home=shared, dry_run=True)

    assert stats["planned_repaired"] == 1
    assert stats["planned_created"] == 1
    assert stats["repaired"] == 0
    assert stats["created"] == 0
    assert (profile / ".env").is_symlink()
    assert not (missing / ".env").exists()
