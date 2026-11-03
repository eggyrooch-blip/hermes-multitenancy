from __future__ import annotations

import secrets
import textwrap
from pathlib import Path

from .security_audit import DEFAULT_AUDIT_PATH

HERMES_LARK_CLI_RUN_TOKEN = "HERMES_LARK_CLI_RUN_TOKEN"
HERMES_LARK_CLI_AUTHORIZED = "HERMES_LARK_CLI_AUTHORIZED"
HERMES_LARK_CLI_REAL_BIN = "HERMES_LARK_CLI_REAL_BIN"
# Non-secret proof of the strict profile runtime for children that the
# terminal/code tools' secret-name scrub keeps the run token away from. It
# grants self-serve only; it can never be traded for the AUTHORIZED fast path.
HERMES_LARK_CLI_SELF_SERVE = "HERMES_LARK_CLI_SELF_SERVE"

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
        HERMES_LARK_CLI_SELF_SERVE = {HERMES_LARK_CLI_SELF_SERVE!r}
        DEFAULT_AUDIT_PATH = {str(DEFAULT_AUDIT_PATH)!r}
        DEFAULT_REAL_BINARY = {str(real_binary)!r}
        _SHANGHAI_TZ = timezone(timedelta(hours=8))

        def _timestamp_iso() -> str:
            return datetime.now(tz=_SHANGHAI_TZ).isoformat(timespec="seconds")

        def _append_security_event(
            *, event_type: str, command_name: str, reason: str, argv_redacted: str = ""
        ) -> bool:
            event = {{
                "@timestamp": _timestamp_iso(),
                "event_type": event_type,
            }}
            if argv_redacted:
                event["argv_redacted"] = argv_redacted
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
                # Denials stay best-effort (a call that is refused anyway needs
                # no receipt to be safe); the self-serve lane checks this return
                # and refuses to exec when the receipt could not be written.
                return False
            return True

        def _requested_identities(argv):
            # Mirrors lark_cli_tool._has_identity_flag / _without_identity_flag:
            # lark-cli accepts only `--as <value>` and `--as=<value>`. EVERY
            # occurrence is collected, never just the first: `--as user --as bot`
            # would otherwise read as "user" here and still reach the binary as
            # bot (last flag wins downstream).
            found = []
            for index, item in enumerate(argv):
                if item == "--as":
                    found.append(str(argv[index + 1] if index + 1 < len(argv) else "").strip().lower())
                elif item.startswith("--as="):
                    found.append(item.split("=", 1)[1].strip().lower())
            return found

        def _argv_digest(argv) -> str:
            # Shape only, never values: argv routinely carries access tokens
            # (`api GET /...?access_token=...`), message bodies and proxy keys,
            # and this line lands in the security audit. An expected flag value
            # is consumed BEFORE the leading-hyphen test, so `--content
            # -private-secret` cannot masquerade as a flag name; positionals are
            # kept only for the leading command words and for API paths (query
            # string stripped), everything else is redacted.
            parts = []
            expect_value = False
            positional_index = 0
            for raw in argv:
                item = str(raw)
                if expect_value:
                    expect_value = False
                    parts.append("<redacted>")
                    continue
                if item.startswith("-"):
                    expect_value = "=" not in item
                    name = item.split("=", 1)[0]
                    parts.append(name if expect_value else name + "=<redacted>")
                    continue
                if positional_index < 2:
                    parts.append(item.split("?", 1)[0])
                elif item.startswith("/"):
                    parts.append(item.split("?", 1)[0])
                else:
                    parts.append("<redacted>")
                positional_index += 1
            return _redact_embedded_ids(" ".join(parts))[:512]

        _DIAGNOSTIC_PAIRS = {{("auth", "status"), ("auth", "check"), ("auth", "list")}}
        _DIAGNOSTIC_SINGLES = {{"--version", "-v", "version"}}

        def _diagnostic_kind(argv):
            # Closed set on purpose: `auth login` and friends are NOT answered
            # locally, they keep going to the sidecar which refuses them.
            if len(argv) == 1 and argv[0] in _DIAGNOSTIC_SINGLES:
                return "version"
            if tuple(argv[:2]) in _DIAGNOSTIC_PAIRS and len(argv) == 2:
                return "_".join(argv[:2])
            return ""

        def _diagnostic_answer(kind):
            # Only facts this process can verify from its own environment. It
            # deliberately does NOT claim the credentials are valid — that is
            # settled by the first real API call, which is forwarded unchanged.
            profile = _redact_embedded_ids(str(os.environ.get("HERMES_PROFILE") or "").strip())
            return {{
                "ok": True,
                "data": {{
                    "managed_by": "hermes",
                    "diagnostic": kind,
                    "profile": profile,
                    "identity": "user",
                    "credentials": "host-managed",
                    "interactive_auth": "unavailable",
                    "note": (
                        "Credentials are provided by the host broker at call time, so"
                        " lark-cli's own auth/version diagnostics are not proxied and"
                        " this answer is synthesized locally. API commands are"
                        " forwarded unchanged; credential validity, permissions and"
                        " connectivity have NOT been checked here."
                    ),
                }},
            }}

        def main() -> int:
            run_token = str(os.environ.get(HERMES_LARK_CLI_RUN_TOKEN) or "")
            authorized = str(os.environ.get(HERMES_LARK_CLI_AUTHORIZED) or "")
            self_serve = str(os.environ.get(HERMES_LARK_CLI_SELF_SERVE) or "").strip() == "1"
            real_binary = str(os.environ.get(HERMES_LARK_CLI_REAL_BIN) or DEFAULT_REAL_BINARY).strip()
            # The marker is mirrored ONLY into terminal children, so its presence
            # says "this is model-authored shell", and such a call must never take
            # the AUTHORIZED fast path — that path skips the bot narrowing. The
            # deployed runtime scrubs HERMES_*_TOKEN out of terminal children so
            # the grant cannot be reconstructed there today, but this does not
            # depend on that: if a future runtime let the run token through, a
            # child could otherwise export AUTHORIZED=$RUN_TOKEN and walk past the
            # bot gate (2026-09-11 review finding p1).
            if not self_serve and run_token and authorized and authorized == run_token:
                os.execve(real_binary, [real_binary, *sys.argv[1:]], dict(os.environ))
            command_name = Path(sys.argv[0] or "lark-cli").name or "lark-cli"

            if run_token or self_serve:
                # Self-serve lane. The run token is minted only by
                # subprocess_env's strict build path, so its presence proves the
                # strict profile runtime — the same proof lark_cli_tool uses to
                # hand out the AUTHORIZED grant. Credentials are NOT in this
                # env and never were: the real binary is the authsidecar, which
                # resolves identity and secrets against the broker per call.
                # Refusing the call therefore protected no credential; the one
                # policy it actually enforced was lark_cli_tool's narrowing of
                # bot-identity escalation, so that is what we enforce here
                # instead of denying the whole path (2026-09-11: 133 denials /
                # 25 profiles in September, agents read the refusal as "this
                # environment cannot reach Feishu" and fabricated output).
                if "bot" in _requested_identities(sys.argv[1:]):
                    reason = "direct execution denied; bot identity requires the registered lark_cli tool."
                    _append_security_event(
                        event_type="lark_cli.direct_exec.denied",
                        command_name=command_name,
                        reason=reason,
                        argv_redacted=_argv_digest(sys.argv[1:]),
                    )
                    print(
                        "Direct execution denied for bot identity. Drop `--as bot` to run as the"
                        " profile user, or use the registered lark_cli tool for a bot-identity call.",
                        file=sys.stderr,
                    )
                    return 126
                kind = _diagnostic_kind(sys.argv[1:])
                if kind:
                    _append_security_event(
                        event_type="lark_cli.diagnostic.answered",
                        command_name=command_name,
                        reason="host-managed credentials; diagnostics answered locally",
                        argv_redacted=_argv_digest(sys.argv[1:]),
                    )
                    print(json.dumps(_diagnostic_answer(kind), ensure_ascii=False))
                    return 0

                audited = _append_security_event(
                    event_type="lark_cli.direct_exec.self_served",
                    command_name=command_name,
                    reason=(
                        "strict runtime proven by "
                        + ("run token" if run_token else "self-serve marker")
                        + "; forced user identity"
                    ),
                    argv_redacted=_argv_digest(sys.argv[1:]),
                )
                if not audited:
                    # No receipt, no run. Self-serve trades the path gate for an
                    # audit trail, so an unwritable/full audit sink must stop the
                    # call rather than silently produce unaudited traffic.
                    print(
                        "Direct execution denied: the security audit receipt could not be"
                        " written. Use the registered lark_cli tool.",
                        file=sys.stderr,
                    )
                    return 126
                env = dict(os.environ)
                # The sandbox owns this env, so pin the default identity rather
                # than trusting an inherited LARKSUITE_CLI_DEFAULT_AS=bot. The
                # AUTHORIZED grant is deliberately NOT set: nested lark-cli
                # calls re-enter this same check and stay under the bot rule.
                env["LARKSUITE_CLI_DEFAULT_AS"] = "user"
                env.pop(HERMES_LARK_CLI_AUTHORIZED, None)
                os.execve(real_binary, [real_binary, *sys.argv[1:]], env)

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
