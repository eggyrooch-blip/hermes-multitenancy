from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _write_fake_real_bin(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

MODE = os.environ.get("FAKE_KEP_MODE", "success")
marker = os.environ.get("FAKE_KEP_MARKER")
if marker:
    open(marker, "w").write("executed")
if MODE == "argv":
    sys.stdout.write(json.dumps(sys.argv[1:], ensure_ascii=False))
    raise SystemExit(0)
if MODE == "exit77":
    sys.stderr.write("state: not logged in\\n")
    raise SystemExit(77)
if MODE == "phrase":
    sys.stdout.write("run kep-auth login\\n")
    raise SystemExit(3)
if MODE == "forbidden403":
    sys.stderr.write("Error: 认证失败: 接口禁止访问 (HTTP 403)\\n")
    raise SystemExit(3)
sys.stdout.buffer.write(b"payload\\x00ok\\n")
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _write_fake_auth_bin(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nprintf 'header.payload.signature\\n'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@contextmanager
def _identity_server(
    body: dict,
    *,
    status: int = 200,
    location: str = "",
    seen_authorization: list[str | None] | None = None,
):
    encoded = json.dumps(body).encode()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if seen_authorization is not None:
                seen_authorization.append(self.headers.get("Authorization"))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
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


@contextmanager
def _verified_shim(tmp_path: Path, *, name: str, real_bin: Path, profile: str):
    from hermes_multitenancy.kep_cli_guard import install_kep_cli_shim

    auth_bin = _write_fake_auth_bin(tmp_path / "real-bin" / "kep-auth")
    body = {
        "errorCode": 0,
        "ok": True,
        "data": {"payload": {"name": profile, "exp": int(time.time()) + 3600}},
    }
    with _identity_server(body) as url:
        [wrapper] = install_kep_cli_shim(
            tmp_path / "shim",
            real_bins={name: str(real_bin)},
            expected_profile=profile,
            identity_urls={"online": url, "pre": url},
        )
        env = os.environ.copy()
        env.update({
            "KEP_PROFILE": profile,
            "HERMES_KEP_CLI_REAL_BIN_KEP_AUTH": str(auth_bin),
        })
        yield wrapper, env


def test_install_kep_cli_shim_blocks_server_rejected_token_before_real_binary(tmp_path: Path):
    from hermes_multitenancy.kep_cli_guard import install_kep_cli_shim

    real_bin = _write_fake_real_bin(tmp_path / "real-bin" / "ocean-cli")
    auth_bin = _write_fake_auth_bin(tmp_path / "real-bin" / "kep-auth")
    marker = tmp_path / "business-executed"
    with _identity_server({"errorCode": 400, "ok": False, "data": None}) as url:
        [wrapper] = install_kep_cli_shim(
            tmp_path / "shim",
            real_bins={"ocean-cli": str(real_bin)},
            expected_profile="alice",
            identity_urls={"online": url, "pre": url},
        )
        env = os.environ.copy()
        env.update({
            "KEP_PROFILE": "alice",
            "HERMES_KEP_CLI_REAL_BIN_KEP_AUTH": str(auth_bin),
            "FAKE_KEP_MARKER": str(marker),
        })
        result = subprocess.run(
            [str(wrapper), "status"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        positional_help = subprocess.run(
            [str(wrapper), "create", "help"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    assert result.returncode == 77
    assert positional_help.returncode == 77
    assert not marker.exists()
    assert "需要授权" in result.stderr
    assert "header.payload.signature" not in result.stderr


def test_install_kep_cli_shim_blocks_when_live_identity_is_unavailable(tmp_path: Path):
    from hermes_multitenancy.kep_cli_guard import install_kep_cli_shim

    real_bin = _write_fake_real_bin(tmp_path / "real-bin" / "ocean-cli")
    auth_bin = _write_fake_auth_bin(tmp_path / "real-bin" / "kep-auth")
    marker = tmp_path / "business-executed"
    closed_server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    url = f"http://127.0.0.1:{closed_server.server_port}/ldap/authjwt"
    closed_server.server_close()
    [wrapper] = install_kep_cli_shim(
        tmp_path / "shim",
        real_bins={"ocean-cli": str(real_bin)},
        expected_profile="alice",
        identity_urls={"online": url, "pre": url},
    )
    env = os.environ.copy()
    env.update({
        "KEP_PROFILE": "alice",
        "HERMES_KEP_CLI_REAL_BIN_KEP_AUTH": str(auth_bin),
        "FAKE_KEP_MARKER": str(marker),
    })
    result = subprocess.run(
        [str(wrapper), "status"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 75
    assert not marker.exists()
    assert "暂时无法验证身份" in result.stderr


def test_install_kep_cli_shim_treats_rate_limit_as_unavailable(tmp_path: Path):
    from hermes_multitenancy.kep_cli_guard import install_kep_cli_shim

    real_bin = _write_fake_real_bin(tmp_path / "real-bin" / "ocean-cli")
    auth_bin = _write_fake_auth_bin(tmp_path / "real-bin" / "kep-auth")
    marker = tmp_path / "business-executed"
    with _identity_server({}, status=429) as url:
        [wrapper] = install_kep_cli_shim(
            tmp_path / "shim",
            real_bins={"ocean-cli": str(real_bin)},
            expected_profile="alice",
            identity_urls={"online": url, "pre": url},
        )
        env = os.environ.copy()
        env.update({
            "KEP_PROFILE": "alice",
            "HERMES_KEP_CLI_REAL_BIN_KEP_AUTH": str(auth_bin),
            "FAKE_KEP_MARKER": str(marker),
        })
        result = subprocess.run(
            [str(wrapper), "status"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    assert result.returncode == 75
    assert not marker.exists()
    assert "暂时无法验证身份" in result.stderr
    assert "需要授权" not in result.stderr


def test_install_kep_cli_shim_refuses_redirect_without_forwarding_bearer(tmp_path: Path):
    from hermes_multitenancy.kep_cli_guard import install_kep_cli_shim

    real_bin = _write_fake_real_bin(tmp_path / "real-bin" / "ocean-cli")
    auth_bin = _write_fake_auth_bin(tmp_path / "real-bin" / "kep-auth")
    marker = tmp_path / "business-executed"
    sink_authorization: list[str | None] = []
    success = {
        "errorCode": 0,
        "ok": True,
        "data": {"payload": {"name": "alice", "exp": int(time.time()) + 3600}},
    }
    with _identity_server(success, seen_authorization=sink_authorization) as sink_url:
        with _identity_server({}, status=302, location=sink_url) as redirect_url:
            [wrapper] = install_kep_cli_shim(
                tmp_path / "shim",
                real_bins={"ocean-cli": str(real_bin)},
                expected_profile="alice",
                identity_urls={"online": redirect_url, "pre": redirect_url},
            )
            env = os.environ.copy()
            env.update({
                "KEP_PROFILE": "alice",
                "HERMES_KEP_CLI_REAL_BIN_KEP_AUTH": str(auth_bin),
                "FAKE_KEP_MARKER": str(marker),
            })
            result = subprocess.run(
                [str(wrapper), "status"],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

    assert result.returncode == 75
    assert not marker.exists()
    assert sink_authorization == []
    assert "暂时无法验证身份" in result.stderr


def test_install_kep_cli_shim_inserts_profile_before_subcommand(tmp_path: Path):
    real_bin = _write_fake_real_bin(tmp_path / "real-bin" / "ocean-cli")
    with _verified_shim(tmp_path, name="ocean-cli", real_bin=real_bin, profile="alice") as (wrapper, env):
        env["FAKE_KEP_MODE"] = "argv"
        result = subprocess.run(
            [str(wrapper), "status", "--env", "pre"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    assert result.returncode == 0
    assert json.loads(result.stdout) == ["--profile", "alice", "status", "--env", "pre"]
    assert "【Hermes】" not in result.stderr


def test_install_kep_cli_shim_preserves_explicit_profile(tmp_path: Path):
    real_bin = _write_fake_real_bin(tmp_path / "real-bin" / "hades-cli")
    with _verified_shim(tmp_path, name="hades-cli", real_bin=real_bin, profile="manual") as (wrapper, env):
        env["FAKE_KEP_MODE"] = "argv"
        result = subprocess.run(
            [str(wrapper), "--profile", "manual", "status"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    assert result.returncode == 0
    assert json.loads(result.stdout) == ["--profile", "manual", "status"]


def test_install_kep_cli_shim_blocks_profile_override_before_real_binary(tmp_path: Path):
    real_bin = _write_fake_real_bin(tmp_path / "real-bin" / "hades-cli")
    marker = tmp_path / "business-executed"
    audit = tmp_path / "security.jsonl"
    with _verified_shim(tmp_path, name="hades-cli", real_bin=real_bin, profile="alice") as (wrapper, env):
        env["FAKE_KEP_MARKER"] = str(marker)
        env["HERMES_PROFILE"] = "employee-sensitive-profile"
        env["HERMES_MT_SECURITY_AUDIT_PATH"] = str(audit)
        result = subprocess.run(
            [str(wrapper), "--profile", "another-person", "status"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    assert result.returncode == 75
    assert not marker.exists()
    assert "身份校验不匹配" in result.stderr
    assert "another-person" not in result.stderr
    assert "alice" not in result.stderr
    event = json.loads(audit.read_text().splitlines()[-1])
    assert event["reason"] == "identity_mismatch"
    assert event["profile_fingerprint"]
    assert "profile" not in event


def test_install_kep_cli_shim_blocks_conflicting_repeated_scope_flags(tmp_path: Path):
    real_bin = _write_fake_real_bin(tmp_path / "real-bin" / "hades-cli")
    marker = tmp_path / "business-executed"
    with _verified_shim(tmp_path, name="hades-cli", real_bin=real_bin, profile="alice") as (wrapper, env):
        env["FAKE_KEP_MARKER"] = str(marker)
        profile_result = subprocess.run(
            [str(wrapper), "--profile", "alice", "--profile", "bob", "status"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        env_result = subprocess.run(
            [str(wrapper), "--env", "online", "--env", "pre", "status"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    assert profile_result.returncode == env_result.returncode == 75
    assert not marker.exists()


def test_install_kep_cli_shim_does_not_trust_overridden_profile_env(tmp_path: Path):
    from hermes_multitenancy.kep_cli_guard import install_kep_cli_shim

    real_bin = _write_fake_real_bin(tmp_path / "real-bin" / "hades-cli")
    auth_bin = _write_fake_auth_bin(tmp_path / "real-bin" / "kep-auth")
    marker = tmp_path / "business-executed"
    body = {
        "errorCode": 0,
        "ok": True,
        "data": {"payload": {"name": "another-person", "exp": int(time.time()) + 3600}},
    }
    with _identity_server(body) as url:
        [wrapper] = install_kep_cli_shim(
            tmp_path / "shim",
            real_bins={"hades-cli": str(real_bin)},
            expected_profile="alice",
            identity_urls={"online": url, "pre": url},
        )
        env = os.environ.copy()
        env.update({
            "KEP_PROFILE": "another-person",
            "HERMES_KEP_CLI_REAL_BIN_KEP_AUTH": str(auth_bin),
            "FAKE_KEP_MARKER": str(marker),
        })
        result = subprocess.run(
            [str(wrapper), "--profile", "another-person", "status"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    assert result.returncode == 75
    assert not marker.exists()
    assert "身份校验不匹配" in result.stderr
    assert "another-person" not in result.stderr
    assert "alice" not in result.stderr


def test_install_kep_cli_shim_relays_auth_failure_with_stable_exit_code(tmp_path: Path):
    from hermes_multitenancy.kep_cli_guard import install_kep_cli_shim

    real_bin = _write_fake_real_bin(tmp_path / "real-bin" / "kep-auth")
    shim_dir = tmp_path / "shim"
    [wrapper] = install_kep_cli_shim(
        shim_dir,
        real_bins={"kep-auth": str(real_bin)},
        expected_profile="alice",
    )

    env = os.environ.copy()
    env["FAKE_KEP_MODE"] = "exit77"
    result = subprocess.run(
        [str(wrapper), "--env", "pre", "status"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 77
    assert "【Hermes】kep-cli pre 需要授权" in result.stderr


def test_install_kep_cli_shim_blocks_headless_login_before_real_binary(tmp_path: Path):
    from hermes_multitenancy.kep_cli_guard import install_kep_cli_shim

    marker = tmp_path / "spawned"
    real_bin = tmp_path / "real-bin" / "kep-auth"
    real_bin.parent.mkdir(parents=True)
    real_bin.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    real_bin.chmod(0o755)
    [wrapper] = install_kep_cli_shim(
        tmp_path / "shim",
        real_bins={"kep-auth": str(real_bin)},
        expected_profile="alice",
    )

    result = subprocess.run(
        [str(wrapper), "login"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 77
    assert not marker.exists()
    assert "Connectors authorization link" in result.stderr


def test_install_kep_cli_shim_detects_auth_failure_from_output(tmp_path: Path):
    real_bin = _write_fake_real_bin(tmp_path / "real-bin" / "kep-dune-cli")
    with _verified_shim(tmp_path, name="kep-dune-cli", real_bin=real_bin, profile="alice") as (wrapper, env):
        env["FAKE_KEP_MODE"] = "phrase"
        result = subprocess.run(
            [str(wrapper), "status"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    assert result.returncode == 3
    assert "【Hermes】kep-cli online 需要授权" in result.stderr


def test_install_kep_cli_shim_relays_forbidden_as_permission_denied(tmp_path: Path):
    real_bin = _write_fake_real_bin(tmp_path / "real-bin" / "ocean-cli")
    with _verified_shim(tmp_path, name="ocean-cli", real_bin=real_bin, profile="alice") as (wrapper, env):
        env["FAKE_KEP_MODE"] = "forbidden403"
        result = subprocess.run(
            [str(wrapper), "--env", "pre", "jd-adjust", "jd-adjust-list"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    assert result.returncode == 3
    assert "接口禁止访问 (HTTP 403)" in result.stderr
    assert "【Hermes】kep-cli pre 接口禁止访问" in result.stderr
    assert "当前账号已登录但没有该接口/数据权限" in result.stderr
    assert "需要授权" not in result.stderr


def test_install_kep_cli_shim_success_stdout_matches_real_binary(tmp_path: Path):
    real_bin = _write_fake_real_bin(tmp_path / "real-bin" / "kep-badge-cli")
    direct = subprocess.run(
        [str(real_bin), "fetch", "--id", "42"],
        capture_output=True,
        check=False,
    )
    with _verified_shim(tmp_path, name="kep-badge-cli", real_bin=real_bin, profile="alice") as (wrapper, env):
        wrapped = subprocess.run(
            [str(wrapper), "fetch", "--id", "42"],
            capture_output=True,
            env=env,
            check=False,
        )

    assert wrapped.returncode == direct.returncode == 0
    assert wrapped.stdout == direct.stdout
    assert wrapped.stderr == direct.stderr
    assert "【Hermes】".encode("utf-8") not in wrapped.stderr


def test_install_kep_cli_shim_blocks_recursive_real_binary(tmp_path: Path):
    from hermes_multitenancy.kep_cli_guard import install_kep_cli_shim

    shim_dir = tmp_path / "shim"
    [wrapper] = install_kep_cli_shim(
        shim_dir,
        real_bins={"kep-auth": str(shim_dir / "kep-auth")},
        expected_profile="alice",
    )

    result = subprocess.run(
        [str(wrapper), "status"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 127
    assert "shim" in result.stderr.lower()
    assert "real" in result.stderr.lower()
