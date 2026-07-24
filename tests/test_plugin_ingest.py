"""Hermetic tests for the reusable plugin ingester.

Every test builds a throwaway plugin repo + shared_home under tmp_path — none
touch the real ~/.hermes. CLI installation (kep-cli) is exercised only via the
"already present" / dry-run paths so no global state is mutated; the real
kep-cli install is covered by the live UAT probe, not here.
"""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest
import yaml

from hermes_multitenancy import plugin_ingest as pi


# ─────────────────────────── fixtures ────────────────────────────────────

GATE = "x approve"


def _write_plugin_repo(
    root: Path,
    *,
    plugin_id="test-plugin",
    env_default="pre",
    audience=None,
    clis=None,
    connectors=None,
    skills=None,
) -> Path:
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
        "id": plugin_id,
        "name": "测试插件",
        "version": "9.9.9",
        "entry_skill": skills[0],
        "skills": {"dir": "./skills/", "list": skills},
        "install_mode": "copy",
        "audience": audience if audience is not None else {"department_ids": []},
        "clis": clis if clis is not None else [],
        "connectors": connectors if connectors is not None else [{"id": "kep-cli", "required": True}],
        "governance": {"env_default": env_default, "approval_required": ["x approve"], "online_requires": "explicit_action"},
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


@pytest.mark.parametrize("missing", ["entry", "orchestrator"])
def test_load_manifest_requires_entry_and_orchestrator(tmp_path, missing):
    repo = _write_plugin_repo(tmp_path / "plug")
    mf = repo / pi.PLUGIN_MANIFEST_REL
    data = json.loads(mf.read_text(encoding="utf-8"))
    if missing == "entry":
        data.pop("entry_skill")
    else:
        data["skills"]["list"] = [
            name for name in data["skills"]["list"] if "orchestrat" not in name
        ]
    mf.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(pi.PluginIngestError, match=missing):
        pi.load_plugin_manifest(repo)


def test_load_manifest_rejects_unsafe_cli_id(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug", clis=[{"id": "../evil", "install": "x"}])
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
    repo = _write_plugin_repo(tmp_path / "plug", clis=[{"id": "x", "install": "--all"}])
    with pytest.raises(pi.PluginIngestError, match=r"unsafe clis\[\].install"):
        pi.load_plugin_manifest(repo)


def test_governance_failure_leaves_no_runtime_manifest(tmp_path):
    repo = tmp_path / "plug"
    _write_plugin_repo(repo, skills=["using-resource-delivery", "kep-trevi-delivery-orchestrate"])
    (repo / "skills" / "kep-trevi-delivery-orchestrate" / "SKILL.md").write_text(
        "---\nname: o\n---\nno gate text here\n", encoding="utf-8")
    home = _shared_home(tmp_path)
    with pytest.raises(pi.PluginIngestError, match="gates not enforced"):
        pi.ingest(repo, audience="feishu_test", shared_home=home)
    managed_path = home / pi.MANAGED_DIR / "test-plugin.json"
    assert not managed_path.exists()


def test_profile_governance_accepts_gates_in_all_owned_declared_skills(tmp_path):
    skills = [
        "using-resource-delivery",
        "kep-trevi-delivery-orchestrate",
        "kep-halo-cli",
        "kep-other-cli",
    ]
    repo = _write_plugin_repo(tmp_path / "plug", skills=skills)
    manifest_path = repo / pi.PLUGIN_MANIFEST_REL
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["governance"]["approval_required"] = [
        "gate alpha",
        "gate beta",
        "gate gamma",
        "gate delta",
        "gate epsilon",
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (repo / "skills" / "kep-halo-cli" / "SKILL.md").write_text(
        "gate alpha\ngate beta\ngate gamma\n", encoding="utf-8"
    )
    (repo / "skills" / "kep-other-cli" / "SKILL.md").write_text(
        "gate delta\ngate epsilon\n", encoding="utf-8"
    )

    report = pi.ingest(repo, audience="feishu_test", shared_home=_shared_home(tmp_path))

    assert report["profile_governance"][0]["gates_present_in_installed_skills"] == 5


def test_profile_governance_rejects_one_missing_gate_and_keeps_previous_active(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug")
    home = _shared_home(tmp_path)
    pi.ingest(repo, audience="feishu_test", shared_home=home)
    manifest_path = repo / pi.PLUGIN_MANIFEST_REL
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["governance"]["approval_required"] = ["x approve", "missing gate"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(pi.PluginIngestError, match="1 of 2"):
        pi.ingest(repo, audience="feishu_test", shared_home=home, force=True)

    managed = json.loads((home / pi.MANAGED_DIR / "test-plugin.json").read_text(encoding="utf-8"))
    assert managed["status"] == "active"


def test_failed_reingest_keeps_previous_active_manifest(tmp_path, monkeypatch):
    repo = _write_plugin_repo(tmp_path / "plug")
    home = _shared_home(tmp_path)
    managed_path = home / pi.MANAGED_DIR / "test-plugin.json"
    pi.ingest(repo, audience="feishu_test", shared_home=home)
    assert json.loads(managed_path.read_text(encoding="utf-8"))["status"] == "active"

    observed = []

    def reject(*_args):
        observed.append(json.loads(managed_path.read_text(encoding="utf-8"))["status"])
        raise pi.PluginIngestError("forced post-install failure")

    monkeypatch.setattr(pi, "assert_profile_governance", reject)
    with pytest.raises(pi.PluginIngestError, match="forced post-install failure"):
        pi.ingest(repo, audience="feishu_test", shared_home=home, force=True)

    assert observed == ["active"]
    assert json.loads(managed_path.read_text(encoding="utf-8"))["status"] == "active"


def test_concurrent_reingests_serialize_activation_and_failed_mutation(tmp_path, monkeypatch):
    valid_repo = _write_plugin_repo(tmp_path / "valid")
    invalid_repo = _write_plugin_repo(tmp_path / "invalid")
    invalid_manifest_path = invalid_repo / pi.PLUGIN_MANIFEST_REL
    invalid_manifest = json.loads(invalid_manifest_path.read_text(encoding="utf-8"))
    invalid_manifest["governance"]["approval_required"] = ["missing gate"]
    invalid_manifest_path.write_text(json.dumps(invalid_manifest), encoding="utf-8")
    home = _shared_home(tmp_path)
    managed_path = home / pi.MANAGED_DIR / "test-plugin.json"
    pi.ingest(valid_repo, audience="feishu_test", shared_home=home)

    first_paused = threading.Event()
    release_first = threading.Event()
    second_mutating = threading.Event()
    release_second = threading.Event()
    original_assert = pi.assert_profile_governance
    original_install = pi._install_skills_to_profile

    def pause_first(plugin, *args):
        result = original_assert(plugin, *args)
        if Path(plugin["_repo"]) == valid_repo:
            first_paused.set()
            assert release_first.wait(2)
        return result

    def pause_second(plugin, *args, **kwargs):
        if Path(plugin["_repo"]) == invalid_repo:
            second_mutating.set()
            assert release_second.wait(2)
        return original_install(plugin, *args, **kwargs)

    monkeypatch.setattr(pi, "assert_profile_governance", pause_first)
    monkeypatch.setattr(pi, "_install_skills_to_profile", pause_second)
    errors = []

    def run(repo):
        try:
            pi.ingest(repo, audience="feishu_test", shared_home=home, force=True)
        except Exception as exc:  # captured for assertions after both threads finish
            errors.append(exc)

    first = threading.Thread(target=run, args=(valid_repo,))
    second = threading.Thread(target=run, args=(invalid_repo,))
    first.start()
    assert first_paused.wait(2)
    assert json.loads(managed_path.read_text(encoding="utf-8"))["status"] == "active"
    second.start()
    assert not second_mutating.wait(0.1)
    release_first.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive() and not second.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], pi.PluginIngestError)
    assert json.loads(managed_path.read_text(encoding="utf-8"))["status"] == "active"


def test_different_plugin_rollback_cannot_clobber_concurrent_commit(tmp_path, monkeypatch):
    skills_a = ["a-entry", "a-orchestrate"]
    skills_b = ["b-entry", "b-orchestrate"]
    original_a = _write_plugin_repo(
        tmp_path / "a-original", plugin_id="plugin-a", skills=skills_a
    )
    original_b = _write_plugin_repo(
        tmp_path / "b-original", plugin_id="plugin-b", skills=skills_b
    )
    home = _shared_home(tmp_path)
    pi.ingest(original_a, audience="feishu_test", shared_home=home)
    pi.ingest(original_b, audience="feishu_test", shared_home=home)

    upgraded_a = _write_plugin_repo(
        tmp_path / "a-upgraded", plugin_id="plugin-a", skills=skills_a
    )
    upgraded_b = _write_plugin_repo(
        tmp_path / "b-upgraded", plugin_id="plugin-b", skills=skills_b
    )
    for repo, skill, marker in (
        (upgraded_a, "a-entry", "candidate-a"),
        (upgraded_b, "b-entry", "candidate-b"),
    ):
        path = repo / "skills" / skill / "SKILL.md"
        path.write_text(path.read_text(encoding="utf-8") + marker, encoding="utf-8")

    a_paused = threading.Event()
    release_a = threading.Event()
    b_mutating = threading.Event()
    original_assert = pi.assert_profile_governance
    original_install = pi._install_skills_to_profile

    def fail_a_after_mutation(plugin, *args):
        result = original_assert(plugin, *args)
        if Path(plugin["_repo"]) == upgraded_a:
            a_paused.set()
            assert release_a.wait(2)
            raise pi.PluginIngestError("forced plugin-a rollback")
        return result

    def observe_b_mutation(plugin, *args, **kwargs):
        if Path(plugin["_repo"]) == upgraded_b:
            b_mutating.set()
        return original_install(plugin, *args, **kwargs)

    monkeypatch.setattr(pi, "assert_profile_governance", fail_a_after_mutation)
    monkeypatch.setattr(pi, "_install_skills_to_profile", observe_b_mutation)
    outcomes = {}

    def run(name, repo):
        try:
            pi.ingest(repo, audience="feishu_test", shared_home=home, force=True)
            outcomes[name] = "ok"
        except Exception as exc:  # asserted after both threads finish
            outcomes[name] = exc

    thread_a = threading.Thread(target=run, args=("a", upgraded_a))
    thread_b = threading.Thread(target=run, args=("b", upgraded_b))
    thread_a.start()
    assert a_paused.wait(2)
    thread_b.start()
    assert not b_mutating.wait(0.1)
    release_a.set()
    thread_a.join(2)
    thread_b.join(2)

    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert isinstance(outcomes["a"], pi.PluginIngestError)
    assert outcomes["b"] == "ok"
    assert "candidate-a" not in (
        home / "skills" / "a-entry" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "candidate-b" in (
        home / "skills" / "b-entry" / "SKILL.md"
    ).read_text(encoding="utf-8")
    owners = json.loads(
        (
            home
            / pi.MANAGED_DIR
            / ".locks"
            / "source-owners.json"
        ).read_text(encoding="utf-8")
    )["skills"]
    assert owners["a-entry"]["plugin_id"] == "plugin-a"
    assert owners["b-entry"]["plugin_id"] == "plugin-b"
    assert json.loads(
        (home / pi.MANAGED_DIR / "plugin-a.json").read_text(encoding="utf-8")
    )["repo"] == str(original_a)
    assert json.loads(
        (home / pi.MANAGED_DIR / "plugin-b.json").read_text(encoding="utf-8")
    )["repo"] == str(upgraded_b)


def test_profile_governance_ignores_foreign_and_undeclared_gate_docs(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug")
    manifest_path = repo / pi.PLUGIN_MANIFEST_REL
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["governance"]["approval_required"] = ["foreign gate"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    home = _shared_home(tmp_path)
    skills = home / "profiles" / "feishu_test" / "skills"
    foreign = skills / "kep-halo-cli"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("foreign gate\n", encoding="utf-8")
    (skills / "undeclared" / "SKILL.md").parent.mkdir()
    (skills / "undeclared" / "SKILL.md").write_text("foreign gate\n", encoding="utf-8")
    (skills / ".hermes-personal-installs.json").write_text(
        json.dumps({"skills": {"kep-halo-cli": {"target": str(tmp_path / "foreign")}}}),
        encoding="utf-8",
    )

    with pytest.raises(pi.PluginIngestError, match="gates not enforced"):
        pi.ingest(repo, audience="feishu_test", shared_home=home)

    assert not (home / pi.MANAGED_DIR / "test-plugin.json").exists()


def test_profile_governance_does_not_join_gate_across_skill_docs(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug")
    manifest_path = repo / pi.PLUGIN_MANIFEST_REL
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["governance"]["approval_required"] = ["approve now"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (repo / "skills" / "using-resource-delivery" / "SKILL.md").write_text(
        "ends with approve", encoding="utf-8"
    )
    (repo / "skills" / "kep-trevi-delivery-orchestrate" / "SKILL.md").write_text(
        " now starts here", encoding="utf-8"
    )

    with pytest.raises(pi.PluginIngestError, match="gates not enforced"):
        pi.ingest(repo, audience="feishu_test", shared_home=_shared_home(tmp_path))


@pytest.mark.parametrize("audience", ["all", "101"])
def test_nonprofile_governance_rejects_missing_gate_before_distribution(tmp_path, audience):
    repo = _write_plugin_repo(tmp_path / "plug")
    (repo / "skills" / "kep-trevi-delivery-orchestrate" / "SKILL.md").write_text(
        "no declared gate", encoding="utf-8"
    )
    home = _shared_home(tmp_path)
    config = home / pi.SKILL_DISTRIBUTION_FILE
    config.write_text("skills: []\n", encoding="utf-8")

    with pytest.raises(pi.PluginIngestError, match="gates not enforced"):
        pi.ingest(repo, audience=audience, shared_home=home)

    assert yaml.safe_load(config.read_text(encoding="utf-8")) == {"skills": []}
    assert not (home / pi.MANAGED_DIR / "test-plugin.json").exists()


def test_profile_ingest_rejects_shared_source_owned_by_other_plugin(tmp_path):
    first = _write_plugin_repo(tmp_path / "first")
    home = _shared_home(tmp_path)
    pi.ingest(first, audience="feishu_test", shared_home=home)
    second = _write_plugin_repo(tmp_path / "second")
    manifest_path = second / pi.PLUGIN_MANIFEST_REL
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["id"] = "other-plugin"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(pi.PluginIngestError, match="belongs to plugin 'test-plugin'"):
        pi.ingest(second, audience="feishu_test", shared_home=home)

    assert not (home / pi.MANAGED_DIR / "other-plugin.json").exists()


def test_legacy_shared_source_migration_rejects_multiple_manifest_claims(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug")
    home = _shared_home(tmp_path)
    for name in ["using-resource-delivery", "kep-trevi-delivery-orchestrate", "kep-halo-cli"]:
        shutil.copytree(repo / "skills" / name, home / "skills" / name)
    managed_dir = home / pi.MANAGED_DIR
    managed_dir.mkdir(parents=True)
    skills = ["using-resource-delivery", "kep-trevi-delivery-orchestrate", "kep-halo-cli"]
    for plugin_id in ["first-plugin", "test-plugin"]:
        (managed_dir / f"{plugin_id}.json").write_text(
            json.dumps(
                {
                    "plugin_id": plugin_id,
                    "status": "active",
                    "skills": skills,
                    "audience": {"mode": "profile", "profiles": ["feishu_test"]},
                }
            ),
            encoding="utf-8",
        )

    with pytest.raises(pi.PluginIngestError, match="ambiguous legacy owners"):
        pi.ingest(repo, audience="feishu_test", shared_home=home, force=True)

    assert json.loads((managed_dir / "first-plugin.json").read_text(encoding="utf-8"))["status"] == "active"
    assert json.loads((managed_dir / "test-plugin.json").read_text(encoding="utf-8"))["status"] == "active"


@pytest.mark.parametrize("invalid_kind", ["corrupt", "duplicate-id"])
def test_legacy_shared_source_migration_rejects_unverifiable_manifests(
    tmp_path, invalid_kind
):
    repo = _write_plugin_repo(tmp_path / "plug")
    home = _shared_home(tmp_path)
    for name in ["using-resource-delivery", "kep-trevi-delivery-orchestrate", "kep-halo-cli"]:
        shutil.copytree(repo / "skills" / name, home / "skills" / name)
    managed_dir = home / pi.MANAGED_DIR
    managed_dir.mkdir(parents=True)
    active = {
        "plugin_id": "test-plugin",
        "status": "active",
        "skills": ["using-resource-delivery", "kep-trevi-delivery-orchestrate", "kep-halo-cli"],
        "audience": {"mode": "profile", "profiles": ["feishu_test"]},
    }
    (managed_dir / "test-plugin.json").write_text(
        json.dumps(active),
        encoding="utf-8",
    )
    invalid_path = managed_dir / "unverifiable.json"
    invalid_path.write_text(
        "{" if invalid_kind == "corrupt" else json.dumps(active),
        encoding="utf-8",
    )

    with pytest.raises(pi.PluginIngestError, match="cannot verify legacy owner"):
        pi.ingest(repo, audience="feishu_test", shared_home=home, force=True)

    assert json.loads(
        (managed_dir / "test-plugin.json").read_text(encoding="utf-8")
    )["status"] == "active"


def test_legacy_shared_source_migration_allows_changed_same_plugin_upgrade(tmp_path):
    original = _write_plugin_repo(tmp_path / "original")
    home = _shared_home(tmp_path)
    pi.ingest(original, audience="feishu_test", shared_home=home)
    registry_path = home / pi.MANAGED_DIR / ".locks" / "source-owners.json"
    registry_path.unlink()

    upgraded = _write_plugin_repo(tmp_path / "upgraded")
    upgraded_skill = upgraded / "skills" / "using-resource-delivery" / "SKILL.md"
    upgraded_skill.write_text(
        upgraded_skill.read_text(encoding="utf-8") + "\nrelease-1.0.5\n",
        encoding="utf-8",
    )

    pi.ingest(upgraded, audience="feishu_test", shared_home=home, force=True)

    shared_skill = home / "skills" / "using-resource-delivery" / "SKILL.md"
    assert "release-1.0.5" in shared_skill.read_text(encoding="utf-8")
    owners = json.loads(registry_path.read_text(encoding="utf-8"))["skills"]
    assert owners["using-resource-delivery"]["plugin_id"] == "test-plugin"
    managed = json.loads(
        (home / pi.MANAGED_DIR / "test-plugin.json").read_text(encoding="utf-8")
    )
    assert managed["status"] == "active"
    assert managed["repo"] == str(upgraded)


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
    repo = _write_plugin_repo(tmp_path / "plug")
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


@pytest.mark.parametrize("value", ["all", "*", "everyone", "ALL"])
def test_resolve_audience_all_variants(tmp_path, value):
    home = _shared_home(tmp_path)
    aud = pi.resolve_audience(value, profiles_root=home / "profiles")
    assert aud.mode == "all"


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


def test_uninstall_profiles_subset_keeps_other_profile(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug")
    home = _shared_home(tmp_path, profiles=("feishu_a", "feishu_b"))
    managed_path = home / pi.MANAGED_DIR / "test-plugin.json"

    pi.ingest(repo, audience="feishu_a,feishu_b", shared_home=home)
    pi.uninstall("test-plugin", shared_home=home, profiles=["feishu_b"])

    for name in ["using-resource-delivery", "kep-trevi-delivery-orchestrate", "kep-halo-cli"]:
        assert (home / "profiles" / "feishu_a" / "skills" / name).exists()
        assert not (home / "profiles" / "feishu_b" / "skills" / name).exists()
    managed = json.loads(managed_path.read_text(encoding="utf-8"))
    assert managed["audience"]["profiles"] == ["feishu_a"]
    assert set(managed["owned_skills"]) == {"feishu_a"}

    pi.uninstall("test-plugin", shared_home=home, profiles=["feishu_a"])
    assert not managed_path.exists()


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
        pi.assert_profile_governance(
            plugin,
            empty_profile,
            ["using-resource-delivery", "kep-trevi-delivery-orchestrate"],
        )


def test_ingest_department_mode_refuses_to_create_config(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug")
    home = _shared_home(tmp_path)  # no skill-distribution.yaml
    with pytest.raises(pi.PluginIngestError, match="refusing to create"):
        pi.ingest(repo, audience="101,202", shared_home=home)


def test_ingest_all_mode_refuses_create_without_flag(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug")
    home = _shared_home(tmp_path)  # no skill-distribution.yaml
    with pytest.raises(pi.PluginIngestError, match="refusing to create"):
        pi.ingest(repo, audience="all", shared_home=home)


def test_ingest_all_mode_writes_global_distribution_and_uninstall_strips(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug")
    home = _shared_home(tmp_path)

    pi.ingest(repo, audience="all", shared_home=home, allow_create_distribution=True)

    dist = home / pi.SKILL_DISTRIBUTION_FILE
    raw = yaml.safe_load(dist.read_text(encoding="utf-8")) or {}
    entries = [it for it in raw.get("skills", []) if it.get("plugin") == "test-plugin"]
    assert {it["path"] for it in entries} == {
        "using-resource-delivery",
        "kep-trevi-delivery-orchestrate",
        "kep-halo-cli",
    }
    assert all(it["audience"] == "all" for it in entries)
    managed = json.loads((home / pi.MANAGED_DIR / "test-plugin.json").read_text(encoding="utf-8"))
    assert managed["audience"]["mode"] == "all"

    pi.uninstall("test-plugin", shared_home=home)
    raw2 = yaml.safe_load(dist.read_text(encoding="utf-8")) or {}
    assert all(it.get("plugin") != "test-plugin" for it in raw2.get("skills", []))
    assert not (home / pi.MANAGED_DIR / "test-plugin.json").exists()


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


def test_department_mode_rejects_other_plugin_same_source_path(tmp_path):
    repo = _write_plugin_repo(tmp_path / "plug")
    home = _shared_home(tmp_path)
    dist = home / pi.SKILL_DISTRIBUTION_FILE
    other = {"path": "kep-halo-cli", "install_mode": "copy",
             "audience": {"department_ids": ["999"]}, "plugin": "other-plugin"}
    dist.write_text(yaml.safe_dump({"skills": [other]}, allow_unicode=True), encoding="utf-8")

    with pytest.raises(pi.PluginIngestError, match="source collision"):
        pi.ingest(repo, audience="101", shared_home=home)

    assert yaml.safe_load(dist.read_text(encoding="utf-8")) == {"skills": [other]}
    assert json.loads(
        (home / pi.MANAGED_DIR / "test-plugin.json").read_text(encoding="utf-8")
    )["status"] == "inactive"


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


# (slash-alias distribution removed — aliases are now skill-declared frontmatter,
#  scanned per-profile by skill_slash; see test_skill_slash_aliases.py)
