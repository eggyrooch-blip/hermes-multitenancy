from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

from tests.test_aiagent_subprocess import (
    _install_fake_feishu_oapi,
    _install_fake_gateway_session_context,
)


def test_webui_vision_unavailable_stops_before_model_and_next_text_runs(
    caplog,
    monkeypatch,
    tmp_path,
):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.run_models import RunRequest
    from hermes_multitenancy.webui_broker_server import _build_webui_event

    profile_home = tmp_path / "profiles" / "coder"
    upload_path = profile_home / "workspace" / "uploads" / "receipt.png"
    upload_path.parent.mkdir(parents=True)
    upload_path.write_bytes(b"fake-png")
    upload_path.with_suffix(".txt").write_text("not an image", encoding="utf-8")
    non_upload_path = profile_home / "workspace" / "notes" / "receipt.png"
    non_upload_path.parent.mkdir(parents=True)
    non_upload_path.write_bytes(b"fake-png")
    with upload_path.with_name("oversized.png").open("wb") as handle:
        handle.truncate(20 * 1024 * 1024 + 1)
    outside_path = tmp_path / "outside.png"
    outside_path.write_bytes(b"fake-png")
    unreadable_dir = profile_home / "workspace" / "uploads" / "private"
    unreadable_dir.mkdir()
    unreadable_path = unreadable_dir / "receipt.png"
    unreadable_path.write_bytes(b"private-png")
    (profile_home / "config.yaml").write_text(
        "model:\n  default: openai/test-model\nplatform_toolsets:\n  webui:\n  - lark-cli\n",
        encoding="utf-8",
    )
    (profile_home / ".env").write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")

    model_calls: list[str] = []
    connector_calls: list[str] = []

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, user_message, task_id, persist_user_message=None):
            model_calls.append(user_message)
            connector_calls.append("lark-cli")
            return {"final_response": "plain text ok"}

        def cleanup(self):
            pass

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    monkeypatch.setattr(
        agent_real,
        "_analyze_webui_uploaded_image",
        lambda *_args, **_kwargs: (False, "vision provider unavailable"),
    )
    _install_fake_feishu_oapi(monkeypatch)
    _install_fake_gateway_session_context(monkeypatch)

    def event(content: str):
        return _build_webui_event(
            RunRequest(
                channel="webui",
                profile_name="coder",
                user_key="ou_owner",
                content=content,
                session_id="webui-session-1",
            )
        )

    def image_request(path: str):
        return agent_real._run_with_aiagent(
            event(
                "\n".join(
                    [
                        "图片中都有什么内容",
                        "[Attached image: receipt.png]",
                        f"Local image path for tools: {path}",
                    ]
                )
            ),
            profile_home,
        )

    image_reply = image_request("/workspace/uploads/receipt.png")

    assert image_reply == agent_real._WEBUI_VISION_UNAVAILABLE_REPLY
    assert model_calls == []
    assert connector_calls == []

    for invalid_path in (
        "/workspace/uploads/missing.png",
        "/workspace/uploads/receipt.txt",
        "/workspace/notes/receipt.png",
        "/workspace/uploads/oversized.png",
        str(outside_path),
    ):
        assert image_request(invalid_path) == agent_real._WEBUI_VISION_UNAVAILABLE_REPLY
        assert model_calls == []
        assert connector_calls == []

    unreadable_dir.chmod(0)
    try:
        assert image_request("/workspace/uploads/private/receipt.png") == (
            agent_real._WEBUI_VISION_UNAVAILABLE_REPLY
        )
        assert model_calls == []
        assert connector_calls == []
    finally:
        unreadable_dir.chmod(0o700)

    for failure in (TimeoutError("private timeout detail"), RuntimeError("private crash detail")):
        def fail_analysis(*_args, **_kwargs):
            raise failure

        monkeypatch.setattr(agent_real, "_analyze_webui_uploaded_image", fail_analysis)
        assert image_request("/workspace/uploads/receipt.png") == agent_real._WEBUI_VISION_UNAVAILABLE_REPLY
        assert model_calls == []
        assert connector_calls == []

    assert "reason=analysis_timeout" in caplog.text
    assert "reason=analysis_exception" in caplog.text
    assert "private timeout detail" not in caplog.text
    assert "private crash detail" not in caplog.text
    assert str(outside_path) not in caplog.text
    assert str(unreadable_path) not in caplog.text

    text_reply = agent_real._run_with_aiagent(event("只回复纯文本 ok"), profile_home)

    assert text_reply == "plain text ok"
    assert len(model_calls) == 1
    assert connector_calls == ["lark-cli"]


@pytest.mark.asyncio
async def test_streaming_vision_terminal_never_replays_legacy_model(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real

    async def rejected_stream(*_args, **_kwargs):
        if False:
            yield
        raise agent_real._WebUIVisionAdmissionRejected(
            agent_real._WEBUI_VISION_UNAVAILABLE_REPLY
        )

    legacy_calls = 0

    async def legacy_stream(*_args, **_kwargs):
        nonlocal legacy_calls
        legacy_calls += 1
        yield "content", "legacy guessed image content"

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", rejected_stream)
    monkeypatch.setattr(agent_real, "_stream_loop", legacy_stream)
    event = SimpleNamespace(text="image", source=SimpleNamespace(platform="webui"))

    chunks = [item async for item in agent_real.stream_run_agent(event, tmp_path)]

    assert chunks == [("content", agent_real._WEBUI_VISION_UNAVAILABLE_REPLY)]
    assert legacy_calls == 0


def test_webui_vision_preflight_bounds_a_hung_analyzer(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real

    async def hang_forever(**_kwargs):
        await asyncio.sleep(0.2)

    fake_vision = types.ModuleType("tools.vision_tools")
    fake_vision.vision_analyze_tool = hang_forever
    monkeypatch.setitem(sys.modules, "tools.vision_tools", fake_vision)
    monkeypatch.setenv("HERMES_MULTITENANCY_IMAGE_PREP_TIMEOUT_S", "0.01")
    image = tmp_path / "image.png"
    image.write_bytes(b"fake-png")

    success, reason = agent_real._analyze_webui_uploaded_image(image)

    assert success is False
    assert reason == "vision_analyze failed."
