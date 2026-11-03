"""The run workspace is the missing step 0: the repo must be ON DISK.

2026-08-26 production: the Server 研发专家 ran 7 sessions and never got past
step 0 of `using-server-dev` — 0 code, 0 MR. `KEP_AGENT_MODE=online` said "you
may clone"; no component ever cloned anything, and the expert had no spec hub
either. These tests pin the three things that were missing:

  * the target repo is cloned into ``<wf>/repo`` with a HEAD and the right remote;
  * ``KEP_SPEC_HUB_DIR`` names a real ``<wf>/KepSpecHub`` checkout, and
    ``$KEP_WORKSPACE_DIR/KepSpecHub`` — how the kep skills resolve it
    positionally — lands on the same directory;
  * the credential doing the clone is read-only and lives in a 0600 file — it is
    absent from every subprocess environment, the agent's included, because the
    agent runs generated code inside that very checkout.

Clones run against local bare repos, so the suite is offline and deterministic.
"""
from __future__ import annotations

import os
import stat
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_multitenancy.agent_real import run_workspace
from hermes_multitenancy.agent_real.run_workspace import (
    RunWorkspaceError,
    env_for,
    prepare,
    prepare_local,
    workflow_id_for,
)


TOKEN = "glpat-readonly-w00"


def _git(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(cwd or Path.cwd()),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )
    return completed.stdout.strip()


@pytest.fixture
def bare_repo(tmp_path_factory):
    """A local bare repo whose ``main`` and ``release`` commits differ."""

    def _make(name: str) -> Path:
        root = tmp_path_factory.mktemp(name)
        work = root / "work"
        work.mkdir()
        _git("init", "-q", "-b", "main", ".", cwd=work)
        (work / "README.md").write_text(name, encoding="utf-8")
        _git("add", "README.md", cwd=work)
        _git("commit", "-qm", "init", cwd=work)
        _git("branch", "release", cwd=work)
        (work / "README.md").write_text(f"{name} main", encoding="utf-8")
        _git("commit", "-qam", "main moves on", cwd=work)
        bare = root / f"{name}.git"
        _git("clone", "-q", "--bare", str(work), str(bare), cwd=work)
        return bare

    return _make


@pytest.fixture
def repos(bare_repo):
    return SimpleNamespace(repo=bare_repo("mall"), hub=bare_repo("kepspechub"))


# --------------------------------------------------------------------------- #
# workflow_id — a directory name built from untrusted channel input
# --------------------------------------------------------------------------- #


def _event(**raw):
    return SimpleNamespace(raw_event=raw)


def test_workflow_id_prefers_explicit_pin_then_session_then_run_id():
    assert (
        workflow_id_for(
            _event(session_id="sess-1", metadata={"workflow_id": "wf-pin", "run_id": "r"})
        )
        == "wf-pin"
    )
    # The normal case: round 2 of the same session resolves round 1's id, so it
    # finds round 1's clone instead of cloning into a fresh directory.
    assert workflow_id_for(_event(session_id="sess-1", metadata={"run_id": "r"})) == "sess-1"
    assert workflow_id_for(_event(metadata={"run_id": "cron-42"})) == "cron-42"


def test_workflow_id_cannot_escape_the_runs_directory():
    assert "/" not in workflow_id_for(_event(session_id="../../etc/passwd"))
    assert workflow_id_for(_event(session_id="a/../b")) == "a-b"
    # A candidate that is nothing BUT traversal sanitizes to empty — fail closed
    # rather than invent a directory name.
    with pytest.raises(RunWorkspaceError):
        workflow_id_for(_event(session_id=".."))


def test_workflow_id_without_any_candidate_fails_loudly():
    with pytest.raises(RunWorkspaceError):
        workflow_id_for(_event(metadata={}))
    with pytest.raises(RunWorkspaceError):
        workflow_id_for(SimpleNamespace())


# --------------------------------------------------------------------------- #
# prepare — the clone actually happens
# --------------------------------------------------------------------------- #


def test_prepare_clones_repo_and_spec_hub(tmp_path, repos):
    workspace = prepare(
        tmp_path / "profile",
        "wf-1",
        repo_git_url=str(repos.repo),
        repo_ref=None,
        spec_hub_git_url=str(repos.hub),
        readonly_token=TOKEN,
    )

    assert workspace.root == tmp_path / "profile" / "workspace" / "runs" / "wf-1"
    assert workspace.repo_dir == workspace.root / "repo"
    # Exact case: the kep skills look for `KepSpecHub` literally.
    assert workspace.spec_hub_dir == workspace.root / "KepSpecHub"
    assert "KepSpecHub" in [child.name for child in workspace.root.iterdir()]
    # The failure this whole module exists to remove: no HEAD, no step 0.
    assert _git("rev-parse", "HEAD", cwd=workspace.repo_dir)
    assert _git("remote", "get-url", "origin", cwd=workspace.repo_dir) == str(repos.repo)
    assert _git("rev-parse", "HEAD", cwd=workspace.spec_hub_dir)
    assert stat.S_IMODE(workspace.root.stat().st_mode) == 0o700


def test_prepare_local_disables_push_to_source_repository(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git("init", "-q", "-b", "main", ".", cwd=source)
    (source / "README.md").write_text("source", encoding="utf-8")
    _git("add", "README.md", cwd=source)
    _git("commit", "-qm", "init", cwd=source)

    workspace = prepare_local(tmp_path / "profile", "wf-local", repo_source=source)

    assert _git("remote", "get-url", "--push", "origin", cwd=workspace.repo_dir) == "disabled://local-harness"


def test_prepare_checks_out_the_requested_ref(tmp_path, repos):
    workspace = prepare(
        tmp_path / "profile",
        "wf-ref",
        repo_git_url=str(repos.repo),
        repo_ref="release",
        spec_hub_git_url=str(repos.hub),
    )
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=workspace.repo_dir) == "release"


@pytest.mark.parametrize("sha_length", [7, 40])
def test_prepare_checks_out_a_requested_commit_sha(
    tmp_path, repos, monkeypatch, sha_length
):
    full_sha = _git("--git-dir", str(repos.repo), "rev-parse", "release^{commit}")
    requested = full_sha[:sha_length]
    repo_url = repos.repo.as_uri()
    monkeypatch.setattr(run_workspace, "REPO_CLONE_DEPTH", 1)

    workspace = prepare(
        tmp_path / "profile",
        f"wf-commit-{sha_length}",
        repo_git_url=repo_url,
        repo_ref=requested,
        spec_hub_git_url=str(repos.hub),
    )

    assert _git("rev-parse", "HEAD", cwd=workspace.repo_dir) == full_sha
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=workspace.repo_dir) == "HEAD"
    assert _git("remote", "get-url", "origin", cwd=workspace.repo_dir) == repo_url
    assert _git("rev-parse", "--is-shallow-repository", cwd=workspace.repo_dir) == "true"
    assert _git("rev-list", "--count", "HEAD", cwd=workspace.repo_dir) == "1"

    marker = workspace.repo_dir / "round-1-work.txt"
    marker.write_text("keep", encoding="utf-8")
    reused = prepare(
        tmp_path / "profile",
        f"wf-commit-{sha_length}",
        repo_git_url=repo_url,
        repo_ref=requested,
        spec_hub_git_url=str(repos.hub),
    )
    assert reused == workspace
    assert marker.read_text(encoding="utf-8") == "keep"
    assert _git("rev-parse", "HEAD", cwd=reused.repo_dir) == full_sha


def test_prepare_reuses_an_existing_checkout(tmp_path, repos, monkeypatch):
    first = prepare(
        tmp_path / "profile",
        "wf-2",
        repo_git_url=str(repos.repo),
        repo_ref=None,
        spec_hub_git_url=str(repos.hub),
    )
    marker = first.repo_dir / "round-1-work.txt"
    marker.write_text("round 1", encoding="utf-8")

    real_run = subprocess.run

    def _no_clone(command, *args, **kwargs):  # pragma: no cover - must not run
        if command[:2] == ["git", "clone"]:
            raise AssertionError("round 2 re-cloned and would have lost round 1's work")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _no_clone)
    second = prepare(
        tmp_path / "profile",
        "wf-2",
        repo_git_url=str(repos.repo),
        repo_ref=None,
        spec_hub_git_url=str(repos.hub),
    )
    assert second == first
    assert marker.read_text(encoding="utf-8") == "round 1"


def test_prepare_rejects_existing_checkout_at_a_different_requested_ref(tmp_path, repos):
    first = prepare(
        tmp_path / "profile",
        "wf-ref-reuse",
        repo_git_url=str(repos.repo),
        repo_ref=None,
        spec_hub_git_url=str(repos.hub),
    )
    original_head = _git("rev-parse", "HEAD", cwd=first.repo_dir)
    release_head = _git("--git-dir", str(repos.repo), "rev-parse", "release^{commit}")
    assert original_head != release_head

    with pytest.raises(RunWorkspaceError, match="requested ref"):
        prepare(
            tmp_path / "profile",
            "wf-ref-reuse",
            repo_git_url=str(repos.repo),
            repo_ref=release_head,
            spec_hub_git_url=str(repos.hub),
        )

    # Reuse validation must never mutate round 1's checkout to satisfy round 2.
    assert _git("rev-parse", "HEAD", cwd=first.repo_dir) == original_head


def test_prepare_rejects_existing_checkout_with_wrong_origin(tmp_path, repos):
    prepare(
        tmp_path / "profile",
        "wf-origin",
        repo_git_url=str(repos.repo),
        spec_hub_git_url=str(repos.hub),
    )
    other = repos.repo.parent / "other.git"
    _git("clone", "-q", "--bare", str(repos.hub), str(other), cwd=repos.hub.parent)
    with pytest.raises(RunWorkspaceError, match="origin does not match"):
        prepare(
            tmp_path / "profile",
            "wf-origin",
            repo_git_url=str(other),
            spec_hub_git_url=str(repos.hub),
        )


def test_prepare_retries_after_failed_clone_left_no_checkout(tmp_path, repos):
    missing = tmp_path / "missing.git"
    with pytest.raises(RunWorkspaceError):
        prepare(
            tmp_path / "profile",
            "wf-retry",
            repo_git_url=str(missing),
            spec_hub_git_url=str(repos.hub),
        )
    workspace = prepare(
        tmp_path / "profile",
        "wf-retry",
        repo_git_url=str(repos.repo),
        spec_hub_git_url=str(repos.hub),
    )
    assert _git("rev-parse", "--verify", "HEAD", cwd=workspace.repo_dir)


def test_failed_commit_checkout_leaves_no_partial_repo(tmp_path, repos):
    profile = tmp_path / "profile"
    repo_dir = profile / "workspace" / "runs" / "wf-bad-commit" / "repo"

    with pytest.raises(RunWorkspaceError):
        prepare(
            profile,
            "wf-bad-commit",
            repo_git_url=repos.repo.as_uri(),
            repo_ref="deadbee",
            spec_hub_git_url=str(repos.hub),
        )

    assert not repo_dir.exists()
    workspace = prepare(
        profile,
        "wf-bad-commit",
        repo_git_url=repos.repo.as_uri(),
        repo_ref=_git("--git-dir", str(repos.repo), "rev-parse", "release^{commit}"),
        spec_hub_git_url=str(repos.hub),
    )
    assert _git("rev-parse", "--verify", "HEAD", cwd=workspace.repo_dir)


def test_overlapping_prepare_calls_leave_one_valid_checkout(tmp_path, repos, monkeypatch):
    real_run = subprocess.run
    first_clone_waiting = threading.Event()
    second_prepare_entered = threading.Event()
    release_first_clone = threading.Event()
    gate_lock = threading.Lock()
    gate_claimed = False

    def _overlap_repo_clone(command, *args, **kwargs):
        nonlocal gate_claimed
        is_repo_clone = (
            command[:2] == ["git", "clone"] and Path(command[-1]).name == "repo"
        )
        if is_repo_clone:
            with gate_lock:
                wait_here = not gate_claimed
                gate_claimed = True
            if wait_here:
                first_clone_waiting.set()
                assert second_prepare_entered.wait(timeout=5)
                assert release_first_clone.wait(timeout=5)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _overlap_repo_clone)
    kwargs = {
        "profile_home": tmp_path / "profile",
        "workflow_id": "wf-overlap",
        "repo_git_url": str(repos.repo),
        "repo_ref": None,
        "spec_hub_git_url": str(repos.hub),
        "readonly_token": TOKEN,
    }

    class _ObservedProfilePath(os.PathLike[str]):
        def __fspath__(self) -> str:
            # Called by Path(profile_home) from inside prepare, so this proves
            # the second public call overlaps the first one's paused clone.
            second_prepare_entered.set()
            return os.fspath(kwargs["profile_home"])

    def _second_prepare():
        return prepare(**{**kwargs, "profile_home": _ObservedProfilePath()})

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(prepare, **kwargs)
        assert first_clone_waiting.wait(timeout=5)
        second = pool.submit(_second_prepare)
        assert second_prepare_entered.wait(timeout=5)
        time.sleep(0.2)  # let the second reach the lock while first stays paused
        assert not second.done()
        release_first_clone.set()
        first_workspace = first.result(timeout=30)
        second_workspace = second.result(timeout=30)

    assert first_workspace == second_workspace
    assert _git("rev-parse", "--verify", "HEAD", cwd=first_workspace.repo_dir)


def test_prepare_rejects_partial_existing_checkout(tmp_path, repos):
    repo_dir = tmp_path / "profile" / "workspace" / "runs" / "wf-partial" / "repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()
    with pytest.raises(RunWorkspaceError, match="validation failed"):
        prepare(
            tmp_path / "profile",
            "wf-partial",
            repo_git_url=str(repos.repo),
            spec_hub_git_url=str(repos.hub),
        )


def test_prepare_rejects_userinfo_without_echoing_credentials(tmp_path, repos):
    for host in ("gitlab.example.com", "evil.example"):
        url = f"https://oauth2:super-secret@{host}/server/mall.git"
        with pytest.raises(RunWorkspaceError) as excinfo:
            prepare(
                tmp_path / "profile",
                "wf-userinfo",
                repo_git_url=url,
                spec_hub_git_url=str(repos.hub),
            )
        message = str(excinfo.value)
        assert "userinfo" in message
        assert "super-secret" not in message
        assert f"https://{host}/server/mall.git" in message


def test_prepare_rejects_query_credentials_without_echoing_them(tmp_path, repos):
    url = "https://gitlab.example.com/server/mall.git?private_token=DO_NOT_ECHO"

    with pytest.raises(RunWorkspaceError) as excinfo:
        prepare(
            tmp_path / "profile",
            "wf-query-secret",
            repo_git_url=url,
            spec_hub_git_url=str(repos.hub),
        )

    message = str(excinfo.value)
    assert "query or fragment" in message
    assert "DO_NOT_ECHO" not in message


def test_rejected_host_cannot_materialize_readonly_credentials(tmp_path, repos):
    token = "must-not-be-written"
    with pytest.raises(RunWorkspaceError, match="allowlisted host"):
        prepare(
            tmp_path / "profile",
            "wf-attacker-host",
            repo_git_url="https://evil.example/server/mall.git",
            spec_hub_git_url=str(repos.hub),
            readonly_token=token,
        )
    root = tmp_path / "profile" / "workspace" / "runs" / "wf-attacker-host"
    assert not (root / ".git-credentials").exists()
    assert token not in str(root)


def test_network_clone_requires_https_and_default_host(tmp_path, repos):
    for url in (
        "http://gitlab.example.com/server/mall.git",
        "https://evil.example/server/mall.git",
    ):
        with pytest.raises(RunWorkspaceError, match="clone"):
            prepare(
                tmp_path / "profile",
                "wf-network-policy",
                repo_git_url=url,
                spec_hub_git_url=str(repos.hub),
            )


def test_file_url_is_supported_for_offline_clone(tmp_path, repos):
    workspace = prepare(
        tmp_path / "profile",
        "wf-file-url",
        repo_git_url=repos.repo.as_uri(),
        spec_hub_git_url=repos.hub.as_uri(),
    )
    assert _git("rev-parse", "--verify", "HEAD", cwd=workspace.repo_dir)


# --------------------------------------------------------------------------- #
# the read-only credential never becomes an environment variable
# --------------------------------------------------------------------------- #


def test_credential_reaches_git_by_file_not_by_env(tmp_path, repos, monkeypatch):
    calls: list[tuple[list[str], dict[str, str]]] = []
    real_run = subprocess.run

    def _record(command, *args, **kwargs):
        if command[:2] == ["git", "clone"]:
            calls.append((list(command), dict(kwargs.get("env") or {})))
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _record)
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-employee-write-scope")

    workspace = prepare(
        tmp_path / "profile",
        "wf-3",
        repo_git_url=str(repos.repo),
        repo_ref=None,
        spec_hub_git_url=str(repos.hub),
        readonly_token=TOKEN,
    )

    assert len(calls) == 2
    for command, env in calls:
        assert TOKEN not in " ".join(command)
        assert not [key for key, value in env.items() if TOKEN in value]
        # The employee's write-scoped token is not inherited either.
        assert "GITLAB_TOKEN" not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_VALUE_0"].startswith("store --file=")
        assert str(workspace.git_credentials_file) in env["GIT_CONFIG_VALUE_0"]
    repo_command, hub_command = (command for command, _ in calls)
    assert repo_command[:4] == ["git", "clone", "--depth", "50"]
    assert hub_command[:4] == ["git", "clone", "--depth", "1"]
    assert "--" in repo_command  # a url starting with '-' stays a url

    credentials = workspace.git_credentials_file
    assert credentials == workspace.root / ".git-credentials"
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert TOKEN in credentials.read_text(encoding="utf-8")


def test_credentials_line_names_the_repo_host(tmp_path, monkeypatch):
    """Written before the clone, so an unreachable host still proves the shape."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "fatal: unreachable"),
    )
    with pytest.raises(RunWorkspaceError, match="fatal: unreachable"):
        prepare(
            tmp_path / "profile",
            "wf-4",
            repo_git_url="https://gitlab.example.com/server/mall.git",
            repo_ref=None,
            spec_hub_git_url="https://gitlab.example.com/server/KepSpecHub.git",
            readonly_token="tok/with@chars",
        )
    line = (tmp_path / "profile" / "workspace" / "runs" / "wf-4" / ".git-credentials").read_text(
        encoding="utf-8"
    )
    # Percent-encoded: a raw '/' or '@' would re-parse into another host.
    assert line.strip() == "https://oauth2:tok%2Fwith%40chars@gitlab.example.com"


def test_no_credentials_file_without_a_token(tmp_path, repos):
    workspace = prepare(
        tmp_path / "profile",
        "wf-5",
        repo_git_url=str(repos.repo),
        repo_ref=None,
        spec_hub_git_url=str(repos.hub),
    )
    assert workspace.git_credentials_file is None
    assert not (workspace.root / ".git-credentials").exists()


# --------------------------------------------------------------------------- #
# fail loudly — a codex run with no repo is the production bug
# --------------------------------------------------------------------------- #


def test_missing_repo_git_url_fails_instead_of_starting_a_runtime(tmp_path, repos):
    with pytest.raises(RunWorkspaceError, match="repo_git_url"):
        prepare(
            tmp_path / "profile",
            "wf-6",
            repo_git_url="  ",
            repo_ref=None,
            spec_hub_git_url=str(repos.hub),
        )
    with pytest.raises(RunWorkspaceError, match="spec_hub_git_url"):
        prepare(
            tmp_path / "profile",
            "wf-6",
            repo_git_url=str(repos.repo),
            repo_ref=None,
            spec_hub_git_url="",
        )


def test_clone_failure_carries_git_stderr(tmp_path, repos):
    with pytest.raises(RunWorkspaceError) as excinfo:
        prepare(
            tmp_path / "profile",
            "wf-7",
            repo_git_url=str(tmp_path / "nope.git"),
            repo_ref=None,
            spec_hub_git_url=str(repos.hub),
        )
    assert "git clone failed" in str(excinfo.value)
    assert "nope.git" in str(excinfo.value)


def test_clone_timeout_is_bounded_and_removes_partial_checkout(
    tmp_path, repos, monkeypatch
):
    observed = {}

    def _hang(command, *args, **kwargs):
        observed["timeout"] = kwargs.get("timeout")
        Path(command[-1]).mkdir(parents=True)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", _hang)
    repo_dir = tmp_path / "profile" / "workspace" / "runs" / "wf-timeout" / "repo"
    with pytest.raises(RunWorkspaceError, match="timed out after 120 seconds"):
        prepare(
            tmp_path / "profile",
            "wf-timeout",
            repo_git_url=str(repos.repo),
            spec_hub_git_url=str(repos.hub),
        )

    assert observed["timeout"] == 120
    assert not repo_dir.exists()


def test_command_executing_url_forms_are_refused(tmp_path, repos):
    for url in ("ext::sh -c 'touch /tmp/pwned'", "git@gitlab.example.com:s/mall.git", "-u ssh"):
        with pytest.raises(RunWorkspaceError, match="unsupported url form"):
            prepare(
                tmp_path / "profile",
                "wf-8",
                repo_git_url=url,
                repo_ref=None,
                spec_hub_git_url=str(repos.hub),
            )


# --------------------------------------------------------------------------- #
# env_for — the spec hub, and nothing secret
# --------------------------------------------------------------------------- #


def test_env_for_names_the_spec_hub_and_holds_no_secret(tmp_path, repos):
    workspace = prepare(
        tmp_path / "profile",
        "wf-9",
        repo_git_url=str(repos.repo),
        repo_ref=None,
        spec_hub_git_url=str(repos.hub),
        readonly_token=TOKEN,
    )
    env = env_for(workspace)

    assert env["KEP_SPEC_HUB_DIR"] == str(workspace.spec_hub_dir)
    assert Path(env["KEP_SPEC_HUB_DIR"]).is_dir()
    # The kep skills also resolve the hub positionally; both routes must land on
    # the same checkout or step 0 dead-ends on whichever one the skill happens
    # to use.
    assert env["KEP_WORKSPACE_DIR"] == str(workspace.root)
    assert Path(env["KEP_WORKSPACE_DIR"], "KepSpecHub") == workspace.spec_hub_dir
    assert Path(env["KEP_WORKSPACE_DIR"], "KepSpecHub").is_dir()
    assert env["HERMES_RUN_REPO_DIR"] == str(workspace.repo_dir)
    assert not [key for key, value in env.items() if TOKEN in value]
    # ticket 04 merges this into the child env; a second GIT_CONFIG_COUNT
    # producer would renumber over credential_materializer.git_auth_env's block.
    assert not [key for key in env if key.startswith("GIT_CONFIG")]


def test_module_exports_the_contract():
    assert run_workspace.SPEC_HUB_DIR_ENV == "KEP_SPEC_HUB_DIR"
    assert run_workspace.WORKSPACE_DIR_ENV == "KEP_WORKSPACE_DIR"
    assert run_workspace.SPEC_HUB_DIR_NAME == "KepSpecHub"
