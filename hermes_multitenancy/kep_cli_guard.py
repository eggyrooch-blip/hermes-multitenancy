from __future__ import annotations

import textwrap
from pathlib import Path

from .security_audit import DEFAULT_AUDIT_PATH

KEP_SHIM_NAMES: tuple[str, ...] = (
    "ocean-cli",
    "hades-cli",
    "kep-asgard-cli",
    "kep-badge-cli",
    "kep-circinus-cli",
    "kep-club-cli",
    "kep-dune-cli",
    "kep-halo-cli",
    "kep-hades-cli",
    "kep-phantasia-cli",
    "kep-trevi-cli",
    "kep-auth",
)


def _real_bin_env_key(name: str) -> str:
    return f"HERMES_KEP_CLI_REAL_BIN_{name.replace('-', '_').upper()}"


def kep_cli_real_bin_env_keys() -> tuple[str, ...]:
    return tuple(_real_bin_env_key(name) for name in KEP_SHIM_NAMES)


def _shim_program(command_name: str, real_binary: Path) -> str:
    env_key = _real_bin_env_key(command_name)
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        from __future__ import annotations

        import hashlib
        import json
        import os
        import re
        import subprocess
        import sys
        import threading
        from datetime import datetime, timedelta, timezone
        from pathlib import Path

        _EMBEDDED_ID_RE = re.compile(r"(?<![A-Za-z0-9])(ou|oc)_[A-Za-z0-9_-]+")

        def _redact_embedded_ids(value):
            return _EMBEDDED_ID_RE.sub(
                lambda m: m.group(1) + "_" + hashlib.sha256(m.group(0).encode("utf-8")).hexdigest()[:12],
                value,
            )

        COMMAND_NAME = {command_name!r}
        DEFAULT_REAL_BINARY = {str(real_binary)!r}
        REAL_BINARY_ENV_KEY = {env_key!r}
        DEFAULT_ENV_NAME = {"online"!r}
        DEFAULT_AUDIT_PATH = {str(DEFAULT_AUDIT_PATH)!r}
        _SHANGHAI_TZ = timezone(timedelta(hours=8))
        _TAIL_LIMIT = 8192
        _AUTH_FAILURE_MARKERS = (
            b"not logged in",
            b"auth: not logged in",
            b"run kep-auth login",
        )
        _PERMISSION_DENIED_MARKERS = (
            "http 403",
            "forbidden",
            "接口禁止访问",
        )

        def _timestamp_iso() -> str:
            return datetime.now(tz=_SHANGHAI_TZ).isoformat(timespec="seconds")

        def _append_security_event(*, event_type: str, command_name: str, reason: str) -> None:
            event = {{
                "@timestamp": _timestamp_iso(),
                "event_type": event_type,
            }}
            profile = str(os.environ.get("HERMES_PROFILE") or "").strip()
            if profile:
                event["profile"] = _redact_embedded_ids(profile)
            if command_name:
                event["command_name"] = command_name
            if reason:
                event["reason"] = reason
            open_id = str(os.environ.get("HERMES_FEISHU_USER_OPEN_ID") or "").strip()
            if open_id:
                event["open_id_hash"] = hashlib.sha256(open_id.encode("utf-8")).hexdigest()[:12]
            try:
                path = Path(
                    str(os.environ.get("HERMES_MT_SECURITY_AUDIT_PATH") or DEFAULT_AUDIT_PATH).strip()
                    or DEFAULT_AUDIT_PATH
                ).expanduser()
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                    fh.write("\\n")
            except Exception:
                pass

        def _has_explicit_profile(argv) -> bool:
            return "--profile" in argv or any(arg.startswith("--profile=") for arg in argv)

        def _parse_env_name(argv) -> str:
            for idx, arg in enumerate(argv):
                if arg == "--env" and idx + 1 < len(argv):
                    value = str(argv[idx + 1] or "").strip().lower()
                    return value or DEFAULT_ENV_NAME
                if arg.startswith("--env="):
                    value = arg.split("=", 1)[1].strip().lower()
                    return value or DEFAULT_ENV_NAME
            return DEFAULT_ENV_NAME

        def _append_tail(buf, chunk):
            if not chunk:
                return
            buf.extend(chunk)
            if len(buf) > _TAIL_LIMIT:
                del buf[:-_TAIL_LIMIT]

        def _pipe_stream(src, dst, buf) -> None:
            try:
                while True:
                    chunk = src.read(4096)
                    if not chunk:
                        break
                    dst.write(chunk)
                    dst.flush()
                    _append_tail(buf, chunk)
            finally:
                try:
                    src.close()
                except Exception:
                    pass

        def main() -> int:
            raw_real_binary = str(os.environ.get(REAL_BINARY_ENV_KEY) or DEFAULT_REAL_BINARY).strip()
            real_binary = os.path.realpath(raw_real_binary)
            shim_dir = os.path.dirname(os.path.realpath(sys.argv[0]))
            if os.path.dirname(real_binary) == shim_dir:
                print(
                    f"Refusing recursive kep-cli shim execution: shim={{sys.argv[0]!r}} real={{raw_real_binary!r}}",
                    file=sys.stderr,
                )
                return 127

            argv = list(sys.argv[1:])
            kep_profile = str(os.environ.get("KEP_PROFILE") or "").strip()
            if kep_profile and not _has_explicit_profile(argv):
                argv = ["--profile", kep_profile, *argv]

            proc = subprocess.Popen(
                [real_binary, *argv],
                stdin=None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout_tail = bytearray()
            stderr_tail = bytearray()
            stdout_thread = threading.Thread(
                target=_pipe_stream,
                args=(proc.stdout, sys.stdout.buffer, stdout_tail),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=_pipe_stream,
                args=(proc.stderr, sys.stderr.buffer, stderr_tail),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            returncode = proc.wait()
            stdout_thread.join()
            stderr_thread.join()

            combined_tail = bytes(stdout_tail + stderr_tail).lower()
            combined_text = combined_tail.decode("utf-8", errors="ignore")
            auth_failure = returncode == 77 or any(marker in combined_tail for marker in _AUTH_FAILURE_MARKERS)
            permission_denied = returncode != 0 and any(
                marker in combined_text for marker in _PERMISSION_DENIED_MARKERS
            )
            if auth_failure:
                env_name = _parse_env_name(argv)
                message = (
                    f'\\n【Hermes】kep-cli {{env_name}} 需要授权：请在 Hermes 连接器面板认证 "kep-cli {{env_name}}"'
                    "（或 /auth），勿在此自行登录或去掉 --profile。"
                )
                try:
                    sys.stderr.buffer.write(message.encode("utf-8"))
                    sys.stderr.buffer.flush()
                except Exception:
                    pass
                try:
                    _append_security_event(
                        event_type="kep_cli.auth_failure.relayed",
                        command_name=COMMAND_NAME,
                        reason="auth failure relayed to Hermes connector auth flow",
                    )
                except Exception:
                    pass
            elif permission_denied:
                env_name = _parse_env_name(argv)
                message = (
                    f"\\n【Hermes】kep-cli {{env_name}} 接口禁止访问：当前账号已登录但没有该接口/数据权限；"
                    "请直接告知用户无权限，不要要求重新登录或重新授权。"
                )
                try:
                    sys.stderr.buffer.write(message.encode("utf-8"))
                    sys.stderr.buffer.flush()
                except Exception:
                    pass
                try:
                    _append_security_event(
                        event_type="kep_cli.permission_denied.relayed",
                        command_name=COMMAND_NAME,
                        reason="permission denied relayed as non-login failure",
                    )
                except Exception:
                    pass
            return returncode

        if __name__ == "__main__":
            raise SystemExit(main())
        """
    )


def install_kep_cli_shim(shim_dir: Path, *, real_bins: dict[str, str]) -> list[Path]:
    shim_dir = Path(shim_dir)
    shim_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, real_path in real_bins.items():
        wrapper = shim_dir / name
        wrapper.write_text(_shim_program(name, Path(real_path).expanduser()), encoding="utf-8")
        wrapper.chmod(0o755)
        written.append(wrapper)
    return written
