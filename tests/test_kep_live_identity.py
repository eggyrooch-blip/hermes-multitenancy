from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@contextmanager
def _server(*, status: int, body: dict, location: str = "", seen=None):
    encoded = json.dumps(body).encode()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if seen is not None:
                seen.append(self.headers.get("Authorization"))
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/ldap/authjwt"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_probe_refuses_redirect_without_forwarding_bearer():
    from hermes_multitenancy.kep_live_identity import probe_kep_identity

    seen = []
    success = {
        "errorCode": 0,
        "ok": True,
        "data": {"payload": {"name": "owner", "exp": int(time.time()) + 3600}},
    }
    with _server(status=200, body=success, seen=seen) as sink_url:
        with _server(status=302, body={}, location=sink_url) as source_url:
            result = probe_kep_identity(
                "header.payload.signature",
                profile_name="owner",
                env_name="online",
                identity_urls={"online": source_url, "pre": source_url},
            )

    assert result["state"] == "unknown"
    assert seen == []


def test_probe_never_authenticates_an_empty_profile_name():
    from hermes_multitenancy.kep_live_identity import probe_kep_identity

    success = {
        "errorCode": 0,
        "ok": True,
        "data": {"payload": {"name": "", "exp": int(time.time()) + 3600}},
    }
    with _server(status=200, body=success) as url:
        result = probe_kep_identity(
            "header.payload.signature",
            profile_name="",
            env_name="online",
            identity_urls={"online": url, "pre": url},
        )

    assert result["state"] == "unknown"
