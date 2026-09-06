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

Pending-activation hint (2026-08-25, skill-install-visible-in-session)
----------------------------------------------------------------------
Sandbox shared-skill binds are computed once at agent spawn, so a skill
installed/granted MID-session shows up in the profile ``skills/`` dir as a
dangling symlink (or an empty bwrap stub dir) that core silently skips —
users then see "面板有 / agent 没有" and file tickets (zhaofanrong 2026-08-25).
The same wrapper therefore also detects those entries and surfaces them:
``skills_list`` gains a ``pending_activation`` block and ``skill_view`` on a
pending name explains itself instead of returning a bare "not found". With no
pending entries both outputs are returned byte-identical.
"""
from __future__ import annotations

import functools
import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# Agent-facing explanation for an installed-but-not-yet-mounted skill.
PENDING_NOTE = "已安装，但内容尚未挂载到当前会话——新开一个会话后生效"

_PENDING_FALLBACK_EXCLUDED = frozenset({"__pycache__", "node_modules", ".git"})


def _dir_has_skill_md(path: Path) -> bool:
    try:
        if (path / "SKILL.md").is_file():
            return True
        return any(True for _ in path.rglob("SKILL.md"))
    except OSError:
        return False


def _entry_pending(entry: Path) -> bool:
    """Installed-but-unusable entry: dangling symlink, or dir with no SKILL.md."""
    try:
        if entry.is_symlink():
            if not entry.exists():
                return True  # dangling — target not mounted in this sandbox
            return entry.is_dir() and not _dir_has_skill_md(entry)
        if entry.is_dir():
            return not _dir_has_skill_md(entry)
    except OSError:
        return False
    return False


def _scan_pending_skills(skills_dir: Path, excluded: frozenset[str] = frozenset()) -> list[str]:
    """Relative names (depth ≤ 2, matching managed install layouts) of pending entries.

    A real dir that has nested SKILL.md files is a category dir — recurse one
    level so ``Keep/<name>`` style installs are covered too. An empty category
    dir is indistinguishable from an install stub and is reported as pending
    (rare, harmless).
    """
    skip = excluded | _PENDING_FALLBACK_EXCLUDED
    pending: list[str] = []
    try:
        entries = sorted(skills_dir.iterdir())
    except OSError:
        return pending
    for entry in entries:
        name = entry.name
        if name.startswith(".") or name in skip:
            continue
        try:
            if entry.is_file() and not entry.is_symlink():
                continue
            if _entry_pending(entry):
                pending.append(name)
                continue
            if entry.is_dir() and not entry.is_symlink() and not (entry / "SKILL.md").is_file():
                # category dir — check its direct children
                for child in sorted(entry.iterdir()):
                    if child.name.startswith(".") or child.name in skip:
                        continue
                    if child.is_file() and not child.is_symlink():
                        continue
                    if _entry_pending(child):
                        pending.append(f"{name}/{child.name}")
        except OSError:
            continue
    return pending

_PATCH_FLAG = "_hermes_mt_dynamic_skills_dir"
_WRAP_FLAG = "_hermes_mt_dynamic_wrapped"
# Serialize "refresh SKILLS_DIR + run the resolver" so two concurrent callers
# with different HERMES_HOME can't cross-contaminate the shared module global
# (A refreshes to A, B refreshes to B, A reads B's dir). Re-entrant because
# skills_list -> _find_all_skills are both wrapped and nest within one call.
# Skill resolution is not a hot path (cron ticks + occasional skill loads), so
# process-wide serialization here is cheap. NOTE: HERMES_HOME itself is a
# process-global env var; this lock makes the wrapped reads consistent, but a
# fully context-isolated resolution would require core to read the dir per-call
# (which we will not fork). In practice the only in-gateway-process caller is
# the serial cron tick — agent skill loads run in per-profile subprocesses.
_resolution_lock = threading.RLock()
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

    def _pending_names() -> list[str]:
        excluded = getattr(st, "_EXCLUDED_SKILL_DIRS", None)
        excluded = frozenset(excluded) if excluded else frozenset()
        return _scan_pending_skills(Path(st.SKILLS_DIR), excluded)

    def _augment_skills_list(result):
        # Byte-identical passthrough unless something is actually pending.
        try:
            pending = _pending_names()
            if not pending:
                return result
            data = json.loads(result)
            if not isinstance(data, dict):
                return result
            data["pending_activation"] = [
                {"name": name, "note": PENDING_NOTE} for name in pending
            ]
            return json.dumps(data, ensure_ascii=False)
        except Exception:
            logger.debug("[multitenancy] pending-activation augment failed", exc_info=True)
            return result

    def _augment_skill_view(result, args, kwargs):
        try:
            data = json.loads(result)
            if not isinstance(data, dict) or data.get("success") is not False:
                return result
            name = args[0] if args else kwargs.get("name")
            if not name:
                return result
            req = str(name)
            req_base = req.rsplit("/", 1)[-1]
            for rel in _pending_names():
                if req == rel or req_base == rel.rsplit("/", 1)[-1]:
                    return json.dumps(
                        {
                            "success": False,
                            "error": f"Skill '{name}' {PENDING_NOTE}",
                            "pending_activation": True,
                        },
                        ensure_ascii=False,
                    )
            return result
        except Exception:
            logger.debug("[multitenancy] pending-activation augment failed", exc_info=True)
            return result

    def _wrap(orig, fname):
        @functools.wraps(orig)
        def wrapper(*args, **kwargs):
            with _resolution_lock:
                _refresh_skills_dir()
                result = orig(*args, **kwargs)
                if fname == "skills_list":
                    return _augment_skills_list(result)
                if fname == "skill_view":
                    return _augment_skill_view(result, args, kwargs)
                return result

        setattr(wrapper, _WRAP_FLAG, True)
        return wrapper

    wrapped = 0
    for fname in _WRAPPED_FUNCS:
        orig = getattr(st, fname, None)
        if orig is None or getattr(orig, _WRAP_FLAG, False):
            continue
        setattr(st, fname, _wrap(orig, fname))
        wrapped += 1

    setattr(st, _PATCH_FLAG, True)
    logger.info(
        "[multitenancy] dynamic SKILLS_DIR patch installed on tools.skills_tool (%d entry point(s))",
        wrapped,
    )
