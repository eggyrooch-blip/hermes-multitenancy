from __future__ import annotations

import ast
import contextvars
import os
import sys
import types
from pathlib import Path

from hermes_multitenancy.agent_real import _core


def test_vendored_toolset_resolver_deduplicates_and_sorts(monkeypatch):
    import toolsets

    calls: list[str] = []
    resolved = {
        "alpha": ["shared", "beta"],
        "omega": ["zeta", "alpha", "shared"],
    }

    def resolve_toolset(name: str) -> list[str]:
        calls.append(name)
        return resolved[name]

    monkeypatch.setattr(toolsets, "resolve_toolset", resolve_toolset)

    assert _core._resolve_toolset_collection(["omega", "alpha", "omega"]) == [
        "alpha",
        "beta",
        "shared",
        "zeta",
    ]
    assert calls == ["omega", "alpha", "omega"]


def test_todo_availability_accepts_legacy_and_current_tool_names(monkeypatch):
    import toolsets

    resolved = {
        "legacy-bundle": ["terminal", "todo"],
        "current-bundle": ["terminal", "todo_list"],
        "plain": ["terminal"],
    }
    monkeypatch.setattr(
        toolsets,
        "resolve_toolset",
        lambda name: resolved.get(name, []),
    )

    assert _core._todo_tool_available(["todo"], None) is True
    assert _core._todo_tool_available(["todo_list"], None) is True
    assert _core._todo_tool_available(["legacy-bundle"], None) is True
    assert _core._todo_tool_available(["current-bundle"], None) is True
    assert _core._todo_tool_available(["plain"], None) is False

    assert _core._todo_tool_available(None, ["todo"]) is False
    assert _core._todo_tool_available(None, ["todo_list"]) is False
    assert _core._todo_tool_available(None, ["legacy-bundle"]) is False
    assert _core._todo_tool_available(None, ["current-bundle"]) is False


def _install_approval_modules(monkeypatch, *, split_context: bool):
    events: list[tuple[str, object]] = []
    current_session = contextvars.ContextVar("compat_approval_session", default="outer")

    def set_session(session_key: str):
        events.append(("set", session_key))
        return current_session.set(session_key)

    def reset_session(token):
        current_session.reset(token)
        events.append(("reset", current_session.get()))

    approval = types.ModuleType("tools.approval")
    approval.register_gateway_notify = lambda *_args: None
    approval.unregister_gateway_notify = lambda *_args: None
    approval.resolve_gateway_approval = lambda *_args: 0

    approval_context = types.ModuleType("tools.approval_context")
    if split_context:
        approval_context.set_current_session_key = set_session
        approval_context.reset_current_session_key = reset_session

        def reject_legacy(*_args):
            raise AssertionError("legacy approval facade should not be used")

        approval.set_current_session_key = reject_legacy
        approval.reset_current_session_key = reject_legacy
    else:
        approval.set_current_session_key = set_session
        approval.reset_current_session_key = reset_session

    tools_package = sys.modules["tools"]
    monkeypatch.setattr(tools_package, "approval", approval, raising=False)
    monkeypatch.setattr(
        tools_package,
        "approval_context",
        approval_context,
        raising=False,
    )
    monkeypatch.setitem(sys.modules, "tools.approval", approval)
    monkeypatch.setitem(sys.modules, "tools.approval_context", approval_context)
    return events, current_session


def _assert_bridge_binds_and_restores(monkeypatch, *, split_context: bool):
    events, current_session = _install_approval_modules(
        monkeypatch,
        split_context=split_context,
    )
    monkeypatch.setenv("HERMES_SESSION_KEY", "prior-session")
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.setenv("HERMES_EXEC_ASK", "prior-exec")

    cleanup = _core._configure_gateway_approval_bridge(None, "session-021")
    assert current_session.get() == "session-021"
    assert os.environ["HERMES_SESSION_KEY"] == "session-021"
    assert os.environ["HERMES_GATEWAY_SESSION"] == "1"
    assert os.environ["HERMES_EXEC_ASK"] == "1"

    cleanup()

    assert current_session.get() == "outer"
    assert events == [("set", "session-021"), ("reset", "outer")]
    assert os.environ["HERMES_SESSION_KEY"] == "prior-session"
    assert "HERMES_GATEWAY_SESSION" not in os.environ
    assert os.environ["HERMES_EXEC_ASK"] == "prior-exec"


def test_hermes_021_uses_split_approval_context_and_cleanup_restores(monkeypatch):
    _assert_bridge_binds_and_restores(monkeypatch, split_context=True)


def test_hermes_014_dynamic_approval_fallback_and_cleanup_restores(monkeypatch):
    _assert_bridge_binds_and_restores(monkeypatch, split_context=False)


def test_core_has_no_deprecated_static_imports_or_dotted_targets():
    source = Path(_core.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_imports = {
        "toolsets": {"resolve_multiple_toolsets"},
        "tools.approval": {
            "set_current_session_key",
            "reset_current_session_key",
        },
    }

    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in forbidden_imports:
            for imported in node.names:
                if imported.name in forbidden_imports[node.module]:
                    hits.append(f"{node.module}.{imported.name}")

    forbidden_dotted_targets = {
        "toolsets." + "resolve_multiple_toolsets",
        "tools.approval." + "set_current_session_key",
        "tools.approval." + "reset_current_session_key",
    }
    assert hits == []
    assert forbidden_dotted_targets.isdisjoint(source.split())
    assert all(target not in source for target in forbidden_dotted_targets)
