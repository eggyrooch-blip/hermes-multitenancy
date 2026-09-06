"""C4: the billed employee key reaches Codex under its surviving env name."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_multitenancy import agent_real
from hermes_multitenancy.agent_real import _core
from hermes_multitenancy.billing_identity import billing_runtime_from_environment


def _profile(tmp_path: Path) -> tuple[Path, Path]:
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    approval = tmp_path / "approval"
    approval.mkdir()
    return profile, approval


def test_codex_key_survives_allowlist_and_non_codex_runs_drop_it(tmp_path, monkeypatch):
    profile, approval = _profile(tmp_path)
    key = "sk-employee-runtime-key-123456"
    monkeypatch.setenv("CODEX_RUNTIME_KEY", "ambient-must-not-leak")
    monkeypatch.setenv("CODEX_HOME", "ambient-codex-home")

    mapped = agent_real._build_subprocess_env(
        profile,
        approval_dir=approval,
        extra={
            _core.EXECUTOR_RUNTIME_ENV: "codex_app_server",
            _core.CODEX_RUNTIME_KEY_ENV: key,
            "CODEX_HOME": str(profile / "workspace" / "runs" / "wf" / "codex-home"),
        },
    )
    assert mapped[_core.CODEX_RUNTIME_KEY_ENV] == key
    assert mapped["CODEX_HOME"].endswith("/codex-home")

    native = agent_real._build_subprocess_env(
        profile,
        approval_dir=approval,
        extra={_core.CODEX_RUNTIME_KEY_ENV: key, "CODEX_HOME": "wrong"},
    )
    assert _core.CODEX_RUNTIME_KEY_ENV not in native
    assert "CODEX_HOME" not in native


def test_billing_name_is_popped_and_codex_alias_is_redacted(monkeypatch):
    key = "sk-employee-runtime-key-123456"
    monkeypatch.setenv("HERMES_LITELLM_RUNTIME_API_KEY", key)
    monkeypatch.setenv("HERMES_LITELLM_RUNTIME_BASE_URL", "https://litellm.example/v1")
    monkeypatch.setenv("HERMES_LITELLM_RUNTIME_EMPLOYEE_ID", "alice")

    runtime = billing_runtime_from_environment()
    assert runtime["api_key"] == key
    assert "HERMES_LITELLM_RUNTIME_API_KEY" not in os.environ

    redacted = agent_real._redact_billing_runtime_text(
        f"codex env: {_core.CODEX_RUNTIME_KEY_ENV}={key}; prefix={key[:12]}",
        object(),
        {_core.CODEX_RUNTIME_KEY_ENV: key},
    )
    assert key not in redacted
    assert key[:12] not in redacted


def test_upstream_codex_spawn_scrub_requires_non_hermes_alias(monkeypatch):
    """Pinned 0.19.1 behavior: HERMES_*_KEY is stripped before app-server."""
    key = "sk-employee-runtime-key-123456"
    source_override = os.environ.get("HERMES_UPSTREAM_0191_ROOT")
    source_root = Path(
        source_override or "/Users/hermes/code/hermes-agent-release-v0191"
    )
    if not source_root.is_dir():
        if source_override:
            pytest.fail(f"HERMES_UPSTREAM_0191_ROOT is unavailable: {source_root}")
        pytest.skip("pinned upstream hermes-agent 0.19.1 checkout is unavailable")
    script = (
        "import os; from tools.environments.local import hermes_subprocess_env; "
        "e=hermes_subprocess_env(inherit_credentials=True); "
        "print('hermes=' + str('HERMES_CODEX_RUNTIME_KEY' in e)); "
        "print('codex=' + str(e.get('CODEX_RUNTIME_KEY') == os.environ['CODEX_RUNTIME_KEY']))"
    )
    child_env = os.environ.copy()
    child_env.update(
        {
            "HERMES_CODEX_RUNTIME_KEY": key,
            "CODEX_RUNTIME_KEY": key,
            "PYTHONPATH": str(source_root)
            + os.pathsep
            + str(child_env.get("PYTHONPATH") or ""),
        }
    )
    output = subprocess.check_output(
        [sys.executable, "-c", script], env=child_env, text=True
    )
    assert "hermes=False" in output
    assert "codex=True" in output
