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
        runner=lambda _argv: {"returncode": 1, "stdout": "", "stderr": "unknown command"},
    )

    assert rows[0]["action"] == "quarantined"
    assert "already exists" in rows[0]["reason"]
    assert (existing / "SKILL.md").read_text(encoding="utf-8") == "curated profile skill"


def _kep_skill_runner(embedded: dict[str, dict[str, str]]):
    """Fake runner: install/update -> ok; skills list/read served from `embedded`.

    embedded maps system -> {relpath: content}; a system absent from `embedded`
    behaves like an older CLI without the `skills` command (returncode 1).
    """

    def runner(argv: list[str]) -> object:
        if len(argv) >= 3 and argv[1] == "skills":
            sub, system = argv[2], argv[3]
            files = embedded.get(system)
            if files is None:
                return {"returncode": 1, "stdout": "", "stderr": 'unknown command "skills"'}
            if sub == "list":
                return {"returncode": 0, "stdout": json.dumps(
                    [{"path": p, "size": len(c)} for p, c in files.items()]), "stderr": ""}
            if sub == "read":
                rel = argv[4] if len(argv) > 4 else "SKILL.md"
                if rel not in files:
                    return {"returncode": 1, "stdout": "", "stderr": "not found"}
                return {"returncode": 0, "stdout": files[rel], "stderr": ""}
        return {"returncode": 0, "stdout": "", "stderr": ""}

    return runner


def test_kep_cli_skill_refresh_writes_embedded_content(tmp_path: Path) -> None:
    from hermes_multitenancy.update_center import KepCliSystem, ensure_kep_cli_skills

    shared = tmp_path / ".hermes"
    (shared / "profiles" / "alice" / "skills").mkdir(parents=True)

    rows = ensure_kep_cli_skills(
        [KepCliSystem(system="hades", binary="hades-cli", target_version="1.2.0")],
        shared_home=shared,
        profiles=["alice"],
        runner=_kep_skill_runner({"hades": {
            "SKILL.md": "---\nname: kep-hades-cli\n---\nREAL EMBEDDED BODY",
            "references/api.md": "# api reference",
        }}),
    )

    src = shared / "skills" / "Keep" / "kep-hades-cli"
    assert (src / "SKILL.md").read_text(encoding="utf-8") == "---\nname: kep-hades-cli\n---\nREAL EMBEDDED BODY"
    assert (src / "references" / "api.md").read_text(encoding="utf-8") == "# api reference"
    assert (src / ".kep-cli-managed").exists()
    assert rows[0]["action"] == "ensured"
    assert (shared / "profiles" / "alice" / "skills" / "Keep" / "kep-hades-cli").is_symlink()


def test_kep_cli_skill_refresh_graceful_when_embedded_absent(tmp_path: Path) -> None:
    from hermes_multitenancy.update_center import KepCliSystem, ensure_kep_cli_skills

    shared = tmp_path / ".hermes"
    (shared / "profiles" / "alice" / "skills").mkdir(parents=True)

    # `embedded` empty -> system CLI has no `skills` command -> stub fallback, no crash.
    ensure_kep_cli_skills(
        [KepCliSystem(system="hades", binary="hades-cli", target_version="1.2.0")],
        shared_home=shared,
        profiles=["alice"],
        runner=_kep_skill_runner({}),
    )

    stub = (shared / "skills" / "Keep" / "kep-hades-cli" / "SKILL.md").read_text(encoding="utf-8")
    assert "hades-cli" in stub  # generated stub, not a crash


def test_kep_cli_skill_refresh_preserves_curated_unmarked_source(tmp_path: Path) -> None:
    from hermes_multitenancy.update_center import KepCliSystem, ensure_kep_cli_skills

    shared = tmp_path / ".hermes"
    (shared / "profiles" / "alice" / "skills").mkdir(parents=True)
    curated = shared / "skills" / "Keep" / "kep-hades-cli"
    curated.mkdir(parents=True)
    (curated / "SKILL.md").write_text("hand-curated content", encoding="utf-8")  # no marker

    ensure_kep_cli_skills(
        [KepCliSystem(system="hades", binary="hades-cli", target_version="1.2.0")],
        shared_home=shared,
        profiles=["alice"],
        runner=_kep_skill_runner({"hades": {"SKILL.md": "EMBEDDED WOULD OVERWRITE"}}),
    )

    assert (curated / "SKILL.md").read_text(encoding="utf-8") == "hand-curated content"
    assert not (curated / ".kep-cli-managed").exists()


def test_kep_cli_skill_refresh_prunes_stale_embedded_files(tmp_path: Path) -> None:
    from hermes_multitenancy.update_center import KepCliSystem, ensure_kep_cli_skills

    shared = tmp_path / ".hermes"
    (shared / "profiles" / "alice" / "skills").mkdir(parents=True)
    sys = KepCliSystem(system="hades", binary="hades-cli", target_version="1.2.0")

    # v1 embeds a references file...
    ensure_kep_cli_skills(
        [sys], shared_home=shared, profiles=["alice"],
        runner=_kep_skill_runner({"hades": {"SKILL.md": "v1", "references/old.md": "gone soon"}}),
    )
    src = shared / "skills" / "Keep" / "kep-hades-cli"
    assert (src / "references" / "old.md").exists()

    # v2 drops it -> the managed source must mirror the embedded truth (prune stale).
    ensure_kep_cli_skills(
        [sys], shared_home=shared, profiles=["alice"],
        runner=_kep_skill_runner({"hades": {"SKILL.md": "v2"}}),
    )
    assert (src / "SKILL.md").read_text(encoding="utf-8") == "v2"
    assert not (src / "references" / "old.md").exists()
    assert not (src / "references").exists()  # empty dir pruned
    assert (src / ".kep-cli-managed").exists()


def test_kep_cli_skill_refresh_surfaces_real_read_failure(tmp_path: Path) -> None:
    from hermes_multitenancy.update_center import KepCliSystem, ensure_kep_cli_skills

    shared = tmp_path / ".hermes"
    (shared / "profiles" / "alice" / "skills").mkdir(parents=True)
    # pre-existing kep-cli-managed source that must NOT be silently replaced by a stale stub
    src = shared / "skills" / "Keep" / "kep-hades-cli"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("prior embedded content", encoding="utf-8")
    (src / ".kep-cli-managed").write_text("", encoding="utf-8")

    def runner(argv: list[str]) -> object:
        if argv[1:3] == ["skills", "list"]:  # CLI advertises skills (new build)
            return {"returncode": 0, "stdout": json.dumps([{"path": "SKILL.md", "size": 9}]), "stderr": ""}
        if argv[1:3] == ["skills", "read"]:  # ...but read fails (transient/runtime) -> real error
            return {"returncode": 1, "stdout": "", "stderr": "runtime error: backend timeout"}
        return {"returncode": 0, "stdout": "", "stderr": ""}

    rows = ensure_kep_cli_skills(
        [KepCliSystem(system="hades", binary="hades-cli", target_version="1.2.0")],
        shared_home=shared, profiles=["alice"], runner=runner,
    )

    assert rows[0]["action"] == "quarantined"
    assert "skill-refresh-failed" in rows[0]["reason"]
    # existing content preserved, NOT overwritten or blanked
    assert (src / "SKILL.md").read_text(encoding="utf-8") == "prior embedded content"


def test_read_kep_cli_skill_files_rejects_path_traversal(tmp_path: Path) -> None:
    from hermes_multitenancy.update_center import read_kep_cli_skill_files

    def runner(argv: list[str]) -> object:
        if argv[1:3] == ["skills", "list"]:
            return {"returncode": 0, "stdout": json.dumps(
                [{"path": "../evil.md", "size": 1}, {"path": "/etc/passwd", "size": 1}]), "stderr": ""}
        return {"returncode": 0, "stdout": "pwned", "stderr": ""}

    # both entries are unsafe and skipped -> nothing to read -> None
    assert read_kep_cli_skill_files("hades", runner=runner) is None


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


def test_build_kep_systems_from_registry_maps_install_and_update() -> None:
    from hermes_multitenancy.update_center import build_kep_systems_from_registry

    registry = [
        {"name": "hades", "bin_name": "hades-cli", "status": "active", "installed": True},
        {"name": "dune", "bin_name": "dune-cli", "status": "active", "installed": False},
        {"name": "wip", "bin_name": "wip-cli", "status": "developing", "installed": False},
    ]

    calls: list[list[str]] = []

    def runner(argv: list[str]) -> object:
        calls.append(argv)
        return {"returncode": 0, "stdout": json.dumps(registry), "stderr": ""}

    systems = build_kep_systems_from_registry(
        runner=runner,
        version_reader=lambda name: "1.1.0" if name == "hades" else None,
    )

    assert ["kep-cli", "list", "--json"] in calls
    by_name = {s.system: s for s in systems}
    # developing filtered out by default
    assert set(by_name) == {"hades", "dune"}
    # installed system -> update candidate (target 'latest' never equals installed version)
    assert by_name["hades"].binary == "hades-cli"
    assert by_name["hades"].installed_version == "1.1.0"
    assert by_name["hades"].target_version == "latest"
    assert by_name["hades"].needs_update is True
    # newly registered system -> install candidate
    assert by_name["dune"].installed_version is None
    assert by_name["dune"].needs_install is True


def test_build_kep_systems_from_registry_include_developing_excludes_other_statuses() -> None:
    from hermes_multitenancy.update_center import build_kep_systems_from_registry

    registry = [
        {"name": "act", "bin_name": "act-cli", "status": "active", "installed": False},
        {"name": "dev", "bin_name": "dev-cli", "status": "developing", "installed": False},
        {"name": "dead", "bin_name": "dead-cli", "status": "deprecated", "installed": False},
    ]

    def runner(_argv: list[str]) -> object:
        return {"returncode": 0, "stdout": json.dumps(registry), "stderr": ""}

    # include_developing must add developing ONLY, never deprecated/disabled rows.
    names = {s.system for s in build_kep_systems_from_registry(runner=runner, include_developing=True)}
    assert names == {"act", "dev"}
    default = {s.system for s in build_kep_systems_from_registry(runner=runner)}
    assert default == {"act"}


def test_build_kep_systems_from_registry_raises_on_cli_failure() -> None:
    from hermes_multitenancy.update_center import build_kep_systems_from_registry

    def runner(_argv: list[str]) -> object:
        return {"returncode": 1, "stdout": "", "stderr": "not logged in"}

    try:
        build_kep_systems_from_registry(runner=runner)
    except ValueError as exc:
        assert "kep-cli list --json failed" in str(exc)
    else:
        raise AssertionError("cli failure should raise")


def test_kep_sync_cli_from_registry_uses_live_manifest(monkeypatch, tmp_path: Path) -> None:
    from hermes_multitenancy import update_center_cli
    from hermes_multitenancy.update_center import KepCliSystem

    shared = tmp_path / ".hermes"
    (shared / "profiles" / "alice").mkdir(parents=True)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        update_center_cli,
        "build_kep_systems_from_registry",
        lambda **kwargs: [KepCliSystem(system="asgard", binary="asgard-cli", target_version="latest")],
    )

    def fake_sync(**kwargs):
        captured.update(kwargs)
        return {"systems": [], "skills": []}

    monkeypatch.setattr(update_center_cli, "sync_kep_cli_systems", fake_sync)

    rc = update_center_cli.main(["--shared-home", str(shared), "kep-sync", "--from-registry"])

    assert rc == 0
    assert [s.system for s in captured["systems"]] == ["asgard"]


def test_kep_sync_cli_exit_code_reflects_skill_refresh_failure(monkeypatch, tmp_path: Path) -> None:
    from hermes_multitenancy import update_center_cli

    shared = tmp_path / ".hermes"
    (shared / "profiles" / "alice").mkdir(parents=True)

    def fake_sync(**_kwargs):
        return {"systems": [{"action": "updated"}], "skills": [
            {"skill_path": "Keep/kep-hades-cli", "action": "quarantined", "reason": "skill-refresh-failed: read timeout"},
        ]}

    monkeypatch.setattr(update_center_cli, "build_kep_systems_from_registry", lambda **_k: [])
    monkeypatch.setattr(update_center_cli, "sync_kep_cli_systems", fake_sync)
    rc = update_center_cli.main(["--shared-home", str(shared), "kep-sync", "--from-registry"])
    assert rc == 2  # real skill mirror failure fails the run

    # benign profile-guard quarantine ("already exists") must NOT fail the run
    def fake_sync_benign(**_kwargs):
        return {"systems": [{"action": "updated"}], "skills": [
            {"skill_path": "Keep/kep-hades-cli", "action": "quarantined", "reason": "profile skill already exists"},
        ]}

    monkeypatch.setattr(update_center_cli, "sync_kep_cli_systems", fake_sync_benign)
    assert update_center_cli.main(["--shared-home", str(shared), "kep-sync", "--from-registry"]) == 0


def test_kep_sync_cli_requires_a_manifest_source(tmp_path: Path) -> None:
    from hermes_multitenancy import update_center_cli

    shared = tmp_path / ".hermes"
    rc = update_center_cli.main(["--shared-home", str(shared), "kep-sync"])

    assert rc == 1


def test_pyproject_exposes_update_center_cli() -> None:
    import tomllib

    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert (
        data["project"]["scripts"]["hermes-multitenancy-update-center"]
        == "hermes_multitenancy.update_center_cli:main"
    )
