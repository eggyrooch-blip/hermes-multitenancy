from pathlib import Path

import pytest

from hermes_multitenancy.agent_real.run_workspace import RunWorkspaceError, bind_existing


def test_local_harness_binds_plain_workspace_and_rejects_session_rebind(tmp_path: Path):
    profile = tmp_path / "profile"
    selected = profile / "workspace" / "plain-folder"
    selected.mkdir(parents=True)

    workspace = bind_existing(profile, "wf-1", "plain-folder")
    assert workspace.repo_dir == selected.resolve()
    assert workspace.spec_hub_dir == selected.resolve()

    other = profile / "workspace" / "other"
    other.mkdir()
    with pytest.raises(RunWorkspaceError, match="workspace binding changed"):
        bind_existing(profile, "wf-1", "other")


def test_local_harness_never_crosses_profile_workspace_roots(tmp_path: Path):
    alice = tmp_path / "alice"
    bob = tmp_path / "bob"
    (alice / "workspace" / "project").mkdir(parents=True)
    (bob / "workspace" / "project").mkdir(parents=True)

    alice_run = bind_existing(alice, "wf", "project")
    bob_run = bind_existing(bob, "wf", "project")

    assert alice_run.repo_dir != bob_run.repo_dir
    assert alice_run.repo_dir.is_relative_to(alice / "workspace")
    assert bob_run.repo_dir.is_relative_to(bob / "workspace")

    (alice / "workspace" / "escape").symlink_to(bob / "workspace" / "project")
    with pytest.raises(ValueError, match="invalid workspace"):
        bind_existing(alice, "escaped", "escape")


def test_default_workspace_binding_round_trips_through_resolver(tmp_path: Path, monkeypatch):
    """prod 2026-09-02: WebUI「默认工作区」→ repo_dir == workspace root →
    raw_event["workspace"] became "." → resolve_profile_workspace rejected it →
    every Codex-harness run surfaced as "Harness is unavailable"."""
    from types import SimpleNamespace

    from hermes_multitenancy.agent_real import _core
    from hermes_multitenancy.run_models import resolve_profile_workspace

    profile = tmp_path / "profile"
    bound = bind_existing(profile, "wf-default", None)
    assert bound.repo_dir == (profile / "workspace").resolve()

    event = SimpleNamespace(raw_event={"workspace": None})
    monkeypatch.setattr(_core, "_codex_run_workspace_for_event", lambda *_: bound)
    _core._bind_codex_run_workspace(event, profile)

    _normalized, cwd = resolve_profile_workspace(profile, event.raw_event["workspace"])
    assert cwd == bound.repo_dir

    # Sub-folder workspaces keep their relative binding.
    (profile / "workspace" / "sub").mkdir()
    sub = bind_existing(profile, "wf-sub", "sub")
    event = SimpleNamespace(raw_event={"workspace": "sub"})
    monkeypatch.setattr(_core, "_codex_run_workspace_for_event", lambda *_: sub)
    _core._bind_codex_run_workspace(event, profile)
    assert event.raw_event["workspace"] == "sub"
