from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from hermes_multitenancy import feishu_uat_auth as fa


def _token() -> dict[str, object]:
    return {
        "app_id": "cli_unique",
        "access_token": "access-secret",
        "refresh_token": "refresh-secret",
        "expires_in": 7200,
        "refresh_token_expires_in": 30 * 24 * 3600,
        "scope": "im:message offline_access",
    }



def _stub_refresh(monkeypatch, owner_of: dict[str, str] | None = None):
    """Stub Feishu's refresh exchange.

    Feishu rotates the pair on every exchange and the account behind the new
    access token is the account that owns the REFRESH token — that asymmetry is
    the whole point of the fix, so the stub reproduces it: the minted access
    token carries the refresh token's owner, not the caller's claim.
    """
    owner_of = owner_of or {}

    def _refresh(refresh_token: str, client_id: str, client_secret: str) -> dict[str, object]:
        owner = owner_of.get(refresh_token, "ou_owner")
        return {
            "access_token": f"access-of-{owner}",
            "refresh_token": f"rotated-{refresh_token}",
            "expires_in": 7200,
            "refresh_expires_in": 30 * 24 * 3600,
            "scope": "im:message offline_access",
        }

    monkeypatch.setattr(fa, "_refresh_uat_token", _refresh)

    def _user_info(access_token: str) -> dict[str, object]:
        # 只有刷新铸出来的 "access-of-ou_x" 才带别人的身份;调用方**提交**的那个
        # access token 一律当成本人合法的(ou_owner) —— 漏洞的真实形态正是
        # "提交的 access 是自己的、refresh 是别人的",stub 必须还原这一点,
        # 否则这条测试在有洞的版本下也会因为别的原因 403,红绿就是假的。
        raw = str(access_token)
        if raw.startswith("access-of-"):
            return {"open_id": raw.replace("access-of-", "")}
        return {"open_id": "ou_owner"}

    return _refresh, _user_info


def test_login_oauth_scope_is_the_app_granted_intersection(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(fa, "_feishu_app_credentials", lambda _home: ("cli_unique", "secret"))
    monkeypatch.setattr(fa, "_app_granted_scope_names", lambda *_args: {"im:message"})
    monkeypatch.setenv("HERMES_FEISHU_UAT_DEFAULT_SCOPE", "im:message search:search")

    assert fa.login_oauth_scope(shared_home=tmp_path) == "im:message offline_access"


def test_import_login_uat_live_verifies_owner_before_store(monkeypatch, tmp_path: Path):
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(fa, "_assert_route", lambda _home, profile, owner: calls.append(("route", (profile, owner))))
    monkeypatch.setattr(fa, "_feishu_app_credentials", lambda _home: ("cli_unique", "secret"))
    _stub_refresh(monkeypatch)
    monkeypatch.setattr(fa, "_fetch_user_info", lambda token: {"open_id": "ou_owner"})
    monkeypatch.setattr(
        fa,
        "_store_uat",
        lambda _home, profile, owner, payload: calls.append(("store", (profile, owner, payload))) or True,
    )

    result = fa.import_login_oauth_uat(
        profile_name="profile-owner",
        open_id="ou_owner",
        token=_token(),
        shared_home=tmp_path,
    )

    assert result == {"ok": True}
    assert calls[0] == ("route", ("profile-owner", "ou_owner"))
    stored = calls[-1][1][2]
    assert stored["user_open_id"] == "ou_owner"
    assert stored["app_id"] == "cli_unique"
    assert stored["scope"] == "im:message offline_access"


@pytest.mark.parametrize(
    ("token_patch", "message"),
    [
        ({"app_id": "cli_other"}, "app"),
        ({"scope": "im:message"}, "offline_access"),
        ({"refresh_token": ""}, "refresh_token"),
        ({"expires_in": 10**9}, "expires_in"),
        ({"expires_in": None}, "expires_in"),
    ],
)
def test_import_login_uat_rejects_invalid_payload_without_store(
    monkeypatch, tmp_path: Path, token_patch: dict[str, object], message: str
):
    stored: list[object] = []
    monkeypatch.setattr(fa, "_assert_route", lambda *_args: None)
    monkeypatch.setattr(fa, "_feishu_app_credentials", lambda _home: ("cli_unique", "secret"))
    _stub_refresh(monkeypatch)
    monkeypatch.setattr(fa, "_fetch_user_info", lambda _token: {"open_id": "ou_owner"})
    monkeypatch.setattr(fa, "_store_uat", lambda *_args: stored.append(_args))
    token = {**_token(), **token_patch}

    with pytest.raises(fa.FeishuUatAuthError, match=message):
        fa.import_login_oauth_uat(
            profile_name="profile-owner",
            open_id="ou_owner",
            token=token,
            shared_home=tmp_path,
        )

    assert stored == []


def test_import_login_uat_writes_vault_and_profile_mirror(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.routing import RoutingTable

    route = RoutingTable(tmp_path / "multitenancy.db")
    try:
        route.upsert(
            user_id="user-owner",
            profile_name="profile-owner",
            open_id="ou_owner",
            provenance="sync",
        )
    finally:
        route.close()
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    monkeypatch.setattr(fa, "_feishu_app_credentials", lambda _home: ("cli_unique", "secret"))
    _stub_refresh(monkeypatch)
    monkeypatch.setattr(fa, "_fetch_user_info", lambda _token: {"open_id": "ou_owner"})

    assert fa.import_login_oauth_uat(
        profile_name="profile-owner",
        open_id="ou_owner",
        token=_token(),
        shared_home=tmp_path,
    ) == {"ok": True}

    store = CredentialStore(tmp_path / "multitenancy.db")
    try:
        payload = store.get_secret_for_runtime(
            profile_name="profile-owner",
            subject_id="ou_owner",
            provider="feishu",
            secret_kind="uat",
        )
    finally:
        store.close()
    mirror = tmp_path / "profiles" / "profile-owner" / "feishu_uat" / "ou_owner.json"
    # 存的是刷新后的那一对：那才是刚被证明属于 ou_owner 的凭据
    assert payload["access_token"] == "access-of-ou_owner"
    assert mirror.is_file()


def test_import_login_uat_rejects_live_owner_mismatch_without_store(monkeypatch, tmp_path: Path):
    stored: list[object] = []
    monkeypatch.setattr(fa, "_assert_route", lambda *_args: None)
    monkeypatch.setattr(fa, "_feishu_app_credentials", lambda _home: ("cli_unique", "secret"))
    _stub_refresh(monkeypatch)
    monkeypatch.setattr(fa, "_fetch_user_info", lambda _token: {"open_id": "ou_other"})
    monkeypatch.setattr(fa, "_store_uat", lambda *_args: stored.append(_args))

    with pytest.raises(fa.FeishuUatAuthError, match="does not match"):
        fa.import_login_oauth_uat(
            profile_name="profile-owner",
            open_id="ou_owner",
            token=_token(),
            shared_home=tmp_path,
        )

    assert stored == []


def test_internal_login_oauth_endpoints_require_master_key_and_trusted_owner(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy import webui_broker_server as broker

    monkeypatch.setattr(broker, "_run_broker_key", lambda: "master-key")
    monkeypatch.setattr(feishu_uat_auth, "login_oauth_scope", lambda: "im:message offline_access")
    seen: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(
        feishu_uat_auth,
        "import_login_oauth_uat",
        lambda *, profile_name, open_id, token: seen.append((profile_name, open_id, token)) or {"ok": True},
    )

    async def runner():
        client = TestClient(TestServer(broker.create_run_broker_app()))
        await client.start_server()
        try:
            denied_scope = await client.get(
                "/api/run-broker/internal/feishu/oauth-scope",
                headers={"Authorization": "Bearer run-scoped-key"},
            )
            scope = await client.get(
                "/api/run-broker/internal/feishu/oauth-scope",
                headers={"Authorization": "Bearer master-key"},
            )
            missing_owner = await client.post(
                "/api/run-broker/internal/feishu/uat/import",
                headers={"Authorization": "Bearer master-key"},
                json={"profile_name": "profile-owner", "token": _token()},
            )
            imported = await client.post(
                "/api/run-broker/internal/feishu/uat/import",
                headers={
                    "Authorization": "Bearer master-key",
                    "X-Hermes-Owner-Open-Id": "ou_owner",
                },
                json={
                    "profile_name": "profile-owner",
                    "open_id": "ou_forged",
                    "token": _token(),
                },
            )
            return (
                denied_scope.status,
                scope.status,
                await scope.json(),
                missing_owner.status,
                imported.status,
                await imported.json(),
            )
        finally:
            await client.close()

    result = asyncio.run(runner())
    assert result == (
        401,
        200,
        {"scope": "im:message offline_access"},
        403,
        200,
        {"ok": True},
    )
    assert seen == [("profile-owner", "ou_owner", _token())]


def test_import_login_uat_rechecks_route_after_live_user_info(monkeypatch, tmp_path: Path):
    """The route may move while the live user_info call is in flight (TOCTOU).

    _assert_route runs before the network hop, so only the in-transaction
    re-check inside _store_uat stands between a moved route and a cross-tenant
    credential write. Drive the move from the user_info stub — the one point
    that is genuinely concurrent with the commit — and prove nothing lands.
    """
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.routing import RoutingTable

    route = RoutingTable(tmp_path / "multitenancy.db")
    try:
        route.upsert(
            user_id="user-owner",
            profile_name="profile-owner",
            open_id="ou_owner",
            provenance="sync",
        )
    finally:
        route.close()
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    monkeypatch.setattr(fa, "_feishu_app_credentials", lambda _home: ("cli_unique", "secret"))
    _stub_refresh(monkeypatch)

    def _move_route_then_answer(_token: str) -> dict[str, object]:
        # open_id is UNIQUE in multitenancy_routing, so a move is an UPDATE of the
        # existing row, not a second binding — write it the way a concurrent sync
        # would.
        with sqlite3.connect(tmp_path / "multitenancy.db") as conn:
            conn.execute(
                "UPDATE multitenancy_routing SET profile_name = ? WHERE open_id = ?",
                ("profile-other", "ou_owner"),
            )
        return {"open_id": "ou_owner"}

    monkeypatch.setattr(fa, "_fetch_user_info", _move_route_then_answer)

    with pytest.raises(fa.FeishuUatAuthError) as excinfo:
        fa.import_login_oauth_uat(
            profile_name="profile-owner",
            open_id="ou_owner",
            token=_token(),
            shared_home=tmp_path,
        )
    assert excinfo.value.status == 403

    store = CredentialStore(tmp_path / "multitenancy.db")
    try:
        status = store.get_status(
            profile_name="profile-owner",
            subject_id="ou_owner",
            provider="feishu",
            secret_kind="uat",
        )
    finally:
        store.close()
    assert status.get("status") != "valid"
    assert not (tmp_path / "profiles" / "profile-owner" / "feishu_uat" / "ou_owner.json").exists()
    assert not (tmp_path / "profiles" / "profile-other" / "feishu_uat" / "ou_owner.json").exists()


def test_import_login_uat_rejects_spliced_token_pair_without_store(monkeypatch, tmp_path: Path):
    """A valid access token from one owner + a refresh token from another must fail.

    Only the access token can be checked with user_info, so verifying just that
    accepted the pair and stored it; the later refresh would then mint the OTHER
    account's access token under this owner's name — cross-tenant credential
    access (codex review #p0). The fix spends the refresh token and checks who
    comes back, so a spliced pair can no longer pass.
    """
    stored: list[object] = []
    monkeypatch.setattr(fa, "_assert_route", lambda *_args: None)
    monkeypatch.setattr(fa, "_feishu_app_credentials", lambda _home: ("cli_unique", "secret"))
    _, user_info = _stub_refresh(monkeypatch, {"refresh-of-other": "ou_other"})
    monkeypatch.setattr(fa, "_fetch_user_info", user_info)
    monkeypatch.setattr(fa, "_store_uat", lambda *_args: stored.append(_args))

    spliced = _token()
    spliced["refresh_token"] = "refresh-of-other"  # belongs to ou_other

    with pytest.raises(fa.FeishuUatAuthError) as excinfo:
        fa.import_login_oauth_uat(
            profile_name="profile-owner",
            open_id="ou_owner",
            token=spliced,
            shared_home=tmp_path,
        )

    assert excinfo.value.status == 403
    assert stored == []


def test_import_login_uat_stores_the_rotated_pair_not_the_submitted_one(monkeypatch, tmp_path: Path):
    """Feishu rotates on every exchange, so the submitted pair may already be spent."""
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(fa, "_assert_route", lambda *_args: None)
    monkeypatch.setattr(fa, "_feishu_app_credentials", lambda _home: ("cli_unique", "secret"))
    _, user_info = _stub_refresh(monkeypatch)
    monkeypatch.setattr(fa, "_fetch_user_info", user_info)
    monkeypatch.setattr(
        fa,
        "_store_uat",
        lambda _home, profile, owner, payload: calls.append(("store", payload)) or True,
    )

    assert fa.import_login_oauth_uat(
        profile_name="profile-owner",
        open_id="ou_owner",
        token=_token(),
        shared_home=tmp_path,
    ) == {"ok": True}

    payload = calls[-1][1]
    assert payload["access_token"] == "access-of-ou_owner"
    assert payload["refresh_token"] == "rotated-refresh-secret"
    assert payload["refresh_token"] != _token()["refresh_token"]
