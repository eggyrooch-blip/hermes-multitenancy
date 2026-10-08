"""Render Feishu tool cards with the real builders for the desktop screen-link evidence.

Usage: python scripts/desktop_screen_card_evidence.py <out_dir> [--off-only]

Writes one JSON file per scenario. ``--off-only`` renders just the
desktop-off scenario, which is the one that must match ``origin/main``
byte-for-byte (run it with ``PYTHONPATH`` pointing at a main export).
Profiles are temporary directories; nothing outside them is touched.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from hermes_multitenancy.card.builder import _render_message_card, _to_cardkit2
from hermes_multitenancy.card.state import _new_state

OFF_TOOLS: list[dict[str, Any]] = [
    {"name": "browser_navigate", "status": "done", "preview": "", "args": {"url": "https://example.com/docs"}, "duration": 1.2},
    {"name": "terminal", "status": "done", "preview": "", "args": {"command": "ls -la"}, "duration": 0.3},
    {"name": "read_file", "status": "running", "preview": "", "args": {"path": "/tmp/report.md"}},
]


def _state(tools: list[dict[str, Any]], screen_url: Any = ...) -> dict[str, Any]:
    state = _new_state()
    state["tools"] = [dict(tool) for tool in tools]
    state["content"] = "已打开页面并截图。"
    if screen_url is not ...:
        state["desktop_screen_url"] = screen_url
    return state


def _render(state: dict[str, Any]) -> dict[str, Any]:
    card = _render_message_card(state)
    return {"im_card": card, "cardkit2": _to_cardkit2(card)}


def _dump(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


COMPUTER_TOOL: dict[str, Any] = {
    "name": "computer_use", "status": "done", "preview": "", "args": {"action": "screenshot"}, "duration": 0.8,
}
# Desktop-off cards that must match origin/main byte for byte. The computer_use
# case covers a stale/replayed event landing on a profile whose desktop is off.
OFF_CASES: dict[str, list[dict[str, Any]]] = {
    "desktop_off": OFF_TOOLS,
    "desktop_off_computer_use": [*OFF_TOOLS, COMPUTER_TOOL],
}


def off_scenario(tools: list[dict[str, Any]]) -> dict[str, Any]:
    from hermes_multitenancy.card.tool_use_display import _render_tool_calls_section

    rendered = _render(_state(tools))
    rendered["live_tool_element_markdown"] = _render_tool_calls_section(tools)
    return rendered


def _profile(root: Path, name: str, *, desktop: bool) -> Path:
    home = root / "profiles" / name
    home.mkdir(parents=True)
    (root / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    enabled = "true" if desktop else "false"
    (home / "config.yaml").write_text(f"multitenancy:\n  desktop:\n    enabled: {enabled}\n", encoding="utf-8")
    return home


def _marked(home: Path, tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Drive the router hook as card start does (before any tool event), then render."""
    from hermes_multitenancy.card.desktop_screen_link import mark_card_desktop_screen
    from hermes_multitenancy.card.state import _states
    from hermes_multitenancy.card.tool_use_display import _render_live_tool_section

    class _Adapter:
        pass

    adapter = _Adapter()
    state = _state([])
    _states(adapter)["om_evidence"] = state
    mark_card_desktop_screen(adapter, "om_evidence", home)
    state["tools"].extend(dict(tool) for tool in tools)

    rendered = _render(state)
    rendered["desktop_enabled"] = state.get("desktop_enabled")
    rendered["desktop_screen_url"] = state.get("desktop_screen_url")
    rendered["live_tool_element_markdown"] = _render_live_tool_section(state["tools"], state.get("desktop_screen_url"))
    return rendered


def main() -> int:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for name, tools in OFF_CASES.items():
        _dump(out / f"{name}.json", off_scenario(tools))
    if "--off-only" in sys.argv:
        return 0

    browser = [{"name": "browser_navigate", "status": "running", "preview": "", "args": {"url": "https://example.com"}}]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.environ["HERMES_SHARED_HOME"] = str(root)
        on_home = _profile(root, "desk_on", desktop=True)
        off_home = _profile(root, "desk_off", desktop=False)

        os.environ["HERMES_PUBLIC_CALLBACK_ORIGIN"] = "https://hermes.example.com"
        _dump(out / "desktop_on_computer_use.json", _marked(on_home, [COMPUTER_TOOL]))
        _dump(out / "desktop_on_browser_navigate.json", _marked(on_home, browser))
        for name, tools in OFF_CASES.items():
            _dump(out / f"{name}_profile_marked.json", _marked(off_home, tools))

        os.environ.pop("HERMES_PUBLIC_CALLBACK_ORIGIN")
        _dump(out / "desktop_on_origin_unset.json", _marked(on_home, [COMPUTER_TOOL]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
