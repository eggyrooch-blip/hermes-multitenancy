"""Layer 4 — bot_added event inviter capture via class-level monkey patch."""
from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _clear_router_cache():
    from hermes_multitenancy import router as router_mod
    router_mod._chat_inviter_cache.clear()
    yield
    router_mod._chat_inviter_cache.clear()


def test_capture_from_event_records_inviter():
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.group_inviter_hook import _capture_inviter_from_event

    data = SimpleNamespace(
        event=SimpleNamespace(
            chat_id="oc_abc",
            chat_name="IT 组",
            operator_id=SimpleNamespace(open_id="ou_inviter"),
        ),
    )
    _capture_inviter_from_event(data)
    cached = router_mod._chat_inviter_cache["oc_abc"]
    assert cached["inviter_open_id"] == "ou_inviter"
    assert cached["chat_name"] == "IT 组"


def test_capture_handles_dict_operator():
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.group_inviter_hook import _capture_inviter_from_event

    data = SimpleNamespace(
        event=SimpleNamespace(
            chat_id="oc_abc",
            chat_name="IT 组",
            operator_id={"open_id": "ou_inviter", "name": "owner"},
        ),
    )
    _capture_inviter_from_event(data)
    cached = router_mod._chat_inviter_cache["oc_abc"]
    assert cached["inviter_open_id"] == "ou_inviter"
    assert cached["inviter_display"] == "owner"


def test_capture_skips_when_no_chat_id():
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.group_inviter_hook import _capture_inviter_from_event

    data = SimpleNamespace(
        event=SimpleNamespace(
            chat_id="",
            operator_id=SimpleNamespace(open_id="ou_inviter"),
        ),
    )
    _capture_inviter_from_event(data)
    assert router_mod._chat_inviter_cache == {}


def test_capture_skips_when_no_inviter_open_id():
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.group_inviter_hook import _capture_inviter_from_event

    data = SimpleNamespace(
        event=SimpleNamespace(
            chat_id="oc_abc",
            operator_id=SimpleNamespace(open_id=""),
        ),
    )
    _capture_inviter_from_event(data)
    assert router_mod._chat_inviter_cache == {}


def test_capture_failure_does_not_break_handler():
    """When event payload is malformed, the helper must not raise."""
    from hermes_multitenancy.group_inviter_hook import _capture_inviter_from_event

    # No exception should escape on malformed input
    _capture_inviter_from_event(SimpleNamespace(event=None))
    _capture_inviter_from_event(SimpleNamespace())


def test_install_hook_patches_class_method():
    """The hook must wrap FeishuAdapter._on_bot_added_to_chat at the CLASS level.

    Patching the instance after adapter init would be too late because the
    Lark SDK captures ``self._on_bot_added_to_chat`` during ``__init__`` via
    ``register_p2_im_chat_member_bot_added_v1``.
    """
    from hermes_multitenancy import group_inviter_hook
    from hermes_multitenancy import router as router_mod

    seen = []

    class FakeFeishuAdapter:
        def _on_bot_added_to_chat(self, data):
            seen.append(("original", data))
            return "original_return"

    # Stand in for gateway.platforms.feishu.FeishuAdapter import.
    import sys, types
    fake_module = types.ModuleType("gateway.platforms.feishu")
    fake_module.FeishuAdapter = FakeFeishuAdapter  # type: ignore[attr-defined]
    # Inject parent packages so the import works
    parents = ("gateway", "gateway.platforms")
    saved_parents = {name: sys.modules.get(name) for name in parents}
    saved_module = sys.modules.get("gateway.platforms.feishu")
    for name in parents:
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules["gateway.platforms.feishu"] = fake_module

    # Reset the hook's own idempotent guard so installation can run in this test.
    group_inviter_hook._HOOK_INSTALLED = False
    original_method = FakeFeishuAdapter._on_bot_added_to_chat
    try:
        group_inviter_hook.install_feishu_bot_added_hook()

        # The class method should now be the wrapped version.
        assert FakeFeishuAdapter._on_bot_added_to_chat is not original_method

        # Simulate an adapter instance that was constructed AFTER the patch:
        # SDK captures `self._on_bot_added_to_chat`; we mimic that capture
        # then invoke the captured reference with a payload.
        adapter = FakeFeishuAdapter()
        sdk_captured = adapter._on_bot_added_to_chat  # bound method via class
        payload = SimpleNamespace(
            event=SimpleNamespace(
                chat_id="oc_test",
                operator_id=SimpleNamespace(open_id="ou_inv"),
            ),
        )
        sdk_captured(payload)
        # Original handler still ran
        assert ("original", payload) in seen
        # Inviter was captured into the router's cache
        assert router_mod._chat_inviter_cache["oc_test"]["inviter_open_id"] == "ou_inv"
    finally:
        # Restore class state + sys.modules entries
        FakeFeishuAdapter._on_bot_added_to_chat = original_method
        if saved_module is None:
            sys.modules.pop("gateway.platforms.feishu", None)
        else:
            sys.modules["gateway.platforms.feishu"] = saved_module
        for name, mod in saved_parents.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        group_inviter_hook._HOOK_INSTALLED = False


def test_install_hook_idempotent():
    """Calling install twice must not double-wrap the class method."""
    from hermes_multitenancy import group_inviter_hook
    import sys, types

    class FakeFeishuAdapter:
        def _on_bot_added_to_chat(self, data):
            return "ok"

    fake_module = types.ModuleType("gateway.platforms.feishu")
    fake_module.FeishuAdapter = FakeFeishuAdapter  # type: ignore[attr-defined]
    parents = ("gateway", "gateway.platforms")
    saved_parents = {name: sys.modules.get(name) for name in parents}
    saved_module = sys.modules.get("gateway.platforms.feishu")
    for name in parents:
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules["gateway.platforms.feishu"] = fake_module
    group_inviter_hook._HOOK_INSTALLED = False
    original = FakeFeishuAdapter._on_bot_added_to_chat
    try:
        group_inviter_hook.install_feishu_bot_added_hook()
        first_wrap = FakeFeishuAdapter._on_bot_added_to_chat
        group_inviter_hook.install_feishu_bot_added_hook()
        second_wrap = FakeFeishuAdapter._on_bot_added_to_chat
        assert first_wrap is second_wrap, "install must be idempotent"
    finally:
        FakeFeishuAdapter._on_bot_added_to_chat = original
        if saved_module is None:
            sys.modules.pop("gateway.platforms.feishu", None)
        else:
            sys.modules["gateway.platforms.feishu"] = saved_module
        for name, mod in saved_parents.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        group_inviter_hook._HOOK_INSTALLED = False


def test_install_hook_patches_chat_added_welcome_for_groups():
    """The class-level patch must replace _send_chat_added_onboarding with a
    group-aware wrapper that sends the group message when chat_type is 'group'.
    """
    import asyncio
    from hermes_multitenancy import group_inviter_hook
    import sys, types

    sent = []

    class FakeFeishuAdapter:
        def __init__(self):
            self._chat_info_cache = {}

        async def get_chat_info(self, chat_id):
            return {"type": "group", "name": "测试群"}

        async def send(self, chat_id, text):
            sent.append((chat_id, text))

        async def _on_bot_added_to_chat(self, data):
            return None

        async def _send_chat_added_onboarding(self, chat_id):
            await self.send(chat_id, "ORIGINAL_P2P_WELCOME")

    fake_module = types.ModuleType("gateway.platforms.feishu")
    fake_module.FeishuAdapter = FakeFeishuAdapter  # type: ignore[attr-defined]
    parents = ("gateway", "gateway.platforms")
    saved_parents = {name: sys.modules.get(name) for name in parents}
    saved_module = sys.modules.get("gateway.platforms.feishu")
    for name in parents:
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules["gateway.platforms.feishu"] = fake_module
    group_inviter_hook._HOOK_INSTALLED = False
    original_welcome = FakeFeishuAdapter._send_chat_added_onboarding
    try:
        group_inviter_hook.install_feishu_bot_added_hook()
        assert FakeFeishuAdapter._send_chat_added_onboarding is not original_welcome

        adapter = FakeFeishuAdapter()
        # Pre-seed cache so the wrapper doesn't have to call get_chat_info
        adapter._chat_info_cache["oc_group"] = {"type": "group", "name": "测试群"}

        asyncio.run(adapter._send_chat_added_onboarding("oc_group"))
        assert sent, "wrapper should have sent the group welcome"
        chat_id, text = sent[-1]
        assert chat_id == "oc_group"
        assert "群" in text and "/feishu_auth" in text
        # Original p2p text MUST NOT have been sent — the wrapper short-circuited
        assert "ORIGINAL_P2P_WELCOME" not in text
    finally:
        FakeFeishuAdapter._send_chat_added_onboarding = original_welcome
        if saved_module is None:
            sys.modules.pop("gateway.platforms.feishu", None)
        else:
            sys.modules["gateway.platforms.feishu"] = saved_module
        for name, mod in saved_parents.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        group_inviter_hook._HOOK_INSTALLED = False


def test_install_hook_welcome_p2p_falls_through_to_original():
    """For p2p chats, the wrapper must delegate to the original welcome."""
    import asyncio
    from hermes_multitenancy import group_inviter_hook
    import sys, types

    sent = []

    class FakeFeishuAdapter:
        def __init__(self):
            self._chat_info_cache = {}

        async def get_chat_info(self, chat_id):
            return {"type": "p2p"}

        async def send(self, chat_id, text):
            sent.append((chat_id, text))

        async def _on_bot_added_to_chat(self, data):
            return None

        async def _send_chat_added_onboarding(self, chat_id):
            await self.send(chat_id, "ORIGINAL_P2P_WELCOME_OK")

    fake_module = types.ModuleType("gateway.platforms.feishu")
    fake_module.FeishuAdapter = FakeFeishuAdapter  # type: ignore[attr-defined]
    parents = ("gateway", "gateway.platforms")
    saved_parents = {name: sys.modules.get(name) for name in parents}
    saved_module = sys.modules.get("gateway.platforms.feishu")
    for name in parents:
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules["gateway.platforms.feishu"] = fake_module
    group_inviter_hook._HOOK_INSTALLED = False
    original_welcome = FakeFeishuAdapter._send_chat_added_onboarding
    try:
        group_inviter_hook.install_feishu_bot_added_hook()
        adapter = FakeFeishuAdapter()
        adapter._chat_info_cache["oc_p2p"] = {"type": "p2p"}

        asyncio.run(adapter._send_chat_added_onboarding("oc_p2p"))
        assert sent == [("oc_p2p", "ORIGINAL_P2P_WELCOME_OK")]
    finally:
        FakeFeishuAdapter._send_chat_added_onboarding = original_welcome
        if saved_module is None:
            sys.modules.pop("gateway.platforms.feishu", None)
        else:
            sys.modules["gateway.platforms.feishu"] = saved_module
        for name, mod in saved_parents.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        group_inviter_hook._HOOK_INSTALLED = False


def test_install_hook_skips_when_module_not_importable():
    """If FeishuAdapter cannot be imported (e.g. in a unit-test process), the
    helper must log + return rather than raise."""
    from hermes_multitenancy import group_inviter_hook
    import sys

    saved_modules = {}
    for name in ("gateway", "gateway.platforms", "gateway.platforms.feishu"):
        if name in sys.modules:
            saved_modules[name] = sys.modules.pop(name)
    group_inviter_hook._HOOK_INSTALLED = False
    try:
        group_inviter_hook.install_feishu_bot_added_hook()  # must not raise
        # And the install flag must NOT be set so a later retry can succeed.
        assert group_inviter_hook._HOOK_INSTALLED is False
    finally:
        for name, mod in saved_modules.items():
            sys.modules[name] = mod
        group_inviter_hook._HOOK_INSTALLED = False
