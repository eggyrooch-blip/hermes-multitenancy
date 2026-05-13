"""US-001 verification: register(ctx) wires the pre_gateway_dispatch hook."""
from __future__ import annotations

import inspect
from pathlib import Path

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
