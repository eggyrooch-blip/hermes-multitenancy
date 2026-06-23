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


def _make_jwt(exp: int) -> str:
    """Build an unsigned-payload JWT carrying ``exp`` (epoch seconds)."""
    import base64 as _b64
    import json as _json

    def _seg(d: dict) -> str:
        return _b64.urlsafe_b64encode(_json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{_seg({'alg': 'HS256'})}.{_seg({'exp': exp})}.sig"


def _kep_run_stub(*, status_out: str, token_out: str, token_rc: int = 0):
    """A _run replacement that answers the `status` and `token` kep-auth calls."""
    class _StatusProc:
        returncode = 0
        stdout = status_out
        stderr = ""

    class _TokenProc:
        returncode = token_rc
        stdout = token_out
        stderr = ""

    def _run(cmd, *a, **k):
        return _TokenProc() if "token" in cmd else _StatusProc()

    return _run


def _kep_bin(monkeypatch, tmp_path):
    bin_path = tmp_path / "bin" / "kep-auth"
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HERMES_KEP_AUTH_BIN", str(bin_path))
    return bin_path


def test_decode_jwt_exp_ms():
    from hermes_multitenancy import credential_hub

    assert credential_hub._decode_jwt_exp_ms(_make_jwt(1781058668)) == 1781058668000
    assert credential_hub._decode_jwt_exp_ms("not.a.jwt") is None
    assert credential_hub._decode_jwt_exp_ms("a.b") is None
    assert credential_hub._decode_jwt_exp_ms("") is None
    # payload without exp claim
    import base64 as _b64, json as _json
    noexp = "h." + _b64.urlsafe_b64encode(_json.dumps({"name": "x"}).encode()).rstrip(b"=").decode() + ".s"
    assert credential_hub._decode_jwt_exp_ms(noexp) is None


def test_kep_cli_authenticated_when_token_live(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub

    _kep_bin(monkeypatch, tmp_path)
    future = int(credential_hub._now_ms() / 1000) + 3600  # +1h
    monkeypatch.setattr(credential_hub, "_run", _kep_run_stub(
        status_out="state: valid\noperator: owner <owner@keep.com>\n",
        token_out=_make_jwt(future) + "\n",
    ))
    row = credential_hub.kep_cli_status(
        profile_dir=tmp_path, home_dir=tmp_path / "home", profile_name="p",
        shared_home=tmp_path, installed=True,
    )
    assert row.status == "authenticated"
    assert row.account_hint == "owner"
    assert row.expires_at == future * 1000


def test_kep_cli_needs_auth_when_token_expired(monkeypatch, tmp_path):
    """The core bug: kep-auth status says valid but the token is server-stale."""
    from hermes_multitenancy import credential_hub

    _kep_bin(monkeypatch, tmp_path)
    past = int(credential_hub._now_ms() / 1000) - 3600  # expired 1h ago
    monkeypatch.setattr(credential_hub, "_run", _kep_run_stub(
        status_out="state: valid\noperator: owner <owner@keep.com>\n",
        token_out=_make_jwt(past) + "\n",
    ))
    row = credential_hub.kep_cli_status(
        profile_dir=tmp_path, home_dir=tmp_path / "home", profile_name="p",
        shared_home=tmp_path, installed=True,
    )
    assert row.status == "needs_auth"
    assert row.expires_at == past * 1000


def test_kep_cli_unknown_when_token_undecodable(monkeypatch, tmp_path):
    """status valid but token can't be fetched/decoded → don't claim authenticated."""
    from hermes_multitenancy import credential_hub

    _kep_bin(monkeypatch, tmp_path)
    monkeypatch.setattr(credential_hub, "_run", _kep_run_stub(
        status_out="state: valid\noperator: owner <owner@keep.com>\n",
        token_out="", token_rc=1,
    ))
    row = credential_hub.kep_cli_status(
        profile_dir=tmp_path, home_dir=tmp_path / "home", profile_name="p",
        shared_home=tmp_path, installed=True,
    )
    assert row.status == "unknown"


def test_kep_cli_token_whitespace_only_no_crash(monkeypatch, tmp_path):
    """rc=0 but whitespace-only stdout must not raise IndexError → unknown."""
    from hermes_multitenancy import credential_hub

    _kep_bin(monkeypatch, tmp_path)
    monkeypatch.setattr(credential_hub, "_run", _kep_run_stub(
        status_out="state: valid\noperator: owner <owner@keep.com>\n",
        token_out="   \n\n", token_rc=0,
    ))
    row = credential_hub.kep_cli_status(
        profile_dir=tmp_path, home_dir=tmp_path / "home", profile_name="p",
        shared_home=tmp_path, installed=True,
    )
    assert row.status == "unknown"


def test_kep_cli_token_with_banner_lines(monkeypatch, tmp_path):
    """A JWT preceded by banner/warning lines is still found and decoded."""
    from hermes_multitenancy import credential_hub

    _kep_bin(monkeypatch, tmp_path)
    future = int(credential_hub._now_ms() / 1000) + 3600
    monkeypatch.setattr(credential_hub, "_run", _kep_run_stub(
        status_out="state: valid\noperator: owner <owner@keep.com>\n",
        token_out=f"WARNING: using cached key\n{_make_jwt(future)}\n",
    ))
    row = credential_hub.kep_cli_status(
        profile_dir=tmp_path, home_dir=tmp_path / "home", profile_name="p",
        shared_home=tmp_path, installed=True,
    )
    assert row.status == "authenticated"
    assert row.expires_at == future * 1000


# -- feishu-project reader ---------------------------------------------------


def test_feishu_project_missing_when_no_cli(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub

    monkeypatch.setattr(credential_hub, "_meegle_invocation", lambda **k: None)
    monkeypatch.setattr(credential_hub.shutil, "which", lambda n: None)  # no npx either
    row = credential_hub.feishu_project_status(profile_dir=tmp_path, profile_name="p")
    assert row.id == "feishu-project"
    assert row.status == "missing"
    assert row.installed is False


def test_feishu_project_npx_only_not_run_for_status(monkeypatch, tmp_path):
    """M5: npx-only + execution gated off → installed via npx presence, no run."""
    from hermes_multitenancy import credential_hub

    monkeypatch.setattr(credential_hub, "_meegle_invocation", lambda **k: None)
    monkeypatch.setattr(credential_hub.shutil, "which", lambda n: "/usr/bin/npx" if n == "npx" else None)
    called = {"ran": False}
    monkeypatch.setattr(credential_hub, "_run", lambda *a, **k: called.__setitem__("ran", True) or None)
    row = credential_hub.feishu_project_status(profile_dir=tmp_path, profile_name="p")
    assert row.installed is True
    assert row.status == "needs_auth"
    assert called["ran"] is False  # npx must NOT be executed for a status read


def test_feishu_project_authenticated(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub

    monkeypatch.setattr(credential_hub, "_meegle_invocation", lambda **k: ["meegle"])

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

    monkeypatch.setattr(credential_hub, "_meegle_invocation", lambda **k: ["meegle"])

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


def test_lark_cli_authenticated_via_file_cache_when_db_missing(monkeypatch, tmp_path):
    """C1 parity: DB says missing but feishu_uat/*.json cache is valid → authenticated."""
    from hermes_multitenancy import credential_hub, feishu_uat_auth

    monkeypatch.setattr(feishu_uat_auth, "credential_status",
                        lambda **kw: {"status": "missing", "lark_cli": {}})
    uat = tmp_path / "feishu_uat"
    uat.mkdir()
    (uat / "ou_x.json").write_text(
        json.dumps({"access_token": "tok", "expires_at": int(time.time() * 1000) + 3600_000}),
        encoding="utf-8",
    )
    row = credential_hub.lark_cli_status(
        profile_name="s", open_id="ou_s", shared_home=tmp_path, profile_dir=tmp_path
    )
    assert row.status == "authenticated"


def test_lark_cli_authenticated_via_file_cache_when_status_reader_keyless_error(monkeypatch, tmp_path):
    """P0: keyless vault status errors must not hide a valid profile-local UAT."""
    from hermes_multitenancy import credential_hub, feishu_uat_auth

    def _keyless_error(**kw):
        raise RuntimeError("credential encryption key is required")

    monkeypatch.setattr(feishu_uat_auth, "credential_status", _keyless_error)
    uat = tmp_path / "feishu_uat"
    uat.mkdir()
    future = int(time.time() * 1000) + 3600_000
    (uat / "ou_s.json").write_text(
        json.dumps({"access_token": "tok", "expires_at": future}),
        encoding="utf-8",
    )

    row = credential_hub.lark_cli_status(
        profile_name="s",
        open_id="ou_s",
        shared_home=tmp_path,
        profile_dir=tmp_path,
    )

    assert row.status == "authenticated"
    assert row.expires_at == future
    assert row.default_identity == "user"


def test_lark_cli_not_authenticated_when_keyless_vault_metadata_has_no_runtime_access(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub, feishu_uat_auth

    monkeypatch.setattr(
        feishu_uat_auth,
        "credential_status",
        lambda **kw: {
            "status": "valid",
            "storage": "multitenancy_db",
            "has_payload": True,
            "runtime_available": False,
            "lark_cli": {},
        },
    )

    row = credential_hub.lark_cli_status(
        profile_name="s",
        open_id="ou_s",
        shared_home=tmp_path,
        profile_dir=tmp_path,
    )

    assert row.status == "unknown"


def test_lark_cli_authenticated_via_default_identity_user(monkeypatch, tmp_path):
    """C1 parity: DB missing, no file cache, but lark_cli.default_identity == user."""
    from hermes_multitenancy import credential_hub, feishu_uat_auth

    monkeypatch.setattr(feishu_uat_auth, "credential_status",
                        lambda **kw: {"status": "missing", "lark_cli": {"default_identity": "user"}})
    row = credential_hub.lark_cli_status(
        profile_name="s", open_id="ou_s", shared_home=tmp_path, profile_dir=tmp_path
    )
    assert row.status == "authenticated"
    assert row.default_identity == "user"


def test_lark_cli_file_cache_ignores_expired_token(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub, feishu_uat_auth

    monkeypatch.setattr(feishu_uat_auth, "credential_status",
                        lambda **kw: {"status": "missing", "lark_cli": {}})
    uat = tmp_path / "feishu_uat"
    uat.mkdir()
    (uat / "ou_x.json").write_text(
        json.dumps({"access_token": "tok", "expires_at": int(time.time() * 1000) - 1000}),
        encoding="utf-8",
    )
    row = credential_hub.lark_cli_status(
        profile_name="s", open_id="ou_s", shared_home=tmp_path, profile_dir=tmp_path
    )
    assert row.status == "needs_auth"  # expired cache must not count as connected


# -- requirement detection parity (M4) ---------------------------------------


def test_skillhub_sourced_skill_requires_kep_cli(tmp_path):
    """M4: a SkillHub-sourced skill forces kep-cli requirement even w/o kep text."""
    from hermes_multitenancy import credential_hub

    skills_root = tmp_path / "skills"
    (skills_root / ".hub").mkdir(parents=True)
    (skills_root / ".hub" / "lock.json").write_text(
        json.dumps({"installed": {"some-hub-skill": {"version": "1.0.0"}}}), encoding="utf-8"
    )
    sk = skills_root / "some-hub-skill"
    sk.mkdir()
    (sk / "SKILL.md").write_text("name: some-hub-skill\njust a normal skill\n", encoding="utf-8")

    scanned = credential_hub.scan_profile_skills(tmp_path)
    hub_skill = next(s for s in scanned if s.name == "some-hub-skill")
    assert hub_skill.source == "hub"
    assert "kep-cli" in credential_hub.detect_skill_requirements(hub_skill)


def test_requirement_patterns_cover_proximity_and_oauth(tmp_path):
    """M4: ported TS patterns — lark proximity + gitlab oauth2 inline form."""
    from hermes_multitenancy.credential_hub import _ProfileSkill, detect_skill_requirements

    lark = _ProfileSkill(name="x", text="this uses feishu docx export", tags=[])
    assert "lark-cli" in detect_skill_requirements(lark)
    gl = _ProfileSkill(name="y", text="clone via oauth2:${gitlab_token}@host", tags=[])
    assert "gitlab" in detect_skill_requirements(gl)


# -- aggregation -------------------------------------------------------------


def test_collect_returns_all_five_in_order(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub, feishu_uat_auth

    monkeypatch.setattr(feishu_uat_auth, "credential_status",
                        lambda **kw: {"status": "valid", "expires_at": int(time.time() * 1000) + 1000})
    monkeypatch.setattr(credential_hub, "_meegle_invocation", lambda **k: None)
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
    monkeypatch.setattr(credential_hub, "_meegle_invocation", lambda **k: None)
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


def test_build_hub_card_renders_pregenerated_entries():
    """方案B：hub card 直接嵌入预生成 URL / QR，不再输出 callback."""
    import json
    from hermes_multitenancy.credential_hub import CredentialRow
    from hermes_multitenancy.feishu_credential_hub_cards import build_hub_card

    rows = [
        CredentialRow(id="lark-cli", title="Lark-cli", provider="lark", installed=True, status="needs_auth"),
        CredentialRow(id="keep-record", title="Keep-record", provider="keep", installed=True, status="needs_auth"),
        CredentialRow(id="gitlab", title="GitLab", provider="gitlab", installed=False, status="missing"),
    ]
    card = build_hub_card(
        rows=rows,
        auth_urls={"lark-cli": "https://x/lark"},
        qr_image_keys={"keep-record": "img_k"},
    )
    assert card["schema"] == "2.0"
    blob = json.dumps(card, ensure_ascii=False)
    assert "Lark-cli" in blob and "Keep-record" in blob and "GitLab" in blob
    assert "https://x/lark" in blob
    assert '"multi_url"' in blob
    assert "img_k" in blob
    assert '"tag": "img"' in blob
    assert '"callback"' not in blob
    assert '"cred"' not in blob
    assert blob.count('"multi_url"') == 1
    assert blob.count('"tag": "img"') == 1


def test_build_hub_card_authenticated_row_has_no_entry():
    import json
    from hermes_multitenancy.credential_hub import CredentialRow
    from hermes_multitenancy.feishu_credential_hub_cards import build_hub_card

    rows = [CredentialRow(id="lark-cli", title="Lark-cli", provider="lark", installed=True,
                          status="authenticated", action={"kind": "feishu_device_flow", "label": "重新授权"})]
    blob = json.dumps(build_hub_card(rows=rows, auth_urls={"lark-cli": "https://x"}), ensure_ascii=False)
    assert "已认证" in blob
    assert '"multi_url"' not in blob
    assert '"tag": "img"' not in blob
    assert '"callback"' not in blob


def test_filter_hub_rows_for_auth_keeps_only_three_mvp_creds():
    import json
    from hermes_multitenancy import router
    from hermes_multitenancy.credential_hub import CredentialRow, CREDENTIAL_ORDER
    from hermes_multitenancy.feishu_credential_hub_cards import build_hub_card

    rows = [
        CredentialRow(id=cid, title=cid, provider=cid, installed=True, status="needs_auth")
        for cid in CREDENTIAL_ORDER
    ]
    filtered = router._filter_hub_rows_for_auth(rows)

    assert [row.id for row in filtered] == ["lark-cli", "keep-record", "kep-cli"]
    assert "gitlab" not in [row.id for row in filtered]
    assert "feishu-project" not in [row.id for row in filtered]

    blob = json.dumps(build_hub_card(rows=filtered), ensure_ascii=False)
    assert "GitLab" not in blob
    assert "飞书项目" not in blob


def test_qr_and_url_card_builders():
    import json
    from hermes_multitenancy.feishu_credential_hub_cards import build_qr_card, build_url_card
    qr = json.dumps(build_qr_card("Keep-record", "img_v3_demo"), ensure_ascii=False)
    assert '"tag": "img"' in qr and "img_v3_demo" in qr and "扫码" in qr
    url = json.dumps(build_url_card("kep-cli", "https://auth.example.com/x", label_zh="前往登录"), ensure_ascii=False)
    assert "https://auth.example.com/x" in url and "前往登录" in url
