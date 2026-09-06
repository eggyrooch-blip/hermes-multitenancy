"""An injected GitLab token must make plain ``git`` work, not only ``glab``.

2026-08-26 production: an employee bound a write-scoped GitLab token, asked the
expert to branch a service repo, and the agent answered "this Hermes environment
cannot reach your internal git" without ever running one ``git``/``glab``
command. ``GITLAB_TOKEN``/``GITLAB_HOST`` were injected — but bare ``git`` reads
neither: over https it asks for a username (``fatal: could not read Username``
on prod), and over ``git@host:`` it needs an ssh key the pivoted HOME does not
have.

The fix is one shared helper — :func:`credential_materializer.git_auth_env` —
called by BOTH env producers that can resolve a GitLab token:

  * ``agent_real/_core.py:_credential_env_for_aiagent``  (personal / regular profile)
  * ``credential_delegation.py:owner_gitlab_env``        (group borrows the initiator's)

It appends ``GIT_TERMINAL_PROMPT=0`` plus ``GIT_CONFIG_COUNT``/``KEY_n``/
``VALUE_n`` entries defining a credential helper (username ``oauth2``, password
read from ``$GITLAB_TOKEN`` **at run time**) and ``insteadOf`` rewrites from the
ssh forms to https. The token literal never enters a config value, so it is not
readable from the rendered git config — only from the env var that already
carries it.

The last two tests drive the REAL ``git`` binary in an isolated HOME (no system
/ global config, exactly like the profile-pivoted runtime) — the mechanism, not
just the strings.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hermes_multitenancy.credential_materializer import git_auth_env, git_identity_env


HOST = "gitlab.example.com"
TOKEN = "glpat-employee-personal"


def _config_pairs(env: dict[str, str]) -> list[tuple[str, str]]:
    """The (key, value) pairs git will read out of ``GIT_CONFIG_*``."""
    count = int(env["GIT_CONFIG_COUNT"])
    return [
        (env[f"GIT_CONFIG_KEY_{index}"], env[f"GIT_CONFIG_VALUE_{index}"])
        for index in range(count)
    ]


# --------------------------------------------------------------------------- #
# the helper itself
# --------------------------------------------------------------------------- #

def test_no_token_yields_no_git_env_at_all():
    """Half an env is worse than none: without a token, emit zero GIT_* keys."""
    assert git_auth_env({}) == {}
    assert git_auth_env({"GITLAB_HOST": HOST}) == {}
    assert git_auth_env({"GITLAB_TOKEN": "", "GITLAB_HOST": HOST}) == {}
    assert git_auth_env({"GITLAB_TOKEN": "   "}) == {}


def test_helper_reads_the_token_at_runtime_never_embeds_it():
    env = git_auth_env({"GITLAB_TOKEN": TOKEN, "GITLAB_HOST": HOST})
    values = [value for _key, value in _config_pairs(env)]
    assert TOKEN not in "\x00".join(values)
    assert not any(TOKEN in value for value in env.values())
    helper = dict(_config_pairs(env))[f"credential.https://{HOST}.helper"]
    assert "$GITLAB_TOKEN" in helper
    assert helper.startswith("!")  # shell helper, evaluated per request


def test_git_config_pairs_cover_credential_and_both_ssh_forms():
    env = git_auth_env({"GITLAB_TOKEN": TOKEN, "GITLAB_HOST": HOST})
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_COUNT"] == "3"
    keys = [key for key, _value in _config_pairs(env)]
    assert keys == [
        f"credential.https://{HOST}.helper",
        f"url.https://{HOST}/.insteadOf",
        f"url.https://{HOST}/.insteadOf",
    ]
    rewrites = [value for key, value in _config_pairs(env) if key.endswith(".insteadOf")]
    assert rewrites == [f"git@{HOST}:", f"ssh://git@{HOST}/"]


def test_host_comes_from_env_and_falls_back_to_the_deployment_default():
    other = git_auth_env({"GITLAB_TOKEN": TOKEN, "GITLAB_HOST": "git.example.com"})
    assert dict(_config_pairs(other))["credential.https://git.example.com.helper"]
    # GITLAB_HOST may legitimately carry a scheme (glab accepts both).
    schemed = git_auth_env({"GITLAB_TOKEN": TOKEN, "GITLAB_HOST": "https://git.example.com/"})
    assert _config_pairs(schemed) == _config_pairs(other)
    # Missing / unusable host must not produce a corrupt config key.
    for bad in ("", "   ", "not a host", "a b;c"):
        fallback = git_auth_env({"GITLAB_TOKEN": TOKEN, "GITLAB_HOST": bad})
        assert dict(_config_pairs(fallback))[f"credential.https://{HOST}.helper"]


def test_existing_git_config_count_is_extended_not_overwritten():
    env = git_auth_env(
        {
            "GITLAB_TOKEN": TOKEN,
            "GITLAB_HOST": HOST,
            "GIT_CONFIG_COUNT": "2",
        }
    )
    assert env["GIT_CONFIG_COUNT"] == "5"
    assert "GIT_CONFIG_KEY_0" not in env  # somebody else's entries survive
    assert env["GIT_CONFIG_KEY_2"] == f"credential.https://{HOST}.helper"
    assert env["GIT_CONFIG_KEY_4"] == f"url.https://{HOST}/.insteadOf"


def test_custom_token_env_name_is_what_the_helper_reads():
    env = git_auth_env({"KEEP_GITLAB_PAT": TOKEN}, token_env_name="KEEP_GITLAB_PAT")
    helper = dict(_config_pairs(env))[f"credential.https://{HOST}.helper"]
    assert "$KEEP_GITLAB_PAT" in helper and "$GITLAB_TOKEN" not in helper


def test_a_non_identifier_env_name_never_reaches_the_shell_helper():
    """The name is interpolated into a shell snippet — config typos stop here."""
    for bad in ("TOKEN; rm -rf /", "TOKEN}$(id)", "", "2TOKEN"):
        assert git_auth_env({bad: TOKEN}, token_env_name=bad) == {}


# --------------------------------------------------------------------------- #
# producer 1 — personal / regular profile credential env
# --------------------------------------------------------------------------- #

def _seed_gitlab_vault(shared: Path, profile: str, *, token: str | None) -> Path:
    from hermes_multitenancy.credentials import CredentialStore

    profile_home = shared / "profiles" / profile
    profile_home.mkdir(parents=True, exist_ok=True)
    (shared / "credential-materialization.yaml").write_text(
        yaml.safe_dump(
            {
                "credentials": [
                    {
                        "provider": "gitlab",
                        "subject_id": "kep-prd-skills",
                        "secret_kind": "token",
                        "env": "GITLAB_TOKEN",
                        "env_extra": {"GITLAB_HOST": HOST},
                        "target": "workspace/credentials/gitlab.token",
                        "profiles": [profile],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    if token is not None:
        store = CredentialStore(shared / "multitenancy.db")
        try:
            store.put_credential(
                profile_name="__shared__",
                subject_id="kep-prd-skills",
                provider="gitlab",
                secret_kind="token",
                payload={"token": token},
            )
        finally:
            store.close()
    return profile_home


def test_credential_env_producer_carries_git_auth(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    profile_home = _seed_gitlab_vault(tmp_path / ".hermes", "huangshuai", token=TOKEN)

    env = agent_real._credential_env_for_aiagent(profile_home)

    assert env["GITLAB_TOKEN"] == TOKEN
    assert env["GITLAB_HOST"] == HOST
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    # Identity is seeded first (see test_git_identity_env.py), auth extends it.
    expected = git_identity_env({}, profile="huangshuai")
    expected.update(
        git_auth_env({**expected, "GITLAB_TOKEN": TOKEN, "GITLAB_HOST": HOST})
    )
    assert _config_pairs(env) == _config_pairs(expected)
    assert not any(TOKEN in value for key, value in _config_pairs(env))


def test_profile_without_token_gets_no_git_auth_keys(monkeypatch, tmp_path: Path):
    """No token → no AUTH env (helper/rewrites/prompt). Commit identity
    (user.name/user.email) is still seeded — it is a runtime anchor for local
    `git commit`, not half a credential (2026-08-26: agent stalled mid-commit
    asking the user for an identity the pivoted HOME never had)."""
    from hermes_multitenancy import agent_real

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    profile_home = _seed_gitlab_vault(tmp_path / ".hermes", "nobody", token=None)

    env = agent_real._credential_env_for_aiagent(profile_home)

    assert "GITLAB_TOKEN" not in env
    assert "GIT_TERMINAL_PROMPT" not in env
    pairs = dict(_config_pairs(env))
    assert set(pairs) == {"user.name", "user.email"}


def test_git_auth_env_survives_the_terminal_second_scrub(monkeypatch, tmp_path: Path):
    """`git clone` runs in a terminal subprocess, which scrubs env a second time."""
    from hermes_multitenancy import agent_real

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    profile_home = _seed_gitlab_vault(shared, "huangshuai", token=TOKEN)
    approval_dir = tmp_path / "approvals"
    approval_dir.mkdir()

    env = agent_real._build_subprocess_env(profile_home, approval_dir=approval_dir)

    assert env["_HERMES_FORCE_GIT_TERMINAL_PROMPT"] == "0"
    assert env["_HERMES_FORCE_GIT_CONFIG_COUNT"] == env["GIT_CONFIG_COUNT"]
    for index in range(int(env["GIT_CONFIG_COUNT"])):
        for name in (f"GIT_CONFIG_KEY_{index}", f"GIT_CONFIG_VALUE_{index}"):
            assert env[f"_HERMES_FORCE_{name}"] == env[name]


# --------------------------------------------------------------------------- #
# producer 2 — group profile borrowing the initiator's personal token
# --------------------------------------------------------------------------- #

def test_delegated_owner_env_carries_the_same_git_auth(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import credential_delegation as leases
    from hermes_multitenancy.credentials import CredentialStore

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / "hermes"
    (shared / "profiles" / "alice").mkdir(parents=True)
    (shared / "credential-materialization.yaml").write_text(
        yaml.safe_dump(
            {
                "credentials": [
                    {
                        "provider": "gitlab",
                        "subject_id": "kep-prd-skills",
                        "secret_kind": "token",
                        "vault_profile": "__self__",
                        "env": "GITLAB_TOKEN",
                        "env_extra": {"GITLAB_HOST": HOST},
                        "target": "workspace/credentials/gitlab.token",
                        "profiles": ["alice"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    store = CredentialStore(shared / "multitenancy.db")
    try:
        store.put_credential(
            profile_name="alice",
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
            payload={"token": TOKEN},
            scopes=["api"],
            expires_at=None,
        )
    finally:
        store.close()

    env = leases.owner_gitlab_env(shared, "alice")

    assert env["GITLAB_TOKEN"] == TOKEN
    assert _config_pairs(env) == _config_pairs(
        git_auth_env({"GITLAB_TOKEN": TOKEN, "GITLAB_HOST": HOST})
    )
    assert not any(TOKEN in value for key, value in _config_pairs(env))


def test_owner_without_token_yields_no_git_keys(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import credential_delegation as leases

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / "hermes"
    (shared / "profiles" / "alice").mkdir(parents=True)
    assert leases.owner_gitlab_env(shared, "alice") == {}


# --------------------------------------------------------------------------- #
# the real git binary, offline, in an isolated HOME (= the pivoted runtime)
# --------------------------------------------------------------------------- #

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _isolated_git_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "isolated-home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),          # profile pivot: no ~/.gitconfig, no ssh key
        "GIT_CONFIG_NOSYSTEM": "1",  # no /etc/gitconfig helper can answer first
        "GITLAB_TOKEN": "dummy-token-not-a-secret",
        "GITLAB_HOST": HOST,
    }
    env.update(git_auth_env(env))
    return env


@requires_git
def test_real_git_credential_fill_returns_oauth2_and_the_env_token(tmp_path: Path):
    result = subprocess.run(
        ["git", "credential", "fill"],
        input=f"protocol=https\nhost={HOST}\n",
        capture_output=True,
        text=True,
        env=_isolated_git_env(tmp_path),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "username=oauth2" in result.stdout
    assert "password=dummy-token-not-a-secret" in result.stdout


@requires_git
def test_real_git_rewrites_the_ssh_url_to_https(tmp_path: Path):
    """`protocol.https.allow=never` makes the rewrite observable without a network."""
    result = subprocess.run(
        [
            "git",
            "-c",
            "protocol.https.allow=never",
            "ls-remote",
            f"git@{HOST}:fd/guide.git",
        ],
        capture_output=True,
        text=True,
        env=_isolated_git_env(tmp_path),
        timeout=30,
    )
    assert result.returncode != 0
    # Reached the https transport => insteadOf rewrote it. Without the rewrite
    # git would have tried ssh and complained about the host/key instead.
    assert "transport 'https' not allowed" in result.stderr


@requires_git
def test_real_git_without_the_helper_cannot_authenticate(tmp_path: Path):
    """Negative control: same isolated HOME, no GIT_CONFIG_* => no credentials."""
    home = tmp_path / "bare-home"
    home.mkdir()
    result = subprocess.run(
        ["git", "credential", "fill"],
        input=f"protocol=https\nhost={HOST}\n",
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GITLAB_TOKEN": "dummy-token-not-a-secret",
        },
        timeout=30,
    )
    assert "username=oauth2" not in result.stdout
    assert "dummy-token-not-a-secret" not in result.stdout


# --------------------------------------------------------------------------- #
# review round 1 (codex + grok) — the two findings that were real
# --------------------------------------------------------------------------- #

def test_borrowed_run_level_token_is_registered_for_the_code_sandbox(
    monkeypatch, tmp_path: Path
):
    """A group profile borrows the token per RUN, so it is in the process env
    but NOT in that profile's materialization config.

    execute_code's scrub drops anything unregistered, and `GITLAB_TOKEN` /
    `GIT_CONFIG_KEY_n` hit its secret substrings (TOKEN / KEY) while the rest of
    the GIT_* set matches no safe prefix — so without this registration the
    borrowed token reaches the terminal tool but `git clone` from a python
    snippet stays unauthenticated.
    """
    from hermes_multitenancy import agent_real

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    # A GROUP profile: no credential entry of its own.
    group_home = shared / "profiles" / "feishu_group_abc"
    group_home.mkdir(parents=True)
    (shared / "credential-materialization.yaml").write_text(
        yaml.safe_dump(
            {
                "credentials": [
                    {
                        "provider": "gitlab",
                        "subject_id": "kep-prd-skills",
                        "secret_kind": "token",
                        "env": "GITLAB_TOKEN",
                        "env_extra": {"GITLAB_HOST": HOST},
                        "target": "workspace/credentials/gitlab.token",
                        "profiles": ["alice"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    own = agent_real._credential_env_for_aiagent(group_home)
    # No credential of its own — only the seeded commit identity survives
    # (identity is a runtime anchor, not a credential; see test_git_identity_env).
    assert "GITLAB_TOKEN" not in own
    assert {key for key, _value in _config_pairs(own)} == {"user.name", "user.email"}

    # What the parent injected into THIS run only.
    run_env = {"GITLAB_TOKEN": TOKEN, "GITLAB_HOST": HOST}
    run_env.update(git_auth_env(run_env))
    for name, value in run_env.items():
        monkeypatch.setenv(name, value)

    registered: list[list[str]] = []
    fake_passthrough = SimpleNamespace(
        register_env_passthrough=lambda names: registered.append(list(names)),
        _config_passthrough=frozenset(),
    )
    tools_mod = sys.modules.get("tools") or types.ModuleType("tools")
    monkeypatch.setattr(tools_mod, "env_passthrough", fake_passthrough, raising=False)
    monkeypatch.setitem(sys.modules, "tools", tools_mod)
    monkeypatch.setitem(sys.modules, "tools.env_passthrough", fake_passthrough)

    agent_real._install_credential_env_passthrough(group_home)

    assert registered, "nothing registered for a borrowed run-level token"
    assert set(registered[0]) >= set(run_env)
    assert fake_passthrough._config_passthrough >= set(run_env)


def test_no_run_level_token_registers_nothing(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    group_home = tmp_path / ".hermes" / "profiles" / "feishu_group_abc"
    group_home.mkdir(parents=True)
    assert agent_real._run_level_gitlab_env_names(group_home) == set()


def test_canonical_token_name_outranks_a_legacy_alias(monkeypatch, tmp_path: Path):
    """Two gitlab entries: git takes the FIRST helper that answers, so the
    canonical GITLAB_TOKEN must be wired before an alias that merely sorts
    earlier."""
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.credentials import CredentialStore

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    profile_home = shared / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    (shared / "credential-materialization.yaml").write_text(
        yaml.safe_dump(
            {
                "credentials": [
                    {
                        "provider": "gitlab",
                        "subject_id": "legacy-alias",
                        "secret_kind": "token",
                        "env": "GITLAB_PAT",  # sorts BEFORE GITLAB_TOKEN
                        "target": "workspace/credentials/gitlab-legacy.token",
                        "profiles": ["alice"],
                    },
                    {
                        "provider": "gitlab",
                        "subject_id": "kep-prd-skills",
                        "secret_kind": "token",
                        "env": "GITLAB_TOKEN",
                        "env_extra": {"GITLAB_HOST": HOST},
                        "target": "workspace/credentials/gitlab.token",
                        "profiles": ["alice"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    store = CredentialStore(shared / "multitenancy.db")
    try:
        for subject, token in (("legacy-alias", "glpat-legacy"), ("kep-prd-skills", TOKEN)):
            store.put_credential(
                profile_name="__shared__",
                subject_id=subject,
                provider="gitlab",
                secret_kind="token",
                payload={"token": token},
            )
    finally:
        store.close()

    env = agent_real._credential_env_for_aiagent(profile_home)
    helpers = [
        value
        for key, value in _config_pairs(env)
        if key == f"credential.https://{HOST}.helper"
    ]
    assert helpers, "no credential helper rendered"
    assert "$GITLAB_TOKEN" in helpers[0]


def test_a_personal_profile_ignores_ambient_env_for_registration(monkeypatch, tmp_path: Path):
    """Only the borrow lane reads the ambient env; a personal profile's names
    come from its own config, so a stray GITLAB_TOKEN in the process must not
    widen its registration."""
    from hermes_multitenancy import agent_real

    monkeypatch.setenv("GITLAB_TOKEN", TOKEN)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "3")
    personal = tmp_path / ".hermes" / "profiles" / "alice"
    personal.mkdir(parents=True)
    assert agent_real._run_level_gitlab_env_names(personal) == set()


# --------------------------------------------------------------------------- #
# review round 2 (codex gpt-5.6-sol) — host size is a limit, not a nicety
# --------------------------------------------------------------------------- #

def test_an_oversized_host_cannot_blow_up_the_process_env(tmp_path: Path):
    """A huge vault host is amplified across five env values until `execve`
    refuses EVERY subprocess for that profile with E2BIG — not just git."""
    env = git_auth_env({"GITLAB_TOKEN": TOKEN, "GITLAB_HOST": "a" * 200_000})
    assert dict(_config_pairs(env))[f"credential.https://{HOST}.helper"]
    assert max(len(value) for value in env.values()) < 1024

    # And prove it with a real spawn: the augmented env still runs a subprocess.
    child = subprocess.run(
        [sys.executable, "-c", "print('spawned')"],
        capture_output=True,
        text=True,
        env={**_isolated_git_env(tmp_path), **env},
        timeout=30,
    )
    assert child.returncode == 0 and "spawned" in child.stdout


@pytest.mark.parametrize(
    "host",
    [
        "a" * 254,                    # over the 253-byte DNS limit
        "a" * 64 + ".example.com",    # label over 63 bytes
        "-lead.example.com",          # label may not start with a hyphen
        "trail-.example.com",
        "git.example.com:0",          # port out of range
        "git.example.com:65536",
        "git.example.com:notaport",
        "git.example.com:",           # empty port
        "..",
    ],
)
def test_malformed_hosts_fall_back_to_the_deployment_default(host):
    env = git_auth_env({"GITLAB_TOKEN": TOKEN, "GITLAB_HOST": host})
    assert dict(_config_pairs(env))[f"credential.https://{HOST}.helper"]


@pytest.mark.parametrize(
    "host",
    [
        "git.example.com",
        "git.example.com:8443",
        "a" * 63 + ".example.com",
        "localhost",
    ],
)
def test_legitimate_hosts_are_used_verbatim(host):
    env = git_auth_env({"GITLAB_TOKEN": TOKEN, "GITLAB_HOST": host})
    assert dict(_config_pairs(env))[f"credential.https://{host}.helper"]


# --------------------------------------------------------------------------- #
# review round 3 (codex gpt-5.6-sol) — `str.isdigit()` is not `int()`
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "port",
    [
        "²",          # isdigit() == True, int() raises ValueError
        "８４４３",     # full-width: int() accepts, git config would not
        "٨٤٤٣",       # Arabic-Indic
        "0000080",    # zero-padded past 5 digits
        "+8443",
    ],
)
def test_a_unicode_port_neither_raises_nor_reaches_the_config(port):
    """Both producers catch broadly, so a raise here would drop the WHOLE
    credential env — the agent would be back to unauthenticated git."""
    env = git_auth_env({"GITLAB_TOKEN": TOKEN, "GITLAB_HOST": f"git.example.com:{port}"})
    assert dict(_config_pairs(env))[f"credential.https://{HOST}.helper"]
    assert all(value.isascii() for value in env.values())


def test_git_auth_env_is_total_over_hostile_host_values():
    """It must never raise: the callers' except-clauses would swallow the token."""
    for host in ("²", ":", "::", "a" * 300_000, "\n", "\x00", "git.example.com:²", "[::1]"):
        env = git_auth_env({"GITLAB_TOKEN": TOKEN, "GITLAB_HOST": host})
        assert env["GIT_CONFIG_COUNT"] == "3"


def test_a_trailing_blank_is_trimmed_not_rejected():
    """Vault values pick up stray whitespace; that is a trim, not a bad host."""
    env = git_auth_env({"GITLAB_TOKEN": TOKEN, "GITLAB_HOST": " git.example.com:8443 "})
    assert dict(_config_pairs(env))["credential.https://git.example.com:8443.helper"]
