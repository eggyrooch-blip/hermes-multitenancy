"""Per-employee Figma remote-MCP OAuth — tokens live in that employee's profile home.

Why this module and not the connector catalog: the catalog path terminates at the
8767 sandbox gateway, which is not deployed in production and is wired to zero of
the 2184 profiles, so a catalog Figma row can never reach a real chat turn. The
hermes-agent core already speaks OAuth to Figma's hosted MCP natively
(``tools.mcp_oauth``), and the core reads its OAuth state from ``HERMES_HOME``,
which multitenancy pins to the employee's own ``profiles/<name>`` per run. So
authorizing here and writing through the core's ``HermesTokenStorage`` gives
per-employee isolation for free, and the resulting ``mcp_servers.figma`` block is
loaded by the core on the employee's next turn.

Three hard facts this module is built around:

1. Figma implements RFC 7591 dynamic client registration as a *name allowlist*:
   ``POST https://api.figma.com/v1/oauth/mcp/register`` 403s for every
   ``client_name`` outside a short fixed set. Upstream hermes-agent registers as
   ``"Claude Code"``; we reuse the core's own
   ``apply_oauth_provider_defaults`` so the name, the ``mcp:connect`` scope and
   the ``client_secret_post`` auth method always come from one place.
2. Figma's authorization-server metadata advertises
   ``authorization_response_iss_parameter_supported: true`` but the redirect
   omits ``iss``. mcp SDK >= 2.0 enforces RFC 9207, so the callback would fail.
   We fill ``iss`` from the discovered metadata issuer, and ONLY when that issuer
   is exactly ``https://api.figma.com`` (same narrowing as upstream #112059).
3. The run broker runs under mcp 1.x in some trees and mcp 2.x in others, and the
   two SDKs use different HTTP libraries (``httpx`` vs ``httpx2``) and different
   callback-handler return types. Everything version-dependent is funnelled
   through ``_sdk()`` / ``_callback_result()`` so the rest of the module is
   version-agnostic.

NEVER serialize a token, client secret, or authorization code from this module —
not into a ``ConnectorStatus``, not into a log line, not into profile config.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import parse_qs, urlsplit

import yaml

from .connectors.models import AuthAction, ConnectorStatus

logger = logging.getLogger(__name__)

CONNECTOR_ID = "figma"
PROVIDER = "figma"
TITLE = "Figma"
REMOTE_URL = "https://mcp.figma.com/mcp"
#: Key used both for ``mcp_servers.<key>`` in the profile config and for the
#: core's ``mcp-tokens/<key>.json`` state files. They must stay equal: the core
#: derives the storage name from the mcp_servers key.
MCP_SERVER_KEY = "figma"
FIGMA_ISSUER = "https://api.figma.com"
DEFAULT_CLIENT_NAME = "Claude Code"
CLIENT_NAME_ENV = "HERMES_FIGMA_OAUTH_CLIENT_NAME"
PUBLIC_ORIGIN_ENV = "HERMES_MCP_PUBLIC_ORIGIN"
#: Reuses the existing public OAuth callback the WebUI already exposes for the
#: connector catalog, so no new public route has to be opened.
CALLBACK_PATH = "/api/auth/skill-credentials/catalog/oauth/callback"
WHOAMI_TOOL = "whoami"
PENDING_TTL_SECONDS = 300.0
#: How long ``complete()`` waits for the token exchange + whoami before it gives
#: up AND kills the flow. Kept under the WebUI's own 60s callback budget so the
#: employee sees our redacted message instead of the proxy's generic timeout.
COMPLETE_TIMEOUT_SECONDS = 55.0
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
_DETAIL = "官方远程 MCP · 个人 OAuth · 一人一授权"


class ConnectorUnavailable(PermissionError):
    """A Figma connector operation cannot proceed; the message is user-facing."""


class FigmaTokenError(ConnectorUnavailable):
    """A token-endpoint failure reduced to a whitelisted code.

    The SDK's own ``OAuthTokenError`` carries the raw response body, and a
    pydantic ``ValidationError`` on a malformed token response embeds the
    payload it rejected — which is how a refresh token reached both the log and
    the exception chain (codex review 2026-09-21,
    ``_run_flow:raw-sdk-errors-leak-tokens``). Nothing here ever holds a secret,
    and the original exception is always suppressed with ``from None``.
    """


# --- secret redaction --------------------------------------------------------

#: OAuth error codes we are willing to repeat back. Anything else collapses to
#: ``unrecognized``: the token endpoint's body is provider-controlled and has
#: carried token material in practice.
_OAUTH_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
        "access_denied",
        "server_error",
        "temporarily_unavailable",
        "invalid_target",
        "invalid_client_metadata",
        "invalid_redirect_uri",
    }
)

_SECRET_KEYS = (
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "code_verifier",
    "authorization_code",
)
#: ``key<sep>value`` where the key is one of the above; the VALUE is replaced.
_SECRET_PATTERN = re.compile(
    r'(?i)(' + '|'.join(_SECRET_KEYS) + r')([\'"]?\s*[:=]\s*[\'"]?)([^\s,;&\'"})\]]+)'
)


def redact_secrets(text: Any) -> str:
    """Replace the VALUE after any token-ish key with ``<redacted>``."""
    return _SECRET_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", str(text))


class _SecretRedactingFilter(logging.Filter):
    """Scrubs token material out of records the MCP SDK logs about itself.

    The SDK wraps the token exchange in ``logger.exception("OAuth flow error")``,
    so the leak happens before any of our code sees the failure. The filter
    formats the record, redacts it, and folds a redacted traceback back in.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - a record we cannot format
            return True
        redacted = redact_secrets(message)
        if record.exc_info:
            import traceback

            trace = "".join(traceback.format_exception(*record.exc_info))
            redacted = f"{redacted}\n{redact_secrets(trace)}"
            record.exc_info = None
            record.exc_text = None
        record.msg = redacted
        record.args = ()
        return True


_LOG_FILTER = _SecretRedactingFilter()


def install_log_redaction(*modules: Any) -> None:
    """Attach the redaction filter to a module's ``logger``. Idempotent."""
    for module in modules:
        log = getattr(module, "logger", None)
        if not isinstance(log, logging.Logger):
            continue
        if not any(isinstance(existing, _SecretRedactingFilter) for existing in log.filters):
            log.addFilter(_LOG_FILTER)


# --- SDK / core compatibility ------------------------------------------------


def _sdk() -> tuple[Any, Any]:
    """Return ``(mcp.client.auth.oauth2 module, http client module)``.

    mcp 1.x builds its OAuth auth on ``httpx``; mcp 2.x on ``httpx2``. The
    oauth2 module itself imports exactly one of them, so asking the module is
    more reliable than guessing from installed distributions.
    """
    oauth2 = importlib.import_module("mcp.client.auth.oauth2")
    install_log_redaction(oauth2)
    http = getattr(oauth2, "httpx2", None) or getattr(oauth2, "httpx", None)
    if http is None:  # pragma: no cover - would mean an unknown SDK layout
        raise ConnectorUnavailable("MCP SDK HTTP client is unavailable")
    return oauth2, http


def _callback_result(oauth2: Any, code: str, state: str | None, iss: str | None) -> Any:
    """Build whatever the installed SDK's ``callback_handler`` must return.

    mcp 2.x wants an ``AuthorizationCodeResult`` (and validates ``iss`` against
    RFC 9207); mcp 1.x wants a plain ``(code, state)`` tuple and ignores ``iss``.
    """
    factory = getattr(oauth2, "AuthorizationCodeResult", None)
    if factory is None:
        return (code, state)
    return factory(code=code, state=state, iss=iss)


def fill_figma_iss(iss: str | None, metadata: Any) -> str | None:
    """Substitute the metadata issuer for a missing ``iss``, Figma only.

    Figma advertises ``authorization_response_iss_parameter_supported`` and then
    omits the parameter from the redirect. Narrow the workaround to the one
    issuer that is known to do it: any other server that omits ``iss`` while
    claiming support keeps failing validation, which is the point of RFC 9207.
    """
    if iss:
        return iss
    issuer = str(getattr(metadata, "issuer", "") or "").rstrip("/")
    if issuer == FIGMA_ISSUER:
        return issuer
    return iss


#: Symbols the core must expose for a Figma authorization to be possible at all.
#: ``apply_oauth_provider_defaults`` is not optional: it carries Figma's DCR
#: allowlist workaround, and a core without it (hermes-agent 0.14.0 is still
#: installed in some venvs) would register under a name Figma 403s.
_REQUIRED_CORE_SYMBOLS = ("HermesTokenStorage", "apply_oauth_provider_defaults")


def _core_mcp_oauth() -> Any:
    """Import ``tools.mcp_oauth`` from whichever hermes-agent core is on the path."""
    try:
        core = importlib.import_module("tools.mcp_oauth")
    except Exception as exc:  # ImportError, or a core that fails to initialize
        raise ConnectorUnavailable(
            "hermes-agent core does not expose MCP OAuth support"
        ) from exc
    install_log_redaction(core)
    missing = [name for name in _REQUIRED_CORE_SYMBOLS if not hasattr(core, name)]
    if missing:
        raise ConnectorUnavailable(
            "hermes-agent core is too old for Figma MCP OAuth "
            f"(missing {', '.join(missing)})"
        )
    return core


def core_available() -> bool:
    """True when the running core tree can do Figma MCP OAuth."""
    try:
        _core_mcp_oauth()
    except ConnectorUnavailable:
        return False
    return True


def client_name() -> str:
    """The DCR ``client_name``. Overridable for when Figma allowlists us properly."""
    configured = str(os.environ.get(CLIENT_NAME_ENV, "") or "").strip()
    return configured or DEFAULT_CLIENT_NAME


# --- paths -------------------------------------------------------------------


def profile_home(shared_home: Path | str, profile_name: str) -> Path:
    """``<shared>/profiles/<name>`` — the same directory MT pins HERMES_HOME to."""
    name = str(profile_name or "").strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\0" in name:
        raise ValueError("invalid profile_name")
    return Path(shared_home).resolve() / "profiles" / name


def token_file(home: Path | str) -> Path:
    """The core's token file for this server.

    Composed rather than read off ``HermesTokenStorage`` because status is a
    synchronous, hot read path. ``test_token_file_matches_core_storage_layout``
    pins this against the core so a layout change in the core fails a test
    instead of silently reporting every employee as unauthenticated.
    """
    return Path(home) / "mcp-tokens" / f"{MCP_SERVER_KEY}.json"


def account_file(home: Path | str) -> Path:
    """Non-secret cache of the verified Figma identity (display hint only)."""
    return Path(home) / "connectors" / f"{MCP_SERVER_KEY}.json"


def config_file(home: Path | str) -> Path:
    return Path(home) / "config.yaml"


def _token_storage(home: Path) -> Any:
    core = _core_mcp_oauth()
    return core.HermesTokenStorage(MCP_SERVER_KEY, hermes_home=str(home))


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


# --- redirect URI ------------------------------------------------------------


def resolve_redirect_uri(public_origin: Optional[str] = None) -> str:
    """Build the OAuth redirect URI, or explain why we cannot.

    HTTPS is required in general. Loopback HTTP is allowed so a developer box can
    run the whole flow without a tunnel — Figma accepts loopback redirects, and a
    loopback origin cannot leak the code to a third party.
    """
    origin = str(public_origin if public_origin is not None else os.environ.get(PUBLIC_ORIGIN_ENV, ""))
    origin = origin.strip().rstrip("/")
    if not origin:
        raise ConnectorUnavailable(
            f"{PUBLIC_ORIGIN_ENV} 未配置，请管理员在 run-broker 上设置公网回调地址后再授权。"
        )
    parsed = urlsplit(origin)
    host = (parsed.hostname or "").strip().lower()
    if not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
        raise ConnectorUnavailable(f"{PUBLIC_ORIGIN_ENV} 必须是纯 origin（scheme://host[:port]）。")
    if parsed.scheme == "https":
        pass
    elif parsed.scheme == "http" and host in _LOOPBACK_HOSTS:
        pass
    else:
        raise ConnectorUnavailable(f"{PUBLIC_ORIGIN_ENV} 必须是 https origin（本机 loopback 可用 http）。")
    return f"{origin}{CALLBACK_PATH}"


# --- profile config injection ------------------------------------------------


def _expected_server_block() -> dict[str, Any]:
    return {"url": REMOTE_URL, "auth": "oauth", "enabled": True}


def _load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConnectorUnavailable("profile config is invalid") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConnectorUnavailable("profile config must be an object")
    return loaded


def _is_managed_server_block(existing: Any) -> bool:
    """True only for the remote block THIS connector writes.

    Ownership, not name matching. A same-named stdio entry (a hand-written
    ``figma-bridge`` / TalkToFigma bridge) belongs to whoever put it there;
    deleting it on revoke — or on the rollback after a failed authorization —
    destroys the employee's own configuration for a key we never owned.
    """
    return isinstance(existing, dict) and str(existing.get("url") or "") == REMOTE_URL


def assert_config_injectable(home: Path | str) -> None:
    """Refuse a conflicting ``mcp_servers.figma`` BEFORE any side effect.

    Checked at ``start()``: a profile whose figma key is taken must fail while
    nothing has happened yet, not after the employee has already granted access
    at Figma and a token exists that we then have to throw away.
    """
    servers = _load_config(config_file(home)).get("mcp_servers")
    if servers is None:
        return
    if not isinstance(servers, dict):
        raise ConnectorUnavailable("profile mcp_servers config must be an object")
    existing = servers.get(MCP_SERVER_KEY)
    if existing is not None and not _is_managed_server_block(existing):
        raise ConnectorUnavailable("profile has a conflicting figma MCP server")


def has_managed_config(home: Path | str) -> bool:
    """True while our own ``mcp_servers.figma`` block is still on disk."""
    servers = _load_config(config_file(home)).get("mcp_servers")
    return isinstance(servers, dict) and _is_managed_server_block(servers.get(MCP_SERVER_KEY))


def inject_profile_config(home: Path | str) -> bool:
    """Add ``mcp_servers.figma`` to the profile config. Idempotent.

    Only called after a successful authorization: injecting into every profile
    would make all 2184 of them open a Figma MCP connection on every turn.
    """
    path = config_file(home)
    config = _load_config(path)
    servers = config.setdefault("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ConnectorUnavailable("profile mcp_servers config must be an object")
    expected = _expected_server_block()
    existing = servers.get(MCP_SERVER_KEY)
    # Anything already sitting on this key that is not our own remote block is a
    # conflict — including a stdio bridge with command/args and no url at all.
    # Overwriting it would silently break whatever put it there.
    if existing is not None and not _is_managed_server_block(existing):
        raise ConnectorUnavailable("profile has a conflicting figma MCP server")
    if existing == expected:
        return False
    servers[MCP_SERVER_KEY] = expected
    _atomic_text(path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    return True


def remove_profile_config(home: Path | str) -> bool:
    """Drop OUR ``mcp_servers.figma`` block. Idempotent, and foreign-safe.

    A block we do not own is left exactly as it was and reported as "nothing
    removed" — see ``_is_managed_server_block``.
    """
    path = config_file(home)
    if not path.exists():
        return False
    config = _load_config(path)
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        return False
    if not _is_managed_server_block(servers.get(MCP_SERVER_KEY)):
        return False
    servers.pop(MCP_SERVER_KEY, None)
    if not servers:
        config.pop("mcp_servers", None)
    _atomic_text(path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    return True


# --- account hint ------------------------------------------------------------


def _redact_hint(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if "@" in value:
        local, _, domain = value.partition("@")
        head = local[:2] if len(local) > 3 else local[:1]
        return f"{head}…@{domain}"
    return value if len(value) <= 7 else f"{value[:4]}…{value[-3:]}"


def extract_account_hint(payload: Any) -> str:
    """Pull a display identity out of a ``whoami`` result, then redact it.

    Figma's ``whoami`` shape is not contractual, so this walks the usual keys and
    degrades to an empty hint rather than guessing or raising.
    """
    candidate = _first_identity_value(payload, depth=0)
    return _redact_hint(candidate)


def _first_identity_value(payload: Any, *, depth: int) -> str:
    if depth > 6:
        return ""
    if isinstance(payload, str):
        stripped = payload.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return _first_identity_value(json.loads(stripped), depth=depth + 1)
            except Exception:
                return ""
        return ""
    if isinstance(payload, dict):
        for key in ("email", "handle", "username", "name", "display_name", "user_email"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            found = _first_identity_value(value, depth=depth + 1)
            if found:
                return found
        return ""
    if isinstance(payload, (list, tuple)):
        for value in payload:
            found = _first_identity_value(value, depth=depth + 1)
            if found:
                return found
    return ""


def _write_account_hint(home: Path, hint: str) -> None:
    _atomic_text(
        account_file(home),
        json.dumps({"account_hint": hint, "verified_at": int(time.time() * 1000)}, ensure_ascii=False),
    )


def _read_account_hint(home: Path) -> Optional[str]:
    try:
        data = json.loads(account_file(home).read_text(encoding="utf-8"))
    except Exception:
        return None
    hint = str((data or {}).get("account_hint") or "").strip()
    return hint or None


# --- status ------------------------------------------------------------------


def _token_state(home: Path) -> tuple[bool, Optional[int], bool]:
    """``(has_tokens, expires_at_ms, has_refresh_token)`` read straight off disk."""
    path = token_file(home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False, None, False
    if not isinstance(raw, dict) or not raw.get("access_token"):
        return False, None, False
    has_refresh = bool(raw.get("refresh_token"))
    absolute = raw.get("expires_at")
    if absolute is not None:
        try:
            return True, int(float(absolute) * 1000), has_refresh
        except (TypeError, ValueError):
            return True, None, has_refresh
    expires_in = raw.get("expires_in")
    if expires_in is not None:
        try:
            return True, int((path.stat().st_mtime + int(expires_in)) * 1000), has_refresh
        except (TypeError, ValueError, OSError):
            return True, None, has_refresh
    return True, None, has_refresh


def status(shared_home: Path | str, profile_name: str, open_id: str) -> ConnectorStatus:
    """Live, secret-free status of one employee's Figma authorization.

    ``open_id`` is accepted for signature parity with the other connectors; the
    Figma credential is physically scoped by profile home, so the profile name is
    what isolates it.
    """
    del open_id  # isolation is by profile_home; kept for call-site symmetry
    installed = core_available()
    state: str = "needs_auth"
    detail = _DETAIL
    expires_at: Optional[int] = None
    hint: Optional[str] = None
    try:
        home = profile_home(shared_home, profile_name)
    except ValueError:
        return _error_status(profile_name, "profile 名称非法")
    if not installed:
        state = "missing"
        detail = "当前 hermes-agent 核心不支持 MCP OAuth，无法连接 Figma。"
    else:
        try:
            has_tokens, expires_at, has_refresh = _token_state(home)
        except Exception:
            return _error_status(profile_name, "Figma 连接器状态暂不可用")
        if has_tokens:
            expired = expires_at is not None and expires_at <= int(time.time() * 1000)
            if expired and not has_refresh:
                state = "needs_auth"
                detail = f"{_DETAIL} · 授权已过期，请重新授权"
            else:
                state = "authenticated"
                hint = _read_account_hint(home)
    if state == "missing":
        action = None
    elif state == "authenticated":
        action = AuthAction(kind="oauth_url", label="重新授权")
    else:
        action = AuthAction(kind="oauth_url", label="授权")
    return ConnectorStatus(
        id=CONNECTOR_ID,
        title=TITLE,
        provider=PROVIDER,
        installed=installed,
        status=state,  # type: ignore[arg-type]
        expires_at=expires_at if state == "authenticated" else None,
        account_hint=hint,
        detail=detail,
        action=action,
        profile=profile_name,
        scope="profile",
        acting_identity="user",
        credential_owner=profile_name,
        # The data plane is the agent core opening mcp.figma.com with the
        # employee's own token — the run broker never proxies a Figma call, so it
        # is not the runtime policy owner here (unlike github-mcp).
        runtime_policy_owner="connector_driver",
        kind="external",
    )


def _error_status(profile_name: str, detail: str) -> ConnectorStatus:
    return ConnectorStatus(
        id=CONNECTOR_ID,
        title=TITLE,
        provider=PROVIDER,
        installed=False,
        status="error",
        detail=detail,
        action=AuthAction(kind="manual", label="重试"),
        profile=profile_name,
        scope="profile",
        acting_identity="user",
        credential_owner=profile_name,
        runtime_policy_owner="connector_driver",
        kind="external",
    )


# --- revoke ------------------------------------------------------------------


#: What the employee is told when a revoke could not finish. Deliberately says
#: nothing about paths or the underlying OSError.
_PURGE_FAILED = "Figma 凭据未能完全清除，请稍后重试或联系管理员。"


def revoke(shared_home: Path | str, profile_name: str) -> bool:
    """Delete this employee's Figma OAuth state and unwire the MCP server.

    Returns True when there was something to delete, and RAISES when the removal
    did not actually happen. Reporting a revoke that did not occur is the worst
    possible answer: the employee stops looking while the token is still on disk
    and the agent keeps loading it (codex review 2026-09-21,
    ``_purge:revoke-errors-swallowed``).

    Local-only: Figma exposes no token revocation endpoint for MCP clients, so
    the honest claim is "this profile can no longer use it", not "the grant is
    dead at Figma".
    """
    home = profile_home(shared_home, profile_name)
    had_tokens = token_file(home).exists()
    _purge(home)
    leftovers = [
        path
        for path in (
            token_file(home),
            _state_path(home, ".client.json"),
            _state_path(home, ".meta.json"),
            account_file(home),
        )
        if path.exists()
    ]
    if leftovers or has_managed_config(home):
        logger.error("figma revoke left %d artefact(s) behind (paths redacted)", len(leftovers))
        raise ConnectorUnavailable(_PURGE_FAILED)
    return had_tokens


def _state_path(home: Path | str, suffix: str) -> Path:
    return Path(home) / "mcp-tokens" / f"{MCP_SERVER_KEY}{suffix}"


def _unlink_or_fail(path: Path) -> None:
    """Delete one state file; a failure is reported, never swallowed."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.error("figma state removal failed type=%s (paths redacted)", type(exc).__name__)
        raise ConnectorUnavailable(_PURGE_FAILED) from None


def _purge(home: Path, *, drop_client_registration: bool = True) -> None:
    """Remove this profile's Figma state, or raise ``ConnectorUnavailable``.

    ``drop_client_registration=False`` keeps ``<server>.client.json``. The DCR
    registration is expensive and independent of the grant: re-registering costs
    a round trip to Figma and a new client_id, and Figma's registration endpoint
    is an allowlist we would rather not hammer. A failed code exchange says
    nothing about the client — see ``_purge_after_failed_flow``.
    """
    suffixes = (".json", ".client.json", ".meta.json") if drop_client_registration else (".json", ".meta.json")
    if drop_client_registration:
        try:
            _token_storage(home).remove()
        except ConnectorUnavailable:
            # Core missing: still remove the files we know about so a revoke is
            # not silently a no-op on a degraded tree.
            for suffix in suffixes:
                _unlink_or_fail(_state_path(home, suffix))
        except Exception as exc:
            logger.error("figma token removal failed type=%s (paths redacted)", type(exc).__name__)
            raise ConnectorUnavailable(_PURGE_FAILED) from None
    else:
        for suffix in suffixes:
            _unlink_or_fail(_state_path(home, suffix))
    _unlink_or_fail(account_file(home))
    # An unreadable profile config means we cannot prove the block is gone, so
    # that propagates too — callers that only want best effort catch it.
    remove_profile_config(home)


def _cleanup_after_abandoned_flow(home: Path) -> None:
    """Best-effort cleanup for a flow that was cancelled or timed out.

    Whatever the SDK already wrote (a token from a finished exchange, the meta
    file) has to go, because nothing verified it and nobody is waiting for it.
    The DCR registration survives: the client was never the problem.
    """
    try:
        _purge(home, drop_client_registration=False)
    except ConnectorUnavailable:
        logger.error("figma: cleanup after an abandoned authorization did not complete")


def _clear_grant_for_reauthorization(home: Path) -> bool:
    """Drop the grant so a fresh authorization can start. Keeps the registration.

    Returns True when there was a grant to clear. The config block goes too: an
    abandoned re-authorization must not leave the agent pointed at a Figma server
    it has no token for.
    """
    had_grant = token_file(home).exists()
    if not had_grant:
        return False
    for suffix in (".json", ".meta.json"):
        _state_path(home, suffix).unlink(missing_ok=True)
    account_file(home).unlink(missing_ok=True)
    try:
        remove_profile_config(home)
    except ConnectorUnavailable:
        logger.warning("figma re-auth: profile config unreadable, block left in place")
    logger.info("figma: cleared an existing grant to start re-authorization")
    return True


def is_invalid_client_error(exc: BaseException | str) -> bool:
    """True when the provider rejected the CLIENT, not the grant.

    ``invalid_grant`` means the authorization code was stale, replayed or bound
    to a different verifier — the registration is still perfectly good, so
    throwing it away just forces a pointless re-registration on the next try and
    loses the client_id the operator may already have been given. Only
    ``invalid_client`` (and an explicit 401 on the token endpoint) says the
    registration itself is no longer usable.
    """
    text = str(exc).lower()
    if "invalid_client" in text:
        return True
    return "unauthorized_client" in text


def _purge_after_failed_flow(home: Path, exc: BaseException) -> None:
    """Undo a half-finished authorization without over-deleting.

    Always drops tokens, the cached identity and the config block, because a
    token that could not be verified must never be left where the agent would
    load it. Keeps the client registration unless the provider said the client
    is the problem.
    """
    drop_client = is_invalid_client_error(exc)
    try:
        _purge(home, drop_client_registration=drop_client)
    except ConnectorUnavailable:
        # The caller is already raising the real failure; a cleanup problem is
        # logged, never substituted for it.
        logger.error("figma: rollback after a failed authorization did not complete")
    logger.warning(
        "figma authorization failed type=%s client_registration=%s",
        type(exc).__name__,
        "dropped" if drop_client else "kept",
    )


# --- OAuth broker ------------------------------------------------------------


async def _cancel_and_wait(task: "asyncio.Task[Any] | None") -> None:
    """Cancel a flow task and wait until it has actually stopped.

    ``gather(..., return_exceptions=True)`` so neither the CancelledError nor
    whatever the flow was failing with escapes into the caller — the caller is
    revoking or timing out and already has its own answer to give. It also
    retrieves the exception of an already-finished task, which keeps asyncio
    from logging it as never-retrieved.
    """
    if task is None:
        return
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@dataclass
class _Pending:
    profile_name: str
    home: Path
    redirect_uri: str
    callback: "asyncio.Future[tuple[str, str | None, str | None]]"
    redirect: "asyncio.Future[str]"
    provider: Any = None
    task: "asyncio.Task[dict[str, Any]] | None" = None
    expiry: "asyncio.Task[None] | None" = None
    state: str = ""
    consumed: bool = False


class FigmaOAuthBroker:
    """Drives the browser half of Figma's OAuth and persists the result.

    Mirrors ``CatalogOAuthBroker``: the SDK's ``OAuthClientProvider`` runs the
    whole RFC 8414 / 7591 / 7636 dance inside a background task, publishing the
    authorization URL through ``redirect_handler`` and then blocking in
    ``callback_handler`` until the public callback route calls ``complete()``.
    """

    def __init__(
        self,
        shared_home: Path | str,
        *,
        transport: Any = None,
        verify: Optional[Callable[[Path], Awaitable[dict[str, Any]]]] = None,
        flow_timeout: float = PENDING_TTL_SECONDS,
        complete_timeout: float = COMPLETE_TIMEOUT_SECONDS,
    ) -> None:
        self.shared_home = Path(shared_home)
        self.transport = transport
        self.verify = verify or verify_whoami
        self.flow_timeout = flow_timeout
        self.complete_timeout = complete_timeout
        self.pending: dict[str, _Pending] = {}
        #: profile_name -> the one flow that profile is allowed to have running.
        self._active: dict[str, _Pending] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # -- pending bookkeeping ------------------------------------------------

    def has_pending(self, state: str) -> bool:
        pending = self.pending.get(str(state or ""))
        return pending is not None and not pending.consumed

    def is_active(self, profile_name: str) -> bool:
        """True while one profile has an authorization in flight."""
        return self._active.get(str(profile_name or "")) is not None

    def _profile_lock(self, profile_name: str) -> asyncio.Lock:
        """Per-profile mutex.

        Built synchronously: there is no await between the lookup and the
        insert, so two concurrent ``start()`` coroutines cannot both decide the
        lock does not exist yet.
        """
        lock = self._locks.get(profile_name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[profile_name] = lock
        return lock

    def _forget(self, pending: _Pending) -> None:
        if pending.state and self.pending.get(pending.state) is pending:
            self.pending.pop(pending.state, None)
        if self._active.get(pending.profile_name) is pending:
            self._active.pop(pending.profile_name, None)

    async def cancel_profile(self, profile_name: str) -> bool:
        """Stop any in-flight authorization for one profile and WAIT for it.

        Revoke calls this first. Without it a callback already sitting in the
        employee's browser lands after the revoke and re-authorizes the profile,
        and a superseded flow can purge the tokens of the flow that replaced it
        (codex review 2026-09-21, ``figmaoauthbroker:profile-flow-races``).
        """
        name = str(profile_name or "")
        pending = self._active.get(name)
        if pending is None:
            return False
        pending.consumed = True
        self._forget(pending)
        if pending.expiry:
            pending.expiry.cancel()
        if not pending.callback.done():
            pending.callback.cancel()
        await _cancel_and_wait(pending.task)
        return True

    async def _expire(self, state: str, pending: _Pending) -> None:
        await asyncio.sleep(self.flow_timeout)
        if self.pending.get(state) is pending:
            pending.consumed = True
            self._forget(pending)
            pending.callback.cancel()
            await _cancel_and_wait(pending.task)
            _cleanup_after_abandoned_flow(pending.home)

    # -- start --------------------------------------------------------------

    async def start(
        self,
        profile_name: str,
        *,
        public_origin: Optional[str] = None,
    ) -> dict[str, str]:
        name = str(profile_name or "")
        home = profile_home(self.shared_home, name)
        if not home.is_dir():
            raise ConnectorUnavailable("profile home is unavailable")
        # Before anything is registered at Figma or written anywhere: a profile
        # whose figma key belongs to someone else can never be injected, so it
        # must fail here rather than after the employee has granted access.
        assert_config_injectable(home)
        async with self._profile_lock(name):
            # One live flow per profile. A second 授权 click supersedes the
            # first: cancel it and wait, so the loser can neither write tokens
            # after the winner nor purge the winner's on its way out.
            await self.cancel_profile(name)
            return await self._start_locked(name, home, public_origin)

    async def _start_locked(
        self,
        profile_name: str,
        home: Path,
        public_origin: Optional[str],
    ) -> dict[str, str]:
        redirect_uri = resolve_redirect_uri(public_origin)
        oauth2, _http = _sdk()
        core = _core_mcp_oauth()
        storage = core.HermesTokenStorage(MCP_SERVER_KEY, hermes_home=str(home))
        # An explicit start means "authorize again" — the card's 重新授权 button,
        # or switching from a personal Figma account to the company one. A still
        # valid token would make the probe request below return 200 instead of
        # 401, the SDK would never run the authorization flow, and start() would
        # fail with no URL to show. Clear the grant (never the registration) so
        # the 401 fires. Observed live 2026-09-21 on an authenticated profile.
        _clear_grant_for_reauthorization(home)
        # A registered client is pinned to the redirect URIs it registered with.
        # If HERMES_MCP_PUBLIC_ORIGIN has changed since (a dev loopback
        # registration reaching a production tree, a domain move), reusing the
        # stored client makes Figma reject the authorization with an opaque
        # redirect_uri mismatch. Drop it and register again instead.
        _drop_client_if_redirect_changed(home, redirect_uri)

        loop = asyncio.get_running_loop()
        pending = _Pending(
            profile_name=profile_name,
            home=home,
            redirect_uri=redirect_uri,
            callback=loop.create_future(),
            redirect=loop.create_future(),
        )
        self._active[profile_name] = pending

        async def redirect_handler(url: str) -> None:
            state = (parse_qs(urlsplit(url).query).get("state") or [""])[0]
            if not state or state in self.pending:
                raise ConnectorUnavailable("Figma OAuth state unavailable")
            pending.state = state
            self.pending[state] = pending
            pending.expiry = asyncio.create_task(self._expire(state, pending))
            if not pending.redirect.done():
                pending.redirect.set_result(url)

        async def callback_handler() -> Any:
            code, state, iss = await pending.callback
            metadata = getattr(getattr(pending.provider, "context", None), "oauth_metadata", None)
            return _callback_result(oauth2, code, state, fill_figma_iss(iss, metadata))

        provider = _build_provider(
            oauth2,
            _client_metadata(redirect_uri),
            storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )
        pending.provider = provider
        pending.task = asyncio.create_task(self._run_flow(pending, provider, storage))
        try:
            done, _ = await asyncio.wait(
                {pending.redirect, pending.task}, return_when=asyncio.FIRST_COMPLETED
            )
            if pending.task in done and not pending.redirect.done():
                # The flow died before producing a URL (403 DCR, discovery failure…).
                self._forget(pending)
                await pending.task
                raise ConnectorUnavailable("Figma 授权未能生成授权链接")
        except BaseException:
            # No half-registered flow may survive a failed start: it would still
            # hold this profile's slot and could still write to its home.
            self._forget(pending)
            await _cancel_and_wait(pending.task)
            raise
        return {"authorization_url": pending.redirect.result(), "state": pending.state}

    # -- complete -----------------------------------------------------------

    async def complete(self, state: str, code: str, *, iss: Optional[str] = None) -> dict[str, Any]:
        key = str(state or "")
        pending = self.pending.get(key)
        if pending is None or pending.consumed or not code:
            raise ConnectorUnavailable("Figma OAuth callback unavailable")
        pending.consumed = True
        if pending.expiry:
            pending.expiry.cancel()
        if not pending.callback.done():
            pending.callback.set_result((str(code), key, iss))
        task = pending.task
        if task is None:  # pragma: no cover - always set in start()
            self._forget(pending)
            raise ConnectorUnavailable("Figma OAuth task unavailable")
        try:
            # No ``shield``: a flow whose result nobody is waiting for any more
            # must not keep running and inject config minutes after the employee
            # was told the authorization failed (codex review 2026-09-21,
            # ``complete:timeout-leaves-live-flow``).
            return await asyncio.wait_for(task, timeout=self.complete_timeout)
        except asyncio.TimeoutError:
            await _cancel_and_wait(task)
            _cleanup_after_abandoned_flow(pending.home)
            raise ConnectorUnavailable("Figma 授权超时，请重新发起授权。") from None
        except asyncio.CancelledError:
            await _cancel_and_wait(task)
            _cleanup_after_abandoned_flow(pending.home)
            raise
        finally:
            self._forget(pending)

    # -- the flow itself ----------------------------------------------------

    async def _run_flow(self, pending: _Pending, provider: Any, storage: Any) -> dict[str, Any]:
        _oauth2, http = _sdk()
        try:
            async with http.AsyncClient(
                auth=provider,
                transport=self.transport,
                timeout=60,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    REMOTE_URL,
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                    },
                    json=_initialize_rpc(),
                )
                response.raise_for_status()

            metadata = getattr(getattr(provider, "context", None), "oauth_metadata", None)
            if metadata is not None and hasattr(storage, "save_oauth_metadata"):
                # Persist discovery so the core can refresh after a restart
                # without re-running the browser flow.
                storage.save_oauth_metadata(metadata)

            identity = await self.verify(pending.home)
            hint = extract_account_hint(identity)
            _write_account_hint(pending.home, hint)
            inject_profile_config(pending.home)
            return {
                "id": CONNECTOR_ID,
                "profile_name": pending.profile_name,
                "status": "authenticated",
                "account_hint": hint or None,
            }
        except Exception as exc:
            if self._active.get(pending.profile_name) is not pending:
                # Superseded or already revoked: this flow no longer owns the
                # profile's state and must not delete the current flow's tokens.
                logger.warning(
                    "figma: a superseded authorization failed type=%s; profile state left untouched",
                    type(exc).__name__,
                )
                raise
            # Half-authorized state is worse than none: a token that cannot call
            # whoami would sit in the profile and fail every turn silently. But
            # an invalid_grant says nothing about the client registration, so
            # that survives — see _purge_after_failed_flow.
            _purge_after_failed_flow(pending.home, exc)
            raise


def client_redirect_uris(home: Path | str) -> list[str]:
    """Redirect URIs the stored client registration was issued for (may be empty)."""
    path = Path(home) / "mcp-tokens" / f"{MCP_SERVER_KEY}.client.json"
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [str(u).rstrip("/") for u in (stored.get("redirect_uris") or []) if str(u).strip()]


def _drop_client_if_redirect_changed(home: Path, redirect_uri: str) -> bool:
    """Discard a stored registration that cannot serve ``redirect_uri``.

    Only the client registration is dropped, never a token: a still-valid token
    keeps working, and the next authorization simply registers a fresh client.
    """
    registered = client_redirect_uris(home)
    if not registered or redirect_uri.rstrip("/") in registered:
        return False
    (Path(home) / "mcp-tokens" / f"{MCP_SERVER_KEY}.client.json").unlink(missing_ok=True)
    logger.info("figma: dropped a client registration bound to a different redirect origin")
    return True


_provider_classes: dict[type, type] = {}


async def _oauth_error_code(response: Any) -> str:
    """The token endpoint's ``error`` code, if it is one we recognise."""
    try:
        body = await response.aread()
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        code = str((json.loads(body) or {}).get("error") or "").strip().lower()
    except Exception:
        return "unreadable"
    return code if code in _OAUTH_ERROR_CODES else "unrecognized"


def _redacting_provider_class(oauth2: Any) -> type:
    """``OAuthClientProvider`` whose token-endpoint failures carry no payload.

    Subclassed rather than wrapped because the SDK drives the provider through
    ``httpx.Auth.async_auth_flow`` and hands the token response straight to
    ``_handle_token_response``; that method is the only place the raw body is
    turned into an exception message.
    """
    base = oauth2.OAuthClientProvider
    cached = _provider_classes.get(base)
    if cached is not None:
        return cached

    class _RedactingOAuthClientProvider(base):  # type: ignore[misc,valid-type]
        async def _handle_token_response(self, response: Any) -> Any:
            status = getattr(response, "status_code", 0)
            if status != 200:
                raise FigmaTokenError(
                    f"figma token exchange rejected: status={status} "
                    f"error={await _oauth_error_code(response)}"
                )
            handler = getattr(base, "_handle_token_response", None)
            if handler is None:  # pragma: no cover - unknown SDK layout
                raise FigmaTokenError("figma token response handler unavailable")
            # The replacement is raised AFTER the except block has exited, so
            # the exception it replaces is not reachable through __context__
            # either — ``raise ... from None`` alone only hides it from the
            # traceback, it still hangs off the object with its payload.
            failure = ""
            try:
                return await handler(self, response)
            except FigmaTokenError:
                raise
            except Exception as exc:
                failure = type(exc).__name__
            raise FigmaTokenError(f"figma token response invalid: {failure}")

        async def _handle_refresh_response(self, response: Any) -> Any:
            handler = getattr(base, "_handle_refresh_response", None)
            if handler is None:  # pragma: no cover - unknown SDK layout
                raise FigmaTokenError("figma refresh handler unavailable")
            failure = ""
            try:
                return await handler(self, response)
            except FigmaTokenError:
                raise
            except Exception as exc:
                failure = type(exc).__name__
            raise FigmaTokenError(f"figma token refresh invalid: {failure}")

    _provider_classes[base] = _RedactingOAuthClientProvider
    return _RedactingOAuthClientProvider


def _build_provider(oauth2: Any, client_metadata: Any, storage: Any, **kwargs: Any) -> Any:
    """Every Figma OAuth provider in this module goes through here."""
    install_log_redaction(oauth2)
    return _redacting_provider_class(oauth2)(REMOTE_URL, client_metadata, storage, **kwargs)


def _client_metadata(redirect_uri: str) -> Any:
    """DCR metadata with Figma's allowlist workarounds applied by the core."""
    from mcp.shared.auth import OAuthClientMetadata

    core = _core_mcp_oauth()
    cfg: dict[str, Any] = {"client_name": client_name()}
    core.apply_oauth_provider_defaults(cfg, server_name=MCP_SERVER_KEY, server_url=REMOTE_URL)
    kwargs: dict[str, Any] = {
        "client_name": cfg["client_name"],
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": cfg.get("token_endpoint_auth_method") or "client_secret_post",
    }
    if cfg.get("scope"):
        kwargs["scope"] = cfg["scope"]
    return OAuthClientMetadata.model_validate(kwargs)


def _initialize_rpc() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": _protocol_version(),
            "capabilities": {},
            "clientInfo": {"name": "hermes-multitenancy", "version": "1"},
        },
    }


def _protocol_version() -> str:
    try:
        from .connector_remote_probe import _latest_protocol_version

        return _latest_protocol_version()
    except Exception:  # pragma: no cover - probe module always present in-tree
        return "2025-06-18"


# --- post-auth verification --------------------------------------------------


async def verify_whoami(home: Path) -> dict[str, Any]:
    """Prove the stored token works AND belongs to a real Figma identity.

    Uses the only zero-input read-only tool Figma exposes. A token that cannot
    answer ``whoami`` is treated as a failed authorization by ``_run_flow``.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    oauth2, http = _sdk()
    storage = _token_storage(home)
    provider = _build_provider(oauth2, _client_metadata_for_refresh(home), storage)
    async with http.AsyncClient(auth=provider, timeout=120) as client:
        async with streamable_http_client(REMOTE_URL, http_client=client) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(WHOAMI_TOOL, {})
    return whoami_payload(result)


def whoami_payload(result: Any) -> dict[str, Any]:
    """Accept a whoami result, or refuse the authorization.

    ``CallToolResult`` is a *successful* MCP response even when the tool failed:
    the SDK raises only on protocol errors, and a permission or service error
    arrives as ``isError=True`` with the reason in ``content``. Not checking it
    made every failing whoami read as "authenticated" while the token stayed on
    disk (codex review 2026-09-21, ``verify_whoami:tool-errors-accepted``).

    The refusal messages are fixed strings: a tool error body can quote the
    request, so none of it is safe to put in a card or a log line.
    """
    payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else {"result": result}
    is_error = bool(getattr(result, "isError", False))
    if not is_error and isinstance(payload, dict):
        is_error = bool(payload.get("isError") or payload.get("is_error"))
    if is_error:
        raise ConnectorUnavailable("Figma 授权校验失败：whoami 返回错误，请重新授权。")
    if not extract_account_hint(payload):
        raise ConnectorUnavailable("Figma 授权校验失败：whoami 未返回可识别的账号身份。")
    return payload


def _client_metadata_for_refresh(home: Path) -> Any:
    """Client metadata for a token-only session (no browser step).

    The registered client is already on disk; the redirect URI only has to be
    structurally valid for the SDK, and is never presented to Figma again.
    """
    from mcp.shared.auth import OAuthClientMetadata

    try:
        stored = json.loads((Path(home) / "mcp-tokens" / f"{MCP_SERVER_KEY}.client.json").read_text(encoding="utf-8"))
        redirect_uris = [str(u) for u in (stored.get("redirect_uris") or []) if str(u).strip()]
    except Exception:
        redirect_uris = []
    if not redirect_uris:
        redirect_uris = ["http://127.0.0.1:0/callback"]
    return OAuthClientMetadata.model_validate(
        {
            "client_name": client_name(),
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
            "scope": "mcp:connect",
        }
    )


# --- process-wide broker registry --------------------------------------------

_brokers: dict[Path, FigmaOAuthBroker] = {}


def get_broker(shared_home: Path | str) -> FigmaOAuthBroker:
    """One broker per shared home so start() and the callback share pending state."""
    path = Path(shared_home).resolve()
    if path not in _brokers:
        _brokers[path] = FigmaOAuthBroker(path)
    return _brokers[path]


def reset_brokers() -> None:
    """Test hook — drop every cached broker (and therefore every pending flow)."""
    _brokers.clear()
