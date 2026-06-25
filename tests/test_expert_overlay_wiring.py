"""Wiring tests for the expert overlay in agent_real + the broker metadata seam.

These exercise the injection seams WITHOUT spawning the real AIAgent subprocess:
  * agent_real._expert_id_for_event reads metadata.expert_id off the event
  * agent_real._compose_system_text leads with the override block, demotes SOUL,
    and is byte-stable (== soul_text) when no overlay applies
  * webui_broker_server._apply_expert_id_to_metadata honors header > metadata >
    payload precedence and rejects unsafe ids
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from hermes_multitenancy import agent_real
from hermes_multitenancy import plugin_ingest as pi
from hermes_multitenancy import webui_broker_server as broker

# reuse the hermetic repo/home builders from the sibling test module
from tests.test_expert_overlay import _plugin_repo, _shared_home, _ingest, EXPERT_ID


def _event(expert_id=None):
    metadata = {}
    if expert_id is not None:
        metadata["expert_id"] = expert_id
    return SimpleNamespace(text="hi", raw_event={"metadata": metadata})


def test_expert_id_for_event():
    assert agent_real._expert_id_for_event(_event("e1")) == "e1"
    assert agent_real._expert_id_for_event(_event()) == ""
    assert agent_real._expert_id_for_event(SimpleNamespace(raw_event=None)) == ""


def test_compose_system_text_no_overlay_is_byte_stable(tmp_path):
    soul = "你是 Hermes Agent。"
    out = agent_real._compose_system_text(_event(), tmp_path, soul)
    assert out == soul  # normal path: untouched


def test_compose_system_text_with_overlay_leads_with_override(tmp_path, monkeypatch):
    repo = _plugin_repo(tmp_path / "plug")
    shared = _shared_home(tmp_path)
    _ingest(repo, shared)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    profile_home = shared / "profiles" / "feishu_test"

    soul = "你是 Hermes Agent。"
    out = agent_real._compose_system_text(_event(EXPERT_ID), profile_home, soul)
    assert out != soul
    assert out.startswith("**Role Override:**")          # override leads (verdict B)
    assert "资源投放领域的执行型专家" in out               # persona present
    assert out.rstrip().endswith("）") or "必须遵守" in out  # MUST-OBEY tail
    assert soul in out                                    # SOUL demoted below, still present
    assert out.index("**Role Override:**") < out.index(soul)
    # SOUL.md on disk is never written by composition
    assert not (profile_home / "SOUL.md").exists()


def test_apply_expert_id_to_metadata_precedence():
    # header wins over metadata + payload
    req = SimpleNamespace(headers={broker._EXPERT_ID_HEADER: "from-header"})
    md = {"expert_id": "from-md"}
    broker._apply_expert_id_to_metadata(req, {"expert_id": "from-payload"}, md)
    assert md["expert_id"] == "from-header"

    # metadata used when no header
    req2 = SimpleNamespace(headers={})
    md2 = {"expert_id": "from-md"}
    broker._apply_expert_id_to_metadata(req2, {}, md2)
    assert md2["expert_id"] == "from-md"

    # payload fallback
    req3 = SimpleNamespace(headers={})
    md3 = {}
    broker._apply_expert_id_to_metadata(req3, {"expert_id": "from-payload"}, md3)
    assert md3["expert_id"] == "from-payload"


def test_apply_expert_id_rejects_unsafe_and_clears_blank():
    req = SimpleNamespace(headers={broker._EXPERT_ID_HEADER: "../evil"})
    md = {"expert_id": "stale"}
    broker._apply_expert_id_to_metadata(req, {}, md)
    assert "expert_id" not in md  # unsafe id dropped (and stale cleared)

    req2 = SimpleNamespace(headers={broker._EXPERT_ID_HEADER: "   "})
    md2 = {"expert_id": "stale"}
    broker._apply_expert_id_to_metadata(req2, {}, md2)
    assert "expert_id" not in md2  # blank clears → no overlay


def test_expert_skill_runtime_env_disables_inactive_expert_skills(tmp_path, monkeypatch):
    repo = _plugin_repo(tmp_path / "plug")
    other_skill = repo / "skills" / "other-private-skill"
    other_skill.mkdir(parents=True)
    (other_skill / "SKILL.md").write_text("---\nname: other-private-skill\n---\n# other\n", encoding="utf-8")
    other_agent = repo / "agents" / "other-expert.md"
    other_agent.write_text("# 其它专家\n", encoding="utf-8")
    manifest_path = repo / ".hermes-plugin" / "plugin.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["skills"]["list"].append("other-private-skill")
    data["experts"].append(
        {
            "id": "other-expert",
            "name": "其它专家",
            "agent_md": "./agents/other-expert.md",
            "skills": ["other-private-skill"],
        }
    )
    manifest_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    shared = _shared_home(tmp_path)
    _ingest(repo, shared)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    profile_home = shared / "profiles" / "feishu_test"

    no_expert = agent_real._expert_skill_runtime_env_for_event(_event(), profile_home)
    assert set(no_expert["HERMES_DISABLED_SKILLS_EXTRA"].split(",")) == {
        "using-resource-delivery",
        "kep-trevi-delivery-orchestrate",
        "other-private-skill",
    }

    active = agent_real._expert_skill_runtime_env_for_event(_event(EXPERT_ID), profile_home)
    assert active == {"HERMES_DISABLED_SKILLS_EXTRA": "other-private-skill"}

    unknown = agent_real._expert_skill_runtime_env_for_event(_event("missing-expert"), profile_home)
    assert set(unknown["HERMES_DISABLED_SKILLS_EXTRA"].split(",")) == {
        "using-resource-delivery",
        "kep-trevi-delivery-orchestrate",
        "other-private-skill",
    }


def test_expert_skill_runtime_env_fail_closed_on_manifest_error(tmp_path, monkeypatch):
    shared = _shared_home(tmp_path)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    profile_home = shared / "profiles" / "feishu_test"
    skill_root = profile_home / "skills"
    (skill_root / "private-a").mkdir(parents=True)
    (skill_root / "private-a" / "SKILL.md").write_text("---\nname: private-a\n---\n", encoding="utf-8")
    (skill_root / "private-b").mkdir(parents=True)
    (skill_root / "private-b" / "SKILL.md").write_text("---\nname: private-b\n---\n", encoding="utf-8")

    managed = shared / ".hermes-plugin-managed"
    managed.mkdir(parents=True)
    (managed / "broken.json").write_text("{not-json", encoding="utf-8")

    env = agent_real._expert_skill_runtime_env_for_event(_event(), profile_home)

    assert set(env["HERMES_DISABLED_SKILLS_EXTRA"].split(",")) == {"private-a", "private-b"}


def test_expert_skill_runtime_env_manifest_error_prefers_readable_expert_names(tmp_path, monkeypatch):
    repo = _plugin_repo(tmp_path / "plug")
    shared = _shared_home(tmp_path)
    _ingest(repo, shared)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    profile_home = shared / "profiles" / "feishu_test"

    non_expert = profile_home / "skills" / "ordinary-skill"
    non_expert.mkdir(parents=True)
    (non_expert / "SKILL.md").write_text("---\nname: ordinary-skill\n---\n", encoding="utf-8")

    managed = shared / ".hermes-plugin-managed"
    (managed / "broken.json").write_text("{not-json", encoding="utf-8")

    env = agent_real._expert_skill_runtime_env_for_event(_event(), profile_home)
    disabled = set(env["HERMES_DISABLED_SKILLS_EXTRA"].split(","))

    assert "using-resource-delivery" in disabled
    assert "kep-trevi-delivery-orchestrate" in disabled
    assert "ordinary-skill" not in disabled
