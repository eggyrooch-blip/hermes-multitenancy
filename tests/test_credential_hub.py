"""Credential hub (`/auth`) — the 归口 aggregation: 5 readers + card + reuse.

`credential_hub` is the single credential-status aggregation in multitenancy.
The Feishu /auth card and hermes-web-ui CredentialsView are two paths over it.
These tests pin the read layer (all 5 credentials), card structure, expiry
rendering, home-dir threading, and device-flow session reuse. Subprocess-backed
readers (feishu-project/meegle, kep-cli/kep-auth) are exercised via monkeypatch
so no real binary is required.
"""
from __future__ import annotations

import json
import time

import pytest


def _write_keepai(home_dir, *, token="jwt", expired_ms=None, username="owner", verified_token=None):
    keepai = home_dir / ".keepai"
    keepai.mkdir(parents=True, exist_ok=True)
    lines = [f"keep_auth_token={token}"]
    if expired_ms is not None:
        lines.append(f"keep_auth_token_expired={expired_ms}")
    if username is not None:
        lines.append(f"keep_username={username}")
    (keepai / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if verified_token is not None:
        import hashlib
        (keepai / "webui-auth-verified.json").write_text(
            json.dumps({"token_sha256": hashlib.sha256(verified_token.encode()).hexdigest(),
                        "account_hint": username}),
            encoding="utf-8",
        )


# -- keep-record reader ------------------------------------------------------


def test_keep_record_authenticated_requires_verification_marker(tmp_path):
    from hermes_multitenancy import credential_hub

    future = int(time.time() * 1000) + 7 * 24 * 3600 * 1000
    _write_keepai(tmp_path, token="tok123", expired_ms=future, username="owner", verified_token="tok123")
    row = credential_hub.keep_record_status(home_dir=tmp_path, installed=True)
    assert row.id == "keep-record"
    assert row.status == "authenticated"
    assert row.expires_at == future
    assert row.account_hint == "owner"


def test_keep_record_token_without_marker_is_unknown(tmp_path):
    from hermes_multitenancy import credential_hub

    future = int(time.time() * 1000) + 1000
    _write_keepai(tmp_path, token="tokA", expired_ms=future, verified_token=None)
    row = credential_hub.keep_record_status(home_dir=tmp_path, installed=True)
    assert row.status == "unknown"


def test_keep_record_verified_is_source_of_truth_and_expiry_unit_normalized(tmp_path):
    """Matches WebUI: verified marker → authenticated (expiry ignored for status).
    keep_auth_token_expired is seconds; expires_at must be normalized to ms."""
    from hermes_multitenancy import credential_hub

    secs = 1_802_602_140  # 10-digit seconds (year ~2027)
    _write_keepai(tmp_path, token="tokB", expired_ms=secs, verified_token="tokB")
    row = credential_hub.keep_record_status(home_dir=tmp_path, installed=True)
    assert row.status == "authenticated"  # verified marker wins, regardless of expiry
    assert row.expires_at == secs * 1000  # seconds normalized to ms


def test_keep_record_missing_when_not_installed(tmp_path):
    from hermes_multitenancy import credential_hub

    row = credential_hub.keep_record_status(home_dir=tmp_path, installed=False)
    assert row.status == "missing"


def test_keep_record_needs_auth_when_installed_no_token(tmp_path):
    from hermes_multitenancy import credential_hub

    row = credential_hub.keep_record_status(home_dir=tmp_path, installed=True)
    assert row.status == "needs_auth"


# -- kep-cli reader ----------------------------------------------------------


def test_kep_cli_missing_when_not_installed(tmp_path):
    from hermes_multitenancy import credential_hub

    row = credential_hub.kep_cli_status(
        profile_dir=tmp_path, home_dir=tmp_path / "home", profile_name="p",
        shared_home=tmp_path, installed=False,
    )
    assert row.id == "kep-cli"
    assert row.status == "missing"


def test_kep_cli_unknown_when_token_present_no_live(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub

    home = tmp_path / "home"
    keyring = home / ".kep-cli" / "keyring-fallback"
    keyring.mkdir(parents=True)
    (keyring / "token-key:online:p").write_bytes(b"x")
    # no kep-auth binary present → _kep_auth_bin path won't exist → live None
    monkeypatch.setenv("HERMES_KEP_AUTH_BIN", str(tmp_path / "nope" / "kep-auth"))
    row = credential_hub.kep_cli_status(
        profile_dir=tmp_path, home_dir=home, profile_name="p",
        shared_home=tmp_path, installed=True,
    )
    assert row.status == "unknown"


def test_kep_cli_authenticated_via_live_status(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub

    bin_path = tmp_path / "bin" / "kep-auth"
    bin_path.parent.mkdir(parents=True)
    bin_path.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HERMES_KEP_AUTH_BIN", str(bin_path))

    class _Proc:
        returncode = 0
        stdout = "state: valid\noperator: owner <owner@keep.com>\n"
        stderr = ""

    monkeypatch.setattr(credential_hub, "_run", lambda *a, **k: _Proc())
    row = credential_hub.kep_cli_status(
        profile_dir=tmp_path, home_dir=tmp_path / "home", profile_name="p",
        shared_home=tmp_path, installed=True,
    )
    assert row.status == "authenticated"
    assert row.account_hint == "owner"


# -- feishu-project reader ---------------------------------------------------


def test_feishu_project_missing_when_no_cli(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub

    monkeypatch.setattr(credential_hub, "_meegle_invocation", lambda: None)
    row = credential_hub.feishu_project_status(profile_dir=tmp_path, profile_name="p")
    assert row.id == "feishu-project"
    assert row.status == "missing"
    assert row.installed is False


def test_feishu_project_authenticated(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub

    monkeypatch.setattr(credential_hub, "_meegle_invocation", lambda: ["meegle"])

    class _Proc:
        returncode = 0
        stdout = json.dumps({"authenticated": True, "account": "owner"})
        stderr = ""

    monkeypatch.setattr(credential_hub, "_run", lambda *a, **k: _Proc())
    row = credential_hub.feishu_project_status(profile_dir=tmp_path, profile_name="p")
    assert row.status == "authenticated"
    assert row.account_hint == "owner"


def test_feishu_project_needs_auth_when_not_authed(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub

    monkeypatch.setattr(credential_hub, "_meegle_invocation", lambda: ["meegle"])

    class _Proc:
        returncode = 0
        stdout = json.dumps({"authenticated": False})
        stderr = ""

    monkeypatch.setattr(credential_hub, "_run", lambda *a, **k: _Proc())
    row = credential_hub.feishu_project_status(profile_dir=tmp_path, profile_name="p")
    assert row.status == "needs_auth"
    assert row.installed is True


# -- gitlab reader -----------------------------------------------------------


def test_gitlab_configured_when_token_readable(tmp_path):
    from hermes_multitenancy import credential_hub

    cred = tmp_path / "workspace" / "credentials"
    cred.mkdir(parents=True)
    (cred / "gitlab.token").write_text("glpat-xxx", encoding="utf-8")
    row = credential_hub.gitlab_status(profile_dir=tmp_path, installed=False)
    assert row.id == "gitlab"
    assert row.status == "configured"


def test_gitlab_missing_when_nothing(tmp_path):
    from hermes_multitenancy import credential_hub

    row = credential_hub.gitlab_status(profile_dir=tmp_path, installed=False)
    assert row.status == "missing"


# -- lark-cli reader (reuses feishu_uat_auth.credential_status) --------------


@pytest.mark.parametrize(
    "raw_status,expected",
    # 'expired' collapses to needs_auth — the WebUI SkillCredentialState has no
    # 'expired'; expiry rides on the additive expires_at field instead.
    [("valid", "authenticated"), ("expired", "needs_auth"),
     ("missing", "needs_auth"), ("scope_missing", "needs_auth"), ("weird", "unknown")],
)
def test_lark_cli_status_maps_feishu_status(monkeypatch, tmp_path, raw_status, expected):
    from hermes_multitenancy import credential_hub, feishu_uat_auth

    exp = int(time.time() * 1000) + 3600_000
    monkeypatch.setattr(feishu_uat_auth, "credential_status",
                        lambda **kw: {"status": raw_status, "expires_at": exp})
    row = credential_hub.lark_cli_status(profile_name="s", open_id="ou_s", shared_home=tmp_path)
    assert row.id == "lark-cli"
    assert row.status == expected
    assert row.expires_at == exp


def test_lark_cli_status_degrades_on_error(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub, feishu_uat_auth

    def _boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(feishu_uat_auth, "credential_status", _boom)
    row = credential_hub.lark_cli_status(profile_name="s", open_id="ou_s", shared_home=tmp_path)
    assert row.status == "unknown"


# -- aggregation -------------------------------------------------------------


def test_collect_returns_all_five_in_order(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub, feishu_uat_auth

    monkeypatch.setattr(feishu_uat_auth, "credential_status",
                        lambda **kw: {"status": "valid", "expires_at": int(time.time() * 1000) + 1000})
    monkeypatch.setattr(credential_hub, "_meegle_invocation", lambda: None)
    home = tmp_path / "profiles" / "owner" / "home"
    home.mkdir(parents=True)
    rows = credential_hub.collect_credential_statuses(
        profile_name="owner", open_id="ou_owner", shared_home=tmp_path
    )
    assert [r.id for r in rows] == ["lark-cli", "feishu-project", "keep-record", "kep-cli", "gitlab"]
    assert rows[0].status == "authenticated"
    # to_dict shape is SkillCredentialEntry-compatible
    d = rows[0].to_dict()
    for key in ("id", "title", "provider", "installed", "status", "action"):
        assert key in d


def test_collect_uses_explicit_home_dir_over_shared(monkeypatch, tmp_path):
    """M1: explicit home_dir wins over shared_home-derived path; profile_dir = home.parent."""
    from hermes_multitenancy import credential_hub, feishu_uat_auth

    monkeypatch.setattr(feishu_uat_auth, "credential_status", lambda **kw: {"status": "missing"})
    monkeypatch.setattr(credential_hub, "_meegle_invocation", lambda: None)
    explicit_home = tmp_path / "weird_root" / "home"
    explicit_home.mkdir(parents=True)
    # keep-record skill installed under profile_dir (= explicit_home.parent)
    skill = explicit_home.parent / "skills" / "Keep" / "keep-record"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("name: keep-record\nget_qrcode keep_auth_token\n", encoding="utf-8")
    _write_keepai(explicit_home, token="t", expired_ms=int(time.time() * 1000) + 1000, verified_token="t")

    rows = credential_hub.collect_credential_statuses(
        profile_name="owner", open_id="ou_owner", shared_home=tmp_path, home_dir=explicit_home
    )
    keep = next(r for r in rows if r.id == "keep-record")
    assert keep.status == "authenticated"  # found via explicit home_dir + installed via skill scan


# -- device-flow session reuse (C2) ------------------------------------------


def test_find_active_session_reuses_pending_and_skips_terminal(monkeypatch):
    from hermes_multitenancy import feishu_uat_auth as fa

    now = int(time.time())
    sessions = {
        "pending-ok": fa.FeishuAuthSession(
            session_id="pending-ok", profile_name="owner", open_id="ou_owner",
            device_code="d", user_code="u", verification_uri="https://x",
            scope="", client_id="c", client_secret="s",
            expires_at=now + 600, interval=3, status="pending"),
        "other-user": fa.FeishuAuthSession(
            session_id="other-user", profile_name="owner", open_id="ou_other",
            device_code="d", user_code="u", verification_uri="https://y",
            scope="", client_id="c", client_secret="s",
            expires_at=now + 600, interval=3, status="pending"),
    }
    monkeypatch.setattr(fa, "_sessions", sessions)
    found = fa.find_active_session(profile_name="owner", open_id="ou_owner")
    assert found is not None and found["session_id"] == "pending-ok"

    sessions["pending-ok"].status = "success"
    assert fa.find_active_session(profile_name="owner", open_id="ou_owner") is None
    sessions["pending-ok"].status = "pending"
    sessions["pending-ok"].expires_at = now - 1
    assert fa.find_active_session(profile_name="owner", open_id="ou_owner") is None


# -- expiry rendering --------------------------------------------------------


def test_human_expiry_phrases():
    from hermes_multitenancy.credential_hub import human_expiry

    now = 1_000_000_000_000
    assert human_expiry(None) == ""
    assert human_expiry(now - 1, now_ms=now) == "已过期"
    assert human_expiry(now + 30 * 24 * 3600 * 1000, now_ms=now) == "30天后过期"
    assert human_expiry(now + 5 * 3600 * 1000, now_ms=now) == "5小时后过期"
    assert human_expiry(now + 10 * 60 * 1000, now_ms=now) == "10分钟后过期"


# -- card builder ------------------------------------------------------------


def test_build_hub_card_structure_and_buttons():
    from hermes_multitenancy.credential_hub import CredentialRow
    from hermes_multitenancy.feishu_credential_hub_cards import build_hub_card

    rows = [
        CredentialRow(id="lark-cli", title="Lark-cli", provider="lark", installed=True, status="needs_auth"),
        CredentialRow(id="keep-record", title="Keep-record", provider="keep", installed=True,
                      status="authenticated", expires_at=1_000_000_000_000),
        CredentialRow(id="gitlab", title="GitLab", provider="gitlab", installed=True, status="configured"),
    ]
    card = build_hub_card(
        rows=rows,
        auth_urls={"lark-cli": "https://example.com/authorize"},
        pending_note={"keep-record": "飞书内认证即将开放"},
    )
    assert card["schema"] == "2.0"
    blob = repr(card)
    assert "Lark-cli" in blob and "Keep-record" in blob and "GitLab" in blob
    assert "https://example.com/authorize" in blob  # needs_auth + url → button
    assert "Token 可读" in blob  # configured badge renders


def test_build_hub_card_no_button_when_authenticated():
    from hermes_multitenancy.credential_hub import CredentialRow
    from hermes_multitenancy.feishu_credential_hub_cards import build_hub_card

    rows = [CredentialRow(id="lark-cli", title="Lark-cli", provider="lark", installed=True, status="authenticated")]
    card = build_hub_card(rows=rows, auth_urls={"lark-cli": "https://x/y"})
    assert "https://x/y" not in repr(card)
