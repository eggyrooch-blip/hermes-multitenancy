"""Regression guards for Feishu websocket ownership.

Only the multitenancy router profile may create the Feishu gateway adapter.
Non-router profile gateways keep their API-server surface, but must not
compete for the same Feishu app websocket lock.
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace


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
    gateway_pkg = types.ModuleType("gateway")
    gateway_run = types.ModuleType("gateway.run")

    class FakeGatewayRunner:
        def __init__(self, config=None):
            self.config = config
            self.original_init_called = True

        def _create_adapter(self, platform, config):
            return SimpleNamespace(platform=platform, config=config)

    gateway_run.GatewayRunner = FakeGatewayRunner
    monkeypatch.setitem(sys.modules, "gateway", gateway_pkg)
    monkeypatch.setitem(sys.modules, "gateway.run", gateway_run)
    return FakeGatewayRunner


def _gateway_config() -> SimpleNamespace:
    return SimpleNamespace(
        platforms={
            _PlatformKey("feishu"): _PlatformConfig(enabled=True),
            _PlatformKey("api_server"): _PlatformConfig(enabled=True),
        }
    )


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
    """The masquerade side-effect fix: owning Feishu (FIXED_EXPERT) must NOT flip
    is_router_profile_runtime True — expert bot still skips cron/broker/renewal."""
    import hermes_multitenancy
    import hermes_multitenancy.group_inviter_hook as group_inviter_hook

    calls = []

    class FakeCtx:
        def register_hook(self, name, cb):
            calls.append((name, cb))

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "expert_krd"))
    monkeypatch.delenv("HERMES_MULTITENANCY_ROUTER_PROFILE", raising=False)
    monkeypatch.setenv("HERMES_MULTITENANCY_FIXED_EXPERT", "kep-trevi-resource-delivery-expert")  # owns Feishu
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")
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

    # FIXED_EXPERT grants Feishu ownership but NOT router-only subsystems.
    assert ("run_broker_server", None) not in calls
    assert ("cron_watcher", None) not in calls
    assert ("cron_patches", None) not in calls
    assert ("bot_added_hook", None) not in calls


def test_register_does_not_start_router_only_sidecars_on_non_router(monkeypatch, tmp_path):
    """Profile-local gateways should not start router-owned sidecars."""
    import hermes_multitenancy
    import hermes_multitenancy.group_inviter_hook as group_inviter_hook

    calls = []

    class FakeCtx:
        def register_hook(self, name, cb):
            calls.append((name, cb))

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "user_profile"))
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")
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
    assert [name for name, _cb in calls] == ["pre_gateway_dispatch"]
