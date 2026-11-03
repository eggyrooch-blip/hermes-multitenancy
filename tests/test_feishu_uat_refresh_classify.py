"""Refresh-token error classification for Feishu UAT renewal."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from hermes_multitenancy import feishu_uat_auth as fua


def _valid_payload(open_id: str = "ou_user_a") -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    return {
        "app_id": "cli_test",
        "user_open_id": open_id,
        "access_token": "t_access_old",
        "refresh_token": "t_refresh_old",
        "expires_at": now_ms + 2 * 3600 * 1000,
        "refresh_expires_at": now_ms + 30 * 24 * 3600 * 1000,
        "scope": "im:message offline_access auth:user.id:read",
        "granted_at": now_ms,
    }


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (None, "ok"),
        (0, "ok"),
        (20050, "retryable"),
        (99991668, "invalid"),
        (20026, "invalid"),
        (20037, "invalid"),
        (20064, "invalid"),
        (20073, "invalid"),
        (12345, "unknown"),
    ],
)
def test_classify_refresh_error_covers_known_and_unknown_codes(code: int | None, expected: str) -> None:
    assert fua.classify_refresh_error(code) == expected


def test_refresh_uat_token_retries_retryable_code_once_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict[str, Any]:
        calls.append((client_id, client_secret, refresh_token))
        return {"code": 20050, "msg": "server error"}

    monkeypatch.setattr(fua, "_refresh_access_token", fake_refresh_access_token)

    with pytest.raises(fua.FeishuUatAuthError) as raised:
        fua._refresh_uat_token("r-token", "app-id", "app-secret")

    assert len(calls) == 2
    assert raised.value.status == 502
    assert raised.value.refresh_class == "retryable_exhausted"
    assert "code=20050" in str(raised.value)


def test_refresh_uat_token_retry_then_invalid_stays_authoritative(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        {"code": 20050, "msg": "server error"},
        {"code": 20026, "msg": "invalid refresh token"},
    ]

    def fake_refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict[str, Any]:
        return responses.pop(0)

    monkeypatch.setattr(fua, "_refresh_access_token", fake_refresh_access_token)

    with pytest.raises(fua.FeishuUatAuthError) as raised:
        fua._refresh_uat_token("r-token", "app-id", "app-secret")

    assert raised.value.status == 401
    assert raised.value.refresh_class == "invalid"


def test_refresh_uat_token_returns_normalised_token_after_retry_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        {"code": 20050, "msg": "server error"},
        {
            "code": 0,
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
            "expires_in": 7200,
            "refresh_token_expires_in": 2_592_000,
            "scope": "im:message offline_access",
        },
    ]
    calls = 0

    def fake_refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return responses.pop(0)

    monkeypatch.setattr(fua, "_refresh_access_token", fake_refresh_access_token)

    refreshed = fua._refresh_uat_token("r-token", "app-id", "app-secret")

    assert calls == 2
    assert refreshed["access_token"] == "fresh-access"
    assert refreshed["refresh_token"] == "fresh-refresh"
    assert refreshed["expires_in"] == 7200


@pytest.mark.parametrize("code", [20026, 20037])
def test_refresh_uat_token_invalid_codes_raise_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    code: int,
) -> None:
    calls = 0

    def fake_refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"code": code, "msg": "refresh token rejected"}

    monkeypatch.setattr(fua, "_refresh_access_token", fake_refresh_access_token)

    with pytest.raises(fua.FeishuUatAuthError) as raised:
        fua._refresh_uat_token("r-token", "app-id", "app-secret")

    assert calls == 1
    assert raised.value.status == 401
    assert raised.value.refresh_class == "invalid"
    assert f"code={code}" in str(raised.value)


def test_refresh_uat_if_needed_propagates_invalid_refresh_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_id = "ou_refresh_invalid"
    payload = _valid_payload(open_id)
    payload["expires_at"] = int(time.time() * 1000) - 60_000
    stored: list[dict[str, Any]] = []

    monkeypatch.setattr(fua, "_assert_route", lambda *args, **kwargs: None)
    monkeypatch.setattr(fua, "_feishu_app_credentials", lambda shared: ("cli_test", "secret"))
    monkeypatch.setattr(fua, "_load_best_uat_payload", lambda *args, **kwargs: payload)
    monkeypatch.setattr(
        fua,
        "_refresh_access_token",
        lambda client_id, client_secret, refresh_token: {"code": 20026, "msg": "invalid refresh token"},
    )
    monkeypatch.setattr(fua, "_store_uat", lambda *args, **kwargs: stored.append(kwargs.get("payload", {})))

    with pytest.raises(fua.FeishuUatAuthError) as raised:
        fua.refresh_uat_if_needed(
            profile_name="alice",
            open_id=open_id,
            shared_home=tmp_path,
            force=True,
        )

    assert raised.value.status == 401
    assert raised.value.refresh_class == "invalid"
    assert stored == []
