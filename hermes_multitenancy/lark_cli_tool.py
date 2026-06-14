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
import urllib.parse
from pathlib import Path
from typing import Any

import yaml

from .lark_cli_guard import (
    HERMES_LARK_CLI_AUTHORIZED,
    HERMES_LARK_CLI_REAL_BIN,
    HERMES_LARK_CLI_RUN_TOKEN,
)
from .runtime import strict_context_enabled

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
_PERSONAL_FEISHU_IM_USER_AUTH_REQUIRED = (
    "飞书个人消息读取需要先完成本人授权。"
    "请在飞书私聊 Hermes 发送 `/feishu_auth`，"
    "或在 WebUI「凭证」页点击 Lark-cli 的「授权/重新授权」。"
)

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
    "HERMES_FEISHU_USER_OPEN_ID",
    "HERMES_FEISHU_BOT_ALLOWED_CHAT_IDS",
    "HERMES_LARK_CLI_BIN",
    "HERMES_LARK_CLI_AUTHORIZED",
    "HERMES_LARK_CLI_RUN_TOKEN",
    "HERMES_LARK_CLI_REAL_BIN",
    "HERMES_MT_SECURITY_AUDIT_PATH",
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


def _normalise_openapi_path_with_query(path: str) -> str:
    path = str(path or "").strip()
    if path.startswith(("http://", "https://")):
        marker = "/open-apis/"
        idx = path.find(marker)
        if idx >= 0:
            path = path[idx:]
    if not path.startswith("/open-apis/"):
        path = "/open-apis/" + path.lstrip("/")
    return path.split("#", 1)[0]


def _api_path_arg_from_argv(argv: list[str]) -> str:
    if len(argv) >= 2 and argv[0].upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        return str(argv[1])
    if len(argv) >= 3 and argv[0] == "api":
        return str(argv[2])
    return ""


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


def _without_identity_flag(argv: list[str]) -> list[str]:
    cleaned: list[str] = []
    skip_next = False
    for item in argv:
        if skip_next:
            skip_next = False
            continue
        if item == "--as":
            skip_next = True
            continue
        if item.startswith("--as="):
            continue
        cleaned.append(item)
    return cleaned


def _effective_identity(requested: Any, *, allow_explicit_bot: bool = False) -> str:
    identity = str(requested or "auto").strip().lower()
    if identity == "bot" and allow_explicit_bot:
        return "bot"
    default_as = str(os.getenv("LARKSUITE_CLI_DEFAULT_AS") or "").strip().lower()
    if default_as in {"user", "bot"}:
        return default_as
    if identity in {"user", "bot"}:
        return identity
    return "auto"


def _is_group_profile(profile_home: Path | None) -> bool:
    if profile_home is None:
        return False
    if profile_home.name.startswith("feishu_group_"):
        return True
    marker = profile_home / "group_profile.json"
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return False
    return str(payload.get("kind") or "").strip().lower() == "group"


def _group_profile_chat_id(profile_home: Path | None) -> str:
    if profile_home is None:
        return ""
    marker = profile_home / "group_profile.json"
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str(payload.get("chat_id") or "").strip()


_PERSONAL_IM_READ_SHORTCUTS = frozenset(
    {
        ("messages", "list"),
        ("im", "+chat-list"),
        ("im", "+chat-messages-list"),
        ("im", "+messages-search"),
        ("im", "+flag-list"),
        ("im", "+messages-mget"),
        ("im", "+threads-messages-list"),
    }
)
_IM_READ_API_PREFIXES = (
    "/open-apis/im/v1/messages",
    "/open-apis/im/v1/chats",
    "/open-apis/im/v1/flags",
    "/open-apis/im/v1/threads",
)
_IM_READ_API_EXACT_METHODS = {
    ("POST", "/open-apis/im/v1/messages/search"),
}


def _shortcut_prefix(argv: list[str]) -> tuple[str, str] | None:
    if len(argv) < 2:
        return None
    return (argv[0], argv[1])


def _argv_option_value(argv: list[str], name: str) -> str:
    prefix = name + "="
    for idx, item in enumerate(argv):
        if item == name and idx + 1 < len(argv):
            return str(argv[idx + 1] or "").strip()
        if item.startswith(prefix):
            return item.split("=", 1)[1].strip()
    return ""


def _argv_json_option(argv: list[str], name: str) -> dict[str, Any]:
    raw = _argv_option_value(argv, name)
    if not raw or raw.startswith(("@", "-")):
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _argv_params_option(argv: list[str]) -> dict[str, str]:
    raw = _argv_option_value(argv, "--params")
    if not raw or raw.startswith(("@", "-")):
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items() if values}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items()}


def _argv_path_query_params(argv: list[str]) -> dict[str, str]:
    parsed = urllib.parse.urlsplit(_api_path_arg_from_argv(argv))
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    return {key: values[-1] for key, values in query.items() if values}


def _bot_im_send_chat_id(mode: str, argv: list[str]) -> str:
    if mode == "shortcut" and _shortcut_prefix(argv) == ("im", "+messages-send"):
        if _argv_option_value(argv, "--user-id"):
            return ""
        return _argv_option_value(argv, "--chat-id")
    if mode != "api":
        return ""
    request = _api_request_from_argv(argv)
    if not request:
        return ""
    method, path = request
    if method != "POST" or path != "/open-apis/im/v1/messages":
        return ""
    params = {**_argv_path_query_params(argv), **_argv_params_option(argv)}
    if str(params.get("receive_id_type") or "").strip() != "chat_id":
        return ""
    body = _argv_json_option(argv, "--data")
    return str(body.get("receive_id") or "").strip()


def _is_bot_im_image_upload(mode: str, argv: list[str]) -> bool:
    if mode in {"shortcut", "schema"}:
        return len(argv) >= 3 and tuple(argv[:3]) == ("im", "images", "create")
    if mode != "api":
        return False
    request = _api_request_from_argv(argv)
    if not request:
        return False
    method, path = request
    return method == "POST" and path == "/open-apis/im/v1/images"


def _allowed_bot_chat_ids(env: dict[str, str]) -> frozenset[str]:
    raw = str(env.get("HERMES_FEISHU_BOT_ALLOWED_CHAT_IDS") or "")
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def _broker_proxy_configured(env: dict[str, str]) -> bool:
    """True only when the lark-cli auth broker proxy is actually wired into this
    subprocess. The broker is the authoritative owner-vs-sender gate; without it
    there is nothing to defer to, so a bot IM send must NOT be let through."""
    return bool(
        str(env.get("LARKSUITE_CLI_AUTH_PROXY") or "").strip()
        and str(env.get("LARKSUITE_CLI_PROXY_KEY") or "").strip()
    )


def _personal_bot_im_send_allowed(env: dict[str, str], mode: str, argv: list[str]) -> bool:
    if not str(env.get("HERMES_FEISHU_USER_OPEN_ID") or "").strip():
        return False
    if _is_group_profile(_profile_home(env)):
        return False
    target_chat_id = _bot_im_send_chat_id(mode, argv)
    return bool(target_chat_id and target_chat_id in _allowed_bot_chat_ids(env))


def _personal_bot_im_image_upload_allowed(env: dict[str, str], mode: str, argv: list[str]) -> bool:
    if not str(env.get("HERMES_FEISHU_USER_OPEN_ID") or "").strip():
        return False
    if _is_group_profile(_profile_home(env)):
        return False
    return bool(_is_bot_im_image_upload(mode, argv) and _allowed_bot_chat_ids(env))


def _is_im_read_request(mode: str, argv: list[str]) -> bool:
    if mode == "shortcut":
        return _shortcut_prefix(argv) in _PERSONAL_IM_READ_SHORTCUTS
    if mode == "api":
        request = _api_request_from_argv(argv)
        if not request:
            return False
        method, path = request
        return (method, path) in _IM_READ_API_EXACT_METHODS or (method == "GET" and path.startswith(_IM_READ_API_PREFIXES))
    return False


def _group_current_chat_im_read_allowed(mode: str, argv: list[str], current_chat_id: str) -> bool:
    if not current_chat_id:
        return False
    if mode == "shortcut" and _shortcut_prefix(argv) == ("im", "+chat-messages-list"):
        return _argv_option_value(argv, "--chat-id") == current_chat_id
    return False


def _feishu_im_read_identity_error(env: dict[str, str], mode: str, argv: list[str], risk: str, identity: str) -> str | None:
    del risk
    if not _is_im_read_request(mode, argv):
        return None
    profile_home = _profile_home(env)
    is_group = _is_group_profile(profile_home)
    if is_group:
        current_chat_id = _group_profile_chat_id(profile_home)
        if _group_current_chat_im_read_allowed(mode, argv, current_chat_id):
            return None
        return (
            "group profile Feishu message read is limited to the current chat; "
            "refusing global or cross-chat bot-visible IM history access"
        )
    if identity == "user" and str(env.get("HERMES_FEISHU_USER_OPEN_ID") or "").strip():
        return None
    return _PERSONAL_FEISHU_IM_USER_AUTH_REQUIRED


def _personal_user_write_identity_error(
    env: dict[str, str],
    mode: str,
    argv: list[str],
    risk: str,
    identity: str,
    requested_identity: str,
) -> str | None:
    if not str(env.get("HERMES_FEISHU_USER_OPEN_ID") or "").strip():
        return None
    if _is_group_profile(_profile_home(env)):
        return None
    if requested_identity == "bot":
        if _personal_bot_im_send_allowed(env, mode, argv) or _personal_bot_im_image_upload_allowed(env, mode, argv):
            return None
        if _is_im_read_request(mode, argv):
            return None
        if _bot_im_send_chat_id(mode, argv):
            # A bot IM send is inherently a write, regardless of the caller-declared
            # `risk` (which can be mis-set to "read"). Decide it on its own merits:
            if _broker_proxy_configured(env):
                # Broker proxy wired: defer to the broker, which live-re-checks
                # routing and allows the sender's own (possibly freshly-created)
                # group or denies an unmapped one. This child preflight runs in the
                # sandboxed subprocess and cannot read routing, so it must NOT
                # hard-refuse here — otherwise a sender's just-created own group
                # fails until the next turn.
                return None
            # No broker to authorize → refuse the unmapped bot IM send outright.
            return (
                "personal profile bot identity is limited to owner mapped group chats; "
                "refusing unmapped or non-message bot write"
            )
        if risk in {"write", "admin"}:
            return (
                "personal profile bot identity is limited to owner mapped group chats; "
                "refusing unmapped or non-message bot write"
            )
    if risk not in {"write", "admin"}:
        return None
    if identity == "user":
        return None
    return (
        "personal profile write requires bound Feishu user identity; "
        "refusing to execute with bot/auto identity"
    )


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
                "description": (
                    "Intended lark-cli identity. Use auto/user for personal profile work; "
                    "use bot for owner-mapped Feishu group message sends/image uploads or group-profile contexts. "
                    "Personal bot writes to unmapped groups or other non-message APIs are refused."
                ),
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
        path_for_command = _normalise_openapi_path_with_query(_api_path_arg_from_argv(argv))
        argv = (
            ["api", api_req[0], path_for_command, *argv[3:]]
            if argv and argv[0] == "api"
            else ["api", api_req[0], path_for_command, *argv[2:]]
        )
    elif argv and argv[0] == "api":
        return tool_error("api command must use mode=api")

    decision = _policy_decision(mode, argv, risk)
    if not decision.get("allowed"):
        return tool_error(decision["reason"], mode=mode, command=argv, risk=risk)

    binary = _resolve_binary()
    if not binary:
        return tool_error("lark-cli binary not found; set HERMES_LARK_CLI_BIN or install lark-cli")

    env = _safe_env()
    run_token = str(os.environ.get(HERMES_LARK_CLI_RUN_TOKEN) or "")
    if strict_context_enabled() and run_token:
        env[HERMES_LARK_CLI_AUTHORIZED] = run_token
    requested_identity = str(args.get("identity") or "auto").strip().lower()
    allow_explicit_bot = requested_identity == "bot" and (
        _personal_bot_im_send_allowed(env, mode, argv)
        or _personal_bot_im_image_upload_allowed(env, mode, argv)
        # An explicit bot IM message send (has a target chat_id) uses the bot
        # identity and lets the broker authorize it (live routing re-check), so a
        # sender's freshly-created own group isn't blocked by the turn-start cache.
        # Only when the broker proxy is wired — without it there is no authoritative
        # gate, so we do not promote to the bot identity.
        or bool(_bot_im_send_chat_id(mode, argv) and _broker_proxy_configured(env))
    )
    identity = _effective_identity(args.get("identity"), allow_explicit_bot=allow_explicit_bot)
    if identity in {"user", "bot"} and _supports_identity_flag(argv, mode):
        argv = _without_identity_flag(argv)
    command = [binary, *_argv_with_json_format(argv, mode)]
    if not _has_identity_flag(command) and identity in {"user", "bot"} and _supports_identity_flag(argv, mode):
        command.extend(["--as", identity])

    timeout = min(int(args.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)
    runtime_error = _profile_runtime_error(env)
    if runtime_error:
        return tool_error(runtime_error, mode=mode, command=argv, risk=risk)
    workspace = _workspace_root(env)
    output_paths, output_error = _extract_output_paths(command, workspace)
    if output_error:
        return tool_error(output_error, mode=mode, command=argv, risk=risk)
    identity_error = _personal_user_write_identity_error(env, mode, argv, risk, identity, requested_identity)
    if identity_error:
        return tool_error(identity_error, mode=mode, command=argv, risk=risk, identity=identity)
    im_read_error = _feishu_im_read_identity_error(env, mode, argv, risk, identity)
    if im_read_error:
        return tool_error(im_read_error, mode=mode, command=argv, risk=risk, identity=identity)

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
