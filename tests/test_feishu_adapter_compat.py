from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import sys
import types
from typing import Any

from hermes_multitenancy import cron_worker
from hermes_multitenancy import feishu_adapter_compat
from hermes_multitenancy.feishu_inbound_richtext import install_feishu_inbound_richtext_patch
import pytest


_REAL_MATERIALIZE = feishu_adapter_compat._materialize_deferred_feishu_platform


@pytest.fixture(autouse=True)
def _isolate_real_feishu_plugin(monkeypatch) -> None:
    """Keep the real core feishu plugin out of these layout tests.

    With core 0.21.4 installed, an earlier test on the same xdist worker may have
    materialized the plugin loader's synthetic module (and ``load_feishu_module``
    would materialize it on demand), so it would win over every fake layout
    below. Each test that needs a synthetic module installs its own.
    """
    for name in feishu_adapter_compat._PLUGIN_LOADER_MODULE_NAMES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(feishu_adapter_compat, "_materialize_deferred_feishu_platform", lambda: None)


@dataclass
class FakeNormalizedMessage:
    raw_type: str = ""
    text_content: str = ""
    image_keys: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    media_refs: list[Any] = field(default_factory=list)


class FakeFeishuAdapter:
    async def _send_raw_message(
        self,
        *,
        chat_id: str,
        msg_type: str,
        payload: str,
        reply_to: str | None,
        metadata: dict[str, Any] | None,
    ) -> str:
        return f"old:{chat_id}:{msg_type}:{payload}:{reply_to}:{metadata}"

    def _build_outbound_payload(self, content: str) -> tuple[str, str]:
        return "text", json.dumps({"text": content}, ensure_ascii=False)

    def _require_mention_for(self, chat_id: str) -> bool:
        return True

    def _should_accept_group_message(self, message: Any, sender_id: str, chat_id: str) -> bool:
        return False

    def _allow_group_message(self, sender_id: str, chat_id: str) -> bool:
        return True

    def _admit(self, sender: Any, message: Any) -> str:
        return "ok"

    def _on_card_action_trigger(self, data: Any) -> None:
        return None

    def _on_bot_added_to_chat(self, data: Any) -> None:
        return None

    def _build_event_handler(self) -> Any:
        return types.SimpleNamespace(_processorMap={})

    async def on_processing_complete(self, event: Any, outcome: Any) -> None:
        return None

    async def _process_inbound_message(self, *args: Any, **kwargs: Any) -> str:
        return "processed"

    async def _extract_message_content(self, message: Any, *args: Any, **kwargs: Any) -> tuple[str, str, list, list]:
        return "text", "text", [], []

    async def _fetch_message_text(self, message_id: str, *args: Any, **kwargs: Any) -> str:
        return "parent"

    async def _dispatch_inbound_event(self, event: Any, *args: Any, **kwargs: Any) -> None:
        return None

    async def _enqueue_text_event(self, event: Any, *args: Any, **kwargs: Any) -> None:
        return None


def _install_plugin_feishu_module(monkeypatch) -> types.ModuleType:
    for name in (
        "gateway.platforms.feishu",
        "plugins.platforms.feishu.adapter",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)

    fake_module = types.ModuleType("plugins.platforms.feishu.adapter")
    fake_module.FeishuAdapter = type("PluginFeishuAdapter", (FakeFeishuAdapter,), {})  # type: ignore[attr-defined]

    def normalize_feishu_message(**kwargs: Any) -> FakeNormalizedMessage:
        return FakeNormalizedMessage(raw_type=kwargs.get("message_type") or "")

    fake_module.normalize_feishu_message = normalize_feishu_message  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "plugins.platforms.feishu.adapter", fake_module)

    real_import_module = feishu_adapter_compat.import_module

    def import_module(name: str) -> types.ModuleType:
        if name == "gateway.platforms.feishu":
            raise ModuleNotFoundError(f"No module named '{name}'", name="gateway")
        if name == "plugins.platforms.feishu.adapter":
            return fake_module
        return real_import_module(name)

    monkeypatch.setattr(feishu_adapter_compat, "import_module", import_module)
    return fake_module


def test_feishu_module_prefers_legacy_gateway_adapter_layout(monkeypatch) -> None:
    legacy_module = types.ModuleType("gateway.platforms.feishu")
    plugin_module = types.ModuleType("plugins.platforms.feishu.adapter")
    seen: list[str] = []

    def import_module(name: str) -> types.ModuleType:
        seen.append(name)
        if name == "gateway.platforms.feishu":
            return legacy_module
        if name == "plugins.platforms.feishu.adapter":
            return plugin_module
        raise ModuleNotFoundError(f"No module named '{name}'", name=name)

    monkeypatch.setattr(feishu_adapter_compat, "import_module", import_module)

    assert feishu_adapter_compat.load_feishu_module() is legacy_module
    assert seen == ["gateway.platforms.feishu"]


def test_feishu_module_does_not_hide_legacy_import_errors(monkeypatch) -> None:
    plugin_module = types.ModuleType("plugins.platforms.feishu.adapter")
    seen: list[str] = []

    def import_module(name: str) -> types.ModuleType:
        seen.append(name)
        if name == "gateway.platforms.feishu":
            raise ModuleNotFoundError("No module named 'legacy_dependency'", name="legacy_dependency")
        if name == "plugins.platforms.feishu.adapter":
            return plugin_module
        raise ModuleNotFoundError(f"No module named '{name}'", name=name)

    monkeypatch.setattr(feishu_adapter_compat, "import_module", import_module)

    try:
        feishu_adapter_compat.load_feishu_module()
    except ModuleNotFoundError as exc:
        assert exc.name == "legacy_dependency"
    else:
        raise AssertionError("expected legacy import error to be raised")
    assert seen == ["gateway.platforms.feishu"]


def test_feishu_module_falls_back_on_bare_candidate_module_errors(monkeypatch) -> None:
    plugin_module = types.ModuleType("plugins.platforms.feishu.adapter")
    seen: list[str] = []

    def import_module(name: str) -> types.ModuleType:
        seen.append(name)
        if name == "gateway.platforms.feishu":
            raise ModuleNotFoundError(name)
        if name == "plugins.platforms.feishu.adapter":
            return plugin_module
        raise ModuleNotFoundError(f"No module named '{name}'", name=name)

    monkeypatch.setattr(feishu_adapter_compat, "import_module", import_module)

    assert feishu_adapter_compat.load_feishu_module() is plugin_module
    assert seen == ["gateway.platforms.feishu", "plugins.platforms.feishu.adapter"]


@pytest.mark.parametrize("synthetic_name", feishu_adapter_compat._PLUGIN_LOADER_MODULE_NAMES)
def test_feishu_module_prefers_already_loaded_synthetic_plugin_module(monkeypatch, synthetic_name) -> None:
    """Regression for the double-import trap behind 0/49 inviter captures.

    ``hermes_cli/plugins.py`` loads the bundled feishu platform plugin under
    a synthetic ``hermes_plugins.<slug>.adapter`` name (``feishu_platform`` on
    core <= 0.21.4, ``platforms__feishu`` on 0.21.5) via
    ``spec_from_file_location`` — a module object DISTINCT from what a fresh
    ``import plugins.platforms.feishu.adapter`` would create from the same
    source file. Class patches (group_inviter_hook, cron delivery patches,
    reply-quote, …) must land on the module the gateway actually runs, so an
    already-loaded candidate must win over a fresh import."""
    synthetic = types.ModuleType(synthetic_name)
    synthetic.FeishuAdapter = type("FeishuAdapter", (), {})  # type: ignore[attr-defined]
    clone = types.ModuleType("plugins.platforms.feishu.adapter")
    clone.FeishuAdapter = type("FeishuAdapter", (), {})  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, synthetic_name, synthetic)

    def import_module(name: str) -> types.ModuleType:
        if name == "plugins.platforms.feishu.adapter":
            return clone
        raise ModuleNotFoundError(f"No module named '{name}'", name=name)

    monkeypatch.setattr(feishu_adapter_compat, "import_module", import_module)

    assert feishu_adapter_compat.load_feishu_module() is synthetic
    assert feishu_adapter_compat.load_feishu_adapter() is synthetic.FeishuAdapter


@pytest.mark.parametrize("synthetic_name", feishu_adapter_compat._PLUGIN_LOADER_MODULE_NAMES)
def test_feishu_module_skips_loaded_module_without_adapter_class(monkeypatch, synthetic_name) -> None:
    """A half-initialized (or unrelated) module under a candidate name must
    not win the sys.modules preference; resolution falls through to import."""
    partial = types.ModuleType(synthetic_name)
    monkeypatch.setitem(sys.modules, synthetic_name, partial)
    legacy = types.ModuleType("gateway.platforms.feishu")
    legacy.FeishuAdapter = type("FeishuAdapter", (), {})  # type: ignore[attr-defined]

    def import_module(name: str) -> types.ModuleType:
        if name == "gateway.platforms.feishu":
            return legacy
        raise ModuleNotFoundError(f"No module named '{name}'", name=name)

    monkeypatch.setattr(feishu_adapter_compat, "import_module", import_module)

    assert feishu_adapter_compat.load_feishu_module() is legacy


def _install_fake_platform_registry(monkeypatch, entry_factory):
    """A ``gateway.platform_registry`` whose ``get('feishu')`` runs *entry_factory*."""
    calls: list[str] = []

    class _Registry:
        def get(self, name: str):
            calls.append(name)
            return entry_factory()

    gateway = types.ModuleType("gateway")
    registry_module = types.ModuleType("gateway.platform_registry")
    registry_module.platform_registry = _Registry()  # type: ignore[attr-defined]
    gateway.platform_registry = registry_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.platform_registry", registry_module)
    return calls


@pytest.mark.parametrize("synthetic_name", feishu_adapter_compat._PLUGIN_LOADER_MODULE_NAMES)
def test_live_feishu_module_materializes_under_either_core_name(monkeypatch, synthetic_name) -> None:
    """core 0.21.5 renamed the synthetic module (``feishu_platform`` →
    ``platforms__feishu``); a miss made ``load_live_feishu_module`` fail closed
    and the router exit at startup (local UAT 2026-10-07)."""
    synthetic = types.ModuleType(synthetic_name)
    synthetic.FeishuAdapter = type("FeishuAdapter", (), {"__module__": synthetic_name})  # type: ignore[attr-defined]

    def materialize():
        monkeypatch.setitem(sys.modules, synthetic_name, synthetic)
        return types.SimpleNamespace(adapter_factory=synthetic.FeishuAdapter)

    calls = _install_fake_platform_registry(monkeypatch, materialize)

    assert feishu_adapter_compat.load_live_feishu_module() is synthetic
    assert calls == ["feishu"]


def test_live_feishu_module_follows_registry_entry_for_unlisted_synthetic_name(monkeypatch) -> None:
    """A per-home scope suffix (``platforms__feishu__home_<digest>``) is not in
    the name list; the registered entry's adapter class still names the live
    module, so resolution follows it instead of failing closed."""
    name = "hermes_plugins.platforms__feishu__home_0123456789ab.adapter"
    synthetic = types.ModuleType(name)
    synthetic.FeishuAdapter = type("FeishuAdapter", (), {"__module__": name})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, synthetic)
    _install_fake_platform_registry(
        monkeypatch, lambda: types.SimpleNamespace(adapter_factory=synthetic.FeishuAdapter)
    )

    assert feishu_adapter_compat.load_live_feishu_module() is synthetic


@pytest.mark.parametrize("bare_name", feishu_adapter_compat._PLUGIN_LOADER_MODULE_NAMES)
def test_scoped_registry_entry_beats_bare_module_from_another_home(monkeypatch, bare_name) -> None:
    """Review P1 (loaded-name-preempts-scoped-registry): core 0.21.5 scopes
    platform entries per HERMES_HOME; the first home's plugin keeps the bare
    module name, later homes get ``__home_<digest>``. With a tenant home's bare
    module already loaded, the router home's registry entry (suffixed module)
    must win — else every class patch lands on the tenant class."""
    tenant = types.ModuleType(bare_name)
    tenant.FeishuAdapter = type("FeishuAdapter", (), {"__module__": bare_name})  # type: ignore[attr-defined]
    router_name = bare_name.replace(".adapter", "__home_x.adapter")
    router = types.ModuleType(router_name)
    router.FeishuAdapter = type("FeishuAdapter", (), {"__module__": router_name})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, bare_name, tenant)
    monkeypatch.setitem(sys.modules, router_name, router)
    _install_fake_platform_registry(
        monkeypatch, lambda: types.SimpleNamespace(adapter_factory=router.FeishuAdapter)
    )
    monkeypatch.setattr(
        feishu_adapter_compat,
        "_materialize_deferred_feishu_platform",
        _REAL_MATERIALIZE,
    )

    assert feishu_adapter_compat.load_live_feishu_module() is router
    assert feishu_adapter_compat.load_feishu_module() is router
    assert feishu_adapter_compat.load_feishu_adapter() is router.FeishuAdapter


def test_registry_entry_class_must_be_the_modules_feishu_adapter(monkeypatch) -> None:
    """Entry class claims a module whose ``FeishuAdapter`` is a different class →
    that module is not trusted; resolution falls back to the known names."""
    name = "hermes_plugins.platforms__feishu__home_y.adapter"
    other = types.ModuleType(name)
    other.FeishuAdapter = type("FeishuAdapter", (), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, other)
    stray = type("FeishuAdapter", (), {"__module__": name})
    bare_name = feishu_adapter_compat._PLUGIN_LOADER_MODULE_NAMES[0]
    bare = types.ModuleType(bare_name)
    bare.FeishuAdapter = type("FeishuAdapter", (), {"__module__": bare_name})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, bare_name, bare)
    _install_fake_platform_registry(monkeypatch, lambda: types.SimpleNamespace(adapter_factory=stray))

    assert feishu_adapter_compat.load_live_feishu_module() is bare


def test_unscoped_registry_entry_on_bare_name_still_resolves(monkeypatch) -> None:
    """core <= 0.21.4 shape: one unscoped registry whose entry sits on the bare
    ``feishu_platform`` module — registry-first must land on that same module."""
    name = "hermes_plugins.feishu_platform.adapter"
    synthetic = types.ModuleType(name)
    synthetic.FeishuAdapter = type("FeishuAdapter", (), {"__module__": name})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, synthetic)
    _install_fake_platform_registry(
        monkeypatch, lambda: types.SimpleNamespace(adapter_factory=synthetic.FeishuAdapter)
    )
    monkeypatch.setattr(
        feishu_adapter_compat,
        "_materialize_deferred_feishu_platform",
        _REAL_MATERIALIZE,
    )

    assert feishu_adapter_compat.load_live_feishu_module() is synthetic
    assert feishu_adapter_compat.load_feishu_module() is synthetic


def test_live_feishu_module_still_fails_closed_when_nothing_materializes(monkeypatch) -> None:
    _install_fake_platform_registry(monkeypatch, lambda: None)

    with pytest.raises(RuntimeError, match="did not materialize"):
        feishu_adapter_compat.load_live_feishu_module()


def test_feishu_adapter_load_error_logger_distinguishes_expected_missing_modules() -> None:
    class FakeLogger:
        def __init__(self) -> None:
            self.info_calls: list[str] = []
            self.warning_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        def info(self, message: str) -> None:
            self.info_calls.append(message)

        def warning(self, *args: Any, **kwargs: Any) -> None:
            self.warning_calls.append((args, kwargs))

    logger = FakeLogger()
    feishu_adapter_compat.log_feishu_adapter_load_error(
        logger,
        "missing",
        ModuleNotFoundError("gateway.platforms.feishu"),
    )
    assert logger.info_calls == ["missing"]
    assert logger.warning_calls == []

    feishu_adapter_compat.log_feishu_adapter_load_error(
        logger,
        "unexpected",
        ModuleNotFoundError("No module named 'legacy_dependency'", name="legacy_dependency"),
    )
    assert len(logger.warning_calls) == 1
    assert logger.warning_calls[0][1]["exc_info"] is True


def test_inbound_richtext_patch_defers_missing_plugin_layout_without_error_log(
    monkeypatch,
    caplog,
) -> None:
    from hermes_multitenancy import feishu_inbound_richtext

    real_import_module = feishu_adapter_compat.import_module

    def import_module(name: str) -> types.ModuleType:
        if name in {"gateway.platforms.feishu", "plugins.platforms.feishu.adapter"}:
            raise ModuleNotFoundError(name)
        return real_import_module(name)

    monkeypatch.setattr(feishu_adapter_compat, "import_module", import_module)
    caplog.set_level(logging.INFO, logger=feishu_inbound_richtext.logger.name)

    install_feishu_inbound_richtext_patch()

    assert "inbound richtext patch deferred" in caplog.text
    assert not [
        record
        for record in caplog.records
        if record.name == feishu_inbound_richtext.logger.name and record.levelno >= logging.WARNING
    ]


def test_cron_feishu_patches_defer_missing_plugin_layout_without_error_log(
    monkeypatch,
    caplog,
) -> None:
    real_import_module = feishu_adapter_compat.import_module

    def import_module(name: str) -> types.ModuleType:
        if name in {"gateway.platforms.feishu", "plugins.platforms.feishu.adapter"}:
            raise ModuleNotFoundError(name)
        return real_import_module(name)

    monkeypatch.setattr(feishu_adapter_compat, "import_module", import_module)
    caplog.set_level(logging.INFO, logger=cron_worker.logger.name)

    cron_worker._patch_feishu_open_id_send()
    cron_worker._patch_feishu_outbound_link_render()

    assert "open_id delivery patch deferred" in caplog.text
    assert "outbound link render patch deferred" in caplog.text
    assert not [
        record
        for record in caplog.records
        if record.name == cron_worker.logger.name and record.levelno >= logging.WARNING
    ]


def test_inbound_richtext_patch_installs_with_plugin_adapter_layout(monkeypatch) -> None:
    fake_module = _install_plugin_feishu_module(monkeypatch)

    install_feishu_inbound_richtext_patch()

    assert getattr(fake_module.normalize_feishu_message, "_hermes_multitenancy_inbound_patched", False)
    # 观测点必须是本层**仍然拥有**的类型。`email` 等 17 类已交还 core（那一批
    # 曾经的"富化"实测为原样返回的死代码），拿它当观测点等于测一个已不存在的行为。
    result = fake_module.normalize_feishu_message(
        message_type="interactive",
        raw_content=json.dumps(
            {"card": {"elements": [{"tag": "div", "text": {"content": "申请人: 张三"}}]}},
            ensure_ascii=False,
        ),
    )
    assert "申请人" in result.text_content
    assert "张三" in result.text_content


def test_cron_feishu_patches_install_with_plugin_adapter_layout(monkeypatch) -> None:
    fake_module = _install_plugin_feishu_module(monkeypatch)
    adapter_cls = fake_module.FeishuAdapter

    cron_worker._patch_feishu_open_id_send()
    cron_worker._patch_feishu_outbound_link_render()

    assert getattr(adapter_cls._send_raw_message, "_hermes_multitenancy_patched", False)
    assert getattr(adapter_cls._build_outbound_payload, "_hermes_multitenancy_patched", False)


def test_feishu_patch_installers_use_plugin_adapter_layout(monkeypatch) -> None:
    fake_module = _install_plugin_feishu_module(monkeypatch)
    adapter_cls = fake_module.FeishuAdapter

    from hermes_multitenancy import feishu_group_valve
    from hermes_multitenancy import feishu_helpdesk_events
    from hermes_multitenancy import feishu_merge_forward_api
    from hermes_multitenancy import feishu_reaction_lifecycle
    from hermes_multitenancy import feishu_reply_quote_api
    from hermes_multitenancy import group_inviter_hook

    feishu_group_valve._HOOK_INSTALLED = False
    feishu_merge_forward_api._HOOK_INSTALLED = False
    feishu_reaction_lifecycle._HOOK_INSTALLED = False
    feishu_reply_quote_api._HOOK_INSTALLED = False
    group_inviter_hook._HOOK_INSTALLED = False

    feishu_group_valve.install_feishu_group_valve_patch()
    feishu_helpdesk_events.install_feishu_helpdesk_events_patch()
    feishu_merge_forward_api.install_feishu_merge_forward_api_patch()
    feishu_reaction_lifecycle.install_feishu_reaction_lifecycle_patch()
    feishu_reply_quote_api.install_feishu_reply_quote_api_patch()
    group_inviter_hook.install_feishu_bot_added_hook()

    assert getattr(adapter_cls._require_mention_for, "_hermes_multitenancy_group_valve_require_mention_patched", False)
    assert getattr(adapter_cls._build_event_handler, "_hermes_mt_helpdesk_events_patched", False)
    assert getattr(adapter_cls._extract_message_content, "_hermes_multitenancy_merge_forward_api_patched", False)
    assert getattr(adapter_cls.on_processing_complete, "_hermes_multitenancy_reaction_lifecycle_patched", False)
    assert getattr(adapter_cls._fetch_message_text, "_hermes_multitenancy_reply_quote_fetch_patched", False)
    assert getattr(adapter_cls._on_card_action_trigger, "_hermes_multitenancy_group_valve_card_action_patched", False)
    assert getattr(adapter_cls._on_bot_added_to_chat, "_hermes_multitenancy_bot_added_class_patched", False)
