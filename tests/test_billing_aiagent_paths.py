"""Billed AIAgent child-run paths (adapted from 25ba89a to main).

Origin: branch ``feat/hermes-user-key-probe-fix``.  The branch also carried a
"billed transport rewrite" (provider forced to ``custom`` + ``api_mode`` from
the endpoint path + a vision-preflight auxiliary install) that main never
absorbed: main keeps the profile provider/transport and relies on the
https-only endpoint gate, the ambient-env purge, the moa toolset ban, the
delegation guard and the in-run auxiliary billing runtime instead.  The tests
below pin down those main-side guarantees; the transport-rewrite assertions
are recorded in the SPEC Dead ends section.
"""
from __future__ import annotations

import contextvars
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _isolate_environ():
    # _run_with_aiagent rewrites HOME/TMPDIR/XDG_*/HERMES_* to the profile home
    # and never restores them (the real process is run-scoped, so restore is
    # someone else's job); running it in-process leaves later tests with HOME
    # pointing at a deleted tmp_path unless we snapshot and restore here.
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def _install_run_stubs(monkeypatch, shared_home: Path) -> None:
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.agent_real import run as run_module

    current_sender = contextvars.ContextVar("billing_test_sender", default=None)

    @contextmanager
    def sender_scope(value):
        token = current_sender.set(value)
        try:
            yield
        finally:
            current_sender.reset(token)

    monkeypatch.setattr(
        run_module,
        "_load_feishu_oapi_runtime",
        lambda _profile: (sender_scope, current_sender, shared_home),
    )
    for name in (
        "_install_credential_env_passthrough",
        "_install_ingest_secret_env_passthrough",
        "_install_skill_runtime_compat",
        "_install_session_search_proxy_for_aiagent",
        "_install_session_search_recall_db_proxy",
        "_log_feishu_identity_context",
        "_mark_session_source_feishu",
        "_register_aiagent_process_image_gen_providers",
    ):
        monkeypatch.setattr(run_module, name, lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        agent_real,
        "_resolve_enabled_toolsets",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        run_module,
        "_resolve_disabled_toolsets",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        agent_real,
        "_role_override_block_for_event",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        agent_real,
        "_apply_expert_skill_scope_for_aiagent",
        lambda *_args, **_kwargs: (lambda: None),
    )
    for name in (
        "_configure_gateway_approval_bridge",
        "_apply_runtime_env_for_aiagent",
        "_apply_vod_image_model_override_for_aiagent",
    ):
        monkeypatch.setattr(
            run_module,
            name,
            lambda *_args, **_kwargs: (lambda: None),
        )

    fake_session_context = ModuleType("gateway.session_context")
    fake_session_context.set_session_vars = lambda **_kwargs: object()
    fake_session_context.clear_session_vars = lambda _token: None
    fake_gateway = ModuleType("gateway")
    fake_gateway.session_context = fake_session_context
    monkeypatch.setitem(sys.modules, "gateway", fake_gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_context", fake_session_context)


def _install_tool_stubs(monkeypatch, vision_runtime, get_aux):
    async def vision_analyze_tool(image_url, user_prompt=None):
        vision_runtime.append(get_aux()._resolve_task_provider_model("vision"))
        return json.dumps({"success": True, "analysis": "employee-key vision"})

    fake_vision = ModuleType("tools.vision_tools")
    fake_vision.vision_analyze_tool = vision_analyze_tool
    fake_delegate = ModuleType("tools.delegate_tool")
    fake_delegate._resolve_delegation_credentials = (
        lambda _config, _parent: {
            "model": None,
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
        }
    )
    fake_tools = ModuleType("tools")
    fake_tools.vision_tools = fake_vision
    fake_tools.delegate_tool = fake_delegate
    monkeypatch.setitem(sys.modules, "tools", fake_tools)
    monkeypatch.setitem(sys.modules, "tools.vision_tools", fake_vision)
    monkeypatch.setitem(sys.modules, "tools.delegate_tool", fake_delegate)


def _install_aux_stub(monkeypatch, runtime_calls):
    def original_task_resolver(*_args, **_kwargs):
        return "openrouter", "vision-model", "https://other/v1", "ambient-key", None

    def original_provider_resolver(provider, model=None, **_kwargs):
        return provider, model

    fake_aux = ModuleType("agent.auxiliary_client")
    fake_aux._resolve_task_provider_model = original_task_resolver
    fake_aux.resolve_provider_client = original_provider_resolver
    fake_aux.set_runtime_main = lambda provider, model, **kwargs: runtime_calls.append(
        (provider, model, kwargs)
    )
    fake_aux.clear_runtime_main = lambda: runtime_calls.append(("clear", "", {}))
    fake_aux._evict_cached_clients = lambda _provider: None
    fake_agent_package = ModuleType("agent")
    fake_agent_package.auxiliary_client = fake_aux
    monkeypatch.setitem(sys.modules, "agent", fake_agent_package)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", fake_aux)
    return fake_aux, original_task_resolver, original_provider_resolver


def _profile_with_image(tmp_path: Path, base_url: str) -> tuple[Path, Path]:
    shared_home = tmp_path / ".hermes"
    profile_home = shared_home / "profiles" / "guest"
    image_path = profile_home / "workspace" / "uploads" / "receipt.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"fake-png")
    (profile_home / "config.yaml").write_text(
        "\n".join(
            [
                "model:",
                "  default: nous/test-model",
                f"  base_url: {base_url}",
            ]
        ),
        encoding="utf-8",
    )
    return shared_home, profile_home


def _webui_event(base_url: str):
    return SimpleNamespace(
        text="\n".join(
            [
                "analyze image",
                "[Attached image: receipt.png]",
                "Local image path for tools: uploads/receipt.png",
            ]
        ),
        message_id="m1",
        source=SimpleNamespace(
            platform=SimpleNamespace(value="webui"),
            chat_id="webui-1",
            chat_name="",
            chat_type="p2p",
            user_id="ou_alice",
            user_name="Alice",
        ),
        raw_event={
            "metadata": {
                "litellm_billing_enforced": True,
                "litellm_billing_base_url": base_url,
            }
        },
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "https://litellm.example/v1",
        "https://litellm.example/anthropic",
    ],
)
def test_billed_webui_run_uses_employee_key_and_purges_ambient_env(
    monkeypatch,
    tmp_path: Path,
    base_url: str,
):
    from hermes_multitenancy.agent_real import run as run_module

    shared_home, profile_home = _profile_with_image(tmp_path, base_url)
    _install_run_stubs(monkeypatch, shared_home)

    runtime_calls: list[tuple[str, str, dict]] = []
    fake_aux, original_task_resolver, original_provider_resolver = _install_aux_stub(
        monkeypatch, runtime_calls
    )
    vision_runtime: list[tuple] = []
    _install_tool_stubs(monkeypatch, vision_runtime, lambda: fake_aux)

    captured: dict[str, object] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_conversation(self, **kwargs):
            captured["user_message"] = kwargs.get("user_message")
            captured["aux_at_run"] = fake_aux._resolve_task_provider_model("title")
            return {"final_response": "ok"}

        def cleanup(self):
            pass

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    monkeypatch.setenv("HERMES_LITELLM_RUNTIME_API_KEY", "employee-key")
    monkeypatch.setenv("HERMES_LITELLM_RUNTIME_BASE_URL", base_url)
    monkeypatch.setenv("HERMES_LITELLM_RUNTIME_EMPLOYEE_ID", "alice")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-shared-key")

    assert run_module._run_with_aiagent(_webui_event(base_url), profile_home) == "ok"

    # The child runs on the employee key against the approved endpoint.
    assert captured["api_key"] == "employee-key"
    assert captured["base_url"] == base_url
    # Billed runs must not cross providers on failure (no ambient fallback).
    assert "fallback_model" not in captured
    # moa calls OpenRouter directly and must stay banned for billed runs.
    assert "moa" in captured["disabled_toolsets"]
    # In-run auxiliary tasks (title/compression/vision) are forced through the
    # billed endpoint+key and restored afterwards.
    assert captured["aux_at_run"] == (
        "custom",
        "vision-model",
        base_url,
        "employee-key",
        None,
    )
    assert fake_aux._resolve_task_provider_model is original_task_resolver
    assert fake_aux.resolve_provider_client is original_provider_resolver
    assert [call[0] for call in runtime_calls] == ["nous", "clear"]
    # The vision preflight ran and its analysis reached the model input.
    assert len(vision_runtime) == 1
    assert "employee-key vision" in str(captured["user_message"])
    # Ambient provider keys are purged from the billed child environment.
    assert os.environ.get("OPENAI_API_KEY") is None


def test_billed_run_rejects_unapproved_profile_base_url(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.agent_real import run as run_module

    billing_base = "https://litellm.example/v1"
    # Profile points somewhere else entirely: the billed key must not follow.
    shared_home, profile_home = _profile_with_image(
        tmp_path, "https://rogue.example/v1"
    )
    _install_run_stubs(monkeypatch, shared_home)
    runtime_calls: list[tuple[str, str, dict]] = []
    fake_aux, *_ = _install_aux_stub(monkeypatch, runtime_calls)
    _install_tool_stubs(monkeypatch, [], lambda: fake_aux)
    monkeypatch.setitem(
        sys.modules,
        "run_agent",
        SimpleNamespace(
            AIAgent=lambda **_kwargs: pytest.fail(
                "unapproved endpoint must fail before AIAgent construction"
            )
        ),
    )
    monkeypatch.setenv("HERMES_LITELLM_RUNTIME_API_KEY", "employee-key")
    monkeypatch.setenv("HERMES_LITELLM_RUNTIME_BASE_URL", billing_base)
    monkeypatch.setenv("HERMES_LITELLM_RUNTIME_EMPLOYEE_ID", "alice")

    with pytest.raises(RuntimeError, match="unapproved LiteLLM endpoint"):
        run_module._run_with_aiagent(_webui_event(billing_base), profile_home)


def test_billed_run_rejects_http_profile_base_url(monkeypatch, tmp_path: Path):
    """http on the child's resolved base_url fails closed even on same host."""
    from hermes_multitenancy.agent_real import run as run_module

    billing_base = "https://litellm.example/v1"
    shared_home, profile_home = _profile_with_image(
        tmp_path, "http://litellm.example/v1"
    )
    _install_run_stubs(monkeypatch, shared_home)
    runtime_calls: list[tuple[str, str, dict]] = []
    fake_aux, *_ = _install_aux_stub(monkeypatch, runtime_calls)
    _install_tool_stubs(monkeypatch, [], lambda: fake_aux)
    monkeypatch.setitem(
        sys.modules,
        "run_agent",
        SimpleNamespace(
            AIAgent=lambda **_kwargs: pytest.fail(
                "http endpoint must fail before AIAgent construction"
            )
        ),
    )
    monkeypatch.setenv("HERMES_LITELLM_RUNTIME_API_KEY", "employee-key")
    monkeypatch.setenv("HERMES_LITELLM_RUNTIME_BASE_URL", billing_base)
    monkeypatch.setenv("HERMES_LITELLM_RUNTIME_EMPLOYEE_ID", "alice")

    with pytest.raises(RuntimeError, match="unapproved LiteLLM endpoint"):
        run_module._run_with_aiagent(_webui_event(billing_base), profile_home)


def test_locked_core_custom_provider_cannot_run_nous_401_refresh():
    code = """
from run_agent import AIAgent
agent = AIAgent.__new__(AIAgent)
agent.provider = 'custom'
agent.api_mode = 'chat_completions'
assert agent._try_refresh_nous_client_credentials(force=True) is False
assert callable(agent._interruptible_api_call)
assert callable(agent._interruptible_streaming_api_call)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_locked_core_exposes_delegation_billing_seam():
    code = """
from tools import delegate_tool
assert callable(delegate_tool._resolve_delegation_credentials)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
