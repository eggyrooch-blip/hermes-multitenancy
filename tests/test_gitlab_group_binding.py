"""Group GitLab binding over the real broker endpoint (group-agent-gitlab-binding).

Lives in its own file so the TEST gate can target exactly the surface this slug
changed. The shared harness (`_gitlab_app`, `_run`) is imported from the broker
test module — same fixtures, same server construction, no duplication.
"""
import pathlib  # noqa: F401  (used by the imported harness)

import pytest  # noqa: F401

from tests.test_webui_broker_server import _gitlab_app, _run


def _gitlab_group_routes(monkeypatch, tmp_path):
    """A live routing table with one group row (owner ou_owner)."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable

    table = RoutingTable(str(tmp_path / "routing.db"))
    table.upsert_group(chat_id="oc_grp", profile_name="grp_profile", owner_open_id="ou_owner")
    monkeypatch.setattr(router_mod, "_routing_table", table)
    return table


def test_gitlab_group_bind_by_owner_targets_the_group_profile(monkeypatch, tmp_path):
    """群主带群 profile 头提交 → 落群 profile，回执明示作用域。"""
    from aiohttp.test_utils import TestClient, TestServer

    captured = {}
    app = _gitlab_app(monkeypatch, capture=captured, resolve_to="owner_profile")
    table = _gitlab_group_routes(monkeypatch, tmp_path)

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            res = await client.post(
                "/api/run-broker/credentials/gitlab",
                headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner",
                    "X-Hermes-Profile": "grp_profile",
                },
                json={"token": "glpat-x", "tier": "read"},
            )
            body = await res.json()
        finally:
            await client.close()
            table.close()
        assert res.status == 200, body
        assert captured.get("profile_name") == "grp_profile", captured
        assert body.get("profile_scope") == "group"
        assert "本群" in (body.get("note") or "")

    _run(runner)


def test_gitlab_group_bind_by_non_owner_falls_back_to_personal(monkeypatch, tmp_path):
    """非群主带群头 → 落个人（现契约），回执说明落点，绝不静默错位。"""
    from aiohttp.test_utils import TestClient, TestServer

    captured = {}
    app = _gitlab_app(monkeypatch, capture=captured, resolve_to="member_profile")
    table = _gitlab_group_routes(monkeypatch, tmp_path)

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            res = await client.post(
                "/api/run-broker/credentials/gitlab",
                headers={
                    "X-Hermes-Owner-Open-Id": "ou_member",
                    "X-Hermes-Profile": "grp_profile",
                },
                json={"token": "glpat-x", "tier": "read"},
            )
            body = await res.json()
        finally:
            await client.close()
            table.close()
        assert res.status == 200, body
        assert captured.get("profile_name") == "member_profile", captured
        assert body.get("profile_scope") == "personal"
        assert "群主" in (body.get("note") or "")

    _run(runner)


def test_gitlab_bind_with_unknown_or_user_profile_header_stays_personal(monkeypatch, tmp_path):
    """未知 profile / 用户 profile 头都不改变现行为：落个人、无附注。"""
    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable

    captured = {}
    app = _gitlab_app(monkeypatch, capture=captured, resolve_to="owner_profile")
    table = RoutingTable(str(tmp_path / "routing.db"))
    table.upsert(user_id="peer", profile_name="peer_profile", open_id="ou_peer")
    monkeypatch.setattr(router_mod, "_routing_table", table)

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            for requested in ("ghost_profile", "peer_profile"):
                captured.clear()
                res = await client.post(
                    "/api/run-broker/credentials/gitlab",
                    headers={
                        "X-Hermes-Owner-Open-Id": "ou_owner",
                        "X-Hermes-Profile": requested,
                    },
                    json={"token": "glpat-x", "tier": "read"},
                )
                body = await res.json()
                assert res.status == 200, body
                assert captured.get("profile_name") == "owner_profile", (requested, captured)
                assert body.get("profile_scope") == "personal"
                assert "note" not in body, (requested, body)
        finally:
            await client.close()
            table.close()

    _run(runner)
