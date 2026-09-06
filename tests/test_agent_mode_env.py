"""Hermes IS the online runtime: every agent subprocess must carry KEP_AGENT_MODE=online.

Keep expert plugins (keep-server-dev-plugin using-server-dev 第 0.5 步) branch all
repo-acquisition / push / gate semantics on this marker: ``online`` = clone target
repos per session via the KEPKnowledge L2 registry; unset = assume a local
long-lived checkout. The plugin side only ever *reads* the variable — the contract
says "由在线运行时注入，本地永远不设". Before 2026-08-26 no producer existed, so
online experts judged themselves 本地模式 and dead-ended in an empty workspace
(could not locate the target repo, refused to clone).

Two boundaries must both carry the marker:
  1. the AIAgent child process env itself, and
  2. the terminal/execute_code second-scrub boundary, via the ``_HERMES_FORCE_*``
     passthrough channel — the model checks the mode with ``echo $KEP_AGENT_MODE``
     in a terminal tool call, which spawns yet another scrubbed subprocess.

And two RUN paths: the subprocess env build and the in-process one
(``_apply_runtime_env_for_aiagent``). Both are fed by the same profile-anchor
helper, which is why the marker lives there and not in one of them.

``KEP_WORKSPACE_DIR`` travels with it: the same plugins use
``$KEP_WORKSPACE_DIR/KepSpecHub`` as the per-session working volume, so an
online mode with no workspace anchor still dead-ends.
"""
from __future__ import annotations

from hermes_multitenancy import agent_real


def _build_env(tmp_path):
    profile_home = tmp_path / "profiles" / "p_test"
    profile_home.mkdir(parents=True)
    approval_dir = tmp_path / "approvals"
    approval_dir.mkdir()
    return agent_real._build_subprocess_env(profile_home, approval_dir=approval_dir)


def test_agent_subprocess_env_marks_online_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("KEP_AGENT_MODE", raising=False)
    env = _build_env(tmp_path)
    assert env.get("KEP_AGENT_MODE") == "online"


def test_agent_mode_survives_terminal_second_scrub(tmp_path, monkeypatch):
    """`echo $KEP_AGENT_MODE` inside the terminal tool must print `online`."""
    monkeypatch.delenv("KEP_AGENT_MODE", raising=False)
    env = _build_env(tmp_path)
    assert env.get("_HERMES_FORCE_KEP_AGENT_MODE") == "online"


def test_gateway_env_cannot_downgrade_agent_mode(tmp_path, monkeypatch):
    """A stray KEP_AGENT_MODE in the gateway env must not leak/override: the
    runtime asserts its own identity unconditionally."""
    monkeypatch.setenv("KEP_AGENT_MODE", "local")
    env = _build_env(tmp_path)
    assert env.get("KEP_AGENT_MODE") == "online"


def test_agent_subprocess_env_carries_the_session_workspace(tmp_path, monkeypatch):
    """`$KEP_WORKSPACE_DIR/KepSpecHub` is the per-session working volume."""
    monkeypatch.delenv("KEP_WORKSPACE_DIR", raising=False)
    profile_home = tmp_path / "profiles" / "p_test"
    profile_home.mkdir(parents=True)
    approval_dir = tmp_path / "approvals"
    approval_dir.mkdir()
    env = agent_real._build_subprocess_env(profile_home, approval_dir=approval_dir)
    assert env.get("KEP_WORKSPACE_DIR") == str(profile_home / "workspace")
    assert env.get("_HERMES_FORCE_KEP_WORKSPACE_DIR") == str(profile_home / "workspace")


def test_in_process_run_path_carries_both_kep_anchors(tmp_path, monkeypatch):
    """The in-process runtime env scope is the other RUN path — same contract."""
    import os

    for key in ("KEP_AGENT_MODE", "KEP_WORKSPACE_DIR"):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"_HERMES_FORCE_{key}", raising=False)
    profile_home = tmp_path / "profiles" / "p_test"
    profile_home.mkdir(parents=True)

    cleanup = agent_real._apply_runtime_env_for_aiagent(profile_home)
    try:
        assert os.environ["KEP_AGENT_MODE"] == "online"
        assert os.environ["KEP_WORKSPACE_DIR"] == str(profile_home / "workspace")
        assert os.environ["_HERMES_FORCE_KEP_AGENT_MODE"] == "online"
        assert os.environ["_HERMES_FORCE_KEP_WORKSPACE_DIR"] == str(
            profile_home / "workspace"
        )
    finally:
        cleanup()
    assert "KEP_AGENT_MODE" not in os.environ
    assert "KEP_WORKSPACE_DIR" not in os.environ


def test_kep_anchors_reach_the_execute_code_child(tmp_path):
    """execute_code scrubs its child env by NAME, and neither KEP key matches a
    safe prefix — verified against the real prod core (`prod-v0191-base`
    `tools/code_execution_tool._scrub_child_env`): both are DROPPED unless the
    runtime re-asserts them.

    Multitenancy re-asserts them twice: the execute_code scrub patch replays
    every anchor out of the `_HERMES_FORCE_*` mirror, and the in-process runtime
    registers the same names for passthrough. This guards the first half — if
    an anchor stops travelling through the force channel, the model's python
    snippets silently lose online mode again.
    """
    profile_home = tmp_path / "profiles" / "p_test"
    profile_home.mkdir(parents=True)
    approval_dir = tmp_path / "approvals"
    approval_dir.mkdir()
    env = agent_real._build_subprocess_env(profile_home, approval_dir=approval_dir)

    replayed = agent_real._forced_profile_anchor_env_from(env)

    assert replayed["KEP_AGENT_MODE"] == "online"
    assert replayed["KEP_WORKSPACE_DIR"] == str(profile_home / "workspace")
