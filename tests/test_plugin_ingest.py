"""Hermetic tests for the reusable plugin ingester.

Every test builds a throwaway plugin repo + shared_home under tmp_path — none
touch the real ~/.hermes. CLI installation (kep-cli) is exercised only via the
"already present" / dry-run paths so no global state is mutated; the real
kep-cli install is covered by the live UAT probe, not here.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from hermes_multitenancy import plugin_ingest as pi


# ─────────────────────────── fixtures ────────────────────────────────────

GATE = "x approve"


def _write_plugin_repo(root: Path, *, env_default="pre", audience=None, clis=None, connectors=None, skills=None, command_aliases=None) -> Path:
    # default repo carries a governance-bearing orchestrator whose SKILL.md states the gate
    skills = skills or ["using-resource-delivery", "kep-trevi-delivery-orchestrate", "kep-halo-cli"]
    for name in skills:
        sk = root / "skills" / name
        sk.mkdir(parents=True, exist_ok=True)
        body = f"# {name}\n"
        if "orchestrat" in name:
            body += f"Gates: `{GATE}` requires explicit confirmation.\n"
        (sk / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}", encoding="utf-8")
    manifest = {
        "schema": pi.SUPPORTED_SCHEMA,
        "id": "test-plugin",
        "name": "测试插件",
        "version": "9.9.9",
        "entry_skill": "using-resource-delivery",
        "skills": {"dir": "./skills/", "list": skills},
        "install_mode": "copy",
        "audience": audience if audience is not None else {"department_ids": []},
        "clis": clis if clis is not None else [],
        "connectors": connectors if connectors is not None else [{"id": "kep-cli", "required": True}],
        "governance": {"env_default": env_default, "approval_required": ["x approve"], "online_requires": "explicit_action"},
        "command_aliases": command_aliases if command_aliases is not None else {},
        "persona_policy": "skill_inline",
    }
    out = root / ".hermes-plugin" / "plugin.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return root


def _shared_home(root: Path, profiles=("feishu_test",)) -> Path:
    home = root / "hermes_home"
    (home / "skills").mkdir(parents=True, exist_ok=True)
    (home / "bin").mkdir(parents=True, exist_ok=True)
    for p in profiles:
        (home / "profiles" / p / "skills").mkdir(parents=True, exist_ok=True)
    return home


# ─────────────────────────── manifest loading ────────────────────────────

def test_load_manifest_valid(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug")
    data = pi.load_plugin_manifest(repo)
    assert data["id"] == "test-plugin"
    assert data["skills"]["list"]


def test_load_manifest_missing(tmp_path):
    with pytest.raises(pi.PluginIngestError, match="no plugin manifest"):
        pi.load_plugin_manifest(tmp_path / "nope")


def test_load_manifest_bad_schema(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug")
    mf = repo / pi.PLUGIN_MANIFEST_REL
    data = json.loads(mf.read_text()); data["schema"] = "bogus/v0"
    mf.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(pi.PluginIngestError, match="unsupported schema"):
        pi.load_plugin_manifest(repo)


def test_load_manifest_unknown_audience_key_rejected(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug", audience={"dept": ["技术部"]})
    with pytest.raises(pi.PluginIngestError, match="unknown key"):
        pi.load_plugin_manifest(repo)


def test_load_manifest_rejects_path_traversal_skill(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug", skills=["using-resource-delivery"])
    mf = repo / pi.PLUGIN_MANIFEST_REL
    data = json.loads(mf.read_text())
    data["skills"]["list"] = ["../../../etc/evil"]
    mf.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(pi.PluginIngestError, match="unsafe skill path"):
        pi.load_plugin_manifest(repo)


def test_load_manifest_rejects_absolute_skill(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug", skills=["using-resource-delivery"])
    mf = repo / pi.PLUGIN_MANIFEST_REL
    data = json.loads(mf.read_text())
    data["skills"]["list"] = ["/abs/evil"]
    mf.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(pi.PluginIngestError, match="unsafe skill path"):
        pi.load_plugin_manifest(repo)


def test_load_manifest_rejects_unsafe_plugin_id(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug", skills=["using-resource-delivery"])
    mf = repo / pi.PLUGIN_MANIFEST_REL
    data = json.loads(mf.read_text()); data["id"] = "../../evil"
    mf.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(pi.PluginIngestError, match="unsafe plugin id"):
        pi.load_plugin_manifest(repo)


def test_load_manifest_rejects_unsafe_cli_id(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug", skills=["using-resource-delivery"],
                              clis=[{"id": "../evil", "install": "x"}])
    with pytest.raises(pi.PluginIngestError, match="unsafe cli id"):
        pi.load_plugin_manifest(repo)


def test_profile_mode_does_not_hijack_or_delete_foreign_skill(tmp_path):
    # an employee already has their OWN personal install of kep-halo-cli (different source)
    repo = _write_plugin_repo(tmp_path / "plug")
    home = _shared_home(tmp_path)
    pskills = home / "profiles" / "feishu_test" / "skills"
    foreign_src = str(tmp_path / "employee_upload" / "kep-halo-cli")
    (pskills / "kep-halo-cli").mkdir(parents=True)
    (pskills / "kep-halo-cli" / "SKILL.md").write_text("employee's own\n", encoding="utf-8")
    (pskills / ".hermes-personal-installs.json").write_text(
        json.dumps({"version": 1, "skills": {"kep-halo-cli": {"source": "personal", "target": foreign_src}}}),
        encoding="utf-8")

    report = pi.ingest(repo, audience="feishu_test", shared_home=home)
    acts = {(i["skill"]): i["action"] for i in report["skills"]["installed"]}
    assert acts["kep-halo-cli"] == "skipped-foreign"  # NOT hijacked
    assert "kep-halo-cli" not in report["skills"]["owned"].get("feishu_test", [])

    # uninstall must NOT delete the employee's skill
    pi.uninstall("test-plugin", shared_home=home)
    assert (pskills / "kep-halo-cli").exists()  # employee's skill survived
    assert _personal_target(pskills) == foreign_src


def test_profile_mode_does_not_overwrite_managed_skill(tmp_path):
    # org already MANAGES kep-halo-cli for this profile (.hermes-managed.json)
    repo = _write_plugin_repo(tmp_path / "plug")
    home = _shared_home(tmp_path)
    pskills = home / "profiles" / "feishu_test" / "skills"
    (pskills / "kep-halo-cli").mkdir(parents=True)
    (pskills / "kep-halo-cli" / "SKILL.md").write_text("org default\n", encoding="utf-8")
    (pskills / ".hermes-managed.json").write_text(
        json.dumps({"version": 1, "skills": {"kep-halo-cli": {"source": str(home / "skills" / "kep-halo-cli")}}}),
        encoding="utf-8")
    report = pi.ingest(repo, audience="feishu_test", shared_home=home)
    acts = {i["skill"]: i["action"] for i in report["skills"]["installed"]}
    assert acts["kep-halo-cli"] == "skipped-managed"
    assert "kep-halo-cli" not in report["skills"]["owned"].get("feishu_test", [])
    pi.uninstall("test-plugin", shared_home=home)
    assert (pskills / "kep-halo-cli").exists()  # managed skill untouched


def _personal_target(pskills):
    d = json.loads((pskills / ".hermes-personal-installs.json").read_text())
    return d["skills"]["kep-halo-cli"]["target"]


def test_uninstall_rejects_unsafe_plugin_id(tmp_path):
    home = _shared_home(tmp_path)
    with pytest.raises(pi.PluginIngestError, match="unsafe plugin id"):
        pi.uninstall("../../evil", shared_home=home)


def test_resolve_audience_rejects_traversal_token(tmp_path):
    home = _shared_home(tmp_path)
    with pytest.raises(pi.PluginIngestError, match="unsafe --audience token"):
        pi.resolve_audience("../../etc", profiles_root=home / "profiles")


def test_load_manifest_rejects_flag_injection_cli_install(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug", skills=["using-resource-delivery"],
                              clis=[{"id": "x", "install": "--all"}])
    with pytest.raises(pi.PluginIngestError, match=r"unsafe clis\[\].install"):
        pi.load_plugin_manifest(repo)


def test_governance_failure_leaves_recoverable_manifest(tmp_path):
    # orchestrator installed but its SKILL.md lacks the gate → governance fails AFTER the
    # manifest is persisted, so --uninstall can still roll the partial install back.
    repo = tmp_path / "plug"
    _write_plugin_repo(repo, skills=["using-resource-delivery", "kep-trevi-delivery-orchestrate"])
    (repo / "skills" / "kep-trevi-delivery-orchestrate" / "SKILL.md").write_text(
        "---\nname: o\n---\nno gate text here\n", encoding="utf-8")
    home = _shared_home(tmp_path)
    with pytest.raises(pi.PluginIngestError, match="gates not enforced"):
        pi.ingest(repo, audience="feishu_test", shared_home=home)
    assert (home / pi.MANAGED_DIR / "test-plugin.json").exists()  # tracked despite failure
    pi.uninstall("test-plugin", shared_home=home)
    assert not (home / pi.MANAGED_DIR / "test-plugin.json").exists()


def test_skills_dir_honored(tmp_path):
    # plugin keeps skills under a custom dir; manifest.skills.dir points at it
    repo = tmp_path / "plug"
    for name in ["using-resource-delivery", "kep-trevi-delivery-orchestrate"]:
        d = repo / "custom_skills" / name
        d.mkdir(parents=True)
        body = "# x\n" + ("Gates: x approve confirmed.\n" if "orchestrat" in name else "")
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}", encoding="utf-8")
    manifest = {
        "schema": pi.SUPPORTED_SCHEMA, "id": "dirplug", "version": "1",
        "entry_skill": "using-resource-delivery",
        "skills": {"dir": "./custom_skills/", "list": ["using-resource-delivery", "kep-trevi-delivery-orchestrate"]},
        "install_mode": "copy", "audience": {"department_ids": []}, "clis": [],
        "connectors": [{"id": "kep-cli", "required": True}],
        "governance": {"env_default": "pre", "approval_required": ["x approve"]},
    }
    out = repo / ".hermes-plugin" / "plugin.json"
    out.parent.mkdir(parents=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    home = _shared_home(tmp_path)

    report = pi.ingest(repo, audience="feishu_test", shared_home=home)
    assert (home / "skills" / "using-resource-delivery" / "SKILL.md").exists()
    assert len(report["skills"]["installed"]) == 2


def test_load_manifest_rejects_nondict_governance(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug", skills=["using-resource-delivery"])
    mf = repo / pi.PLUGIN_MANIFEST_REL
    data = json.loads(mf.read_text()); data["governance"] = "online"
    mf.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(pi.PluginIngestError, match="governance must be an object"):
        pi.load_plugin_manifest(repo)


def test_load_manifest_rejects_absolute_skills_dir(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug", skills=["using-resource-delivery"])
    mf = repo / pi.PLUGIN_MANIFEST_REL
    data = json.loads(mf.read_text()); data["skills"]["dir"] = "/tmp/skills"
    mf.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(pi.PluginIngestError, match="unsafe skills.dir"):
        pi.load_plugin_manifest(repo)


def test_load_manifest_online_default_rejected(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug", env_default="online")
    with pytest.raises(pi.PluginIngestError, match="online-by-default is forbidden"):
        pi.load_plugin_manifest(repo)


# ─────────────────────────── audience resolution ─────────────────────────

def test_resolve_audience_profile(tmp_path):
    home = _shared_home(tmp_path, profiles=("feishu_test",))
    aud = pi.resolve_audience("feishu_test", profiles_root=home / "profiles")
    assert aud.mode == "profile" and aud.profiles == ["feishu_test"]


def test_resolve_audience_department_ids(tmp_path):
    home = _shared_home(tmp_path)
    aud = pi.resolve_audience("101,202", profiles_root=home / "profiles")
    assert aud.mode == "department_ids" and aud.department_ids == ["101", "202"]


def test_resolve_audience_department_name_rejected(tmp_path):
    home = _shared_home(tmp_path)
    with pytest.raises(pi.PluginIngestError, match="department NAMES are rejected"):
        pi.resolve_audience("技术部", profiles_root=home / "profiles")


def test_resolve_audience_mixed_rejected(tmp_path):
    home = _shared_home(tmp_path, profiles=("feishu_test",))
    with pytest.raises(pi.PluginIngestError, match="mixes known profiles"):
        pi.resolve_audience("feishu_test,999notaprofile_x", profiles_root=home / "profiles")


def test_resolve_audience_empty_rejected(tmp_path):
    home = _shared_home(tmp_path)
    with pytest.raises(pi.PluginIngestError):
        pi.resolve_audience("", profiles_root=home / "profiles")


# ─────────────────────────── connectors / governance ─────────────────────

def test_validate_connectors_ok_kep_cli():
    # kep-cli is a real built-in connector
    res = pi.validate_connectors([{"id": "kep-cli", "required": True}])
    assert res["connectors"][0]["registered"] is True


def test_validate_connectors_required_missing():
    with pytest.raises(pi.PluginIngestError, match="not in registry"):
        pi.validate_connectors([{"id": "does-not-exist-xyz", "required": True}])


def test_validate_connectors_optional_missing_ok():
    res = pi.validate_connectors([{"id": "does-not-exist-xyz", "required": False}])
    assert res["connectors"][0]["registered"] is False


def test_assert_governance_pre_ok(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug")
    plugin = pi.load_plugin_manifest(repo)
    assert pi.assert_governance(plugin)["env_default"] == "pre"


# ─────────────────────────── ingest: dry-run ─────────────────────────────

def test_ingest_dry_run_writes_nothing(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug")
    home = _shared_home(tmp_path)
    report = pi.ingest(repo, audience="feishu_test", shared_home=home, dry_run=True)
    assert report["dry_run"] is True
    # nothing materialized
    assert not (home / pi.MANAGED_DIR / "test-plugin.json").exists()
    assert not (home / "profiles" / "feishu_test" / "skills" / "kep-halo-cli").exists()
    assert not (home / "skills" / "kep-halo-cli").exists()


# ─────────────────────────── ingest: profile mode ───────────────────────

def test_ingest_profile_mode_install_idempotent_uninstall(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug")
    home = _shared_home(tmp_path)

    r1 = pi.ingest(repo, audience="feishu_test", shared_home=home)
    profile_skill = home / "profiles" / "feishu_test" / "skills" / "kep-halo-cli"
    assert profile_skill.exists()
    assert (home / pi.MANAGED_DIR / "test-plugin.json").exists()
    assert len(r1["skills"]["installed"]) == 3  # 3 skills × 1 profile

    # idempotent: re-run does not error and source already present
    r2 = pi.ingest(repo, audience="feishu_test", shared_home=home)
    assert all(v == "present" for v in r2["skills"]["source_actions"].values())

    # uninstall removes profile skill installs + manifest
    u = pi.uninstall("test-plugin", shared_home=home)
    assert not profile_skill.exists()
    assert not (home / pi.MANAGED_DIR / "test-plugin.json").exists()
    assert any(item.get("action") == "removed" for item in u["removed"])


# ─────────────────────────── ingest: department mode ────────────────────

def test_ingest_profile_mode_asserts_profile_governance(tmp_path):
    repo = tmp_path / "plug"
    # build a repo whose orchestrator SKILL.md actually contains the gate text
    skills = ["using-resource-delivery", "kep-trevi-delivery-orchestrate"]
    _write_plugin_repo(repo, skills=skills)
    orch = repo / "skills" / "kep-trevi-delivery-orchestrate" / "SKILL.md"
    orch.write_text("---\nname: orch\n---\nGates: x approve must be confirmed.\n", encoding="utf-8")
    home = _shared_home(tmp_path)

    report = pi.ingest(repo, audience="feishu_test", shared_home=home)
    pg = report["profile_governance"][0]
    assert pg["profile"] == "feishu_test"
    assert pg["env_default"] == "pre"
    assert pg["gates_present_in_installed_skills"] == 1  # "x approve" present in orchestrator doc
    assert "kep-trevi-delivery-orchestrate" in pg["governance_skills_live"]


def test_assert_profile_governance_raises_if_orchestrator_absent(tmp_path):
    # orchestrator declared by plugin but never installed in the profile → fatal
    repo = _write_plugin_repo(tmp_path / "plug", skills=["using-resource-delivery", "kep-trevi-delivery-orchestrate"])
    home = _shared_home(tmp_path)
    plugin = pi.load_plugin_manifest(repo)
    empty_profile = home / "profiles" / "feishu_test"  # has empty skills/ dir
    with pytest.raises(pi.PluginIngestError, match="gates cannot be enforced"):
        pi.assert_profile_governance(plugin, empty_profile)


def test_ingest_department_mode_refuses_to_create_config(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug")
    home = _shared_home(tmp_path)  # no skill-distribution.yaml
    with pytest.raises(pi.PluginIngestError, match="refusing to create"):
        pi.ingest(repo, audience="101,202", shared_home=home)


def test_ingest_department_mode_writes_distribution_and_strips(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug")
    home = _shared_home(tmp_path)
    # production env: the distribution config already exists
    (home / pi.SKILL_DISTRIBUTION_FILE).write_text("skills: []\n", encoding="utf-8")

    pi.ingest(repo, audience="101,202", shared_home=home)
    dist = home / pi.SKILL_DISTRIBUTION_FILE
    assert dist.exists()
    raw = yaml.safe_load(dist.read_text())
    entries = {it["path"]: it for it in raw["skills"]}
    assert entries["kep-halo-cli"]["audience"]["department_ids"] == ["101", "202"]
    assert entries["kep-halo-cli"]["plugin"] == "test-plugin"

    pi.uninstall("test-plugin", shared_home=home)
    raw2 = yaml.safe_load(dist.read_text()) or {}
    assert all(it.get("plugin") != "test-plugin" for it in (raw2.get("skills") or []))


def test_department_mode_does_not_clobber_other_plugin_same_path(tmp_path):
    # another plugin already distributes the SAME skill path to a different audience
    repo = _write_plugin_repo(tmp_path / "plug", skills=["kep-halo-cli"])
    home = _shared_home(tmp_path)
    dist = home / pi.SKILL_DISTRIBUTION_FILE
    other = {"path": "kep-halo-cli", "install_mode": "copy",
             "audience": {"department_ids": ["999"]}, "plugin": "other-plugin"}
    dist.write_text(yaml.safe_dump({"skills": [other]}, allow_unicode=True), encoding="utf-8")

    pi.ingest(repo, audience="101", shared_home=home)
    entries = yaml.safe_load(dist.read_text())["skills"]
    # both the other plugin's entry AND ours survive for the same path
    owners = {(e["path"], e["plugin"]): e for e in entries}
    assert ("kep-halo-cli", "other-plugin") in owners
    assert ("kep-halo-cli", "test-plugin") in owners
    assert owners[("kep-halo-cli", "other-plugin")]["audience"]["department_ids"] == ["999"]

    # uninstall removes only ours; the other plugin's entry is untouched
    pi.uninstall("test-plugin", shared_home=home)
    remain = yaml.safe_load(dist.read_text())["skills"]
    assert [e["plugin"] for e in remain] == ["other-plugin"]


# ─────────────────────────── CLI install (no real kep-cli) ──────────────

def test_install_clis_skips_when_present(tmp_path):
    home = _shared_home(tmp_path)
    binp = home / "bin" / "halo-cli"
    binp.write_text("#!/bin/sh\n"); binp.chmod(0o755)
    res = pi.install_clis([{"id": "halo-cli", "install": "halo"}], shared_bin=home / "bin", dry_run=False, force=False)
    assert res[0]["action"] == "skipped"


def test_install_clis_dry_run_no_write(tmp_path):
    home = _shared_home(tmp_path)
    res = pi.install_clis([{"id": "newcli", "install": "new"}], shared_bin=home / "bin", dry_run=True, force=False)
    assert res[0]["action"] == "would-install"
    assert not (home / "bin" / "newcli").exists()


# ─────────────────────────── command_aliases (slash passthrough) ─────────

_ALIASES = {"strategy": "kep-trevi-delivery-orchestrate", "recap": "kep-halo-cli"}


def test_load_manifest_rejects_unsafe_alias_key(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug", command_aliases={"a/b": "kep-halo-cli"})
    with pytest.raises(pi.PluginIngestError, match="unsafe command_aliases key"):
        pi.load_plugin_manifest(repo)


def test_load_manifest_rejects_alias_target_not_in_skills(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug", command_aliases={"strategy": "not-a-real-skill"})
    with pytest.raises(pi.PluginIngestError, match="not in this plugin's skills.list"):
        pi.load_plugin_manifest(repo)


def test_ingest_writes_and_uninstall_removes_slash_aliases(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug", command_aliases=_ALIASES)
    home = _shared_home(tmp_path)
    pi.ingest(repo, audience="feishu_test", shared_home=home)
    cfg = home / pi.SLASH_ALIASES_FILE
    raw = yaml.safe_load(cfg.read_text())["aliases"]
    assert raw["strategy"]["skill"] == "kep-trevi-delivery-orchestrate"
    assert raw["strategy"]["plugin"] == "test-plugin"
    # uninstall strips this plugin's aliases
    pi.uninstall("test-plugin", shared_home=home)
    raw2 = yaml.safe_load(cfg.read_text()).get("aliases") or {}
    assert all(v.get("plugin") != "test-plugin" for v in raw2.values())


def test_ingest_dry_run_writes_no_aliases(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug", command_aliases=_ALIASES)
    home = _shared_home(tmp_path)
    pi.ingest(repo, audience="feishu_test", shared_home=home, dry_run=True)
    assert not (home / pi.SLASH_ALIASES_FILE).exists()


def test_alias_register_preserves_other_plugin(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug", command_aliases=_ALIASES)
    home = _shared_home(tmp_path)
    cfg = home / pi.SLASH_ALIASES_FILE
    cfg.write_text(yaml.safe_dump(
        {"aliases": {"otherc": {"skill": "other-skill", "plugin": "other-plugin"}}}), encoding="utf-8")
    pi.ingest(repo, audience="feishu_test", shared_home=home)
    al = yaml.safe_load(cfg.read_text())["aliases"]
    assert al["otherc"]["plugin"] == "other-plugin"  # untouched
    assert al["strategy"]["plugin"] == "test-plugin"
    pi.uninstall("test-plugin", shared_home=home)
    al2 = yaml.safe_load(cfg.read_text())["aliases"]
    assert "otherc" in al2 and "strategy" not in al2
