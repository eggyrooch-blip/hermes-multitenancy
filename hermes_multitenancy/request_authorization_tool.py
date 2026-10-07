"""Child-side ``request_authorization`` tool — inline, in-place credential asks.

The model can hit a wall mid-task ("I need your Feishu identity to read that
doc"). Before this tool the only exit was the whole-message ``auth_required ->
credential.replay`` path: the turn dies, the user authorizes somewhere else, and
the ENTIRE message is replayed from scratch. This tool is the TRAE-style
alternative — the tool call itself blocks while the human authorizes in the
browser, then returns, and the same turn carries on with the work it had already
done. The replay path is untouched and remains the fallback for everything that
is not one of the three whitelisted services.

Security shape (all of it deliberate):

* The model may pass ONLY ``service`` and ``scopes``. It cannot name an owner, a
  profile, a session, a URL or a token — every one of those is server state the
  broker already holds, and accepting a model-asserted value would turn this
  tool into a cross-tenant authorization primitive.
* The run is identified through core's ``tools.approval`` contextvar, never
  through anything in the arguments.
* No bridge registered for this session (a Feishu run, a cron run, an untrusted
  or non-WebUI run) ⇒ fail closed with an explanation. No event, no URL, no
  business execution.
* The tool result carries ONLY ``{ok, service, state, reason}``. No token, no
  ``open_id``, no authorization URL ever reaches the model's context.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Optional

from .credential_hub.model import KEP_CLI_ONLINE, KEP_CLI_PRE, LARK_CLI

try:  # pragma: no cover - depends on the host hermes-agent runtime
    from tools.registry import registry, tool_result
except ModuleNotFoundError:  # pragma: no cover - plugin imported outside a runtime
    registry = None

    def tool_result(**kwargs: Any) -> str:
        return json.dumps(kwargs, ensure_ascii=False)


logger = logging.getLogger(__name__)

# The inline-authorization whitelist. These are the ids the credential hub
# already owns end to end (start-flow + live verification); anything else keeps
# using the existing out-of-band credential hub card.
SUPPORTED_SERVICES: tuple[str, ...] = (LARK_CLI, KEP_CLI_ONLINE, KEP_CLI_PRE)

TERMINAL_STATES: frozenset[str] = frozenset({"success", "cancelled", "expired", "failed"})

_ALLOWED_ARG_KEYS: frozenset[str] = frozenset({"service", "scopes"})

# Deliberately NOT core's ``_clarify_timeout_response`` wording. Clarify tells the
# model to "use your best judgement and proceed" — for an authorization that is
# exactly wrong: nothing was authorized, so proceeding means inventing a result
# or running unauthorized. The model must stop and hand the decision back.
_EXPIRED_REASON = (
    "The authorization window closed before the user completed it. The task DID NOT "
    "CONTINUE and nothing was authorized. Do not work around this and do not assume "
    "access. Tell the user the authorization timed out and ask them to start it again "
    "explicitly."
)

_authorization_bridges: dict[str, Callable[..., dict[str, Any]]] = {}
_inflight_sessions: set[str] = set()
_bridges_lock = threading.Lock()


def register_authorization_bridge(session_key: str, cb: Callable[..., dict[str, Any]]) -> None:
    """Bind this run's blocking authorization bridge (parent-side wiring only)."""
    key = str(session_key or "").strip()
    if not key or cb is None:
        return
    with _bridges_lock:
        _authorization_bridges[key] = cb


def unregister_authorization_bridge(session_key: str) -> None:
    key = str(session_key or "").strip()
    with _bridges_lock:
        _authorization_bridges.pop(key, None)
        _inflight_sessions.discard(key)


def get_authorization_bridge(session_key: str) -> Optional[Callable[..., dict[str, Any]]]:
    key = str(session_key or "").strip()
    with _bridges_lock:
        return _authorization_bridges.get(key)


def _current_session_key() -> str:
    """The run identity, taken from core's approval contextvar — never from args."""
    try:
        from tools.approval import get_current_session_key
    except Exception:
        return ""
    try:
        return str(get_current_session_key() or "").strip()
    except Exception:
        return ""


def _current_tool_call_id() -> str:
    """The core tool-call id for the call we are executing inside.

    ``model_tools.py`` binds it around every handler dispatch via
    ``tools.approval.set_current_observability_context``. There is no public
    getter (only ``get_current_session_key``), so read the ContextVar
    defensively — a core that stops exporting it must degrade to "no id", never
    raise inside a tool call.
    """
    try:
        from tools import approval as _approval

        var = getattr(_approval, "_approval_tool_call_id", None)
        return str(var.get() or "").strip() if var is not None else ""
    except Exception:
        return ""


def _refusal(service: str, reason: str) -> str:
    return tool_result(ok=False, service=service, state="failed", reason=reason)


def _claim_session(session_key: str) -> bool:
    """At most one pending authorization per run."""
    with _bridges_lock:
        if session_key in _inflight_sessions:
            return False
        _inflight_sessions.add(session_key)
        return True


def _release_session(session_key: str) -> None:
    with _bridges_lock:
        _inflight_sessions.discard(session_key)


def _handle_request_authorization(args: Any = None, **_kwargs: Any) -> str:
    if not isinstance(args, dict):
        return _refusal("", "request_authorization expects an object with 'service' and 'scopes'.")

    extra = sorted(str(key) for key in args if key not in _ALLOWED_ARG_KEYS)
    if extra:
        # Owner / profile / session / url / token are broker state. A caller that
        # tries to supply one is either confused or probing — refuse, never trim.
        return _refusal(
            str(args.get("service") or ""),
            "request_authorization only accepts 'service' and 'scopes'. Remove: "
            + ", ".join(extra)
            + ". Identity, profile, session and the authorization URL are decided by "
            "the server and can never be supplied here.",
        )

    service = str(args.get("service") or "").strip()
    if service not in SUPPORTED_SERVICES:
        return _refusal(
            service,
            f"'{service or '(missing)'}' cannot be authorized inline. Supported services: "
            + ", ".join(SUPPORTED_SERVICES)
            + ". For anything else, tell the user to open the credential hub.",
        )

    scopes_raw = args.get("scopes")
    if not isinstance(scopes_raw, list):
        return _refusal(service, "'scopes' must be an array of scope strings.")
    scopes = [str(item).strip() for item in scopes_raw if isinstance(item, str) and str(item).strip()]
    if not scopes:
        return _refusal(
            service,
            "'scopes' must list at least one concrete scope you need; an empty request "
            "cannot be shown to the user.",
        )

    session_key = _current_session_key()
    if not session_key:
        return _refusal(
            service,
            "This run has no identified session, so no authorization can be requested. "
            "Ask the user to authorize from the credential hub instead.",
        )

    bridge = get_authorization_bridge(session_key)
    if bridge is None:
        # Fail closed: only a trusted WebUI run gets a bridge. Feishu / cron /
        # subprocess runs must not be able to raise an inline auth card.
        return _refusal(
            service,
            "Inline authorization is not available in this run. Ask the user to open "
            "the credential hub and authorize there.",
        )

    if not _claim_session(session_key):
        return _refusal(
            service,
            "An authorization request is already pending for this run. Wait for the user "
            "to answer it before asking for another credential.",
        )

    try:
        outcome = bridge(service=service, scopes=scopes, tool_call_id=_current_tool_call_id())
    except Exception:
        logger.debug("[multitenancy] authorization bridge failed", exc_info=True)
        return _refusal(service, "The authorization request could not be delivered.")
    finally:
        _release_session(session_key)

    if not isinstance(outcome, dict):
        return _refusal(service, "The authorization request returned no usable result.")

    state = str(outcome.get("state") or "").strip().lower()
    if state not in TERMINAL_STATES:
        state = "failed"
    reason = str(outcome.get("reason") or "").strip()
    if state == "expired":
        reason = _EXPIRED_REASON
    elif state == "cancelled":
        reason = reason or (
            "The user declined the authorization. Nothing was authorized; do not retry "
            "without being asked to."
        )
    elif state == "success":
        reason = reason or "The user authorized the requested access."
    elif not reason:
        reason = "The authorization request failed."

    return tool_result(ok=state == "success", service=service, state=state, reason=reason)


REQUEST_AUTHORIZATION_SCHEMA = {
    "name": "request_authorization",
    # The TRIGGER lives here, not in registry.register(description=...).
    # Verified in hermes-agent @ tools/registry.py: `get_tool_definitions` builds
    # each tool the model sees as `{**entry.schema, "name": entry.name}`, so the
    # model reads THIS "description". `entry.description` is registry-side
    # metadata that merely DEFAULTS FROM this field
    # (`description=description or schema.get("description", "")` in `register`)
    # and is never rendered into a prompt — trigger text put there would have
    # been invisible to the model.
    "description": (
        "Call this the moment lark-cli or kep-cli reports not logged in / auth required / "
        "token expired. Do NOT retry the CLI first — retrying cannot create a credential. "
        "It asks the human owner to authorize inline; the call blocks while they authorize "
        "in the browser and returns in place, so the work you already did this turn is "
        "preserved. On 'expired' or 'cancelled', stop and hand the decision back to the "
        "user — do not proceed on your own judgement and do not assume access."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "enum": list(SUPPORTED_SERVICES),
                "description": "Which credential you need. Only these three can be authorized inline.",
            },
            "scopes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "The concrete scopes/permissions you need, so the user can see what "
                    "they are agreeing to. Must not be empty."
                ),
            },
        },
        "required": ["service", "scopes"],
        "additionalProperties": False,
    },
}


if registry is not None:  # pragma: no cover - only inside a live hermes runtime
    registry.register_toolset_alias("request-authorization", "multitenancy_authorization")
    registry.register(
        name="request_authorization",
        toolset="multitenancy_authorization",
        schema=REQUEST_AUTHORIZATION_SCHEMA,
        handler=_handle_request_authorization,
        check_fn=lambda: True,
        requires_env=[],
        is_async=False,
        # Registry metadata only (tool lists / diagnostics). The model-facing
        # trigger text lives in REQUEST_AUTHORIZATION_SCHEMA["description"].
        description="Ask the owner to authorize a whitelisted credential without losing this turn.",
        emoji="🔐",
    )
