"""Per-profile podman desktop sandbox for Bot Screen / computer_use / browser.

A profile that sets ``multitenancy.desktop.enabled: true`` no longer runs its
AIAgent worker under bwrap. Instead the gateway keeps one long-lived podman
container per profile (``hermes-p-<hash>``) that carries the desktop image
(Xvnc + Xfce + Chromium + cua-driver) and runs every worker command inside it
via ``podman exec -i``. The stdin/stdout JSON protocol between gateway and
worker is unchanged; only the wrapper around ``cmd`` differs.

Design facts this module encodes (verified on hermes-pre, rootful podman
4.4.1, 2026-10-07 — see the task SPEC "Plan 1 实测结论"):

* ``--userns=keep-id`` is rootless-only. Ownership is bridged with an explicit
  ``--uidmap/--gidmap`` that maps container uid/gid 10000 to the owner of
  PROFILE_HOME, and container root to an unprivileged subordinate range.
* The upstream image's s6 entrypoint needs root + capabilities; we bypass it
  (``--init --entrypoint /bin/sleep``) so PID 1 and every exec run as uid 10000
  with ``--cap-drop ALL``.
* Host paths are mounted at the SAME path inside the container, so the worker
  payload and env (HERMES_HOME, HOME, WORKSPACE, …) need no rewriting. Only
  ``cmd[0]`` (the gateway venv python) is mapped to the image's interpreter.
* Worker env reaches the container through ``podman exec -e KEY`` (no value):
  podman copies KEY from its own process env, so secrets never appear in argv.

Trust boundary (review round 2026-10-07):

* Everything that controls the HOST runtime (which podman binary runs as the
  gateway user, which image, which network, quotas, id maps, exec env) is read
  from the gateway-owned SHARED config only. The profile's own ``config.yaml``
  lives inside the tenant-writable tree that is mounted read-write into the
  container, so it may only flip ``enabled`` and shorten ``idle_stop_minutes``.
* Lifecycle state (last use, active turns, locks) lives under
  ``<SHARED_HOME>/desktop-state/<hash>/``, which is never mounted into any
  container. The gateway never writes through a tenant-controlled path, and the
  few tenant paths it has to create (mask targets) are opened ``O_NOFOLLOW``.

This module never falls back to a bare host exec. Every failure raises
:class:`DesktopSandboxError` with a stable ``reason`` that the caller turns into
a ``sandbox.denied`` security event.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import stat as stat_module
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Optional


logger = logging.getLogger(__name__)

ROUTER_PROFILE = "multitenancy_router"
CONTAINER_PREFIX = "hermes-p-"
CONTAINER_UID = 10000
CONTAINER_GID = 10000
DEFAULT_IMAGE = "localhost/hermes-multitenancy/desktop:v2026.9.24-20261007"
DEFAULT_NETWORK = "hermes-desktop"
DEFAULT_IDLE_STOP_MINUTES = 30
DEFAULT_MAX_CONTAINERS = 60
DEFAULT_SUBID_BASE = 300000
DEFAULT_CONTAINER_PYTHON = "/opt/hermes/.venv/bin/python"
DEFAULT_CONTAINER_HERMES = "/opt/hermes/.venv/bin/hermes"
DEFAULT_CONTAINER_PATH = (
    "/opt/hermes/bin:/opt/hermes/.venv/bin:/usr/local/sbin:/usr/local/bin:"
    "/usr/sbin:/usr/bin:/sbin:/bin"
)
#: SPEC budgets: container Up ≤ 30s, screen ready ≤ 20s. Overrun fails closed.
DEFAULT_ENSURE_TIMEOUT_S = 30.0
DEFAULT_SCREEN_TIMEOUT_S = 20.0
CONTAINER_RUNTIME_DIR = "/tmp/hermes-runtime"
DEFAULT_EXEC_ENV: dict[str, str] = {
    "XDG_RUNTIME_DIR": CONTAINER_RUNTIME_DIR,
}
#: Host env names that must not cross into the container verbatim.
#: PATH is rebuilt for the image; the CA-bundle names point at host paths that
#: do not exist in the Debian image.
EXEC_ENV_EXCLUDE = frozenset({
    "PATH",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
})
#: Image-owned names inherit its ENV unless ``desktop.exec_env`` overrides them.
IMAGE_EXEC_ENV = frozenset({
    "HERMES_CUA_DRIVER_CMD",
    "AGENT_BROWSER_ARGS",
    "AGENT_BROWSER_EXECUTABLE_PATH",
})
#: Profile subdirectories masked with a private tmpfs for the container's whole
#: life (same list bwrap uses for local-harness runs).
TMPFS_PROFILE_SUBDIRS = ("feishu_uat", "tokens", "workspace/credentials", "home")
#: Every tenant-tree path the gateway creates before ``podman run`` (mask
#: targets + the directories the worker and Bot Screen write into). Order
#: matters: a parent is validated before its child.
PROFILE_DIRS = ("workspace", "workspace/credentials", "feishu_uat", "tokens", "home", "bot-desktop", "skills")
PROFILE_FILES = (".env", "auth.json")
#: Shared-home roots that installed profile skills may symlink into.
SHARED_SKILL_ROOTS = (
    "skills",
    "skill-releases",
    "_managed/aidock-skillhub",
    ".hermes-plugin-managed/.sources",
)
#: ``multitenancy.desktop`` keys that control the host runtime. They are read
#: from the shared config ONLY; a profile config that carries them is ignored
#: with a warning (the profile tree is tenant-writable).
HOST_ONLY_KEYS = frozenset({
    "podman_bin",
    "network",
    "image",
    "subid_base",
    "max_containers_per_host",
    "memory",
    "cpus",
    "shm_size",
    "exec_env",
    "container_python",
    "container_hermes",
    "container_path",
    "ensure_timeout_s",
    "screen_timeout_s",
})
#: The only ``multitenancy.desktop`` keys a profile config may set.
PROFILE_KEYS = frozenset({"enabled", "idle_stop_minutes"})

LABEL_ROLE = "io.hermes.mt.role"
LABEL_ROLE_VALUE = "desktop"
LABEL_PROFILE = "io.hermes.mt.profile"
LABEL_PROFILE_HOME = "io.hermes.mt.profile_home"
LABEL_SPEC = "io.hermes.mt.spec"
LABEL_IMAGE_ID = "io.hermes.mt.image_id"
LABEL_SHARED_CONFIG = "io.hermes.mt.shared_config"
RFB_SOCKET_RELPATH = Path("bot-desktop") / "rfb.sock"
STATE_DIRNAME = "desktop-state"
HOST_LOCK_NAME = "host.lock"
PROFILE_LOCK_NAME = "profile.lock"
TURNS_LOCK_NAME = "turns.lock"
LAST_USED_NAME = "last_used"
TURNS_NAME = "turns.json"
META_NAME = "meta.json"
QUOTA_USER_MESSAGE = "桌面名额已满，稍后再试"
#: A turn lease older than this is treated as leaked (gateway crashed mid-turn)
#: so a container can still become idle.
ACTIVE_TURN_MAX_S = 3 * 3600
#: How long an ensure() waits for the same profile's in-flight ensure().
PROFILE_LOCK_WAIT_S = DEFAULT_ENSURE_TIMEOUT_S + DEFAULT_SCREEN_TIMEOUT_S + 10.0
#: How long an ensure() waits for the host-wide quota section.
HOST_LOCK_WAIT_S = DEFAULT_ENSURE_TIMEOUT_S + 5.0
TURNS_LOCK_WAIT_S = 5.0

_TRUTHY = {"1", "true", "yes", "on", "enabled"}
_PODMAN_TIMEOUT_S = 60.0
_NO_SUCH_CONTAINER_MARKERS = ("no such container", "no such object", "does not exist")


class DesktopSandboxError(RuntimeError):
    """A desktop container could not be provided. Never fall back to bare exec."""

    def __init__(self, reason: str, message: str, *, user_message: str | None = None):
        super().__init__(message)
        self.reason = reason
        self.user_message = user_message


@dataclass(frozen=True)
class DesktopDecision:
    enabled: bool
    reason: str
    profile_home: Path
    profile_name: str
    shared_home: Path
    image: str = DEFAULT_IMAGE
    podman_bin: str = "podman"
    network: str = DEFAULT_NETWORK
    idle_stop_minutes: int = DEFAULT_IDLE_STOP_MINUTES
    max_containers_per_host: int = DEFAULT_MAX_CONTAINERS
    subid_base: int = DEFAULT_SUBID_BASE
    container_python: str = DEFAULT_CONTAINER_PYTHON
    container_hermes: str = DEFAULT_CONTAINER_HERMES
    container_path: str = DEFAULT_CONTAINER_PATH
    exec_env: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_EXEC_ENV))
    memory: str = "3g"
    cpus: str = "2"
    shm_size: str = "1g"
    ensure_timeout_s: float = DEFAULT_ENSURE_TIMEOUT_S
    screen_timeout_s: float = DEFAULT_SCREEN_TIMEOUT_S
    #: Host-only keys the profile config tried to set (ignored, for diagnostics).
    ignored_profile_keys: tuple[str, ...] = ()

    @property
    def container_name(self) -> str:
        return container_name(self.profile_home)

    @property
    def rfb_socket(self) -> Path:
        return self.profile_home / RFB_SOCKET_RELPATH

    @property
    def state_dir(self) -> Path:
        return profile_state_dir(self.shared_home, self.profile_home)

    def spec_hash(self, owner_uid: int, owner_gid: int) -> str:
        """Digest of everything a running container must have been created with."""
        spec = {
            "image": self.image,
            "network": self.network,
            "subid_base": self.subid_base,
            "owner": [owner_uid, owner_gid],
            "memory": self.memory,
            "cpus": self.cpus,
            "shm_size": self.shm_size,
            "container_python": self.container_python,
            "container_hermes": self.container_hermes,
            "container_path": self.container_path,
            "exec_env": sorted(self.exec_env.items()),
            "tmpfs": list(TMPFS_PROFILE_SUBDIRS),
        }
        return hashlib.sha256(json.dumps(spec, sort_keys=True).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ContainerHandle:
    name: str
    started: bool
    screen_started: bool
    #: Absolute podman path resolved by ensure(); exec_args() reuses it so the
    #: worker exec never re-resolves ``podman`` through PATH.
    podman_bin: str


@dataclass(frozen=True)
class _ContainerInfo:
    status: str
    labels: dict[str, str]
    image_id: str


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in _TRUTHY


def _as_int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _as_float(value: Any, default: float) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def desktop_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return ``multitenancy.desktop`` from one config mapping (shared or profile)."""
    if not isinstance(config, Mapping):
        return {}
    mt_cfg = config.get("multitenancy") or {}
    if not isinstance(mt_cfg, Mapping):
        return {}
    desktop_cfg = mt_cfg.get("desktop") or {}
    return dict(desktop_cfg) if isinstance(desktop_cfg, Mapping) else {}


def desktop_enabled_in_config(config: Mapping[str, Any] | None) -> bool:
    """Config-only check used by browser_policy (no profile-name semantics).

    ``enabled`` is a profile-allowed key, so the merged (shared + profile) config
    is the right input here.
    """
    return _truthy(desktop_config(config).get("enabled"))


def desktop_enabled_for_profile(config: Mapping[str, Any] | None, profile_home: Path) -> bool:
    """Whether ``profile_home`` gets desktop toolsets: merged ``enabled`` + not the router."""
    if Path(profile_home).expanduser().name == ROUTER_PROFILE:
        return False
    return desktop_enabled_in_config(config)


def _profile_digest(profile_home: Path) -> str:
    return hashlib.sha256(str(profile_home).encode("utf-8")).hexdigest()


def container_name(profile_home: Path) -> str:
    """``hermes-p-<sha256(canonical profile path)[:12]>``.

    The readable profile name goes into :data:`LABEL_PROFILE`. Hashing the
    canonical path keeps two profiles with special characters (or two gateways
    with different shared homes on one host) from colliding on a lossy
    sanitized name.
    """
    return f"{CONTAINER_PREFIX}{_profile_digest(Path(profile_home).expanduser().resolve())[:12]}"


def _resolve_shared_home(profile_home: Path) -> Path:
    explicit = os.getenv("HERMES_SHARED_HOME")
    if explicit:
        return Path(explicit).expanduser()
    if profile_home.parent.name == "profiles":
        return profile_home.parent.parent
    return profile_home


def shared_home_from_env() -> Path | None:
    """The gateway's shared home for callers without a profile (the idle sweep)."""
    explicit = os.getenv("HERMES_SHARED_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    hermes_home = os.getenv("HERMES_HOME", "").strip()
    if not hermes_home:
        return None
    home = Path(hermes_home).expanduser()
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def host_desktop_settings(shared_home: Path) -> DesktopDecision:
    """Host-level desktop settings from the shared config alone (no profile).

    Used at gateway startup for the idle sweep, which needs the trusted
    ``podman_bin`` and nothing profile-specific. ``enabled`` is irrelevant here.
    """
    import yaml

    config: dict[str, Any] = {}
    path = Path(shared_home).expanduser() / "config.yaml"
    try:
        if path.exists():
            config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("[multitenancy] desktop host settings: shared config unreadable (%s); using defaults", exc)
        config = {}
    return _decision_from_sections(
        desktop_config(config), {}, Path(shared_home).expanduser() / "profiles" / "_host",
        profile_name="_host", enabled=True, reason="host settings",
    )


def _decision_from_sections(
    shared_cfg: Mapping[str, Any],
    profile_cfg: Mapping[str, Any],
    profile_home: Path,
    *,
    profile_name: str,
    enabled: bool,
    reason: str,
) -> DesktopDecision:
    ignored = tuple(sorted(key for key in profile_cfg if key in HOST_ONLY_KEYS))
    if ignored:
        logger.warning(
            "[multitenancy] desktop config: profile %s sets host-only keys %s in config.yaml; "
            "ignored (host runtime settings come from the shared config only)",
            profile_name, ",".join(ignored),
        )
    exec_env = dict(DEFAULT_EXEC_ENV)
    extra_env = shared_cfg.get("exec_env")
    if isinstance(extra_env, Mapping):
        for key, value in extra_env.items():
            name = str(key).strip()
            if name and name not in EXEC_ENV_EXCLUDE:
                exec_env[name] = str(value)

    shared_idle = _as_int(shared_cfg.get("idle_stop_minutes"), DEFAULT_IDLE_STOP_MINUTES)
    idle = shared_idle
    if "idle_stop_minutes" in profile_cfg:
        requested = _as_int(profile_cfg.get("idle_stop_minutes"), -1, minimum=-1)
        # A profile may only shorten the window. 0 means "never stop" and is
        # therefore never shorter; a value above the shared one is ignored.
        if requested > 0 and (shared_idle == 0 or requested <= shared_idle):
            idle = requested
        else:
            logger.warning(
                "[multitenancy] desktop config: profile %s idle_stop_minutes=%r ignored "
                "(must be >0 and <= shared %d)",
                profile_name, profile_cfg.get("idle_stop_minutes"), shared_idle,
            )

    return DesktopDecision(
        enabled=enabled,
        reason=reason,
        profile_home=profile_home,
        profile_name=profile_name,
        shared_home=_resolve_shared_home(profile_home),
        image=str(shared_cfg.get("image") or DEFAULT_IMAGE).strip() or DEFAULT_IMAGE,
        podman_bin=str(shared_cfg.get("podman_bin") or "podman").strip() or "podman",
        network=str(shared_cfg.get("network") or DEFAULT_NETWORK).strip() or DEFAULT_NETWORK,
        idle_stop_minutes=idle,
        max_containers_per_host=_as_int(
            shared_cfg.get("max_containers_per_host"), DEFAULT_MAX_CONTAINERS, minimum=1
        ),
        subid_base=_as_int(shared_cfg.get("subid_base"), DEFAULT_SUBID_BASE, minimum=65536),
        container_python=str(shared_cfg.get("container_python") or DEFAULT_CONTAINER_PYTHON).strip()
        or DEFAULT_CONTAINER_PYTHON,
        container_hermes=str(shared_cfg.get("container_hermes") or DEFAULT_CONTAINER_HERMES).strip()
        or DEFAULT_CONTAINER_HERMES,
        container_path=str(shared_cfg.get("container_path") or DEFAULT_CONTAINER_PATH).strip()
        or DEFAULT_CONTAINER_PATH,
        exec_env=exec_env,
        memory=str(shared_cfg.get("memory") or "3g").strip() or "3g",
        cpus=str(shared_cfg.get("cpus") or "2").strip() or "2",
        shm_size=str(shared_cfg.get("shm_size") or "1g").strip() or "1g",
        ensure_timeout_s=_as_float(shared_cfg.get("ensure_timeout_s"), DEFAULT_ENSURE_TIMEOUT_S),
        screen_timeout_s=_as_float(shared_cfg.get("screen_timeout_s"), DEFAULT_SCREEN_TIMEOUT_S),
        ignored_profile_keys=ignored,
    )


def desktop_decision(
    shared_config: Mapping[str, Any] | None,
    profile_home: Path,
    *,
    profile_config: Mapping[str, Any] | None = None,
    profile_name: str | None = None,
) -> DesktopDecision:
    """Decide whether ``profile_home`` runs inside a desktop container.

    ``shared_config`` is the gateway-owned ``<SHARED_HOME>/config.yaml`` and is
    the only source for host runtime settings. ``profile_config`` is the
    profile's own ``config.yaml`` (tenant-writable) and may only set
    ``enabled`` and a shorter ``idle_stop_minutes``.
    """
    profile_home = Path(profile_home).expanduser().resolve()
    profile_name = (profile_name or profile_home.name or "").strip()
    shared_cfg = desktop_config(shared_config)
    profile_cfg = desktop_config(profile_config)
    if profile_name == ROUTER_PROFILE:
        return DesktopDecision(
            enabled=False,
            reason="router profile never runs a desktop",
            profile_home=profile_home,
            profile_name=profile_name,
            shared_home=_resolve_shared_home(profile_home),
        )
    enabled = _truthy(profile_cfg["enabled"] if "enabled" in profile_cfg else shared_cfg.get("enabled"))
    if not enabled:
        return DesktopDecision(
            enabled=False,
            reason="profile desktop capability is disabled",
            profile_home=profile_home,
            profile_name=profile_name,
            shared_home=_resolve_shared_home(profile_home),
        )
    return _decision_from_sections(
        shared_cfg, profile_cfg, profile_home,
        profile_name=profile_name, enabled=True, reason="profile desktop capability enabled",
    )


def desktop_toolsets_for_policy(
    toolsets: list[str] | None,
    enabled: bool,
) -> list[str] | None:
    """Add ``computer_use`` + ``browser`` for a desktop-enabled profile.

    ``None`` means "core decides every toolset" and is left alone: turning it
    into a two-item list would strip the tenant down to just these tools.
    """
    if toolsets is None or not enabled:
        return toolsets
    items = {str(item).strip() for item in toolsets if str(item).strip()}
    items.update({"computer_use", "browser"})
    return sorted(items)


# --- podman plumbing (monkeypatched in tests) --------------------------------


def _run_podman(
    podman_bin: str,
    args: list[str],
    *,
    timeout: float = _PODMAN_TIMEOUT_S,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """Run one podman command on the host. Separate function so tests mock it."""
    return subprocess.run(
        [podman_bin, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _podman_available(podman_bin: str) -> str | None:
    """Return the resolved podman path, or None when it cannot be executed."""
    resolved = shutil.which(podman_bin) if os.sep not in podman_bin else podman_bin
    if not resolved or not os.access(resolved, os.X_OK):
        return None
    return resolved


def _unavailable(message: str) -> DesktopSandboxError:
    return DesktopSandboxError("desktop_container_unavailable", message)


def _remaining(deadline: float, what: str) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise _unavailable(f"{what}: budget exhausted before the call")
    return max(left, 1.0)


def _inspect_container(podman_bin: str, name: str, *, timeout: float = _PODMAN_TIMEOUT_S) -> _ContainerInfo | None:
    """Inspect ``name``. None ONLY when podman says the container does not exist.

    Any other failure (daemon down, transient error, malformed output) raises:
    treating it as "absent" would make the caller recreate over a healthy
    container.
    """
    try:
        proc = _run_podman(
            podman_bin,
            ["inspect", "--type", "container", "--format", "json", name],
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise _unavailable(f"podman inspect {name} failed: {exc}") from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if any(marker in stderr.lower() for marker in _NO_SUCH_CONTAINER_MARKERS):
            return None
        raise _unavailable(f"podman inspect {name} failed (exit={proc.returncode}): {stderr[-500:]}")
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise _unavailable(f"podman inspect {name} returned invalid JSON: {exc}") from exc
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    state = row.get("State") if isinstance(row.get("State"), dict) else {}
    config = row.get("Config") if isinstance(row.get("Config"), dict) else {}
    labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
    return _ContainerInfo(
        status=str(state.get("Status") or "").strip(),
        labels={str(k): str(v) for k, v in labels.items()},
        image_id=str(row.get("Image") or "").strip(),
    )


def _image_id(podman_bin: str, image: str, *, timeout: float) -> str:
    """Current ID of ``image`` on this host, or "" when it cannot be resolved."""
    try:
        proc = _run_podman(podman_bin, ["image", "inspect", "--format", "{{.Id}}", image], timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _running_desktop_containers(podman_bin: str) -> list[dict[str, Any]]:
    try:
        proc = _run_podman(
            podman_bin,
            ["ps", "--filter", f"label={LABEL_ROLE}={LABEL_ROLE_VALUE}", "--format", "json"],
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise _unavailable(f"podman ps failed: {exc}") from exc
    if proc.returncode != 0:
        raise _unavailable(f"podman ps failed (exit={proc.returncode}): {proc.stderr.strip()[-500:]}")
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise _unavailable(f"podman ps returned invalid JSON: {exc}") from exc
    return [row for row in rows if isinstance(row, dict)]


def _row_name(row: Mapping[str, Any]) -> str:
    names = row.get("Names")
    if isinstance(names, list) and names:
        return str(names[0])
    return str(row.get("Name") or "")


def _ensure_network(podman_bin: str, network: str, *, timeout: float) -> None:
    exists = _run_podman(podman_bin, ["network", "exists", network], timeout=timeout)
    if exists.returncode == 0:
        return
    created = _run_podman(podman_bin, ["network", "create", network], timeout=timeout)
    if created.returncode != 0:
        raise _unavailable(f"podman network create {network} failed: {created.stderr.strip()[-500:]}")


def _remove_container(podman_bin: str, name: str, *, timeout: float) -> None:
    proc = _run_podman(podman_bin, ["rm", "-f", "-t", "10", name], timeout=timeout)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if any(marker in stderr.lower() for marker in _NO_SUCH_CONTAINER_MARKERS):
            return
        raise _unavailable(f"podman rm -f {name} failed (exit={proc.returncode}): {stderr[-500:]}")


def _rollback(podman_bin: str, name: str, *, created: bool) -> None:
    """Stop (and remove, when this ensure created it) a container that never became usable.

    Best effort: a rollback failure is logged, the original error still
    propagates. Without this a bad image or a screen that never publishes would
    leak a running container per attempt until the host quota is exhausted.
    """
    try:
        stop = _run_podman(podman_bin, ["stop", "-t", "5", name], timeout=30.0)
        if stop.returncode != 0:
            logger.warning("[multitenancy] desktop rollback: podman stop %s failed: %s", name, stop.stderr.strip()[-300:])
        if created:
            _remove_container(podman_bin, name, timeout=30.0)
    except (subprocess.TimeoutExpired, OSError, DesktopSandboxError) as exc:
        logger.warning("[multitenancy] desktop rollback of %s failed: %s", name, exc)


# --- gateway-owned lifecycle state ---------------------------------------------------


def _open_nofollow(path: Path, flags: int, mode: int) -> int:
    try:
        return os.open(path, flags | os.O_NOFOLLOW, mode)
    except OSError as exc:
        raise DesktopSandboxError(
            "desktop_profile_home_tampered", f"{path.name}: refusing to open through a symlink ({exc})"
        ) from exc


def state_root(shared_home: Path) -> Path:
    return Path(shared_home).expanduser().resolve() / STATE_DIRNAME


def profile_state_dir(shared_home: Path, profile_home: Path) -> Path:
    """``<SHARED_HOME>/desktop-state/<sha256(profile_home)[:16]>`` — never mounted.

    Fails closed when the state root would sit inside the profile tree (no
    ``profiles/`` layout and no ``HERMES_SHARED_HOME``): the whole point of the
    directory is that no container can write to it.
    """
    root = state_root(shared_home)
    profile_home = Path(profile_home).expanduser().resolve()
    if root == profile_home or profile_home in root.parents:
        raise DesktopSandboxError(
            "desktop_state_dir_unavailable",
            f"desktop state root {root} would live inside the tenant-writable profile {profile_home}; "
            "set HERMES_SHARED_HOME or use the profiles/<name> layout",
        )
    return root / _profile_digest(profile_home)[:16]


def _ensure_state_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


@contextmanager
def _flock(path: Path, *, timeout_s: float, what: str) -> Iterator[None]:
    """Exclusive ``flock`` on ``path``; fails closed after ``timeout_s``.

    flock locks belong to the open file description, so two threads of one
    gateway exclude each other exactly like two processes do.
    """
    _ensure_state_dir(path.parent)
    fd = _open_nofollow(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise _unavailable(f"{what}: lock {path} still held after {timeout_s:g}s")
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_private(path: Path, text: str) -> None:
    fd = _open_nofollow(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def touch_last_used(state_dir: Path, *, now: float | None = None) -> None:
    _ensure_state_dir(state_dir)
    _write_private(state_dir / LAST_USED_NAME, f"{int(now if now is not None else time.time())}\n")


def read_last_used(state_dir: Path) -> float | None:
    try:
        return float((Path(state_dir) / LAST_USED_NAME).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_meta(decision: DesktopDecision, *, now: float) -> None:
    _write_private(
        decision.state_dir / META_NAME,
        json.dumps(
            {
                "profile": decision.profile_name,
                "profile_home": str(decision.profile_home),
                "container": decision.container_name,
                "idle_stop_minutes": decision.idle_stop_minutes,
                "updated": int(now),
            },
            ensure_ascii=False,
        ) + "\n",
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _live_turns(turns: Mapping[str, Any], *, now: float) -> dict[str, dict[str, Any]]:
    live: dict[str, dict[str, Any]] = {}
    for token, entry in turns.items():
        if not isinstance(entry, dict):
            continue
        started = _as_float(entry.get("started"), 0.0)
        pid = _as_int(entry.get("pid"), 0)
        if now - started > ACTIVE_TURN_MAX_S or not _pid_alive(pid):
            continue
        live[str(token)] = dict(entry)
    return live


def begin_turn(state_dir: Path, *, now: float | None = None) -> str:
    """Register one in-flight worker run; returns the token for :func:`end_turn`."""
    now = time.time() if now is None else now
    token = secrets.token_hex(8)
    with _flock(state_dir / TURNS_LOCK_NAME, timeout_s=TURNS_LOCK_WAIT_S, what="begin_turn"):
        turns = _live_turns(_read_json(state_dir / TURNS_NAME), now=now)
        turns[token] = {"pid": os.getpid(), "started": now}
        _write_private(state_dir / TURNS_NAME, json.dumps(turns) + "\n")
    return token


def end_turn(state_dir: Path, token: str, *, now: float | None = None) -> None:
    """Release a turn lease and stamp last use (the idle window starts at turn end)."""
    now = time.time() if now is None else now
    with _flock(state_dir / TURNS_LOCK_NAME, timeout_s=TURNS_LOCK_WAIT_S, what="end_turn"):
        turns = _live_turns(_read_json(state_dir / TURNS_NAME), now=now)
        turns.pop(token, None)
        _write_private(state_dir / TURNS_NAME, json.dumps(turns) + "\n")
        touch_last_used(state_dir, now=now)


def active_turn_count(state_dir: Path, *, now: float | None = None) -> int:
    now = time.time() if now is None else now
    return len(_live_turns(_read_json(Path(state_dir) / TURNS_NAME), now=now))


# --- filesystem preparation ----------------------------------------------------


def _mt_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _tampered(rel: str, what: str) -> DesktopSandboxError:
    return DesktopSandboxError(
        "desktop_profile_home_tampered",
        f"profile path {rel!r} is {what}; refusing to prepare the desktop container",
    )


def _prepare_profile_home(profile_home: Path, *, create: bool) -> None:
    """Validate (and with ``create`` make) every tenant-tree path the gateway touches.

    Every mask target must already exist and be owned by the gateway user:
    podman would otherwise create a root-owned mountpoint inside the tenant's
    profile and break the gateway's own later writes there. The tree is
    tenant-writable, so each path is checked with ``lstat`` and created
    ``O_NOFOLLOW``: a symlink anywhere on these paths fails closed
    (``desktop_profile_home_tampered``) — the gateway must never follow a
    tenant-planted link while running as itself.
    """
    if create:
        profile_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    for rel in PROFILE_DIRS:
        path = profile_home / rel
        info = _lstat(path)
        if info is None:
            if create:
                os.mkdir(path, 0o700)
            continue
        if stat_module.S_ISLNK(info.st_mode):
            raise _tampered(rel, "a symlink")
        if not stat_module.S_ISDIR(info.st_mode):
            raise _tampered(rel, "not a directory")
    for rel in PROFILE_FILES:
        path = profile_home / rel
        info = _lstat(path)
        if info is None:
            if create:
                fd = _open_nofollow(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    if rel == "auth.json":
                        os.write(fd, b"{}\n")
                finally:
                    os.close(fd)
            continue
        if stat_module.S_ISLNK(info.st_mode):
            raise _tampered(rel, "a symlink")
        if not stat_module.S_ISREG(info.st_mode):
            raise _tampered(rel, "not a regular file")


def _selinux_enforcing() -> bool:
    try:
        return Path("/sys/fs/selinux/enforce").read_text().strip() == "1"
    except OSError:
        return False


def _label(opts: str, shared: bool, selinux: bool, *, mount: bool = False) -> str:
    if not selinux:
        return opts
    if mount:
        return f"{opts},relabel={'shared' if shared else 'private'}"
    return f"{opts},{'z' if shared else 'Z'}"


def _id_maps(flag: str, host_id: int, base: int) -> list[str]:
    """Map container id 10000 → host_id, everything else → subordinate range."""
    return [
        flag, f"0:{base}:{CONTAINER_UID}",
        flag, f"{CONTAINER_UID}:{host_id}:1",
        flag, f"{CONTAINER_UID + 1}:{base + CONTAINER_UID + 1}:{65536 - CONTAINER_UID - 1}",
    ]


def _shared_config_id(shared_home: Path) -> str:
    """``<dev>:<ino>`` of the shared config.yaml, or "none".

    The file is bind-mounted as a single file, so a host-side edit that
    replaces the inode (editors, ``sed -i``) leaves a running container reading
    the old content. Recording the inode as a label lets ensure() rebuild.
    """
    try:
        info = os.stat(shared_home / "config.yaml")
    except OSError:
        return "none"
    return f"{info.st_dev}:{info.st_ino}"


def run_args(decision: DesktopDecision, *, image_id: str = "") -> list[str]:
    """Build the full ``podman run`` argv for a profile's desktop container."""
    profile_home = decision.profile_home.resolve()
    shared_home = decision.shared_home.resolve()
    mt_repo = _mt_repo_root()
    empty_auth = mt_repo / "hermes_multitenancy" / "sandbox" / "empty-auth.json"
    selinux = _selinux_enforcing()
    shared_file_opts = _label("ro", True, selinux, mount=True)
    stat = profile_home.stat()
    if decision.subid_base <= stat.st_uid < decision.subid_base + 65536:
        raise _unavailable(
            f"profile owner uid {stat.st_uid} collides with desktop.subid_base "
            f"{decision.subid_base}; set multitenancy.desktop.subid_base elsewhere",
        )

    args: list[str] = [
        "run", "-d",
        "--name", decision.container_name,
        "--init",
        "--entrypoint", "/bin/sleep",
        "--user", f"{CONTAINER_UID}:{CONTAINER_GID}",
        *_id_maps("--uidmap", stat.st_uid, decision.subid_base),
        *_id_maps("--gidmap", stat.st_gid, decision.subid_base),
        "--label", f"{LABEL_ROLE}={LABEL_ROLE_VALUE}",
        "--label", f"{LABEL_PROFILE}={decision.profile_name}",
        "--label", f"{LABEL_PROFILE_HOME}={profile_home}",
        "--label", f"{LABEL_SPEC}={decision.spec_hash(stat.st_uid, stat.st_gid)}",
        "--label", f"{LABEL_IMAGE_ID}={image_id}",
        "--label", f"{LABEL_SHARED_CONFIG}={_shared_config_id(shared_home) if shared_home != profile_home else 'none'}",
        "--memory", decision.memory,
        "--cpus", decision.cpus,
        "--shm-size", decision.shm_size,
        "--network", decision.network,
        "--security-opt", "no-new-privileges",
        "--cap-drop", "ALL",
        "-e", f"HERMES_UID={CONTAINER_UID}",
        "-e", f"HERMES_GID={CONTAINER_GID}",
        "-e", f"HERMES_HOME={profile_home}",
        # This profile, read-write, at its host path (rfb.sock lands on the host).
        "-v", f"{profile_home}:{profile_home}:{_label('rw', False, selinux)}",
        # OpenClaw compatibility: skills hardcode /workspace/...
        "-v", f"{profile_home / 'workspace'}:/workspace:{_label('rw', False, selinux)}",
        # Plugin code, read-only; the worker script lives here.
        "-v", f"{mt_repo}:{mt_repo}:{_label('ro', True, selinux)}",
        # Secret files masked exactly like the bwrap local-harness policy.
        "--mount", f"type=bind,src=/dev/null,dst={profile_home / '.env'},ro",
        "--mount", f"type=bind,src={empty_auth},dst={profile_home / 'auth.json'},{shared_file_opts}",
        "--tmpfs", CONTAINER_RUNTIME_DIR,
    ]
    for rel in TMPFS_PROFILE_SUBDIRS:
        args.extend(["--tmpfs", str(profile_home / rel)])

    if shared_home != profile_home:
        # Allowlist only. ``desktop-state`` (lifecycle state), ``profiles``
        # (other tenants) and every secret file stay outside the container.
        for rel in ("config.yaml", "active_profile"):
            target = shared_home / rel
            if target.exists():
                args.extend(["--mount", f"type=bind,src={target},dst={target},{shared_file_opts}"])
        for rel in (".env", "auth.lock"):
            target = shared_home / rel
            if target.exists():
                args.extend(["--mount", f"type=bind,src=/dev/null,dst={target},ro"])
        if (shared_home / "auth.json").exists():
            args.extend(["--mount", f"type=bind,src={empty_auth},dst={shared_home / 'auth.json'},{shared_file_opts}"])
        for rel in ("bin", "browser-browsers", "authorization", *SHARED_SKILL_ROOTS):
            target = shared_home / rel
            if target.is_dir():
                args.extend(["-v", f"{target}:{target}:{_label('ro', True, selinux)}"])
        for rel in ("cron", "snapshots", "audio_cache"):
            target = shared_home / rel
            if target.is_dir():
                args.extend(["-v", f"{target}:{target}:{_label('rw', True, selinux)}"])

    args.extend([decision.image, "infinity"])
    return args


# --- lifecycle --------------------------------------------------------------------


def _wait_for(predicate: Callable[[], bool], deadline: float, *, interval_s: float = 0.5) -> bool:
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval_s)


def _socket_present(path: Path) -> bool:
    info = _lstat(path)
    return info is not None and stat_module.S_ISSOCK(info.st_mode)


def _screen_env_args(decision: DesktopDecision) -> list[str]:
    profile_home = decision.profile_home
    args = [
        "-e", f"HERMES_HOME={profile_home}",
        "-e", f"HOME={profile_home / 'home'}",
        "-e", f"PATH={decision.container_path}",
    ]
    for key, value in sorted(decision.exec_env.items()):
        args.extend(["-e", f"{key}={value}"])
    return args


def _screen_exec_base(decision: DesktopDecision) -> list[str]:
    return [
        "exec",
        "--user", f"{CONTAINER_UID}:{CONTAINER_GID}",
        *_screen_env_args(decision),
        decision.container_name,
        decision.container_hermes, "computer-use", "screen",
    ]


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _probe_profile_home_reachable(decision: DesktopDecision, podman_bin: str, *, deadline: float) -> None:
    """Fail precisely when uid 10000 cannot read config or write Bot Screen state.

    Same-path mounting inherits the image's permission bits on every ancestor
    directory. A profile under a directory the image ships as ``0700`` (e.g.
    ``/root``) mounts fine but is untraversable for uid 10000; without this
    probe the first symptom is an opaque PermissionError deep inside Bot
    Screen. Runs once per container (re)start.
    """
    profile_home = decision.profile_home
    base = [
        "exec", "--user", f"{CONTAINER_UID}:{CONTAINER_GID}", decision.container_name,
    ]

    def probe(command: list[str], failure: str) -> None:
        proc = _run_podman(podman_bin, [*base, *command], timeout=_remaining(deadline, "profile probe"))
        if proc.returncode != 0:
            raise DesktopSandboxError(
                "desktop_profile_home_unreachable",
                f"uid {CONTAINER_UID} {failure} inside {decision.container_name}; "
                "check parent-directory traversal permissions and SELinux labels",
            )

    shared_config = decision.shared_home.resolve() / "config.yaml"
    if shared_config.exists():
        probe(["test", "-r", str(shared_config)], f"cannot read shared config {shared_config}")

    bot_desktop = profile_home / "bot-desktop"
    write_probe = bot_desktop / ".desktop-sandbox-write-probe"
    probe(
        [
            "/bin/sh", "-c",
            f"mkdir -p {_shell_quote(str(bot_desktop))} && "
            f"touch {_shell_quote(str(write_probe))} && rm -f {_shell_quote(str(write_probe))}",
        ],
        f"cannot create and remove a probe file under {bot_desktop}",
    )
    profile_config = profile_home / "config.yaml"
    probe(["test", "-r", str(profile_config)], f"cannot read profile config {profile_config}")


def screen_alive(decision: DesktopDecision, podman_bin: str, *, deadline: float) -> bool:
    """True when ``rfb.sock`` is a socket AND ``screen status --json`` reports running.

    A file merely existing at the socket path (stale after an Xvnc crash, or
    anything a tenant put there) is not a live screen.
    """
    if not _socket_present(decision.rfb_socket):
        return False
    try:
        proc = _run_podman(
            podman_bin,
            [*_screen_exec_base(decision), "status", "--json"],
            timeout=min(_remaining(deadline, "screen status"), 15.0),
        )
    except (subprocess.TimeoutExpired, OSError, DesktopSandboxError) as exc:
        logger.warning("[multitenancy] desktop screen status failed in %s: %s", decision.container_name, exc)
        return False
    if proc.returncode != 0:
        return False
    try:
        status = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return False
    if not isinstance(status, dict) or status.get("running") is not True:
        return False
    socket = str(status.get("socket") or "")
    return not socket or socket == str(decision.rfb_socket)


def start_screen(decision: DesktopDecision, podman_bin: str, *, deadline: float) -> None:
    """Start Bot Screen inside the container and wait for ``rfb.sock``."""
    name = decision.container_name
    proc = _run_podman(
        podman_bin,
        [*_screen_exec_base(decision), "start"],
        timeout=_remaining(deadline, "screen start"),
    )
    if proc.returncode != 0:
        raise DesktopSandboxError(
            "desktop_screen_unavailable",
            f"screen start failed in {name} (exit={proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[-800:]}",
        )
    if not _wait_for(lambda: _socket_present(decision.rfb_socket), deadline):
        raise DesktopSandboxError(
            "desktop_screen_unavailable",
            f"rfb.sock did not appear at {decision.rfb_socket} within "
            f"{decision.screen_timeout_s:g}s",
        )


def _spec_drift(info: _ContainerInfo, decision: DesktopDecision, *, image_id: str) -> list[str]:
    """Which recorded facts of an existing container no longer match the decision."""
    stat = decision.profile_home.stat()
    expected = {
        LABEL_PROFILE: decision.profile_name,
        LABEL_PROFILE_HOME: str(decision.profile_home),
        LABEL_SPEC: decision.spec_hash(stat.st_uid, stat.st_gid),
        LABEL_SHARED_CONFIG: (
            _shared_config_id(decision.shared_home.resolve())
            if decision.shared_home.resolve() != decision.profile_home else "none"
        ),
    }
    drift = [key for key, value in expected.items() if info.labels.get(key) != value]
    if image_id and info.image_id and info.image_id != image_id:
        drift.append("image")
    return drift


def _check_quota(podman_bin: str, decision: DesktopDecision) -> None:
    running = [
        row for row in _running_desktop_containers(podman_bin)
        if _row_name(row) != decision.container_name
    ]
    if len(running) >= decision.max_containers_per_host:
        raise DesktopSandboxError(
            "desktop_quota_exhausted",
            f"desktop quota exhausted: {len(running)}/{decision.max_containers_per_host} "
            f"containers running on this host",
            user_message=QUOTA_USER_MESSAGE,
        )


def ensure(decision: DesktopDecision) -> ContainerHandle:
    """Make sure the profile's container is running with a live Bot Screen.

    Two locks, taken in this order:

    * ``<state>/profile.lock`` for the whole call — two turns of one profile
      never create/start/rebuild the same container concurrently.
    * ``<root>/host.lock`` around inspect → reconcile → quota → create/start →
      running: the quota count and the transition it guards are one atomic
      step across every gateway thread and process on the host.

    Budgets are two monotonic deadlines: ``ensure_timeout_s`` for the container
    to be running, ``screen_timeout_s`` for Bot Screen to publish. Any podman
    call gets only what is left of its phase. A container this call started but
    could not bring to a usable state is stopped (and removed when it was
    created here) before the error propagates.
    """
    if not decision.enabled:
        raise DesktopSandboxError("desktop_disabled", "ensure() called for a non-desktop profile")
    podman = _podman_available(decision.podman_bin)
    if podman is None:
        raise _unavailable(f"podman is not executable at {decision.podman_bin!r}")
    name = decision.container_name
    state_dir = _ensure_state_dir(decision.state_dir)
    host_lock = state_root(decision.shared_home) / HOST_LOCK_NAME
    now = time.time()

    with _flock(state_dir / PROFILE_LOCK_NAME, timeout_s=PROFILE_LOCK_WAIT_S, what=f"ensure {name}"):
        deadline = time.monotonic() + decision.ensure_timeout_s
        started = False
        created = False
        try:
            with _flock(host_lock, timeout_s=HOST_LOCK_WAIT_S, what=f"ensure {name} (host quota)"):
                info = _inspect_container(podman, name, timeout=_remaining(deadline, "inspect"))
                image_id = _image_id(podman, decision.image, timeout=_remaining(deadline, "image inspect"))
                if info is not None:
                    drift = _spec_drift(info, decision, image_id=image_id)
                    if drift:
                        active = active_turn_count(state_dir, now=now)
                        if active > 0:
                            logger.warning(
                                "[multitenancy] desktop container ensure: %s drifted (%s) but %d turn(s) "
                                "active; reusing until idle",
                                name, ",".join(drift), active,
                            )
                        else:
                            logger.warning(
                                "[multitenancy] desktop container ensure: %s drifted (%s); rebuilding",
                                name, ",".join(drift),
                            )
                            _remove_container(podman, name, timeout=_remaining(deadline, "rm drifted"))
                            info = None
                if info is None:
                    _check_quota(podman, decision)
                    _prepare_profile_home(decision.profile_home, create=True)
                    _ensure_network(podman, decision.network, timeout=_remaining(deadline, "network"))
                    args = run_args(decision, image_id=image_id)
                    logger.info(
                        "[multitenancy] desktop container ensure: creating %s profile=%s image=%s",
                        name, decision.profile_name, decision.image,
                    )
                    proc = _run_podman(podman, args, timeout=_remaining(deadline, "podman run"))
                    if proc.returncode != 0:
                        raise _unavailable(
                            f"podman run {name} failed (exit={proc.returncode}): {proc.stderr.strip()[-800:]}"
                        )
                    started = created = True
                elif info.status != "running":
                    _check_quota(podman, decision)
                    _prepare_profile_home(decision.profile_home, create=False)
                    logger.info(
                        "[multitenancy] desktop container ensure: starting %s profile=%s (was %s)",
                        name, decision.profile_name, info.status,
                    )
                    proc = _run_podman(podman, ["start", name], timeout=_remaining(deadline, "podman start"))
                    if proc.returncode != 0:
                        raise _unavailable(
                            f"podman start {name} failed (exit={proc.returncode}): {proc.stderr.strip()[-800:]}"
                        )
                    started = True
                else:
                    logger.info("[multitenancy] desktop container ensure: %s already up", name)

                def _is_running() -> bool:
                    current = _inspect_container(podman, name, timeout=_remaining(deadline, "inspect"))
                    return current is not None and current.status == "running"

                if not _wait_for(_is_running, deadline):
                    raise _unavailable(
                        f"{name} did not reach State.Status=running within {decision.ensure_timeout_s:g}s"
                    )
                # Visible to the sweep before the screen phase: a container that
                # is up but has no last_used must never look "idle forever".
                touch_last_used(state_dir, now=now)
                _write_meta(decision, now=now)
        except (subprocess.TimeoutExpired, OSError) as exc:
            if started:
                _rollback(podman, name, created=created)
            raise _unavailable(f"podman call for {name} failed: {exc}") from exc
        except DesktopSandboxError:
            if started:
                _rollback(podman, name, created=created)
            raise

        screen_deadline = time.monotonic() + decision.screen_timeout_s
        screen_started = False
        try:
            if started or not screen_alive(decision, podman, deadline=screen_deadline):
                _probe_profile_home_reachable(decision, podman, deadline=screen_deadline)
                start_screen(decision, podman, deadline=screen_deadline)
                screen_started = True
        except (subprocess.TimeoutExpired, OSError) as exc:
            if started:
                _rollback(podman, name, created=created)
            raise DesktopSandboxError("desktop_screen_unavailable", f"screen phase for {name} failed: {exc}") from exc
        except DesktopSandboxError:
            if started:
                _rollback(podman, name, created=created)
            raise
        touch_last_used(state_dir)
    logger.info(
        "[multitenancy] desktop container ensure: %s up (profile=%s started=%s screen_started=%s rfb=%s)",
        name, decision.profile_name, started, screen_started, decision.rfb_socket,
    )
    return ContainerHandle(name=name, started=started, screen_started=screen_started, podman_bin=podman)


def exec_args(
    decision: DesktopDecision,
    handle: ContainerHandle,
    cmd: list[str],
    env: Mapping[str, str],
    *,
    workdir: Path,
) -> list[str]:
    """Build ``podman exec -i …`` argv that runs ``cmd`` with ``env`` inside.

    Env values never enter argv: every passthrough name is given as ``-e KEY``
    and podman reads the value from its own process environment (the caller
    spawns podman with exactly this ``env``). Only non-secret container-side
    overrides (PATH and runtime directories) are inline. Image-internal paths
    come from the image ENV; MT only overrides them through ``desktop.exec_env``.
    ``argv[0]`` is the absolute podman that ensure() resolved, never a bare name
    the caller's PATH would resolve again.
    """
    args = [
        handle.podman_bin, "exec", "-i",
        "--user", f"{CONTAINER_UID}:{CONTAINER_GID}",
        "-w", str(workdir),
    ]
    for key in sorted(env):
        if key in EXEC_ENV_EXCLUDE or key in IMAGE_EXEC_ENV or key in decision.exec_env:
            continue
        args.extend(["-e", key])
    shared_bin = decision.shared_home / "bin"
    container_path = decision.container_path
    if shared_bin.is_dir():
        container_path = f"{shared_bin}{os.pathsep}{container_path}"
    args.extend(["-e", f"PATH={container_path}"])
    for key, value in sorted(decision.exec_env.items()):
        args.extend(["-e", f"{key}={value}"])
    args.append(handle.name)
    args.extend(cmd)
    return args


def map_executable(decision: DesktopDecision, executable: str) -> str | None:
    """Map the gateway's interpreter path to the image's; None when unmappable."""
    candidate = Path(executable)
    if not candidate.is_absolute():
        return None
    gateway_python = Path(os.path.realpath(sys.executable))
    resolved = Path(os.path.realpath(executable))
    if resolved == gateway_python or candidate.name.startswith("python"):
        return decision.container_python
    return None


def stop(decision: DesktopDecision, *, timeout_s: int = 10) -> bool:
    podman = _podman_available(decision.podman_bin)
    if podman is None:
        return False
    proc = _run_podman(podman, ["stop", "-t", str(timeout_s), decision.container_name])
    return proc.returncode == 0


def idle_stop_sweep(
    *,
    podman_bin: str = "podman",
    shared_home: Path | None = None,
    now: float | None = None,
) -> list[str]:
    """Stop desktop containers idle for longer than their idle_stop_minutes.

    Idle means: no turn lease alive (``turns.json``, kept by the gateway, not by
    podman ``ExecIDs`` — a warm worker is itself a permanent exec) AND the last
    turn ended more than the window ago (``last_used``). Both live in the
    gateway-owned state directory; a container without one was not started by
    this gateway and is left alone. A profile whose ensure() is in flight (its
    profile lock is held) is skipped this tick.
    """
    podman = _podman_available(podman_bin)
    if podman is None:
        logger.debug("[multitenancy] desktop idle sweep skipped: %s not executable", podman_bin)
        return []
    shared_home = shared_home or shared_home_from_env()
    if shared_home is None:
        logger.warning("[multitenancy] desktop idle sweep skipped: shared home unknown (HERMES_SHARED_HOME unset)")
        return []
    now = time.time() if now is None else now
    stopped: list[str] = []
    try:
        rows = _running_desktop_containers(podman)
    except DesktopSandboxError as exc:
        logger.warning("[multitenancy] desktop idle sweep could not list containers: %s", exc)
        return []
    for row in rows:
        name = _row_name(row)
        labels = row.get("Labels") or {}
        if not isinstance(labels, dict) or not name:
            continue
        profile_home_raw = str(labels.get(LABEL_PROFILE_HOME) or "").strip()
        if not profile_home_raw:
            continue
        try:
            state_dir = profile_state_dir(shared_home, Path(profile_home_raw))
        except DesktopSandboxError as exc:
            logger.warning("[multitenancy] desktop idle sweep: %s skipped: %s", name, exc)
            continue
        if not state_dir.is_dir():
            logger.debug("[multitenancy] desktop idle sweep: %s has no state dir; not ours", name)
            continue
        idle_minutes = _as_int(_read_json(state_dir / META_NAME).get("idle_stop_minutes"), DEFAULT_IDLE_STOP_MINUTES)
        if idle_minutes <= 0:
            continue
        last_used = read_last_used(state_dir)
        if last_used is not None and now - last_used < idle_minutes * 60:
            continue
        try:
            with _flock(state_dir / PROFILE_LOCK_NAME, timeout_s=0.0, what=f"idle sweep {name}"):
                active = active_turn_count(state_dir, now=now)
                if active > 0:
                    logger.info(
                        "[multitenancy] desktop idle sweep: %s past idle window but %d turn(s) active; keeping",
                        name, active,
                    )
                    continue
                proc = _run_podman(podman, ["stop", "-t", "10", name], timeout=60.0)
        except DesktopSandboxError:
            logger.info("[multitenancy] desktop idle sweep: %s ensure in flight; skipping this tick", name)
            continue
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("[multitenancy] desktop idle_stop: podman stop %s failed: %s", name, exc)
            continue
        if proc.returncode == 0:
            logger.info(
                "[multitenancy] desktop idle_stop: %s stopped (idle %.0fs >= %dm)",
                name, now - (last_used or 0.0), idle_minutes,
            )
            stopped.append(name)
        else:
            logger.warning(
                "[multitenancy] desktop idle_stop: podman stop %s failed: %s",
                name, proc.stderr.strip()[-300:],
            )
    return stopped


# --- sweep scheduler ---------------------------------------------------------------

SWEEP_INTERVAL_S = 300

_sweep_lock = threading.Lock()
_sweep_thread: Optional[threading.Thread] = None
_sweep_stop: Optional[threading.Event] = None


def _sweep_loop(interval: int, stop_event: threading.Event, podman_bin: str, shared_home: Path | None) -> None:
    while not stop_event.is_set():
        try:
            idle_stop_sweep(podman_bin=podman_bin, shared_home=shared_home)
        except Exception:
            logger.exception("[multitenancy] desktop idle sweep tick failed")
        stop_event.wait(timeout=interval)
    logger.info("[multitenancy] desktop idle sweep worker stopped")


def ensure_desktop_idle_sweeps_started(
    *,
    interval: int = SWEEP_INTERVAL_S,
    podman_bin: str = "podman",
    shared_home: Path | None = None,
) -> None:
    """Start the idle-stop sweep thread once, at gateway startup (Linux only).

    ``podman_bin`` must come from the shared config (:func:`host_desktop_settings`),
    never from a profile.
    """
    global _sweep_thread, _sweep_stop
    if not sys.platform.startswith("linux"):
        return
    with _sweep_lock:
        if _sweep_thread is not None and _sweep_thread.is_alive():
            return
        _sweep_stop = threading.Event()
        _sweep_thread = threading.Thread(
            target=_sweep_loop, args=(int(interval), _sweep_stop, podman_bin, shared_home),
            daemon=True, name="desktop-idle-sweeps",
        )
        _sweep_thread.start()
        logger.info(
            "[multitenancy] desktop idle sweep worker started (interval=%ds podman=%s)", interval, podman_bin
        )


def stop_desktop_idle_sweeps() -> None:
    global _sweep_thread, _sweep_stop
    with _sweep_lock:
        if _sweep_stop is not None:
            _sweep_stop.set()
        thread = _sweep_thread
    if thread is not None:
        thread.join(timeout=2.0)
    with _sweep_lock:
        _sweep_thread = None
        _sweep_stop = None
