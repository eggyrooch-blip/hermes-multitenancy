from __future__ import annotations

import json
from pathlib import Path

from hermes_multitenancy import plugin_ingest as pi


def test_sanitize_update_notice_hides_user_update_commands() -> None:
    from hermes_multitenancy.update_center import sanitize_user_visible_output

    raw = """lark-cli 有新版本（v1.0.59），建议更新：

bash
Copy
lark-cli update
更新后重新打开 AI Agent 以加载最新 Skills。

正常业务输出保留。
"""

    cleaned = sanitize_user_visible_output(raw)

    assert "lark-cli update" not in cleaned
    assert "重新打开 AI Agent" not in cleaned
    assert "正常业务输出保留" in cleaned


def test_lark_cli_update_notice_becomes_internal_candidate(tmp_path: Path) -> None:
    from hermes_multitenancy.update_center import UpdateLedger, scan_lark_cli_notice

    ledger = UpdateLedger(tmp_path / "ledger.jsonl")

    result = scan_lark_cli_notice(
        "lark-cli 有新版本（v1.0.59），建议更新：\nlark-cli update\n更新后重新打开 AI Agent",
        ledger=ledger,
        current_version="v1.0.58",
    )

    assert result["candidate"]["component"] == "lark-cli"
    assert result["candidate"]["target_version"] == "v1.0.59"
    assert result["auto_apply"] is False
    assert "lark-cli update" not in json.dumps(result, ensure_ascii=False)
    events = ledger.read_events()
    assert events[-1]["event"] == "candidate_detected"
    assert events[-1]["component"] == "lark-cli"
    assert events[-1]["auto_apply"] is False


def test_kep_cli_daily_sync_installs_updates_and_syncs_shared_bin(tmp_path: Path) -> None:
    from hermes_multitenancy.update_center import KepCliSystem, UpdateLedger, sync_kep_cli_systems

    shared = tmp_path / ".hermes"
    (shared / "profiles" / "alice" / "skills").mkdir(parents=True)
    source_bin = tmp_path / "kep-systems"
    for name, body in {"hades-cli": "new-hades", "halo-cli": "new-halo"}.items():
        path = source_bin / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    calls: list[list[str]] = []

    def runner(argv: list[str]) -> object:
        calls.append(argv)
        return {"returncode": 0, "stdout": "", "stderr": ""}

    report = sync_kep_cli_systems(
        systems=[
            KepCliSystem(system="hades", binary="hades-cli", target_version="1.2.0", installed_version="1.1.0"),
            KepCliSystem(system="halo", binary="halo-cli", target_version="0.1.0", installed_version=None),
        ],
        shared_home=shared,
        ledger=UpdateLedger(tmp_path / "ledger.jsonl"),
        profiles=["alice"],
        resolve_binary=lambda binary: source_bin / binary,
        runner=runner,
    )

    assert ["kep-cli", "update", "hades"] in calls
    assert ["kep-cli", "install", "halo"] in calls
    assert (shared / "bin" / "hades-cli").read_text(encoding="utf-8") == "new-hades"
    assert (shared / "bin" / "halo-cli").read_text(encoding="utf-8") == "new-halo"
    assert {item["action"] for item in report["systems"]} == {"updated", "installed"}
    assert all(item["sha256"] for item in report["systems"])
    assert (shared / "skills" / "Keep" / "kep-hades-cli" / "SKILL.md").exists()
    assert (shared / "profiles" / "alice" / "skills" / "Keep" / "kep-hades-cli").is_symlink()
    assert {item["skill_path"] for item in report["skills"]} == {"Keep/kep-hades-cli", "Keep/kep-halo-cli"}


def test_kep_cli_sync_quarantines_failure_without_overwriting_active_bin(tmp_path: Path) -> None:
    from hermes_multitenancy.update_center import KepCliSystem, UpdateLedger, sync_kep_cli_systems

    shared = tmp_path / ".hermes"
    active = shared / "bin" / "hades-cli"
    active.parent.mkdir(parents=True, exist_ok=True)
    active.write_text("old-hades", encoding="utf-8")
    active.chmod(0o755)

    def runner(_argv: list[str]) -> object:
        return {"returncode": 1, "stdout": "", "stderr": "download failed SECRET_TOKEN"}

    report = sync_kep_cli_systems(
        systems=[KepCliSystem(system="hades", binary="hades-cli", target_version="1.2.0", installed_version="1.1.0")],
        shared_home=shared,
        ledger=UpdateLedger(tmp_path / "ledger.jsonl"),
        resolve_binary=lambda _binary: tmp_path / "missing",
        runner=runner,
    )

    assert active.read_text(encoding="utf-8") == "old-hades"
    assert report["systems"][0]["action"] == "quarantined"
    assert "SECRET_TOKEN" not in json.dumps(report, ensure_ascii=False)


def test_kep_cli_skill_sync_preserves_existing_profile_skill(tmp_path: Path) -> None:
    from hermes_multitenancy.update_center import KepCliSystem, ensure_kep_cli_skills

    shared = tmp_path / ".hermes"
    existing = shared / "profiles" / "alice" / "skills" / "Keep" / "kep-hades-cli"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("curated profile skill", encoding="utf-8")

    rows = ensure_kep_cli_skills(
        [KepCliSystem(system="hades", binary="hades-cli", target_version="1.2.0")],
        shared_home=shared,
        profiles=["alice"],
    )

    assert rows[0]["action"] == "quarantined"
    assert "already exists" in rows[0]["reason"]
    assert (existing / "SKILL.md").read_text(encoding="utf-8") == "curated profile skill"


def test_lark_cli_preflight_does_not_replace_binary(monkeypatch, tmp_path: Path) -> None:
    from hermes_multitenancy import update_center

    shared = tmp_path / ".hermes"
    binary = shared / "bin" / "lark-cli-authsidecar"
    binary.parent.mkdir(parents=True)
    binary.write_text("old-binary", encoding="utf-8")
    binary.chmod(0o755)

    monkeypatch.setattr(
        update_center,
        "lark_cli_canary_preflight",
        lambda **_kwargs: {"ready": False, "missing": ["user_uat_credential"], "secret_free": True},
    )

    report = update_center.check_lark_cli_candidate(
        shared_home=shared,
        profile_name="alice",
        open_id="ou_alice",
        target_version="v1.0.59",
        ledger=update_center.UpdateLedger(tmp_path / "ledger.jsonl"),
    )

    assert report["auto_apply"] is False
    assert report["preflight"]["ready"] is False
    assert binary.read_text(encoding="utf-8") == "old-binary"


def test_lark_cli_tool_notice_stripper_removes_reopen_agent_line() -> None:
    from hermes_multitenancy.lark_cli_tool import _strip_non_business_notices

    cleaned = _strip_non_business_notices("更新后重新打开 AI Agent 以加载最新 Skills。\n业务结果")

    assert "AI Agent" not in cleaned
    assert cleaned == "业务结果"


def _write_skill_plugin(repo: Path, *, plugin_id: str = "skill-pack", skills: list[str] | None = None, clis=None) -> Path:
    skills = skills or ["Keep/additive-skill"]
    for name in skills:
        path = repo / "skills" / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(
            f"---\nname: {Path(name).name}\ntitle: {Path(name).name}\n---\n# {Path(name).name}\n",
            encoding="utf-8",
        )
    manifest = {
        "schema": pi.SUPPORTED_SCHEMA,
        "id": plugin_id,
        "version": "1.0.0",
        "skills": {"dir": "skills", "list": skills},
        "clis": clis or [],
        "connectors": [],
        "governance": {"env_default": "pre", "approval_required": []},
    }
    mf = repo / ".hermes-plugin" / "plugin.json"
    mf.parent.mkdir(parents=True, exist_ok=True)
    mf.write_text(json.dumps(manifest), encoding="utf-8")
    return repo


def test_additive_skill_plugin_auto_applies_when_gates_pass(tmp_path: Path) -> None:
    from hermes_multitenancy.update_center import UpdateLedger, apply_additive_plugin_update

    shared = tmp_path / ".hermes"
    (shared / "profiles" / "alice" / "skills").mkdir(parents=True)
    repo = _write_skill_plugin(tmp_path / "plugin")

    report = apply_additive_plugin_update(
        repo,
        audience="alice",
        shared_home=shared,
        ledger=UpdateLedger(tmp_path / "ledger.jsonl"),
    )

    assert report["auto_apply"] is True
    assert report["applied"] is True
    assert (shared / "profiles" / "alice" / "skills" / "Keep" / "additive-skill").is_symlink()
    assert report["verification"]["profiles"]["alice"]["missing"] == []


def test_plugin_skill_removal_is_quarantined_not_auto_applied(tmp_path: Path) -> None:
    from hermes_multitenancy.update_center import UpdateLedger, apply_additive_plugin_update

    shared = tmp_path / ".hermes"
    (shared / "profiles" / "alice" / "skills").mkdir(parents=True)
    existing = {
        "plugin_id": "skill-pack",
        "skills": ["Keep/old-skill"],
        "audience": {"mode": "profile", "profiles": ["alice"]},
    }
    managed = shared / pi.MANAGED_DIR / "skill-pack.json"
    managed.parent.mkdir(parents=True)
    managed.write_text(json.dumps(existing), encoding="utf-8")
    repo = _write_skill_plugin(tmp_path / "plugin", skills=["Keep/new-skill"])

    report = apply_additive_plugin_update(
        repo,
        audience="alice",
        shared_home=shared,
        ledger=UpdateLedger(tmp_path / "ledger.jsonl"),
    )

    assert report["auto_apply"] is False
    assert report["applied"] is False
    assert report["reason"] == "skill-removal-or-rename"
    assert not (shared / "profiles" / "alice" / "skills" / "Keep" / "new-skill").exists()


def test_kep_sync_cli_defaults_to_existing_profiles(monkeypatch, tmp_path: Path) -> None:
    from hermes_multitenancy import update_center_cli

    shared = tmp_path / ".hermes"
    (shared / "profiles" / "alice").mkdir(parents=True)
    (shared / "profiles" / "bob").mkdir(parents=True)
    systems = tmp_path / "systems.json"
    systems.write_text(json.dumps([{"system": "hades", "binary": "hades-cli"}]), encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_sync(**kwargs):
        captured.update(kwargs)
        return {"systems": [], "skills": []}

    monkeypatch.setattr(update_center_cli, "sync_kep_cli_systems", fake_sync)

    rc = update_center_cli.main(["--shared-home", str(shared), "kep-sync", "--systems-file", str(systems)])

    assert rc == 0
    assert list(captured["profiles"]) == ["alice", "bob"]


def test_apply_plugin_cli_calls_additive_gate(monkeypatch, tmp_path: Path) -> None:
    from hermes_multitenancy import update_center_cli

    shared = tmp_path / ".hermes"
    repo = tmp_path / "plugin"
    captured: dict[str, object] = {}

    def fake_apply(repo_arg, **kwargs):
        captured["repo"] = repo_arg
        captured.update(kwargs)
        return {"auto_apply": True, "applied": True}

    monkeypatch.setattr(update_center_cli, "apply_additive_plugin_update", fake_apply)

    rc = update_center_cli.main(
        ["--shared-home", str(shared), "apply-plugin", str(repo), "--audience", "alice"]
    )

    assert rc == 0
    assert captured["repo"] == repo
    assert captured["audience"] == "alice"


def test_systems_file_rejects_unknown_keys(tmp_path: Path) -> None:
    from hermes_multitenancy.update_center_cli import _load_systems

    systems = tmp_path / "systems.json"
    systems.write_text(json.dumps([{"system": "hades", "binary": "hades-cli", "extra": True}]), encoding="utf-8")

    try:
        _load_systems(systems)
    except ValueError as exc:
        assert "unknown key" in str(exc)
    else:
        raise AssertionError("unknown keys should be rejected")


def test_pyproject_exposes_update_center_cli() -> None:
    import tomllib

    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert (
        data["project"]["scripts"]["hermes-multitenancy-update-center"]
        == "hermes_multitenancy.update_center_cli:main"
    )
