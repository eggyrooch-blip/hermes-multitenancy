from __future__ import annotations

import inspect
import textwrap
from pathlib import Path

from . import kep_live_identity
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
_KEP_IDENTITY_URLS = {
    "online": "https://auth.gotokeep.com/ldap/authjwt",
    "pre": "https://auth.pre.gotokeep.com/ldap/authjwt",
}


def _real_bin_env_key(name: str) -> str:
    return f"HERMES_KEP_CLI_REAL_BIN_{name.replace('-', '_').upper()}"


def kep_cli_real_bin_env_keys() -> tuple[str, ...]:
    return tuple(_real_bin_env_key(name) for name in KEP_SHIM_NAMES)


def _shim_program(
    command_name: str,
    real_binary: Path,
    *,
    expected_profile: str,
    identity_urls: dict[str, str],
) -> str:
    env_key = _real_bin_env_key(command_name)
    probe_source = inspect.getsource(kep_live_identity)
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

        _LIVE_IDENTITY_NAMESPACE = {{}}
        exec({probe_source!r}, _LIVE_IDENTITY_NAMESPACE, _LIVE_IDENTITY_NAMESPACE)
        probe_kep_identity = _LIVE_IDENTITY_NAMESPACE["probe_kep_identity"]

        _EMBEDDED_ID_RE = re.compile(r"(?<![A-Za-z0-9])(ou|oc)_[A-Za-z0-9_-]+")

        def _redact_embedded_ids(value):
            return _EMBEDDED_ID_RE.sub(
                lambda m: m.group(1) + "_" + hashlib.sha256(m.group(0).encode("utf-8")).hexdigest()[:12],
                value,
            )

        COMMAND_NAME = {command_name!r}
        EXPECTED_PROFILE = {expected_profile!r}
        DEFAULT_REAL_BINARY = {str(real_binary)!r}
        REAL_BINARY_ENV_KEY = {env_key!r}
        KEP_AUTH_REAL_BINARY_ENV_KEY = {_real_bin_env_key("kep-auth")!r}
        DEFAULT_ENV_NAME = {"online"!r}
        IDENTITY_URLS = {identity_urls!r}
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

        def _append_security_event(
            *,
            event_type: str,
            command_name: str,
            reason: str,
            profile_fingerprint_only: bool = False,
        ) -> None:
            event = {{
                "@timestamp": _timestamp_iso(),
                "event_type": event_type,
            }}
            profile = (
                EXPECTED_PROFILE
                if profile_fingerprint_only
                else str(os.environ.get("HERMES_PROFILE") or "").strip()
            )
            if profile:
                if profile_fingerprint_only:
                    event["profile_fingerprint"] = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:12]
                else:
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

        def _option_values(argv, name):
            values = []
            for idx, arg in enumerate(argv):
                if arg == name:
                    values.append(str(argv[idx + 1] or "").strip() if idx + 1 < len(argv) else "")
                elif arg.startswith(name + "="):
                    values.append(arg.split("=", 1)[1].strip())
            return values

        def _scope_args_match(argv) -> bool:
            profiles = _option_values(argv, "--profile")
            if profiles and any(value != EXPECTED_PROFILE for value in profiles):
                return False
            envs = [value.lower() for value in _option_values(argv, "--env")]
            return not envs or (len(set(envs)) == 1 and envs[0] in IDENTITY_URLS)

        def _is_help_request(argv) -> bool:
            skip_next = False
            first_word = ""
            for arg in argv:
                item = str(arg or "").strip().lower()
                if skip_next:
                    skip_next = False
                    continue
                if item in ("--profile", "--env"):
                    skip_next = True
                    continue
                if item in ("--help", "-h"):
                    return True
                if item.startswith("--"):
                    continue
                first_word = item
                break
            return first_word == "help"

        _READONLY_WRITE_WORDS = (
            "add",
            "approve",
            "auth",
            "bind",
            "create",
            "delete",
            "deny",
            "edit",
            "grant",
            "import",
            "login",
            "logout",
            "patch",
            "post",
            "put",
            "remove",
            "revoke",
            "save",
            "send",
            "set",
            "submit",
            "unbind",
            "update",
            "upload",
            "write",
        )
        _READONLY_READ_WORDS = (
            "describe",
            "detail",
            "fetch",
            "find",
            "get",
            "info",
            "list",
            "query",
            "read",
            "search",
            "show",
            "status",
        )

        def _readonly_enabled() -> bool:
            return str(os.environ.get("HERMES_MULTITENANCY_FEISHU_EXPERT_READONLY") or "").strip() == "1"

        def _readonly_words(argv):
            words = set()
            skip_next = False
            for arg in argv:
                if skip_next:
                    skip_next = False
                    continue
                item = str(arg or "").strip().lower()
                if item in ("--profile", "--env"):
                    skip_next = True
                    continue
                if item.startswith("--"):
                    continue
                for part in re.split(r"[^a-z0-9]+", item):
                    if part:
                        words.add(part)
            return words

        def _readonly_write_reason(argv) -> str:
            if not _readonly_enabled():
                return ""
            if _is_help_request(argv):
                return ""
            words = _readonly_words(argv)
            if not words:
                return "read-only kep/hades command denied: missing read subcommand"
            if words.intersection(_READONLY_WRITE_WORDS):
                return "read-only kep/hades command denied: write subcommand"
            if words.intersection(_READONLY_READ_WORDS):
                return ""
            return "read-only kep/hades command denied: command is not in the read allowlist"

        def _parse_env_name(argv) -> str:
            for idx, arg in enumerate(argv):
                if arg == "--env" and idx + 1 < len(argv):
                    value = str(argv[idx + 1] or "").strip().lower()
                    return value if value in IDENTITY_URLS else DEFAULT_ENV_NAME
                if arg.startswith("--env="):
                    value = arg.split("=", 1)[1].strip().lower()
                    return value if value in IDENTITY_URLS else DEFAULT_ENV_NAME
            return DEFAULT_ENV_NAME

        def _parse_profile(argv) -> str:
            for idx, arg in enumerate(argv):
                if arg == "--profile" and idx + 1 < len(argv):
                    return str(argv[idx + 1] or "").strip()
                if arg.startswith("--profile="):
                    return arg.split("=", 1)[1].strip()
            return ""

        def _live_identity_state(argv) -> str:
            profile = EXPECTED_PROFILE
            if _parse_profile(argv) != profile or not _scope_args_match(argv):
                return "identity_mismatch"
            if COMMAND_NAME == "kep-auth" or _is_help_request(argv):
                return "authenticated"
            if str(os.environ.get("KEP_PROFILE") or "").strip() != profile:
                return "identity_mismatch"
            auth_binary = str(os.environ.get(KEP_AUTH_REAL_BINARY_ENV_KEY) or "").strip()
            if not auth_binary:
                return "unknown"
            try:
                token_proc = subprocess.run(
                    [auth_binary, "--profile", profile, "--env", _parse_env_name(argv), "token"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=dict(os.environ),
                )
            except (OSError, subprocess.TimeoutExpired):
                return "unknown"
            if token_proc.returncode != 0:
                return "needs_auth"
            token = next(
                (line.strip() for line in (token_proc.stdout or "").splitlines() if line.strip().count(".") == 2),
                "",
            )
            if not token:
                return "needs_auth"
            return str(probe_kep_identity(
                token,
                profile_name=profile,
                env_name=_parse_env_name(argv),
                identity_urls=IDENTITY_URLS,
            )["state"])

        def _block_unverified_identity(state: str, argv) -> int:
            env_name = _parse_env_name(argv)
            if state == "needs_auth":
                message = (
                    f'【Hermes】kep-cli {{env_name}} 需要授权：请在 Hermes 连接器面板认证 "kep-cli {{env_name}}"'
                    "（或 /auth），勿在此自行登录或去掉 --profile。"
                )
                exit_code = 77
            elif state == "identity_mismatch":
                message = f"【Hermes】kep-cli {{env_name}} 身份校验不匹配，已阻止请求。"
                exit_code = 75
            else:
                message = f"【Hermes】kep-cli {{env_name}} 暂时无法验证身份，已阻止请求。"
                exit_code = 75
            print(message, file=sys.stderr)
            _append_security_event(
                event_type="kep_cli.live_identity.denied",
                command_name=COMMAND_NAME,
                reason=state,
                profile_fingerprint_only=True,
            )
            return exit_code

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
            if not _has_explicit_profile(argv):
                argv = ["--profile", EXPECTED_PROFILE, *argv]
            readonly_reason = _readonly_write_reason(argv)
            if readonly_reason:
                print(readonly_reason, file=sys.stderr)
                try:
                    _append_security_event(
                        event_type="kep_cli.readonly.denied",
                        command_name=COMMAND_NAME,
                        reason=readonly_reason,
                    )
                except Exception:
                    pass
                return 126

            identity_state = _live_identity_state(argv)
            if identity_state != "authenticated":
                return _block_unverified_identity(identity_state, argv)

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


def install_kep_cli_shim(
    shim_dir: Path,
    *,
    real_bins: dict[str, str],
    expected_profile: str,
    identity_urls: dict[str, str] | None = None,
) -> list[Path]:
    shim_dir = Path(shim_dir)
    shim_dir.mkdir(parents=True, exist_ok=True)
    expected_profile = str(expected_profile or "").strip()
    if not expected_profile:
        raise ValueError("expected_profile is required")
    urls = dict(_KEP_IDENTITY_URLS if identity_urls is None else identity_urls)
    written: list[Path] = []
    for name, real_path in real_bins.items():
        wrapper = shim_dir / name
        wrapper.write_text(
            _shim_program(
                name,
                Path(real_path).expanduser(),
                expected_profile=expected_profile,
                identity_urls=urls,
            ),
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        written.append(wrapper)
    return written
