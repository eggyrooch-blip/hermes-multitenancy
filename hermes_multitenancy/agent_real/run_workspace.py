"""Per-workflow run workspace: the checkout an expert is supposed to work in.

Production evidence (2026-08-26): the Server 研发专家 never got past step 0 of
`using-server-dev` in 7 sessions — no target repo on disk, so 0 code and 0 MR.
`KEP_AGENT_MODE=online` (a profile anchor, `_core._profile_anchor_env_for_aiagent`)
tells the skill it MAY clone; nothing ever did the clone.

This module is that missing step, and only that step:

    <profile_home>/workspace/runs/<workflow_id>/
        repo/            the target repo, cloned with READ-ONLY credentials
        KepSpecHub/      the spec hub the kep skills read
        .git-credentials 0600, the read-only token — never in any child env
        codex-home/      (written by codex_home.materialize, ticket 02)

Wiring — exporting :func:`env_for`, pointing the agent's cwd at ``repo/`` — is
deliberately NOT here (ticket 04 owns every env/`_core` seam). This file only
builds the directory and answers what the env should contain.

Read-only by construction: the employee's write-scoped ``GITLAB_TOKEN`` must not
reach the clone. The token used here lands in ``<wf>/.git-credentials`` (0600)
and is handed to git through ``credential.helper=store --file=...``, so no
subprocess environment — not the clone's, not the agent's — carries the literal.
"""
from __future__ import annotations

import fcntl
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, unquote, urlsplit, urlunsplit

# ponytail: importing two private helpers instead of copying them. Making them
# public would edit credential_materializer.py, which this ticket must not
# touch; a second atomic-0600-write / host-validator is how the two disagree.
from ..credential_materializer import (
    DEFAULT_GITLAB_HOST,
    _atomic_write_secret,
    _normalized_git_host,
)

#: The kep skills' spec-hub contract. Exported through :func:`env_for`.
SPEC_HUB_DIR_ENV = "KEP_SPEC_HUB_DIR"
#: The same skills also resolve the hub positionally as
#: ``$KEP_WORKSPACE_DIR/KepSpecHub``, so :func:`env_for` returns this pointed at
#: the workflow root (the profile anchor's value is the profile workspace root,
#: one level up). Whether it is exported is ticket 04's call.
WORKSPACE_DIR_ENV = "KEP_WORKSPACE_DIR"

RUNS_DIR_NAME = "runs"
REPO_DIR_NAME = "repo"
#: Exact case is the contract: the kep skills look for ``KepSpecHub`` literally,
#: and a case-insensitive dev machine will not catch a lowercase rename that
#: then fails on Linux.
SPEC_HUB_DIR_NAME = "KepSpecHub"

#: The target repo is cloned shallow but with history enough to branch/diff; the
#: spec hub is read-only reference material, so one commit is plenty.
REPO_CLONE_DEPTH = 50
# A stalled internal GitLab clone must fail before the request spends minutes
# looking alive. Healthy production smoke clones complete well inside this.
CLONE_TIMEOUT_SECONDS = 120

#: A workflow id becomes a directory name under a profile-scoped root, and its
#: sources (event metadata / channel session id) are attacker-influenced. A
#: strict whitelist — no dot, so no ``..`` can survive — is the whole guard.
_WORKFLOW_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
_WORKFLOW_ID_MAX_LEN = 64
_COMMIT_REF = re.compile(r"[0-9a-fA-F]{7,40}")

#: Network clones are pinned to the internal GitLab host by default. Local
#: paths and ``file://`` URLs exist only so tests can use a local bare repo.


class RunWorkspaceError(RuntimeError):
    """The run workspace could not be prepared — the run must fail, not start.

    Starting codex without the repo on disk is exactly the production failure
    this module exists to remove, so every path here raises instead of
    degrading.
    """


@dataclass(frozen=True)
class RunWorkspace:
    root: Path
    repo_dir: Path
    spec_hub_dir: Path
    git_credentials_file: Path | None


def workflow_id_for(event: Any) -> str:
    """The workflow this event belongs to — stable across a session's rounds.

    A workflow is one piece of work, not one turn: round 2 must find round 1's
    clone. So this resolves the identity established on the FIRST round rather
    than the per-run id minted fresh in ``_core`` for every spawn:

      1. ``metadata["workflow_id"]`` — an explicit pin, if a caller has one;
      2. ``raw_event["session_id"]`` — the channel session, i.e. the first
         round's run identity carried forward (this is the normal answer);
      3. ``metadata["run_id"]``      — single-shot runs (cron) with no session.

    Sanitized to ``[A-Za-z0-9_-]``: the result is a directory name and the
    sources are untrusted.
    """
    raw_event = getattr(event, "raw_event", None)
    raw_event = raw_event if isinstance(raw_event, dict) else {}
    metadata = raw_event.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    for candidate in (
        metadata.get("workflow_id"),
        raw_event.get("session_id"),
        metadata.get("run_id"),
    ):
        workflow_id = _safe_workflow_id(candidate)
        if workflow_id:
            return workflow_id
    raise RunWorkspaceError(
        "cannot resolve workflow_id: event carries no metadata.workflow_id, "
        "session_id or metadata.run_id"
    )


def workflow_root(profile_home: Path, workflow_id: str) -> Path:
    """Where this workflow's run dir lives. Pure path math — no mkdir, no lock.

    The one place that answers "round 2, same workflow, same directory", so a
    reader that only wants to look at round 1's leftovers (e.g. the model this
    thread pinned into ``codex-home/config.toml``) does not have to re-derive
    the layout or take :func:`bind_existing`'s side effects.
    """
    workflow_id = _safe_workflow_id(workflow_id)
    if not workflow_id:
        raise RunWorkspaceError("workflow_id is required")
    return Path(profile_home).expanduser() / "workspace" / RUNS_DIR_NAME / workflow_id


def prepare(
    profile_home: Path,
    workflow_id: str,
    *,
    repo_git_url: str,
    repo_ref: str | None = None,
    spec_hub_git_url: str,
    readonly_token: str | None = None,
) -> RunWorkspace:
    """Materialize ``<profile_home>/workspace/runs/<workflow_id>/``, idempotently.

    Create-only: an existing checkout is reused (a second round must not lose
    round 1's work), and nothing here deletes.
    """
    workflow_id = _safe_workflow_id(workflow_id)
    if not workflow_id:
        raise RunWorkspaceError("workflow_id is required")
    repo_git_url = str(repo_git_url or "").strip()
    spec_hub_git_url = str(spec_hub_git_url or "").strip()
    if not repo_git_url:
        raise RunWorkspaceError(
            "repo_git_url is required: without the target repo on disk the "
            "expert cannot pass step 0, so the run must fail instead of "
            "starting a coding runtime in an empty directory"
        )
    if not spec_hub_git_url:
        raise RunWorkspaceError("spec_hub_git_url is required")
    _assert_clonable(repo_git_url)
    _assert_clonable(spec_hub_git_url)

    root = workflow_root(profile_home, workflow_id)
    root.mkdir(parents=True, exist_ok=True)
    # mkdir's mode is ignored for parents and masked by umask; C2 says 0700.
    os.chmod(root, 0o700)

    workspace = RunWorkspace(
        root=root,
        repo_dir=root / REPO_DIR_NAME,
        spec_hub_dir=root / SPEC_HUB_DIR_NAME,
        git_credentials_file=(root / ".git-credentials") if readonly_token else None,
    )
    lock_path = root / ".prepare.lock"
    with lock_path.open("a") as prepare_lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(prepare_lock.fileno(), fcntl.LOCK_EX)
        if workspace.git_credentials_file is not None:
            _write_git_credentials(
                workspace.git_credentials_file,
                url=repo_git_url,
                token=str(readonly_token),
            )

        env = _clone_env(workspace)
        _clone(
            repo_git_url,
            workspace.repo_dir,
            depth=REPO_CLONE_DEPTH,
            ref=str(repo_ref or "").strip() or None,
            env=env,
        )
        _clone(spec_hub_git_url, workspace.spec_hub_dir, depth=1, ref=None, env=env)
    return workspace


def prepare_local(
    profile_home: Path,
    workflow_id: str,
    *,
    repo_source: Path,
    spec_hub_source: Path | None = None,
) -> RunWorkspace:
    """Clone one server-configured local repo into the normal run workspace."""
    workflow_id = _safe_workflow_id(workflow_id)
    if not workflow_id:
        raise RunWorkspaceError("workflow_id is required")
    source = Path(repo_source).expanduser().resolve(strict=True)
    if not source.is_dir() or not (source / ".git").exists():
        raise RunWorkspaceError("local Harness source must be a git repository")

    root = workflow_root(profile_home, workflow_id)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    workspace = RunWorkspace(
        root=root,
        repo_dir=root / REPO_DIR_NAME,
        spec_hub_dir=root / SPEC_HUB_DIR_NAME,
        git_credentials_file=None,
    )
    lock_path = root / ".prepare.lock"
    with lock_path.open("a") as prepare_lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(prepare_lock.fileno(), fcntl.LOCK_EX)
        _clone(
            str(source),
            workspace.repo_dir,
            depth=REPO_CLONE_DEPTH,
            ref=None,
            env=_clone_env(workspace),
        )
        _disable_local_push(workspace.repo_dir, _clone_env(workspace))
        if spec_hub_source is None:
            workspace.spec_hub_dir.mkdir(exist_ok=True)
        else:
            _clone(
                str(Path(spec_hub_source).expanduser().resolve(strict=True)),
                workspace.spec_hub_dir,
                depth=1,
                ref=None,
                env=_clone_env(workspace),
            )
    return workspace


def bind_existing(
    profile_home: Path,
    workflow_id: str,
    workspace: str | None,
) -> RunWorkspace:
    """Bind one Harness workflow to the same actor workspace Hermes resolved."""
    from ..run_models import resolve_profile_workspace

    workflow_id = _safe_workflow_id(workflow_id)
    if not workflow_id:
        raise RunWorkspaceError("workflow_id is required")
    normalized, work_dir = resolve_profile_workspace(Path(profile_home), workspace)
    root = workflow_root(profile_home, workflow_id)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    lock_path = root / ".prepare.lock"
    binding_path = root / ".workspace-binding"
    binding = normalized or ""
    with lock_path.open("a") as prepare_lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(prepare_lock.fileno(), fcntl.LOCK_EX)
        try:
            previous = binding_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            binding_path.write_text(binding, encoding="utf-8")
            os.chmod(binding_path, 0o600)
        else:
            if previous != binding:
                raise RunWorkspaceError("workspace binding changed for existing session")
    return RunWorkspace(
        root=root,
        repo_dir=work_dir,
        spec_hub_dir=work_dir,
        git_credentials_file=None,
    )


def _disable_local_push(repo_dir: Path, env: Mapping[str, str]) -> None:
    completed = subprocess.run(
        ["git", "-C", str(repo_dir), "remote", "set-url", "--push", "origin", "disabled://local-harness"],
        env=dict(env), capture_output=True, text=True, timeout=15,
    )
    if completed.returncode != 0:
        raise RunWorkspaceError("could not disable pushes from local Harness clone")


def env_for(workspace: RunWorkspace) -> dict[str, str]:
    """Child-env additions for a run bound to ``workspace``. Never a secret.

    No ``GIT_CONFIG_*`` here on purpose: ``credential_materializer.git_auth_env``
    already writes that block into the agent env, and both producers number
    their keys from ``GIT_CONFIG_COUNT``. A second independent block would
    renumber over the first and break the git auth that already works in
    production. The clone's credential helper stays scoped to the clone
    (:func:`_clone_env`).
    """
    return {
        SPEC_HUB_DIR_ENV: str(workspace.spec_hub_dir),
        WORKSPACE_DIR_ENV: str(workspace.root),
        "HERMES_RUN_WORKSPACE_DIR": str(workspace.root),
        "HERMES_RUN_REPO_DIR": str(workspace.repo_dir),
    }


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #


def _safe_workflow_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return _WORKFLOW_ID_UNSAFE.sub("-", raw)[:_WORKFLOW_ID_MAX_LEN].strip("-")


def _assert_clonable(url: str) -> None:
    safe_url = _safe_url_for_error(url)
    if any(c.isspace() or ord(c) < 0x20 for c in url):
        raise RunWorkspaceError(
            f"refusing to clone unsupported url form: {safe_url!r} "
            "(https or local file:///absolute path only)"
        )
    try:
        parsed = urlsplit(url)
        # Accessing hostname/port also validates malformed bracketed hosts and
        # ports. Most importantly, never let credentials reach an error.
        hostname = (parsed.hostname or "").lower()
        _ = parsed.port
    except ValueError as exc:
        raise RunWorkspaceError(
            f"refusing to clone malformed url: {safe_url!r}"
        ) from exc
    if parsed.username is not None or parsed.password is not None:
        raise RunWorkspaceError(
            f"refusing to clone url with userinfo: {safe_url!r}"
        )
    if parsed.query or parsed.fragment:
        raise RunWorkspaceError(
            f"refusing to clone url with query or fragment: {safe_url!r}"
        )
    if parsed.scheme == "https":
        if not hostname or hostname not in _repo_allowed_hosts():
            raise RunWorkspaceError(
                f"refusing to clone from non-allowlisted host: {safe_url!r}"
            )
        return
    if parsed.scheme == "file" and not parsed.netloc and parsed.path.startswith("/"):
        return
    # Keep bare absolute paths for the existing offline test fixtures. They
    # never invoke a network transport and are normalized before comparison.
    if not parsed.scheme and Path(url).is_absolute():
        return
    raise RunWorkspaceError(
        f"refusing to clone unsupported url form: {safe_url!r} "
        "(https or local file:///absolute path only)"
    )


def _repo_allowed_hosts() -> frozenset[str]:
    # Reuse the existing GitLab host setting; W0 has one approved GitLab host,
    # not an open-ended URL allowlist.
    configured = _normalized_git_host(os.environ.get("GITLAB_HOST"))
    return frozenset({configured or DEFAULT_GITLAB_HOST})


def _safe_url_for_error(url: str) -> str:
    """Return a URL suitable for diagnostics, with userinfo removed."""
    try:
        parsed = urlsplit(str(url))
    except ValueError:
        return "<malformed-url>"
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    # Queries/fragments are not needed to identify a clone and may carry a
    # token-like value supplied by an untrusted event.
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _normalized_repo_url(url: str) -> str:
    """Canonical form used to prove an existing checkout is the requested one."""
    parsed = urlsplit(url)
    if parsed.scheme == "https":
        host = parsed.hostname or ""
        port = parsed.port
        netloc = host if port in (None, 443) else f"{host}:{port}"
        path = parsed.path.rstrip("/") or "/"
        return urlunsplit(("https", netloc, path, parsed.query, parsed.fragment))
    if parsed.scheme == "file":
        return Path(unquote(parsed.path)).expanduser().resolve().as_uri()
    return Path(url).expanduser().resolve().as_uri()


def _write_git_credentials(path: Path, *, url: str, token: str) -> None:
    """One ``store``-format line for the host we are about to clone from."""
    host = _normalized_git_host(urlsplit(url).netloc or "")
    # quote(): a token is a password inside a URL; '/' or '@' in it would
    # otherwise re-parse the line into a different host.
    _atomic_write_secret(path, f"https://oauth2:{quote(token, safe='')}@{host}\n")


def _clone_env(workspace: RunWorkspace) -> dict[str, str]:
    """A from-scratch env for the clone — inheriting one could leak the token.

    ``os.environ`` here holds the employee's write-scoped ``GITLAB_TOKEN`` (and
    the run's LiteLLM key). Building up instead of filtering down means a new
    secret added upstream is absent by default rather than present by accident.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        # Isolate git from the operator's own config/credentials.
        "HOME": str(workspace.root),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        # No prompt: a non-interactive run must fail loudly, not hang.
        "GIT_TERMINAL_PROMPT": "0",
    }
    if workspace.git_credentials_file is not None:
        # shlex.quote: git runs a helper string containing a space through the
        # shell, so an unquoted profile path with a space would split.
        helper = f"store --file={shlex.quote(str(workspace.git_credentials_file))}"
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": helper,
            }
        )
    return env


def _clone(
    url: str,
    dest: Path,
    *,
    depth: int,
    ref: str | None,
    env: Mapping[str, str],
) -> None:
    if dest.exists():
        _validate_existing_checkout(dest, url, env, ref=ref)
        return
    if ref and _COMMIT_REF.fullmatch(ref):
        commands = [
            [
                "git", "clone", "--depth", str(depth), "--no-checkout",
                "--no-single-branch", "--", url, str(dest),
            ],
        ]
        if len(ref) == 40:
            commands.append(
                ["git", "-C", str(dest), "fetch", "--depth", str(depth), "origin", ref]
            )
        commands.append(
            ["git", "-C", str(dest), "checkout", "-q", "--detach", ref]
        )
    else:
        command = ["git", "clone", "--depth", str(depth)]
        if ref:
            command += ["--branch", ref]
        # `--` so a url or ref beginning with '-' cannot become a git option.
        commands = [command + ["--", url, str(dest)]]
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                env=dict(env),
                capture_output=True,
                text=True,
                timeout=CLONE_TIMEOUT_SECONDS,
            )
        except OSError as exc:  # git missing / not executable
            shutil.rmtree(dest, ignore_errors=True)
            raise RunWorkspaceError(f"git clone failed to start: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise RunWorkspaceError(
                f"git clone timed out after {CLONE_TIMEOUT_SECONDS} seconds: "
                f"{_safe_url_for_error(url)}"
            ) from exc
        if completed.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            # Verbatim stderr: the auditor needs git's own reason (auth, ref, DNS),
            # not our paraphrase of it.
            raise RunWorkspaceError(
                f"git clone failed ({completed.returncode}) for {_safe_url_for_error(url)}: "
                f"{(completed.stderr or completed.stdout or '').strip()}"
            )


def _validate_existing_checkout(
    dest: Path,
    requested_url: str,
    env: Mapping[str, str],
    *,
    ref: str | None,
) -> None:
    """Reuse only a healthy checkout of exactly the requested repository.

    A partial clone is evidence of a failed prior attempt; fail closed instead
    of deleting a workspace that may contain the employee's work.
    """
    if not (dest / ".git").exists():
        raise RunWorkspaceError(f"existing checkout is invalid: {dest}")
    checks = (
        ("rev-parse", "--verify", "HEAD"),
        ("remote", "get-url", "origin"),
    )
    outputs: list[str] = []
    for args in checks:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=str(dest),
                env=dict(env),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RunWorkspaceError(f"existing checkout validation failed: {dest}") from exc
        output = (completed.stdout or "").strip()
        if completed.returncode != 0 or not output:
            raise RunWorkspaceError(f"existing checkout validation failed: {dest}")
        outputs.append(output)
    if _normalized_repo_url(outputs[1]) != _normalized_repo_url(requested_url):
        raise RunWorkspaceError(
            f"existing checkout origin does not match requested url: {dest}"
        )
    if ref:
        try:
            completed = subprocess.run(
                ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
                cwd=str(dest),
                env=dict(env),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RunWorkspaceError(
                f"existing checkout requested ref validation failed: {dest}"
            ) from exc
        requested_head = (completed.stdout or "").strip()
        if completed.returncode != 0 or not requested_head:
            raise RunWorkspaceError(
                f"existing checkout requested ref validation failed: {dest}"
            )
        if requested_head != outputs[0]:
            raise RunWorkspaceError(
                f"existing checkout HEAD does not match requested ref: {dest}"
            )
