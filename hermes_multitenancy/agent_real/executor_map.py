"""Which runtime executes a run — the MT executor map (PLAN.md §2, contract C1).

The mapping is an OPERATOR-managed config file named by ``HERMES_EXECUTOR_MAP``
(yaml or json)::

    {"<expert_id or plugin_id>": "codex_app_server"}

Three properties are the whole point of this module:

* **Config is the only authority.** A request body / event metadata / plugin
  manifest can never pick its own executor. ``executor`` / ``runtime`` /
  ``api_mode`` arriving on an inbound event are dropped and audited.
* **Default off.** No env var, no file, or no matching key ⇒ ``hermes_default``,
  i.e. today's behaviour byte-for-byte. A run with no expert is never mapped.
* **Fail closed, never degrade.** A run mapped to ``codex_app_server`` whose
  provider is not OpenAI-wire, or whose ``codex`` binary is missing from the
  child PATH, RAISES. It must not quietly fall back to the hermes-native
  runtime — silently running the native tool loop while the operator believes
  codex is driving is the exact failure this slug exists to make visible.

``ExecutorUnavailable`` subclasses ``ExpertUnavailableError`` deliberately: that
is the ONLY error shape the existing child→parent chain carries past the
billing-retry / legacy-spike-runner ladder. ``_core._subprocess_failure`` keys
on ``error_code == "EXPERT_UNAVAILABLE"`` + ``failure_subsystem ==
"expert_resolution"`` to rebuild a fail-closed error in the parent, and
``_core.real_run_agent`` / ``_core.stream_run_agent`` re-raise that type BEFORE
the fallback ladder. Any other code would be reported to the user *and* then
silently re-run on the native runtime — the degradation C1 forbids. Reusing the
shape buys the whole rail with zero change to ``_core.py``.

MT builds ``runtime_kwargs`` itself and never reaches the core's own gate
(``hermes_cli.runtime_provider._maybe_apply_codex_app_server_runtime``, which
only rewrites api_mode for provider ∈ {openai, openai-codex}), so
``assert_openai_wire`` re-asserts the equivalent invariant on this side.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

import yaml

from ._core import ExpertUnavailableError, _event_metadata, _expert_id_for_event
from ..security_audit import append_security_event

logger = logging.getLogger(__name__)

EXECUTOR_MAP_ENV = "HERMES_EXECUTOR_MAP"
HERMES_DEFAULT = "hermes_default"
CODEX_APP_SERVER = "codex_app_server"
KNOWN_RUNTIMES = frozenset({HERMES_DEFAULT, CODEX_APP_SERVER})

CODEX_BINARY = "codex"

#: Fields a caller might try to smuggle an executor choice through. Ignored.
REQUEST_OVERRIDE_KEYS = ("executor", "runtime", "api_mode")

#: Mirror of the resolved runtime, published on ``event.raw_event`` for the
#: subprocess env scope. A MIRROR, never an input — see ``_stamp_event_runtime``.
EVENT_RUNTIME_KEY = "_executor_runtime"

_OPENAI_WIRE_PROVIDERS = frozenset({"openai", "openai-codex"})
#: The LiteLLM gateway also exposes an Anthropic-wire path; codex cannot use it.
_NON_OPENAI_WIRE_PATHS = ("/anthropic",)


class ExecutorUnavailable(ExpertUnavailableError):
    """A mapped executor could not be proven runnable — fail closed, no fallback."""

    # Keep the parent's code/subsystem verbatim: they are the wire contract that
    # makes _core rebuild this as a non-degrading failure. The human-readable
    # reason travels in the message and the audit line instead.
    error_code = "EXPERT_UNAVAILABLE"
    failure_subsystem = "expert_resolution"
    retryable = False

    def __init__(self, reason: str = "") -> None:
        # ExpertUnavailableError.__init__ takes no args and hardcodes its own
        # message, so go straight to RuntimeError to keep the reason.
        self.reason = str(reason or "")
        RuntimeError.__init__(
            self,
            f"EXECUTOR_UNAVAILABLE: {self.reason}"
            if self.reason
            else "EXECUTOR_UNAVAILABLE",
        )


def _audit(**fields: Any) -> None:
    # force=True: these events ARE the control (a dropped tenant override leaves
    # no other trace), and neither fires in a default-off deploy — no map means
    # no unknown runtime, and no request carries executor fields today.
    try:
        append_security_event(force=True, **fields)
    except Exception:  # pragma: no cover - audit must never break a run
        logger.debug("[multitenancy] executor map audit append failed", exc_info=True)


def _load_map(environ: Mapping[str, str]) -> dict[str, str]:
    """Parse the executor map, or ``{}`` when the operator has not opted in."""
    raw_path = str(environ.get(EXECUTOR_MAP_ENV) or "").strip()
    if not raw_path:
        return {}
    path = Path(raw_path).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Default off: an unset or absent map means every run stays native.
        # Only a PRESENT-but-broken map fails closed.
        return {}
    except OSError as exc:
        raise ExecutorUnavailable(f"executor map unreadable: {path}") from exc
    try:
        # YAML 1.2 is a JSON superset, so one loader covers both formats.
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ExecutorUnavailable(
            f"executor map is not valid yaml/json: {path}"
        ) from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ExecutorUnavailable(f"executor map must be a mapping: {path}")
    return {
        str(key).strip(): str(value or "").strip()
        for key, value in parsed.items()
        if str(key).strip()
    }


def resolve_runtime(
    expert_id: Optional[str],
    plugin_id: Optional[str],
    *,
    environ: Mapping[str, str],
) -> str:
    """Return the runtime the config picks for this expert/plugin pair.

    ``expert_id`` wins over ``plugin_id`` (most specific key). An unmapped key,
    an empty map and an absent file all mean ``hermes_default``.
    """
    mapping = _load_map(environ)
    if not mapping:
        return HERMES_DEFAULT
    for candidate in (expert_id, plugin_id):
        key = str(candidate or "").strip()
        if not key or key not in mapping:
            continue
        runtime = mapping[key]
        if runtime not in KNOWN_RUNTIMES:
            # ponytail: validate the MATCHED key only, not the whole file — a
            # typo then kills exactly the run it was written for, loudly,
            # instead of taking every other tenant's run down with it.
            _audit(
                event_type="executor_map_runtime_unknown",
                expert_id=key,
                reason=f"unknown_runtime:{runtime[:32]}",
                decision="rejected",
            )
            raise ExecutorUnavailable(
                f"executor map names an unknown runtime for {key!r}"
            )
        return runtime
    return HERMES_DEFAULT


def assert_codex_available(path_env: str) -> None:
    """Fail closed when a mapped run has no ``codex`` binary to execute it.

    ``path_env`` is the PATH the AIAgent child will actually run with — in prod
    that is ``${SHARED_HOME}/bin`` first (``subprocess_env.py``), which is where
    codex is installed. An empty PATH resolves nothing and therefore fails.
    """
    if shutil.which(CODEX_BINARY, path=str(path_env or "")) is None:
        raise ExecutorUnavailable(
            "codex_app_server was mapped but no 'codex' binary is on the run PATH"
        )


def assert_openai_wire(provider: str, base_url: str) -> None:
    """Fail closed unless the resolved credentials speak the OpenAI wire.

    A LiteLLM alias (``custom`` / ``custom:<slug>``) counts, because the gateway
    serves the OpenAI Responses API that codex app-server talks — but only on an
    OpenAI-wire path; the same gateway also exposes ``/anthropic``.
    """
    name = str(provider or "").strip().lower()
    url = str(base_url or "").strip()
    if name in _OPENAI_WIRE_PROVIDERS:
        return
    if name == "custom" or name.startswith("custom:"):
        if not url:
            raise ExecutorUnavailable(
                "codex_app_server needs an explicit LiteLLM base_url"
            )
        if urlsplit(url).path.rstrip("/").lower().endswith(_NON_OPENAI_WIRE_PATHS):
            raise ExecutorUnavailable(
                "codex_app_server cannot run against an Anthropic-wire endpoint"
            )
        return
    shown = name or "<unset>"
    raise ExecutorUnavailable(
        f"codex_app_server is not available for provider {shown!r}"
    )


def _stamp_event_runtime(event: Any, runtime: str) -> None:
    """Publish the resolved runtime on the event for the env-scope wiring.

    This is a MIRROR of the config decision, never an input to it:
    ``runtime_for_event`` re-reads the file every call, so a value forged on an
    inbound event cannot select a runtime — it is simply overwritten.
    """
    raw_event = getattr(event, "raw_event", None)
    if isinstance(raw_event, dict):
        raw_event[EVENT_RUNTIME_KEY] = runtime


def _reject_request_override(event: Any, profile_home: Path) -> list[str]:
    """Drop and audit any executor choice the request tried to carry."""
    metadata = _event_metadata(event)
    present = [
        name
        for name in REQUEST_OVERRIDE_KEYS
        if str(metadata.get(name) or "").strip()
    ]
    if present:
        _audit(
            event_type="executor_request_override_ignored",
            profile=str(getattr(profile_home, "name", profile_home)),
            expert_id=_expert_id_for_event(event),
            reason="ignored_fields:" + ",".join(present),
            decision="ignored",
        )
        logger.warning(
            "[multitenancy] ignored request-supplied executor fields %s — the "
            "%s config is the only authority",
            present,
            EXECUTOR_MAP_ENV,
        )
    return present


def _plugin_id_for(profile_home: Path, expert_id: str) -> str:
    """Best-effort plugin_id, so a map may key on the plugin instead of the expert.

    ponytail: expert_id is the primary key, so this manifest scan is only
    reached on an expert_id miss. Returns "" when the manifest is unreadable
    (e.g. the sandboxed child) — the run then stays on hermes_default rather
    than guessing at a mapping.
    """
    try:
        from ..expert_overlay import resolve_expert

        overlay = resolve_expert(profile_home, expert_id)
    except Exception:
        logger.debug(
            "[multitenancy] executor map plugin_id lookup failed", exc_info=True
        )
        return ""
    return str(getattr(overlay, "plugin_id", "") or "")


def runtime_for_event(
    event: Any,
    profile_home: Path,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """The runtime for this run: config only, request ignored, expert required.

    Also the seam the subprocess env scope reads (``EVENT_RUNTIME_KEY``) so the
    parent and the child agree without re-deriving the ids twice.
    """
    env = os.environ if environ is None else environ
    _reject_request_override(event, profile_home)
    runtime = _runtime_for_event(event, profile_home, env)
    _stamp_event_runtime(event, runtime)
    return runtime


def _runtime_for_event(
    event: Any, profile_home: Path, env: Mapping[str, str]
) -> str:
    if (
        str(env.get("HERMES_LOCAL_HARNESS") or "") == "1"
        and str(env.get("HERMES_EXECUTOR_RUNTIME") or "") == CODEX_APP_SERVER
    ):
        return CODEX_APP_SERVER
    # Local WebUI Harness selection is admitted by the authenticated broker
    # and sealed on the in-process event. It is not request metadata and it
    # never carries a repository path from the browser.
    from .harness_webui_runtime import (
        HarnessAdmissionRejected,
        require_event_admission,
    )

    try:
        if require_event_admission(event, profile_home) is not None:
            return CODEX_APP_SERVER
    except HarnessAdmissionRejected as exc:
        raise ExecutorUnavailable(f"local Harness admission rejected: {exc}") from exc

    mapping = _load_map(env)
    if not mapping:
        return HERMES_DEFAULT
    expert_id = _expert_id_for_event(event)
    if not expert_id:
        # C1: a run with no expert is never mapped onto a coding runtime.
        return HERMES_DEFAULT
    plugin_id = "" if expert_id in mapping else _plugin_id_for(profile_home, expert_id)
    return resolve_runtime(expert_id, plugin_id, environ=env)
