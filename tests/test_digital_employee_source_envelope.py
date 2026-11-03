from __future__ import annotations

import asyncio
import contextvars
import json
from pathlib import Path
import sys
import types
from contextlib import contextmanager
from types import SimpleNamespace


FIXTURE = Path(__file__).parent / "fixtures" / "digital_employee_source_refs.json"


def _profile(tmp_path: Path, name: str) -> Path:
    home = tmp_path / "profiles" / name
    (home / "workspace" / "reports").mkdir(parents=True)
    return home


def _install_fake_feishu_identity(monkeypatch) -> None:
    current_sender_open_id = contextvars.ContextVar("current_sender_open_id", default=None)

    @contextmanager
    def sender_open_id_scope(value):
        token = current_sender_open_id.set(value)
        try:
            yield
        finally:
            current_sender_open_id.reset(token)

    fake_client = SimpleNamespace(
        current_sender_open_id=current_sender_open_id,
        sender_open_id_scope=sender_open_id_scope,
        FEISHU_UAT_PATH=None,
        FEISHU_UAT_DIR=None,
    )
    tools_mod = sys.modules.get("tools") or types.ModuleType("tools")
    tools_mod.feishu_oapi_client = fake_client
    monkeypatch.setitem(sys.modules, "tools", tools_mod)
    monkeypatch.setitem(sys.modules, "tools.feishu_oapi_client", fake_client)


def test_explicit_tool_source_refs_are_normalized_to_the_fixed_contract(tmp_path: Path):
    from hermes_multitenancy.source_envelope import normalize_tool_source_refs

    fixture = json.loads(FIXTURE.read_text("utf-8"))
    profile_home = _profile(tmp_path, "profile_a")
    (profile_home / "workspace" / "reports" / "source.txt").write_text("ok", "utf-8")

    assert normalize_tool_source_refs(fixture["tool_result"], profile_home) == fixture["final_event"][
        "source_refs"
    ]


def test_plain_text_and_answer_urls_do_not_create_source_refs(tmp_path: Path):
    from hermes_multitenancy.source_envelope import normalize_tool_source_refs

    profile_home = _profile(tmp_path, "profile_a")
    assert normalize_tool_source_refs("See https://example.com/from-answer", profile_home) == []
    assert normalize_tool_source_refs({"output": "https://example.com/from-tool-text"}, profile_home) == []
    assert normalize_tool_source_refs({"source_refs": []}, profile_home) == []


def test_invalid_sensitive_and_overlong_source_fields_are_removed(tmp_path: Path):
    from hermes_multitenancy.source_envelope import normalize_tool_source_refs

    profile_home = _profile(tmp_path, "profile_a")
    outside = tmp_path / "outside.txt"
    outside.write_text("private", "utf-8")
    refs = [
        {"id": "js", "type": "web", "label": "bad", "uri": "javascript:alert(1)"},
        {"id": "file", "type": "web", "label": "bad", "uri": "file:///etc/passwd"},
        {"id": "traverse", "type": "workspace", "label": "bad", "locator": "../../outside.txt"},
        {"id": "unknown", "type": "database", "label": "bad", "locator": "row-1"},
        {"id": "long", "type": "other", "label": "x" * 257, "locator": "safe"},
        {"id": "identity", "type": "other", "label": "private", "locator": "open_id=ou_secret"},
        {"id": "user:ou_secret", "type": "other", "label": "private", "locator": "safe"},
        {"id": "identity-label", "type": "other", "label": "Employee ou_secret", "locator": "safe"},
        {
            "id": "identity-uri",
            "type": "web",
            "label": "private",
            "uri": "https://example.com/employee/ou_secret",
        },
        {
            "id": "encoded-identity-uri",
            "type": "web",
            "label": "private",
            "uri": "https://example.com/employee/%6f%75%5fsecret",
        },
        {
            "id": "credential-label",
            "type": "other",
            "label": "API credential sk-secret",
            "locator": "safe",
        },
        *(
            {"id": f"bare-{key}", "type": "other", "label": f"{key}={value}", "locator": "safe"}
            for key, value in (
                ("token", "secret"),
                ("password", "secret"),
                ("session_token", "secret"),
            )
        ),
        {
            "id": "safe",
            "type": "web",
            "label": "Safe",
            "uri": "https://example.com/a?token=secret#private",
            "authorization": "Bearer secret",
            "raw_identity": "ou_secret",
        },
    ]

    assert normalize_tool_source_refs({"source_refs": refs}, profile_home) == [
        {
            "id": "safe",
            "type": "web",
            "label": "Safe",
            "uri": "https://example.com/a",
        }
    ]


def test_workspace_locator_is_bound_to_the_routed_profile(tmp_path: Path):
    from hermes_multitenancy.source_envelope import normalize_tool_source_refs

    fixture = json.loads(FIXTURE.read_text("utf-8"))["tool_result"]
    profile_a = _profile(tmp_path, "profile_a")
    profile_b = _profile(tmp_path, "profile_b")
    (profile_a / "workspace" / "reports" / "source.txt").write_text("A", "utf-8")

    refs_a = normalize_tool_source_refs(fixture, profile_a)
    refs_b = normalize_tool_source_refs(fixture, profile_b)

    assert [ref["id"] for ref in refs_a] == ["web-guide", "workspace-report", "lark-policy"]
    assert [ref["id"] for ref in refs_b] == ["web-guide", "lark-policy"]
    assert not ({ref["id"] for ref in refs_a if ref["type"] == "workspace"} & {
        ref["id"] for ref in refs_b if ref["type"] == "workspace"
    })


def test_only_final_success_event_carries_refs_and_absence_is_omitted(tmp_path: Path):
    from hermes_multitenancy.run_broker import RunBroker, record_current_run_source_refs
    from hermes_multitenancy.run_models import RunEvent, RunRequest
    from hermes_multitenancy.webui_broker.periphery import _event_to_sse

    fixture = json.loads(FIXTURE.read_text("utf-8"))["final_event"]
    events: list[RunEvent] = []

    async def dispatch(_request: RunRequest) -> str:
        record_current_run_source_refs(fixture["source_refs"])
        return "final answer"

    broker = RunBroker(dispatch_agent=dispatch, emit_event=events.append)
    asyncio.run(
        broker.run(
            RunRequest(channel="webui", profile_name="profile_a", user_key="user-a", content="go")
        )
    )

    assert [event.kind for event in events] == ["content", "done"]
    assert events[0].source_refs is None
    assert events[1].source_refs == fixture["source_refs"]
    assert json.loads(_event_to_sse(events[1]).removeprefix("data: ")) == fixture

    no_refs = json.loads(_event_to_sse(RunEvent(kind="done")).removeprefix("data: "))
    assert "source_refs" not in no_refs


def test_real_aiagent_tool_complete_seam_forwards_only_normalized_refs(
    monkeypatch,
    tmp_path: Path,
):
    from hermes_multitenancy import agent_real

    fixture = json.loads(FIXTURE.read_text("utf-8"))
    profile_home = _profile(tmp_path, "profile_a")
    (profile_home / "workspace" / "reports" / "source.txt").write_text("ok", "utf-8")
    (profile_home / "config.yaml").write_text(
        "model:\n  default: openai/test-model\n",
        encoding="utf-8",
    )
    (profile_home / ".env").write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")

    class FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run_conversation(self, **_kwargs):
            tool_result = json.dumps(fixture["tool_result"])
            self.kwargs["tool_progress_callback"](
                "tool.completed",
                "controlled_source_tool",
                duration=0.1,
                is_error=False,
                result=tool_result,
            )
            self.kwargs["tool_complete_callback"](
                "call-1",
                "controlled_source_tool",
                {},
                tool_result,
            )
            self.kwargs["stream_delta_callback"]("final answer")
            return {"final_response": "final answer"}

        def cleanup(self):
            pass

    _install_fake_feishu_identity(monkeypatch)
    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    event = SimpleNamespace(
        text="go",
        message_id="message-1",
        source=SimpleNamespace(
            platform=SimpleNamespace(value="feishu"),
            chat_id="chat-1",
            chat_name="chat",
            chat_type="dm",
            user_id="user-1",
            user_name="tester",
            user_id_alt=None,
            message_id="message-1",
        ),
    )
    events: list[dict] = []

    result = agent_real._run_with_aiagent(
        event,
        profile_home,
        event_sink=lambda event_name, **payload: events.append(
            {"event": event_name, **payload}
        ),
    )

    assert result == "final answer"
    assert events[:-1] == [
            {
                "event": "tool_completed",
                "session_id": "agent:profile:profile_a:platform:feishu:chat_type:dm:chat:chat-1:user:user-1",
                "tool_call_id": "call-1",
                "name": "controlled_source_tool",
                "duration": 0.1,
                "is_error": False,
                "failure_subsystem": None,
                "error_code": None,
                "retryable": False,
            },
        {"event": "content", "text": "final answer"},
    ]
    assert events[-1] == {
        "event": "source_refs",
        "source_refs": fixture["final_event"]["source_refs"],
    }
