"""WebUI run broker HTTP seam tests."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types
from pathlib import Path

import pytest


async def _read_sse_data_line(response, *, timeout: float = 2.0):
    while True:
        line = await asyncio.wait_for(response.content.readline(), timeout=timeout)
        if line.startswith(b"data: "):
            return line


def test_webui_run_broker_endpoint_streams_channel_neutral_events():
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def runner():
        seen = []

        async def dispatch(request):
            seen.append(request)
            return f"echo:{request.content}"

        app = create_run_broker_app(
            dispatch_agent=dispatch,
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post("/api/run-broker/runs", json={
                "channel": "webui",
                "profile_name": "owner",
                "user_key": "ou_webui",
                "content": "hello broker",
                "session_id": "session-webui",
                "delivery_mode": "socket",
                "credential_subject": "ou_webui",
                "requires_host_tools": True,
                "metadata": {"model": "gpt-5.4"},
            })
            body = await response.text()
        finally:
            await client.close()

        assert response.status == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert '"kind": "content"' in body
        assert '"text": "echo:hello broker"' in body
        assert '"kind": "done"' in body
        assert len(seen) == 1
        assert seen[0].channel == "webui"
        assert seen[0].profile_name == "owner"
        assert seen[0].user_key == "ou_webui"
        assert seen[0].requires_host_tools is True

    asyncio.run(runner())


def test_webui_run_broker_accepts_image_sized_json_body(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_CLIENT_MAX_SIZE_BYTES", raising=False)
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_CLIENT_MAX_SIZE_MB", raising=False)

    async def runner():
        seen = []

        async def dispatch(request):
            seen.append(request)
            return "accepted large body"

        app = create_run_broker_app(
            dispatch_agent=dispatch,
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post(
                "/api/run-broker/runs",
                data=json.dumps({
                    "channel": "webui",
                    "profile_name": "owner",
                    "user_key": "ou_webui",
                    "content": "image follow-up",
                    "session_id": "session-large-image",
                    "metadata": {"input": "x" * (2 * 1024 * 1024)},
                }),
                headers={"Content-Type": "application/json"},
            )
            body = await response.text()
        finally:
            await client.close()

        assert response.status == 200
        assert '"kind": "content"' in body
        assert '"text": "accepted large body"' in body
        assert len(seen) == 1
        assert seen[0].metadata["input"].startswith("xxx")

    asyncio.run(runner())


def test_webui_run_broker_client_max_size_defaults_to_32mb(monkeypatch):
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_CLIENT_MAX_SIZE_BYTES", raising=False)
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_CLIENT_MAX_SIZE_MB", raising=False)

    app = create_run_broker_app()

    assert app._client_max_size == 32 * 1024 * 1024


def test_webui_run_broker_client_max_size_can_be_overridden(monkeypatch):
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_CLIENT_MAX_SIZE_MB", "8")

    app = create_run_broker_app()

    assert app._client_max_size == 8 * 1024 * 1024


def test_webui_run_broker_rejects_body_over_configured_limit(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_CLIENT_MAX_SIZE_BYTES", "1024")

    async def runner():
        seen = []

        async def dispatch(request):
            seen.append(request)
            return "should not run"

        app = create_run_broker_app(
            dispatch_agent=dispatch,
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post(
                "/api/run-broker/runs",
                data=json.dumps({
                    "channel": "webui",
                    "profile_name": "owner",
                    "user_key": "ou_webui",
                    "content": "x" * 2048,
                }),
                headers={"Content-Type": "application/json"},
            )
            body = await response.json()
        finally:
            await client.close()

        assert response.status == 400
        assert (
            "Request Entity Too Large" in body["error"]
            or "Content Too Large" in body["error"]
        )
        assert seen == []

    asyncio.run(runner())


def test_webui_run_broker_default_dispatch_streams_tool_events(monkeypatch, tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import agent_real
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def fake_stream_run_agent(event, profile_home, *, messages=None):
        assert profile_home == tmp_path / "profiles" / "owner"
        assert event.raw_event["session_id"] == "session-webui"
        yield "tool_started", {"name": "lark_cli", "preview": "contact --help"}
        yield "tool_completed", {"name": "lark_cli", "duration": 0.12, "is_error": False}
        yield "content", "tool-backed answer"

    async def fake_real_run_agent(event, profile_home, *, messages=None):  # pragma: no cover
        raise AssertionError("real_run_agent fallback should not run when stream yielded content")

    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )
    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream_run_agent)
    monkeypatch.setattr(agent_real, "real_run_agent", fake_real_run_agent)

    async def runner():
        app = create_run_broker_app(
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post("/api/run-broker/runs", json={
                "channel": "webui",
                "profile_name": "owner",
                "user_key": "ou_webui",
                "content": "hello broker",
                "session_id": "session-webui",
                "requires_host_tools": True,
            })
            body = await response.text()
        finally:
            await client.close()

        assert response.status == 200
        assert '"kind": "tool_started"' in body
        assert '"name": "lark_cli"' in body
        assert '"kind": "tool_completed"' in body
        assert '"kind": "content"' in body
        assert '"text": "tool-backed answer"' in body
        assert '"kind": "done"' in body

    asyncio.run(runner())


def test_webui_run_broker_clarify_response_is_owner_scoped(monkeypatch, tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import agent_real
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.upsert(
        user_id="other-owner",
        profile_name="other_sync_profile",
        open_id="ou_other",
        provenance="sync",
    )
    seeded.close()

    response_path = tmp_path / "clarify-response.json"

    async def fake_stream_run_agent(event, profile_home, *, messages=None):
        assert profile_home == tmp_path / "profiles" / "owner_sync_profile"
        assert event.raw_event["session_id"] == "session-webui"
        yield "clarify_required", {
            "session_key": "agent:profile:owner_sync_profile:platform:webui:session:session-webui",
            "clarify_id": "clarify_1",
            "question": "Which report style?",
            "choices": ["brief", "detailed"],
            "response_path": str(response_path),
        }
        for _ in range(20):
            if response_path.exists():
                break
            await asyncio.sleep(0.01)
        answer = json.loads(response_path.read_text(encoding="utf-8"))["response"]
        yield "clarify_resolved", {
            "session_key": "agent:profile:owner_sync_profile:platform:webui:session:session-webui",
            "clarify_id": "clarify_1",
            "response": answer,
        }
        yield "content", f"answer:{answer}"

    async def fake_real_run_agent(event, profile_home, *, messages=None):  # pragma: no cover
        raise AssertionError("real_run_agent fallback should not run when stream yielded content")

    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )
    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream_run_agent)
    monkeypatch.setattr(agent_real, "real_run_agent", fake_real_run_agent)

    async def runner():
        router_mod.override_routing_table(db_path)
        app = create_run_broker_app(
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        run_response = None
        try:
            run_response = await client.post("/api/run-broker/runs", headers={
                "X-Hermes-Owner-Open-Id": "ou_owner",
            }, json={
                "channel": "webui",
                "profile_name": "spoofed_client_profile",
                "user_key": "ou_spoofed",
                "content": "make a report",
                "session_id": "session-webui",
                "requires_host_tools": True,
            })
            first_line = await _read_sse_data_line(run_response)
            clarify_event = json.loads(first_line[len(b"data: "):])

            wrong_owner_response = await client.post(
                "/api/run-broker/clarify/clarify_1/respond",
                headers={"X-Hermes-Owner-Open-Id": "ou_other"},
                json={
                    "profile_name": "owner_sync_profile",
                    "session_id": "session-webui",
                    "response": "detailed",
                },
            )
            wrong_owner_body = await wrong_owner_response.json()

            right_owner_response = await client.post(
                "/api/run-broker/clarify/clarify_1/respond",
                headers={"X-Hermes-Owner-Open-Id": "ou_owner"},
                json={
                    "profile_name": "owner_sync_profile",
                    "session_id": "session-webui",
                    "response": "brief",
                },
            )
            right_owner_body = await right_owner_response.json()
            remaining_body = await run_response.text()
        finally:
            if run_response is not None:
                run_response.close()
            await client.close()
            router_mod.override_routing_table(None)

        assert run_response.status == 200
        assert clarify_event["kind"] == "clarify_required"
        assert clarify_event["payload"]["clarify_id"] == "clarify_1"
        assert clarify_event["payload"]["question"] == "Which report style?"
        assert "response_path" not in clarify_event["payload"]
        assert wrong_owner_response.status == 404
        assert wrong_owner_body == {"error": "clarify request not found"}
        assert right_owner_response.status == 200
        assert right_owner_body == {"ok": True, "clarify_id": "clarify_1"}
        assert '"kind": "clarify_resolved"' in remaining_body
        assert '"text": "answer:brief"' in remaining_body

    asyncio.run(runner())


def test_webui_run_broker_session_history_commands_are_owner_scoped(monkeypatch, tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.sessions import SessionStore
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.upsert(
        user_id="other-owner",
        profile_name="other_sync_profile",
        open_id="ou_other",
        provenance="sync",
    )
    seeded.close()

    store = SessionStore(tmp_path / "sessions.db")
    store.append("owner_sync_profile", "ou_owner", "user", "hello")
    store.append("owner_sync_profile", "ou_owner", "assistant", "hi")
    store.append("other_sync_profile", "ou_other", "user", "private")

    async def runner():
        router_mod.override_routing_table(db_path)
        router_mod.override_session_store(store)
        try:
            app = create_run_broker_app(
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                wrong_owner_response = await client.post(
                    "/api/run-broker/session-commands",
                    headers={"X-Hermes-Owner-Open-Id": "ou_other"},
                    json={
                        "profile_name": "owner_sync_profile",
                        "session_id": "session-webui",
                        "command": "/reset",
                    },
                )
                wrong_owner_body = await wrong_owner_response.json()
                status_response = await client.post(
                    "/api/run-broker/session-commands",
                    headers={"X-Hermes-Owner-Open-Id": "ou_owner"},
                    json={
                        "session_id": "session-webui",
                        "command": "/status",
                    },
                )
                status_body = await status_response.json()
                reset_response = await client.post(
                    "/api/run-broker/session-commands",
                    headers={"X-Hermes-Owner-Open-Id": "ou_owner"},
                    json={
                        "profile_name": "owner_sync_profile",
                        "session_id": "session-webui",
                        "command": "/reset",
                    },
                )
                reset_body = await reset_response.json()
            finally:
                await client.close()
            owner_count = store.count("owner_sync_profile", "ou_owner")
            other_count = store.count("other_sync_profile", "ou_other")
        finally:
            router_mod.override_session_store(None)
            router_mod.override_routing_table(None)

        assert wrong_owner_response.status == 403
        assert "asserted owner" in wrong_owner_body["error"]
        assert status_response.status == 200
        assert status_body["profile_name"] == "owner_sync_profile"
        assert status_body["command"] == "status"
        assert status_body["type"] == "session_history"
        assert status_body["history_count"] == 2
        assert "profile: owner_sync_profile" in status_body["message"]
        assert reset_response.status == 200
        assert reset_body["action"] == "reset"
        assert reset_body["history_count"] == 0
        assert owner_count == 0
        assert other_count == 1
        assert str(tmp_path) not in json.dumps(status_body)
        assert str(tmp_path) not in json.dumps(reset_body)

    asyncio.run(runner())


def test_webui_run_broker_session_command_expands_plan_owner_scoped(monkeypatch, tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    profile_home = tmp_path / "profiles" / "owner_sync_profile"
    skill_dir = profile_home / "skills" / "software-development" / "plan"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: plan\n---\nPLAN BODY\n", encoding="utf-8")

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.close()

    async def runner():
        router_mod.override_routing_table(db_path)
        monkeypatch.setattr(router_mod, "_profile_name_to_home", lambda profile_name: tmp_path / "profiles" / profile_name)
        try:
            app = create_run_broker_app(
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/api/run-broker/session-commands",
                    headers={"X-Hermes-Owner-Open-Id": "ou_owner"},
                    json={
                        "session_id": "session-webui",
                        "command": "/plan build the feature",
                    },
                )
                body = await response.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 200
        assert body["profile_name"] == "owner_sync_profile"
        assert body["handled"] is True
        assert body["command"] == "plan"
        assert body["type"] == "skill"
        assert body["action"] == "plan"
        assert "PLAN BODY" in body["message"]
        assert "relative output paths are relative to the active profile home" in body["message"]
        assert "User request: build the feature" in body["message"]
        assert str(tmp_path) not in body["message"]

    asyncio.run(runner())


def test_webui_run_broker_goal_done_matches_real_goal_manager_contract(monkeypatch, tmp_path: Path):
    goals_mod = pytest.importorskip("hermes_cli.goals")

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manager = goals_mod.GoalManager("session-webui-contract")
    mark_done = getattr(manager, "mark_done", None)

    assert callable(mark_done)
    manager.set("fix the tests")
    mark_done("user-completed")
    assert manager.state.status == "done"


def test_webui_run_broker_goal_set_and_evaluate_are_owner_scoped(monkeypatch, tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy import webui_broker_server as broker_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    profile_home = tmp_path / "profiles" / "owner_sync_profile"
    profile_home.mkdir(parents=True)
    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.upsert(
        user_id="other-owner",
        profile_name="other_sync_profile",
        open_id="ou_other",
        provenance="sync",
    )
    seeded.close()

    class FakeState:
        goal = "fix the tests"
        status = "active"
        max_turns = 20

    class FakeGoalManager:
        state = FakeState()

        def __init__(self, session_id):
            self.session_id = session_id

        def set(self, goal):
            self.state.goal = goal
            self.state.status = "active"
            return self.state

        def evaluate_after_turn(self, final_response, user_initiated=False):
            return {
                "status": "active",
                "should_continue": True,
                "continuation_prompt": f"[Continuing]\\nGoal: {self.state.goal}",
                "verdict": "continue",
                "reason": "more work remains",
                "message": "Continuing toward goal.",
            }

        def mark_done(self, reason):
            self.state.status = "done"
            self.done_reason = reason
            return self.state

    monkeypatch.setattr(broker_mod, "_goal_manager", lambda session_id: FakeGoalManager(session_id))

    async def runner():
        router_mod.override_routing_table(db_path)
        monkeypatch.setattr(router_mod, "_profile_name_to_home", lambda profile_name: tmp_path / "profiles" / profile_name)
        try:
            app = create_run_broker_app(
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                wrong_owner_response = await client.post(
                    "/api/run-broker/session-commands",
                    headers={"X-Hermes-Owner-Open-Id": "ou_other"},
                    json={
                        "profile_name": "owner_sync_profile",
                        "session_id": "session-webui",
                        "command": "/goal fix the tests",
                    },
                )
                wrong_owner_body = await wrong_owner_response.json()
                set_response = await client.post(
                    "/api/run-broker/session-commands",
                    headers={"X-Hermes-Owner-Open-Id": "ou_owner"},
                    json={
                        "profile_name": "owner_sync_profile",
                        "session_id": "session-webui",
                        "command": "/goal fix the tests",
                    },
                )
                set_body = await set_response.json()
                evaluate_response = await client.post(
                    "/api/run-broker/goals/evaluate",
                    headers={"X-Hermes-Owner-Open-Id": "ou_owner"},
                    json={
                        "profile_name": "owner_sync_profile",
                        "session_id": "session-webui",
                        "final_response": "not done yet",
                    },
                )
                evaluate_body = await evaluate_response.json()
                done_response = await client.post(
                    "/api/run-broker/session-commands",
                    headers={"X-Hermes-Owner-Open-Id": "ou_owner"},
                    json={
                        "profile_name": "owner_sync_profile",
                        "session_id": "session-webui",
                        "command": "/goal done",
                    },
                )
                done_body = await done_response.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert wrong_owner_response.status == 403
        assert "asserted owner" in wrong_owner_body["error"]
        assert set_response.status == 200
        assert set_body["handled"] is True
        assert set_body["command"] == "goal"
        assert set_body["action"] == "set"
        assert set_body["message"] == "Goal set."
        assert set_body["kickoff_prompt"] == "fix the tests"
        assert evaluate_response.status == 200
        assert evaluate_body["handled"] is True
        assert evaluate_body["status"] == "active"
        assert evaluate_body["should_continue"] is True
        assert evaluate_body["verdict"] == "continue"
        assert evaluate_body["reason"] == "more work remains"
        assert "Goal: fix the tests" in evaluate_body["continuation_prompt"]
        assert done_response.status == 200
        assert done_body["handled"] is True
        assert done_body["command"] == "goal"
        assert done_body["action"] == "done"
        assert done_body["message"] == "Goal marked done."
        assert done_body["clear_goal_continuations"] is True

    asyncio.run(runner())


def test_webui_run_broker_streams_media_as_workspace_alias(monkeypatch, tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import agent_real
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    profile_home = tmp_path / "profiles" / "owner"
    generated = profile_home / "home" / "generated_art.png"
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b"png")

    async def fake_stream_run_agent(event, profile_home_arg, *, messages=None):
        assert profile_home_arg == profile_home
        yield "content", f"done\nMEDIA:{generated}"

    async def fake_real_run_agent(event, profile_home_arg, *, messages=None):  # pragma: no cover
        raise AssertionError("real_run_agent fallback should not run when stream yielded content")

    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )
    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream_run_agent)
    monkeypatch.setattr(agent_real, "real_run_agent", fake_real_run_agent)

    async def runner():
        app = create_run_broker_app(
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post("/api/run-broker/runs", json={
                "channel": "webui",
                "profile_name": "owner",
                "user_key": "ou_webui",
                "content": "generate image",
                "session_id": "session-webui",
                "requires_host_tools": True,
            })
            body = await response.text()
        finally:
            await client.close()

        assert response.status == 200
        assert '"kind": "content"' in body
        assert "MEDIA:/workspace/Downloads/generated_art.png" in body
        assert str(generated) not in body
        assert (profile_home / "workspace" / "Downloads" / "generated_art.png").read_bytes() == b"png"

    asyncio.run(runner())


def test_webui_run_broker_buffers_split_media_directives(monkeypatch, tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import agent_real
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    profile_home = tmp_path / "profiles" / "owner"
    generated = profile_home / "home" / "plasma_art.png"
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b"png")

    async def fake_stream_run_agent(event, profile_home_arg, *, messages=None):
        assert profile_home_arg == profile_home
        yield "content", "\n\n图片生成成功：\n\nMEDIA:"
        yield "content", str(generated)
        yield "content", "\n\n完成"

    async def fake_real_run_agent(event, profile_home_arg, *, messages=None):  # pragma: no cover
        raise AssertionError("real_run_agent fallback should not run when stream yielded content")

    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )
    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream_run_agent)
    monkeypatch.setattr(agent_real, "real_run_agent", fake_real_run_agent)

    async def runner():
        app = create_run_broker_app(
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post("/api/run-broker/runs", json={
                "channel": "webui",
                "profile_name": "owner",
                "user_key": "ou_webui",
                "content": "generate image",
                "session_id": "session-webui",
                "requires_host_tools": True,
            })
            body = await response.text()
        finally:
            await client.close()

        assert response.status == 200
        assert "MEDIA:/workspace/Downloads/plasma_art.png" in body
        assert str(generated) not in body
        assert (profile_home / "workspace" / "Downloads" / "plasma_art.png").read_bytes() == b"png"

    asyncio.run(runner())


def test_webui_run_broker_flushes_events_before_agent_finishes(monkeypatch, tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import agent_real
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    release = asyncio.Event()

    async def fake_stream_run_agent(event, profile_home, *, messages=None):
        assert profile_home == tmp_path / "profiles" / "owner"
        yield "thinking", "正在连接模型和工具运行环境..."
        await release.wait()
        yield "content", "done"

    async def fake_real_run_agent(event, profile_home, *, messages=None):  # pragma: no cover
        raise AssertionError("real_run_agent fallback should not run when stream yielded content")

    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )
    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream_run_agent)
    monkeypatch.setattr(agent_real, "real_run_agent", fake_real_run_agent)

    async def runner():
        app = create_run_broker_app(
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        response = None
        try:
            post_task = asyncio.create_task(client.post("/api/run-broker/runs", json={
                "channel": "webui",
                "profile_name": "owner",
                "user_key": "ou_webui",
                "content": "hello broker",
                "session_id": "session-webui",
                "requires_host_tools": True,
            }))
            response = await asyncio.wait_for(post_task, timeout=0.2)
            first_line = await asyncio.wait_for(response.content.readline(), timeout=0.2)
            assert b'"kind": "thinking"' in first_line
            assert "正在连接模型".encode("utf-8") in first_line
            release.set()
            body = await response.text()
        finally:
            release.set()
            if response is not None:
                response.close()
            await client.close()

        assert response.status == 200
        assert '"kind": "content"' in body
        assert '"text": "done"' in body

    asyncio.run(runner())


def test_webui_run_broker_default_dispatch_passes_messages_to_aiagent(monkeypatch, tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import agent_real
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    expected_messages = [
        {"role": "user", "content": "查看下明天的天气"},
        {"role": "assistant", "content": "你在哪个城市？"},
        {"role": "user", "content": "北京"},
    ]

    async def fake_stream_run_agent(event, profile_home, *, messages=None):
        assert messages == expected_messages
        yield "content", "北京明天的天气..."

    async def fake_real_run_agent(event, profile_home, *, messages=None):  # pragma: no cover
        raise AssertionError("real_run_agent fallback should not run when stream yielded content")

    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )
    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream_run_agent)
    monkeypatch.setattr(agent_real, "real_run_agent", fake_real_run_agent)

    async def runner():
        app = create_run_broker_app(
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post("/api/run-broker/runs", json={
                "channel": "webui",
                "profile_name": "owner",
                "user_key": "ou_webui",
                "content": "北京",
                "session_id": "session-webui",
                "requires_host_tools": True,
                "messages": expected_messages,
            })
            body = await response.text()
        finally:
            await client.close()

        assert response.status == 200
        assert '"kind": "content"' in body
        assert '"text": "北京明天的天气..."' in body
        assert '"kind": "done"' in body

    asyncio.run(runner())


def test_webui_run_broker_default_dispatch_streams_thinking_separately(monkeypatch, tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import agent_real
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def fake_stream_run_agent(event, profile_home, *, messages=None):
        yield "thinking", "The user wants me to call lark_cli first."
        yield "tool_started", {"name": "lark_cli", "preview": "calendar --help"}
        yield "tool_completed", {"name": "lark_cli", "duration": 0.12, "is_error": False}
        yield "content", "日历已创建成功。"

    async def fake_real_run_agent(event, profile_home, *, messages=None):  # pragma: no cover
        raise AssertionError("real_run_agent fallback should not run when stream yielded content")

    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )
    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream_run_agent)
    monkeypatch.setattr(agent_real, "real_run_agent", fake_real_run_agent)

    async def runner():
        app = create_run_broker_app(
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post("/api/run-broker/runs", json={
                "channel": "webui",
                "profile_name": "owner",
                "user_key": "ou_webui",
                "content": "帮我创建一个日历",
                "session_id": "session-webui",
                "requires_host_tools": True,
            })
            body = await response.text()
        finally:
            await client.close()

        assert response.status == 200
        assert '"kind": "thinking"' in body
        assert "The user wants me to call lark_cli first." in body
        assert body.count('"kind": "content"') == 1
        assert '"kind": "done"' in body
        assert '"kind": "done", "text": ""' in body

    asyncio.run(runner())


def test_webui_run_broker_default_dispatch_continues_after_sse_disconnect(
    monkeypatch,
    tmp_path: Path,
):
    from aiohttp.client_exceptions import ClientConnectionResetError

    from hermes_multitenancy import agent_real
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.run_models import RunEvent, RunRequest
    from hermes_multitenancy.webui_broker_server import _default_dispatch_agent

    consumed: list[str] = []

    async def fake_stream_run_agent(event, profile_home, *, messages=None):
        yield "tool_started", {"name": "delegate_task"}
        consumed.append("tool_started")
        yield "tool_completed", {"name": "delegate_task", "duration": 337.79, "is_error": False}
        consumed.append("tool_completed")
        yield "content", "final answer after delegate_task"
        consumed.append("content")

    async def fake_real_run_agent(event, profile_home, *, messages=None):  # pragma: no cover
        raise AssertionError("real_run_agent fallback should not run when stream yielded content")

    emitted: list[str] = []

    async def disconnected_emit(event: RunEvent):
        emitted.append(event.kind)
        if event.kind == "tool_completed":
            raise ClientConnectionResetError("Cannot write to closing transport")

    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )
    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream_run_agent)
    monkeypatch.setattr(agent_real, "real_run_agent", fake_real_run_agent)

    async def runner():
        result = await _default_dispatch_agent(
            RunRequest(
                channel="webui",
                profile_name="owner",
                user_key="ou_webui",
                content="long task",
                session_id="session-webui",
                requires_host_tools=True,
            ),
            emit_event=disconnected_emit,
        )

        assert result == ""
        assert consumed == ["tool_started", "tool_completed", "content"]
        assert emitted == ["tool_started", "tool_completed"]

    asyncio.run(runner())


def test_webui_disconnect_tolerant_emitter_propagates_cancellation():
    from hermes_multitenancy.run_models import RunEvent
    from hermes_multitenancy.webui_broker_server import _DisconnectTolerantEmitter

    async def cancelled_emit(event: RunEvent):
        raise asyncio.CancelledError()

    async def runner():
        emitter = _DisconnectTolerantEmitter(cancelled_emit)
        with pytest.raises(asyncio.CancelledError):
            await emitter.emit(RunEvent(kind="content", text="partial"))

    asyncio.run(runner())


def test_webui_disconnect_tolerant_emitter_reraises_non_transport_errors():
    from hermes_multitenancy.run_models import RunEvent
    from hermes_multitenancy.webui_broker_server import _DisconnectTolerantEmitter

    async def failed_emit(event: RunEvent):
        raise RuntimeError("unrelated write failure")

    async def runner():
        emitter = _DisconnectTolerantEmitter(failed_emit)
        with pytest.raises(RuntimeError, match="unrelated write failure"):
            await emitter.emit(RunEvent(kind="content", text="partial"))

    asyncio.run(runner())


def test_webui_run_broker_http_client_disconnect_keeps_agent_running(
    monkeypatch,
    tmp_path: Path,
):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import agent_real
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    client_disconnected = asyncio.Event()
    stream_finished = asyncio.Event()
    consumed: list[str] = []

    async def fake_stream_run_agent(event, profile_home, *, messages=None):
        assert profile_home == tmp_path / "profiles" / "carol"
        consumed.append("tool_started")
        yield "tool_started", {"name": "delegate_task"}
        consumed.append("tool_completed")
        yield "tool_completed", {"name": "delegate_task", "duration": 337.79, "is_error": False}
        await client_disconnected.wait()
        consumed.append("content")
        yield "content", "final answer after browser refresh"
        consumed.append("after_content")
        stream_finished.set()

    async def fake_real_run_agent(event, profile_home, *, messages=None):  # pragma: no cover
        raise AssertionError("real_run_agent fallback should not run when stream yielded content")

    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )
    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream_run_agent)
    monkeypatch.setattr(agent_real, "real_run_agent", fake_real_run_agent)

    async def runner():
        app = create_run_broker_app(
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        response = None
        try:
            response = await client.post("/api/run-broker/runs", json={
                "channel": "webui",
                "profile_name": "carol",
                "user_key": "ou_carol",
                "content": "long delegate task",
                "session_id": "mpgl63gwl57asf",
                "requires_host_tools": True,
            })
            assert response.status == 200

            first_line = await _read_sse_data_line(response)
            second_line = await _read_sse_data_line(response)
            assert b'"kind": "tool_started"' in first_line
            assert b'"kind": "tool_completed"' in second_line

            response.close()
            client_disconnected.set()

            await asyncio.wait_for(stream_finished.wait(), timeout=1.0)
        finally:
            client_disconnected.set()
            if response is not None:
                response.close()
            await client.close()

        assert consumed == ["tool_started", "tool_completed", "content", "after_content"]

    asyncio.run(runner())


def test_webui_run_broker_endpoint_rejects_missing_bearer_key(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def runner():
        monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "broker-secret")
        app = create_run_broker_app(
            dispatch_agent=lambda request: f"echo:{request.content}",
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            missing = await client.post("/api/run-broker/runs", json={
                "channel": "webui",
                "profile_name": "owner",
                "user_key": "ou_webui",
                "content": "hello broker",
            })
            authorized = await client.post("/api/run-broker/runs", headers={
                "Authorization": "Bearer broker-secret",
            }, json={
                "channel": "webui",
                "profile_name": "owner",
                "user_key": "ou_webui",
                "content": "hello broker",
            })
        finally:
            await client.close()

        assert missing.status == 401
        assert authorized.status == 200

    asyncio.run(runner())


def test_webui_run_broker_event_preserves_webui_session_boundary():
    from hermes_multitenancy.agent_real import _resolve_aiagent_session_id
    from hermes_multitenancy.webui_broker_server import _build_webui_event
    from hermes_multitenancy.run_models import RunRequest

    request = RunRequest(
        channel="webui",
        profile_name="feishu_group_dfe8bc83167b_e18e",
        user_key="ou_webui",
        content="hello",
        session_id="matrix_t1_group_webui_doc_123",
    )

    event = _build_webui_event(request)
    session_id = _resolve_aiagent_session_id(
        event,
        profile_home=types.SimpleNamespace(name=request.profile_name),
        sender_open_id=request.user_key,
    )

    assert event.source.platform.value == "webui"
    assert event.raw_event["session_id"] == "matrix_t1_group_webui_doc_123"
    assert "platform:webui" in session_id
    assert "session:matrix_t1_group_webui_doc_123" in session_id
    assert "platform:feishu" not in session_id


def test_webui_run_broker_event_exposes_owner_open_id_for_lark_cli_identity():
    from hermes_multitenancy.agent_real import _resolve_subprocess_sender_open_id
    from hermes_multitenancy.run_models import RunRequest
    from hermes_multitenancy.webui_broker_server import _build_webui_event

    request = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_owner",
        content="创建一个飞书云文档",
        session_id="webui-lark-cli-identity",
    )

    event = _build_webui_event(request)

    assert event.sender_open_id == "ou_owner"
    assert _resolve_subprocess_sender_open_id(event) == "ou_owner"


def test_webui_run_broker_event_injects_current_datetime_instruction():
    from hermes_multitenancy.run_models import RunRequest
    from hermes_multitenancy.webui_broker_server import _build_webui_event

    request = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_owner",
        content="今天中午一点约会议室",
        session_id="webui-current-date",
        metadata={"instructions": "existing instructions"},
    )

    event = _build_webui_event(request)
    instructions = event.raw_event["metadata"]["instructions"]

    assert "existing instructions" in instructions
    assert "Current date/time:" in instructions
    assert "Timezone:" in instructions
    assert "Interpret relative dates" in instructions
    assert "room attendee declined" in instructions


def test_run_broker_loads_only_shared_runtime_env(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.webui_broker_server import load_run_broker_shared_env

    shared = tmp_path / ".hermes"
    shared.mkdir()
    (shared / ".env").write_text(
        "\n".join(
            [
                "HERMES_MULTITENANCY_CREDENTIAL_KEY=vault-key",
                "HERMES_LARK_CLI_APP_ID=cli_public",
                "FEISHU_APP_SECRET=must-not-load",
                "ANTHROPIC_API_KEY=model-key",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(shared))
    monkeypatch.delenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("HERMES_LARK_CLI_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    loaded = load_run_broker_shared_env()

    assert loaded == {
        "HERMES_MULTITENANCY_CREDENTIAL_KEY": "vault-key",
        "HERMES_LARK_CLI_APP_ID": "cli_public",
    }
    assert os.environ["HERMES_MULTITENANCY_CREDENTIAL_KEY"] == "vault-key"
    assert os.environ["HERMES_LARK_CLI_APP_ID"] == "cli_public"
    assert "FEISHU_APP_SECRET" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_run_broker_shared_env_does_not_override_existing_secret(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.webui_broker_server import load_run_broker_shared_env

    shared = tmp_path / ".hermes"
    shared.mkdir()
    (shared / ".env").write_text(
        "HERMES_MULTITENANCY_CREDENTIAL_KEY=file-key\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(shared))
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "process-key")

    loaded = load_run_broker_shared_env()

    assert loaded == {}
    assert os.environ["HERMES_MULTITENANCY_CREDENTIAL_KEY"] == "process-key"


def _install_fake_cron(monkeypatch):
    store = {}
    cron_pkg = types.ModuleType("cron")
    jobs_mod = types.ModuleType("cron.jobs")
    scheduler_mod = types.ModuleType("cron.scheduler")

    jobs_mod.HERMES_DIR = None
    jobs_mod.CRON_DIR = None
    jobs_mod.JOBS_FILE = None
    jobs_mod.OUTPUT_DIR = None
    scheduler_mod._hermes_home = None
    scheduler_mod._LOCK_DIR = None
    scheduler_mod._LOCK_FILE = None

    def bucket():
        key = str(jobs_mod.JOBS_FILE)
        store.setdefault(key, [])
        return store[key]

    def create_job(
        *,
        prompt,
        schedule,
        name=None,
        repeat=None,
        deliver=None,
        skills=None,
        model=None,
        provider=None,
        base_url=None,
        workdir=None,
        profile=None,
    ):
        del model, provider, base_url, workdir, profile
        job = {
            "id": "abc123abc123",
            "job_id": "abc123abc123",
            "name": name,
            "prompt": prompt,
            "schedule": schedule,
            "schedule_display": schedule,
            "repeat": {"times": repeat, "completed": 0},
            "deliver": deliver or "local",
            "enabled": True,
            "state": "scheduled",
            "skills": skills or [],
        }
        bucket().append(job)
        return job

    def list_jobs(include_disabled=False):
        jobs = list(bucket())
        return jobs if include_disabled else [j for j in jobs if j.get("enabled", True)]

    def get_job(job_id):
        return next((j for j in bucket() if j["id"] == job_id), None)

    def update_job(job_id, updates):
        job = get_job(job_id)
        if not job:
            return None
        job.update(updates)
        return job

    def remove_job(job_id):
        jobs = bucket()
        before = len(jobs)
        jobs[:] = [j for j in jobs if j["id"] != job_id]
        return len(jobs) != before

    def pause_job(job_id):
        return update_job(job_id, {"enabled": False, "state": "paused"})

    def resume_job(job_id):
        return update_job(job_id, {"enabled": True, "state": "scheduled"})

    def trigger_job(job_id):
        return update_job(
            job_id,
            {
                "enabled": True,
                "state": "scheduled",
                "paused_at": None,
                "paused_reason": None,
                "next_run_at": "now",
            },
        )

    jobs_mod.create_job = create_job
    jobs_mod.list_jobs = list_jobs
    jobs_mod.get_job = get_job
    jobs_mod.update_job = update_job
    jobs_mod.remove_job = remove_job
    jobs_mod.pause_job = pause_job
    jobs_mod.resume_job = resume_job
    jobs_mod.trigger_job = trigger_job
    cron_pkg.jobs = jobs_mod
    cron_pkg.scheduler = scheduler_mod

    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", jobs_mod)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler_mod)
    return store


def test_webui_run_broker_jobs_manage_profile_local_cron(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def runner():
        store = _install_fake_cron(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "broker-secret")

        app = create_run_broker_app(
            dispatch_agent=lambda request: f"echo:{request.content}",
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {
            "Authorization": "Bearer broker-secret",
            "X-Hermes-Profile": "owner",
            "X-Hermes-User-Key": "ou_owner",
        }
        try:
            created = await client.post("/api/run-broker/jobs", headers=headers, json={
                "name": "cron canary",
                "schedule": "*/5 * * * *",
                "prompt": "ping",
                "deliver": "feishu",
                "owner_open_id": "spoofed",
                "owner_profile": "other",
            })
            create_body = await created.json()
            listed = await client.get("/api/run-broker/jobs?include_disabled=true", headers=headers)
            list_body = await listed.json()
            paused = await client.post("/api/run-broker/jobs/abc123abc123/pause", headers=headers)
            pause_body = await paused.json()
            triggered = await client.post("/api/run-broker/jobs/abc123abc123/run", headers=headers)
            trigger_body = await triggered.json()
            deleted = await client.delete("/api/run-broker/jobs/abc123abc123", headers=headers)
            delete_body = await deleted.json()
        finally:
            await client.close()

        expected_jobs_file = tmp_path / ".hermes" / "profiles" / "owner" / "cron" / "jobs.json"
        assert created.status == 200
        assert create_body["job"]["owner_open_id"] == "ou_owner"
        assert create_body["job"]["owner_profile"] == "owner"
        assert str(expected_jobs_file) in store
        assert listed.status == 200
        assert list_body["jobs"][0]["id"] == "abc123abc123"
        assert paused.status == 200
        assert pause_body["job"]["state"] == "paused"
        assert triggered.status == 200
        assert trigger_body["queued"] is True
        assert trigger_body["job"]["state"] == "scheduled"
        assert trigger_body["job"]["next_run_at"] == "now"
        assert deleted.status == 200
        assert delete_body == {"ok": True}
        assert store[str(expected_jobs_file)] == []

    asyncio.run(runner())


def test_webui_run_broker_job_run_requires_authorization(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def runner():
        store = _install_fake_cron(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "broker-secret")

        app = create_run_broker_app(
            dispatch_agent=lambda request: f"echo:{request.content}",
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            unauthorized = await client.post("/api/run-broker/jobs/abc123abc123/run")
            body = await unauthorized.json()
        finally:
            await client.close()

        assert unauthorized.status == 401
        assert body == {"error": "unauthorized"}
        assert store == {}

    asyncio.run(runner())


def test_webui_run_broker_jobs_default_to_feishu_delivery(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def runner():
        _install_fake_cron(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "broker-secret")

        app = create_run_broker_app(
            dispatch_agent=lambda request: f"echo:{request.content}",
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {
            "Authorization": "Bearer broker-secret",
            "X-Hermes-Profile": "yaojunhua",
            "X-Hermes-User-Key": "ou_yaojunhua",
        }
        try:
            created = await client.post("/api/run-broker/jobs", headers=headers, json={
                "name": "cron canary",
                "schedule": "*/5 * * * *",
                "prompt": "ping",
            })
            create_body = await created.json()
        finally:
            await client.close()

        assert created.status == 200
        assert create_body["job"]["deliver"] == "feishu"
        assert create_body["job"]["owner_open_id"] == "ou_yaojunhua"
        assert create_body["job"]["owner_profile"] == "yaojunhua"

    asyncio.run(runner())


def test_webui_run_broker_jobs_expose_shadow_plan(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def runner():
        _install_fake_cron(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "broker-secret")

        app = create_run_broker_app(
            dispatch_agent=lambda request: f"echo:{request.content}",
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {
            "Authorization": "Bearer broker-secret",
            "X-Hermes-Profile": "owner",
            "X-Hermes-User-Key": "ou_owner",
        }
        try:
            created = await client.post("/api/run-broker/jobs", headers=headers, json={
                "name": "cron canary",
                "schedule": "*/5 * * * *",
                "prompt": "ping",
            })
            plan_response = await client.get("/api/run-broker/jobs/abc123abc123/plan?shadow=1&due=1", headers=headers)
            plan_body = await plan_response.json()
        finally:
            await client.close()

        assert created.status == 200
        assert plan_response.status == 200
        assert plan_body["plan"]["mode"] == "shadow"
        assert plan_body["plan"]["will_execute"] is False
        assert plan_body["plan"]["would_execute"] is True
        assert plan_body["plan"]["profile_name"] == "owner"
        assert plan_body["plan"]["user_key"] == "ou_owner"
        assert plan_body["plan"]["deliver_target"] == {
            "platform": "feishu",
            "chat_id": "ou_owner",
            "thread_id": None,
        }
        assert plan_body["plan"]["secret_free"] is True

    asyncio.run(runner())


def test_webui_run_broker_jobs_reject_invalid_profile(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def runner():
        _install_fake_cron(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path))
        app = create_run_broker_app(
            dispatch_agent=lambda request: f"echo:{request.content}",
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get("/api/run-broker/jobs", headers={
                "X-Hermes-Profile": "../shared",
                "X-Hermes-User-Key": "ou_owner",
            })
            body = await response.json()
        finally:
            await client.close()

        assert response.status == 400
        assert body == {"error": "invalid profile_name"}

    asyncio.run(runner())


def test_webui_run_broker_owner_header_agent_id_resolves_owned_profile(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    # An owned non-root agent has its own open_id (router auto-provision writes
    # user_id == open_id) and an owner_open_id pointing back at the owner — the
    # production-equivalent state the routing backfill / self-agent provisioning
    # produces. Sharing the owner's open_id here would make the backfill rewrite
    # both rows to provenance='sync', tripping the one-sync-root-per-open_id
    # unique index — an invalid topology, not a real ownership case.
    seeded.upsert(
        user_id="agent-owned",
        profile_name="owned_agent_profile",
        open_id="agent-owned",
        provenance="auto",
    )
    seeded._conn.execute(
        "UPDATE multitenancy_routing SET owner_open_id = 'ou_owner' "
        "WHERE user_id = 'agent-owned'"
    )
    seeded._conn.commit()
    seeded.close()

    async def runner():
        seen = []
        router_mod.override_routing_table(db_path)
        try:
            table = router_mod._get_routing_table()
            assert table is not None
            owned = table.lookup_agent("agent-owned")
            assert owned is not None
            assert owned.owner_open_id == "ou_owner"

            async def dispatch(request):
                seen.append(request)
                return f"echo:{request.content}"

            app = create_run_broker_app(
                dispatch_agent=dispatch,
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post("/api/run-broker/runs", headers={
                    "X-Hermes-Owner-Open-Id": " ou_owner ",
                    "X-Hermes-Agent-Id": " agent-owned ",
                }, json={
                    "channel": "webui",
                    "profile_name": "spoofed_client_profile",
                    "user_key": "ou_webui",
                    "content": "hello owner gate",
                })
                body = await response.text()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 200
        assert '"kind": "content"' in body
        assert len(seen) == 1
        assert seen[0].profile_name == "owned_agent_profile"
        assert seen[0].user_key == "ou_owner"
        assert seen[0].credential_subject == "ou_owner"

    asyncio.run(runner())


def test_webui_slash_registry_lists_only_owner_scoped_profile_skills(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared = tmp_path / ".hermes"
    owner_skill = shared / "profiles" / "owned_agent_profile" / "skills" / "Keep" / "kep-prd-analysis"
    victim_skill = shared / "profiles" / "victim_profile" / "skills" / "Private" / "secret-runbook"
    owner_skill.mkdir(parents=True)
    victim_skill.mkdir(parents=True)
    (owner_skill / "SKILL.md").write_text(
        "\n".join([
            "---",
            "name: kep-prd-analysis",
            "description: PRD analysis helper",
            "---",
            "# KEP PRD Analysis",
            "",
            "Internal runbook body that must never be returned.",
        ]),
        encoding="utf-8",
    )
    (victim_skill / "SKILL.md").write_text("# Secret Runbook\nvictim-only command\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))

    db_path = shared / "multitenancy.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.upsert(
        user_id="agent-owned",
        profile_name="owned_agent_profile",
        open_id="agent-owned",
        provenance="auto",
    )
    seeded.upsert(
        user_id="victim-root",
        profile_name="victim_profile",
        open_id="ou_victim",
        provenance="sync",
    )
    seeded._conn.execute(
        "UPDATE multitenancy_routing SET owner_open_id = 'ou_owner' "
        "WHERE user_id = 'agent-owned'"
    )
    seeded._conn.commit()
    seeded.close()

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app(
                dispatch_agent=lambda request: f"echo:{request.content}",
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.get(
                    "/api/run-broker/slash/commands?profile_name=victim_profile",
                    headers={
                        "X-Hermes-Owner-Open-Id": "ou_owner",
                        "X-Hermes-Agent-Id": "agent-owned",
                    },
                )
                body = await response.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 200
        assert body["ok"] is True
        assert body["profile_name"] == "owned_agent_profile"
        assert [command["name"] for command in body["commands"][:6]] == [
            "new",
            "reset",
            "status",
            "plan",
            "goal",
            "subgoal",
        ]
        assert all(command["type"] == "session_history" for command in body["commands"][:3])
        assert [command["type"] for command in body["commands"][3:6]] == ["skill", "goal", "goal"]
        assert body["commands"][-1] == {
            "name": "kep-prd-analysis",
            "slash": "/kep-prd-analysis",
            "title": "kep-prd-analysis",
            "description": "PRD analysis helper",
            "source": "skill",
            "type": "skill",
            "category": "Keep",
        }
        encoded = json.dumps(body, ensure_ascii=False)
        assert "secret-runbook" not in encoded
        assert "victim-only" not in encoded
        assert "KEP PRD Analysis" not in encoded
        assert "Internal runbook body" not in encoded
        assert str(shared) not in encoded

    asyncio.run(runner())


def test_webui_slash_registry_returns_empty_list_for_profile_without_skills(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared = tmp_path / ".hermes"
    (shared / "profiles" / "owner_sync_profile").mkdir(parents=True)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))

    db_path = shared / "multitenancy.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.close()

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app(
                dispatch_agent=lambda request: f"echo:{request.content}",
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.get(
                    "/api/run-broker/slash/commands",
                    headers={"X-Hermes-Owner-Open-Id": "ou_owner"},
                )
                body = await response.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 200
        assert body["ok"] is True
        assert body["profile_name"] == "owner_sync_profile"
        assert [command["name"] for command in body["commands"]] == [
            "new",
            "reset",
            "status",
            "plan",
            "goal",
            "subgoal",
        ]
        assert [command["type"] for command in body["commands"]] == [
            "session_history",
            "session_history",
            "session_history",
            "skill",
            "goal",
            "goal",
        ]

    asyncio.run(runner())


def test_webui_slash_registry_requires_owner_header(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared = tmp_path / ".hermes"
    (shared / "profiles" / "owner_sync_profile").mkdir(parents=True)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))

    db_path = shared / "multitenancy.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.close()

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app(
                dispatch_agent=lambda request: f"echo:{request.content}",
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.get(
                    "/api/run-broker/slash/commands?profile_name=owner_sync_profile",
                )
                body = await response.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 403
        assert body == {"error": "owner identity required (X-Hermes-Owner-Open-Id)"}

    asyncio.run(runner())


def test_webui_profile_provisioning_creates_owner_scoped_agent_route(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="feishu_g41a5b5g",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.close()

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app(
                dispatch_agent=lambda _request: "",
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post("/api/run-broker/profiles", headers={
                    "X-Hermes-Owner-Open-Id": " ou_owner ",
                }, json={
                    "profile_name": "web_coder",
                    "upstream_profile": "feishu_g41a5b5g",
                    "display_label": "Coder",
                    "description": "Software engineering agent",
                })
                body = await response.json()
            finally:
                await client.close()

            table = router_mod._get_routing_table()
            assert table is not None
            row = table.lookup_agent(body["agent_id"])
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 200
        assert body["profile_name"] == "web_coder"
        assert body["routed_profile_name"].startswith("webui_")
        assert body["owner_open_id"] == "ou_owner"
        assert body["agent_id"] == "webui:ou_owner:web_coder"
        assert row is not None
        assert row.profile_name == body["routed_profile_name"]
        assert row.kind == "agent"
        assert row.provenance == "webui-agent"
        assert row.owner_open_id == "ou_owner"
        assert row.upstream_profile == "feishu_g41a5b5g"
        assert row.display_label == "Coder"

    asyncio.run(runner())


def test_webui_profile_provisioning_initializes_group_like_agent_profile(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared_home = tmp_path / "home"
    (shared_home / "profiles" / "feishu_g41a5b5g").mkdir(parents=True)
    (shared_home / "skills" / "lark-docs").mkdir(parents=True)
    (shared_home / "skills" / "lark-docs" / "SKILL.md").write_text("# Lark Docs\n", encoding="utf-8")
    (shared_home / "profile-skill-defaults.yaml").write_text(
        "skills:\n  - lark-docs\n",
        encoding="utf-8",
    )
    (shared_home / "config.yaml").write_text(
        "model:\n  default: openai/test-model\n"
        "platforms:\n"
        "  feishu:\n"
        "    enabled: true\n"
        "    extra:\n"
        "      app_id: cli_should_not_copy\n"
        "      app_secret: secret_should_not_copy\n"
        "      connection_mode: websocket\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="feishu_g41a5b5g",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.close()

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app(
                dispatch_agent=lambda _request: "",
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post("/api/run-broker/profiles", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner",
                }, json={
                    "profile_name": "web_coder",
                    "upstream_profile": "feishu_g41a5b5g",
                    "display_label": "Coder",
                })
                body = await response.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 200
        assert body["agent_id"] == "webui:ou_owner:web_coder"
        return body

    body = asyncio.run(runner())

    profile_home = shared_home / "profiles" / body["routed_profile_name"]
    marker = json.loads((profile_home / "group_profile.json").read_text(encoding="utf-8"))
    assert marker == {
        "kind": "group",
        "chat_id": "webui:ou_owner:web_coder",
        "owner_open_id": "ou_owner",
        "display_label": "Coder",
        "feishu_auth_disabled": True,
        "source": "webui-agent",
    }
    assert (profile_home / "feishu_uat").is_dir()
    assert list((profile_home / "feishu_uat").glob("*.json")) == []
    assert (profile_home / "skills" / "lark-docs").is_symlink()

    config_text = (profile_home / "config.yaml").read_text(encoding="utf-8")
    assert "lark-cli" in config_text
    assert "platform_toolsets_mode" in config_text
    assert "enabled: false" in config_text
    assert "cli_should_not_copy" not in config_text
    assert "secret_should_not_copy" not in config_text
    assert "connection_mode" not in config_text

    env_text = (profile_home / ".env").read_text(encoding="utf-8")
    assert "FEISHU_HOME_CHANNEL" not in env_text


def test_webui_profile_provisioning_isolates_same_name_across_owners(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared_home = tmp_path / "home"
    (shared_home / "profiles" / "owner_a").mkdir(parents=True)
    (shared_home / "profiles" / "owner_b").mkdir(parents=True)
    (shared_home / "config.yaml").write_text("model:\n  default: openai/test-model\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(user_id="root-a", profile_name="owner_a", open_id="ou_owner_a", provenance="sync")
    seeded.upsert(user_id="root-b", profile_name="owner_b", open_id="ou_owner_b", provenance="sync")
    seeded.close()

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app(
                dispatch_agent=lambda _request: "",
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response_a = await client.post("/api/run-broker/profiles", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner_a",
                }, json={
                    "profile_name": "web_coder",
                    "display_label": "Coder",
                })
                body_a = await response_a.json()
                response_b = await client.post("/api/run-broker/profiles", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner_b",
                }, json={
                    "profile_name": "web_coder",
                    "display_label": "Coder",
                })
                body_b = await response_b.json()
            finally:
                await client.close()

            table = router_mod._get_routing_table()
            assert table is not None
            row_a = table.lookup_agent(body_a["agent_id"])
            row_b = table.lookup_agent(body_b["agent_id"])
        finally:
            router_mod.override_routing_table(None)

        assert response_a.status == 200
        assert response_b.status == 200
        assert row_a is not None
        assert row_b is not None
        return body_a, body_b, row_a.profile_name, row_b.profile_name

    body_a, body_b, profile_a, profile_b = asyncio.run(runner())

    assert body_a["agent_id"] == "webui:ou_owner_a:web_coder"
    assert body_b["agent_id"] == "webui:ou_owner_b:web_coder"
    assert profile_a != profile_b
    assert profile_a != "web_coder"
    assert profile_b != "web_coder"
    assert (shared_home / "profiles" / profile_a / "group_profile.json").is_file()
    assert (shared_home / "profiles" / profile_b / "group_profile.json").is_file()
    assert not (shared_home / "profiles" / "web_coder").exists()


def test_webui_profile_provisioning_isolates_truncated_names_for_same_owner(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared_home = tmp_path / "home"
    (shared_home / "profiles" / "owner_a").mkdir(parents=True)
    (shared_home / "config.yaml").write_text("model:\n  default: openai/test-model\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(user_id="root-a", profile_name="owner_a", open_id="ou_owner_a", provenance="sync")
    seeded.close()

    base = "a" * 109
    logical_a = base + ("x" * 19)
    logical_b = base + ("y" * 19)

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app(
                dispatch_agent=lambda _request: "",
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response_a = await client.post("/api/run-broker/profiles", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner_a",
                }, json={"profile_name": logical_a})
                body_a = await response_a.json()
                response_b = await client.post("/api/run-broker/profiles", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner_a",
                }, json={"profile_name": logical_b})
                body_b = await response_b.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response_a.status == 200
        assert response_b.status == 200
        return body_a, body_b

    body_a, body_b = asyncio.run(runner())

    assert body_a["agent_id"] != body_b["agent_id"]
    assert body_a["routed_profile_name"] != body_b["routed_profile_name"]
    assert len(body_a["routed_profile_name"]) <= 128
    assert len(body_b["routed_profile_name"]) <= 128
    assert (shared_home / "profiles" / body_a["routed_profile_name"] / "group_profile.json").is_file()
    assert (shared_home / "profiles" / body_b["routed_profile_name"] / "group_profile.json").is_file()


def test_webui_profile_provisioning_requires_owner_header(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    db_path = tmp_path / "routing.db"
    RoutingTable(db_path).close()

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app(
                dispatch_agent=lambda _request: "",
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post("/api/run-broker/profiles", json={
                    "profile_name": "web_coder",
                    "upstream_profile": "feishu_g41a5b5g",
                })
                body = await response.text()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 403
        assert "owner identity required" in body

    asyncio.run(runner())


def test_webui_profile_provisioning_rejects_cross_owner_upstream_profile(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared_home = tmp_path / "home"
    (shared_home / "profiles" / "owner_a").mkdir(parents=True)
    (shared_home / "profiles" / "owner_b").mkdir(parents=True)
    (shared_home / "config.yaml").write_text("model:\n  default: openai/test-model\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(user_id="root-a", profile_name="owner_a", open_id="ou_owner_a", provenance="sync")
    seeded.upsert(user_id="root-b", profile_name="owner_b", open_id="ou_owner_b", provenance="sync")
    seeded.close()

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app(
                dispatch_agent=lambda _request: "",
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post("/api/run-broker/profiles", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner_a",
                }, json={
                    "profile_name": "web_coder",
                    "upstream_profile": "owner_b",
                })
                body = await response.text()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 403
        assert "upstream_profile" in body
        assert not any((shared_home / "profiles").glob("webui_*_web_coder_*"))

    asyncio.run(runner())


def test_webui_profile_provisioning_allows_owned_upstream_agent_profile(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared_home = tmp_path / "home"
    (shared_home / "profiles" / "owner_a").mkdir(parents=True)
    (shared_home / "profiles" / "owned_template").mkdir(parents=True)
    (shared_home / "config.yaml").write_text("model:\n  default: openai/test-model\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(user_id="root-a", profile_name="owner_a", open_id="ou_owner_a", provenance="sync")
    seeded.upsert_owned_agent(
        agent_id="webui:ou_owner_a:template",
        profile_name="owned_template",
        owner_open_id="ou_owner_a",
        display_label="Template",
        upstream_profile="owner_a",
    )
    seeded.close()

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app(
                dispatch_agent=lambda _request: "",
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post("/api/run-broker/profiles", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner_a",
                }, json={
                    "profile_name": "web_coder",
                    "upstream_profile": "owned_template",
                })
                body = await response.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 200
        assert body["upstream_profile"] == "owned_template"

    asyncio.run(runner())


def test_webui_profile_provisioning_rejects_cross_owner_agent_id(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.upsert(
        user_id="root-other",
        profile_name="other_profile",
        open_id="ou_other",
        provenance="sync",
    )
    seeded._conn.execute(
        """
        INSERT INTO multitenancy_routing
            (user_id, profile_name, open_id, active, synced_at, version,
             created_at, updated_at, kind, owner_open_id, display_label,
             agent_id, provenance, upstream_profile)
        VALUES
            ('webui:ou_other:web_coder', 'web_coder', 'webui:ou_other:web_coder',
             1, 1, 1, 1, 1, 'agent', 'ou_other', 'Other Coder',
             'webui:ou_other:web_coder', 'webui-agent', 'other_profile')
        """
    )
    seeded._conn.commit()
    seeded.close()

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app(
                dispatch_agent=lambda _request: "",
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post("/api/run-broker/profiles", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner",
                }, json={
                    "profile_name": "web_coder",
                    "agent_id": "webui:ou_other:web_coder",
                    "upstream_profile": "owner_profile",
                })
                body = await response.text()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 403
        assert "does not belong" in body

    asyncio.run(runner())


def test_webui_run_broker_owner_header_rejects_cross_owner_agent_id(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.upsert(
        user_id="other-agent",
        profile_name="other_agent_profile",
        open_id="ou_other",
        provenance="auto",
    )
    seeded.close()

    async def runner():
        seen = []
        router_mod.override_routing_table(db_path)
        try:
            table = router_mod._get_routing_table()
            assert table is not None
            assert table.lookup_agent("other-agent") is not None

            async def dispatch(request):
                seen.append(request)
                return f"echo:{request.content}"

            app = create_run_broker_app(
                dispatch_agent=dispatch,
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post("/api/run-broker/runs", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner",
                }, json={
                    "channel": "webui",
                    "profile_name": "spoofed_client_profile",
                    "agent_id": "other-agent",
                    "user_key": "ou_webui",
                    "content": "hello owner gate",
                })
                body = await response.text()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 403
        assert "does not belong" in body
        assert seen == []

    asyncio.run(runner())


def test_webui_skillhub_install_uses_owner_scoped_profile(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared = tmp_path / ".hermes"
    skill_source = shared / "skills" / "hub" / "weather"
    profile = shared / "profiles" / "owner_sync_profile"
    skill_source.mkdir(parents=True)
    profile.mkdir(parents=True)
    (skill_source / "SKILL.md").write_text("# Weather\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))

    db_path = shared / "multitenancy.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.close()

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app(
                dispatch_agent=lambda request: f"echo:{request.content}",
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/api/run-broker/skills/install",
                    headers={"X-Hermes-Owner-Open-Id": "ou_owner"},
                    json={
                        "profile_name": "spoofed_profile",
                        "skill_path": "hub/weather",
                        "version": "v1",
                    },
                )
                body = await response.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 200
        assert body["profile_name"] == "owner_sync_profile"
        assert body["install"]["installed"] is True
        assert body["install"]["skill_path"] == "hub/weather"
        assert (profile / "skills" / "hub" / "weather").is_symlink()
        assert not (shared / "profiles" / "spoofed_profile").exists()

    asyncio.run(runner())


def test_webui_skillhub_install_without_owner_header_rejects_spoofed_profile(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared = tmp_path / ".hermes"
    skill_source = shared / "skills" / "hub" / "weather"
    skill_source.mkdir(parents=True)
    (skill_source / "SKILL.md").write_text("# Weather\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", raising=False)

    async def runner():
        app = create_run_broker_app(
            dispatch_agent=lambda request: f"echo:{request.content}",
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post(
                "/api/run-broker/skills/install",
                json={
                    "profile_name": "victim",
                    "skill_path": "hub/weather",
                    "version": "v1",
                },
            )
            body = await response.json()
        finally:
            await client.close()

        assert response.status == 403
        assert body == {"error": "owner identity required (X-Hermes-Owner-Open-Id)"}
        assert not (shared / "profiles" / "victim").exists()

    asyncio.run(runner())


def test_webui_skillhub_install_blocks_external_source_without_policy_allow(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared = tmp_path / ".hermes"
    skill_source = shared / "skills" / "hub" / "weather"
    profile = shared / "profiles" / "owner_sync_profile"
    skill_source.mkdir(parents=True)
    profile.mkdir(parents=True)
    (skill_source / "SKILL.md").write_text("# Weather\n", encoding="utf-8")
    (shared / "discovery-policy.yaml").write_text(
        """
        enabled: true
        audit:
          enabled: true
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))

    db_path = shared / "multitenancy.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.close()

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app(
                dispatch_agent=lambda request: f"echo:{request.content}",
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/api/run-broker/skills/install",
                    headers={"X-Hermes-Owner-Open-Id": "ou_owner"},
                    json={
                        "skill_path": "hub/weather",
                        "version": "v1",
                        "discovery_source": "skills-sh",
                    },
                )
                body = await response.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        audit_path = shared / "audit" / "discovery-policy.jsonl"
        record = json.loads(audit_path.read_text(encoding="utf-8").strip())

        assert response.status == 403
        assert body["error"] == "discovery source blocked by policy"
        assert body["discovery_policy"]["source"] == "skills-sh"
        assert body["discovery_policy"]["allowed"] is False
        assert not (profile / "skills" / "hub" / "weather").exists()
        assert record["profile_name"] == "owner_sync_profile"
        assert record["user_key"] == "ou_owner"
        assert record["sources"]["blocked"] == ["skills-sh"]

    asyncio.run(runner())


def test_webui_skillhub_install_allows_external_source_for_matching_owner(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared = tmp_path / ".hermes"
    skill_source = shared / "skills" / "hub" / "weather"
    profile = shared / "profiles" / "owner_sync_profile"
    skill_source.mkdir(parents=True)
    profile.mkdir(parents=True)
    (skill_source / "SKILL.md").write_text("# Weather\n", encoding="utf-8")
    (shared / "discovery-policy.yaml").write_text(
        """
        enabled: true
        audit:
          enabled: true
        sources:
          skills-sh:
            action: allow
            audience:
              users:
                - ou_owner
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))

    db_path = shared / "multitenancy.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.close()

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app(
                dispatch_agent=lambda request: f"echo:{request.content}",
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/api/run-broker/skills/install",
                    headers={"X-Hermes-Owner-Open-Id": "ou_owner"},
                    json={
                        "skill_path": "hub/weather",
                        "version": "v1",
                        "discovery_source": "skills-sh",
                    },
                )
                body = await response.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        audit_path = shared / "audit" / "discovery-policy.jsonl"
        record = json.loads(audit_path.read_text(encoding="utf-8").strip())

        assert response.status == 200
        assert body["profile_name"] == "owner_sync_profile"
        assert body["discovery_policy"]["source"] == "skills-sh"
        assert body["discovery_policy"]["allowed"] is True
        assert body["install"]["installed"] is True
        assert (profile / "skills" / "hub" / "weather").is_symlink()
        assert record["sources"]["allowed"] == ["skills-sh"]

    asyncio.run(runner())


def test_webui_skill_audit_collects_all_profiles(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared = tmp_path / ".hermes"
    alice_skill = shared / "profiles" / "alice" / "skills" / "managed" / "weather"
    group_skill = shared / "profiles" / "feishu_group_sales" / "skills" / "shared" / "readonly"
    alice_skill.mkdir(parents=True)
    group_skill.mkdir(parents=True)
    (alice_skill / "SKILL.md").write_text("# Weather\n", encoding="utf-8")
    (group_skill / "SKILL.md").write_text("# Readonly\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))

    async def runner():
        app = create_run_broker_app(
            dispatch_agent=lambda request: f"echo:{request.content}",
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get("/api/run-broker/skills/audit")
            body = await response.json()
        finally:
            await client.close()

        assert response.status == 200
        assert sorted(body["profiles"]) == ["alice", "feishu_group_sales"]
        assert body["profiles"]["alice"]["skills"][0]["skill_path"] == "managed/weather"
        assert body["profiles"]["feishu_group_sales"]["skills"][0]["skill_path"] == "shared/readonly"

    asyncio.run(runner())


def test_webui_run_broker_owner_header_without_agent_id_resolves_sync_root(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.close()

    async def runner():
        seen = []
        router_mod.override_routing_table(db_path)
        try:
            async def dispatch(request):
                seen.append(request)
                return f"echo:{request.content}"

            app = create_run_broker_app(
                dispatch_agent=dispatch,
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post("/api/run-broker/runs", headers={
                    "X-Hermes-Owner-Open-Id": " ou_owner ",
                }, json={
                    "channel": "webui",
                    "profile_name": "spoofed_client_profile",
                    "user_key": "ou_webui",
                    "content": "hello owner root",
                })
                body = await response.text()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 200
        assert '"kind": "content"' in body
        assert len(seen) == 1
        assert seen[0].profile_name == "owner_sync_profile"
        assert seen[0].user_key == "ou_owner"
        assert seen[0].credential_subject == "ou_owner"

    asyncio.run(runner())


def test_webui_run_broker_without_owner_header_keeps_legacy_profile_resolution():
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def runner():
        seen = []

        async def dispatch(request):
            seen.append(request)
            return f"echo:{request.content}"

        app = create_run_broker_app(
            dispatch_agent=dispatch,
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post("/api/run-broker/runs", json={
                "channel": "webui",
                "profile_name": "legacy_client_profile",
                "agent_id": "ignored-without-owner-header",
                "user_key": "ou_webui",
                "content": "hello legacy path",
            })
            body = await response.text()
        finally:
            await client.close()

        assert response.status == 200
        assert '"kind": "content"' in body
        assert len(seen) == 1
        assert seen[0].profile_name == "legacy_client_profile"

    asyncio.run(runner())


def test_webui_run_broker_enforced_without_owner_header_returns_403(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")

    async def runner():
        seen = []

        async def dispatch(request):
            seen.append(request)
            return f"echo:{request.content}"

        app = create_run_broker_app(
            dispatch_agent=dispatch,
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post("/api/run-broker/runs", json={
                "channel": "webui",
                "profile_name": "legacy_client_profile",
                "user_key": "ou_webui",
                "content": "hello enforced path",
            })
            body = await response.json()
        finally:
            await client.close()

        assert response.status == 403
        assert body == {"error": "owner identity required (X-Hermes-Owner-Open-Id)"}
        assert seen == []

    asyncio.run(runner())


def test_webui_run_broker_enforced_with_whitespace_owner_header_returns_403(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")

    async def runner():
        seen = []

        async def dispatch(request):
            seen.append(request)
            return f"echo:{request.content}"

        app = create_run_broker_app(
            dispatch_agent=dispatch,
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post("/api/run-broker/runs", headers={
                "X-Hermes-Owner-Open-Id": "   ",
            }, json={
                "channel": "webui",
                "profile_name": "legacy_client_profile",
                "user_key": "ou_webui",
                "content": "hello enforced path",
            })
            body = await response.json()
        finally:
            await client.close()

        assert response.status == 403
        assert body == {"error": "owner identity required (X-Hermes-Owner-Open-Id)"}
        assert seen == []

    asyncio.run(runner())


def test_webui_run_broker_enforced_with_owner_header_resolves_owned_profile(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.upsert(
        user_id="agent-owned",
        profile_name="owned_agent_profile",
        open_id="agent-owned",
        provenance="auto",
    )
    seeded._conn.execute(
        "UPDATE multitenancy_routing SET owner_open_id = 'ou_owner' "
        "WHERE user_id = 'agent-owned'"
    )
    seeded._conn.commit()
    seeded.close()

    async def runner():
        seen = []
        router_mod.override_routing_table(db_path)
        try:
            async def dispatch(request):
                seen.append(request)
                return f"echo:{request.content}"

            app = create_run_broker_app(
                dispatch_agent=dispatch,
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post("/api/run-broker/runs", headers={
                    "X-Hermes-Owner-Open-Id": " ou_owner ",
                    "X-Hermes-Agent-Id": " agent-owned ",
                }, json={
                    "channel": "webui",
                    "profile_name": "spoofed_client_profile",
                    "user_key": "ou_webui",
                    "content": "hello owner gate",
                })
                body = await response.text()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 200
        assert '"kind": "content"' in body
        assert len(seen) == 1
        assert seen[0].profile_name == "owned_agent_profile"

    asyncio.run(runner())


def test_webui_run_broker_enforcement_disabled_keeps_legacy_resolution():
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def runner():
        seen = []

        async def dispatch(request):
            seen.append(request)
            return f"echo:{request.content}"

        app = create_run_broker_app(
            dispatch_agent=dispatch,
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post("/api/run-broker/runs", json={
                "channel": "webui",
                "profile_name": "legacy_client_profile",
                "agent_id": "ignored-without-owner-header",
                "user_key": "ou_webui",
                "content": "hello legacy path",
            })
            body = await response.text()
        finally:
            await client.close()

        assert response.status == 200
        assert '"kind": "content"' in body
        assert len(seen) == 1
        assert seen[0].profile_name == "legacy_client_profile"

    asyncio.run(runner())


def test_webui_run_broker_enforced_cross_owner_error_is_not_masked(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.upsert(
        user_id="other-agent",
        profile_name="other_agent_profile",
        open_id="ou_other",
        provenance="auto",
    )
    seeded.close()

    async def runner():
        seen = []
        router_mod.override_routing_table(db_path)
        try:
            async def dispatch(request):
                seen.append(request)
                return f"echo:{request.content}"

            app = create_run_broker_app(
                dispatch_agent=dispatch,
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post("/api/run-broker/runs", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner",
                }, json={
                    "channel": "webui",
                    "profile_name": "spoofed_client_profile",
                    "agent_id": "other-agent",
                    "user_key": "ou_webui",
                    "content": "hello owner gate",
                })
                body = await response.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 403
        assert body == {"error": "agent_id 'other-agent' does not belong to asserted owner"}
        assert seen == []

    asyncio.run(runner())


def test_start_run_broker_server_refuses_without_key(monkeypatch):
    import hermes_multitenancy.webui_broker_server as broker_mod

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_PORT", "0")

    async def runner():
        await broker_mod.stop_run_broker_server()
        with pytest.raises(SystemExit) as excinfo:
            await broker_mod.start_run_broker_server()
        assert excinfo.value.code not in (None, 0)
        assert broker_mod._runner is None
        assert broker_mod._site is None

    asyncio.run(runner())


def test_start_run_broker_server_starts_with_key(monkeypatch):
    import hermes_multitenancy.webui_broker_server as broker_mod

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "broker-secret")
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_PORT", "0")

    async def runner():
        await broker_mod.stop_run_broker_server()
        try:
            await broker_mod.start_run_broker_server()
            assert broker_mod._runner is not None
            assert broker_mod._site is not None
        finally:
            await broker_mod.stop_run_broker_server()

    asyncio.run(runner())


def test_ensure_run_broker_server_started_is_noop_when_disabled(monkeypatch):
    import hermes_multitenancy.webui_broker_server as broker_mod

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", raising=False)
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)

    async def runner():
        await broker_mod.stop_run_broker_server()
        broker_mod.ensure_run_broker_server_started()
        await asyncio.sleep(0)
        assert broker_mod._server_task is None
        assert broker_mod._runner is None
        assert broker_mod._site is None

    asyncio.run(runner())
