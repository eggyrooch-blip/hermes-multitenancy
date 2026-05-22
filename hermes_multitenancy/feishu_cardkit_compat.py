"""Feishu CardKit streaming compatibility — backward-compatible re-export shim.

The implementation now lives under ``hermes_multitenancy.card.*``; this module
is preserved as a thin shim so existing callers
(``from hermes_multitenancy.feishu_cardkit_compat import ensure_feishu_cardkit_streaming``)
keep working without a touch to ``router.py`` / ``agent_real.py`` / tests.
"""
from __future__ import annotations

from hermes_multitenancy.card import ensure_feishu_cardkit_streaming
from hermes_multitenancy.card.builder import _render_cardkit_initial_card
from hermes_multitenancy.card.markdown_style import _optimize_markdown_style

__all__ = [
    "ensure_feishu_cardkit_streaming",
    "_render_cardkit_initial_card",
    "_optimize_markdown_style",
]
