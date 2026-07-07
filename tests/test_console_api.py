"""Console M1a read-only endpoints — SPEC console-m1a-endpoints acceptance."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_multitenancy import console_api


def _seed_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE multitenancy_routing (
                user_id TEXT PRIMARY KEY,
                profile_name TEXT NOT NULL,
                open_id TEXT,
                union_id TEXT,
                kind TEXT NOT NULL DEFAULT 'user',
                active INTEGER NOT NULL DEFAULT 1,
                chat_id TEXT,
                owner_open_id TEXT,
                display_label TEXT,
                last_active_at INTEGER,
                synced_at INTEGER NOT NULL DEFAULT 0,
                provenance TEXT NOT NULL DEFAULT 'sync'
            )
            """
        )
        rows = [
            ("u-1", "feishu_aaa111", "ou_aaa111", "on_aaa111", "user", 1, None, None, "张三", 200, 100, "sync"),
            ("u-2", "feishu_bbb222", "ou_bbb222", "on_bbb222", "user", 1, None, None, "李四", 100, 100, "sync"),
            ("g-1", "feishu_group_ccc", None, None, "group", 1, "oc_ccc333", "ou_aaa111", "数据组日常", None, 100, "auto"),
            ("u-3", "feishu_gone", "ou_gone", None, "user", 0, None, None, "离职者", 50, 100, "sync"),
        ]
        conn.executemany(
            "INSERT INTO multitenancy_routing VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        conn.execute(
            """
            CREATE TABLE skillhub_events (
                event_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                received_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.executemany(
            "INSERT INTO skillhub_events VALUES (?,?,?)",
            [
                ("evt-1", "queued", 1),
                ("evt-2", "queued_unknown_type", 2),
                ("evt-3", "failed", 3),
                ("evt-4", "installed", 4),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _seed_audit(audit_path: Path) -> None:
    lines = [
        {"@timestamp": "2026-07-07T01:00:00+00:00", "profile": "feishu_aaa111",
         "platform": "feishu", "finish_reason": "stop", "content": "NORMAL_BODY"},
        {"@timestamp": "2026-07-07T02:00:00+00:00", "profile": "feishu_aaa111",
         "platform": "feishu", "finish_reason": "error", "content": "SECRET_BODY_MUST_NOT_LEAK"},
        {"@timestamp": "2026-07-07T03:00:00+00:00", "profile": "feishu_bbb222",
         "platform": "webui", "finish_reason": "error", "content": "OTHER_PROFILE"},
        "not-json-garbage",
    ]
    with audit_path.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line if isinstance(line, str) else json.dumps(line, ensure_ascii=False))
            fh.write("\n")


@pytest.fixture()
def console_env(tmp_path, monkeypatch):
    """Isolated db/shared-home/audit + master key; resets the module cache."""
    db_path = tmp_path / "multitenancy.db"
    _seed_db(db_path)
    shared_home = tmp_path / "hermes-home"
    marker_dir = shared_home / "profiles" / "feishu_aaa111" / "feishu_uat"
    marker_dir.mkdir(parents=True)
    (marker_dir / "ou_aaa111.needs_reauth").write_text(
        json.dumps({"reason": "refresh_token_expired", "ts": 1751000000}), encoding="utf-8"
    )
    audit_path = tmp_path / "conversation-audit.jsonl"
    _seed_audit(audit_path)

    monkeypatch.setenv("HERMES_MULTITENANCY_DB", str(db_path))
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_PATH", str(audit_path))
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "console-test-key")
    console_api._reauth_cache["ts"] = 0.0
    console_api._reauth_cache["items"] = []
    return {"db": db_path, "shared_home": shared_home, "audit": audit_path,
            "headers": {"Authorization": "Bearer console-test-key"}}


def _run_with_client(fn):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def runner():
        app = create_run_broker_app(
            mark_seen=lambda _request: True, sandbox_available=lambda: True
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            return await fn(client)
        finally:
            await client.close()

    return asyncio.run(runner())


# ---------------------------------------------------------------- overview --


def test_overview_shape_and_counts(console_env):
    async def scenario(client):
        resp = await client.get("/api/run-broker/console/overview", headers=console_env["headers"])
        assert resp.status == 200
        return await resp.json()

    body = _run_with_client(scenario)
    assert body["broker"]["alive"] is True
    assert body["broker"]["uptime_s"] >= 0
    assert body["gateway_alive"] is True
    assert body["active"] == {"group": 1, "user": 2}  # active=1 rows only
    assert body["skillhub"] == {"queued": 2, "failed": 1, "installed": 1, "total": 4}
    assert body["reauth_pending_count"] == 1
    assert body["generated_at"]


def test_overview_unauthorized_without_key(console_env):
    async def scenario(client):
        resp = await client.get("/api/run-broker/console/overview")
        return resp.status

    assert _run_with_client(scenario) == 401


def test_console_endpoints_reject_post(console_env):
    async def scenario(client):
        resp = await client.post(
            "/api/run-broker/console/overview", headers=console_env["headers"]
        )
        return resp.status

    assert _run_with_client(scenario) == 405


# ---------------------------------------------------------------- profiles --


def test_profile_search_matches_and_pages(console_env):
    async def scenario(client):
        by_name = await client.get(
            "/api/run-broker/console/profiles", params={"q": "张三"},
            headers=console_env["headers"],
        )
        by_ou = await client.get(
            "/api/run-broker/console/profiles", params={"q": "ou_bbb"},
            headers=console_env["headers"],
        )
        paged = await client.get(
            "/api/run-broker/console/profiles", params={"limit": 2, "offset": 0},
            headers=console_env["headers"],
        )
        return await by_name.json(), await by_ou.json(), await paged.json()

    by_name, by_ou, paged = _run_with_client(scenario)
    assert by_name["total"] == 1
    assert by_name["items"][0]["profile_name"] == "feishu_aaa111"
    assert by_name["items"][0]["display_label"] == "张三"
    assert by_ou["total"] == 1 and by_ou["items"][0]["open_id"] == "ou_bbb222"
    assert paged["total"] == 4 and len(paged["items"]) == 2
    # newest activity first
    assert paged["items"][0]["profile_name"] == "feishu_aaa111"


def test_profile_search_clamps_and_validates_limit(console_env):
    async def scenario(client):
        clamped = await client.get(
            "/api/run-broker/console/profiles", params={"limit": 5000},
            headers=console_env["headers"],
        )
        bad = await client.get(
            "/api/run-broker/console/profiles", params={"limit": "abc"},
            headers=console_env["headers"],
        )
        return await clamped.json(), bad.status

    clamped, bad_status = _run_with_client(scenario)
    assert clamped["limit"] == 100
    assert bad_status == 400


def test_profile_search_like_wildcards_are_literal(console_env):
    async def scenario(client):
        resp = await client.get(
            "/api/run-broker/console/profiles", params={"q": "%"},
            headers=console_env["headers"],
        )
        return await resp.json()

    body = _run_with_client(scenario)
    assert body["total"] == 0  # '%' is matched literally, not as a wildcard


# ------------------------------------------------------------------ detail --


def test_profile_detail_aggregates_and_redacts(console_env, monkeypatch):
    from hermes_multitenancy.connectors import registry

    class _FakeStatus:
        def to_dict(self):
            return {"id": "lark-cli", "status": "authenticated", "installed": True}

    monkeypatch.setattr(
        registry, "collect_connector_statuses", lambda **kwargs: [_FakeStatus()]
    )
    from hermes_multitenancy import cron_api

    monkeypatch.setattr(
        cron_api, "list_jobs", lambda name, include_disabled=True: [{"id": "job-1", "name": "日报"}]
    )

    async def scenario(client):
        ok = await client.get(
            "/api/run-broker/console/profiles/feishu_aaa111", headers=console_env["headers"]
        )
        missing = await client.get(
            "/api/run-broker/console/profiles/no_such_profile", headers=console_env["headers"]
        )
        return await ok.json(), ok.status, missing.status

    body, ok_status, missing_status = _run_with_client(scenario)
    assert ok_status == 200 and missing_status == 404
    assert body["routing"]["open_id"] == "ou_aaa111"
    assert body["connectors"] == [{"id": "lark-cli", "status": "authenticated", "installed": True}]
    assert body["cron_jobs"] == [{"id": "job-1", "name": "日报"}]
    # error rows: category+ts only, own profile only, no message content anywhere
    assert body["recent_errors"] == [
        {"ts": "2026-07-07T02:00:00+00:00", "category": "error", "platform": "feishu"}
    ]
    assert "SECRET_BODY_MUST_NOT_LEAK" not in json.dumps(body)


def test_recent_errors_tail_is_bounded(console_env, monkeypatch):
    # A file larger than the tail window must still be read only partially.
    audit = console_env["audit"]
    filler = json.dumps(
        {"@timestamp": "x", "profile": "other", "finish_reason": "stop", "content": "pad" * 40}
    )
    with audit.open("a", encoding="utf-8") as fh:
        for _ in range(20000):
            fh.write(filler + "\n")
        fh.write(
            json.dumps(
                {"@timestamp": "2026-07-07T04:00:00+00:00", "profile": "feishu_aaa111",
                 "platform": "feishu", "finish_reason": "error", "content": "late"}
            )
            + "\n"
        )
    rows = console_api.recent_errors_for_profile("feishu_aaa111")
    # the early 02:00 row fell outside the bounded window; the late row is present
    assert rows == [
        {"ts": "2026-07-07T04:00:00+00:00", "category": "error", "platform": "feishu"}
    ]


# ---------------------------------------------------------- reauth-pending --


def test_reauth_pending_lists_and_caches(console_env):
    async def scenario(client):
        first = await client.get(
            "/api/run-broker/console/reauth-pending", headers=console_env["headers"]
        )
        first_body = await first.json()
        # delete the marker: a second call must still serve the cached item,
        # proving the TTL cache prevents a rescan (mechanical-disk red line)
        marker = (
            console_env["shared_home"] / "profiles" / "feishu_aaa111" / "feishu_uat"
            / "ou_aaa111.needs_reauth"
        )
        marker.unlink()
        second = await client.get(
            "/api/run-broker/console/reauth-pending", headers=console_env["headers"]
        )
        return first_body, await second.json()

    first, second = _run_with_client(scenario)
    assert first["count"] == 1 and first["cache_age_s"] == 0.0
    assert first["items"][0] == {
        "profile": "feishu_aaa111", "open_id": "ou_aaa111",
        "reason": "refresh_token_expired", "ts": 1751000000,
    }
    assert second["count"] == 1  # cache hit — marker deletion not observed
    assert second["cache_age_s"] >= 0.0


def test_reauth_ttl_floor_is_600s():
    assert console_api._REAUTH_TTL_S >= 600


# ---------------------------------------------------------------- degraded --


def test_missing_db_degrades_cleanly(console_env, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_MULTITENANCY_DB", str(tmp_path / "absent.db"))

    async def scenario(client):
        overview = await client.get(
            "/api/run-broker/console/overview", headers=console_env["headers"]
        )
        search = await client.get(
            "/api/run-broker/console/profiles", headers=console_env["headers"]
        )
        detail = await client.get(
            "/api/run-broker/console/profiles/feishu_aaa111", headers=console_env["headers"]
        )
        return overview.status, await search.json(), detail.status

    overview_status, search_body, detail_status = _run_with_client(scenario)
    assert overview_status == 200  # degraded but alive
    assert search_body["available"] is False and search_body["items"] == []
    assert detail_status == 404
