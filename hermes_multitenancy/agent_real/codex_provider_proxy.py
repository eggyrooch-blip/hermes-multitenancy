"""Run-scoped loopback proxy that keeps the employee LiteLLM key out of Codex."""
from __future__ import annotations

import hmac
import http.client
import http.server
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import threading
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit


_MAX_BODY = 16 * 1024 * 1024
_READ_SIZE = 64 * 1024
_BODY_READ_TIMEOUT_S = 5
MAX_HARNESS_REQUESTS = 64
# ponytail: token 总量护栏关闭（sunke 2026-09-04）——钱的护栏在 LiteLLM 员工 key
# 预算，循环的护栏是 MAX_HARNESS_REQUESTS；1_500_000 会误伤正常重活（单轮 24 万，
# 后半段每次重发 8-10 万上下文且 92% 是缓存输入）。置回一个 int 即重新开启。
MAX_HARNESS_TOTAL_TOKENS: int | None = None
PROXY_BASE_URL_ENV = "CODEX_PROXY_BASE_URL"
_ROUTE_PATH = re.compile(r"/[A-Za-z0-9_-]{16,}/v1")


def _usage_int(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _usage_tokens(usage: Any) -> Optional[tuple[int, int, int]]:
    """(input, output, total) for a SCHEMA-VALID usage object, else ``None``.

    ``{}``, a string counter, a negative one — anything ``_usage_int`` would
    silently flatten to 0 — must NOT count as usage present. The receipt gate
    passes on ``usage_response_count == request_count``, so counting malformed
    usage would let a whole run through with a zero-token ledger on upstream
    schema drift. Not counting it makes the gate fail closed instead.
    """
    if not isinstance(usage, dict):
        return None
    present = 0
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        if name not in usage:
            continue
        value = usage[name]
        if type(value) is not int or value < 0:
            return None  # one bad counter makes the whole usage object untrusted
        present += 1
    if not present:
        return None  # `{}` is not usage
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    total = usage.get("total_tokens")
    return (
        input_tokens,
        output_tokens,
        total if type(total) is int else input_tokens + output_tokens,
    )


class _Audit:
    def __init__(self, request_limit: int) -> None:
        self._buffer = b""
        self._response_completed = 0
        self._usage_response_count = 0
        self._tool_events = 0
        self._usage_present = False
        self._request_count = 0
        self._rejected_over_limit = 0
        self._rejected_concurrent = 0
        self._request_active = False
        self._request_limit = request_limit
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_tokens = 0
        self._cache_read_tokens = 0
        self._store_forced = False
        self._lock = threading.Lock()

    def begin_request(self) -> str:
        """Admit a request (return "") or return the 409 error type rejecting it.

        Concurrency rejections stay separate from budget rejections: only the
        budget ones are a normal end-of-run condition the receipt gate may pass
        through (prod incident 2026-09-03 — a spent budget threw away the whole
        turn's output as "audit incomplete").
        """
        with self._lock:
            if self._request_active:
                self._rejected_concurrent += 1
                return "request_limit_exceeded"
            if self._request_count >= self._request_limit:
                self._rejected_over_limit += 1
                return "request_limit_exceeded"
            if (
                MAX_HARNESS_TOTAL_TOKENS is not None
                and self._total_tokens >= MAX_HARNESS_TOTAL_TOKENS
            ):
                self._rejected_over_limit += 1
                return "token_budget_exceeded"
            self._request_count += 1
            self._request_active = True
            self._buffer = b""
            return ""

    def end_request(self) -> None:
        with self._lock:
            self._request_active = False

    def feed(self, chunk: bytes) -> None:
        """Observe SSE metadata only. Parsing failure must never break the stream."""
        try:
            with self._lock:
                self._buffer = (self._buffer + chunk)[-_MAX_BODY:].replace(
                    b"\r\n", b"\n"
                )
                while b"\n\n" in self._buffer:
                    event, self._buffer = self._buffer.split(b"\n\n", 1)
                    data = b"\n".join(
                        line[5:].lstrip(b" ")
                        for line in event.splitlines()
                        if line.startswith(b"data:")
                    )
                    if not data:
                        continue
                    payload = json.loads(data)
                    kind = str(payload.get("type") or "") if isinstance(payload, dict) else ""
                    if kind == "response.completed":
                        self._response_completed += 1
                        response = payload.get("response") or {}
                        usage = response.get("usage") if isinstance(response, dict) else None
                        tokens = _usage_tokens(usage)
                        if tokens is not None:
                            self._usage_present = True
                            self._usage_response_count += 1
                            input_tokens, output_tokens, total_tokens = tokens
                            self._input_tokens += input_tokens
                            self._output_tokens += output_tokens
                            self._total_tokens += total_tokens
                            details = usage.get("input_tokens_details")
                            if isinstance(details, dict):
                                self._cache_read_tokens += _usage_int(
                                    details.get("cached_tokens")
                                )
                    elif kind == "response.output_item.added":
                        item = payload.get("item") or {}
                        if str(item.get("type") or "") in {
                            "function_call",
                            "local_shell_call",
                            "mcp_call",
                        }:
                            self._tool_events += 1
        except Exception:
            return

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "request_count": self._request_count,
                "request_limit": self._request_limit,
                "rejected_requests": self._rejected_over_limit
                + self._rejected_concurrent,
                "rejected_over_limit": self._rejected_over_limit,
                "rejected_concurrent": self._rejected_concurrent,
                "budget_exhausted": self._rejected_over_limit > 0,
                "response_completed": self._response_completed,
                "usage_response_count": self._usage_response_count,
                "tool_events": self._tool_events,
                "usage_present": self._usage_present,
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "total_tokens": self._total_tokens,
                "cache_read_tokens": self._cache_read_tokens,
                "store_forced": self._store_forced,
            }

    def mark_store_forced(self) -> None:
        with self._lock:
            self._store_forced = True


def _safe_upstream(raw: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(str(raw or "").strip())
    except ValueError as exc:
        raise ValueError("codex proxy upstream URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("codex proxy upstream URL must be credential-free HTTPS")
    base_path = parsed.path.rstrip("/")
    if not base_path.endswith("/v1"):
        base_path += "/v1"
    authority = parsed.hostname
    if parsed.port:
        authority += f":{parsed.port}"
    return f"https://{authority}", f"{base_path}/responses"


def _open_upstream(origin: str):
    parsed = urlsplit(origin)
    return http.client.HTTPSConnection(
        parsed.hostname,
        parsed.port or 443,
        timeout=120,
        context=ssl.create_default_context(),
    )


def _is_loopback(peer: Any) -> bool:
    try:
        return ipaddress.ip_address(str(peer)).is_loopback
    except ValueError:
        return False


def runtime_from_environment(metadata: dict[str, Any]) -> dict[str, str]:
    """Resolve only the disposable loopback credential inside the AIAgent child."""
    token = os.environ.get("CODEX_RUNTIME_KEY", "").strip()
    base_url = os.environ.get(PROXY_BASE_URL_ENV, "").strip()
    employee_id = str(metadata.get("litellm_billing_employee_user_id") or "").strip()
    try:
        parsed = urlsplit(base_url)
    except ValueError:
        parsed = urlsplit("")
    if (
        not token.startswith("hcx_")
        or len(token) > 256
        or parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not parsed.port
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not _ROUTE_PATH.fullmatch(parsed.path)
        or not employee_id
    ):
        raise RuntimeError("Codex proxy runtime credential is incomplete")
    return {"api_key": token, "base_url": base_url, "employee_id": employee_id}


@dataclass(slots=True)
class Proxy:
    base_url: str
    token: str
    audit: _Audit
    _server: http.server.ThreadingHTTPServer
    _thread: threading.Thread
    _revoked: threading.Event
    _active_sockets: set[Any]
    _active_upstreams: set[Any]
    _active_lock: threading.Lock
    _credential_lock: threading.Lock
    _key_holder: list[str]
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._revoked.set()
        with self._active_lock:
            active = tuple(self._active_sockets)
            upstreams = tuple(self._active_upstreams)
        for client in active:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass
        for connection in upstreams:
            try:
                connection.close()
            except OSError:
                pass
        with self._credential_lock:
            self._key_holder[0] = ""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


def start(
    *, upstream_base_url: str, upstream_api_key: str, max_requests: int = 1
) -> Proxy:
    origin, upstream_path = _safe_upstream(upstream_base_url)
    real_key = str(upstream_api_key or "")
    if not real_key or len(real_key) > 4096:
        raise ValueError("codex proxy upstream API key is invalid")
    if type(max_requests) is not int or not 1 <= max_requests <= MAX_HARNESS_REQUESTS:
        raise ValueError("codex proxy request limit is invalid")
    token = f"hcx_{secrets.token_urlsafe(32)}"
    route = secrets.token_urlsafe(24)
    audit = _Audit(max_requests)
    expected_path = f"/{route}/v1/responses"
    revoked = threading.Event()
    active_sockets: set[Any] = set()
    active_upstreams: set[Any] = set()
    active_lock = threading.Lock()
    credential_lock = threading.Lock()
    key_holder = [real_key]

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def setup(self) -> None:
            super().setup()
            self._audit_request_started = False
            with active_lock:
                active_sockets.add(self.request)

        def finish(self) -> None:
            self._end_audit_request()
            with active_lock:
                active_sockets.discard(self.request)
            try:
                super().finish()
            except OSError:
                pass

        def _end_audit_request(self) -> None:
            if self._audit_request_started:
                self._audit_request_started = False
                audit.end_request()

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _error(self, status: int, code: str) -> None:
            body = json.dumps({"error": {"type": code}}).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            if revoked.is_set():
                self._error(410, "run_revoked")
                return
            if not _is_loopback(self.client_address[0]):
                self._error(403, "non_loopback_denied")
                return
            if self.path != expected_path:
                self._error(404, "not_found")
                return
            authorization = self.headers.get("Authorization", "")
            if not hmac.compare_digest(authorization, f"Bearer {token}"):
                self._error(401, "authentication_error")
                return
            if self.headers.get("Transfer-Encoding"):
                self._error(400, "invalid_body")
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if length < 2 or length > _MAX_BODY or content_type != "application/json":
                self._error(400, "invalid_body")
                return
            rejection = audit.begin_request()
            if rejection:
                self._error(409, rejection)
                return
            self._audit_request_started = True
            self.connection.settimeout(_BODY_READ_TIMEOUT_S)
            try:
                body = self.rfile.read(length)
            except OSError:
                self._error(408, "body_timeout")
                return
            if len(body) != length:
                self._error(400, "invalid_body")
                return
            if revoked.is_set():
                self._error(410, "run_revoked")
                return
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if not isinstance(payload, dict) or payload.get("stream") is not True:
                self._error(400, "invalid_body")
                return
            items = payload.get("input")
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and item.get("type") == "message":
                        item.pop("id", None)
            payload["store"] = False
            audit.mark_store_forced()
            body = json.dumps(payload, separators=(",", ":")).encode()

            connection = _open_upstream(origin)
            with active_lock:
                active_upstreams.add(connection)
            response_started = False
            try:
                with credential_lock:
                    if revoked.is_set() or not key_holder[0]:
                        self._error(410, "run_revoked")
                        return
                    connection.request(
                        "POST",
                        upstream_path,
                        body=body,
                        headers={
                            "Authorization": f"Bearer {key_holder[0]}",
                            "Content-Type": "application/json",
                            "Accept": "text/event-stream, application/json",
                        },
                    )
                response = connection.getresponse()
                if 300 <= response.status < 400:
                    self._error(502, "upstream_redirect_denied")
                    return
                self.send_response(response.status, response.reason)
                response_type = response.headers.get("Content-Type")
                if response_type:
                    self.send_header("Content-Type", response_type)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                response_started = True
                while True:
                    chunk = response.read(_READ_SIZE)
                    if not chunk:
                        break
                    audit.feed(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (OSError, http.client.HTTPException, ssl.SSLError):
                if not response_started:
                    self._error(502, "upstream_unavailable")
                elif not self.wfile.closed:
                    self.close_connection = True
            finally:
                with active_lock:
                    active_upstreams.discard(connection)
                self._end_audit_request()
                connection.close()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.05),
        daemon=True,
    )
    thread.start()
    return Proxy(
        base_url=f"http://127.0.0.1:{server.server_port}/{route}/v1",
        token=token,
        audit=audit,
        _server=server,
        _thread=thread,
        _revoked=revoked,
        _active_sockets=active_sockets,
        _active_upstreams=active_upstreams,
        _active_lock=active_lock,
        _credential_lock=credential_lock,
        _key_holder=key_holder,
    )
