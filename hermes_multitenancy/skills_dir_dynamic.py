"""Plugin-layer patch: make core skill resolution honor the CURRENT HERMES_HOME.

Why this exists
---------------
Core ``tools/skills_tool.py`` binds the skills root ONCE at import time::

    HERMES_HOME = get_hermes_home()
    SKILLS_DIR  = HERMES_HOME / "skills"

In the multitenancy router gateway the process imports skills_tool while
HERMES_HOME points at the ``multitenancy_router`` profile. So every skill lookup
(``skill_view`` / ``skills_list``) forever searches ``<router>/skills`` — no
matter which profile's cron job or request is actually running. Owner-profile
skills (e.g. ``cuihuanyu/skills/.../social-daily-radar``,
``songtingting/skills/.../daily-management-wisdom``) are therefore never found,
and ``cron.scheduler`` logs "skill not found, skipping". ``cron_profile_scope``
rebinds HERMES_HOME + the cron storage globals per profile but NOT this frozen
``SKILLS_DIR`` (same module-global-frozen-at-import bug class as the 2026-06-16
broker deadlock).

The fix (no core fork — same monkeypatch discipline as feishu_reply_quote_api /
feishu_merge_forward_api)
---------------------------------------------------------------------------
Wrap the skills_tool entry points so each call refreshes the module global
``skills_tool.SKILLS_DIR = get_hermes_home() / "skills"`` BEFORE running. Core's
internal bare-global references (``if SKILLS_DIR.exists()`` etc.) then read the
freshly-resolved, profile-correct value. ``get_hermes_home()`` reads the
context-local override / HERMES_HOME env (pure in-memory when HERMES_HOME is set,
which it always is in the gateway), so resolution follows whichever profile is
active for this tick/request.

NOT done via ``del SKILLS_DIR`` + a module ``__getattr__``: PEP 562 module
``__getattr__`` is only consulted for *external* attribute access
(``skills_tool.SKILLS_DIR``), NOT for the bare-global name lookups inside
skills_tool's own functions — deleting the attribute would make
``skill_view`` raise ``NameError`` internally. Refresh-on-entry avoids that.

Idempotent — safe to call from plugin ``register()``.
"""
from __future__ import annotations

import functools
import logging

logger = logging.getLogger(__name__)

_PATCH_FLAG = "_hermes_mt_dynamic_skills_dir"
_WRAP_FLAG = "_hermes_mt_dynamic_wrapped"
# Public + internal entry points whose bare-global SKILLS_DIR references must
# see the live profile dir. skills_list delegates to _find_all_skills, so both
# are wrapped (the inner call re-hits the wrapper — a harmless double refresh).
_WRAPPED_FUNCS = ("skill_view", "skills_list", "_find_all_skills")


def install_dynamic_skills_dir_patch() -> None:
    """Make tools.skills_tool resolve SKILLS_DIR from the current HERMES_HOME."""
    try:
        import tools.skills_tool as st  # type: ignore
    except Exception:
        logger.info(
            "[multitenancy] tools.skills_tool not importable yet; dynamic SKILLS_DIR patch deferred"
        )
        return

    if getattr(st, _PATCH_FLAG, False):
        return

    get_home = getattr(st, "get_hermes_home", None)
    if get_home is None:
        try:
            from hermes_constants import get_hermes_home as get_home  # type: ignore
        except Exception:
            logger.warning(
                "[multitenancy] get_hermes_home unavailable; dynamic SKILLS_DIR patch skipped"
            )
            return

    def _refresh_skills_dir() -> None:
        try:
            st.SKILLS_DIR = get_home() / "skills"
        except Exception:
            logger.debug("[multitenancy] dynamic SKILLS_DIR refresh failed", exc_info=True)

    def _wrap(orig):
        @functools.wraps(orig)
        def wrapper(*args, **kwargs):
            _refresh_skills_dir()
            return orig(*args, **kwargs)

        setattr(wrapper, _WRAP_FLAG, True)
        return wrapper

    wrapped = 0
    for fname in _WRAPPED_FUNCS:
        orig = getattr(st, fname, None)
        if orig is None or getattr(orig, _WRAP_FLAG, False):
            continue
        setattr(st, fname, _wrap(orig))
        wrapped += 1

    setattr(st, _PATCH_FLAG, True)
    logger.info(
        "[multitenancy] dynamic SKILLS_DIR patch installed on tools.skills_tool (%d entry point(s))",
        wrapped,
    )
