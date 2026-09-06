"""Commit identity must be present in the profile-pivoted runtime env.

2026-08-26 guide-repo session: the pivoted HOME has no ``~/.gitconfig``, so the
first ``git commit`` died with "Please tell me who you are". The agent broke
flow mid-commit, asked the user for a name/email, and improvised an address the
user then had to vouch for.

Fix: :func:`credential_materializer.git_identity_env` seeds ``user.name`` /
``user.email`` through the same ``GIT_CONFIG_*`` env-only channel as
``git_auth_env``, FIRST in the same producer dict (a separate producer would
collide on ``GIT_CONFIG_COUNT``/``KEY_0`` at merge time). The address is the
GitLab noreply convention — attributable to the profile without impersonating a
real mailbox nobody authorized.

The real-git test drives the actual ``git`` binary in an isolated HOME (exactly
like the pivoted runtime): without the fix the commit either fails outright or
carries auto-detected machine junk; with it the author is exactly the injected
identity.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_multitenancy.credential_materializer import git_auth_env, git_identity_env


PROFILE = "dengwenhui"
IDENTITY = "dengwenhui <dengwenhui@users.noreply.gitlab.example.com>"


def _config_pairs(env: dict[str, str]) -> list[tuple[str, str]]:
    count = int(env["GIT_CONFIG_COUNT"])
    return [
        (env[f"GIT_CONFIG_KEY_{index}"], env[f"GIT_CONFIG_VALUE_{index}"])
        for index in range(count)
    ]


# --------------------------------------------------------------------------- #
# the helper itself
# --------------------------------------------------------------------------- #

def test_identity_derived_from_profile_and_deployment_host():
    env = git_identity_env({}, profile=PROFILE)
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert dict(_config_pairs(env)) == {
        "user.name": PROFILE,
        "user.email": f"{PROFILE}@users.noreply.gitlab.example.com",
    }


def test_email_host_follows_gitlab_host_and_drops_the_port():
    env = git_identity_env({"GITLAB_HOST": "git.example.com:8443"}, profile=PROFILE)
    assert dict(_config_pairs(env))["user.email"] == f"{PROFILE}@users.noreply.git.example.com"


def test_identity_seeds_first_and_auth_extends_the_same_count():
    merged: dict[str, str] = {"GITLAB_TOKEN": "glpat-x", "GITLAB_HOST": "gitlab.example.com"}
    merged.update(git_identity_env(merged, profile=PROFILE))
    merged.update(git_auth_env(merged))
    keys = [key for key, _value in _config_pairs(merged)]
    assert keys[:2] == ["user.name", "user.email"]
    assert keys[2].startswith("credential.")
    assert merged["GIT_CONFIG_COUNT"] == "5"


def test_an_existing_explicit_identity_is_never_overridden():
    """git lets LATER config entries win — ours must yield, not append."""
    pre = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "user.email",
        "GIT_CONFIG_VALUE_0": "someone@corp.example",
    }
    assert git_identity_env(pre, profile=PROFILE) == {}


def test_hostile_or_empty_profile_names_yield_nothing():
    """The name lands verbatim in config values — confine the charset."""
    for bad in ("", "   ", "a b", "x\ny", "-lead", ".lead", "名字", "a" * 80):
        assert git_identity_env({}, profile=bad) == {}


# --------------------------------------------------------------------------- #
# the mechanism — real git in an isolated HOME, like the pivoted runtime
# --------------------------------------------------------------------------- #

def test_real_git_commit_carries_the_injected_identity(tmp_path: Path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git unavailable")
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    base_env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    subprocess.run([git, "init", "-q", str(repo)], env=base_env, check=True)

    env = {**base_env, **git_identity_env({}, profile=PROFILE)}
    commit = subprocess.run(
        [git, "-C", str(repo), "commit", "--allow-empty", "-m", "x"],
        env=env, capture_output=True, text=True,
    )
    assert commit.returncode == 0, commit.stderr
    author = subprocess.run(
        [git, "-C", str(repo), "log", "-1", "--format=%an <%ae>"],
        env=env, capture_output=True, text=True,
    ).stdout.strip()
    assert author == IDENTITY


# --------------------------------------------------------------------------- #
# the producer — identity present even with NO credential config at all
# --------------------------------------------------------------------------- #

def test_credential_env_producer_seeds_identity_without_any_credential_config(tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / ".hermes" / "profiles" / PROFILE
    profile_home.mkdir(parents=True)

    env = agent_real._credential_env_for_aiagent(profile_home)

    pairs = dict(_config_pairs(env))
    assert pairs["user.name"] == PROFILE
    assert pairs["user.email"] == f"{PROFILE}@users.noreply.gitlab.example.com"
