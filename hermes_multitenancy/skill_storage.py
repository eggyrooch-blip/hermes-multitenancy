"""Profile-scoped secret storage for skills (档 A — token isolation).

Skills that need to cache OAuth tokens, API keys, refresh tokens, or any
short-term secret MUST route through this module rather than the ad-hoc
``Path.home() / ".cache" / <skill>`` pattern that ships with most upstream
skills. Doing so guarantees:

  * Every cached secret lives at ``<HERMES_HOME>/tokens/<skill>.<ext>``, so a
    single audit (``ls`` or ``find``) can enumerate the entire per-profile
    token surface. Cross-profile reads are blocked by ``chmod 0700`` on the
    profile root (applied at provision time by
    :func:`hermes_multitenancy.sync.feishu_org._sync_one_profile`).
  * Skill names are slugged and validated so a malicious or accidental
    ``"../../evil"`` cannot escape the ``tokens/`` directory.
  * Files are created ``chmod 0600`` with atomic ``tempfile + os.replace``
    writes so a crash mid-write never leaves a half-written secret on disk.

Lookup order for the profile home:

  1. ``profile_home`` keyword argument (explicit caller intent).
  2. ``HERMES_HOME`` environment variable (the canonical anchor — set by
     :func:`hermes_multitenancy.agent_real._build_subprocess_env` for every
     spawned tool subprocess).

There is *intentionally* no ``~/.hermes`` default. A skill that reaches this
module without HERMES_HOME set is misconfigured, not "running outside a
profile", and we raise rather than silently write to a shared location.

Example
-------

>>> from hermes_multitenancy.skill_storage import write_token, read_token
>>> write_token("google_drive", json.dumps({"refresh_token": "..."}))
>>> read_token("google_drive")
'{"refresh_token": "..."}'
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Skill identifier slug pattern. Allows lowercase letters, digits, dashes and
# underscores. Conservative on purpose — anything outside this set is rejected
# rather than silently coerced, so a typo surfaces immediately instead of
# masking a path-traversal attempt.
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

_TOKENS_DIRNAME = "tokens"


class SkillStorageError(ValueError):
    """Raised when skill_storage cannot resolve a safe path."""


def _resolve_profile_home(profile_home: Optional[Path]) -> Path:
    if profile_home is not None:
        return Path(profile_home).expanduser()
    raw = os.environ.get("HERMES_HOME")
    if not raw:
        raise SkillStorageError(
            "skill_storage: HERMES_HOME is not set; cannot resolve profile-scoped "
            "token path. Either pass profile_home= explicitly or run inside an "
            "AIAgent subprocess (which sets HERMES_HOME per profile)."
        )
    return Path(raw).expanduser()


def _validate_skill_name(skill_name: str) -> str:
    """Strip whitespace and validate against the slug pattern.

    Deliberately does NOT lowercase the input — case-folding would silently
    map ``"Google_Drive"`` and ``"google_drive"`` to the same token file,
    which is a footgun (different skills, same secret). Callers must pass
    the lowercase slug they actually want.
    """
    if not isinstance(skill_name, str):
        raise SkillStorageError(f"skill name must be str, got {type(skill_name).__name__}")
    normalised = skill_name.strip()
    if not _SKILL_NAME_RE.match(normalised):
        raise SkillStorageError(
            f"skill name {skill_name!r} is not a valid slug; expected "
            "[a-z0-9][a-z0-9._-]{0,62} (lowercase, no path separators, no leading dot)."
        )
    return normalised


def _validate_extension(extension: str) -> str:
    if not extension or not re.fullmatch(r"[a-z0-9]{1,8}", extension):
        raise SkillStorageError(
            f"extension {extension!r} must match [a-z0-9]{{1,8}} (no dot, no slash)."
        )
    return extension


def _tokens_dir(profile_home: Optional[Path]) -> Path:
    home = _resolve_profile_home(profile_home)
    tokens = home / _TOKENS_DIRNAME
    tokens.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Re-tighten in case the dir existed at a looser mode.
    try:
        os.chmod(tokens, 0o700)
    except OSError:  # pragma: no cover — non-POSIX FS
        pass
    return tokens


def get_token_path(
    skill_name: str,
    *,
    extension: str = "json",
    profile_home: Optional[Path] = None,
) -> Path:
    """Return ``<profile>/tokens/<skill>.<ext>`` without creating the file.

    The parent ``tokens/`` directory is created (mode 0700) so callers can
    open the returned path for writing immediately. The file itself is not
    created — use :func:`write_token` to materialise it.
    """
    skill = _validate_skill_name(skill_name)
    ext = _validate_extension(extension)
    return _tokens_dir(profile_home) / f"{skill}.{ext}"


def write_token(
    skill_name: str,
    content: str,
    *,
    extension: str = "json",
    profile_home: Optional[Path] = None,
) -> Path:
    """Atomically write ``content`` to ``<profile>/tokens/<skill>.<ext>``.

    The file is created mode 0600. Write is atomic on POSIX via
    ``tempfile + os.replace`` so a crash mid-write never leaves a corrupted
    secret on disk. Returns the final path.
    """
    if not isinstance(content, str):
        raise SkillStorageError(f"content must be str, got {type(content).__name__}")
    target = get_token_path(skill_name, extension=extension, profile_home=profile_home)
    # NamedTemporaryFile in the same directory guarantees os.replace stays on
    # the same filesystem (cross-device replace would fail with EXDEV).
    fd, tmp_path = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, target)
    except Exception:
        # Best-effort cleanup of the temp on failure; re-raise.
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    logger.debug("[skill_storage] wrote token skill=%s path=%s bytes=%d", skill_name, target, len(content))
    return target


def read_token(
    skill_name: str,
    *,
    extension: str = "json",
    profile_home: Optional[Path] = None,
) -> Optional[str]:
    """Return the token content for *skill_name*, or ``None`` if absent."""
    target = get_token_path(skill_name, extension=extension, profile_home=profile_home)
    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def delete_token(
    skill_name: str,
    *,
    extension: str = "json",
    profile_home: Optional[Path] = None,
) -> bool:
    """Remove the token file for *skill_name*. Returns ``True`` if removed."""
    target = get_token_path(skill_name, extension=extension, profile_home=profile_home)
    try:
        target.unlink()
        logger.debug("[skill_storage] deleted token skill=%s", skill_name)
        return True
    except FileNotFoundError:
        return False


def list_skills_with_tokens(profile_home: Optional[Path] = None) -> list[str]:
    """Return a sorted list of skill names that currently have a stored token.

    Names are returned without their extension. A skill with multiple
    extensions appears once per extension (e.g. ``["google_drive", "google_drive.refresh"]``).
    """
    tokens = _tokens_dir(profile_home)
    names: list[str] = []
    for entry in tokens.iterdir():
        if entry.is_file():
            # Strip the trailing .<ext>; keep any earlier dots intact (some
            # skills layer compound names like "google_drive.refresh").
            stem = entry.name.rsplit(".", 1)[0]
            if stem:
                names.append(stem)
    return sorted(set(names))
