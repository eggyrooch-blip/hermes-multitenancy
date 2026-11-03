"""Gateway startup must not deadlock on MT register (production, core 0.21.4, 2026-09-24).

Production timing, reproduced with a synthetic ``gateway.run`` and discovery lock
so it does not depend on disk speed or on the core build CI installs:

* the ``plugin-discovery`` thread holds the discovery lock and calls MT ``register``;
* the main thread is inside ``gateway.run``'s module body, waiting for that lock
  (core: ``_bridge_auxiliary_config_to_env`` -> ``get_plugin_auxiliary_tasks``).

Before the fix, register imported ``gateway.run`` and blocked on its module lock
forever; the child process hit the 20s timeout.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CHILD_TIMEOUT_SECONDS = 20
FIXED_BUDGET_SECONDS = 5

_PROBE = """
import threading
discovery_lock = threading.RLock()
discovery_holding = threading.Event()
body_entered = threading.Event()
events = []
"""

# Stands in for core's gateway/run.py. Only the module-level shape matters:
# the body waits for plugin discovery before GatewayRunner is defined.
_FAKE_RUN = """
import os
import _deadlock_probe as probe

probe.events.append("run_body_started")
probe.body_entered.set()
with probe.discovery_lock:
    pass
probe.events.append("run_body_resumed")


def _cron_tick_profile_homes(config):
    return [("peer", "/elsewhere")]


class GatewayRunner:
    def __init__(self, config=None):
        self.config = config
        self.adapters = {}

    def _create_adapter(self, platform, config):
        return None

    async def _handle_message(self, event):
        return None

    async def _handle_active_session_busy_message(self, event):
        return False


if os.environ.get("FAKE_GATEWAY_RUN_BROKEN") == "1":
    del GatewayRunner._handle_message
"""

_CHILD = r"""
import faulthandler, json, logging, os, sys, threading, time
faulthandler.dump_traceback_later(15)  # thread stacks in the failure message if it hangs
from types import SimpleNamespace

repo, fake_dir, mode = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, fake_dir)
sys.path.insert(0, repo)

critical = []

class _Capture(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.CRITICAL:
            critical.append(record.getMessage())

logging.getLogger().addHandler(_Capture())
logging.getLogger().setLevel(logging.INFO)

import _deadlock_probe as probe
import gateway  # the real core package; only gateway.run is synthetic
assert "gateway.run" not in sys.modules, "core imported gateway.run before the probe"
gateway.__path__.insert(0, os.path.join(fake_dir, "gateway_shadow"))

import hermes_multitenancy
from hermes_multitenancy import trusted_feishu_ingress

# Real Feishu admission needs a live core Feishu adapter; it never touches gateway.run.
trusted_feishu_ingress.install_trusted_feishu_ingress_admission = lambda: None

register_calls = []
_real_register = hermes_multitenancy._register

def _counting_register(ctx):
    register_calls.append(threading.current_thread().name)
    return _real_register(ctx)

hermes_multitenancy._register = _counting_register

class Ctx:
    _manager = object()
    def __init__(self):
        self.hooks = []
    def register_hook(self, name, cb):
        self.hooks.append(name)

ctx = Ctx()
register_error = []

def discovery():
    # core: PluginManager.discover_and_load holds _discovery_lock while plugins register.
    with probe.discovery_lock:
        probe.discovery_holding.set()
        if mode != "loaded":
            probe.body_entered.wait(10)
        try:
            hermes_multitenancy.register(ctx)
        except BaseException as exc:
            register_error.append(type(exc).__name__)

if mode == "loaded":
    import gateway.run  # legacy timing: fully loaded before register
    worker = threading.Thread(target=discovery, name="plugin-discovery", daemon=True)
    worker.start()
    worker.join(10)
else:
    worker = threading.Thread(target=discovery, name="plugin-discovery", daemon=True)
    worker.start()
    probe.discovery_holding.wait(10)
    try:
        import gateway.run
    except BaseException as exc:
        worker.join(10)
        from hermes_multitenancy import gateway_run_ready
        print(json.dumps({
            "import_error": type(exc).__name__,
            "import_message": str(exc),
            "gateway_run_in_sys_modules": "gateway.run" in sys.modules,
            "register_error": register_error,
            "pending": gateway_run_ready.pending_keys(),
        }))
        sys.exit(3)
    worker.join(10)

from gateway import run as gateway_run
Runner = gateway_run.GatewayRunner

def flag(fn, name):
    return bool(getattr(fn, name, False))

create_adapter = Runner._create_adapter
markers = {
    "handle_message": flag(Runner._handle_message, "_hermes_multitenancy_internal_guard"),
    "init": flag(Runner.__init__, "_hermes_multitenancy_ownership_patched"),
    "create_adapter_ownership": flag(create_adapter, "_hermes_multitenancy_ownership_patched"),
    "create_adapter_cron_watcher": False,
    "create_adapter_push_capture": False,
    "cron_scope": flag(gateway_run._cron_tick_profile_homes, "_hermes_multitenancy_profile_guard"),
}
# The three _create_adapter layers wrap each other via functools.wraps.
layer = create_adapter
while layer is not None:
    markers["create_adapter_cron_watcher"] |= flag(layer, "_hermes_multitenancy_patched")
    markers["create_adapter_push_capture"] |= flag(layer, "_hermes_push_card_capture_patched")
    layer = getattr(layer, "__wrapped__", None)

# First GatewayRunner built after the import: the patched __init__ must run.
config = SimpleNamespace(platforms={}, multiplex_profiles=True)
runner = Runner(config)

# Router startup hook path: every _create_adapter schedules the broker again.
from hermes_multitenancy import webui_broker_server
for _ in range(3):
    runner._create_adapter("api_server", SimpleNamespace(enabled=True))
webui_broker_server.ensure_run_broker_server_started()

from urllib.request import Request, urlopen
port = os.environ["HERMES_MULTITENANCY_RUN_BROKER_PORT"]
with urlopen(Request(f"http://127.0.0.1:{port}/api/run-broker/health",
                     headers={"Authorization": "Bearer deadlock-probe"}), timeout=5) as response:
    health = json.loads(response.read())

mt_copies = sorted(name for name in sys.modules if name.endswith(".gateway_ownership"))
print(json.dumps({
    "register_calls": register_calls,
    "register_error": register_error,
    "hooks": ctx.hooks,
    "markers": markers,
    "multiplex_after_init": config.multiplex_profiles,
    "cron_scope": gateway_run._cron_tick_profile_homes(None),
    "broker_threads": sum(1 for t in threading.enumerate() if t.name == "multitenancy-run-broker"),
    "broker_health": health,
    "critical": critical,
    "mt_copies": mt_copies,
    "events": probe.events,
    "gateway_class": type(gateway).__name__,
}, default=str))
sys.stdout.flush()
os._exit(0)
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _run_child(tmp_path: Path, mode: str, *, broken: bool = False) -> tuple[subprocess.CompletedProcess, float]:
    fake = tmp_path / "fake"
    (fake / "gateway_shadow").mkdir(parents=True)
    (fake / "_deadlock_probe.py").write_text(textwrap.dedent(_PROBE))
    (fake / "gateway_shadow" / "run.py").write_text(textwrap.dedent(_FAKE_RUN))
    (fake / "child.py").write_text(_CHILD)
    home = tmp_path / "hermes" / "profiles" / "multitenancy_router"
    home.mkdir(parents=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HERMES_", "FEISHU_", "PYTEST_"))
    }
    env.update(
        {
            "HERMES_HOME": str(home),
            "HERMES_MULTITENANCY_RUN_BROKER_SERVER": "1",
            "HERMES_MULTITENANCY_RUN_BROKER_KEY": "deadlock-probe",
            "HERMES_MULTITENANCY_RUN_BROKER_PORT": str(_free_port()),
        }
    )
    if broken:
        env["FAKE_GATEWAY_RUN_BROKEN"] = "1"
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, str(fake / "child.py"), str(REPO), str(fake), mode],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=CHILD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"gateway startup deadlocked: child did not exit within {CHILD_TIMEOUT_SECONDS}s "
            f"(mode={mode})\nstderr tail:\n{(exc.stderr or b'')[-3000:].decode(errors='replace')}"
        )
    return result, time.monotonic() - started


def _report(result: subprocess.CompletedProcess) -> dict:
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert lines, f"no report; rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr[-4000:]}"
    return json.loads(lines[-1])


def _assert_patched_single_registration(report: dict) -> None:
    assert report["register_error"] == []
    assert report["register_calls"] == ["plugin-discovery"], "MT must register exactly once"
    assert report["mt_copies"] == ["hermes_multitenancy.gateway_ownership"], "MT loaded twice"
    assert "pre_gateway_dispatch" in report["hooks"]
    assert report["markers"] == {
        "handle_message": True,
        "init": True,
        "create_adapter_ownership": True,
        "create_adapter_cron_watcher": True,
        "create_adapter_push_capture": True,
        "cron_scope": True,
    }
    # The patched __init__ ran for the first runner built after the import.
    assert report["multiplex_after_init"] is False
    assert report["cron_scope"][0][0] == "multitenancy_router"
    # Run Broker: bound once despite register + three startup-hook calls.
    assert report["broker_threads"] == 1
    assert report["broker_health"] == {"ok": True, "service": "hermes-multitenancy-run-broker"}
    assert not [m for m in report["critical"] if "run broker" in m.lower()], report["critical"]
    # The package hook is removed once the patches ran.
    assert report["gateway_class"] == "module"


@pytest.fixture(scope="module")
def warm_bytecode(tmp_path_factory):
    """One untimed child first, so the timed ones measure startup, not a cold
    compile of MT + core (a fresh CI venv has no .pyc; cold imports alone take
    several seconds). The 20s hang detector still applies to this run."""
    _run_child(tmp_path_factory.mktemp("warm"), "loaded")


def test_register_during_gateway_run_import_does_not_deadlock(tmp_path, warm_bytecode):
    result, elapsed = _run_child(tmp_path, "deadlock")
    assert result.returncode == 0, result.stderr[-4000:]
    assert elapsed < FIXED_BUDGET_SECONDS, f"startup took {elapsed:.1f}s"
    report = _report(result)
    _assert_patched_single_registration(report)
    assert report["events"] == ["run_body_started", "run_body_resumed"]


def test_register_after_gateway_run_loaded_patches_synchronously(tmp_path, warm_bytecode):
    result, elapsed = _run_child(tmp_path, "loaded")
    assert result.returncode == 0, result.stderr[-4000:]
    assert elapsed < FIXED_BUDGET_SECONDS, f"startup took {elapsed:.1f}s"
    _assert_patched_single_registration(_report(result))


def test_deferred_patch_failure_fails_gateway_run_import(tmp_path):
    result, _elapsed = _run_child(tmp_path, "deadlock", broken=True)
    assert result.returncode == 3, result.stdout + result.stderr[-4000:]
    report = _report(result)
    assert report["import_error"] == "GatewayRunPatchError"
    assert "gateway_ownership_guard" in report["import_message"]
    # The unpatched module must not stay importable (a retry must not fail open).
    assert report["gateway_run_in_sys_modules"] is False
    assert report["register_error"] == []
    assert "gateway_ownership_guard" in report["pending"]


def test_gateway_run_ready_three_states(monkeypatch):
    import types

    from hermes_multitenancy import gateway_run_ready as ready

    calls = []
    package = types.ModuleType("gateway")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "gateway", package)
    monkeypatch.setattr(ready, "_pending", [])
    monkeypatch.setattr(ready, "_hook_class", None)
    monkeypatch.setattr(ready, "_original_class", None)

    # loaded: runs now
    loaded = types.ModuleType("gateway.run")
    monkeypatch.setitem(sys.modules, "gateway.run", loaded)
    assert ready.when_gateway_run_loaded("a", lambda m: calls.append(("a", m))) is True
    assert calls == [("a", loaded)]

    # initializing: deferred until the import system binds gateway.run on its parent
    initializing = types.ModuleType("gateway.run")
    initializing.__spec__ = types.SimpleNamespace(_initializing=True)
    monkeypatch.setitem(sys.modules, "gateway.run", initializing)
    assert ready.when_gateway_run_loaded("b", lambda m: calls.append(("b", m))) is False
    assert ready.when_gateway_run_loaded("b", lambda m: calls.append(("dup", m))) is False
    assert ready.pending_keys() == ["b"]
    package.run = initializing  # bind while still initializing: must not fire
    assert ready.pending_keys() == ["b"]
    initializing.__spec__._initializing = False
    package.run = initializing
    assert calls[-1] == ("b", initializing)
    assert ready.pending_keys() == []
    assert type(package) is types.ModuleType

    # absent but importable: deferred until its first import completes
    monkeypatch.delitem(sys.modules, "gateway.run")
    monkeypatch.setattr(ready.importlib.util, "find_spec", lambda name: object())
    assert ready.when_gateway_run_loaded("e", lambda m: calls.append(("e", m))) is False
    fresh = types.ModuleType("gateway.run")
    monkeypatch.setitem(sys.modules, "gateway.run", fresh)
    package.run = fresh
    assert calls[-1] == ("e", fresh)
    assert type(package) is types.ModuleType

    # absent and not importable: fail closed like the old synchronous import
    monkeypatch.delitem(sys.modules, "gateway.run")
    monkeypatch.setattr(ready.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(ModuleNotFoundError):
        ready.when_gateway_run_loaded("c", lambda m: None)

    # blocked (None in sys.modules): fail closed
    monkeypatch.setitem(sys.modules, "gateway.run", None)
    with pytest.raises(ModuleNotFoundError):
        ready.when_gateway_run_loaded("d", lambda m: None)


# --- gateway_run_ready publication barrier / retry (codex review round 1) -------
# Fully synthetic ``gateway`` package: these only exercise the helper against the
# real import system, so they need no core build at all.

_BARRIER_CHILD = r"""
import json, os, sys, threading, time
repo, pkg_root, mode = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, pkg_root)
sys.path.insert(0, repo)
import gateway  # synthetic package
from hermes_multitenancy import gateway_run_ready as ready

in_callback = threading.Event()
release = threading.Event()
attempts = {"own": 0, "watcher": 0}

def own(module):
    attempts["own"] += 1
    in_callback.set()
    release.wait(10)
    if mode == "fail_closed":
        raise RuntimeError("ownership patch failed")
    module.GatewayRunner.guarded = True

def watcher(module):
    attempts["watcher"] += 1
    if mode == "retry" and attempts["watcher"] == 1:
        raise RuntimeError("watcher failed once")
    module.GatewayRunner.watched = True

ready.when_gateway_run_loaded("own", own)
if mode == "retry":
    ready.when_gateway_run_loaded("watcher", watcher)
    release.set()
    first = None
    try:
        import gateway.run
    except ready.GatewayRunPatchError as exc:
        first = str(exc)
    in_sys = "gateway.run" in sys.modules
    pending_after_fail = ready.pending_keys()
    import gateway.run as retried
    print(json.dumps({
        "first": first, "in_sys_after_fail": in_sys, "pending_after_fail": pending_after_fail,
        "guarded": getattr(retried.GatewayRunner, "guarded", False),
        "watched": getattr(retried.GatewayRunner, "watched", False),
        "attempts": attempts, "pending": ready.pending_keys(),
    }))
    sys.exit(0)

results = {}

def importer(name):
    try:
        from gateway.run import GatewayRunner
        results[name] = {"guarded": getattr(GatewayRunner, "guarded", False)}
    except BaseException as exc:
        results[name] = {"error": type(exc).__name__}

a = threading.Thread(target=importer, args=("a",)); a.start()
in_callback.wait(10)
b = threading.Thread(target=importer, args=("b",)); b.start()
b.join(0.5)
b_waited = b.is_alive()
release.set()
a.join(10); b.join(10)
print(json.dumps({"b_waited": b_waited, "results": results, "attempts": attempts}))
"""


def _run_barrier_child(tmp_path: Path, mode: str) -> dict:
    pkg = tmp_path / "pkgroot" / "gateway"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "run.py").write_text("class GatewayRunner:\n    pass\n")
    child = tmp_path / "barrier_child.py"
    child.write_text(_BARRIER_CHILD)
    result = subprocess.run(
        [sys.executable, str(child), str(REPO), str(tmp_path / "pkgroot"), mode],
        cwd=tmp_path, capture_output=True, text=True, timeout=CHILD_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, result.stdout + result.stderr[-4000:]
    return _report(result)


def test_concurrent_importer_waits_until_deferred_patches_ran(tmp_path):
    report = _run_barrier_child(tmp_path, "barrier")
    assert report["b_waited"] is True, "second importer got gateway.run before the patches finished"
    assert report["results"] == {"a": {"guarded": True}, "b": {"guarded": True}}
    assert report["attempts"]["own"] == 1


def test_concurrent_importer_stays_fail_closed_when_patch_fails(tmp_path):
    report = _run_barrier_child(tmp_path, "fail_closed")
    assert report["b_waited"] is True
    # Neither importer may obtain a usable (unpatched) runner.
    assert report["results"] == {"a": {"error": "GatewayRunPatchError"}, "b": {"error": "GatewayRunPatchError"}}


def test_failed_batch_retry_reapplies_every_patch(tmp_path):
    report = _run_barrier_child(tmp_path, "retry")
    assert report["first"] == "gateway_run_patch_failed:watcher"
    assert report["in_sys_after_fail"] is False
    assert report["pending_after_fail"] == ["own", "watcher"]
    assert report["guarded"] is True and report["watched"] is True
    assert report["attempts"] == {"own": 2, "watcher": 2}
    assert report["pending"] == []
