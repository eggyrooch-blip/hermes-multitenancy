"""Real ``discover_plugins()`` → MT ``register()`` → run-broker start point.

Every other compat test drives MT with fake plugin contexts, which is how core
0.21.5 renaming the bundled Feishu platform module
(``hermes_plugins.feishu_platform.adapter`` → ``hermes_plugins.platforms__feishu.adapter``)
slipped through and killed the router at startup (local UAT 2026-10-07).

This test runs the REAL core plugin discovery against a throwaway router
HERMES_HOME (minimal config.yaml, no secrets, no ``.env``) in a subprocess, so
the class patches MT installs never leak into this pytest process. The run
broker, credential-renewal subsystem and push-card sweeps are replaced by call
recorders BEFORE discovery: nothing listens on a port, nothing talks to Feishu.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_DRIVER = textwrap.dedent(
    r"""
    import json
    import logging
    import sys
    import traceback

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            if record.levelno < logging.WARNING:
                return
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + "".join(traceback.format_exception(*record.exc_info))
            records.append(text)

    logging.getLogger().addHandler(_Capture())
    logging.getLogger().setLevel(logging.INFO)

    calls = []

    import hermes_multitenancy as mt
    from hermes_multitenancy import push_card_workers, webui_broker_server

    webui_broker_server.ensure_run_broker_server_started = (
        lambda *a, **k: calls.append("run_broker")
    )
    mt._start_credential_renewal_subsystem = (
        lambda *a, **k: calls.append("credential_renewal")
    )
    push_card_workers.ensure_push_card_sweeps_started = (
        lambda *a, **k: calls.append("push_card_sweeps")
    )

    from hermes_cli.plugins import discover_plugins, get_plugin_manager

    discover_plugins()

    plugin_errors = {}
    for key, loaded in dict(getattr(get_plugin_manager(), "_plugins", {})).items():
        error = getattr(loaded, "error", None)
        if error:
            plugin_errors[str(key)] = str(error)

    from gateway.platform_registry import platform_registry
    from hermes_multitenancy.feishu_adapter_compat import (
        load_feishu_module,
        load_live_feishu_module,
    )
    from hermes_multitenancy.group_inviter_hook import _CLASS_PATCH_FLAG

    entry = platform_registry.get("feishu")
    registry_cls = getattr(entry, "adapter_factory", None)
    resolve_errors = []

    def _resolve(fn):
        try:
            return fn()
        except Exception as exc:
            resolve_errors.append(f"{fn.__name__}: {type(exc).__name__}: {exc}")
            return None

    live = _resolve(load_live_feishu_module)
    compat = _resolve(load_feishu_module)
    hook = getattr(registry_cls, "_on_bot_added_to_chat", None)
    send_retry = getattr(registry_cls, "_feishu_send_with_retry", None)

    print(
        "@@RESULT@@"
        + json.dumps(
            {
                "calls": calls,
                "plugin_errors": plugin_errors,
                "registry_cls_module": getattr(registry_cls, "__module__", None),
                "resolve_errors": resolve_errors,
                "live_module": getattr(live, "__name__", None),
                "live_is_registry_cls": getattr(live, "FeishuAdapter", None) is registry_cls,
                "compat_module": getattr(compat, "__name__", None),
                "compat_is_registry_cls": getattr(compat, "FeishuAdapter", None) is registry_cls,
                "inviter_hook_on_live_cls": bool(getattr(hook, _CLASS_PATCH_FLAG, False)),
                "send_retry_patch_on_live_cls": bool(
                    getattr(send_retry, "_hermes_multitenancy_send_retry_fatal_patched", False)
                ),
                "synthetic_adapter_modules": sorted(
                    name
                    for name in sys.modules
                    if name.startswith("hermes_plugins.") and name.endswith(".adapter")
                ),
                "warnings": records,
            }
        )
    )
    """
)

_KNOWN_FEISHU_PLUGIN_MODULES = {
    "hermes_plugins.platforms__feishu.adapter",  # core >= 0.21.5
    "hermes_plugins.feishu_platform.adapter",  # core <= 0.21.4
}


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    hermes_home = home / ".hermes" / "profiles" / "multitenancy_router"
    hermes_home.mkdir(parents=True)
    # Same shape as the router config (plugins block), minus everything secret.
    # load_timeout_seconds: 0 mirrors the router config the 0.21.5 rollout sets
    # (core <= 0.21.4 ignores the key).
    (hermes_home / "config.yaml").write_text(
        "plugins:\n"
        "  enabled:\n"
        "    - multitenancy\n"
        "  load_timeout_seconds: 0\n",
        encoding="utf-8",
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HERMES_", "FEISHU_", "LARK", "OPENAI_", "ANTHROPIC_"))
    }
    env.update(
        {
            "HOME": str(home),
            "HERMES_HOME": str(hermes_home),
            "PYTHONPATH": os.pathsep.join(
                [str(REPO_ROOT), *filter(None, [os.environ.get("PYTHONPATH")])]
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


def test_real_discovery_registers_mt_through_run_broker_start(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER],
        cwd=tmp_path,
        env=_isolated_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = proc.stdout + "\n" + proc.stderr
    assert proc.returncode == 0, output
    marker = [line for line in proc.stdout.splitlines() if line.startswith("@@RESULT@@")]
    assert marker, output
    result = json.loads(marker[-1][len("@@RESULT@@"):])
    dump = json.dumps(result, indent=1, ensure_ascii=False)

    warnings = "\n".join(result["warnings"])
    assert not result["resolve_errors"], dump
    assert "StartupGuardError" not in output + warnings, warnings
    assert "did not materialize" not in output + warnings, warnings
    assert not {k: v for k, v in result["plugin_errors"].items() if "multitenancy" in k}, dump

    # register() ran to the end of the router branch: broker start point reached.
    assert result["calls"][-2:] == ["run_broker", "credential_renewal"], dump

    # Every MT resolution path lands on the class the gateway will instantiate.
    assert result["registry_cls_module"] in _KNOWN_FEISHU_PLUGIN_MODULES, dump
    assert result["live_module"] == result["registry_cls_module"], dump
    assert result["live_is_registry_cls"] is True, dump
    assert result["compat_module"] == result["registry_cls_module"], dump
    assert result["compat_is_registry_cls"] is True, dump
    # group_inviter_hook and cron/patches resolve the adapter by the same
    # compat path, so their class patches land on the live class.
    assert result["inviter_hook_on_live_cls"] is True, dump
    assert result["send_retry_patch_on_live_cls"] is True, dump
