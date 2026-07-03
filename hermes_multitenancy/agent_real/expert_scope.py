from __future__ import annotations

import sys as _sys
_pkg = _sys.modules["hermes_multitenancy.agent_real"]

import json
import logging
import os
import sys
import time
import hashlib
import tempfile
import uuid
import re
import secrets
import importlib
import threading
from contextlib import closing, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional


_EXPERT_SKILL_SCOPE_LOCK = threading.RLock()


def _apply_expert_skill_scope_for_aiagent(event: Any, profile_home: Path):
    """Scope skills for this run to 能力随专家私有 by HIDING every non-active expert
    skill, with ZERO hermes-agent change.

    Expert skills are installed into the profile ``skills/`` scan root by the
    ingester (``plugin_ingest._install_skills_to_profile``; ``assert_profile_governance``
    REQUIRES the entry+orchestrate skills there). So the ACTIVE expert's own skills
    are already resolvable — we only need to HIDE the others. Purely subtractive via
    two subprocess-local monkeypatches (the run is a fresh
    ``asyncio.create_subprocess_exec`` child, so these module-global patches can
    never leak across the 1279 profiles):

      * ``get_disabled_skill_names`` (skill_utils source + the prompt_builder /
        skill_commands bound imports) — hides them from the system-prompt CATALOG.
        Works on UPSTREAM/prod core, which reads disabled skills from
        ``config.yaml`` only and IGNORES ``HERMES_DISABLED_SKILLS_EXTRA`` (that env
        is honored solely by the abandoned fork; relying on it silently leaks).
      * ``tools.skills_tool._is_skill_disabled`` — the ``skill_view`` INVOCATION
        gate (reads config.yaml directly, NOT get_disabled_skill_names), so a hidden
        skill cannot be LOADED/EXECUTED, not merely un-advertised.

    On a non-expert run every expert skill is hidden → byte-identical catalog AND
    not invocable. The active expert's own skills stay visible (in the scan root,
    absent from the hidden set). NO additive temp-root is used: the skills are
    already in the scan root, so adding a copy would make ``skill_view`` raise
    "Ambiguous skill name" and break the active expert's own skill loading.

    Returns a cleanup restoring all patches; a true no-op when there is nothing to
    hide (no expert manifests installed → non-expert byte-identical path).

    DOCUMENTED RESIDUAL: the raw ``Read`` tool can still read a hidden skill's
    ``SKILL.md`` from ``<profile>/skills/``. True file-level isolation would require
    not co-installing expert skills into non-audience profiles — out of this
    0-core-change seam. Mitigated operationally by audience-scoped ingest (the plugin
    lands only in its audience profiles).
    """
    disabled = _expert_disabled_skill_names_for_event(event, profile_home)
    if not disabled:
        return lambda: None

    _EXPERT_SKILL_SCOPE_LOCK.acquire()
    try:
        import agent.skill_utils as skill_utils
    except Exception:
        logger.warning("[multitenancy] could not patch expert skill scope into Hermes core", exc_info=True)
        _EXPERT_SKILL_SCOPE_LOCK.release()
        return lambda: None

    _hide = frozenset(disabled)
    patched_module_attrs: list[tuple[Any, str, Any]] = []
    original_disabled = None
    try:
        # ── catalog: hide from the system-prompt skill catalog ──
        original_disabled = getattr(skill_utils, "get_disabled_skill_names", None)

        def _with_expert_disabled(platform=None, *, _orig=original_disabled, _extra=_hide):
            base = set(_orig(platform) if callable(_orig) else set())
            return base | set(_extra)

        if callable(original_disabled):
            skill_utils.get_disabled_skill_names = _with_expert_disabled
        for module_name in ("agent.prompt_builder", "agent.skill_commands"):
            module = sys.modules.get(module_name)
            if module is None or not hasattr(module, "get_disabled_skill_names"):
                continue
            patched_module_attrs.append(
                (module, "get_disabled_skill_names", getattr(module, "get_disabled_skill_names"))
            )
            setattr(module, "get_disabled_skill_names", _with_expert_disabled)

        # ── invocation gate: skill_view → _is_skill_disabled (reads config.yaml directly,
        #    NOT get_disabled_skill_names) — force-import so the patch lands before use.
        try:
            import tools.skills_tool as skills_tool
        except Exception:
            skills_tool = None
        if skills_tool is not None and hasattr(skills_tool, "_is_skill_disabled"):
            _orig_is_disabled = skills_tool._is_skill_disabled

            def _with_expert_is_disabled(name, platform=None, *, _orig=_orig_is_disabled, _extra=_hide):
                if name in _extra:
                    return True
                try:
                    return bool(_orig(name, platform))
                except TypeError:
                    return bool(_orig(name))

            patched_module_attrs.append((skills_tool, "_is_skill_disabled", _orig_is_disabled))
            skills_tool._is_skill_disabled = _with_expert_is_disabled
    except Exception:
        logger.warning("[multitenancy] failed to patch expert skill scope into Hermes core", exc_info=True)
        try:
            if callable(original_disabled):
                skill_utils.get_disabled_skill_names = original_disabled
            for module, attr, original in patched_module_attrs:
                setattr(module, attr, original)
        finally:
            _EXPERT_SKILL_SCOPE_LOCK.release()
        return lambda: None

    logger.info(
        "[multitenancy] expert skill scope active profile=%s hide=%s",
        profile_home.name,
        sorted(disabled),
    )

    def _cleanup() -> None:
        try:
            if callable(original_disabled):
                skill_utils.get_disabled_skill_names = original_disabled
            for module, attr, original in patched_module_attrs:
                setattr(module, attr, original)
        finally:
            _EXPERT_SKILL_SCOPE_LOCK.release()

    return _cleanup
