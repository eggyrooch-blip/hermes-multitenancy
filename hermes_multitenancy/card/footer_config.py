"""Footer observability configuration.

Reads the ``FOOTER_SHOW_METRICS`` environment variable to decide whether
the final card's done footer should include LLM observability fields
(tokens, cache hit %, context %, model name) alongside the elapsed timer.

Default ``False`` so production cards stay clean and only UAT / dev runs
that explicitly opt-in via ``FOOTER_SHOW_METRICS=1`` expose the metrics.
"""
from __future__ import annotations

import os

_ENV_NAME = "FOOTER_SHOW_METRICS"
_TRUTHY = {"1", "true", "yes", "on"}


def get_show_metrics() -> bool:
    """Return True iff ``FOOTER_SHOW_METRICS`` env is set to a truthy value."""
    value = os.environ.get(_ENV_NAME, "")
    return value.strip().lower() in _TRUTHY
