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


def test_register_calls_register_hook_once():
    """register(ctx) must call ctx.register_hook('pre_gateway_dispatch', callback) exactly once.

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
    assert len(calls) == 1, f"expected exactly one register_hook call, got {len(calls)}"
    name, cb = calls[0]
    assert name == "pre_gateway_dispatch"
    assert callable(cb)


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

    assert len(hook_calls) == 1
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
