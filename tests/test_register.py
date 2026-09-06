"""US-001 verification: register(ctx) wires the pre_gateway_dispatch hook."""
from __future__ import annotations

import inspect
from pathlib import Path
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - test env is 3.11+, kept for local compatibility.
    import tomli as tomllib

import pytest


@pytest.fixture(autouse=True)
def _trusted_ingress_core_contract(monkeypatch):
    """Registration-unit tests do not install Hermes core; the seam has focused tests."""
    from hermes_multitenancy import trusted_feishu_ingress

    monkeypatch.setattr(trusted_feishu_ingress, "install_trusted_feishu_ingress_admission", lambda: None)


def test_register_function_exists():
    from hermes_multitenancy import register
    assert callable(register)
    sig = inspect.signature(register)
    params = list(sig.parameters)
    assert params == ["ctx"], f"register signature should be (ctx) — got {params}"


def test_plugin_yaml_present():
    """Plugin manifest is shipped as package data."""
    pkg_root = Path(__file__).parent.parent / "hermes_multitenancy"
    manifest = pkg_root / "plugin.yaml"
    assert manifest.exists(), f"plugin.yaml missing at {manifest}"
    text = manifest.read_text()
    assert "name: multitenancy" in text
    assert "kind: standalone" in text
    assert "pre_gateway_dispatch" in text


def test_pyproject_entry_point():
    """pyproject.toml declares the hermes_agent.plugins entry point."""
    proj = Path(__file__).parent.parent / "pyproject.toml"
    text = proj.read_text()
    assert 'multitenancy = "hermes_multitenancy"' in text


def test_pyproject_declares_all_python_packages():
    """Explicit setuptools package lists must include new subpackages."""
    root = Path(__file__).parent.parent
    config = tomllib.loads((root / "pyproject.toml").read_text())
    declared = set(config["tool"]["setuptools"]["packages"])
    discovered = {
        path.relative_to(root).as_posix().replace("/", ".")
        for path in (root / "hermes_multitenancy").rglob("*")
        if path.is_dir() and (path / "__init__.py").exists()
    }
    discovered.add("hermes_multitenancy")
    assert discovered <= declared


def test_register_calls_required_hooks():
    """register(ctx) wires routing plus durable lark-cli result interception.

    The callback may be a thin wrapper around ``on_pre_gateway_dispatch``
    (used to lazy-start the multi-profile cron worker on first dispatch).
    Identity is not required — callable and same dispatch contract are.
    """
    from hermes_multitenancy import register

    calls = []

    class FakeCtx:
        def register_hook(self, name, cb):
            calls.append((name, cb))

    register(FakeCtx())
    assert [name for name, _cb in calls] == [
        "post_tool_call",
        "transform_tool_result",
        "pre_gateway_dispatch",
    ]
    assert all(callable(cb) for _name, cb in calls)


def test_register_terminates_when_required_boundary_fails(monkeypatch):
    import hermes_multitenancy

    monkeypatch.setattr(
        hermes_multitenancy,
        "_register",
        lambda _ctx: (_ for _ in ()).throw(PermissionError("unreadable plugin file")),
    )

    with pytest.raises(SystemExit) as exc:
        hermes_multitenancy.register(object())

    assert exc.value.code == 1


def test_register_adds_tencent_vod_image_provider_when_supported():
    """Newer Hermes plugin contexts expose register_image_gen_provider."""
    from hermes_multitenancy import register

    hook_calls = []
    image_providers = []

    class FakeCtx:
        def register_hook(self, name, cb):
            hook_calls.append((name, cb))

        def register_image_gen_provider(self, provider):
            image_providers.append(provider)

    register(FakeCtx())

    assert [name for name, _cb in hook_calls] == [
        "post_tool_call",
        "transform_tool_result",
        "pre_gateway_dispatch",
    ]
    assert len(image_providers) == 1
    assert image_providers[0].name == "tencent-vod"


def test_register_schedules_optional_webui_run_broker_sidecar(monkeypatch, tmp_path):
    """The WebUI broker sidecar is opt-in but wired during plugin register."""
    import hermes_multitenancy

    calls = []

    class FakeCtx:
        def register_hook(self, name, cb):
            calls.append((name, cb))

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "multitenancy_router"))
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")
    monkeypatch.setattr(
        hermes_multitenancy.webui_broker_server,
        "ensure_run_broker_server_started",
        lambda: calls.append(("run_broker_server", None)),
    )

    hermes_multitenancy.register(FakeCtx())

    assert ("run_broker_server", None) in calls


def test_router_register_disables_direct_helpdesk_and_installs_clarify_after_media_retry(monkeypatch):
    import hermes_multitenancy
    from hermes_multitenancy import feishu_clarify_cards, feishu_media_retry
    from hermes_multitenancy import feishu_helpdesk_events, group_inviter_hook

    calls: list[str] = []

    class FakeCtx:
        def register_hook(self, *_args):
            pass

    monkeypatch.setattr(hermes_multitenancy, "may_own_cron_runtime", lambda: False)
    monkeypatch.setattr(hermes_multitenancy, "is_router_profile_runtime", lambda: True)
    monkeypatch.setattr(group_inviter_hook, "install_feishu_bot_added_hook", lambda: None)
    monkeypatch.setattr(
        feishu_helpdesk_events,
        "install_feishu_helpdesk_events_patch",
        lambda: calls.append("helpdesk"),
    )
    monkeypatch.setattr(feishu_media_retry, "install_feishu_media_retry_patch", lambda: calls.append("media"))
    monkeypatch.setattr(feishu_clarify_cards, "install_feishu_clarify_card_action_patch", lambda: calls.append("clarify"))
    monkeypatch.setattr(hermes_multitenancy.webui_broker_server, "ensure_run_broker_server_started", lambda: None)
    monkeypatch.setattr(hermes_multitenancy, "_start_credential_renewal_subsystem", lambda: None)

    hermes_multitenancy.register(FakeCtx())

    assert calls == ["media", "clarify"]


def test_runtime_pool_settings_can_be_overridden_from_env(monkeypatch):
    """Production can relax runtime cache/timeout without code changes."""
    monkeypatch.setenv("HERMES_MULTITENANCY_MAX_LOADED_RUNTIMES", "12")
    monkeypatch.setenv("HERMES_MULTITENANCY_IDLE_EVICT_SECONDS", "1800")
    monkeypatch.setenv("HERMES_MULTITENANCY_COLD_START_CONCURRENCY", "3")
    monkeypatch.setenv("HERMES_MULTITENANCY_INFLIGHT_TIMEOUT_SECONDS", "1200")

    from hermes_multitenancy import _build_runtime_pool
    from hermes_multitenancy.runtime import ProfileRuntime

    pool = _build_runtime_pool(lambda _name, home: ProfileRuntime(home))

    assert pool.max_loaded_runtimes == 12
    assert pool.idle_evict_seconds == 1800
    assert pool.inflight_timeout_seconds == 1200


def test_router_dispatch_fails_closed_before_core_when_run_broker_is_not_ready(monkeypatch):
    import hermes_multitenancy

    calls: list[str] = []
    monkeypatch.setattr(hermes_multitenancy, "is_router_profile_runtime", lambda: True)
    monkeypatch.setattr(
        hermes_multitenancy.webui_broker_server,
        "ensure_run_broker_server_started",
        lambda: calls.append("ensure"),
    )
    monkeypatch.setattr(
        hermes_multitenancy.webui_broker_server,
        "run_broker_server_ready",
        lambda: False,
    )
    monkeypatch.setattr(
        hermes_multitenancy,
        "on_pre_gateway_dispatch",
        lambda **_kwargs: calls.append("dispatch") or {"action": "skip"},
    )

    result = hermes_multitenancy._dispatch_with_worker_init(event=object(), gateway=object())

    assert result == {"action": "skip", "reason": "multitenancy run broker unavailable"}
    assert calls == ["ensure"]


def test_router_dispatch_fails_closed_when_run_broker_ensure_raises(monkeypatch):
    import hermes_multitenancy

    calls: list[str] = []
    monkeypatch.setattr(hermes_multitenancy, "is_router_profile_runtime", lambda: True)

    def fail_ensure():
        calls.append("ensure")
        raise RuntimeError("broker startup failed")

    monkeypatch.setattr(
        hermes_multitenancy.webui_broker_server,
        "ensure_run_broker_server_started",
        fail_ensure,
    )
    monkeypatch.setattr(
        hermes_multitenancy,
        "on_pre_gateway_dispatch",
        lambda **_kwargs: calls.append("dispatch") or {"action": "skip"},
    )

    result = hermes_multitenancy._dispatch_with_worker_init(event=object(), gateway=object())

    assert result == {"action": "skip", "reason": "multitenancy router hook failed closed"}
    assert calls == ["ensure"]


def test_router_dispatch_fails_closed_when_route_hook_raises(monkeypatch):
    import hermes_multitenancy

    monkeypatch.setattr(hermes_multitenancy, "is_router_profile_runtime", lambda: False)
    monkeypatch.setattr(hermes_multitenancy, "may_own_cron_runtime", lambda: False)
    monkeypatch.setattr(
        hermes_multitenancy,
        "on_pre_gateway_dispatch",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("route hook failed")),
    )

    result = hermes_multitenancy._dispatch_with_worker_init(event=object(), gateway=object())

    assert result == {"action": "skip", "reason": "multitenancy router hook failed closed"}


def test_hook_callback_is_sync_def():
    """Critical: invoke_hook (plugins.py:954) calls cb(**kwargs) synchronously.

    If on_pre_gateway_dispatch were async, it would emit RuntimeWarning
    'coroutine was never awaited' and the action would never apply.
    """
    import asyncio
    from hermes_multitenancy import on_pre_gateway_dispatch
    assert not asyncio.iscoroutinefunction(on_pre_gateway_dispatch), \
        "hook callback MUST be sync def (invoke_hook does not await)"


def test_log_task_failure_surfaces_exceptions(caplog):
    """Done-callback must log non-cancellation exceptions so silent failures
    don't disappear into the asyncio task GC. (architect nit #2)"""
    import asyncio
    import logging
    from hermes_multitenancy.router import _log_task_failure

    async def boom():
        raise ValueError("test boom")

    async def runner():
        task = asyncio.create_task(boom())
        try:
            await task
        except ValueError:
            pass
        # Task is done with exception — call the done-callback directly
        with caplog.at_level(logging.ERROR, logger="hermes_multitenancy.router"):
            _log_task_failure(task)

    asyncio.run(runner())
    assert any(
        "background task crashed" in r.message and "ValueError" in r.message
        for r in caplog.records
    ), [r.message for r in caplog.records]


def test_log_task_failure_silent_on_cancel():
    """Cancellation is normal control flow, not a crash — must NOT log error."""
    import asyncio
    from hermes_multitenancy.router import _log_task_failure

    async def runner():
        async def hang():
            await asyncio.sleep(60)
        task = asyncio.create_task(hang())
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Should not raise or log an error
        _log_task_failure(task)

    asyncio.run(runner())


def test_every_lazily_mapped_name_resolves():
    """The lazy __init__ contract: no mapped name may dangle.

    A renamed/dropped symbol behind PEP 562 is invisible until Hermes calls
    register() at startup and the plugin exits SystemExit(1). This is the check
    `hermes_multitenancy._import_smoke` runs during the release-bundle build.
    """
    import hermes_multitenancy as pkg

    for name in (*pkg._LAZY_ATTRS, *pkg._LAZY_SUBMODULES):
        assert getattr(pkg, name) is not None, name


def test_import_smoke_fails_when_a_mapped_symbol_goes_missing(monkeypatch):
    """The deploy smoke must actually fail on a broken mapping."""
    import importlib
    import sys

    import hermes_multitenancy as pkg

    monkeypatch.setitem(pkg._LAZY_ATTRS, "_symbol_that_does_not_exist", ".plugin_entry")
    monkeypatch.delitem(sys.modules, f"{pkg.__name__}._import_smoke", raising=False)

    with pytest.raises(AttributeError):
        importlib.import_module(f"{pkg.__name__}._import_smoke")


def test_attribute_probing_never_imports_an_unaudited_submodule():
    """`hasattr(pkg, "tencent_vod_image_gen")` must not import it.

    The eager version only bound the six audited submodules on the package;
    an open-ended __getattr__ would widen that and let a probe raise
    ModuleNotFoundError from an optional module's own missing dependency.
    """
    import hermes_multitenancy as pkg

    # __getattr__ directly: a submodule imported by some other test would be
    # bound on the package and short-circuit plain attribute access.
    with pytest.raises(AttributeError):
        pkg.__getattr__("tencent_vod_image_gen")

    assert pkg.__getattr__("webui_broker_server").__name__.endswith("webui_broker_server")
