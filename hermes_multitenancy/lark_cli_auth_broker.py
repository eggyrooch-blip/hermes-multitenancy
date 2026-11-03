"""Credential-vault backed auth broker for lark-cli authsidecar requests."""
from __future__ import annotations

import hashlib
import http.server
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional


PROTOCOL_VERSION = "v1"
MAX_TIMESTAMP_DRIFT_SECONDS = 60
ALLOWED_HOSTS = frozenset({"open.feishu.cn", "open.larksuite.com"})
ALLOWED_AUTH_HEADERS = frozenset({"Authorization", "X-Lark-MCP-UAT", "X-Lark-MCP-TAT"})
PROXY_HEADERS = frozenset(
    {
        "x-lark-proxy-version",
        "x-lark-proxy-target",
        "x-lark-proxy-identity",
        "x-lark-proxy-auth-header",
        "x-lark-proxy-signature",
        "x-lark-proxy-timestamp",
        "x-lark-body-sha256",
    }
)
HOP_BY_HOP_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
    }
)
_PERSONAL_FEISHU_IM_USER_AUTH_REQUIRED = (
    "飞书个人消息读取需要先完成本人授权。"
    "请在飞书私聊 Hermes 发送 `/feishu_auth`，"
    "或在 WebUI「凭证」页点击 Lark-cli 的「授权/重新授权」。"
)
_CRED_RESOLVE_BACKOFFS = (0.2, 0.4, 0.8)


class CredentialExpiredError(PermissionError):
    """Terminal: stored UAT present but expired — re-auth required, never retry."""


class CredentialIdentityVerificationError(PermissionError):
    """Terminal: a UAT cannot be proven to belong to the routed Feishu actor."""


@dataclass(frozen=True)
class BrokerResponse:
    status: int
    headers: dict[str, str] | None = None
    body: bytes = b""


@dataclass(frozen=True)
class LarkCliAuthBrokerContext:
    shared_home: Path
    profile_name: str
    user_open_id: str
    hmac_key: str
    allowed_identities: frozenset[str] = frozenset({"user"})
    request_timeout_seconds: float = 30.0
    profile_kind: str = "user"
    current_chat_id: str = ""
    allowed_bot_chat_ids: frozenset[str] = frozenset()
    # Thread-safe callback invoked once when a stored UAT is present but expired
    # (CredentialExpiredError). Runs on the http.server handler thread, so the
    # sink must guard its own state. Left None on paths that don't need the
    # signal (e.g. the non-streaming / Feishu path). Holds a callable, never
    # mutated after construction — safe on this frozen dataclass.
    credential_expiry_sink: Optional[Callable[[dict], None]] = None
    # Same contract, but for a forwarded Feishu response carrying an app/user
    # scope-missing code (99991672 / 99991679). Unlike expiry (raised BEFORE the
    # forward when the stored UAT is stale), a permission error is only visible in
    # the response Feishu returns, so it fires AFTER the forward. Drives the JIT
    # device-code auth card + original-request replay in the streaming run scope.
    permission_denied_sink: Optional[Callable[[dict], None]] = None


Forwarder = Callable[[str, str, Mapping[str, str], bytes, float], BrokerResponse]


class LarkCliAuthBroker:
    """Validate lark-cli sidecar requests and inject vault credentials.

    This object is intentionally HTTP-framework neutral. The Run Broker sidecar
    can wrap it with aiohttp/http.server while tests exercise the security
    boundary without opening sockets.
    """

    def __init__(
        self,
        context: LarkCliAuthBrokerContext,
        *,
        forwarder: Forwarder | None = None,
    ) -> None:
        self.context = context
        self.forwarder = forwarder or _urllib_forwarder
        self._validated_user_token_fingerprints: set[bytes] = set()
        self._user_identity_validation_lock = threading.Lock()

    def handle(
        self,
        *,
        method: str,
        path_and_query: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> BrokerResponse:
        method = method.upper()
        if not path_and_query.startswith("/"):
            return _error(400, "invalid request path")

        header_map = _case_insensitive(headers)
        version = header_map.get("x-lark-proxy-version", "")
        if version != PROTOCOL_VERSION:
            return _error(400, "unsupported X-Lark-Proxy-Version")

        body_sha = header_map.get("x-lark-body-sha256", "")
        if not body_sha:
            return _error(400, "missing X-Lark-Body-SHA256")
        actual_sha = hashlib.sha256(body).hexdigest()
        if not hmac.compare_digest(body_sha, actual_sha):
            return _error(400, "body SHA256 mismatch")

        target = header_map.get("x-lark-proxy-target", "")
        host, target_error = _parse_target(target)
        if target_error:
            return _error(403, target_error)

        identity = header_map.get("x-lark-proxy-identity", "")
        auth_header = header_map.get("x-lark-proxy-auth-header", "")
        timestamp = header_map.get("x-lark-proxy-timestamp", "")
        signature = header_map.get("x-lark-proxy-signature", "")
        missing = [
            name
            for name, value in (
                ("X-Lark-Proxy-Identity", identity),
                ("X-Lark-Proxy-Auth-Header", auth_header),
                ("X-Lark-Proxy-Timestamp", timestamp),
                ("X-Lark-Proxy-Signature", signature),
            )
            if not value
        ]
        if missing:
            return _error(400, "missing " + ", ".join(missing))

        sig_error = _verify_signature(
            key=self.context.hmac_key.encode("utf-8"),
            version=version,
            method=method,
            host=host,
            path_and_query=path_and_query,
            body_sha=body_sha,
            timestamp=timestamp,
            identity=identity,
            auth_header=auth_header,
            signature=signature,
        )
        if sig_error:
            return _error(401, sig_error)

        if host not in ALLOWED_HOSTS:
            return _error(403, "target host not allowed")
        if identity not in self.context.allowed_identities:
            return _error(403, "identity not allowed")
        if auth_header not in ALLOWED_AUTH_HEADERS:
            return _error(403, "auth-header not allowed")
        im_policy_error = _im_read_policy_error(
            self.context,
            identity=identity,
            method=method,
            path_and_query=path_and_query,
        )
        if im_policy_error:
            return _error(403, im_policy_error)
        bot_policy_error = _personal_bot_identity_policy_error(
            self.context,
            identity=identity,
            method=method,
            path_and_query=path_and_query,
            body=body,
        )
        if bot_policy_error:
            return _error(403, bot_policy_error)

        token = None
        last_transient = "credential unavailable"
        attempts = 1 + len(_CRED_RESOLVE_BACKOFFS)
        for attempt in range(attempts):
            try:
                token = self._resolve_token(identity)
                break
            except CredentialIdentityVerificationError:
                return _error(503, "credential identity verification failed")
            except CredentialExpiredError:
                self._signal_credential_expiry()
                return _error(503, "credential expired")
            except PermissionError:
                last_transient = "credential unavailable"
            except Exception:
                last_transient = "credential lookup failed"
            if attempt < len(_CRED_RESOLVE_BACKOFFS):
                time.sleep(_CRED_RESOLVE_BACKOFFS[attempt])
            else:
                return _error(503, last_transient)

        forward_headers = _forward_headers(headers)
        forward_headers.pop("Authorization", None)
        forward_headers.pop("X-Lark-MCP-UAT", None)
        forward_headers.pop("X-Lark-MCP-TAT", None)
        if auth_header == "Authorization":
            forward_headers["Authorization"] = "Bearer " + token
        else:
            forward_headers[auth_header] = token

        response = self.forwarder(
            method,
            "https://" + host + path_and_query,
            forward_headers,
            body,
            self.context.request_timeout_seconds,
        )
        self._maybe_signal_permission_denied(identity, response.body)
        return response

    def _resolve_token(self, identity: str) -> str:
        from .credentials import CredentialStore

        if identity == "user":
            _refresh_profile_uat_if_needed(
                self.context.shared_home,
                self.context.profile_name,
                self.context.user_open_id,
            )
        json_payload = (
            _load_profile_uat_json(
                self.context.shared_home,
                self.context.profile_name,
                self.context.user_open_id,
            )
            if identity == "user"
            else {}
        )
        try:
            store = CredentialStore(self.context.shared_home / "multitenancy.db")
        except Exception:
            if identity == "user" and json_payload:
                return self._validated_user_token(json_payload)
            raise PermissionError("credential unavailable")

        try:
            if identity == "user":
                payload = self._resolve_user_payload(store, json_payload=json_payload)
                return self._validated_user_token(payload)
            payload = store.get_secret_for_runtime(
                profile_name="__global__",
                subject_id="feishu_app",
                provider="feishu",
                secret_kind="app",
            )
            try:
                return _first_token(payload, ("tenant_access_token", "app_access_token", "token"))
            except PermissionError:
                return _mint_tenant_access_token(payload, timeout=self.context.request_timeout_seconds)
        finally:
            store.close()

    def _validated_user_token(self, payload: Mapping[str, object]) -> str:
        _validate_user_payload_identity(payload, self.context.user_open_id)
        token = _first_token(payload, ("access_token", "user_access_token", "token"))
        fingerprint = hashlib.sha256(token.encode("utf-8")).digest()
        with self._user_identity_validation_lock:
            if fingerprint in self._validated_user_token_fingerprints:
                return token
            try:
                from .feishu_uat_auth import _fetch_user_info

                user_info = _fetch_user_info(token)
            except Exception as exc:
                raise CredentialIdentityVerificationError(
                    "credential identity verification unavailable"
                ) from exc
            actual_open_id = str(user_info.get("open_id") or "").strip()
            expected_open_id = str(self.context.user_open_id or "").strip()
            if not expected_open_id or actual_open_id != expected_open_id:
                raise CredentialIdentityVerificationError("credential identity mismatch")
            self._validated_user_token_fingerprints.add(fingerprint)
        return token

    def _resolve_user_payload(self, store: object, *, json_payload: dict | None = None) -> dict:
        from .credential_broker import BrokerClientError, fetch_via_broker, running_as_broker_child

        if running_as_broker_child():
            try:
                payload = fetch_via_broker(
                    kind="feishu_uat",
                    profile_name=self.context.profile_name,
                    open_id=self.context.user_open_id,
                    run_id=os.environ.get("HERMES_MULTITENANCY_RUN_ID", ""),
                )
            except BrokerClientError:
                raise PermissionError("credential unavailable")
            if not payload:
                raise PermissionError("credential unavailable")
            if _payload_is_expired(payload):
                raise CredentialExpiredError("credential expired")
            return payload
        json_payload = json_payload or {}
        try:
            vault_payload = store.get_secret_for_runtime(
                profile_name=self.context.profile_name,
                subject_id=self.context.user_open_id,
                provider="feishu",
                secret_kind="uat",
            )
        except Exception:
            if json_payload:
                if _payload_is_expired(json_payload):
                    raise CredentialExpiredError("credential expired")
                return json_payload
            raise
        if not json_payload:
            if _payload_is_expired(vault_payload):
                raise CredentialExpiredError("credential expired")
            return vault_payload
        if _payload_freshness(json_payload) > _payload_freshness(vault_payload):
            selected = json_payload
        else:
            selected = vault_payload
        if _payload_is_expired(selected):
            raise CredentialExpiredError("credential expired")
        return selected

    def _signal_credential_expiry(self) -> None:
        """Notify the run scope that a stored UAT expired (re-auth needed).

        Best-effort: a broken sink must never turn the 503 into a 500.
        """
        sink = self.context.credential_expiry_sink
        if sink is None:
            return
        try:
            sink({"provider": "feishu", "connector_id": "lark-cli"})
        except Exception:
            pass

    def _maybe_signal_permission_denied(self, identity: str, body: bytes) -> None:
        """Fire the permission sink when a forwarded Feishu response is a genuine
        app/user scope-missing error (code 99991672 / 99991679).

        Cheap fast-path: a normal success response never contains those digit runs,
        so a bytes substring check short-circuits before any JSON parse. Structured
        confirm (``classify_lark_error``: STRUCTURED FIELDS ONLY) then rejects a
        coincidental digit run inside data — only a top-level ``code`` classifies.
        Best-effort: a broken sink or unparseable body must never disturb the
        proxied response.
        """
        sink = self.context.permission_denied_sink
        if sink is None:
            return
        # 群/智能体 profile 按设计只用应用(bot)身份 (sunke: 群里只用应用身份、不授权
        # 个人 lark-cli)。用户授权卡只对 USER 身份的调用有意义 —— bot/群一律不触发。
        if identity != "user":
            return
        if b"99991672" not in body and b"99991679" not in body:
            return
        try:
            from .feishu_permission_errors import classify_lark_error

            label = classify_lark_error(json.loads(body.decode("utf-8")))
        except Exception:
            return
        if label is None:
            return
        try:
            sink(
                {
                    "provider": "feishu",
                    "connector_id": "lark-cli",
                    "identity": identity,
                    "scope_status": label,  # app_scope_missing | user_scope_insufficient
                }
            )
        except Exception:
            pass


class RunningLarkCliAuthBrokerServer:
    def __init__(self, server: http.server.ThreadingHTTPServer, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread
        host, port = server.server_address[:2]
        self.url = f"http://{host}:{port}"
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        # Idempotent: the scope that owns this server can be exited more than
        # once (turn finalization plus the caller's own finally), and a second
        # server_close() on an already-closed socket must not raise into the
        # teardown path that still has to release the worker and tmpdirs.
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def start_lark_cli_auth_broker_server(
    context: LarkCliAuthBrokerContext,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    forwarder: Forwarder | None = None,
) -> RunningLarkCliAuthBrokerServer:
    """Start a localhost HTTP wrapper for lark-cli authsidecar traffic."""
    broker = LarkCliAuthBroker(context, forwarder=forwarder)

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
            self._handle()

        def do_POST(self) -> None:  # noqa: N802 - stdlib hook name
            self._handle()

        def do_PUT(self) -> None:  # noqa: N802 - stdlib hook name
            self._handle()

        def do_PATCH(self) -> None:  # noqa: N802 - stdlib hook name
            self._handle()

        def do_DELETE(self) -> None:  # noqa: N802 - stdlib hook name
            self._handle()

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length) if length else b""
            response = broker.handle(
                method=self.command,
                path_and_query=self.path,
                headers={key: value for key, value in self.headers.items()},
                body=body,
            )
            self.send_response(response.status)
            for key, value in _response_headers(response.headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            if response.body:
                self.wfile.write(response.body)

    server = http.server.ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="lark-cli-auth-broker", daemon=True)
    thread.start()
    return RunningLarkCliAuthBrokerServer(server, thread)


def _first_token(payload: Mapping[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    raise PermissionError("credential payload does not contain a usable token")


def _validate_user_payload_identity(payload: Mapping[str, object], expected_open_id: str) -> None:
    actual_open_id = str(payload.get("user_open_id") or "").strip()
    expected_open_id = str(expected_open_id or "").strip()
    if not expected_open_id or (actual_open_id and actual_open_id != expected_open_id):
        raise CredentialIdentityVerificationError("credential identity mismatch")


_IM_READ_PATH_PREFIXES = (
    "/open-apis/im/v1/messages",
    "/open-apis/im/v1/chats",
    "/open-apis/im/v1/flags",
    "/open-apis/im/v1/threads",
)
_IM_READ_EXACT_METHODS = {
    ("POST", "/open-apis/im/v1/messages/search"),
}


def _im_read_policy_error(
    context: LarkCliAuthBrokerContext,
    *,
    identity: str,
    method: str,
    path_and_query: str,
) -> str | None:
    method = method.upper()
    parsed = urllib.parse.urlsplit(path_and_query)
    path = parsed.path
    if (method, path) not in _IM_READ_EXACT_METHODS and not (method == "GET" and path.startswith(_IM_READ_PATH_PREFIXES)):
        return None

    profile_kind = str(context.profile_kind or "user").strip().lower()
    if profile_kind == "group":
        query = urllib.parse.parse_qs(parsed.query)
        container_id = (query.get("container_id") or [""])[0]
        if (
            identity == "bot"
            and path == "/open-apis/im/v1/messages"
            and container_id
            and container_id == str(context.current_chat_id or "").strip()
        ):
            return None
        return (
            "group profile Feishu message read is limited to the current chat; "
            "refusing global or cross-chat bot-visible IM history access"
        )

    if identity == "user" and str(context.user_open_id or "").strip():
        return None
    return _PERSONAL_FEISHU_IM_USER_AUTH_REQUIRED


def _live_owner_mapped_group(shared_home: Path, chat_id: str, user_open_id: str) -> bool:
    """Live routing re-check: is ``chat_id`` a group owned by ``user_open_id`` right now?

    ``context.allowed_bot_chat_ids`` is frozen at turn start, so a group the sender
    just created / was mapped to mid-turn isn't in it yet (a freshness race that made
    bot sends to a sender's own fresh group fail until the next turn). On the cache
    miss we re-read the routing table for this one chat_id; if it now shows the
    sender as the group owner, allow. Best-effort: any error → not-allowed (keeps the
    original deny).
    """
    chat_id = str(chat_id or "").strip()
    user_open_id = str(user_open_id or "").strip()
    if not (chat_id and user_open_id):
        return False
    table = None
    try:
        from .routing import RoutingTable

        table = RoutingTable(shared_home / "multitenancy.db")
        row = table.lookup_by_chat_id(chat_id)
    except Exception:
        return False
    finally:
        try:
            if table is not None:
                table.close()
        except Exception:
            pass
    return bool(row) and str(getattr(row, "owner_open_id", "") or "").strip() == user_open_id


def _personal_bot_identity_policy_error(
    context: LarkCliAuthBrokerContext,
    *,
    identity: str,
    method: str,
    path_and_query: str,
    body: bytes,
) -> str | None:
    profile_kind = str(context.profile_kind or "user").strip().lower()
    if profile_kind != "user" or identity != "bot":
        return None
    target_chat_id = _bot_im_message_send_chat_id(method, path_and_query, body)
    if target_chat_id and target_chat_id in context.allowed_bot_chat_ids:
        return None
    # Cache miss: re-check routing live so a sender's freshly-created own group works
    # immediately instead of failing until the next turn (allowlist freshness race).
    if target_chat_id and _live_owner_mapped_group(
        context.shared_home, target_chat_id, context.user_open_id
    ):
        return None
    if context.allowed_bot_chat_ids and _is_bot_im_image_upload(method, path_and_query):
        return None
    return (
        "personal profile bot identity is limited to owner mapped group chats; "
        "refusing unmapped or non-message bot write"
    )


def _is_bot_im_image_upload(method: str, path_and_query: str) -> bool:
    parsed = urllib.parse.urlsplit(path_and_query)
    return method.upper() == "POST" and parsed.path == "/open-apis/im/v1/images"


def _bot_im_message_send_chat_id(method: str, path_and_query: str, body: bytes) -> str:
    parsed = urllib.parse.urlsplit(path_and_query)
    if method.upper() != "POST" or parsed.path != "/open-apis/im/v1/messages":
        return ""
    query = urllib.parse.parse_qs(parsed.query)
    receive_id_type = (query.get("receive_id_type") or [""])[0]
    if receive_id_type != "chat_id":
        return ""
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("receive_id") or "").strip()


def _mint_tenant_access_token(payload: Mapping[str, object], *, timeout: float) -> str:
    app_id = str(payload.get("app_id") or payload.get("FEISHU_APP_ID") or "").strip()
    app_secret = str(payload.get("app_secret") or payload.get("FEISHU_APP_SECRET") or "").strip()
    if not (app_id and app_secret):
        raise PermissionError("app credential missing")
    domain = str(payload.get("domain") or payload.get("FEISHU_DOMAIN") or "feishu").strip().lower()
    host = "open.larksuite.com" if domain == "larksuite" else "open.feishu.cn"
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    request = urllib.request.Request(
        f"https://{host}/open-apis/auth/v3/tenant_access_token/internal",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - host is fixed above.
        raw = response.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise PermissionError("tenant token response invalid") from exc
    if int(data.get("code") or 0) != 0:
        raise PermissionError("tenant token request rejected")
    return _first_token(data, ("tenant_access_token", "app_access_token", "token"))


def _load_profile_uat_json(shared_home: Path, profile_name: str, open_id: str) -> dict | None:
    path = shared_home / "profiles" / profile_name / "feishu_uat" / f"{open_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        _first_token(data, ("access_token", "user_access_token", "token"))
    except PermissionError:
        return None
    return data


def _payload_freshness(payload: Mapping[str, object]) -> tuple[int, int]:
    def _as_int(value: object) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    return (
        _as_int(payload.get("granted_at") or payload.get("updated_at")),
        _as_int(payload.get("expires_at") or payload.get("expire_at") or payload.get("access_token_expires_at")),
    )


def _payload_is_expired(payload: Mapping[str, object]) -> bool:
    try:
        expires_at = int(
            payload.get("expires_at")
            or payload.get("expire_at")
            or payload.get("access_token_expires_at")
            or 0
        )
    except (TypeError, ValueError):
        expires_at = 0
    return bool(expires_at and expires_at <= int(time.time() * 1000))


def _refresh_profile_uat_if_needed(shared_home: Path, profile_name: str, open_id: str) -> None:
    try:
        from .feishu_uat_auth import refresh_uat_if_needed

        refresh_uat_if_needed(
            profile_name=profile_name,
            open_id=open_id,
            shared_home=shared_home,
            headroom_seconds=300,
        )
    except Exception:
        return


def _parse_target(target: str) -> tuple[str, str | None]:
    parsed = urllib.parse.urlparse(target)
    if parsed.scheme != "https":
        return "", "target scheme must be https"
    if not parsed.netloc:
        return "", "target host is required"
    if parsed.username or parsed.password:
        return "", "target userinfo is not allowed"
    if parsed.path not in ("", "/"):
        return "", "target path is not allowed"
    if parsed.query:
        return "", "target query is not allowed"
    if parsed.fragment:
        return "", "target fragment is not allowed"
    return parsed.netloc, None


def _verify_signature(
    *,
    key: bytes,
    version: str,
    method: str,
    host: str,
    path_and_query: str,
    body_sha: str,
    timestamp: str,
    identity: str,
    auth_header: str,
    signature: str,
) -> str | None:
    try:
        drift = abs(time.time() - int(timestamp))
    except ValueError:
        return "invalid timestamp"
    if drift > MAX_TIMESTAMP_DRIFT_SECONDS:
        return "timestamp drift exceeds limit"
    canonical = "\n".join(
        [
            version,
            method,
            host,
            path_and_query,
            body_sha,
            timestamp,
            identity,
            auth_header,
        ]
    )
    expected = hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return "HMAC signature mismatch"
    return None


def _case_insensitive(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _forward_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in headers.items()
        if str(key).lower() not in PROXY_HEADERS
    }


def _response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in headers.items()
        if str(key).lower() not in HOP_BY_HOP_RESPONSE_HEADERS
    }


def _urllib_forwarder(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout: float,
) -> BrokerResponse:
    request = urllib.request.Request(url=url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return BrokerResponse(
                status=int(response.status),
                headers=dict(response.headers.items()),
                body=response.read(),
            )
    except urllib.error.HTTPError as exc:
        return BrokerResponse(
            status=int(exc.code),
            headers=dict(exc.headers.items()),
            body=exc.read(),
        )
    except urllib.error.URLError:
        return _error(502, "forward request failed")


def _error(status: int, message: str) -> BrokerResponse:
    return BrokerResponse(
        status=status,
        headers={"Content-Type": "text/plain; charset=utf-8"},
        body=(message + "\n").encode("utf-8"),
    )
