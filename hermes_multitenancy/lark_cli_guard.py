from __future__ import annotations

import secrets
import textwrap
from pathlib import Path

from .security_audit import DEFAULT_AUDIT_PATH

HERMES_LARK_CLI_RUN_TOKEN = "HERMES_LARK_CLI_RUN_TOKEN"
HERMES_LARK_CLI_AUTHORIZED = "HERMES_LARK_CLI_AUTHORIZED"
HERMES_LARK_CLI_REAL_BIN = "HERMES_LARK_CLI_REAL_BIN"

_SHIM_NAMES = ("lark-cli", "lark", "lark-mcp")


def generate_lark_cli_run_token() -> str:
    return secrets.token_urlsafe(24)


def _shim_program(real_binary: Path) -> str:
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        from __future__ import annotations

        import hashlib
        import json
        import os
        import re
        import sys
        from datetime import datetime, timedelta, timezone
        from pathlib import Path

        # Self-contained mirror of security_audit._redact_embedded_ids: the shim
        # cannot import the plugin, so it scrubs embedded ou_/oc_ ids from the
        # raw HERMES_PROFILE here too (prod profiles embed chat_id/open_id).
        _EMBEDDED_ID_RE = re.compile(r"(?<![A-Za-z0-9])(ou|oc)_[A-Za-z0-9_-]+")

        def _redact_embedded_ids(value):
            return _EMBEDDED_ID_RE.sub(
                lambda m: m.group(1) + "_" + hashlib.sha256(m.group(0).encode("utf-8")).hexdigest()[:12],
                value,
            )

        HERMES_LARK_CLI_RUN_TOKEN = {HERMES_LARK_CLI_RUN_TOKEN!r}
        HERMES_LARK_CLI_AUTHORIZED = {HERMES_LARK_CLI_AUTHORIZED!r}
        HERMES_LARK_CLI_REAL_BIN = {HERMES_LARK_CLI_REAL_BIN!r}
        DEFAULT_AUDIT_PATH = {str(DEFAULT_AUDIT_PATH)!r}
        DEFAULT_REAL_BINARY = {str(real_binary)!r}
        _SHANGHAI_TZ = timezone(timedelta(hours=8))

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

        def main() -> int:
            run_token = str(os.environ.get(HERMES_LARK_CLI_RUN_TOKEN) or "")
            authorized = str(os.environ.get(HERMES_LARK_CLI_AUTHORIZED) or "")
            if run_token and authorized and authorized == run_token:
                real_binary = str(os.environ.get(HERMES_LARK_CLI_REAL_BIN) or DEFAULT_REAL_BINARY).strip()
                os.execve(real_binary, [real_binary, *sys.argv[1:]], dict(os.environ))
            command_name = Path(sys.argv[0] or "lark-cli").name or "lark-cli"
            reason = "direct execution denied; Use the registered lark_cli tool."
            _append_security_event(
                event_type="lark_cli.direct_exec.denied",
                command_name=command_name,
                reason=reason,
            )
            print(
                "Direct execution denied. Use the registered lark_cli tool."
                ' Packaged skill scripts run via lark_cli mode="script".',
                file=sys.stderr,
            )
            return 126

        if __name__ == "__main__":
            raise SystemExit(main())
        """
    )


def install_lark_cli_shim(shim_dir: Path, *, real_binary: Path) -> Path:
    shim_dir = Path(shim_dir)
    shim_dir.mkdir(parents=True, exist_ok=True)
    body = _shim_program(Path(real_binary).expanduser())
    primary = shim_dir / "lark-cli"
    for name in _SHIM_NAMES:
        path = shim_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return primary
