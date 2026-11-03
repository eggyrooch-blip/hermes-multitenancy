"""Feishu CardKit streaming compatibility — modularized.

Mirrors openclaw-lark `src/card/` layout. External entry point is
`ensure_feishu_cardkit_streaming` — re-exported here for compatibility
with the legacy `hermes_multitenancy.feishu_cardkit_compat` import path.
"""
from __future__ import annotations

from .streaming_controller import (
    ensure_feishu_cardkit_streaming,
    supports_streaming_closed_recovery,
)

__all__ = ["ensure_feishu_cardkit_streaming", "supports_streaming_closed_recovery"]
