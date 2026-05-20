"""Compatibility registration for the trusted lark-cli bridge.

Official upstream Hermes does not ship owner's local fork tool named
``lark_cli``. Multitenancy-owned profiles still depend on that bridge for
Feishu/Lark OpenAPI access, so the plugin registers the tool itself when the
routed AIAgent runtime imports this module.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

try:
    from tools.registry import registry, tool_error, tool_result
except ModuleNotFoundError:
    registry = None

    def tool_error(message: str, **kwargs: Any) -> str:
        return json.dumps({"ok": False, "error": message, **kwargs}, ensure_ascii=False)

    def tool_result(**kwargs: Any) -> str:
        return json.dumps(kwargs, ensure_ascii=False)


DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 120
POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "lark_cli_policy.yaml"

_SECRET_PATTERNS = [
    re.compile(r"(?i)(Authorization\s*[:=]\s*Bearer\s+)[^\s\"',}]+"),
    re.compile(r"(?i)(Bearer\s+)[^\s\"',}]+"),
    re.compile(r"(?i)((?:access_token|refresh_token|app_secret|proxy_key)\s*[\"'=:\s]+\s*)[^\"'\s,}]+"),
    re.compile(r"(?i)(LARKSUITE_CLI_PROXY_KEY=)[^\s]+"),
]

_NON_BUSINESS_NOTICE_PATTERNS = [
    re.compile(
        r"(?im)^.*(?:new version|newer version|update available|lark-cli update|upgrade lark-cli).*(?:\n|$)"
    ),
    re.compile(
        r"(?im)^.*(?:有新版本|新版本可用|可升级|升级 lark-cli|更新 lark-cli).*(?:\n|$)"
    ),
]

_SAFE_ENV_NAMES = {
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "TERMINAL_CWD",
    "WORKSPACE",
    "HERMES_BASE_HOME",
    "HERMES_HOME",
    "HERMES_PROFILE",
    "HERMES_LARK_CLI_BIN",
    "LARKSUITE_CLI_AUTH_PROXY",
    "LARKSUITE_CLI_PROXY_KEY",
    "LARKSUITE_CLI_APP_ID",
    "LARKSUITE_CLI_BRAND",
    "LARKSUITE_CLI_DEFAULT_AS",
    "LARKSUITE_CLI_STRICT_MODE",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
}


def _redact(text: str) -> str:
    redacted = text or ""
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(lambda match: f"{match.group(1)}***REDACTED***", redacted)
    return redacted


def _strip_non_business_notices(text: str) -> str:
    cleaned = text or ""
    for pattern in _NON_BUSINESS_NOTICE_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    return cleaned.strip()


def _load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        data = {}
    return data if isinstance(data, dict) else {}


def _normalise_openapi_path(path: str) -> str:
    path = str(path or "").strip()
    if path.startswith(("http://", "https://")):
        marker = "/open-apis/"
        idx = path.find(marker)
        if idx >= 0:
            return path[idx:]
    if not path.startswith("/open-apis/"):
        path = "/open-apis/" + path.lstrip("/")
    return path.split("?", 1)[0].split("#", 1)[0]


def _matches_pattern(argv: list[str], pattern: list[str]) -> bool:
    return len(argv) >= len(pattern) and argv[: len(pattern)] == pattern


def _api_request_from_argv(argv: list[str]) -> tuple[str, str] | None:
    if len(argv) >= 2 and argv[0].upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        return argv[0].upper(), _normalise_openapi_path(argv[1])
    if len(argv) < 3 or argv[0] != "api":
        return None
    return argv[1].upper(), _normalise_openapi_path(argv[2])


def _rule_matches(mode: str, argv: list[str], rule: dict[str, Any]) -> bool:
    if rule.get("mode") != mode:
        return False
    if mode == "api":
        request = _api_request_from_argv(argv)
        if not request:
            return False
        method, path = request
        if str(rule.get("method", "")).upper() != method:
            return False
        if rule.get("path") and path == rule["path"]:
            return True
        prefix = rule.get("path_prefix")
        return bool(prefix and path.startswith(str(prefix)))

    pattern = rule.get("pattern")
    return isinstance(pattern, list) and _matches_pattern(argv, [str(part) for part in pattern])


def _policy_decision(mode: str, argv: list[str], risk: str, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = policy or _load_policy()
    if str(policy.get("default") or "allow").lower() == "allow":
        return {"allowed": True, "approval_required": False, "reason": "trusted lark-cli command"}

    for rule in policy.get("commands") or []:
        if rule.get("risk") == risk and _rule_matches(mode, argv, rule):
            return {"allowed": True, "approval_required": False, "reason": "allowed by policy"}

    return {
        "allowed": False,
        "approval_required": False,
        "reason": f"lark-cli command is not in the {mode} allowlist",
    }


def _resolve_binary() -> str | None:
    configured = os.getenv("HERMES_LARK_CLI_BIN")
    if configured:
        return configured
    base_home = Path(os.getenv("HERMES_BASE_HOME") or Path.home() / ".hermes").expanduser()
    sidecar = base_home / "bin" / "lark-cli-authsidecar"
    if sidecar.is_file():
        return str(sidecar)
    return shutil.which("lark-cli")


def _check_lark_cli() -> bool:
    return bool(_resolve_binary())


def _has_format_flag(argv: list[str]) -> bool:
    return any(item == "--format" or item.startswith("--format=") for item in argv)


def _argv_with_json_format(argv: list[str], mode: str = "api") -> list[str]:
    if _has_format_flag(argv) or mode != "api" or (argv and argv[0] in {"schema", "doctor"}):
        return argv
    return [*argv, "--format", "json"]


def _supports_identity_flag(argv: list[str], mode: str) -> bool:
    if not argv or argv[0] in {"auth", "doctor", "schema"}:
        return False
    if any(item in {"--help", "-h", "help"} for item in argv):
        return False
    return not (mode == "shortcut" and argv[0] in {"event"})


def _has_identity_flag(command: list[str]) -> bool:
    return any(item == "--as" or item.startswith("--as=") for item in command)


def _effective_identity(requested: Any) -> str:
    default_as = str(os.getenv("LARKSUITE_CLI_DEFAULT_AS") or "").strip().lower()
    if default_as in {"user", "bot"}:
        return default_as
    identity = str(requested or "auto").strip().lower()
    if identity in {"user", "bot"}:
        return identity
    return "auto"


def _safe_env() -> dict[str, str]:
    env = {name: value for name, value in os.environ.items() if name in _SAFE_ENV_NAMES and value}
    if "PATH" not in env:
        env["PATH"] = os.defpath
    return env


def _profile_home(env: dict[str, str]) -> Path | None:
    raw_profile = str(env.get("HERMES_PROFILE") or "").strip()
    if raw_profile:
        profile_path = Path(raw_profile).expanduser()
        if profile_path.is_absolute():
            return profile_path.resolve(strict=False)

    raw_home = str(env.get("HERMES_HOME") or "").strip()
    raw_workspace = str(env.get("WORKSPACE") or env.get("TERMINAL_CWD") or "").strip()
    if raw_home and raw_workspace:
        home_path = Path(raw_home).expanduser().resolve(strict=False)
        workspace_path = Path(raw_workspace).expanduser().resolve(strict=False)
        if workspace_path.is_relative_to(home_path):
            return home_path

    if raw_workspace and raw_profile:
        workspace_path = Path(raw_workspace).expanduser().resolve(strict=False)
        return workspace_path.parent if workspace_path.name == "workspace" else workspace_path

    if raw_home:
        return Path(raw_home).expanduser().resolve(strict=False)
    return None


def _workspace_root(env: dict[str, str]) -> Path | None:
    workspace = str(env.get("WORKSPACE") or env.get("TERMINAL_CWD") or "").strip()
    if not workspace:
        profile = _profile_home(env)
        candidate = profile / "workspace" if profile is not None else None
        if candidate is not None and candidate.exists():
            workspace = str(candidate)
    if not workspace:
        return None
    return Path(workspace).expanduser().resolve()


def _profile_runtime_error(env: dict[str, str]) -> str | None:
    profile_home = _profile_home(env)
    workspace = _workspace_root(env)
    if profile_home is None or workspace is None:
        return "lark-cli must run inside a routed profile runtime sandbox"
    if not workspace.is_relative_to(profile_home):
        return "lark-cli workspace must stay inside the current profile"
    return None


def _extract_output_paths(argv: list[str], workspace: Path | None) -> tuple[list[Path], str | None]:
    outputs: list[Path] = []
    idx = 0
    while idx < len(argv):
        item = argv[idx]
        raw_path = ""
        if item == "--output":
            if idx + 1 >= len(argv):
                return [], "--output requires a path"
            raw_path = argv[idx + 1]
            idx += 2
        elif item.startswith("--output="):
            raw_path = item.split("=", 1)[1]
            idx += 1
        else:
            idx += 1
            continue
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            if workspace is None:
                return [], "--output relative paths require workspace"
            path = workspace / path
        resolved = path.resolve(strict=False)
        if workspace is None or not resolved.is_relative_to(workspace):
            return [], "--output path must stay inside workspace"
        outputs.append(resolved)
    return outputs, None


def _existing_output_files(paths: list[Path]) -> list[str]:
    return [str(path) for path in paths if path.is_file()]


def _parse_json_output(stdout: str) -> Any:
    if not stdout.strip():
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None


LARK_CLI_SCHEMA = {
    "name": "lark_cli",
    "description": (
        "Run the trusted official lark-cli command for Feishu/Lark OpenAPI work. "
        "Prefer this over native Feishu tools for reads, writes, exports, and long-tail OAPI calls; "
        "Hermes only supplies identity, profile sandboxing, redaction, and result display."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["shortcut", "schema", "api"],
                "description": "Command family: shortcut, schema method, or raw OpenAPI api call.",
            },
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "description": "lark-cli arguments excluding the binary name.",
            },
            "identity": {
                "type": "string",
                "enum": ["user", "bot", "auto"],
                "description": "Intended lark-cli identity.",
            },
            "risk": {
                "type": "string",
                "enum": ["read", "write", "export", "admin"],
                "description": "Risk class for display and logging; lark-cli is trusted to execute directly.",
            },
            "reason": {"type": "string", "description": "Why this lark-cli call is needed."},
            "timeout_seconds": {"type": "integer", "description": "Optional timeout, capped by Hermes."},
        },
        "required": ["mode", "argv", "risk", "reason"],
    },
}


def _handle_lark_cli_execute(args: dict, **_kwargs: Any) -> str:
    mode = str(args.get("mode") or "").strip()
    risk = str(args.get("risk") or "read").strip()
    argv_raw = args.get("argv")
    if mode not in {"shortcut", "schema", "api"}:
        return tool_error("mode must be one of shortcut, schema, api")
    if risk not in {"read", "write", "export", "admin"}:
        return tool_error("risk must be one of read, write, export, admin")
    if not isinstance(argv_raw, list) or not all(isinstance(item, str) and item for item in argv_raw):
        return tool_error("argv must be a non-empty list of strings")
    if any(item == "--" for item in argv_raw):
        return tool_error("argv must not contain raw -- separators")

    argv = list(argv_raw)
    if mode == "api":
        api_req = _api_request_from_argv(argv)
        if not api_req:
            return tool_error("api mode requires argv like ['api', '<METHOD>', '<PATH>'] or ['<METHOD>', '<PATH>']")
        argv = ["api", api_req[0], api_req[1], *argv[3:]] if argv and argv[0] == "api" else ["api", api_req[0], api_req[1], *argv[2:]]
    elif argv and argv[0] == "api":
        return tool_error("api command must use mode=api")

    decision = _policy_decision(mode, argv, risk)
    if not decision.get("allowed"):
        return tool_error(decision["reason"], mode=mode, command=argv, risk=risk)

    binary = _resolve_binary()
    if not binary:
        return tool_error("lark-cli binary not found; set HERMES_LARK_CLI_BIN or install lark-cli")

    identity = _effective_identity(args.get("identity"))
    command = [binary, *_argv_with_json_format(argv, mode)]
    if not _has_identity_flag(command) and identity in {"user", "bot"} and _supports_identity_flag(argv, mode):
        command.extend(["--as", identity])

    timeout = min(int(args.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)
    env = _safe_env()
    runtime_error = _profile_runtime_error(env)
    if runtime_error:
        return tool_error(runtime_error, mode=mode, command=argv, risk=risk)
    workspace = _workspace_root(env)
    output_paths, output_error = _extract_output_paths(command, workspace)
    if output_error:
        return tool_error(output_error, mode=mode, command=argv, risk=risk)

    cwd = str(workspace) if workspace is not None and workspace.exists() else None
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
            cwd=cwd,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return tool_error(
            f"lark-cli timed out after {timeout}s",
            mode=mode,
            command=argv,
            risk=risk,
            stdout_redacted=_redact(str(exc.output or "")),
            stderr_redacted=_redact(str(exc.stderr or "")),
        )
    except PermissionError as exc:
        return tool_error(
            f"lark-cli failed in profile sandbox: {_redact(str(exc))}",
            mode=mode,
            command=argv,
            risk=risk,
        )

    stdout = _strip_non_business_notices(_redact(completed.stdout))
    stderr = _strip_non_business_notices(_redact(completed.stderr))
    parsed = _parse_json_output(stdout)
    return tool_result(
        ok=completed.returncode == 0,
        approval_required=False,
        mode=mode,
        identity=identity,
        command=argv,
        exit_code=completed.returncode,
        json=parsed,
        stdout=stdout if parsed is None else "",
        stderr_redacted=stderr,
        files=_existing_output_files(output_paths),
    )


if registry is not None:
    registry.register_toolset_alias("lark-cli", "lark_cli")
    registry.register(
        name="lark_cli",
        toolset="lark_cli",
        schema=LARK_CLI_SCHEMA,
        handler=_handle_lark_cli_execute,
        check_fn=_check_lark_cli,
        requires_env=[],
        is_async=False,
        description="Official lark-cli bridge for Feishu/Lark OpenAPI",
        emoji="Lark",
    )
