"""Regression: v0190 lazy platform plugins split the FeishuAdapter class in two.

prod 2026-07-25 → 07-30 (5 days, silent): v0190 made bundled ``kind: platform``
plugins LAZY (``hermes_cli/plugins.py`` → ``_register_deferred_platform``), so at
multitenancy ``register()`` time the synthetic module
``hermes_plugins.feishu_platform.adapter`` does not exist yet.
``load_feishu_module()``'s sys.modules lookup missed, the legacy fallback
imported the SAME source file a second time as ``plugins.platforms.feishu.adapter``,
and all 15 class-level patches landed on that clone. The gateway then
materialized the synthetic module and instantiated a fresh, unpatched class.

Fix under test: on a synthetic miss, ask the registry to materialize the
deferred platform (``platform_registry.get("feishu")``) and re-check sys.modules
before falling back.

The fake registry below mirrors the real
``gateway/platform_registry.py::PlatformRegistry`` from the prod release
``hermes-agent-v0190-20260725T003243`` (read 2026-07-30):

* ``register_deferred(name, loader)`` stores a zero-arg loader.
* ``get(name)`` → ``if name not in self._entries: self._resolve(name)`` then a
  plain dict lookup; ``_resolve`` POPS the loader and runs it inside
  ``try/except Exception`` (warning only) — so ``get()`` never raises.
* ``check_fn`` / ``validate_config`` are only ever called from
  ``create_adapter()``, NEVER from ``get()`` — which is why calling this from a
  non-gateway context (cron worker subprocess, CLI) with no ``FEISHU_APP_ID``
  cannot fail. ``test_registry_get_never_consults_check_fn`` nails that.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

from hermes_multitenancy import feishu_adapter_compat as compat

SYNTHETIC = "hermes_plugins.feishu_platform.adapter"
CLONE = "plugins.platforms.feishu.adapter"
LEGACY = "gateway.platforms.feishu"


class _FakePlatformRegistry:
    """Mini-model of the prod ``PlatformRegistry`` (deferred-loader semantics)."""

    def __init__(self) -> None:
        self._entries: dict[str, object] = {}
        self._deferred: dict[str, object] = {}
        self.get_calls: list[str] = []

    def register_deferred(self, name, loader) -> None:
        self._deferred[name] = loader

    def register(self, name, entry) -> None:
        self._deferred.pop(name, None)
        self._entries[name] = entry

    def _resolve(self, name) -> None:
        loader = self._deferred.pop(name, None)
        if loader is None:
            return
        try:
            loader()
        except Exception:  # prod logs a warning and swallows
            pass

    def get(self, name):
        self.get_calls.append(name)
        if name not in self._entries:
            self._resolve(name)
        return self._entries.get(name)


def _synthetic_module() -> types.ModuleType:
    module = types.ModuleType(SYNTHETIC)
    module.FeishuAdapter = type("FeishuAdapter", (), {})  # type: ignore[attr-defined]
    return module


@pytest.fixture(autouse=True)
def _clean_feishu_modules():
    """Snapshot/restore every module name this file plants into sys.modules."""
    names = (SYNTHETIC, CLONE, LEGACY, "gateway", "gateway.platform_registry")
    saved = {name: sys.modules.get(name) for name in names}
    for name in names:
        sys.modules.pop(name, None)
    yield
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


@pytest.fixture
def fake_registry() -> _FakePlatformRegistry:
    registry = _FakePlatformRegistry()
    gateway = types.ModuleType("gateway")
    registry_module = types.ModuleType("gateway.platform_registry")
    registry_module.platform_registry = registry  # type: ignore[attr-defined]
    gateway.platform_registry = registry_module  # type: ignore[attr-defined]
    sys.modules["gateway"] = gateway
    sys.modules["gateway.platform_registry"] = registry_module
    return registry


def _forbid_fallback(monkeypatch) -> None:
    def _never(name: str):
        raise AssertionError(f"fallback import_module({name!r}) must not run")

    monkeypatch.setattr(compat, "import_module", _never)


def test_registry_materializes_deferred_synthetic_module(monkeypatch, fake_registry) -> None:
    """Synthetic absent + a pending deferred loader → registry materializes it,
    the synthetic module wins, and the clone is never imported (one exec only)."""
    synthetic = _synthetic_module()

    def loader() -> None:
        # What ``spec_from_file_location`` + ``exec_module`` do in the real loader.
        sys.modules[SYNTHETIC] = synthetic
        fake_registry.register("feishu", types.SimpleNamespace(name="feishu"))

    fake_registry.register_deferred("feishu", loader)
    _forbid_fallback(monkeypatch)

    module = compat.load_feishu_module()

    assert module is synthetic
    assert module.__name__ == SYNTHETIC
    assert CLONE not in sys.modules  # the duplicate exec is gone
    assert fake_registry.get_calls == ["feishu"]
    assert compat.load_feishu_adapter() is synthetic.FeishuAdapter


def test_already_loaded_synthetic_never_touches_registry(monkeypatch, fake_registry) -> None:
    """Synthetic already in sys.modules → return it, no registry call at all."""
    synthetic = _synthetic_module()
    sys.modules[SYNTHETIC] = synthetic
    _forbid_fallback(monkeypatch)

    assert compat.load_feishu_module() is synthetic
    assert fake_registry.get_calls == []  # sentinel: registry untouched


def test_fail_open_when_registry_module_is_unimportable(monkeypatch) -> None:
    """No ``gateway`` package (CLI / cron subprocess) → silent fall-through to
    the legacy candidate chain, byte-for-byte the old behaviour."""
    clone = types.ModuleType(CLONE)
    seen: list[str] = []

    def import_module(name: str) -> types.ModuleType:
        seen.append(name)
        if name == LEGACY:
            raise ModuleNotFoundError(f"No module named '{name}'", name="gateway")
        if name == CLONE:
            return clone
        raise ModuleNotFoundError(f"No module named '{name}'", name=name)

    monkeypatch.setattr(compat, "import_module", import_module)

    assert compat.load_feishu_module() is clone
    assert seen == [LEGACY, CLONE]


def test_fail_open_when_registry_get_raises(monkeypatch, fake_registry) -> None:
    """A registry that explodes must not propagate — old fallback still wins."""

    def boom(name: str):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(fake_registry, "get", boom)

    clone = types.ModuleType(CLONE)
    seen: list[str] = []

    def import_module(name: str) -> types.ModuleType:
        seen.append(name)
        if name == LEGACY:
            raise ModuleNotFoundError(f"No module named '{name}'", name="gateway")
        if name == CLONE:
            return clone
        raise ModuleNotFoundError(f"No module named '{name}'", name=name)

    monkeypatch.setattr(compat, "import_module", import_module)

    assert compat.load_feishu_module() is clone
    assert seen == [LEGACY, CLONE]


def test_registry_get_never_consults_check_fn(monkeypatch, fake_registry) -> None:
    """Regression nail for the non-gateway context: ``get()`` only runs the
    deferred loader. ``check_fn`` (which is what needs FEISHU_APP_ID/SECRET) is
    reached only from ``create_adapter``, so a missing env cannot raise here."""
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    def check_fn() -> bool:
        raise AssertionError("check_fn must never run during platform_registry.get()")

    synthetic = _synthetic_module()

    def loader() -> None:
        assert not os.getenv("FEISHU_APP_ID")  # loader is pure import + register
        sys.modules[SYNTHETIC] = synthetic
        fake_registry.register("feishu", types.SimpleNamespace(check_fn=check_fn))

    fake_registry.register_deferred("feishu", loader)
    _forbid_fallback(monkeypatch)

    assert compat.load_feishu_module() is synthetic


def test_registry_materialization_that_yields_nothing_falls_back(monkeypatch, fake_registry) -> None:
    """Registry answers but the synthetic module still isn't there (loader
    failed inside prod's try/except) → old fallback chain, no raise."""
    clone = types.ModuleType(CLONE)
    seen: list[str] = []

    def loader() -> None:
        raise RuntimeError("lark_oapi import blew up")

    fake_registry.register_deferred("feishu", loader)

    def import_module(name: str) -> types.ModuleType:
        seen.append(name)
        if name == LEGACY:
            raise ModuleNotFoundError(f"No module named '{name}'", name="gateway")
        if name == CLONE:
            return clone
        raise ModuleNotFoundError(f"No module named '{name}'", name=name)

    monkeypatch.setattr(compat, "import_module", import_module)

    assert compat.load_feishu_module() is clone
    assert fake_registry.get_calls == ["feishu"]
    assert seen == [LEGACY, CLONE]


def test_half_initialized_synthetic_after_materialization_falls_back(monkeypatch, fake_registry) -> None:
    """The ``FeishuAdapter`` attr guard survives the new seam: a synthetic module
    without the class must not win just because the registry produced it."""
    partial = types.ModuleType(SYNTHETIC)
    legacy = types.ModuleType(LEGACY)
    legacy.FeishuAdapter = type("FeishuAdapter", (), {})  # type: ignore[attr-defined]

    def loader() -> None:
        sys.modules[SYNTHETIC] = partial
        fake_registry.register("feishu", types.SimpleNamespace(name="feishu"))

    fake_registry.register_deferred("feishu", loader)

    def import_module(name: str) -> types.ModuleType:
        if name == LEGACY:
            return legacy
        raise ModuleNotFoundError(f"No module named '{name}'", name=name)

    monkeypatch.setattr(compat, "import_module", import_module)

    assert compat.load_feishu_module() is legacy
