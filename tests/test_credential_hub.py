"""Credential hub (`/auth`) — status aggregation + card rendering.

`credential_hub` is the Feishu-side credential-status aggregation in
multitenancy (the intended 归口). Full convergence — hermes-web-ui reading the
same endpoint + credential-set parity — is a follow-up slice. These tests pin
the read layer, card structure, home-dir threading, and device-flow session
reuse.
"""
from __future__ import annotations

import time

import pytest


def _write_keepai(home_dir, *, token="jwt", expired_ms=None, username="owner"):
    keepai = home_dir / ".keepai"
    keepai.mkdir(parents=True, exist_ok=True)
    lines = [f"keep_auth_token={token}"]
    if expired_ms is not None:
        lines.append(f"keep_auth_token_expired={expired_ms}")
    if username is not None:
        lines.append(f"keep_username={username}")
    (keepai / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


# -- keep-record reader ------------------------------------------------------


def test_keep_record_authenticated_with_future_expiry(tmp_path):
    from hermes_multitenancy import credential_hub

    future = int(time.time() * 1000) + 7 * 24 * 3600 * 1000
    _write_keepai(tmp_path, expired_ms=future, username="owner")
    row = credential_hub.keep_record_status(home_dir=tmp_path)
    assert row.id == "keep-record"
    assert row.status == "authenticated"
    assert row.expires_at == future
    assert row.account_hint == "owner"


def test_keep_record_expired_when_past(tmp_path):
    from hermes_multitenancy import credential_hub

    past = int(time.time() * 1000) - 1000
    _write_keepai(tmp_path, expired_ms=past)
    row = credential_hub.keep_record_status(home_dir=tmp_path)
    assert row.status == "expired"
    assert row.expires_at == past


def test_keep_record_missing_when_no_env(tmp_path):
    from hermes_multitenancy import credential_hub

    row = credential_hub.keep_record_status(home_dir=tmp_path)
    assert row.status == "missing"
    assert row.expires_at is None


def test_keep_record_needs_auth_when_token_empty(tmp_path):
    from hermes_multitenancy import credential_hub

    _write_keepai(tmp_path, token="", expired_ms=None, username=None)
    row = credential_hub.keep_record_status(home_dir=tmp_path)
    assert row.status == "needs_auth"


# -- kep-cli reader ----------------------------------------------------------


def test_kep_cli_needs_auth_when_no_tokens(tmp_path):
    from hermes_multitenancy import credential_hub

    row = credential_hub.kep_cli_status(home_dir=tmp_path)
    assert row.id == "kep-cli"
    assert row.status == "needs_auth"


def test_kep_cli_unknown_when_token_present(tmp_path):
    from hermes_multitenancy import credential_hub

    tokens = tmp_path / ".kep-cli" / "tokens"
    tokens.mkdir(parents=True)
    (tokens / "online.enc").write_bytes(b"sealed")
    row = credential_hub.kep_cli_status(home_dir=tmp_path)
    assert row.status == "unknown"


# -- lark-cli reader (reuses feishu_uat_auth.credential_status) --------------


@pytest.mark.parametrize(
    "raw_status,expected",
    [
        ("valid", "authenticated"),
        ("expired", "expired"),
        ("missing", "needs_auth"),
        ("scope_missing", "needs_auth"),
        ("weird", "unknown"),
    ],
)
def test_lark_cli_status_maps_feishu_status(monkeypatch, tmp_path, raw_status, expected):
    from hermes_multitenancy import credential_hub, feishu_uat_auth

    exp = int(time.time() * 1000) + 3600_000
    monkeypatch.setattr(
        feishu_uat_auth,
        "credential_status",
        lambda **kw: {"status": raw_status, "expires_at": exp},
    )
    row = credential_hub.lark_cli_status(
        profile_name="owner", open_id="ou_owner", shared_home=tmp_path
    )
    assert row.id == "lark-cli"
    assert row.status == expected
    assert row.expires_at == exp


def test_lark_cli_status_degrades_on_error(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub, feishu_uat_auth

    def _boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(feishu_uat_auth, "credential_status", _boom)
    row = credential_hub.lark_cli_status(
        profile_name="owner", open_id="ou_owner", shared_home=tmp_path
    )
    assert row.status == "unknown"


# -- aggregation -------------------------------------------------------------


def test_collect_returns_all_three_in_order(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub, feishu_uat_auth

    monkeypatch.setattr(
        feishu_uat_auth,
        "credential_status",
        lambda **kw: {"status": "valid", "expires_at": int(time.time() * 1000) + 1000},
    )
    # profile home: <shared>/profiles/<p>/home
    home = tmp_path / "profiles" / "owner" / "home"
    home.mkdir(parents=True)
    _write_keepai(home, expired_ms=int(time.time() * 1000) + 1000)

    rows = credential_hub.collect_credential_statuses(
        profile_name="owner", open_id="ou_owner", shared_home=tmp_path
    )
    assert [r.id for r in rows] == ["lark-cli", "keep-record", "kep-cli"]
    assert rows[0].status == "authenticated"
    assert rows[1].status == "authenticated"
    assert rows[2].status == "needs_auth"


def test_collect_uses_explicit_home_dir_over_shared(monkeypatch, tmp_path):
    """M1: an explicit home_dir wins over the shared_home-derived path."""
    from hermes_multitenancy import credential_hub, feishu_uat_auth

    monkeypatch.setattr(
        feishu_uat_auth, "credential_status", lambda **kw: {"status": "missing"}
    )
    # keepai lives under the EXPLICIT home dir, not under shared/profiles/...
    explicit_home = tmp_path / "weird_root" / "home"
    explicit_home.mkdir(parents=True)
    _write_keepai(explicit_home, expired_ms=int(time.time() * 1000) + 1000)

    rows = credential_hub.collect_credential_statuses(
        profile_name="owner",
        open_id="ou_owner",
        shared_home=tmp_path,  # would derive a DIFFERENT, empty home
        home_dir=explicit_home,
    )
    keep = next(r for r in rows if r.id == "keep-record")
    assert keep.status == "authenticated"  # found via explicit home_dir


# -- device-flow session reuse (C2) ------------------------------------------


def test_find_active_session_reuses_pending_and_skips_terminal(monkeypatch):
    from hermes_multitenancy import feishu_uat_auth as fa

    now = int(time.time())
    sessions = {
        "pending-ok": fa.FeishuAuthSession(
            session_id="pending-ok", profile_name="owner", open_id="ou_owner",
            device_code="d", user_code="u", verification_uri="https://x",
            scope="", client_id="c", client_secret="s",
            expires_at=now + 600, interval=3, status="pending",
        ),
        "other-user": fa.FeishuAuthSession(
            session_id="other-user", profile_name="owner", open_id="ou_other",
            device_code="d", user_code="u", verification_uri="https://y",
            scope="", client_id="c", client_secret="s",
            expires_at=now + 600, interval=3, status="pending",
        ),
    }
    monkeypatch.setattr(fa, "_sessions", sessions)
    found = fa.find_active_session(profile_name="owner", open_id="ou_owner")
    assert found is not None and found["session_id"] == "pending-ok"

    # terminal/expired sessions are not reused
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
        CredentialRow(id="lark-cli", title="Lark-cli", status="needs_auth"),
        CredentialRow(id="keep-record", title="Keep-record", status="authenticated",
                      expires_at=1_000_000_000_000),
        CredentialRow(id="kep-cli", title="Kep-cli", status="needs_auth"),
    ]
    card = build_hub_card(
        rows=rows,
        auth_urls={"lark-cli": "https://example.com/authorize"},
        pending_note={"kep-cli": "飞书内认证即将开放"},
    )
    assert card["schema"] == "2.0"
    blob = repr(card)
    # all three credentials are listed
    assert "Lark-cli" in blob and "Keep-record" in blob and "Kep-cli" in blob
    # lark-cli (needs_auth + url) gets a button with the authorize url
    assert "https://example.com/authorize" in blob
    # authenticated row does NOT get a button (no url passed for it)
    # pending note rendered for kep-cli
    assert "飞书内认证即将开放" in blob


def test_build_hub_card_no_button_when_authenticated():
    from hermes_multitenancy.credential_hub import CredentialRow
    from hermes_multitenancy.feishu_credential_hub_cards import build_hub_card

    rows = [CredentialRow(id="lark-cli", title="Lark-cli", status="authenticated")]
    card = build_hub_card(rows=rows, auth_urls={"lark-cli": "https://x/y"})
    # url provided but row authenticated → no button element
    assert "https://x/y" not in repr(card)
