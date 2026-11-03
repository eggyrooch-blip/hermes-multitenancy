from __future__ import annotations

import contextvars
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from tests.test_aiagent_subprocess import _event, _install_fake_feishu_oapi


def _install_fake_env_passthrough(monkeypatch, cache):
    registered: list[list[str]] = []
    module = SimpleNamespace(
        register_env_passthrough=lambda names: registered.append(list(names)),
        _config_passthrough=cache,
    )
    tools_mod = sys.modules.get("tools") or types.ModuleType("tools")
    tools_mod.env_passthrough = module
    monkeypatch.setitem(sys.modules, "tools", tools_mod)
    monkeypatch.setitem(sys.modules, "tools.env_passthrough", module)
    return module, registered


def test_process_wide_passthrough_preserves_legacy_frozenset(monkeypatch):
    from hermes_multitenancy import agent_real

    module, registered = _install_fake_env_passthrough(
        monkeypatch,
        frozenset({"EXISTING_TOKEN"}),
    )

    agent_real._register_env_passthrough_process_wide(["GOOGLE_TENANTS_FILE"])

    assert registered == [["GOOGLE_TENANTS_FILE"]]
    assert module._config_passthrough == frozenset(
        {"EXISTING_TOKEN", "GOOGLE_TENANTS_FILE"}
    )


def test_process_wide_passthrough_preserves_home_keyed_dict(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    cache: dict[str, frozenset[str]] = {}
    module, registered = _install_fake_env_passthrough(monkeypatch, cache)

    agent_real._register_env_passthrough_process_wide(["GOOGLE_TENANTS_FILE"])

    assert registered == [["GOOGLE_TENANTS_FILE"]]
    assert module._config_passthrough is cache
    assert list(cache.values()) == [frozenset({"GOOGLE_TENANTS_FILE"})]


def test_feishu_oneshot_disables_detached_delivery(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n  default: openai/test-model\nplatform_toolsets:\n  feishu:\n  - delegate_task\n",
        encoding="utf-8",
    )
    (profile_home / ".env").write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")

    captured: list[dict] = []
    async_delivery = contextvars.ContextVar("async_delivery", default=True)

    def set_session_vars(**kwargs):
        captured.append(kwargs)
        return async_delivery.set(bool(kwargs.get("async_delivery", True)))

    fake_session_context = SimpleNamespace(
        set_session_vars=set_session_vars,
        clear_session_vars=async_delivery.reset,
        async_delivery_supported=async_delivery.get,
    )
    gateway_mod = sys.modules.get("gateway") or types.ModuleType("gateway")
    gateway_mod.session_context = fake_session_context
    monkeypatch.setitem(sys.modules, "gateway", gateway_mod)
    monkeypatch.setitem(sys.modules, "gateway.session_context", fake_session_context)

    observed: dict[str, bool] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        def run_conversation(self, user_message, task_id, persist_user_message=None):
            observed["async_delivery"] = async_delivery.get()
            return {"final_response": "done"}

        def cleanup(self):
            pass

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    _install_fake_feishu_oapi(monkeypatch)

    assert agent_real._run_with_aiagent(_event(), profile_home) == "done"
    assert captured[0]["async_delivery"] is False
    assert observed["async_delivery"] is False


def test_home_keyed_dict_without_core_predicate_uses_registered_allow_set(monkeypatch, tmp_path: Path):
    # Review P1 (filter-bypass): a transitional core whose register rejects a
    # provider credential must not see it widened back into the process cache.
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    cache: dict[str, frozenset[str]] = {}
    allowed: set[str] = set()

    def register(names):
        allowed.update(name for name in names if name != "OPENAI_API_KEY")

    module = SimpleNamespace(
        register_env_passthrough=register,
        _get_allowed=lambda: allowed,
        _config_passthrough=cache,
    )
    tools_mod = sys.modules.get("tools") or types.ModuleType("tools")
    tools_mod.env_passthrough = module
    monkeypatch.setitem(sys.modules, "tools", tools_mod)
    monkeypatch.setitem(sys.modules, "tools.env_passthrough", module)

    agent_real._register_env_passthrough_process_wide(
        ["OPENAI_API_KEY", "GOOGLE_TENANTS_FILE"]
    )

    assert list(cache.values()) == [frozenset({"GOOGLE_TENANTS_FILE"})]


def test_home_keyed_dict_without_any_core_filter_drops_model_provider_keys(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    cache: dict[str, frozenset[str]] = {}
    _install_fake_env_passthrough(monkeypatch, cache)

    agent_real._register_env_passthrough_process_wide(
        ["OPENAI_API_KEY", "ANTHROPIC_BASE_URL", "GOOGLE_TENANTS_FILE"]
    )

    assert list(cache.values()) == [frozenset({"GOOGLE_TENANTS_FILE"})]
