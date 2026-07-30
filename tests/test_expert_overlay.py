"""Tests for the expert Role-Override overlay (expert-role-override slug).

Covers:
  * expert_overlay.resolve_expert + build_role_override_block (resolve + block format)
  * plugin_ingest experts[] validation + persistence into the managed manifest
  * GET /api/run-broker/experts aggregation + audience filtering

Hermetic: every test builds a throwaway plugin repo + shared_home under tmp_path
and points HERMES_SHARED_HOME at it; none touch the real ~/.hermes. The
INVARIANT under test is that the persona overlay is RESOLVED from disk and
COMPOSED in-memory — it is never written to SOUL.md.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from hermes_multitenancy import expert_overlay as eo
from hermes_multitenancy import plugin_ingest as pi


def _seed_owner_routing(tmp_path, owner_open_id="ou_test", profile_name="feishu_test"):
    """Owner-scoped broker resolver verifies the caller owns the requested profile.

    Seed <owner>→<profile> and point the resolver at it; the autouse conftest
    fixture resets routing back to ":memory:" after the test.
    """
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable

    db_path = tmp_path / "expert_routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id=owner_open_id,
        profile_name=profile_name,
        open_id=owner_open_id,
        provenance="sync",
    )
    seeded.close()
    router_mod.override_routing_table(db_path)
    return owner_open_id


AGENT_MD = """---
name: kep-trevi-resource-delivery-expert
description: 资源投放专家
---

# 资源投放专家（Resource Delivery Expert）

你是资源投放领域的执行型专家。默认全程 `--env pre` 演练。
"""

EXPERT_ID = "kep-trevi-resource-delivery-expert"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\xf8\x0f\x00\x01\x01\x01\x00"
    b"\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _plugin_repo(
    root: Path,
    *,
    experts="default",
    audience=None,
    plugin_id="keep-resource-delivery",
) -> Path:
    """Build a plugin repo carrying skills + an agents/*.md persona + experts[]."""
    skills = ["using-resource-delivery", "kep-trevi-delivery-orchestrate"]
    for name in skills:
        sk = root / "skills" / name
        sk.mkdir(parents=True, exist_ok=True)
        body = "# x\n"
        if "orchestrat" in name:
            body += "Gates: `x approve` requires explicit confirmation.\n"
        (sk / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}", encoding="utf-8")
    (root / "agents").mkdir(parents=True, exist_ok=True)
    (root / "agents" / "kep-trevi-resource-delivery-expert.md").write_text(AGENT_MD, encoding="utf-8")
    (root / "avatars").mkdir(parents=True, exist_ok=True)
    (root / "avatars" / "expert.png").write_bytes(PNG_BYTES)

    if experts == "default":
        experts = [
            {
                "id": EXPERT_ID,
                "name": "资源投放专家",
                "title": "资源投放专家",
                "tagline": "一句话投放意图→选题到复盘全链路",
                "avatar": "./avatars/expert.png",
                "category": "营销增长",
                "display_tags": ["资源投放", "圈人"],
                "featured": True,
                "agent_md": "./agents/kep-trevi-resource-delivery-expert.md",
                "skills": skills,
                "governance": {"env_default": "pre", "approval_required": ["x approve"]},
                **({"audience": audience} if audience is not None else {}),
            }
        ]

    manifest = {
        "schema": pi.SUPPORTED_SCHEMA,
        "id": plugin_id,
        "name": "资源投放专家",
        "version": "0.1.0",
        "entry_skill": "using-resource-delivery",
        "skills": {"dir": "./skills/", "list": skills},
        "install_mode": "copy",
        "audience": {"department_ids": []},
        "clis": [],
        "connectors": [{"id": "kep-cli", "required": True}],
        "governance": {"env_default": "pre", "approval_required": ["x approve"], "online_requires": "explicit_action"},
        "persona_policy": "skill_inline",
    }
    if experts is not None:
        manifest["experts"] = experts
    out = root / pi.PLUGIN_MANIFEST_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return root


def _shared_home(root: Path, profile="feishu_test") -> Path:
    home = root / "hermes_home"
    (home / "skills").mkdir(parents=True, exist_ok=True)
    (home / "bin").mkdir(parents=True, exist_ok=True)
    (home / "profiles" / profile / "skills").mkdir(parents=True, exist_ok=True)
    return home


def _ingest(repo: Path, shared: Path, profile="feishu_test"):
    return pi.ingest(repo, audience=profile, shared_home=shared, profiles_root=shared / "profiles")


# ─────────────────────────── manifest validation ─────────────────────────────

def test_load_manifest_with_experts_ok(tmp_path):
    repo = _plugin_repo(tmp_path / "plug")
    data = pi.load_plugin_manifest(repo)
    assert isinstance(data["experts"], list)
    assert data["experts"][0]["id"] == EXPERT_ID


def test_experts_optional_absent_is_fine(tmp_path):
    repo = _plugin_repo(tmp_path / "plug", experts=None)
    data = pi.load_plugin_manifest(repo)
    assert "experts" not in data or not data.get("experts")


def test_experts_missing_agent_md_file_rejected(tmp_path):
    repo = _plugin_repo(tmp_path / "plug")
    # point at a non-existent persona file
    mf = repo / pi.PLUGIN_MANIFEST_REL
    data = json.loads(mf.read_text())
    data["experts"][0]["agent_md"] = "./agents/nope.md"
    mf.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(pi.PluginIngestError, match="agent_md"):
        pi.load_plugin_manifest(repo)


def test_experts_agent_md_traversal_rejected(tmp_path):
    repo = _plugin_repo(tmp_path / "plug")
    mf = repo / pi.PLUGIN_MANIFEST_REL
    data = json.loads(mf.read_text())
    data["experts"][0]["agent_md"] = "../../etc/passwd"
    mf.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(pi.PluginIngestError, match="agent_md"):
        pi.load_plugin_manifest(repo)


def test_experts_missing_id_rejected(tmp_path):
    repo = _plugin_repo(tmp_path / "plug")
    mf = repo / pi.PLUGIN_MANIFEST_REL
    data = json.loads(mf.read_text())
    data["experts"][0].pop("id")
    mf.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(pi.PluginIngestError, match="id"):
        pi.load_plugin_manifest(repo)


def test_experts_duplicate_id_rejected(tmp_path):
    repo = _plugin_repo(tmp_path / "plug")
    mf = repo / pi.PLUGIN_MANIFEST_REL
    data = json.loads(mf.read_text())
    data["experts"].append(dict(data["experts"][0]))
    mf.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(pi.PluginIngestError, match="duplicate"):
        pi.load_plugin_manifest(repo)


# ─────────────────────────── ingest persistence ──────────────────────────────

def test_ingest_persists_experts_and_repo(tmp_path):
    repo = _plugin_repo(tmp_path / "plug")
    shared = _shared_home(tmp_path)
    _ingest(repo, shared)
    mpath = shared / pi.MANAGED_DIR / "keep-resource-delivery.json"
    assert mpath.is_file()
    manifest = json.loads(mpath.read_text())
    assert manifest["repo"] == str(repo)
    assert manifest["experts"][0]["id"] == EXPERT_ID
    assert manifest["experts"][0]["avatar"].startswith(
        "/api/run-broker/plugin-assets/keep-resource-delivery/"
    )
    assert str(repo) not in manifest["experts"][0]["avatar"]
    asset_name = manifest["experts"][0]["avatar"].rsplit("/", 1)[-1]
    assert manifest["assets"][asset_name]["mime"] == "image/png"
    assert Path(manifest["assets"][asset_name]["path"]).read_bytes() == PNG_BYTES
    # SOUL.md is NEVER created by ingest (persona must not pollute the default agent)
    assert not (shared / "profiles" / "feishu_test" / "SOUL.md").exists()


def test_inactive_manifest_hides_and_blocks_expert(tmp_path, monkeypatch):
    repo = _plugin_repo(tmp_path / "plug")
    shared = _shared_home(tmp_path)
    _ingest(repo, shared)
    manifest_path = shared / pi.MANAGED_DIR / "keep-resource-delivery.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "inactive"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    profile = shared / "profiles" / "feishu_test"

    assert eo.list_experts(profile) == []
    assert eo.resolve_expert(profile, EXPERT_ID) is None


def test_ingest_rejects_local_avatar_with_url_unsafe_plugin_id(tmp_path):
    repo = _plugin_repo(tmp_path / "plug", plugin_id="keep resource delivery")
    shared = _shared_home(tmp_path)
    with pytest.raises(pi.PluginIngestError, match="URL component"):
        _ingest(repo, shared)


def test_ingest_dry_run_does_not_copy_avatar_asset(tmp_path):
    repo = _plugin_repo(tmp_path / "plug")
    shared = _shared_home(tmp_path)
    report = pi.ingest(
        repo,
        audience="feishu_test",
        shared_home=shared,
        profiles_root=shared / "profiles",
        dry_run=True,
    )
    assert report["assets"][0]["action"] == "would-copy"
    assert not (shared / pi.MANAGED_ASSETS_DIR).exists()
    assert not (shared / pi.MANAGED_DIR / "keep-resource-delivery.json").exists()


# ─────────────────────────── resolve + block format ──────────────────────────

def test_resolve_expert_and_block(tmp_path, monkeypatch):
    repo = _plugin_repo(tmp_path / "plug")
    shared = _shared_home(tmp_path)
    _ingest(repo, shared)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    profile_home = shared / "profiles" / "feishu_test"

    overlay = eo.resolve_expert(profile_home, EXPERT_ID)
    assert overlay is not None
    assert overlay.expert_id == EXPERT_ID
    assert overlay.plugin_id == "keep-resource-delivery"
    assert "资源投放领域的执行型专家" in overlay.agent_md
    assert overlay.skills

    block = eo.build_role_override_block(overlay)
    # Hermes remains the trusted host while the selected expert supplies the
    # active domain role and capabilities.
    assert block.startswith("## Hermes 专家模式")
    assert overlay.name in block
    assert "资源投放领域的执行型专家" in block
    assert "当前用户本人" in block
    assert "Hermes" in block
    assert "你是谁" in block
    assert "忽略上文" not in block
    assert "最高优先级" not in block
    assert "不再是 Hermes" not in block
    assert "不得自称 Hermes" not in block
    assert "不得透露或承认底层模型" not in block


def test_resolve_unknown_expert_is_none(tmp_path, monkeypatch):
    repo = _plugin_repo(tmp_path / "plug")
    shared = _shared_home(tmp_path)
    _ingest(repo, shared)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    profile_home = shared / "profiles" / "feishu_test"
    assert eo.resolve_expert(profile_home, "no-such-expert") is None
    assert eo.resolve_expert(profile_home, "") is None
    assert eo.role_override_block_for(profile_home, "no-such-expert") is None


def test_resolve_failsafe_on_corrupt_manifest(tmp_path, monkeypatch):
    shared = _shared_home(tmp_path)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    bad = shared / pi.MANAGED_DIR
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "broken.json").write_text("{ not json", encoding="utf-8")
    # must not raise — corrupt manifest is skipped
    assert eo.resolve_expert(shared / "profiles" / "feishu_test", EXPERT_ID) is None


# ─────────────────────────── /experts aggregation ────────────────────────────

def test_list_experts_aggregation(tmp_path, monkeypatch):
    repo = _plugin_repo(tmp_path / "plug")
    shared = _shared_home(tmp_path)
    _ingest(repo, shared)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    profile_home = shared / "profiles" / "feishu_test"
    rows = eo.list_experts(profile_home)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == EXPERT_ID
    assert row["category"] == "营销增长"
    assert row["featured"] is True
    assert row["avatar"].startswith("/api/run-broker/plugin-assets/keep-resource-delivery/")
    assert str(repo) not in row["avatar"]
    # redacted: no persona body / repo path leaks into the catalog row
    assert "agent_md" not in row
    assert all("你是资源投放" not in str(v) for v in row.values())


def test_list_experts_exposes_optional_skillhub_release_metadata(tmp_path, monkeypatch):
    repo = _plugin_repo(tmp_path / "plug")
    shared = _shared_home(tmp_path)
    _ingest(repo, shared)
    manifest_path = shared / pi.MANAGED_DIR / "keep-resource-delivery.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["release_version"] = "1.0.5"
    manifest["release_id"] = "196"
    manifest["release_installed_at"] = 1_784_892_894
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))

    row = eo.list_experts(shared / "profiles" / "feishu_test")[0]

    assert row["release_version"] == "1.0.5"
    assert row["release_installed_at"] == 1_784_892_894
    assert "release_id" not in row


def test_list_experts_falls_back_to_ingested_at_for_unstamped_manifests(tmp_path, monkeypatch):
    repo = _plugin_repo(tmp_path / "plug")
    shared = _shared_home(tmp_path)
    _ingest(repo, shared)
    manifest_path = shared / pi.MANAGED_DIR / "keep-resource-delivery.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # pre-2026-07-24 ingest: no release stamp, only ingested_at
    manifest.pop("release_version", None)
    manifest.pop("release_installed_at", None)
    manifest["ingested_at"] = 1_784_000_000
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))

    row = eo.list_experts(shared / "profiles" / "feishu_test")[0]

    assert row["release_installed_at"] == 1_784_000_000
    assert "release_version" not in row


def test_list_experts_ignores_invalid_ingested_at_fallback(tmp_path, monkeypatch):
    repo = _plugin_repo(tmp_path / "plug")
    shared = _shared_home(tmp_path)
    _ingest(repo, shared)
    manifest_path = shared / pi.MANAGED_DIR / "keep-resource-delivery.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("release_version", None)
    manifest.pop("release_installed_at", None)
    manifest["ingested_at"] = "not-a-timestamp"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))

    row = eo.list_experts(shared / "profiles" / "feishu_test")[0]

    assert "release_installed_at" not in row


def test_list_experts_audience_filter(tmp_path, monkeypatch):
    # expert scoped to department 42
    repo = _plugin_repo(tmp_path / "plug", audience={"department_ids": ["42"]})
    shared = _shared_home(tmp_path)
    _ingest(repo, shared)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    profile_home = shared / "profiles" / "feishu_test"
    # caller in dept 42 sees it; caller in dept 99 does not; unknown dept fails closed
    assert len(eo.list_experts(profile_home, department_ids=["42"])) == 1
    assert eo.list_experts(profile_home, department_ids=["99"]) == []
    assert eo.list_experts(profile_home, department_ids=None) == []


def test_experts_endpoint(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy import webui_broker_server as broker

    repo = _plugin_repo(tmp_path / "plug")
    shared = _shared_home(tmp_path)
    _ingest(repo, shared)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.delenv("HERMES_RUN_BROKER_KEY", raising=False)
    profile_home = shared / "profiles" / "feishu_test"
    monkeypatch.setattr(broker, "_profile_home_for_name", lambda profile_name: profile_home)

    owner = _seed_owner_routing(tmp_path)

    async def runner():
        app = broker.create_run_broker_app(
            mark_seen=lambda _request: True, sandbox_available=lambda: True
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get(
                "/api/run-broker/experts",
                headers={"X-Hermes-User-Key": owner, "X-Hermes-Profile": "feishu_test"},
            )
            assert resp.status == 200, await resp.text()
            data = await resp.json()
            return data
        finally:
            await client.close()

    data = asyncio.run(runner())
    assert data["profile_name"] == "feishu_test"
    assert len(data["experts"]) == 1
    assert data["experts"][0]["id"] == EXPERT_ID
    assert data["experts"][0]["avatar"].startswith("/api/run-broker/plugin-assets/")


def test_plugin_asset_endpoint_serves_registered_avatar(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy import webui_broker_server as broker

    repo = _plugin_repo(tmp_path / "plug")
    shared = _shared_home(tmp_path)
    _ingest(repo, shared)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "secret")
    (shared / "profiles" / "bob" / "skills").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(broker, "_profile_home_for_name", lambda profile_name: shared / "profiles" / profile_name)
    owner = _seed_owner_routing(tmp_path)

    async def runner():
        app = broker.create_run_broker_app(
            mark_seen=lambda _request: True, sandbox_available=lambda: True
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            catalog = await client.get(
                "/api/run-broker/experts",
                headers={"Authorization": "Bearer secret", "X-Hermes-User-Key": owner, "X-Hermes-Profile": "feishu_test"},
            )
            assert catalog.status == 200, await catalog.text()
            data = await catalog.json()
            avatar_url = data["experts"][0]["avatar"]

            unauthorized = await client.get(avatar_url)
            assert unauthorized.status == 401

            missing_profile = await client.get(avatar_url, headers={"Authorization": "Bearer secret"})
            assert missing_profile.status == 400

            hidden_profile = await client.get(
                avatar_url,
                headers={"Authorization": "Bearer secret", "X-Hermes-Profile": "bob"},
            )
            assert hidden_profile.status == 404

            asset = await client.get(
                avatar_url,
                headers={"Authorization": "Bearer secret", "X-Hermes-Profile": "feishu_test"},
            )
            assert asset.status == 200, await asset.text()
            assert asset.headers["Content-Type"].startswith("image/png")
            assert await asset.read() == PNG_BYTES

            missing = await client.get(
                "/api/run-broker/plugin-assets/keep-resource-delivery/nope.png",
                headers={"Authorization": "Bearer secret", "X-Hermes-Profile": "feishu_test"},
            )
            assert missing.status == 404
            traversal = await client.get(
                "/api/run-broker/plugin-assets/keep-resource-delivery/..%2Fsecret.png",
                headers={"Authorization": "Bearer secret", "X-Hermes-Profile": "feishu_test"},
            )
            assert traversal.status in (400, 404)
        finally:
            await client.close()

    asyncio.run(runner())


# ─────────────────────────── AUTHORIZATION (audience enforcement) ─────────────
# These cover the CODEX REVIEW FAIL: manifest-level audience must be enforced on
# BOTH the list (catalog) AND resolve (activation) paths, fail-CLOSED, and the
# caller's departments must be resolved SERVER-SIDE (never from a query param).


def _ingest_for(repo: Path, shared: Path, profile: str):
    """Ingest the plugin so it's owned by ``profile`` (sets manifest repo/experts)."""
    (shared / "profiles" / profile / "skills").mkdir(parents=True, exist_ok=True)
    return pi.ingest(repo, audience=profile, shared_home=shared, profiles_root=shared / "profiles")


def test_resolve_audience_profile_mode_cross_profile_denied(tmp_path, monkeypatch):
    """mode=profile profiles=[A]: A activates+lists; B (known id) is denied both."""
    repo = _plugin_repo(
        tmp_path / "plug",
        audience={"mode": "profile", "profiles": ["alice"], "department_ids": []},
    )
    shared = _shared_home(tmp_path, profile="alice")
    (shared / "profiles" / "bob" / "skills").mkdir(parents=True, exist_ok=True)
    _ingest_for(repo, shared, "alice")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    alice_home = shared / "profiles" / "alice"
    bob_home = shared / "profiles" / "bob"

    # caller A (in audience) — resolves AND lists
    assert eo.resolve_expert(alice_home, EXPERT_ID) is not None
    assert any(r["id"] == EXPERT_ID for r in eo.list_experts(alice_home))

    # caller B (NOT in audience) — denied even though the id is known
    assert eo.resolve_expert(bob_home, EXPERT_ID) is None
    assert all(r["id"] != EXPERT_ID for r in eo.list_experts(bob_home))
    assert eo.role_override_block_for(bob_home, EXPERT_ID) is None


def test_resolve_audience_department_mode(tmp_path, monkeypatch):
    """department_ids=[123]: dept-123 caller sees; dept-999 doesn't; query can't bypass."""
    repo = _plugin_repo(
        tmp_path / "plug",
        audience={"mode": "department_ids", "profiles": [], "department_ids": ["123"]},
    )
    shared = _shared_home(tmp_path)
    _ingest(repo, shared)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    profile_home = shared / "profiles" / "feishu_test"

    # caller whose resolved departments include 123 sees + activates
    assert len(eo.list_experts(profile_home, department_ids=["123"])) == 1
    assert eo.resolve_expert(profile_home, EXPERT_ID, department_ids=["123"]) is not None
    # caller in a different department does not
    assert eo.list_experts(profile_home, department_ids=["999"]) == []
    assert eo.resolve_expert(profile_home, EXPERT_ID, department_ids=["999"]) is None
    # unknown departments (None) → fail closed
    assert eo.list_experts(profile_home, department_ids=None) == []
    assert eo.resolve_expert(profile_home, EXPERT_ID, department_ids=None) is None


def test_department_resolution_is_server_side_query_cannot_bypass(tmp_path, monkeypatch):
    """A caller-supplied ?department_ids= must NOT expose a dept-scoped expert.

    The broker resolves departments SERVER-SIDE from the org snapshot; the query
    param is ignored. A snapshot placing feishu_test in dept 123 exposes the
    expert; an attacker passing ?department_ids=123 with NO snapshot match gets
    nothing.
    """
    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy import webui_broker_server as broker

    repo = _plugin_repo(
        tmp_path / "plug",
        audience={"mode": "department_ids", "profiles": [], "department_ids": ["123"]},
    )
    shared = _shared_home(tmp_path)
    _ingest(repo, shared)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.delenv("HERMES_RUN_BROKER_KEY", raising=False)
    profile_home = shared / "profiles" / "feishu_test"
    monkeypatch.setattr(broker, "_profile_home_for_name", lambda profile_name: profile_home)

    owner = _seed_owner_routing(tmp_path)

    def _query(profile_in_dept_123: bool):
        # Server-side resolver returns the TRUSTED departments, ignoring any query.
        monkeypatch.setattr(
            eo,
            "resolve_caller_departments",
            lambda ph, **kw: (["123"] if profile_in_dept_123 else None),
        )

        async def runner():
            app = broker.create_run_broker_app(
                mark_seen=lambda _r: True, sandbox_available=lambda: True
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                # attacker always tries to spoof dept 123 via the query param
                resp = await client.get(
                    "/api/run-broker/experts?department_ids=123",
                    headers={"X-Hermes-User-Key": owner, "X-Hermes-Profile": "feishu_test"},
                )
                assert resp.status == 200, await resp.text()
                return await resp.json()
            finally:
                await client.close()

        return asyncio.run(runner())

    # caller actually in dept 123 (server-resolved) → expert visible
    assert len(_query(True)["experts"]) == 1
    # caller NOT in dept 123 (server resolves None) → query spoof cannot expose it
    assert _query(False)["experts"] == []


def test_resolve_caller_departments_from_snapshot(tmp_path, monkeypatch):
    """The server-side resolver reads the newest org-*.json and matches the caller."""
    shared = _shared_home(tmp_path)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    snap_dir = shared / "org-snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "org-2026-06-25.json").write_text(
        json.dumps(
            {
                "employees": {
                    "agent-1": {
                        "profile_name": "feishu_test",
                        "open_id": "ou_abc",
                        "dept_id": "123",
                        "dept_name": "增长部",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    profile_home = shared / "profiles" / "feishu_test"
    depts = eo.resolve_caller_departments(profile_home, profile_name="feishu_test")
    assert depts is not None and "123" in depts
    # unknown caller → None (fail closed)
    assert eo.resolve_caller_departments(profile_home, profile_name="nobody") is None
    # no snapshot at all → None
    other = _shared_home(tmp_path / "empty")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(other))
    assert eo.resolve_caller_departments(other / "profiles" / "feishu_test") is None


def _set_install_audience(shared: Path, audience: dict | None, plugin_id="keep-resource-delivery"):
    """Overwrite the persisted MANAGED-MANIFEST install audience (manifest['audience']).

    ``pi.ingest`` always resolves a CONCRETE install audience (a profile or numeric
    departments) — it can never produce a truly-public (empty) install audience. To
    exercise the public case we patch the persisted manifest directly. ``audience=None``
    removes the key entirely (absent install audience).
    """
    mpath = shared / pi.MANAGED_DIR / f"{plugin_id}.json"
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    if audience is None:
        manifest.pop("audience", None)
    else:
        manifest["audience"] = audience
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def test_resolve_audience_install_scope_no_per_expert_audience_does_not_leak(tmp_path, monkeypatch):
    """SAFE behavior: install scoped to A + NO per-expert audience → A only, B denied.

    Managed manifests are shared-home GLOBAL, so an expert with no per-expert audience
    must INHERIT the plugin install audience — it must NOT leak to unrelated profiles.
    (This replaces the old test that locked in the unsafe leak.)
    """
    repo = _plugin_repo(tmp_path / "plug")  # NO per-expert audience
    shared = _shared_home(tmp_path, profile="alice")
    (shared / "profiles" / "bob" / "skills").mkdir(parents=True, exist_ok=True)
    # install the plugin for alice ONLY → manifest install audience = profiles=[alice]
    _ingest_for(repo, shared, "alice")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    alice_home = shared / "profiles" / "alice"
    bob_home = shared / "profiles" / "bob"

    # owner A: inherits the install audience → resolves + lists
    assert eo.resolve_expert(alice_home, EXPERT_ID) is not None
    assert any(r["id"] == EXPERT_ID for r in eo.list_experts(alice_home))

    # unrelated B: install audience excludes B → denied on BOTH paths (no leak)
    assert eo.resolve_expert(bob_home, EXPERT_ID) is None
    assert all(r["id"] != EXPERT_ID for r in eo.list_experts(bob_home))
    assert eo.role_override_block_for(bob_home, EXPERT_ID) is None


def test_resolve_audience_public_install_narrowed_by_per_expert(tmp_path, monkeypatch):
    """Public install audience + per-expert audience profiles=[A] → only A sees it."""
    repo = _plugin_repo(
        tmp_path / "plug",
        audience={"mode": "profile", "profiles": ["alice"], "department_ids": []},
    )
    shared = _shared_home(tmp_path, profile="alice")
    (shared / "profiles" / "bob" / "skills").mkdir(parents=True, exist_ok=True)
    _ingest_for(repo, shared, "alice")
    # force the INSTALL audience public so only the per-expert audience narrows
    _set_install_audience(shared, {"mode": "profile", "profiles": [], "department_ids": []})
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    alice_home = shared / "profiles" / "alice"
    bob_home = shared / "profiles" / "bob"

    # per-expert audience [alice] narrows the public install → A only
    assert eo.resolve_expert(alice_home, EXPERT_ID) is not None
    assert any(r["id"] == EXPERT_ID for r in eo.list_experts(alice_home))
    assert eo.resolve_expert(bob_home, EXPERT_ID) is None
    assert all(r["id"] != EXPERT_ID for r in eo.list_experts(bob_home))


def test_resolve_audience_both_empty_is_public(tmp_path, monkeypatch):
    """ONLY when BOTH install audience AND per-expert audience are empty → public."""
    repo = _plugin_repo(tmp_path / "plug")  # NO per-expert audience
    shared = _shared_home(tmp_path)
    (shared / "profiles" / "stranger" / "skills").mkdir(parents=True, exist_ok=True)
    _ingest(repo, shared)
    # strip the install audience entirely → both scopes empty
    _set_install_audience(shared, None)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    stranger_home = shared / "profiles" / "stranger"
    assert eo.resolve_expert(stranger_home, EXPERT_ID) is not None
    assert len(eo.list_experts(stranger_home)) == 1
    # explicitly-empty install-audience shape is also public
    _set_install_audience(shared, {"mode": "profile", "profiles": [], "department_ids": []})
    assert eo.resolve_expert(stranger_home, EXPERT_ID) is not None
    assert len(eo.list_experts(stranger_home)) == 1
