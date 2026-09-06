from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import sys
import types
from typing import Any

import pytest

from hermes_multitenancy import feishu_media_retry


_MODULE_NAMES = (
    "gateway",
    "gateway.platform_registry",
    "gateway.platforms",
    "gateway.platforms.feishu",
    "hermes_plugins",
    "hermes_plugins.feishu_platform",
    "hermes_plugins.feishu_platform.adapter",
)


@pytest.fixture(autouse=True)
def _reset_patch_state() -> Iterator[None]:
    feishu_media_retry._INSTALLED = False
    feishu_media_retry._spent_retry_message_ids.clear()
    yield
    feishu_media_retry._INSTALLED = False
    feishu_media_retry._spent_retry_message_ids.clear()


def _link_module(name: str, module: types.ModuleType) -> None:
    parts = name.split(".")
    for index in range(1, len(parts)):
        parent_name = ".".join(parts[:index])
        parent = sys.modules.get(parent_name)
        if parent is None:
            parent = types.ModuleType(parent_name)
            parent.__path__ = []  # type: ignore[attr-defined]
            sys.modules[parent_name] = parent
        child_name = parts[index]
        child_full_name = ".".join(parts[: index + 1])
        child = sys.modules.get(child_full_name)
        if child is not None:
            setattr(parent, child_name, child)
    if len(parts) > 1:
        parent = sys.modules[".".join(parts[:-1])]
        setattr(parent, parts[-1], module)


def _inject_adapter_module(name: str, cls: type[Any]) -> None:
    parts = name.split(".")
    for index in range(1, len(parts)):
        parent_name = ".".join(parts[:index])
        parent = sys.modules.get(parent_name)
        if parent is None:
            parent = types.ModuleType(parent_name)
            parent.__path__ = []  # type: ignore[attr-defined]
            sys.modules[parent_name] = parent
            _link_module(parent_name, parent)
    module = types.ModuleType(name)
    module.FeishuAdapter = cls  # type: ignore[attr-defined]
    sys.modules[name] = module
    _link_module(name, module)


@contextmanager
def _installed(modules: dict[str, type[Any]]) -> Iterator[None]:
    saved = {name: sys.modules.get(name) for name in _MODULE_NAMES}
    for name in reversed(_MODULE_NAMES):
        sys.modules.pop(name, None)
    for name, cls in modules.items():
        _inject_adapter_module(name, cls)
    feishu_media_retry._INSTALLED = False
    feishu_media_retry.install_feishu_media_retry_patch()
    try:
        yield
    finally:
        for name in reversed(_MODULE_NAMES):
            sys.modules.pop(name, None)
        for name in _MODULE_NAMES:
            module = saved[name]
            if module is not None:
                sys.modules[name] = module
                _link_module(name, module)


def _make_adapter(behaviors: dict[str, Callable[[], tuple[str, str]]]) -> type[Any]:
    namespace: dict[str, Any] = {}
    for name, behavior in behaviors.items():
        if name == "_download_feishu_image":
            async def _download_feishu_image(
                self,
                *,
                message_id: str,
                image_key: str,
                _behavior: Callable[[], tuple[str, str]] = behavior,
            ) -> tuple[str, str]:
                return _behavior()

            namespace[name] = _download_feishu_image
            continue
        if name == "_download_feishu_message_resource":
            async def _download_feishu_message_resource(
                self,
                *,
                message_id: str,
                file_key: str,
                resource_type: str,
                fallback_filename: str,
                _behavior: Callable[[], tuple[str, str]] = behavior,
            ) -> tuple[str, str]:
                return _behavior()

            namespace[name] = _download_feishu_message_resource
            continue
        if name == "_download_remote_document":
            async def _download_remote_document(
                self,
                file_url: str,
                *,
                default_ext: str,
                preferred_name: str,
                _behavior: Callable[[], tuple[str, str]] = behavior,
            ) -> tuple[str, str]:
                return _behavior()

            namespace[name] = _download_remote_document
            continue
        raise ValueError(f"Unsupported fake adapter method: {name}")
    return type("FakeFeishuAdapter", (), namespace)


async def test_empty_then_success_retries_once_zero_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    sleep_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def fake_sleep(*args: Any, **kwargs: Any) -> None:
        sleep_calls.append((args, kwargs))

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    def behavior() -> tuple[str, str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ("", "")
        return ("/cache/x.jpg", "image/jpeg")

    adapter_cls = _make_adapter({"_download_feishu_image": behavior})
    with _installed({"gateway.platforms.feishu": adapter_cls}):
        result = await adapter_cls()._download_feishu_image(message_id="m1", image_key="img1")

    assert result == ("/cache/x.jpg", "image/jpeg")
    assert calls == 2
    assert sleep_calls == []


async def test_second_failing_item_same_message_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    sleep_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def fake_sleep(*args: Any, **kwargs: Any) -> None:
        sleep_calls.append((args, kwargs))

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    def behavior() -> tuple[str, str]:
        nonlocal calls
        calls += 1
        return ("", "")

    adapter_cls = _make_adapter({"_download_feishu_image": behavior})
    with _installed({"gateway.platforms.feishu": adapter_cls}):
        adapter = adapter_cls()
        first = await adapter._download_feishu_image(message_id="m1", image_key="img1")
        assert calls == 2
        second = await adapter._download_feishu_image(message_id="m1", image_key="img2")

    assert first == ("", "")
    assert second == ("", "")
    assert calls == 3
    assert sleep_calls == []


async def test_different_message_ids_each_get_one_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    current_message_id = ""
    per_message_calls = {"mA": 0, "mB": 0}

    async def fake_sleep(*args: Any, **kwargs: Any) -> None:
        sleep_calls.append((args, kwargs))

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    def behavior() -> tuple[str, str]:
        per_message_calls[current_message_id] += 1
        if per_message_calls[current_message_id] == 1:
            return ("", "")
        return (f"/cache/{current_message_id}.bin", "application/octet-stream")

    adapter_cls = _make_adapter({"_download_feishu_message_resource": behavior})
    with _installed({"gateway.platforms.feishu": adapter_cls}):
        adapter = adapter_cls()
        current_message_id = "mA"
        result_a = await adapter._download_feishu_message_resource(
            message_id="mA",
            file_key="fileA",
            resource_type="file",
            fallback_filename="a.bin",
        )
        current_message_id = "mB"
        result_b = await adapter._download_feishu_message_resource(
            message_id="mB",
            file_key="fileB",
            resource_type="file",
            fallback_filename="b.bin",
        )

    assert result_a == ("/cache/mA.bin", "application/octet-stream")
    assert result_b == ("/cache/mB.bin", "application/octet-stream")
    assert per_message_calls == {"mA": 2, "mB": 2}
    assert sleep_calls == []


async def test_normal_path_no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    sleep_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def fake_sleep(*args: Any, **kwargs: Any) -> None:
        sleep_calls.append((args, kwargs))

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    def behavior() -> tuple[str, str]:
        nonlocal calls
        calls += 1
        return ("/cache/ok.png", "image/png")

    adapter_cls = _make_adapter({"_download_feishu_message_resource": behavior})
    with _installed({"gateway.platforms.feishu": adapter_cls}):
        result = await adapter_cls()._download_feishu_message_resource(
            message_id="m1",
            file_key="file1",
            resource_type="image",
            fallback_filename="ok.png",
        )

    assert result == ("/cache/ok.png", "image/png")
    assert calls == 1
    assert sleep_calls == []
    assert feishu_media_retry._spent_retry_message_ids == {}


def test_install_idempotent_no_double_wrap() -> None:
    def behavior() -> tuple[str, str]:
        return ("/cache/ok.png", "image/png")

    adapter_cls = _make_adapter({"_download_feishu_image": behavior})
    with _installed({"gateway.platforms.feishu": adapter_cls}):
        first_wrap = adapter_cls._download_feishu_image
        feishu_media_retry._INSTALLED = False
        feishu_media_retry.install_feishu_media_retry_patch()
        second_wrap = adapter_cls._download_feishu_image

    assert first_wrap is second_wrap
    assert getattr(first_wrap, "_media_retry_patched", False) is True


def test_download_remote_document_not_patched() -> None:
    def image_behavior() -> tuple[str, str]:
        return ("/cache/ok.jpg", "image/jpeg")

    def remote_behavior() -> tuple[str, str]:
        return ("/cache/anim.gif", "image/gif")

    adapter_cls = _make_adapter(
        {
            "_download_feishu_image": image_behavior,
            "_download_remote_document": remote_behavior,
        }
    )
    original_remote = adapter_cls._download_remote_document
    with _installed({"gateway.platforms.feishu": adapter_cls}):
        assert adapter_cls._download_remote_document is original_remote
        assert getattr(adapter_cls._download_remote_document, "_media_retry_patched", False) is False
        assert getattr(adapter_cls._download_feishu_image, "_media_retry_patched", False) is True


def test_patch_lands_on_synthetic_production_module() -> None:
    def behavior() -> tuple[str, str]:
        return ("/cache/ok.jpg", "image/jpeg")

    synthetic_adapter = _make_adapter({"_download_feishu_image": behavior})
    legacy_adapter = _make_adapter({"_download_feishu_image": behavior})

    with _installed(
        {
            "hermes_plugins.feishu_platform.adapter": synthetic_adapter,
            "gateway.platforms.feishu": legacy_adapter,
        }
    ):
        # Regression for the 2026-07-05 double-load trap: patch the synthetic module
        # the gateway instantiates, not the legacy clone class sitting beside it.
        assert getattr(synthetic_adapter._download_feishu_image, "_media_retry_patched", False) is True
        assert getattr(legacy_adapter._download_feishu_image, "_media_retry_patched", False) is False
