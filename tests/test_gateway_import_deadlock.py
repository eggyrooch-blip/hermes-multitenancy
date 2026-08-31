import sys
import threading
import time
import types


def test_gateway_runner_patch_waits_for_partially_initialized_module(monkeypatch):
    from hermes_multitenancy.gateway_deferred import install_when_gateway_runner_ready

    partial = types.ModuleType("gateway.run")
    monkeypatch.setitem(sys.modules, "gateway.run", partial)
    seen = []

    assert install_when_gateway_runner_ready("test-patch", seen.append) is False
    assert seen == []

    runner = type("GatewayRunner", (), {})
    partial.GatewayRunner = runner

    deadline = time.monotonic() + 2
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)

    assert seen == [runner]
    assert install_when_gateway_runner_ready("test-patch", seen.append) is True
    assert seen == [runner]


def test_deferred_installer_never_imports_gateway_run(monkeypatch):
    from hermes_multitenancy import gateway_deferred

    monkeypatch.delitem(sys.modules, "gateway.run", raising=False)
    monkeypatch.setattr(
        "importlib.import_module",
        lambda name: (_ for _ in ()).throw(AssertionError(f"unexpected import: {name}")),
    )

    assert gateway_deferred.install_when_gateway_runner_ready(
        "no-import-test-patch", lambda _runner: None
    ) is False


def test_concurrent_ready_checks_install_a_patch_once(monkeypatch):
    from hermes_multitenancy.gateway_deferred import install_when_gateway_runner_ready

    module = types.ModuleType("gateway.run")
    module.GatewayRunner = type("GatewayRunner", (), {})
    monkeypatch.setitem(sys.modules, "gateway.run", module)
    seen = []
    entered = threading.Event()
    release = threading.Event()

    def install(runner):
        seen.append(runner)
        entered.set()
        release.wait(timeout=2)

    first = threading.Thread(
        target=install_when_gateway_runner_ready,
        args=("concurrent-test-patch", install),
    )
    first.start()
    assert entered.wait(timeout=2)
    threads = [threading.Thread(
        target=install_when_gateway_runner_ready,
        args=("concurrent-test-patch", install),
    ) for _ in range(19)]
    for thread in threads:
        thread.start()
    release.set()
    first.join()
    for thread in threads:
        thread.join()

    assert seen == [module.GatewayRunner]


def test_required_deferred_failure_aborts_gateway_startup(monkeypatch):
    from hermes_multitenancy import gateway_deferred

    module = types.ModuleType("gateway.run")
    module.GatewayRunner = type("GatewayRunner", (), {})
    monkeypatch.setitem(sys.modules, "gateway.run", module)
    aborted = []
    monkeypatch.setattr(gateway_deferred, "_abort_gateway_startup", lambda: aborted.append(True))

    def fail(_runner):
        raise RuntimeError("required boundary failed")

    assert gateway_deferred.install_when_gateway_runner_ready(
        "required-failure-test", fail, required=True
    ) is False
    deadline = time.monotonic() + 2
    while not aborted and time.monotonic() < deadline:
        time.sleep(0.01)

    assert aborted == [True]
