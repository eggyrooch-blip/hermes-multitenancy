"""Regression guards for Feishu websocket ownership.

Only the multitenancy router profile may create the Feishu gateway adapter.
Non-router profile gateways keep their API-server surface, but must not
compete for the same Feishu app websocket lock.
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("platform", ["feishu", "telegram"])
def test_update_text_answers_remain_available_only_outside_feishu(platform):
    from types import SimpleNamespace
    from gateway.run_inbound import GatewayInboundMixin
    from hermes_multitenancy.gateway_ownership import _patch_gateway_update_text_reply

    class Runner(GatewayInboundMixin):
        pass

    _patch_gateway_update_text_reply(Runner)
    runner = Runner()
    state = SimpleNamespace(persistent=SimpleNamespace(update_prompt_pending=True))
    runner._peek_session_state = lambda key: state
    writes = []
    runner._hm_write_update_response = lambda text: writes.append(text)
    event = SimpleNamespace(source=SimpleNamespace(platform=platform), text="y", get_command=lambda: None)
    reply = runner._hm_update_prompt_reply(event, "probe_session")
    assert writes == ([] if platform == "feishu" else ["y"])
    assert state.persistent.update_prompt_pending == (platform == "feishu")
    assert (reply is None) == (platform == "feishu")


class _PlatformKey:
    def __init__(self, value: str):
        self.value = value

    def __hash__(self) -> int:
        return hash(self.value)

    def __eq__(self, other: object) -> bool:
        return getattr(other, "value", other) == self.value

    def __repr__(self) -> str:
        return f"_PlatformKey({self.value!r})"


class _PlatformConfig:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled


def _install_fake_gateway_runner(monkeypatch):
    from hermes_constants import get_process_hermes_home

    gateway_pkg = types.ModuleType("gateway")
    gateway_run = types.ModuleType("gateway.run")
    gateway_status = types.ModuleType("gateway.status")
    gateway_status._get_process_hermes_home = get_process_hermes_home
    gateway_pkg.status = gateway_status

    class FakeGatewayRunner:
        def __init__(self, config=None):
            self.config = config
            self.original_init_called = True

        def _create_adapter(self, platform, config):
            return SimpleNamespace(platform=platform, config=config)

        async def _handle_message(self, event):
            return None

        async def _handle_active_session_busy_message(self, event):
            return False

    gateway_run.GatewayRunner = FakeGatewayRunner
    monkeypatch.setitem(sys.modules, "gateway", gateway_pkg)
    monkeypatch.setitem(sys.modules, "gateway.run", gateway_run)
    monkeypatch.setitem(sys.modules, "gateway.status", gateway_status)
    return FakeGatewayRunner


def _gateway_config() -> SimpleNamespace:
    return SimpleNamespace(
        platforms={
            _PlatformKey("feishu"): _PlatformConfig(enabled=True),
            _PlatformKey("api_server"): _PlatformConfig(enabled=True),
        }
    )


@pytest.mark.parametrize("injected", [False, True])
def test_mt_runner_keeps_core_multiplexer_out_of_tenant_admission(monkeypatch, tmp_path, injected):
    from gateway.config import GatewayConfig
    from gateway import config as config_module, run as run_module
    from hermes_multitenancy import gateway_ownership as go

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "multitenancy_router"))
    config = GatewayConfig.from_dict({"multiplex_profiles": True})
    monkeypatch.setattr(config_module, "load_gateway_config", lambda: config)
    monkeypatch.setattr(run_module, "load_gateway_config_for_runner", lambda: pytest.fail("native host preflight bypassed MT admission"))
    runner_type = run_module.GatewayRunner
    # Keep real core construction/config selection; isolate unrelated I/O.
    for name in ("_warn_if_docker_media_delivery_is_risky", "_init_runtime_settings",
                 "_init_session_store", "_init_lifecycle_state", "_init_runtime_caches",
                 "_init_startup_checks", "_init_session_db", "_init_registries_and_clocks"):
        monkeypatch.setattr(runner_type, name, lambda self: None)
    monkeypatch.setattr(runner_type, "__init__", runner_type.__init__)
    go._patch_gateway_runner_init(runner_type)
    runner = runner_type(config) if injected else runner_type()
    assert runner.config is config
    assert runner.config.multiplex_profiles is False


def test_mt_native_cron_never_enumerates_or_switches_to_peer_stores(monkeypatch, tmp_path):
    from gateway import run as gateway_run
    from hermes_cli import profiles
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_multitenancy import gateway_ownership as go

    home = tmp_path / "profiles" / "multitenancy_router"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setattr(profiles, "profiles_to_serve", lambda **kw: pytest.fail("native ticker enumerated MT tenant stores"))
    monkeypatch.setattr(gateway_run, "_cron_tick_profile_homes", lambda cfg: profiles.profiles_to_serve(multiplex=True))
    go._patch_gateway_cron_profile_scope()
    assert gateway_run._cron_tick_profile_homes(None) == [("multitenancy_router", home)]
    token = set_hermes_home_override(tmp_path / "profiles" / "peer")
    try:
        assert gateway_run._cron_tick_profile_homes(None) == [("multitenancy_router", home)]
    finally:
        reset_hermes_home_override(token)


def test_non_router_profile_strips_feishu_before_gateway_start(monkeypatch, tmp_path):
    """A profile gateway must keep API-server config but never create Feishu."""
    FakeGatewayRunner = _install_fake_gateway_runner(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "user_profile"))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)

    from hermes_multitenancy.gateway_ownership import install_gateway_ownership_guard

    install_gateway_ownership_guard()
    runner = FakeGatewayRunner(_gateway_config())

    platform_names = {getattr(platform, "value", platform) for platform in runner.config.platforms}
    assert "feishu" not in platform_names
    assert "api_server" in platform_names
    assert runner.original_init_called is True


def test_router_profile_keeps_feishu_gateway_platform(monkeypatch, tmp_path):
    """The router is the only process allowed to own the Feishu websocket."""
    FakeGatewayRunner = _install_fake_gateway_runner(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "multitenancy_router"))

    from hermes_multitenancy.gateway_ownership import install_gateway_ownership_guard

    install_gateway_ownership_guard()
    runner = FakeGatewayRunner(_gateway_config())

    platform_names = {getattr(platform, "value", platform) for platform in runner.config.platforms}
    assert "feishu" in platform_names
    assert "api_server" in platform_names


def test_existing_non_router_runner_cannot_create_feishu_adapter(monkeypatch, tmp_path):
    """Plugin discovery runs after GatewayRunner construction in Hermes gateway.start()."""
    FakeGatewayRunner = _install_fake_gateway_runner(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "user_profile"))
    runner = FakeGatewayRunner(_gateway_config())

    from hermes_multitenancy.gateway_ownership import install_gateway_ownership_guard

    install_gateway_ownership_guard()

    assert runner._create_adapter(_PlatformKey("feishu"), _PlatformConfig(enabled=True)) is None
    assert runner._create_adapter(_PlatformKey("api_server"), _PlatformConfig(enabled=True)) is not None


def test_fixed_expert_env_must_not_leak_to_subprocess_allowlist():
    """Security invariant (codex review): HERMES_MULTITENANCY_FIXED_EXPERT must
    NOT be in the AIAgent subprocess env allowlist — otherwise a per-user
    subprocess would inherit it and _may_own_feishu_runtime() would treat that
    per-user gateway as a fixed-expert Feishu owner. Only READONLY is allowlisted.
    """
    from hermes_multitenancy.agent_real._core import _SUBPROCESS_ENV_ALLOWLIST

    assert "HERMES_MULTITENANCY_FIXED_EXPERT" not in _SUBPROCESS_ENV_ALLOWLIST
    # ROUTER_PROFILE (the old masquerade env) must also not leak.
    assert "HERMES_MULTITENANCY_ROUTER_PROFILE" not in _SUBPROCESS_ENV_ALLOWLIST
    # The app-id LABEL is safe to forward — it is not an ownership grant
    # (_may_own_feishu_runtime reads only FIXED_EXPERT) — and it lets the expert
    # bot's create subprocess tag a cron job's source_app.
    assert "HERMES_MULTITENANCY_FIXED_EXPERT_APP_ID" in _SUBPROCESS_ENV_ALLOWLIST


def test_fixed_expert_profile_keeps_feishu_without_masquerade(monkeypatch, tmp_path):
    """Expert bot (own app, FIXED_EXPERT set) owns Feishu — one method, no
    ROUTER_PROFILE masquerade needed."""
    FakeGatewayRunner = _install_fake_gateway_runner(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "expert_krd"))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_MULTITENANCY_ROUTER_PROFILE", raising=False)  # NO masquerade
    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT", "kep-trevi-resource-delivery-expert")

    from hermes_multitenancy.gateway_ownership import install_gateway_ownership_guard

    install_gateway_ownership_guard()
    runner = FakeGatewayRunner(_gateway_config())

    platform_names = {getattr(platform, "value", platform) for platform in runner.config.platforms}
    assert "feishu" in platform_names  # kept — expert bot may own its app's WS
    assert runner._create_adapter(_PlatformKey("feishu"), _PlatformConfig(enabled=True)) is not None


def test_may_own_feishu_predicate_router_expert_and_per_user(monkeypatch, tmp_path):
    from hermes_multitenancy import gateway_ownership as go

    # router profile → owns
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "multitenancy_router"))
    monkeypatch.delenv("HERMES_MULTITENANCY_FIXED_EXPERT", raising=False)
    assert go._may_own_feishu_runtime() is True

    # per-user profile, no FIXED_EXPERT → does NOT own (fail-closed)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "feishu_g41a5b5g"))
    assert go._may_own_feishu_runtime() is False

    # fixed-expert instance (non-router) → owns
    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT", "kep-trevi-resource-delivery-expert")
    assert go._may_own_feishu_runtime() is True


def test_fixed_expert_owns_feishu_but_does_NOT_run_router_sidecars(monkeypatch, tmp_path):
    """FIXED_EXPERT owns Feishu AND now runs the cron worker (源进源出:
    source_app-partitioned self-execution), but must STILL NOT run router-only
    sidecars (run-broker / credential renewal / bot-added) — owning Feishu +
    running cron does not flip is_router_profile_runtime True."""
    import hermes_multitenancy
    import hermes_multitenancy.group_inviter_hook as group_inviter_hook
    from hermes_multitenancy import trusted_feishu_ingress

    calls = []

    class FakeCtx:
        def register_hook(self, name, cb):
            calls.append((name, cb))

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "expert_krd"))
    monkeypatch.delenv("HERMES_MULTITENANCY_ROUTER_PROFILE", raising=False)
    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT", "kep-trevi-resource-delivery-expert")  # owns Feishu
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")
    monkeypatch.setattr(trusted_feishu_ingress, "install_trusted_feishu_ingress_admission", lambda: None)
    monkeypatch.setattr(hermes_multitenancy, "install_cron_runtime_patches", lambda: calls.append(("cron_patches", None)))
    monkeypatch.setattr(hermes_multitenancy, "install_gateway_startup_watcher", lambda: calls.append(("cron_watcher", None)))
    monkeypatch.setattr(
        group_inviter_hook, "install_feishu_bot_added_hook",
        lambda: calls.append(("bot_added_hook", None)),
    )
    monkeypatch.setattr(
        hermes_multitenancy.webui_broker_server,
        "ensure_run_broker_server_started",
        lambda: calls.append(("run_broker_server", None)),
    )

    hermes_multitenancy.register(FakeCtx())

    # FIXED_EXPERT runs the cron worker (owns its app's jobs), but NOT the
    # router-only sidecars.
    assert ("cron_patches", None) in calls
    assert ("cron_watcher", None) in calls
    assert ("run_broker_server", None) not in calls
    assert ("bot_added_hook", None) not in calls


def test_register_does_not_start_router_only_sidecars_on_non_router(monkeypatch, tmp_path):
    """Profile-local gateways should not start router-owned sidecars."""
    import hermes_multitenancy
    import hermes_multitenancy.group_inviter_hook as group_inviter_hook
    from hermes_multitenancy import trusted_feishu_ingress

    calls = []

    class FakeCtx:
        def register_hook(self, name, cb):
            calls.append((name, cb))

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "user_profile"))
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")
    monkeypatch.setattr(trusted_feishu_ingress, "install_trusted_feishu_ingress_admission", lambda: None)
    monkeypatch.setattr(hermes_multitenancy, "install_cron_runtime_patches", lambda: calls.append(("cron_patches", None)))
    monkeypatch.setattr(hermes_multitenancy, "install_gateway_startup_watcher", lambda: calls.append(("cron_watcher", None)))
    monkeypatch.setattr(
        group_inviter_hook,
        "install_feishu_bot_added_hook",
        lambda: calls.append(("bot_added_hook", None)),
    )
    monkeypatch.setattr(
        hermes_multitenancy.webui_broker_server,
        "ensure_run_broker_server_started",
        lambda: calls.append(("run_broker_server", None)),
    )

    hermes_multitenancy.register(FakeCtx())

    assert ("run_broker_server", None) not in calls
    assert ("cron_watcher", None) not in calls
    assert ("cron_patches", None) not in calls
    assert ("bot_added_hook", None) not in calls
    assert [name for name, _cb in calls] == [
        "post_tool_call",
        "transform_tool_result",
        "pre_gateway_dispatch",
    ]
