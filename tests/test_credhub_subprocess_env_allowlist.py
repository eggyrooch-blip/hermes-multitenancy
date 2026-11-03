"""CRIT-1 regression (audit 2026-07-03): credential-status/login subprocesses
must NOT inherit secrets promoted into os.environ (vault master decryption key /
FEISHU_APP_SECRET). A poisoned npx @lark-project/meegle package or any dump-env
tool would otherwise exfiltrate the key that decrypts every tenant's credentials.

These FAIL on pre-fix code where the env builders used ``{**os.environ, ...}``.
"""
from __future__ import annotations

import pytest

_SECRETS = (
    "HERMES_MULTITENANCY_CREDENTIAL_KEY",
    "HERMES_CREDENTIAL_KEY",
    "FEISHU_APP_SECRET",
)


def _plant_secrets(monkeypatch):
    for k in _SECRETS:
        monkeypatch.setenv(k, "SECRET-" + k)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")


def test_helper_excludes_secrets_keeps_benign_and_overrides(monkeypatch):
    from hermes_multitenancy.credential_renewal_common import build_status_subprocess_env

    _plant_secrets(monkeypatch)
    env = build_status_subprocess_env({"HOME": "/tmp/x", "KEP_PROFILE": "p"})

    for k in _SECRETS:
        assert k not in env, f"{k} leaked into status subprocess env"
    assert env["HOME"] == "/tmp/x"          # explicit override preserved
    assert env["KEP_PROFILE"] == "p"
    assert env["PATH"] == "/usr/bin:/bin"   # benign var passes through


def test_kep_status_subprocess_env_has_no_secret(monkeypatch, tmp_path):
    """Integration: the real kep status call site must not hand secrets to _run."""
    from hermes_multitenancy import credential_hub as ch

    _plant_secrets(monkeypatch)
    captured: dict = {}

    def fake_run(cmd, *, cwd=None, env=None):
        captured["env"] = env or {}
        return None  # short-circuit — we only assert on the env we were handed

    bin_path = tmp_path / "kep-auth"
    bin_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bin_path.chmod(0o755)
    monkeypatch.setattr(ch, "_run", fake_run)

    ch._kep_env_status(
        bin_path=str(bin_path),
        profile_dir=tmp_path,
        home_dir=tmp_path,
        profile_name="p",
        env_name="online",
    )

    env = captured["env"]
    for k in _SECRETS:
        assert k not in env, f"{k} leaked into kep status subprocess env"
    assert env["KEP_PROFILE"] == "p"        # explicit override still threaded through


def test_kep_login_env_preserves_browser_shim(monkeypatch, tmp_path):
    """Regression guard: the allowlist helper must not swallow the login env's
    browser-shim overrides (PATH prepend + BROWSER), while still dropping secrets."""
    from hermes_multitenancy import credential_hub_auth as cha

    _plant_secrets(monkeypatch)
    monkeypatch.setenv("TMPDIR", str(tmp_path))  # keep the shim dir out of real /tmp

    env = cha._kep_login_env(tmp_path, "p")

    for k in _SECRETS:
        assert k not in env, f"{k} leaked into kep login subprocess env"
    assert env.get("BROWSER", "").endswith("/open"), "browser shim override lost"
    assert "hermes-credhub-nobrowser" in env["PATH"], "PATH shim prepend lost"
    assert env["KEP_PROFILE"] == "p"
