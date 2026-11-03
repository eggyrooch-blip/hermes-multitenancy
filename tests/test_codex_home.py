"""Per-workflow CODEX_HOME materialization (harness-base-codex-w0, C2/C3).

A codex app-server run that inherits the operator's ``~/.codex`` gets their
ChatGPT auth, their ``danger-full-access`` sandbox and none of the expert's
identity. These tests pin the four things the generated home has to guarantee:
the provider is the company LiteLLM gateway over the ``responses`` wire API with
the key taken from an env var, the sandbox is workspace-write with no network,
approvals are pre-answered, and the expert's ``.codex-plugin`` is on disk while
its instructions/skills load without the networked plugin engine. Plus the boring-but-load-bearing properties: parseable TOML, 0700,
per-workflow isolation, idempotence, and a loud failure when the plugin is absent.
"""
from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path

import pytest

from hermes_multitenancy.agent_real import codex_home as ch

BASE_URL = "https://ai-gateway.internal.example.com"
MODEL = "gpt-5.6-terra"


def _make_plugin(root: Path, *, name: str = "keep-server-dev", extra_manifest: dict | None = None) -> Path:
    """A plugin repo shaped like the real ones (keep-resource-delivery-plugin)."""
    plugin = root / f"{name}-plugin"
    (plugin / ".codex-plugin").mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "0.1.0",
        "description": "server dev expert",
        "skills": "./skills/",
        "hooks": "./hooks/codex-hooks.json",
        "interface": {"displayName": "Keep Server Dev", "logo": "./assets/logo.png"},
    }
    manifest.update(extra_manifest or {})
    (plugin / ".codex-plugin" / "plugin.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (plugin / "skills" / "using-server-dev").mkdir(parents=True)
    (plugin / "skills" / "using-server-dev" / "SKILL.md").write_text(
        "---\nname: using-server-dev\ndescription: Server development workflow.\n---\n\n# 第 0 步",
        encoding="utf-8",
    )
    (plugin / "hooks").mkdir()
    (plugin / "hooks" / "codex-hooks.json").write_text("{}", encoding="utf-8")
    (plugin / "assets").mkdir()
    (plugin / "assets" / "logo.png").write_bytes(b"\x89PNG")
    (plugin / "AGENTS.md").write_text("you are the server dev expert", encoding="utf-8")
    # Repo spoil that must NOT follow the plugin into the sandbox.
    (plugin / ".git").mkdir()
    (plugin / ".git" / "config").write_text("[remote]", encoding="utf-8")
    (plugin / "node_modules").mkdir()
    (plugin / "node_modules" / "junk.js").write_text("//", encoding="utf-8")
    (plugin / ".env").write_text("GITLAB_TOKEN=glpat-secret", encoding="utf-8")
    (plugin / "graphify-out").mkdir()
    (plugin / "graphify-out" / "GRAPH.md").write_text("#", encoding="utf-8")
    return plugin


def _config(codex_home: Path) -> dict:
    return tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))


# ───────────────────────────── config.toml (C3) ─────────────────────────────

def test_config_pins_litellm_responses_provider_and_sealed_sandbox(tmp_path):
    home = ch.materialize(
        tmp_path / "runs" / "wf-1", base_url=BASE_URL, model=MODEL, plugin_dir=None
    )

    cfg = _config(home)
    assert cfg["model_provider"] == "litellm"
    assert cfg["model"] == MODEL
    assert "disable_response_storage" not in cfg
    assert cfg["features"] == {"plugins": False, "remote_plugin": False}
    assert cfg["default_permissions"] == ":workspace"
    assert cfg["sandbox_mode"] == "workspace-write"
    provider = cfg["model_providers"]["litellm"]
    assert provider["base_url"] == f"{BASE_URL}/v1"
    assert provider["wire_api"] == "responses"
    assert provider["requires_openai_auth"] is False
    assert provider["env_key"] == "CODEX_RUNTIME_KEY"
    assert provider["name"]
    # Default-deny egress: the model-driven shell loop must not be able to
    # exfiltrate the repo it was handed.
    assert cfg["sandbox_workspace_write"]["network_access"] is False


def test_config_has_no_duplicate_root_keys(tmp_path):
    """tomllib rejects duplicate keys outright, so a successful parse is the
    proof — but assert on the raw text too, because the ticket's real worry is
    the hermes_cli codex-runtime migration appending a second
    ``default_permissions`` into the same file."""
    home = ch.materialize(
        tmp_path / "wf", base_url=BASE_URL, model=MODEL, plugin_dir=None
    )
    text = (home / "config.toml").read_text(encoding="utf-8")
    assigned = [line.split("=", 1)[0].strip() for line in text.splitlines()
                if "=" in line and not line.lstrip().startswith("#")]
    root_keys = [k for k in assigned if k in
                 ("model_provider", "model", "default_permissions", "sandbox_mode")]
    assert sorted(root_keys) == ["default_permissions", "model", "model_provider", "sandbox_mode"]
    for marker in ("# hermes-codex-runtime-migration", "[mcp_servers"):
        assert marker not in text


@pytest.mark.parametrize(
    "case,given",
    [
        ("bare", "https://gw.example.com"),
        ("trailing-slash", "https://gw.example.com/"),
        ("with-v1", "https://gw.example.com/v1"),
        ("with-v1-slash", "https://gw.example.com/v1/"),
    ],
)
def test_base_url_always_carries_exactly_one_v1(tmp_path, case, given):
    """``wire_api="responses"`` makes codex POST ``<base_url>/responses``, so a
    base without ``/v1`` 404s and a doubled ``/v1/v1`` 404s too. Callers hand us
    either shape (raw OPENAI_BASE_URL vs a profile-resolved base)."""
    home = ch.materialize(tmp_path / case, base_url=given, model=MODEL, plugin_dir=None)
    assert _config(home)["model_providers"]["litellm"]["base_url"] == "https://gw.example.com/v1"


def test_env_key_is_overridable_and_no_key_value_is_written(tmp_path):
    home = ch.materialize(
        tmp_path / "wf", base_url=BASE_URL, model=MODEL, plugin_dir=None, env_key="OTHER_KEY"
    )
    text = (home / "config.toml").read_text(encoding="utf-8")
    assert _config(home)["model_providers"]["litellm"]["env_key"] == "OTHER_KEY"
    # C4: the key travels by env var name only; nothing key-shaped on disk.
    assert "sk-" not in text and "api_key" not in text


@pytest.mark.parametrize("bad", [{"base_url": ""}, {"model": ""}, {"env_key": ""}])
def test_empty_required_field_is_refused(tmp_path, bad):
    kwargs = {"base_url": BASE_URL, "model": MODEL, "plugin_dir": None, **bad}
    with pytest.raises(ValueError):
        ch.materialize(tmp_path / "wf", **kwargs)


# ───────────────────────────── plugin landing ─────────────────────────────

def test_expert_plugin_lands_but_runtime_uses_sealed_instructions_and_skills(tmp_path):
    plugin = _make_plugin(tmp_path / "src")
    home = ch.materialize(
        tmp_path / "wf", base_url=BASE_URL, model=MODEL, plugin_dir=plugin,
        developer_instructions="SEALED EXPERT ROLE",
    )

    dest = home / "plugins" / "keep-server-dev"
    assert (dest / ".codex-plugin" / "plugin.json").is_file()
    # Manifest-referenced bundled content follows the plugin, or the expert boots
    # with no skills and dead-ends at step 0.
    assert (dest / "skills" / "using-server-dev" / "SKILL.md").read_text(encoding="utf-8")
    assert (dest / "hooks" / "codex-hooks.json").is_file()
    assert (dest / "assets" / "logo.png").is_file()
    assert (dest / "AGENTS.md").is_file()

    cfg = _config(home)
    assert cfg["developer_instructions"] == "SEALED EXPERT ROLE"
    assert cfg["features"]["plugins"] is False
    assert "marketplaces" not in cfg and "plugins" not in cfg
    assert (home / "skills" / "using-server-dev" / "SKILL.md").is_file()
    assert not (home / ".agents").exists()


def test_plugin_runtime_cache_is_not_materialized(tmp_path):
    """The 0.149 plugin engine auto-syncs openai/plugins.git at thread startup."""
    plugin = _make_plugin(tmp_path / "src")
    home = ch.materialize(tmp_path / "wf", base_url=BASE_URL, model=MODEL, plugin_dir=plugin)

    assert not (home / "plugins" / "cache").exists()
    assert (home / "skills" / "using-server-dev" / "SKILL.md").is_file()


def test_plugin_skill_without_codex_frontmatter_is_refused(tmp_path):
    plugin = _make_plugin(tmp_path / "src")
    (plugin / "skills" / "using-server-dev" / "SKILL.md").write_text(
        "# not a Codex skill", encoding="utf-8"
    )
    with pytest.raises(ch.CodexPluginMissing, match="YAML frontmatter"):
        ch.materialize(tmp_path / "wf", base_url=BASE_URL, model=MODEL, plugin_dir=plugin)


def test_plugin_version_that_is_not_a_safe_path_component_is_refused(tmp_path):
    plugin = _make_plugin(tmp_path / "src", extra_manifest={"version": "../0.1.0"})
    with pytest.raises(ch.CodexPluginMissing):
        ch.materialize(tmp_path / "wf", base_url=BASE_URL, model=MODEL, plugin_dir=plugin)


def test_repo_spoil_and_secrets_do_not_follow_the_plugin(tmp_path):
    """Only manifest-declared content is copied. A whole-repo copytree would put
    the plugin repo's ``.env`` / ``.git`` inside a sandbox whose shell loop the
    model drives."""
    plugin = _make_plugin(tmp_path / "src")
    home = ch.materialize(tmp_path / "wf", base_url=BASE_URL, model=MODEL, plugin_dir=plugin)

    dest = home / "plugins" / "keep-server-dev"
    for spoil in (".git", "node_modules", ".env", "graphify-out"):
        assert not (dest / spoil).exists(), f"{spoil} leaked into CODEX_HOME"
    leaked = [p for p in dest.rglob("*") if p.is_file() and "glpat-secret" in p.read_bytes().decode("utf-8", "replace")]
    assert leaked == []


def test_manifest_path_escaping_the_plugin_root_is_not_copied(tmp_path):
    """The manifest decides what gets copied, so traversal has to be refused
    rather than resolved."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "host-secret.txt").write_text("do not copy me", encoding="utf-8")
    plugin = _make_plugin(tmp_path / "src", extra_manifest={"apps": "./../outside/"})

    home = ch.materialize(tmp_path / "wf", base_url=BASE_URL, model=MODEL, plugin_dir=plugin)

    copied = [p.name for p in (home / "plugins" / "keep-server-dev").rglob("*")]
    assert "host-secret.txt" not in copied
    assert "outside" not in copied


def test_missing_codex_plugin_raises_instead_of_silently_degrading(tmp_path):
    """A plugin-less codex run answers as a generic assistant and looks healthy —
    the one failure mode the ticket forbids being silent."""
    bare = tmp_path / "src" / "no-codex-plugin"
    bare.mkdir(parents=True)
    (bare / "README.md").write_text("#", encoding="utf-8")

    with pytest.raises(ch.CodexPluginMissing) as err:
        ch.materialize(tmp_path / "wf", base_url=BASE_URL, model=MODEL, plugin_dir=bare)
    # Name the missing artefact: this string is what lands in the audit log, and
    # asserting on it pins *this* guard rather than some later one tripping by luck.
    assert ".codex-plugin/plugin.json" in str(err.value)
    assert not (tmp_path / "wf" / "codex-home" / "config.toml").exists()


def test_no_plugin_requested_leaves_no_plugin_wiring(tmp_path):
    home = ch.materialize(tmp_path / "wf", base_url=BASE_URL, model=MODEL, plugin_dir=None)
    cfg = _config(home)
    assert "plugins" not in cfg and "marketplaces" not in cfg
    assert not (home / ".agents").exists()


# ──────────────────────── permissions / isolation / idempotence ────────────────────────

def test_codex_home_is_0700_and_copied_plugin_files_are_read_only(tmp_path):
    plugin = _make_plugin(tmp_path / "src")
    home = ch.materialize(tmp_path / "wf", base_url=BASE_URL, model=MODEL, plugin_dir=plugin)

    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "config.toml").stat().st_mode) == 0o600
    files = [p for p in (home / "plugins").rglob("*") if p.is_file()]
    assert files
    assert {stat.S_IMODE(p.stat().st_mode) for p in files} == {0o500}


def test_two_workflows_get_independent_homes(tmp_path):
    plugin = _make_plugin(tmp_path / "src")
    one = ch.materialize(tmp_path / "runs" / "wf-a", base_url=BASE_URL, model=MODEL, plugin_dir=plugin)
    two = ch.materialize(tmp_path / "runs" / "wf-b", base_url=BASE_URL, model="gpt-5.6-sol", plugin_dir=plugin)

    assert one != two
    assert one.parent.name == "wf-a" and two.parent.name == "wf-b"
    assert _config(one)["model"] == MODEL
    assert _config(two)["model"] == "gpt-5.6-sol"
    assert (one / "plugins" / "keep-server-dev").is_dir()
    assert (two / "plugins" / "keep-server-dev").is_dir()


def test_second_run_reuses_plugin_and_re_renders_config(tmp_path):
    """R4: build-only, never destroy. A retry inside the same workflow must not
    wipe the plugin tree, but must pick up a changed model."""
    plugin = _make_plugin(tmp_path / "src")
    wf = tmp_path / "runs" / "wf-1"
    first = ch.materialize(wf, base_url=BASE_URL, model=MODEL, plugin_dir=plugin)
    sentinel = first / "plugins" / "keep-server-dev" / "sentinel.txt"
    sentinel.write_text("survivor", encoding="utf-8")

    second = ch.materialize(wf, base_url=f"{BASE_URL}/v1", model="gpt-5.6-sol", plugin_dir=plugin)

    assert second == first
    assert sentinel.read_text(encoding="utf-8") == "survivor"
    cfg = _config(second)
    assert cfg["model"] == "gpt-5.6-sol"
    assert cfg["model_providers"]["litellm"]["base_url"] == f"{BASE_URL}/v1"
    assert cfg["features"]["plugins"] is False
    assert (second / "skills" / "using-server-dev" / "SKILL.md").is_file()


def test_codex_home_for_matches_c2_layout(tmp_path):
    assert ch.codex_home_for(tmp_path / "runs" / "wf-1") == tmp_path / "runs" / "wf-1" / "codex-home"
    home = ch.materialize(tmp_path / "runs" / "wf-1", base_url=BASE_URL, model=MODEL, plugin_dir=None)
    assert home == ch.codex_home_for(tmp_path / "runs" / "wf-1")
    assert home.parent.parent.name == "runs"


def test_plugin_name_that_is_not_a_safe_path_component_is_refused(tmp_path):
    plugin = _make_plugin(tmp_path / "src", extra_manifest={"name": "../../escape"})
    with pytest.raises(ch.CodexPluginMissing):
        ch.materialize(tmp_path / "wf", base_url=BASE_URL, model=MODEL, plugin_dir=plugin)
