"""LIVE verification that a whitelisted credential is authorized right now.

Every caller of this module is answering one question: "may I unblock a waiting
agent turn and tell it the credential is there?" A cached or file-only answer is
not good enough for that — ``credential_hub/readers/lark.py:lark_cli_status``
happily reports ``authenticated`` for a token that was revoked an hour ago, and
a forged confirm would then hand the agent a credential the user never granted.

So both checks here go to the wire:

* kep-cli — reuse ``credential_hub_auth.kep_cli_logged_in``, which is already
  cache-free (keyring file + two ``kep-auth`` subprocesses + a live HTTPS
  identity probe). ``online`` and ``pre`` are INDEPENDENT targets and are never
  substituted for one another.
* lark-cli — take the credential through the RUNTIME's own selection path
  (vault-or-JSON, freshest wins), refresh if needed, reject an expired one,
  require the stored grant to COVER the scopes this request froze, then call
  Feishu's user-info endpoint with the token and require the returned
  ``open_id`` to be the owner we are authorizing for.

Three identity/authority rules, all fail-closed:

1. **Scopes are server policy, not model input.** ``normalize_requested_scopes``
   runs BEFORE anything is registered. Whatever the model typed is normalized,
   shape-checked and allow-listed; only the result is frozen on the record, put
   on the consent card, and required to be covered by the credential at both the
   initial and the confirmation probe.
2. **The actor owns the credential.** kep-cli has one credential store per
   PROFILE and no per-actor dimension, so on a shared agent (owner's profile +
   grantee's identity) the owner's login would otherwise satisfy the grantee's
   request. ``_verify_kep_cli`` therefore refuses whenever the requesting
   ``open_id`` does not route to ``profile_name``.
3. **One credential source.** ``_verify_lark_cli`` reads through the same
   selection the resumed tool call will use, so it can never verify one token
   while lark-cli goes on to use another.

Unknown service, missing payload, network failure, identity mismatch, scope
shortfall and any exception all return ``False``. ``configured`` and ``unknown``
are never success.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from .credential_hub.model import KEP_CLI_ONLINE, KEP_CLI_PRE, LARK_CLI

logger = logging.getLogger(__name__)

SUPPORTED_SERVICES: tuple[str, ...] = (LARK_CLI, KEP_CLI_ONLINE, KEP_CLI_PRE)

# The env each kep service id verifies against. Keep this mapping literal: an
# ``online`` request satisfied by a ``pre`` credential would be a real
# cross-environment authorization bug.
KEP_ENV_BY_SERVICE: dict[str, str] = {KEP_CLI_ONLINE: "online", KEP_CLI_PRE: "pre"}

# kep-cli has NO scope model: `kep-auth login` grants whatever the human account
# already has, and nothing narrower can be requested. A model-chosen scope
# string would therefore be pure consent-card decoration that the server never
# enforces — exactly the "arbitrary string reaches the user as consent copy"
# hole. So the server replaces the request with this literal.
KEP_FIXED_SCOPES: tuple[str, ...] = ("kep-cli",)

# Feishu scope shape (``docx:document:readonly``, ``offline_access``, …).
# Anything that is not a plausible scope token is refused before it can reach
# either the OAuth call or the card.
_SCOPE_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]*(?:[.:-][a-z0-9_]+)*$")
_MAX_REQUESTED_SCOPES = 20


class ScopePolicyError(ValueError):
    """A requested scope set the server refuses to register."""


def _clean_scope_list(scopes: Iterable[Any]) -> list[str]:
    seen: list[str] = []
    for raw in scopes or ():
        value = str(raw or "").strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def lark_allowed_scopes(shared_home: Any) -> frozenset[str]:
    """The scope set this Feishu app is actually granted.

    Server policy, read from the app itself rather than invented here. Empty
    result = "policy unknown", and the caller MUST refuse rather than fall back
    to a permissive literal list — a hardcoded fallback would silently become
    the allowlist on any host where app credentials cannot be read.
    """
    try:
        from . import feishu_uat_auth

        granted = feishu_uat_auth.parse_scopes(
            feishu_uat_auth.login_oauth_scope(shared_home=Path(shared_home))
        )
    except Exception:
        logger.debug("[multitenancy] lark scope policy lookup failed", exc_info=True)
        return frozenset()
    return frozenset(scope for scope in granted if scope)


def normalize_requested_scopes(
    service: str, scopes: Iterable[Any], *, shared_home: Any
) -> list[str]:
    """Server-side normalization + allow-listing. Raises ``ScopePolicyError``.

    Runs BEFORE registration so a refused scope never reaches the pending
    registry, the consent card, or Feishu's OAuth endpoint.
    """
    service = str(service or "").strip()
    requested = _clean_scope_list(scopes)
    if not requested:
        raise ScopePolicyError("the authorization request named no scopes")
    if len(requested) > _MAX_REQUESTED_SCOPES:
        raise ScopePolicyError("the authorization request named too many scopes")

    if service in KEP_ENV_BY_SERVICE:
        # Fixed by the server; the model's wording is discarded on purpose.
        return list(KEP_FIXED_SCOPES)

    if service != LARK_CLI:
        raise ScopePolicyError("this service cannot be authorized inline")

    malformed = [scope for scope in requested if not _SCOPE_TOKEN_RE.match(scope)]
    if malformed:
        raise ScopePolicyError("the authorization request named a malformed scope")

    allowed = lark_allowed_scopes(shared_home)
    if not allowed:
        # Fail closed: without the app's granted set there is no policy to
        # enforce, and "allow everything" is precisely the defect being fixed.
        raise ScopePolicyError("the server cannot determine the permitted scope set")
    forbidden = [scope for scope in requested if scope not in allowed]
    if forbidden:
        raise ScopePolicyError("the authorization request named a scope this app cannot grant")
    return sorted(set(requested))


def _granted_scopes_from_payload(payload: dict[str, Any]) -> frozenset[str]:
    from . import feishu_uat_auth

    raw = payload.get("scope") or payload.get("scopes") or ""
    return frozenset(feishu_uat_auth.parse_scopes(raw))


def verify_service_authorized(
    service: str,
    *,
    profile_name: str,
    open_id: str,
    profile_dir: Any,
    shared_home: Any,
    required_scopes: Sequence[str] = (),
) -> bool:
    """True only when ``service`` is authorized for this owner AT THIS MOMENT.

    ``required_scopes`` is the frozen, already-normalized set from the pending
    record — never raw model input. An empty set is NOT "no requirement": the
    registry always freezes at least one scope, so an empty set here means the
    caller lost it, and lark verification refuses.
    """
    service = str(service or "").strip()
    profile_name = str(profile_name or "").strip()
    open_id = str(open_id or "").strip()
    required = tuple(_clean_scope_list(required_scopes))
    if not service or not profile_name:
        return False

    if service in KEP_ENV_BY_SERVICE:
        return _verify_kep_cli(
            service,
            profile_name=profile_name,
            open_id=open_id,
            profile_dir=Path(profile_dir),
            shared_home=Path(shared_home),
            required_scopes=required,
        )
    if service == LARK_CLI:
        return _verify_lark_cli(
            profile_name=profile_name,
            open_id=open_id,
            shared_home=Path(shared_home),
            required_scopes=required,
        )
    return False


def _actor_owns_profile(shared_home: Path, profile_name: str, open_id: str) -> bool:
    """Does ``open_id`` route to ``profile_name`` as its OWN profile?

    Same routing source ``feishu_uat_auth._assert_route`` uses for the Feishu
    device flow. On a shared agent the run carries the OWNER's profile with the
    GRANTEE's identity, so this is False and the caller must refuse.
    """
    try:
        from . import feishu_uat_auth

        routed = feishu_uat_auth._profile_name_for_open_id(shared_home, open_id)
    except Exception:
        logger.debug("[multitenancy] authorization actor routing lookup failed", exc_info=True)
        return False
    return str(routed or "").strip() == profile_name


def _verify_kep_cli(
    service: str,
    *,
    profile_name: str,
    open_id: str,
    profile_dir: Path,
    shared_home: Path,
    required_scopes: Sequence[str] = (),
) -> bool:
    if not open_id:
        return False
    if not _actor_owns_profile(shared_home, profile_name, open_id):
        # Shared agent (owner's profile, grantee's identity): the only kep-cli
        # credential reachable from here belongs to the OWNER. Declaring the
        # grantee authorized on it — or starting a login INTO the owner's
        # profile on the grantee's behalf — is cross-user credential use.
        # Refuse until kep-cli has an actor-owned store (see DEBT.md).
        logger.info(
            "[multitenancy] kep-cli inline authorization refused: actor does not own this profile"
        )
        return False
    if set(required_scopes) - set(KEP_FIXED_SCOPES):
        # kep-cli logins cannot be narrowed; a record carrying anything else was
        # not normalized by this module.
        return False
    env_name = KEP_ENV_BY_SERVICE[service]
    try:
        from . import credential_hub_auth

        return bool(
            credential_hub_auth.kep_cli_logged_in(
                profile_dir,
                profile_name,
                shared_home,
                env_name=env_name,
            )
        )
    except Exception:
        logger.debug("[multitenancy] kep-cli live verification failed", exc_info=True)
        return False


def _verify_lark_cli(
    *,
    profile_name: str,
    open_id: str,
    shared_home: Path,
    required_scopes: Sequence[str] = (),
) -> bool:
    if not open_id:
        return False
    required = frozenset(required_scopes)
    if not required:
        return False
    try:
        from . import feishu_uat_auth

        # The RUNTIME's selection path, not the plaintext JSON file: lark-cli
        # resolves vault-or-JSON (freshest wins) and may lease through the
        # broker, so reading only the JSON both prompts needlessly for a
        # vault-only credential and can verify a different token than the
        # resumed tool call uses.
        feishu_uat_auth.refresh_uat_if_needed(
            profile_name=profile_name,
            open_id=open_id,
            shared_home=shared_home,
            headroom_seconds=300,
        )
        payload = feishu_uat_auth._load_best_uat_payload(shared_home, profile_name, open_id)
        if not payload:
            return False
        expires_at = feishu_uat_auth._as_int(
            payload.get("expires_at")
            or payload.get("expire_at")
            or payload.get("access_token_expires_at")
        )
        if expires_at and expires_at <= feishu_uat_auth._now_ms():
            return False
        if not required <= _granted_scopes_from_payload(payload):
            # The stored grant does not cover what this request froze. Treat as
            # "not authorized" so the user is asked for the missing consent
            # instead of the agent silently running with a narrower token.
            logger.info("[multitenancy] lark-cli credential does not cover the requested scopes")
            return False
        token = ""
        for key in ("access_token", "user_access_token", "token"):
            token = str(payload.get(key) or "").strip()
            if token:
                break
        if not token:
            return False
        user_info = feishu_uat_auth._fetch_user_info(token)
    except Exception:
        logger.debug("[multitenancy] lark-cli live verification failed", exc_info=True)
        return False

    if not isinstance(user_info, dict):
        return False
    return str(user_info.get("open_id") or "").strip() == open_id
