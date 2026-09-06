"""The MT executor map decides which runtime executes a run (PLAN.md §2, C1).

Production symptom this exists for: the Server research expert never got past
step 0 because hermes' own tool loop is not a coding harness. Core 0.19.1 ships
the official Codex App-Server runtime, but MT builds ``runtime_kwargs`` itself
(``agent_real/run.py``), bypasses ``hermes_cli.runtime_provider`` and therefore
never set ``api_mode`` at all.

Four properties are pinned here, in this order of importance:

1. **fail closed** — mapped but codex missing / wrong wire ⇒ raise, and the
   error must be the shape that does NOT get re-run on the native runtime.
2. **request cannot rewrite** — metadata ``executor``/``runtime``/``api_mode``
   are dropped and audited; the config still wins.
3. **hit** — a mapped expert's AIAgent is constructed with
   ``api_mode="codex_app_server"``.
4. **miss** — everything else is byte-identical to today (no api_mode kwarg).
"""
from __future__ import annotations

import contextvars
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from hermes_multitenancy.agent_real import executor_map
from hermes_multitenancy.agent_real._core import ExpertUnavailableError


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _map_file(tmp_path: Path, payload: str, name: str = "executors.yaml") -> Path:
    path = tmp_path / name
    path.write_text(payload, encoding="utf-8")
    return path


def _env(path: Path | str | None) -> dict[str, str]:
    return {} if path is None else {executor_map.EXECUTOR_MAP_ENV: str(path)}


def _event(expert_id: str = "", **extra_metadata):
    metadata: dict[str, object] = {}
    if expert_id:
        metadata["expert_id"] = expert_id
    metadata.update(extra_metadata)
    return SimpleNamespace(
        text="ship it",
        message_id="m1",
        source=SimpleNamespace(
            platform=SimpleNamespace(value="webui"),
            chat_id="webui-1",
            chat_name="",
            chat_type="p2p",
            user_id="ou_alice",
            user_name="Alice",
        ),
        raw_event={"metadata": metadata},
    )


def _fake_codex_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    codex = bin_dir / executor_map.CODEX_BINARY
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex.chmod(0o755)
    return bin_dir


# --------------------------------------------------------------------------- #
# 1. resolve_runtime — the pure decision (C8 entry signature)
# --------------------------------------------------------------------------- #
def test_no_env_var_is_default_off():
    assert executor_map.resolve_runtime("kep-server", None, environ={}) == "hermes_default"


def test_absent_map_file_is_default_off(tmp_path: Path):
    missing = tmp_path / "nope.yaml"
    assert (
        executor_map.resolve_runtime("kep-server", None, environ=_env(missing))
        == "hermes_default"
    )


def test_expert_id_hit_selects_codex(tmp_path: Path):
    path = _map_file(tmp_path, 'kep-server: codex_app_server\n')
    assert (
        executor_map.resolve_runtime("kep-server", None, environ=_env(path))
        == "codex_app_server"
    )


def test_plugin_id_hit_selects_codex(tmp_path: Path):
    path = _map_file(tmp_path, 'keep-server-dev-plugin: codex_app_server\n')
    assert (
        executor_map.resolve_runtime("kep-server", "keep-server-dev-plugin", environ=_env(path))
        == "codex_app_server"
    )


def test_expert_id_wins_over_plugin_id(tmp_path: Path):
    path = _map_file(
        tmp_path,
        "kep-server: hermes_default\nkeep-server-dev-plugin: codex_app_server\n",
    )
    assert (
        executor_map.resolve_runtime("kep-server", "keep-server-dev-plugin", environ=_env(path))
        == "hermes_default"
    )


def test_unmapped_key_is_default(tmp_path: Path):
    path = _map_file(tmp_path, 'kep-server: codex_app_server\n')
    assert (
        executor_map.resolve_runtime("trevi-readonly", "other-plugin", environ=_env(path))
        == "hermes_default"
    )


def test_json_map_is_accepted(tmp_path: Path):
    path = _map_file(
        tmp_path, json.dumps({"kep-server": "codex_app_server"}), name="executors.json"
    )
    assert (
        executor_map.resolve_runtime("kep-server", None, environ=_env(path))
        == "codex_app_server"
    )


def test_unknown_runtime_value_fails_closed(tmp_path: Path):
    # A hyphen typo must not silently degrade to the native runtime.
    path = _map_file(tmp_path, 'kep-server: codex-app-server\n')
    with pytest.raises(executor_map.ExecutorUnavailable, match="unknown runtime"):
        executor_map.resolve_runtime("kep-server", None, environ=_env(path))


def test_unknown_runtime_for_another_key_does_not_break_this_run(tmp_path: Path):
    """Blast radius: a typo kills its own expert's runs, not everyone else's."""
    path = _map_file(tmp_path, "kep-server: nonsense\ntrevi: codex_app_server\n")
    assert (
        executor_map.resolve_runtime("trevi", None, environ=_env(path))
        == "codex_app_server"
    )


def test_malformed_map_fails_closed(tmp_path: Path):
    path = _map_file(tmp_path, "- not: a\n- mapping: b\n")
    with pytest.raises(executor_map.ExecutorUnavailable, match="must be a mapping"):
        executor_map.resolve_runtime("kep-server", None, environ=_env(path))


def test_empty_map_file_is_default_off(tmp_path: Path):
    path = _map_file(tmp_path, "")
    assert (
        executor_map.resolve_runtime("kep-server", None, environ=_env(path))
        == "hermes_default"
    )


# --------------------------------------------------------------------------- #
# 2. fail-closed preconditions
# --------------------------------------------------------------------------- #
def test_missing_codex_binary_fails_closed(tmp_path: Path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(executor_map.ExecutorUnavailable, match="no 'codex' binary"):
        executor_map.assert_codex_available(str(empty_dir))


def test_empty_path_fails_closed():
    with pytest.raises(executor_map.ExecutorUnavailable):
        executor_map.assert_codex_available("")


def test_codex_on_path_passes(tmp_path: Path):
    executor_map.assert_codex_available(str(_fake_codex_bin(tmp_path)))


@pytest.mark.parametrize(
    "provider,base_url",
    [
        ("openai", ""),
        ("openai-codex", ""),
        ("custom", "https://litellm.example/v1"),
        ("custom:litellm-sre", "https://litellm.example/v1"),
    ],
)
def test_openai_wire_providers_pass(provider: str, base_url: str):
    executor_map.assert_openai_wire(provider, base_url)


@pytest.mark.parametrize(
    "provider,base_url,match",
    [
        ("anthropic", "https://api.anthropic.com", "not available for provider"),
        ("openrouter", "https://openrouter.ai/api/v1", "not available for provider"),
        ("nous", "https://inference.nous/v1", "not available for provider"),
        ("", "https://litellm.example/v1", "not available for provider"),
        ("custom:litellm-sre", "", "explicit LiteLLM base_url"),
        # Same gateway, Anthropic-wire path — codex speaks Responses only.
        ("custom:litellm-sre", "https://litellm.example/anthropic", "Anthropic-wire"),
    ],
)
def test_non_openai_wire_fails_closed(provider: str, base_url: str, match: str):
    with pytest.raises(executor_map.ExecutorUnavailable, match=match):
        executor_map.assert_openai_wire(provider, base_url)


def test_unavailable_rides_the_no_degradation_rail():
    """The error shape is load-bearing, not cosmetic.

    ``_core._subprocess_failure`` rebuilds an ExpertUnavailableError in the
    parent ONLY for this (error_code, failure_subsystem) pair, and
    ``real_run_agent``/``stream_run_agent`` re-raise that type BEFORE the
    legacy-spike-runner fallback. Any other code and a fail-closed run would be
    silently re-executed on the hermes-native runtime.
    """
    from hermes_multitenancy.agent_real import _core

    exc = executor_map.ExecutorUnavailable("codex missing")
    assert isinstance(exc, ExpertUnavailableError)
    assert exc.error_code == "EXPERT_UNAVAILABLE"
    assert exc.failure_subsystem == "expert_resolution"
    assert exc.retryable is False
    assert "codex missing" in str(exc)

    rebuilt = _core._subprocess_failure(
        str(exc),
        error_code=exc.error_code,
        failure_subsystem=exc.failure_subsystem,
    )
    assert isinstance(rebuilt, ExpertUnavailableError)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_mapped_runtime_never_falls_back_on_untyped_precondition_failure(
    tmp_path: Path, monkeypatch, streaming: bool
):
    """A proxy/config ValueError must not re-run a mapped turn on Hermes."""
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.agent_real import _core

    profile_home = tmp_path / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    map_path = _map_file(tmp_path, "kep-server: codex_app_server\n")
    monkeypatch.setenv(executor_map.EXECUTOR_MAP_ENV, str(map_path))
    monkeypatch.setattr(
        _core, "_resolve_explicit_expert_for_execution", lambda *_args: None
    )
    legacy_calls = 0

    if streaming:
        async def failed_stream(*_args, **_kwargs):
            raise ValueError("codex proxy upstream URL is invalid")
            yield

        async def legacy_stream(*_args, **_kwargs):
            nonlocal legacy_calls
            legacy_calls += 1
            yield "content", "legacy"

        monkeypatch.setattr(_core, "_verified_codex_stream", failed_stream)
        monkeypatch.setattr(agent_real, "_stream_loop", legacy_stream)
        with pytest.raises(executor_map.ExecutorUnavailable):
            _ = [
                item
                async for item in agent_real.stream_run_agent(
                    _event("kep-server"), profile_home
                )
            ]
    else:
        async def failed_run(*_args, **_kwargs):
            raise ValueError("codex proxy upstream URL is invalid")

        async def legacy_run(*_args, **_kwargs):
            nonlocal legacy_calls
            legacy_calls += 1
            return "legacy"

        monkeypatch.setattr(_core, "_run_aiagent_subprocess", failed_run)
        monkeypatch.setattr(_core, "_legacy_real_run_agent", legacy_run)
        with pytest.raises(executor_map.ExecutorUnavailable):
            await agent_real.real_run_agent(_event("kep-server"), profile_home)

    assert legacy_calls == 0


# --------------------------------------------------------------------------- #
# 3. the request can never pick its executor
# --------------------------------------------------------------------------- #
def test_request_override_is_ignored_and_audited(tmp_path: Path, monkeypatch):
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))
    profile_home = tmp_path / "profiles" / "p_test"
    profile_home.mkdir(parents=True)
    # No map at all: the request asking for codex must still get hermes_default.
    event = _event(
        "kep-server",
        executor="codex_app_server",
        runtime="codex_app_server",
        api_mode="codex_app_server",
    )

    runtime = executor_map.runtime_for_event(event, profile_home, environ={})

    assert runtime == "hermes_default"
    assert event.raw_event[executor_map.EVENT_RUNTIME_KEY] == "hermes_default"
    lines = [json.loads(line) for line in audit_path.read_text().splitlines() if line]
    ignored = [
        line for line in lines
        if line["event_type"] == "executor_request_override_ignored"
    ]
    assert len(ignored) == 1
    assert ignored[0]["decision"] == "ignored"
    for field in executor_map.REQUEST_OVERRIDE_KEYS:
        assert field in ignored[0]["reason"]


def test_request_override_cannot_downgrade_a_mapped_run(tmp_path: Path):
    """The mirror field is an output, not an input: a forged value is overwritten."""
    path = _map_file(tmp_path, 'kep-server: codex_app_server\n')
    profile_home = tmp_path / "profiles" / "p_test"
    profile_home.mkdir(parents=True)
    event = _event("kep-server", executor="hermes_default")
    event.raw_event[executor_map.EVENT_RUNTIME_KEY] = "hermes_default"

    assert (
        executor_map.runtime_for_event(event, profile_home, environ=_env(path))
        == "codex_app_server"
    )
    assert event.raw_event[executor_map.EVENT_RUNTIME_KEY] == "codex_app_server"


def test_run_without_expert_is_never_mapped(tmp_path: Path):
    path = _map_file(tmp_path, 'kep-server: codex_app_server\n')
    profile_home = tmp_path / "profiles" / "p_test"
    profile_home.mkdir(parents=True)
    assert (
        executor_map.runtime_for_event(_event(), profile_home, environ=_env(path))
        == "hermes_default"
    )


# --------------------------------------------------------------------------- #
# 4. end-to-end through _run_with_aiagent — what AIAgent is actually built with
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate_environ():
    # _run_with_aiagent rewrites HOME/TMPDIR/XDG_*/HERMES_* to the profile home
    # and never restores them (a real run is process-scoped); running it
    # in-process would otherwise strand later tests on a deleted tmp_path.
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def _install_run_stubs(monkeypatch, shared_home: Path) -> None:
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.agent_real import run as run_module

    current_sender = contextvars.ContextVar("executor_test_sender", default=None)

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
        monkeypatch.setattr(run_module, name, lambda *_a, **_k: None)
    monkeypatch.setattr(agent_real, "_resolve_enabled_toolsets", lambda *_a, **_k: [])
    monkeypatch.setattr(run_module, "_resolve_disabled_toolsets", lambda *_a, **_k: [])
    monkeypatch.setattr(agent_real, "_role_override_block_for_event", lambda *_a, **_k: "")
    monkeypatch.setattr(
        agent_real, "_apply_expert_skill_scope_for_aiagent", lambda *_a, **_k: (lambda: None)
    )
    for name in (
        "_configure_gateway_approval_bridge",
        "_apply_runtime_env_for_aiagent",
        "_apply_vod_image_model_override_for_aiagent",
        "_sync_auxiliary_runtime_main_for_aiagent",
    ):
        monkeypatch.setattr(run_module, name, lambda *_a, **_k: (lambda: None))

    fake_session_context = ModuleType("gateway.session_context")
    fake_session_context.set_session_vars = lambda **_k: object()
    fake_session_context.clear_session_vars = lambda _t: None
    fake_gateway = ModuleType("gateway")
    fake_gateway.session_context = fake_session_context
    monkeypatch.setitem(sys.modules, "gateway", fake_gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_context", fake_session_context)


def _profile(tmp_path: Path, base_url: str = "https://litellm.example/v1"):
    shared_home = tmp_path / ".hermes"
    profile_home = shared_home / "profiles" / "sunke"
    (profile_home / "workspace").mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "\n".join(
            [
                "model:",
                "  default: custom:litellm-sre/gpt-5.6",
                f"  base_url: {base_url}",
                "custom_providers:",
                "  - name: litellm-sre",
                f"    base_url: {base_url}",
                "    api_key: sk-employee-key",
            ]
        ),
        encoding="utf-8",
    )
    return shared_home, profile_home


def _unbilled_env(monkeypatch):
    for name in (
        "HERMES_LITELLM_RUNTIME_API_KEY",
        "HERMES_LITELLM_RUNTIME_BASE_URL",
        "HERMES_LITELLM_RUNTIME_EMPLOYEE_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def _mapped_event(monkeypatch, expert_id: str = "kep-server"):
    token = "hcx_unit-test-disposable-token"
    base_url = "http://127.0.0.1:8765/unit-test-route-123456/v1"
    monkeypatch.setenv("CODEX_RUNTIME_KEY", token)
    monkeypatch.setenv("CODEX_PROXY_BASE_URL", base_url)
    return _event(
        expert_id,
        litellm_billing_enforced=True,
        litellm_billing_employee_user_id="employee-a",
    )


def _capture_agent(monkeypatch) -> dict:
    captured: dict = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_conversation(self, **_kwargs):
            return {"final_response": "ok"}

        def cleanup(self):
            pass

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    return captured


def test_mapped_expert_run_builds_aiagent_with_codex_api_mode(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.agent_real import run as run_module

    shared_home, profile_home = _profile(tmp_path)
    _install_run_stubs(monkeypatch, shared_home)
    _unbilled_env(monkeypatch)
    captured = _capture_agent(monkeypatch)
    path = _map_file(tmp_path, 'kep-server: codex_app_server\n')
    monkeypatch.setenv(executor_map.EXECUTOR_MAP_ENV, str(path))
    monkeypatch.setenv("PATH", str(_fake_codex_bin(tmp_path)))

    event = _mapped_event(monkeypatch)
    assert run_module._run_with_aiagent(event, profile_home) == "ok"

    assert captured["api_mode"] == "codex_app_server"
    assert captured["api_key"] == "hcx_unit-test-disposable-token"
    assert captured["base_url"] == "http://127.0.0.1:8765/unit-test-route-123456/v1"
    assert event.raw_event[executor_map.EVENT_RUNTIME_KEY] == "codex_app_server"


def test_mapped_webui_image_fails_before_any_auxiliary_model(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.agent_real import run as run_module

    shared_home, profile_home = _profile(tmp_path)
    _install_run_stubs(monkeypatch, shared_home)
    _unbilled_env(monkeypatch)
    path = _map_file(tmp_path, "kep-server: codex_app_server\n")
    monkeypatch.setenv(executor_map.EXECUTOR_MAP_ENV, str(path))
    monkeypatch.setenv("PATH", str(_fake_codex_bin(tmp_path)))
    monkeypatch.setattr(
        run_module,
        "_enrich_webui_image_attachments_for_aiagent",
        lambda *_a, **_k: pytest.fail("mapped image preflight must not call a model"),
    )
    event = _mapped_event(monkeypatch)
    event.text = "look\nLocal image path for tools: /workspace/uploads/receipt.png"

    with pytest.raises(RuntimeError, match="image attachments"):
        run_module._run_with_aiagent(event, profile_home)


def test_mapped_codex_run_reports_session_api_calls(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.agent_real import run as run_module

    shared_home, profile_home = _profile(tmp_path)
    _install_run_stubs(monkeypatch, shared_home)
    _unbilled_env(monkeypatch)

    class FakeCodexAgent:
        session_api_calls = 1
        _api_call_count = 0

        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, **_kwargs):
            return {"final_response": "ok"}

        def cleanup(self):
            pass

    monkeypatch.setitem(
        sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeCodexAgent)
    )
    path = _map_file(tmp_path, "kep-server: codex_app_server\n")
    monkeypatch.setenv(executor_map.EXECUTOR_MAP_ENV, str(path))
    monkeypatch.setenv("PATH", str(_fake_codex_bin(tmp_path)))
    usage: dict[str, object] = {}

    assert run_module._run_with_aiagent(
        _mapped_event(monkeypatch), profile_home, usage_sink=usage
    ) == "ok"
    assert usage["api_calls"] == 1


def test_unmapped_run_never_gets_an_api_mode_kwarg(monkeypatch, tmp_path: Path):
    """The miss path must be byte-identical to today: no api_mode kwarg at all."""
    from hermes_multitenancy.agent_real import run as run_module

    shared_home, profile_home = _profile(tmp_path)
    _install_run_stubs(monkeypatch, shared_home)
    _unbilled_env(monkeypatch)
    captured = _capture_agent(monkeypatch)
    path = _map_file(tmp_path, 'kep-server: codex_app_server\n')
    monkeypatch.setenv(executor_map.EXECUTOR_MAP_ENV, str(path))
    monkeypatch.setenv("PATH", str(_fake_codex_bin(tmp_path)))

    assert run_module._run_with_aiagent(_event("trevi-readonly"), profile_home) == "ok"
    assert "api_mode" not in captured


def test_no_map_configured_never_gets_an_api_mode_kwarg(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.agent_real import run as run_module

    shared_home, profile_home = _profile(tmp_path)
    _install_run_stubs(monkeypatch, shared_home)
    _unbilled_env(monkeypatch)
    captured = _capture_agent(monkeypatch)
    monkeypatch.delenv(executor_map.EXECUTOR_MAP_ENV, raising=False)

    assert run_module._run_with_aiagent(_event("kep-server"), profile_home) == "ok"
    assert "api_mode" not in captured


def test_mapped_run_without_codex_binary_refuses_to_start(monkeypatch, tmp_path: Path):
    """fail-closed: no codex ⇒ the run dies BEFORE AIAgent, no native fallback."""
    from hermes_multitenancy.agent_real import run as run_module

    shared_home, profile_home = _profile(tmp_path)
    _install_run_stubs(monkeypatch, shared_home)
    _unbilled_env(monkeypatch)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setitem(
        sys.modules,
        "run_agent",
        SimpleNamespace(
            AIAgent=lambda **_k: pytest.fail(
                "a mapped run with no codex must fail before AIAgent construction"
            )
        ),
    )
    path = _map_file(tmp_path, 'kep-server: codex_app_server\n')
    monkeypatch.setenv(executor_map.EXECUTOR_MAP_ENV, str(path))
    monkeypatch.setenv("PATH", str(empty_bin))

    with pytest.raises(executor_map.ExecutorUnavailable, match="no 'codex' binary"):
        run_module._run_with_aiagent(_event("kep-server"), profile_home)


def test_mapped_run_on_anthropic_wire_refuses_to_start(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.agent_real import run as run_module

    shared_home, profile_home = _profile(tmp_path, "https://litellm.example/anthropic")
    _install_run_stubs(monkeypatch, shared_home)
    _unbilled_env(monkeypatch)
    monkeypatch.setitem(
        sys.modules,
        "run_agent",
        SimpleNamespace(
            AIAgent=lambda **_k: pytest.fail("wrong wire must fail before AIAgent")
        ),
    )
    path = _map_file(tmp_path, 'kep-server: codex_app_server\n')
    monkeypatch.setenv(executor_map.EXECUTOR_MAP_ENV, str(path))
    monkeypatch.setenv("PATH", str(_fake_codex_bin(tmp_path)))

    with pytest.raises(executor_map.ExecutorUnavailable, match="Anthropic-wire"):
        run_module._run_with_aiagent(_event("kep-server"), profile_home)


def test_request_metadata_cannot_flip_a_run_to_codex(monkeypatch, tmp_path: Path):
    """No map entry + a request begging for codex ⇒ still the native runtime."""
    from hermes_multitenancy.agent_real import run as run_module

    shared_home, profile_home = _profile(tmp_path)
    _install_run_stubs(monkeypatch, shared_home)
    _unbilled_env(monkeypatch)
    captured = _capture_agent(monkeypatch)
    path = _map_file(tmp_path, 'someone-else: codex_app_server\n')
    monkeypatch.setenv(executor_map.EXECUTOR_MAP_ENV, str(path))
    monkeypatch.setenv("PATH", str(_fake_codex_bin(tmp_path)))
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    event = _event(
        "kep-server", executor="codex_app_server", api_mode="codex_app_server"
    )
    assert run_module._run_with_aiagent(event, profile_home) == "ok"
    assert "api_mode" not in captured


def test_auxiliary_runtime_is_not_switched_to_codex(monkeypatch, tmp_path: Path):
    """Title/compression/vision stay plain LiteLLM calls on a mapped run.

    ``_sync_auxiliary_runtime_main_for_aiagent`` forwards whatever api_mode it
    is handed into the auxiliary client; codex_app_server has no auxiliary
    implementation behind it, so leaking the main turn's transport there would
    break the mapped run's titles and vision preflight.
    """
    from hermes_multitenancy.agent_real import run as run_module

    shared_home, profile_home = _profile(tmp_path)
    _install_run_stubs(monkeypatch, shared_home)
    _unbilled_env(monkeypatch)
    _capture_agent(monkeypatch)
    aux_calls: list[dict] = []
    monkeypatch.setattr(
        run_module,
        "_sync_auxiliary_runtime_main_for_aiagent",
        lambda **kwargs: aux_calls.append(kwargs) or (lambda: None),
    )
    path = _map_file(tmp_path, 'kep-server: codex_app_server\n')
    monkeypatch.setenv(executor_map.EXECUTOR_MAP_ENV, str(path))
    monkeypatch.setenv("PATH", str(_fake_codex_bin(tmp_path)))

    assert run_module._run_with_aiagent(_mapped_event(monkeypatch), profile_home) == "ok"
    assert len(aux_calls) == 1
    assert aux_calls[0]["api_mode"] == ""
    assert aux_calls[0]["provider"] == ""
    assert aux_calls[0]["model"] == ""
    assert aux_calls[0]["base_url"] == ""
    assert aux_calls[0]["api_key"] == ""
