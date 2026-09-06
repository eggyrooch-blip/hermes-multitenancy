"""Per-workflow ``CODEX_HOME`` materialization for the Codex app-server runtime.

Contract C2/C3 of ``.ftask/harness-base-codex-w0``. A research-expert run that is
mapped to ``api_mode="codex_app_server"`` must NOT inherit the operator's personal
``~/.codex`` (their ChatGPT auth, their ``danger-full-access`` sandbox, their
plugins). Instead every workflow gets its own throwaway ``CODEX_HOME`` that
pins four things:

1. the model provider is the company LiteLLM gateway, spoken over the
   ``responses`` wire API, with the key read from an env var (``env_key``) so the
   employee key never lands on disk — see C4;
2. the sandbox is ``workspace-write`` with ``network_access = false``, so the
   model-driven shell loop can edit the cloned repo but cannot exfiltrate;
3. ``default_permissions = ":workspace"`` (a codex ≥0.149 built-in profile —
   verified against the 0.149.1 binary's profile table) so the app-server does
   not stall on an approval prompt nobody is there to answer;
4. the expert's ``.codex-plugin`` is present and enabled, so codex boots up
   already being that expert instead of a blank assistant.

The official runtime only ever *reads* ``CODEX_HOME`` (it passes it through as
``spawn_env["CODEX_HOME"]``, see
``agent/transports/codex_app_server.py:CodexAppServerClient.__init__``); nothing
in hermes-agent writes this directory, so we own its whole content.

Plugin wiring, as codex 0.149.1 actually resolves it (facts pulled from the
shipped binary and from the bundled ``openai-bundled`` marketplace on disk):

    <CODEX_HOME>/.agents/plugins/marketplace.json   # marketplace manifest
    <CODEX_HOME>/plugins/<plugin-name>/.codex-plugin/plugin.json

i.e. the marketplace root *is* ``CODEX_HOME`` and its manifest lists entries as
``./plugins/<plugin-name>`` — which lands the plugin on exactly the C2 path.
Because the manifest enumerates its entries explicitly, codex's own scratch dirs
under ``<CODEX_HOME>/plugins/`` (``cache/``, ``data/``, staging) are never
mistaken for plugins.

Deliberately NOT wired here: this module is pure functions over a directory.
Exporting ``CODEX_HOME`` into the agent subprocess env, the allowlist, and the
key injection are ticket 04's surface.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tomllib
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Hermes' own spawn scrub removes HERMES_*_KEY names before starting Codex;
# this alias is the one that survives and reaches Codex' env_key.
DEFAULT_ENV_KEY = "CODEX_RUNTIME_KEY"
PROVIDER_ID = "litellm"
PROVIDER_LABEL = "Keep LiteLLM"
CODEX_HOME_DIRNAME = "codex-home"
PLUGIN_MANIFEST_REL = Path(".codex-plugin") / "plugin.json"
MANAGED_HEADER = (
    "# hermes-multitenancy managed CODEX_HOME (harness-base-codex-w0 / C3).\n"
    "# Regenerated on every run of this workflow — hand edits are lost.\n"
    "# Intentionally NOT using the hermes_cli codex-runtime managed-block markers:\n"
    "# that migration targets the operator's ~/.codex, and a second\n"
    "# `default_permissions` here would make this file unparseable.\n"
)

# Plugin content we always take when present, whether or not plugin.json names it.
_ALWAYS_COPY = ("AGENTS.md", ".mcp.json")
# Never carry a repo's VCS/build spoil into the sandbox.
_COPY_IGNORE = (".git", ".hg", "node_modules", "__pycache__", "*.pyc", ".env", ".env.*")

_DIR_MODE = 0o700
_RO_FILE_MODE = 0o500
_CONFIG_MODE = 0o600


class CodexPluginMissing(RuntimeError):
    """The expert's plugin dir has no ``.codex-plugin/plugin.json``.

    Loud on purpose: a codex run with a silently absent plugin looks healthy and
    answers as a generic assistant, which is exactly the failure the ticket is
    trying to make impossible.
    """


# ───────────────────────────── toml rendering ─────────────────────────────

def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    # TOML basic strings share JSON's escaping rules for everything we emit.
    return json.dumps(str(value), ensure_ascii=False)


def _normalize_base_url(base_url: str) -> str:
    """Return the gateway base with exactly one ``/v1`` suffix.

    ``wire_api = "responses"`` makes codex POST ``<base_url>/responses``, so the
    version segment has to be part of the base. Callers hand us either
    ``https://host`` (raw ``OPENAI_BASE_URL``) or ``https://host/v1`` (already
    resolved from a profile) — normalize instead of trusting either.
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("codex_home.materialize: base_url must not be empty")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def render_config(
    *,
    codex_home: Path,
    base_url: str,
    model: str,
    env_key: str = DEFAULT_ENV_KEY,
    developer_instructions: str | None = None,
) -> str:
    """Render the whole ``config.toml``. Root keys first — TOML has no way back
    to the document root once a table header is open."""
    model = str(model or "").strip()
    if not model:
        raise ValueError("codex_home.materialize: model must not be empty")
    env_key = str(env_key or "").strip()
    if not env_key:
        raise ValueError("codex_home.materialize: env_key must not be empty")

    lines = [
        MANAGED_HEADER,
        f"model_provider = {_toml_value(PROVIDER_ID)}",
        f"model = {_toml_value(model)}",
        *(
            [f"developer_instructions = {_toml_value(developer_instructions)}"]
            if developer_instructions
            else []
        ),
        'default_permissions = ":workspace"',
        'sandbox_mode = "workspace-write"',
        "",
        "[features]",
        "plugins = false",
        "remote_plugin = false",
        "",
        f"[model_providers.{PROVIDER_ID}]",
        f"name = {_toml_value(PROVIDER_LABEL)}",
        f"base_url = {_toml_value(_normalize_base_url(base_url))}",
        'wire_api = "responses"',
        "requires_openai_auth = false",
        f"env_key = {_toml_value(env_key)}",
        "",
        "[sandbox_workspace_write]",
        "network_access = false",
    ]
    return "\n".join(lines) + "\n"


# ───────────────────────────── plugin copying ─────────────────────────────

def _safe_component(value: Any, *, kind: str) -> str:
    name = str(value or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name or name.startswith("-"):
        raise CodexPluginMissing(f"codex plugin {kind} is unusable as a path component: {value!r}")
    return name


def read_plugin_manifest(plugin_dir: Path) -> dict[str, Any]:
    """Load ``<plugin_dir>/.codex-plugin/plugin.json`` or raise CodexPluginMissing."""
    manifest_path = Path(plugin_dir) / PLUGIN_MANIFEST_REL
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CodexPluginMissing(f"missing {manifest_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexPluginMissing(f"unreadable {manifest_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CodexPluginMissing(f"{manifest_path} must contain a JSON object")
    return data


def _referenced_components(manifest: Any) -> set[str]:
    """Top-level plugin-dir entries the manifest points at.

    codex manifests reference bundled content by repo-relative path — ``skills``
    (``"./skills/"``), ``hooks``, ``mcpServers``, ``apps``, and the
    ``interface.logo`` / ``composerIcon`` / ``screenshots`` assets. Copying the
    named top-level entries (rather than the whole repo) keeps unrelated repo
    content — other harnesses' configs, build output, stray dotfiles — out of the
    sandbox.
    """
    found: set[str] = set()
    stack: list[Any] = [manifest]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str):
            raw = node.strip()
            if not raw.startswith("./"):
                continue  # absolute paths, URLs and plain labels are not bundled content
            parts = [p for p in raw[2:].split("/") if p and p != "."]
            if not parts or ".." in parts:
                continue
            found.add(parts[0])
    return found


def _copy_into(src: Path, dest: Path, *, plugin_root: Path) -> None:
    # Containment check at the trust boundary: the manifest decides what gets
    # copied, so a crafted "./../../etc" style entry must not read outside the
    # plugin. Mirrors expert_overlay._read_agent_md's commonpath guard.
    resolved = src.resolve()
    root = plugin_root.resolve()
    if os.path.commonpath([str(resolved), str(root)]) != str(root):
        logger.warning("[multitenancy] codex plugin entry escapes plugin root, skipped: %s", src)
        return
    if src.is_dir():
        shutil.copytree(src, dest, symlinks=False, ignore=shutil.ignore_patterns(*_COPY_IGNORE))
    else:
        dest.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        shutil.copy2(src, dest)


def _harden(root: Path) -> None:
    """Dirs 0700, files 0500 (read-only for the sandboxed run).

    Dirs stay writable by us on purpose: unlinking a file needs write permission
    on its *directory*, not on the file, so a later rebuild can still rmtree
    read-only content without a chmod dance.
    """
    for dirpath, _dirnames, filenames in os.walk(root):
        os.chmod(dirpath, _DIR_MODE)
        for name in filenames:
            try:
                os.chmod(os.path.join(dirpath, name), _RO_FILE_MODE)
            except OSError:
                logger.debug("[multitenancy] chmod failed for %s/%s", dirpath, name, exc_info=True)


def _materialize_plugin(codex_home: Path, plugin_dir: Path) -> str:
    """Copy the auditable plugin and expose its skills without plugin startup."""
    plugin_dir = Path(plugin_dir).expanduser()
    manifest = read_plugin_manifest(plugin_dir)
    name = _safe_component(manifest.get("name"), kind="name")
    _safe_component(manifest.get("version"), kind="version")
    dest = codex_home / "plugins" / name

    # ponytail: idempotent by reuse, not by content hash — a workflow's plugin is
    # pinned for the life of the workflow (R4: build only, never destroy). If the
    # plugin is ever expected to change mid-workflow, key the dir on a manifest
    # digest instead.
    if not (dest / PLUGIN_MANIFEST_REL).is_file():
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        wanted = {".codex-plugin"} | _referenced_components(manifest)
        wanted.update(entry for entry in _ALWAYS_COPY if (plugin_dir / entry).exists())
        for entry in sorted(wanted):
            src = plugin_dir / entry
            if not src.exists():
                logger.warning(
                    "[multitenancy] codex plugin %s references missing entry %s", name, entry
                )
                continue
            _copy_into(src, dest / entry, plugin_root=plugin_dir)
        if not (dest / PLUGIN_MANIFEST_REL).is_file():
            raise CodexPluginMissing(
                f"codex plugin {name}: {PLUGIN_MANIFEST_REL} did not land in {dest}"
            )
        _harden(dest)

    skills_src = plugin_dir / "skills"
    if skills_src.is_dir():
        skills_dest = codex_home / "skills"
        for skill in skills_src.iterdir():
            if not skill.is_dir():
                continue
            skill_name = _safe_component(skill.name, kind="skill name")
            _validate_codex_skill(skill, skill_name)
            target = skills_dest / skill_name
            if not target.exists():
                _copy_into(skill, target, plugin_root=plugin_dir)
        if skills_dest.exists():
            _harden(skills_dest)
    return name


def _validate_codex_skill(skill: Path, name: str) -> None:
    try:
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
    except OSError as exc:
        raise CodexPluginMissing(f"codex skill {name} has no readable SKILL.md") from exc
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise CodexPluginMissing(f"codex skill {name} is missing YAML frontmatter")
    frontmatter = text[4 : text.index("\n---\n", 4)]
    if not any(line.startswith("name:") for line in frontmatter.splitlines()) or not any(
        line.startswith("description:") for line in frontmatter.splitlines()
    ):
        raise CodexPluginMissing(f"codex skill {name} frontmatter needs name and description")


# ────────────────────────────── entry point ──────────────────────────────

def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    tmp = path.parent / f".{path.name}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _CONFIG_MODE)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, _CONFIG_MODE)
    os.replace(tmp, path)


def read_config_model(codex_home: Path | str) -> str:
    """The model a previously materialized ``config.toml`` is pinned to.

    Returns ``""`` when there is no readable, parseable config with a model —
    the caller must fail closed on that, never guess a model.
    """
    try:
        text = (Path(codex_home).expanduser() / "config.toml").read_text(encoding="utf-8")
        return str(tomllib.loads(text).get("model") or "").strip()
    except (OSError, tomllib.TOMLDecodeError, TypeError):
        return ""


def codex_home_for(workflow_dir: Path | str) -> Path:
    """Where this workflow's ``CODEX_HOME`` lives (C2). Pure path math."""
    return Path(workflow_dir).expanduser() / CODEX_HOME_DIRNAME


def materialize(
    workflow_dir: Path | str,
    *,
    base_url: str,
    model: str,
    plugin_dir: Path | str | None,
    env_key: str = DEFAULT_ENV_KEY,
    developer_instructions: str | None = None,
) -> Path:
    """Build ``<workflow_dir>/codex-home/`` and return it (C8 signature).

    Idempotent: re-running for the same workflow reuses the plugin copy and
    re-renders ``config.toml``. ``plugin_dir=None`` means "no expert plugin" —
    a bare-but-configured codex home. A ``plugin_dir`` that has no
    ``.codex-plugin/plugin.json`` raises :class:`CodexPluginMissing`; it is never
    downgraded to the no-plugin case.
    """
    codex_home = codex_home_for(workflow_dir)
    codex_home.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    os.chmod(codex_home, _DIR_MODE)

    plugin_name: str | None = None
    if plugin_dir is not None:
        plugin_name = _materialize_plugin(codex_home, Path(plugin_dir))

    _write_atomic(
        codex_home / "config.toml",
        render_config(
            codex_home=codex_home,
            base_url=base_url,
            model=model,
            env_key=env_key,
            developer_instructions=developer_instructions,
        ),
    )
    logger.info(
        "[multitenancy] codex home materialized dir=%s model=%s env_key=%s plugin=%s",
        codex_home,
        model,
        env_key,
        plugin_name or "-",
    )
    return codex_home
