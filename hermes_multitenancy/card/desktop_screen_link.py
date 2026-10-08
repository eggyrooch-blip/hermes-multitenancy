"""Desktop flag and Bot Screen link on Feishu tool cards.

The router knows the routed ``profile_home``; the card controller only knows
its per-message state. As soon as a card's message_id exists — before the
agent stream is consumed — the router resolves the profile once and stores
two keys on that card's state:

* ``desktop_enabled`` — the profile runs a desktop; switches on desktop-only
  row descriptors (「操作电脑」 for ``computer_use``).
* ``desktop_screen_url`` — ``<origin>/#/hermes/screen`` for the 「打开屏幕」
  button/link, or ``None`` when desktop is off or no valid public origin is
  configured.

A state with ``desktop_enabled`` false renders exactly as before.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from .state import _states

logger = logging.getLogger("hermes_multitenancy.feishu_cardkit_compat")

SCREEN_ROUTE = "/#/hermes/screen"
ENABLED_STATE_KEY = "desktop_enabled"
URL_STATE_KEY = "desktop_screen_url"


def _public_origin() -> Optional[str]:
    """``scheme://host[:port]`` from ``HERMES_PUBLIC_CALLBACK_ORIGIN``, or ``None``.

    Only a bare absolute http(s) origin is accepted: userinfo, a query, a
    fragment or any path beyond ``/`` would make the button point somewhere
    other than the screen page, so such values yield no link at all.
    """
    raw = os.environ.get("HERMES_PUBLIC_CALLBACK_ORIGIN", "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 — raises ValueError on a malformed port
    except ValueError:
        parts = None
    if (
        parts is None
        or parts.scheme not in {"https", "http"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        logger.warning("multitenancy: HERMES_PUBLIC_CALLBACK_ORIGIN is not a bare http(s) origin; no screen link")
        return None
    return f"{parts.scheme}://{parts.netloc}"


def profile_desktop_enabled(profile_home: Any) -> bool:
    """Same gate as the desktop toolsets: merged profile config + never the router."""
    if profile_home is None:
        return False
    from ..agent_real._core import _load_profile_config
    from ..desktop_sandbox import desktop_enabled_for_profile

    home = Path(profile_home).expanduser()
    return desktop_enabled_for_profile(_load_profile_config(home), home)


def screen_url_for_profile(profile_home: Any) -> Optional[str]:
    """``<origin>/#/hermes/screen`` when ``profile_home`` has desktop enabled, else ``None``."""
    if not profile_desktop_enabled(profile_home):
        return None
    origin = _public_origin()
    return origin + SCREEN_ROUTE if origin else None


def mark_card_desktop_screen(adapter: Any, message_id: Any, profile_home: Any) -> None:
    """Resolve the desktop flag and screen link once for the card ``message_id``.

    Idempotent per card, so callers may invoke it on card start and again on
    tool events (covers a card whose message_id rolled mid-turn). Fail-open: a
    config read error leaves the card as a desktop-off card and never blocks
    delivery.
    """
    if not message_id:
        return
    state = _states(adapter).get(str(message_id))
    if state is None or ENABLED_STATE_KEY in state:
        return
    try:
        enabled = profile_desktop_enabled(profile_home)
    except Exception as exc:
        logger.warning("multitenancy: desktop card flag resolution failed: %s", exc)
        enabled = False
    origin = _public_origin() if enabled else None
    state[ENABLED_STATE_KEY] = enabled
    state[URL_STATE_KEY] = origin + SCREEN_ROUTE if origin else None
