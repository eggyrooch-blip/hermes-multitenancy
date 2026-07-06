"""feishu-auth 设备流 scope 与 app 已授权 scope 求交集(修 20027)。"""
from __future__ import annotations

import io
import json

import pytest

from hermes_multitenancy import feishu_uat_auth as fa


def test_scope_intersect_keeps_granted_and_offline_drops_ungranted():
    # 请求 A/B/C,app 只授权 A/B → 只发 A/B,C 被丢;offline_access 恒保留。
    granted = {"contact:user.base:readonly", "im:message"}
    out = fa._scope_with_offline_access(
        "contact:user.base:readonly im:message search:search", granted=granted
    )
    parts = out.split()
    assert "contact:user.base:readonly" in parts
    assert "im:message" in parts
    assert "search:search" not in parts  # app 未授权 → 丢弃(正是 20027 的根因)
    assert "offline_access" in parts  # OAuth grant type,非 app scope,恒保留


def test_scope_offline_access_kept_even_if_not_in_granted():
    # offline_access 不在 granted 里也必须保留。
    out = fa._scope_with_offline_access("im:message", granted={"im:message"})
    assert "offline_access" in out.split()


def test_scope_granted_none_requests_full_set_unchanged():
    # granted=None(查询失败 fail-open)→ 不过滤,行为同旧版。
    out = fa._scope_with_offline_access("im:message search:search", granted=None)
    parts = out.split()
    assert "im:message" in parts
    assert "search:search" in parts  # 未过滤
    assert "offline_access" in parts


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def test_app_granted_scope_names_parses_granted_only(monkeypatch):
    monkeypatch.setattr(fa, "_mint_tenant_access_token", lambda *a, **k: "t-fake")
    payload = {
        "code": 0,
        "data": {
            "scopes": [
                {"scope_name": "im:message", "grant_status": 1},
                {"scope_name": "search:search", "grant_status": 0},  # 未授权
                {"scope_name": "contact:user.base:readonly", "grant_status": 1},
            ]
        },
    }
    monkeypatch.setattr(
        fa.urllib.request, "urlopen",
        lambda *a, **k: _FakeResp(json.dumps(payload).encode("utf-8")),
    )
    got = fa._app_granted_scope_names("cli_x", "sec")
    assert got == {"im:message", "contact:user.base:readonly"}  # grant_status==1 only


def test_app_granted_scope_names_fail_open_returns_none(monkeypatch):
    def _boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(fa, "_mint_tenant_access_token", _boom)
    assert fa._app_granted_scope_names("cli_x", "sec") is None  # fail-open


def test_device_auth_requests_only_granted_scopes(monkeypatch):
    # 集成:_begin_device_authorization_local 用交集后的 scope 调飞书。
    monkeypatch.setattr(
        fa, "_app_granted_scope_names", lambda *a, **k: {"im:message"}
    )
    captured: dict = {}

    def _fake_post(url, payload):
        captured["scope"] = payload["scope"]
        return {
            "device_code": "dc",
            "user_code": "uc",
            "verification_uri_complete": "https://x",
        }

    monkeypatch.setattr(fa, "_api_post", _fake_post)
    fa._begin_device_authorization_local("cli_x", "im:message search:search", "sec")
    sent = captured["scope"].split()
    assert "im:message" in sent
    assert "search:search" not in sent  # 交集丢弃
    assert "offline_access" in sent
