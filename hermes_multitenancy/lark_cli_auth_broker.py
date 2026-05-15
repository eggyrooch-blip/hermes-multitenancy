"""Credential-vault backed auth broker for lark-cli authsidecar requests."""
from __future__ import annotations

import hashlib
import http.server
import hmac
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


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

        try:
            token = self._resolve_token(identity)
        except PermissionError:
            return _error(503, "credential unavailable")
        except Exception:
            return _error(503, "credential lookup failed")

        forward_headers = _forward_headers(headers)
        forward_headers.pop("Authorization", None)
        forward_headers.pop("X-Lark-MCP-UAT", None)
        forward_headers.pop("X-Lark-MCP-TAT", None)
        if auth_header == "Authorization":
            forward_headers["Authorization"] = "Bearer " + token
        else:
            forward_headers[auth_header] = token

        return self.forwarder(
            method,
            "https://" + host + path_and_query,
            forward_headers,
            body,
            self.context.request_timeout_seconds,
        )

    def _resolve_token(self, identity: str) -> str:
        from .credentials import CredentialStore

        store = CredentialStore(self.context.shared_home / "multitenancy.db")
        try:
            if identity == "user":
                payload = store.get_secret_for_runtime(
                    profile_name=self.context.profile_name,
                    subject_id=self.context.user_open_id,
                    provider="feishu",
                    secret_kind="uat",
                )
                return _first_token(payload, ("access_token", "user_access_token", "token"))
            payload = store.get_secret_for_runtime(
                profile_name="__global__",
                subject_id="feishu_app",
                provider="feishu",
                secret_kind="app",
            )
            return _first_token(payload, ("tenant_access_token", "app_access_token", "token"))
        finally:
            store.close()


class RunningLarkCliAuthBrokerServer:
    def __init__(self, server: http.server.ThreadingHTTPServer, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread
        host, port = server.server_address[:2]
        self.url = f"http://{host}:{port}"

    def close(self) -> None:
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
