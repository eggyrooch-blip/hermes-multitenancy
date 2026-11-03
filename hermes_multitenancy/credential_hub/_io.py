"""Small fs/subprocess/time IO helpers for the credential hub.

Pure utilities with no dependency on the readers or the models — safe to import
from anywhere in the package. ``_run`` is monkeypatched by the test-suite via the
``credential_hub`` package namespace, so callers in sibling modules must resolve
it through the package object (see the ``_hub`` indirection in the readers).
"""
from __future__ import annotations
from hermes_multitenancy import credential_hub as _hub  # route patchable helpers via package namespace

import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes_multitenancy.credential_hub")

_SUBPROCESS_TIMEOUT = 10  # seconds — matches the WebUI execFile timeouts


def _now_ms() -> int:
    return int(time.time() * 1000)


def _normalize_epoch_ms(value: Any) -> Optional[int]:
    """Parse an epoch value to milliseconds.

    Accepts ms (13-digit) or seconds (10-digit) — keep-record writes
    ``keep_auth_token_expired`` in seconds, so values < 1e12 are scaled to ms.
    """
    if value in (None, ""):
        return None
    try:
        n = int(float(value))
    except (ValueError, TypeError):
        return None
    if n <= 0:
        return None
    return n * 1000 if n < 1_000_000_000_000 else n


def profile_root(shared_home: Path, profile_name: str) -> Path:
    """``<shared_home>/profiles/<profile_name>`` — the profile root dir."""
    return Path(shared_home) / "profiles" / str(profile_name)


def profile_home_dir(shared_home: Path, profile_name: str) -> Path:
    """The HOME-redirect dir where per-profile tools drop their dotfiles."""
    return _hub.profile_root(shared_home, profile_name) / "home"


def _read_small_text(path: Path, *, max_bytes: int = 64 * 1024) -> str:
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return ""
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _safe_account(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _run(cmd: list[str], *, cwd: Optional[Path] = None, env: Optional[dict[str, str]] = None) -> Optional[subprocess.CompletedProcess]:
    """Run a guarded subprocess. Returns CompletedProcess or None on failure."""
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("credential_hub: subprocess failed (%s): %s", cmd[:1], exc)
        return None


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    text = _hub._read_small_text(path)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out
