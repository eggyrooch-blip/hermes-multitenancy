"""Missing upstream admission support must fail closed before dispatch."""
import logging
import sys
import threading

import pytest

import hermes_multitenancy as mt
import hermes_multitenancy.plugin_entry as pe
from hermes_multitenancy import startup_guard
import hermes_multitenancy.trusted_feishu_ingress as tfi


@pytest.mark.parametrize("reason", [
    "Feishu core lacks trusted ingress contract",
    "live Feishu adapter module did not materialize",
    "unexpected adapter failure",
])
def test_missing_admission_aborts_install_and_cannot_bypass_dispatch(monkeypatch, reason):
    def fail():
        raise RuntimeError(reason)
    monkeypatch.setattr(tfi, "install_trusted_feishu_ingress_admission", fail)
    with pytest.raises(RuntimeError, match=reason):
        pe._install_trusted_feishu_ingress()
    monkeypatch.setattr(tfi, "validate_admitted_feishu_event", lambda *args: False)
    def forbidden(**kwargs):
        pytest.fail("unadmitted event reached routing")
    monkeypatch.setattr(mt, "on_pre_gateway_dispatch", forbidden)
    assert pe._dispatch_with_worker_init(event=object(), gateway=object()) == {
        "action": "skip", "reason": "trusted Feishu ingress denied",
    }


def test_installed_admission_keeps_dispatch_validation(monkeypatch):
    monkeypatch.setattr(tfi, "install_trusted_feishu_ingress_admission", lambda: None)
    pe._install_trusted_feishu_ingress()
    monkeypatch.setattr(tfi, "validate_admitted_feishu_event", lambda *args: False)
    assert pe._dispatch_with_worker_init(event=object(), gateway=object())["action"] == "skip"


def _pin_core_load_timeout(monkeypatch, seconds):
    """Pin core's per-plugin load deadline (``plugins.load_timeout_seconds``).

    core 0.21.5 runs import + register() on a deadline worker thread by default
    (10s) and inline when the key is 0; cores without the key always load inline,
    so only the inline case applies to them.
    """
    import hermes_cli.plugins_loader as loader

    if not hasattr(loader, "_resolve_plugin_load_timeout"):
        if seconds:
            pytest.skip("core has no per-plugin load deadline; the inline case covers it")
        return
    monkeypatch.setattr(loader, "_resolve_plugin_load_timeout", lambda: float(seconds))


def _failing_admission(monkeypatch, error=None):
    """Make the required trusted-ingress step fail; record the thread it ran on."""
    threads = []

    def fail():
        threads.append(threading.current_thread())
        raise error() if error else RuntimeError("Feishu core lacks trusted ingress contract")

    monkeypatch.setattr(tfi, "install_trusted_feishu_ingress_admission", fail)
    monkeypatch.setattr(mt, "_register", lambda ctx: pe._install_trusted_feishu_ingress())
    return threads


def _multitenancy_manager(monkeypatch):
    from hermes_cli.plugins import PluginManager, PluginManifest

    manager = PluginManager()
    monkeypatch.setattr(manager, "_load_entrypoint_module", lambda manifest: mt)
    return manager, PluginManifest(name="multitenancy", source="entrypoint")


def _assert_load_mode(threads, load_timeout):
    assert len(threads) == 1
    if load_timeout:
        assert threads[0] is not threading.current_thread()
        assert threads[0].name == "plugin-load:multitenancy"
    else:
        assert threads[0] is threading.current_thread()


def _mt_critical_lines(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == mt.__name__ and record.levelno >= logging.CRITICAL
    ]


@pytest.mark.parametrize(
    ("error", "error_name"),
    [
        (lambda: RuntimeError("Feishu core lacks trusted ingress contract"), "RuntimeError"),
        (lambda: SystemExit(7), "SystemExit"),
    ],
    ids=["exception", "systemexit"],
)
@pytest.mark.parametrize("load_timeout", [0, 10], ids=["inline", "deadline"])
def test_real_core_loader_cannot_swallow_missing_admission(
    monkeypatch, caplog, load_timeout, error, error_name,
):
    _pin_core_load_timeout(monkeypatch, load_timeout)
    threads = _failing_admission(monkeypatch, error)
    manager, manifest = _multitenancy_manager(monkeypatch)
    caplog.set_level(logging.INFO)

    with pytest.raises(mt.RequiredRegistrationFailed) as stopped:
        manager._load_plugin(manifest)

    assert stopped.value.code == 1
    assert not isinstance(stopped.value, (Exception, SystemExit))
    _assert_load_mode(threads, load_timeout)
    plugin = manager._plugins.get("multitenancy")
    assert plugin is None or not plugin.enabled
    assert not hasattr(sys, "_hermes_multitenancy_registered_module")
    assert _mt_critical_lines(caplog) == [
        "multitenancy required registration failed "
        f"(_install_trusted_feishu_ingress: {error_name}): stopping gateway"
    ]


@pytest.mark.parametrize("load_timeout", [0, 10], ids=["inline", "deadline"])
def test_real_core_loader_registers_without_critical_when_admission_installs(
    monkeypatch, caplog, load_timeout,
):
    _pin_core_load_timeout(monkeypatch, load_timeout)
    monkeypatch.setattr(tfi, "install_trusted_feishu_ingress_admission", lambda: None)
    monkeypatch.setattr(mt, "_register", lambda ctx: pe._install_trusted_feishu_ingress())
    manager, manifest = _multitenancy_manager(monkeypatch)
    caplog.set_level(logging.INFO)

    manager._load_plugin(manifest)

    plugin = manager._plugins["multitenancy"]
    assert plugin.enabled and not plugin.error
    assert _mt_critical_lines(caplog) == []


def _gateway_guard(monkeypatch, manager, manifest):
    """Drive ``startup_guard gateway`` against a real core loader, never past the guard."""
    import hermes_cli.plugins as core_plugins

    monkeypatch.setattr(startup_guard, "validate_startup", lambda: None)
    monkeypatch.setattr(core_plugins, "discover_plugins", lambda: manager._load_plugin(manifest))
    monkeypatch.setattr(core_plugins, "get_plugin_manager", lambda: manager)

    def unreachable(*args, **kwargs):
        pytest.fail("gateway started although multitenancy did not register")

    monkeypatch.setattr(startup_guard, "wait_run_broker", unreachable)
    return startup_guard.main(["gateway"])


@pytest.mark.parametrize("load_timeout", [0, 10], ids=["inline", "deadline"])
def test_gateway_startup_exits_nonzero_when_admission_is_missing(monkeypatch, capsys, load_timeout):
    _pin_core_load_timeout(monkeypatch, load_timeout)
    threads = _failing_admission(monkeypatch)
    manager, manifest = _multitenancy_manager(monkeypatch)

    assert _gateway_guard(monkeypatch, manager, manifest) == 1

    _assert_load_mode(threads, load_timeout)
    assert "multitenancy startup guard failed: RequiredRegistrationFailed" in capsys.readouterr().err


@pytest.mark.parametrize("load_timeout", [0, 10], ids=["inline", "deadline"])
def test_gateway_guard_refuses_a_failure_the_loader_recorded(monkeypatch, capsys, load_timeout):
    """Second layer: a register() failure the core loader swallows still stops the gateway."""
    _pin_core_load_timeout(monkeypatch, load_timeout)
    manager, manifest = _multitenancy_manager(monkeypatch)

    def exits(ctx):
        raise SystemExit(1)

    monkeypatch.setattr(mt, "register", exits)

    assert _gateway_guard(monkeypatch, manager, manifest) == 1

    err = capsys.readouterr().err
    plugin = manager._plugins.get("multitenancy")
    if plugin is None:  # core propagates SystemExit itself
        assert "multitenancy startup guard failed: SystemExit" in err
    else:
        assert not plugin.enabled
        assert "SystemExit(1)" in plugin.error
        assert "multitenancy startup guard failed: StartupGuardError" in err
