from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from hermes_multitenancy.card import desktop_screen_link, tool_use_display
from hermes_multitenancy.card.builder import _render_message_card
from hermes_multitenancy.card.state import _states

_ROOT = Path(__file__).resolve().parent.parent
_SNAPSHOTS = {
    "desktop_off": _ROOT / "tests" / "fixtures" / "desktop_screen_card_off_main_snapshot.json",
    "desktop_off_computer_use": _ROOT / "tests" / "fixtures" / "desktop_screen_card_off_computer_use_main_snapshot.json",
}
_ORIGIN = "https://hermes.example.com"
_SCREEN_URL = f"{_ORIGIN}/#/hermes/screen"

_COMPUTER = {"name": "computer_use", "status": "done", "preview": "", "args": {"action": "screenshot"}, "duration": 0.8}
_BROWSER = {"name": "browser_navigate", "status": "running", "preview": "", "args": {"url": "https://example.com"}}
_TERMINAL = {"name": "terminal", "status": "done", "preview": "", "args": {"command": "ls"}, "duration": 0.1}


def _evidence_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "desktop_screen_card_evidence", _ROOT / "scripts" / "desktop_screen_card_evidence.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _profile(root: Path, name: str, *, desktop: bool) -> Path:
    home = root / "profiles" / name
    home.mkdir(parents=True)
    (root / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    flag = "true" if desktop else "false"
    (home / "config.yaml").write_text(f"multitenancy:\n  desktop:\n    enabled: {flag}\n", encoding="utf-8")
    return home


class _Adapter:
    pass


def _marked_state(home: Path, tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Card start marks the state before any tool row exists (raw intents included)."""
    adapter = _Adapter()
    state = _evidence_module()._state([])
    _states(adapter)["om_1"] = state
    desktop_screen_link.mark_card_desktop_screen(adapter, "om_1", home)
    state["tools"].extend(dict(tool) for tool in tools)
    return state


def _buttons(card: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("tag") == "button":
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(card)
    return found


@pytest.fixture
def shared_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HERMES_SHARED_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PUBLIC_CALLBACK_ORIGIN", _ORIGIN)
    return tmp_path


def test_computer_use_descriptor_applies_only_on_desktop_cards() -> None:
    for name in ("computer_use", "computer-use"):
        assert tool_use_display.resolve_tool_descriptor(name) is None
        descriptor = tool_use_display.resolve_tool_descriptor(name, desktop_enabled=True)
        assert descriptor is not None
        assert descriptor.title == "操作电脑"
        assert descriptor.icon_token == "browser-mac_outlined"

    steps = [{"tool_name": "computer_use", "params": {"action": "left_click"}, "status": "success"}]
    assert tool_use_display.normalize_tool_use_display(steps, desktop_enabled=True)["content"] == "- 操作电脑: left_click"
    assert tool_use_display.normalize_tool_use_display(steps)["content"] == "- Computer use"


@pytest.mark.parametrize("tool", [_COMPUTER, _BROWSER], ids=["computer_use", "browser_navigate"])
def test_desktop_profile_card_carries_open_screen_button(shared_root: Path, tool: dict[str, Any]) -> None:
    home = _profile(shared_root, "desk_on", desktop=True)
    state = _marked_state(home, [_TERMINAL, tool])

    assert state["desktop_enabled"] is True
    assert state["desktop_screen_url"] == _SCREEN_URL
    card = _render_message_card(state)
    buttons = _buttons(card)
    assert len(buttons) == 1
    button = buttons[0]
    assert button["text"] == {"tag": "plain_text", "content": "打开屏幕"}
    assert set(button["multi_url"].values()) == {_SCREEN_URL}
    assert "value" not in button
    # The button sits at the top level right after the (collapsed) tool panel.
    assert card["elements"][0]["tag"] == "collapsible_panel"
    assert card["elements"][1]["tag"] == "column_set"

    live = tool_use_display._render_live_tool_section(state["tools"], state["desktop_screen_url"])
    assert live.endswith(f"\n[打开屏幕]({_SCREEN_URL})")


def test_desktop_profile_without_screen_tool_gets_no_button(shared_root: Path) -> None:
    home = _profile(shared_root, "desk_on", desktop=True)
    state = _marked_state(home, [_TERMINAL])

    assert state["desktop_screen_url"] == _SCREEN_URL
    assert _buttons(_render_message_card(state)) == []


@pytest.mark.parametrize("case", sorted(_SNAPSHOTS))
def test_desktop_off_card_is_byte_identical_to_main_snapshot(shared_root: Path, case: str) -> None:
    evidence = _evidence_module()
    snapshot = _SNAPSHOTS[case].read_text(encoding="utf-8")
    tools = evidence.OFF_CASES[case]

    unmarked = evidence.off_scenario(tools)
    assert json.dumps(unmarked, ensure_ascii=False, indent=2, sort_keys=True) + "\n" == snapshot

    home = _profile(shared_root, "desk_off", desktop=False)
    state = _marked_state(home, tools)
    assert state["desktop_enabled"] is False
    assert state["desktop_screen_url"] is None
    marked = evidence._render(state)
    marked["live_tool_element_markdown"] = tool_use_display._render_live_tool_section(
        state["tools"], state["desktop_screen_url"]
    )
    assert json.dumps(marked, ensure_ascii=False, indent=2, sort_keys=True) + "\n" == snapshot


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "hermes.example.com",
        "/relative",
        "ftp://hermes.example.com",
        "https://hermes.example.com/oauth/callback",
        "https://hermes.example.com?x=1",
        "https://hermes.example.com/#/other",
        "https://user:pw@hermes.example.com",
        "https://evil.example@hermes.example.com",
        "https://hermes.example.com:notaport",
    ],
)
def test_missing_or_relative_origin_yields_no_button(
    shared_root: Path, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    if origin:
        monkeypatch.setenv("HERMES_PUBLIC_CALLBACK_ORIGIN", origin)
    else:
        monkeypatch.delenv("HERMES_PUBLIC_CALLBACK_ORIGIN")
    home = _profile(shared_root, "desk_on", desktop=True)
    state = _marked_state(home, [_COMPUTER])

    # Desktop stays on (the row keeps 「操作电脑」); only the link is withheld.
    assert state["desktop_enabled"] is True
    assert state["desktop_screen_url"] is None
    card = _render_message_card(state)
    assert _buttons(card) == []
    assert "操作电脑" in json.dumps(card, ensure_ascii=False)
    assert "打开屏幕" not in tool_use_display._render_live_tool_section(state["tools"], None)


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        (f"{_ORIGIN}/", _SCREEN_URL),
        ("https://hermes.example.com:8443", "https://hermes.example.com:8443/#/hermes/screen"),
        ("http://127.0.0.1:8648", "http://127.0.0.1:8648/#/hermes/screen"),
    ],
)
def test_valid_origins_build_screen_url(
    shared_root: Path, monkeypatch: pytest.MonkeyPatch, origin: str, expected: str
) -> None:
    monkeypatch.setenv("HERMES_PUBLIC_CALLBACK_ORIGIN", origin)
    home = _profile(shared_root, "desk_on", desktop=True)
    assert desktop_screen_link.screen_url_for_profile(home) == expected


def test_router_profile_never_gets_a_screen_link(shared_root: Path) -> None:
    home = _profile(shared_root, "multitenancy_router", desktop=True)
    assert desktop_screen_link.screen_url_for_profile(home) is None


def test_link_resolution_failure_is_fail_open(shared_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_home: Any) -> bool:
        raise OSError("config unreadable")

    monkeypatch.setattr(desktop_screen_link, "profile_desktop_enabled", boom)
    home = _profile(shared_root, "desk_on", desktop=True)
    state = _marked_state(home, [_COMPUTER])

    assert state["desktop_enabled"] is False
    assert state["desktop_screen_url"] is None
    assert _buttons(_render_message_card(state)) == []


def test_mark_ignores_unknown_card_and_resolves_once(shared_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    def counting(home: Any) -> bool:
        calls.append(home)
        return True

    monkeypatch.setattr(desktop_screen_link, "profile_desktop_enabled", counting)
    adapter = _Adapter()
    desktop_screen_link.mark_card_desktop_screen(adapter, "om_missing", shared_root)
    desktop_screen_link.mark_card_desktop_screen(adapter, None, shared_root)
    assert _states(adapter) == {}

    _states(adapter)["om_1"] = {"tools": []}
    for _ in range(3):
        desktop_screen_link.mark_card_desktop_screen(adapter, "om_1", shared_root)
    assert calls == [shared_root]
    assert _states(adapter)["om_1"]["desktop_screen_url"] == _SCREEN_URL


@pytest.mark.parametrize("completed", [True, False], ids=["completion_only", "started"])
def test_router_tool_event_marks_card_before_rendering(shared_root: Path, completed: bool) -> None:
    """A card that only ever sees a completion (reconnect / lost start) still gets the link."""
    from hermes_multitenancy.router.streaming import _update_feishu_stream_tool_event

    home = _profile(shared_root, "desk_on", desktop=True)
    seen: dict[str, Any] = {}

    class Adapter:
        async def _record(self, **kwargs: Any) -> None:
            state = _states(self)[kwargs["message_id"]]
            seen["url"] = state.get("desktop_screen_url")
            seen["enabled"] = state.get("desktop_enabled")
            state["tools"].append({"name": kwargs["tool_name"], "status": "done"})

        update_streaming_card_tool_started = _record
        update_streaming_card_tool_completed = _record

    adapter = Adapter()
    state = {"tools": []}
    _states(adapter)["om_1"] = state
    asyncio.run(
        _update_feishu_stream_tool_event(
            adapter,
            "oc_1",
            "om_1",
            {"name": "computer_use", "args": {"action": "screenshot"}, "duration": 0.5},
            mode="card",
            completed=completed,
            profile_home=home,
        )
    )
    assert seen == {"url": _SCREEN_URL, "enabled": True}
    card = _render_message_card({**_evidence_module()._state([]), **state})
    assert [b["multi_url"]["url"] for b in _buttons(card)] == [_SCREEN_URL]
