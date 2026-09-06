"""Compatibility registration for the trusted lark-cli bridge.

Official upstream Hermes does not ship owner's local fork tool named
``lark_cli``. Multitenancy-owned profiles still depend on that bridge for
Feishu/Lark OpenAPI access, so the plugin registers the tool itself when the
routed AIAgent runtime imports this module.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

import yaml

from .connector_failure_classifier import classify_connector_failure
from .feishu_permission_errors import annotate_permission_error
from .lark_cli_guard import (
    HERMES_LARK_CLI_AUTHORIZED,
    HERMES_LARK_CLI_REAL_BIN,
    HERMES_LARK_CLI_RUN_TOKEN,
)
from .runtime import strict_context_enabled
from .oauth_cli_guard import is_headless_oauth_attempt
from .update_center import sanitize_user_visible_output

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
logger = logging.getLogger(__name__)
_OPERATION_FIELD = "_hermes_operation"
_OPERATION_DB_NAME = "operation-checkpoints.db"
_STRICT_WRITE_VERBS = frozenset(
    {
        "add",
        "append",
        "archive",
        "create",
        "delete",
        "import",
        "move",
        "patch",
        "remove",
        "replace",
        "reply",
        "send",
        "set",
        "update",
        "upload",
    }
)
_STRICT_READ_SCHEMA_METHODS = frozenset(
    {
        ("approval", "approvals", "get"),
        ("approval", "instances", "get"),
        ("approval", "instances", "initiated"),
        ("approval", "tasks", "query"),
        ("calendar", "calendars", "get"),
        ("calendar", "calendars", "list"),
        ("calendar", "calendars", "primary"),
        ("calendar", "calendars", "search"),
        ("calendar", "event.attendees", "list"),
        ("calendar", "events", "get"),
        ("calendar", "events", "instance_view"),
        ("calendar", "events", "search_event"),
        ("calendar", "events", "share_info"),
        ("calendar", "freebusys", "list"),
        ("contact", "user_profiles", "batch_query"),
        ("drive", "file.comment.replys", "list"),
        ("drive", "file.comments", "batch_query"),
        ("drive", "file.comments", "list"),
        ("drive", "file.statistics", "get"),
        ("drive", "file.view_records", "list"),
        ("drive", "files", "list"),
        ("drive", "metas", "batch_query"),
        ("drive", "permission.members", "auth"),
        ("drive", "permission.public", "get"),
        ("drive", "quota_details", "get"),
        ("drive", "user", "subscription_status"),
        ("im", "chat.members", "bots"),
        ("im", "chat.members", "get"),
        ("im", "chat.moderation", "get"),
        ("im", "chat.nickname", "get"),
        ("im", "chat.user_setting", "batch_query"),
        ("im", "chats", "get"),
        ("im", "feed.groups", "batch_query"),
        ("im", "messages", "read_users"),
        ("im", "pins", "list"),
        ("im", "reactions", "batch_query"),
        ("im", "reactions", "list"),
        ("mail", "user_mailbox.drafts", "get"),
        ("mail", "user_mailbox.drafts", "list"),
        ("mail", "user_mailbox.folders", "get"),
        ("mail", "user_mailbox.folders", "list"),
        ("mail", "user_mailbox.labels", "get"),
        ("mail", "user_mailbox.labels", "list"),
        ("mail", "user_mailbox.mail_contacts", "list"),
        ("mail", "user_mailbox.message.attachments", "download_url"),
        ("mail", "user_mailbox.messages", "get"),
        ("mail", "user_mailbox.messages", "list"),
        ("mail", "user_mailbox.messages", "send_status"),
        ("mail", "user_mailbox.rules", "list"),
        ("mail", "user_mailbox.sent_messages", "get_recall_detail"),
        ("mail", "user_mailbox.settings", "send_as"),
        ("mail", "user_mailbox.template.attachments", "download_url"),
        ("mail", "user_mailbox.templates", "get"),
        ("mail", "user_mailbox.templates", "list"),
        ("mail", "user_mailbox.threads", "get"),
        ("mail", "user_mailbox.threads", "list"),
        ("mail", "user_mailboxes", "accessible_mailboxes"),
        ("mail", "user_mailboxes", "profile"),
        ("mindnotes", "nodes", "list"),
        ("minutes", "minutes", "get"),
        ("okr", "alignments", "get"),
        ("okr", "categories", "list"),
        ("okr", "cycle.objectives", "list"),
        ("okr", "cycles", "list"),
        ("okr", "key_result.indicators", "list"),
        ("okr", "key_results", "get"),
        ("okr", "objective.alignments", "list"),
        ("okr", "objective.indicators", "list"),
        ("okr", "objective.key_results", "list"),
        ("okr", "objectives", "get"),
        ("slides", "xml_presentation.history", "list"),
        ("slides", "xml_presentation.history", "revert_status"),
        ("slides", "xml_presentation.slide", "get"),
        ("slides", "xml_presentation.slide_image", "list"),
        ("slides", "xml_presentations", "get"),
        ("task", "custom_fields", "get"),
        ("task", "custom_fields", "list"),
        ("task", "sections", "get"),
        ("task", "sections", "list"),
        ("task", "sections", "tasks"),
        ("task", "subtasks", "list"),
        ("task", "tasklists", "get"),
        ("task", "tasklists", "list"),
        ("task", "tasklists", "tasks"),
        ("task", "tasks", "get"),
        ("task", "tasks", "list"),
        ("vc", "meeting", "get"),
        ("wiki", "members", "list"),
        ("wiki", "nodes", "list"),
        ("wiki", "spaces", "get"),
        ("wiki", "spaces", "get_node"),
        ("wiki", "spaces", "list"),
    }
)
_STRICT_READ_SHORTCUTS = frozenset(
    {
        ("application", "+slash-command-list"),
        ("apps", "+access-scope-get"),
        ("apps", "+analytics-list"),
        ("apps", "+automation-get"),
        ("apps", "+automation-list"),
        ("apps", "+cache-get"),
        ("apps", "+db-audit-list"),
        ("apps", "+db-audit-status"),
        ("apps", "+db-changelog-list"),
        ("apps", "+db-data-export"),
        ("apps", "+db-env-diff"),
        ("apps", "+db-quota-get"),
        ("apps", "+db-recovery-diff"),
        ("apps", "+db-sync-get"),
        ("apps", "+db-sync-list"),
        ("apps", "+db-table-get"),
        ("apps", "+db-table-list"),
        ("apps", "+env-list"),
        ("apps", "+file-download"),
        ("apps", "+file-get"),
        ("apps", "+file-list"),
        ("apps", "+file-quota-get"),
        ("apps", "+file-sign"),
        ("apps", "+get"),
        ("apps", "+git-credential-list"),
        ("apps", "+list"),
        ("apps", "+log-get"),
        ("apps", "+log-list"),
        ("apps", "+member-list"),
        ("apps", "+member-settings-get"),
        ("apps", "+metric-list"),
        ("apps", "+openapi-key-get"),
        ("apps", "+openapi-key-list"),
        ("apps", "+plugin-list"),
        ("apps", "+release-get"),
        ("apps", "+release-list"),
        ("apps", "+role-get"),
        ("apps", "+role-list"),
        ("apps", "+role-match-list"),
        ("apps", "+role-member-list"),
        ("apps", "+session-get"),
        ("apps", "+session-list"),
        ("apps", "+session-messages-list"),
        ("apps", "+trace-get"),
        ("apps", "+trace-list"),
        ("apps", "+user-id-convert"),
        ("base", "+base-block-list"),
        ("base", "+base-get"),
        ("base", "+dashboard-block-get"),
        ("base", "+dashboard-block-get-data"),
        ("base", "+dashboard-block-list"),
        ("base", "+dashboard-get"),
        ("base", "+dashboard-list"),
        ("base", "+data-query"),
        ("base", "+field-get"),
        ("base", "+field-list"),
        ("base", "+field-search-options"),
        ("base", "+form-detail"),
        ("base", "+form-get"),
        ("base", "+form-list"),
        ("base", "+form-questions-list"),
        ("base", "+record-download-attachment"),
        ("base", "+record-get"),
        ("base", "+record-history-list"),
        ("base", "+record-list"),
        ("base", "+record-search"),
        ("base", "+role-get"),
        ("base", "+role-list"),
        ("base", "+table-copy-status"),
        ("base", "+table-get"),
        ("base", "+table-list"),
        ("base", "+title-resolve"),
        ("base", "+url-resolve"),
        ("base", "+view-get"),
        ("base", "+view-get-card"),
        ("base", "+view-get-filter"),
        ("base", "+view-get-group"),
        ("base", "+view-get-sort"),
        ("base", "+view-get-timebar"),
        ("base", "+view-get-visible-fields"),
        ("base", "+view-list"),
        ("base", "+workflow-get"),
        ("base", "+workflow-list"),
        ("calendar", "+agenda"),
        ("calendar", "+freebusy"),
        ("calendar", "+get"),
        ("calendar", "+meeting"),
        ("calendar", "+room-find"),
        ("calendar", "+search-event"),
        ("calendar", "+suggestion"),
        ("contact", "+get-user"),
        ("contact", "+search-bot"),
        ("contact", "+search-user"),
        ("docs", "+fetch"),
        ("docs", "+history-list"),
        ("docs", "+history-revert-status"),
        ("docs", "+media-download"),
        ("docs", "+media-preview"),
        ("docs", "+resource-download"),
        ("docs", "+script"),
        ("docs", "+search"),
        ("drive", "+batch-query-comments"),
        ("drive", "+cover"),
        ("drive", "+download"),
        ("drive", "+export"),
        ("drive", "+export-download"),
        ("drive", "+inspect"),
        ("drive", "+list-comments"),
        ("drive", "+list-replies"),
        ("drive", "+member-list"),
        ("drive", "+permission-get-setting"),
        ("drive", "+preview"),
        ("drive", "+search"),
        ("drive", "+secure-label-list"),
        ("drive", "+status"),
        ("drive", "+task_result"),
        ("drive", "+version-get"),
        ("drive", "+version-history"),
        ("im", "+chat-list"),
        ("im", "+chat-members-list"),
        ("im", "+chat-messages-list"),
        ("im", "+chat-search"),
        ("im", "+feed-group-list"),
        ("im", "+feed-group-list-item"),
        ("im", "+feed-group-query-item"),
        ("im", "+feed-shortcut-list"),
        ("im", "+flag-list"),
        ("im", "+messages-mget"),
        ("im", "+messages-search"),
        ("im", "+threads-messages-list"),
        ("mail", "+lint-html"),
        ("mail", "+message"),
        ("mail", "+messages"),
        ("mail", "+signature"),
        ("mail", "+thread"),
        ("mail", "+triage"),
        ("mail", "+watch"),
        ("markdown", "+diff"),
        ("markdown", "+fetch"),
        ("minutes", "+detail"),
        ("minutes", "+download"),
        ("minutes", "+search"),
        ("note", "+detail"),
        ("note", "+transcript"),
        ("okr", "+cycle-detail"),
        ("okr", "+cycle-list"),
        ("okr", "+progress-get"),
        ("okr", "+progress-list"),
        ("sheets", "+cells-get"),
        ("sheets", "+cells-search"),
        ("sheets", "+changeset-get"),
        ("sheets", "+chart-list"),
        ("sheets", "+cond-format-list"),
        ("sheets", "+csv-get"),
        ("sheets", "+dropdown-get"),
        ("sheets", "+filter-list"),
        ("sheets", "+filter-view-list"),
        ("sheets", "+float-image-list"),
        ("sheets", "+formula-verify"),
        ("sheets", "+history-list"),
        ("sheets", "+history-revert-status"),
        ("sheets", "+pivot-list"),
        ("sheets", "+revision-get"),
        ("sheets", "+sheet-info"),
        ("sheets", "+sparkline-list"),
        ("sheets", "+table-get"),
        ("sheets", "+workbook-export"),
        ("sheets", "+workbook-info"),
        ("slides", "+history-list"),
        ("slides", "+history-revert-status"),
        ("slides", "+screenshot"),
        ("slides", "+xml-get"),
        ("task", "+get-my-tasks"),
        ("task", "+get-related-tasks"),
        ("task", "+search"),
        ("task", "+tasklist-search"),
        ("vc", "+detail"),
        ("vc", "+meeting-events"),
        ("vc", "+meeting-list-active"),
        ("vc", "+recording"),
        ("vc", "+search"),
        ("whiteboard", "+export"),
        ("wiki", "+member-list"),
        ("wiki", "+node-get"),
        ("wiki", "+node-list"),
        ("wiki", "+space-list"),
    }
)
_RESUMABLE_SHORTCUT_WRITES = frozenset(
    {
        ("im", "+messages-send"),
        ("im", "+messages-reply"),
    }
)

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
    # HERMES_LARK_CLI_AUTHORIZED is deliberately absent: the grant must be
    # minted from THIS dispatch's run token (set explicitly at the two spawn
    # sites), never inherited from a stale/dirty ambient environment.
    "HERMES_LARK_CLI_RUN_TOKEN",
    "HERMES_LARK_CLI_REAL_BIN",
    "CODEX_HOME",
    "HERMES_CODEX_PLUGIN_SOURCE",
    "HERMES_TRUSTED_FEISHU_TOOL_SCOPE",
    "HERMES_TRUSTED_FEISHU_CHAT_TYPE",
    "HERMES_TRUSTED_FEISHU_CHAT_FENCE",
    "HERMES_MULTITENANCY_FEISHU_EXPERT_READONLY",
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
    "XDG_STATE_HOME",
}


def _failure_fields(
    *,
    exit_code: int | None = None,
    stderr: str = "",
    business_payload: dict[str, Any] | None = None,
    failure_hint: str | None = None,
    timed_out: bool = False,
) -> dict[str, str | bool | None]:
    classified = classify_connector_failure(
        "lark-cli",
        exit_code=exit_code,
        stderr=stderr,
        business_payload=business_payload,
        failure_hint=failure_hint,
        timed_out=timed_out,
    )
    return {
        key: classified[key]
        for key in ("failure_subsystem", "error_code", "retryable")
    }


def _classified_tool_error(message: str, *, failure_hint: str, **kwargs: Any) -> str:
    return tool_error(message, **kwargs, **_failure_fields(failure_hint=failure_hint))


def _operation_result(result: Any, receipt: dict[str, str]) -> str:
    if isinstance(result, dict):
        payload = dict(result)
    else:
        try:
            payload = json.loads(str(result))
        except (TypeError, json.JSONDecodeError):
            payload = {"ok": False, "error": "lark-cli returned an invalid host result"}
    if not isinstance(payload, dict):
        payload = {"ok": False, "error": "lark-cli returned an invalid host result"}
    payload[_OPERATION_FIELD] = receipt
    return json.dumps(payload, ensure_ascii=False)


def _operation_store_path(env: dict[str, str]) -> Path | None:
    state_home = str(env.get("XDG_STATE_HOME") or "").strip()
    if state_home:
        return Path(state_home).expanduser() / _OPERATION_DB_NAME
    profile_home = str(env.get("HERMES_HOME") or "").strip()
    if profile_home:
        return Path(profile_home).expanduser() / "state" / _OPERATION_DB_NAME
    return None


def _command_verb(argv: list[str]) -> str:
    words = []
    for item in argv:
        if item.startswith("-"):
            break
        if item:
            words.append(item)
    if not words:
        return ""
    return words[-1].lstrip("+").replace("_", "-").rsplit("-", 1)[-1].lower()


def _strict_operation_kind(mode: str, argv: list[str]) -> str:
    """Classify from the executable request, never the model-declared risk."""
    if mode == "api":
        request = _api_request_from_argv(argv)
        if request is None:
            return "unknown"
        return "read" if request[0] == "GET" else "write"
    if not argv or any(item in {"--help", "-h", "help"} for item in argv):
        return "read"
    if mode == "schema":
        if tuple(argv[:3]) in _STRICT_READ_SCHEMA_METHODS:
            return "read"
        return "write" if _command_verb(argv) in _STRICT_WRITE_VERBS else "unknown"
    if argv[0] in {"auth", "doctor", "schema"}:
        return "read"
    shortcut = _shortcut_prefix(argv)
    if shortcut in _RESUMABLE_SHORTCUT_WRITES:
        return "write"
    if shortcut == ("im", "+messages-resources-download"):
        return "write"
    if shortcut in _STRICT_READ_SHORTCUTS:
        return "read"
    verb = _command_verb(argv)
    if verb in _STRICT_WRITE_VERBS:
        return "write"
    return "unknown"


def _is_mutating_operation(mode: str, argv: list[str], risk: str) -> bool:
    if mode == "api":
        request = _api_request_from_argv(argv)
        if request is not None:
            return request[0] != "GET"
    kind = _strict_operation_kind(mode, argv)
    return kind == "write" or (kind == "unknown" and risk in {"write", "admin"})


def _operation_not_resumable_error(mode: str, argv: list[str]) -> str:
    return tool_error(
        "lark-cli command is not in the strict typed allowlist",
        ok=False,
        retryable=False,
        error_code="FEISHU_OPERATION_NOT_RESUMABLE",
        failure_subsystem="lark_api",
        mode=mode,
        command=argv,
    )


def _server_idempotency_key(
    *,
    profile_name: str,
    subject: str,
    session_id: str,
    tool_call_id: str,
) -> str:
    return "hm_" + hashlib.sha256(
        "\0".join((profile_name, subject, session_id, tool_call_id)).encode("utf-8")
    ).hexdigest()[:40]


def _prepare_resumable_write(
    *,
    env: dict[str, str],
    mode: str,
    argv: list[str],
    session_id: str,
    tool_call_id: str,
) -> tuple[list[str], str | None, str | None]:
    """Return connector argv + opaque intent, or a fail-closed error."""
    kind = _strict_operation_kind(mode, argv)
    if kind == "read":
        return argv, None, None
    if _shortcut_prefix(argv) not in _RESUMABLE_SHORTCUT_WRITES:
        return argv, None, _operation_not_resumable_error(mode, argv)
    if _message_write_descriptor(argv) is None:
        return argv, None, _operation_not_resumable_error(mode, argv)
    profile_name = str(env.get("HERMES_PROFILE") or "").strip()
    subject = str(env.get("HERMES_FEISHU_USER_OPEN_ID") or "").strip()
    session_id = str(session_id or "").strip()
    tool_call_id = str(tool_call_id or "").strip()
    if not profile_name or not subject or not session_id or not tool_call_id:
        return argv, None, _classified_tool_error(
            "resumable lark-cli write requires actor, session, and tool call identity",
            failure_hint="identity_unbound",
        )
    key = _server_idempotency_key(
        profile_name=profile_name,
        subject=subject,
        session_id=session_id,
        tool_call_id=tool_call_id,
    )
    clean = []
    skip = False
    for item in argv:
        if skip:
            skip = False
            continue
        if item == "--idempotency-key":
            skip = True
            continue
        if item.startswith("--idempotency-key="):
            continue
        clean.append(item)
    return [*clean, "--idempotency-key", key], f"call:{session_id}:{tool_call_id}", None


def _begin_lark_cli_operation(
    *,
    env: dict[str, str],
    mode: str,
    argv: list[str],
    identity: str,
    risk: str,
    task_id: str,
    intent_key: str | None = None,
) -> tuple[dict[str, str] | None, str | None]:
    """Claim one exact host step; never infer whether an uncertain write landed."""
    if not strict_context_enabled() or not _is_mutating_operation(mode, argv, risk):
        return None, None
    profile_name = str(env.get("HERMES_PROFILE") or "").strip()
    subject = str(env.get("HERMES_FEISHU_USER_OPEN_ID") or "").strip()
    db_path = _operation_store_path(env)
    task_id = str(task_id or "").strip()
    if not profile_name or not subject or db_path is None or not task_id:
        return None, _classified_tool_error(
            "durable lark-cli write requires an actor-bound task context",
            failure_hint="identity_unbound",
        )

    if not intent_key or not intent_key.startswith("call:"):
        return None, _classified_tool_error(
            "durable lark-cli write requires a connector-owned call identity",
            failure_hint="identity_unbound",
        )
    from .operation_checkpoint import OperationCheckpointStore

    store = OperationCheckpointStore(db_path)
    try:
        session_ref = None
        call_ref = None
        if intent_key.startswith("call:"):
            _prefix, session_ref, call_ref = intent_key.split(":", 2)
        row, created = store.claim(
            profile_name=profile_name,
            subject=subject,
            connector="lark-cli",
            intent_key=intent_key,
            step="execute",
            session_ref=session_ref,
            call_ref=call_ref,
            tool_scope=str(env.get("HERMES_TRUSTED_FEISHU_TOOL_SCOPE") or "").strip() or None,
            chat_type=str(env.get("HERMES_TRUSTED_FEISHU_CHAT_TYPE") or "").strip() or None,
            chat_fence=str(env.get("HERMES_TRUSTED_FEISHU_CHAT_FENCE") or "").strip() or None,
        )
        if (
            not created
            and intent_key.startswith("call:")
            and row["state"] == "confirmed"
            and row.get("result_ref")
        ):
            receipt = {
                "operation_id": str(row["operation_id"]),
                "state": "confirmed",
                "step": str(row["step"]),
            }
            return receipt, _operation_result(
                tool_result(
                    ok=True,
                    approval_required=False,
                    mode=mode,
                    identity=identity,
                    recovered=True,
                    json={"code": 0, "data": {"message_id": row["result_ref"]}},
                    failure_subsystem=None,
                    error_code=None,
                    retryable=False,
                ),
                receipt,
            )
    finally:
        store.close()
    receipt = {
        "operation_id": str(row["operation_id"]),
        "state": str(row["state"]),
        "step": str(row["step"]),
    }
    if created:
        return receipt, None
    return receipt, _operation_result(
        tool_error(
            "previous lark-cli write outcome is not confirmed; automatic replay blocked",
            ok=False,
            retryable=False,
            error_code="FEISHU_OPERATION_OUTCOME_UNCERTAIN",
            failure_subsystem="lark_api",
            mode=mode,
            identity=identity,
        ),
        receipt,
    )


def post_lark_cli_operation(*, tool_name: str, result: Any, **_kwargs: Any) -> None:
    """Persist the wrapper-owned receipt after real registry dispatch."""
    if tool_name != "lark_cli":
        return
    try:
        payload = result if isinstance(result, dict) else json.loads(str(result))
    except (TypeError, json.JSONDecodeError):
        return
    receipt = payload.get(_OPERATION_FIELD) if isinstance(payload, dict) else None
    if not isinstance(receipt, dict) or receipt.get("state") not in {"confirmed", "uncertain", "waiting_auth"}:
        return
    env = _safe_env()
    profile_name = str(env.get("HERMES_PROFILE") or "").strip()
    subject = str(env.get("HERMES_FEISHU_USER_OPEN_ID") or "").strip()
    db_path = _operation_store_path(env)
    operation_id = str(receipt.get("operation_id") or "").strip()
    if not profile_name or not subject or db_path is None or not operation_id:
        return
    from .operation_checkpoint import OperationCheckpointStore

    store = OperationCheckpointStore(db_path)
    try:
        store.transition(
            operation_id,
            profile_name=profile_name,
            subject=subject,
            expected_state="running",
            state=str(receipt["state"]),
            step=str(receipt.get("step") or "execute"),
            result_ref=str(receipt.get("result_ref") or "") or None,
        )
    finally:
        store.close()


def _remove_operation_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _remove_operation_fields(item)
            for key, item in value.items()
            if key != _OPERATION_FIELD
        }
    if isinstance(value, list):
        return [_remove_operation_fields(item) for item in value]
    return value


def transform_lark_cli_operation_result(*, tool_name: str, result: Any, **_kwargs: Any) -> str | None:
    """Remove host-only operation metadata before the model sees the result."""
    if tool_name != "lark_cli":
        return None
    try:
        payload = result if isinstance(result, dict) else json.loads(str(result))
        cleaned = _remove_operation_fields(payload)
        if cleaned == payload:
            return None
        return json.dumps(cleaned, ensure_ascii=False)
    except (TypeError, ValueError, RecursionError):
        # The host treats transform errors as fail-open, so keep this boundary total.
        return json.dumps(
            {
                "ok": False,
                "error": "lark-cli result unavailable",
                "error_code": "FEISHU_OPERATION_RESULT_UNAVAILABLE",
                "retryable": False,
            },
            ensure_ascii=False,
        )


def _redact(text: str) -> str:
    redacted = text or ""
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(lambda match: f"{match.group(1)}***REDACTED***", redacted)
    return redacted


def _strip_non_business_notices(text: str) -> str:
    cleaned = text or ""
    for pattern in _NON_BUSINESS_NOTICE_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    return sanitize_user_visible_output(cleaned).strip()


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


def _requested_json_format(argv: list[str]) -> bool:
    for idx, item in enumerate(argv):
        if item == "--format":
            if idx + 1 < len(argv) and argv[idx + 1].strip().lower() == "json":
                return True
        elif item.startswith("--format="):
            if item.split("=", 1)[1].strip().lower() == "json":
                return True
    return False


def _is_help_command(argv: list[str]) -> bool:
    if not argv:
        return False
    if argv[0] == "help":
        return True
    return any(
        item in {"--help", "-h"}
        and (idx == 0 or not (argv[idx - 1].startswith("-") and "=" not in argv[idx - 1]))
        for idx, item in enumerate(argv)
    )


def _control_or_diagnostic_kind(argv: list[str]) -> str:
    if _is_help_command(argv):
        return "help"
    if not argv:
        return ""
    if argv[0] in {"schema", "doctor", "whoami"}:
        return argv[0]
    return {
        ("auth", "status"): "auth_status",
        ("auth", "list"): "auth_list",
        ("auth", "check"): "auth_check",
        ("skills", "list"): "skills_list",
        ("skills", "read"): "skills_read",
        ("config", "show"): "config_show",
        ("config", "default-as"): "config_default_as",
        ("profile", "list"): "profile_list",
    }.get(tuple(argv[:2]), "")


def _is_control_or_diagnostic_command(argv: list[str]) -> bool:
    return bool(_control_or_diagnostic_kind(argv))


def _argv_with_json_format(argv: list[str], mode: str = "api", risk: str = "") -> list[str]:
    needs_json = mode == "api" or (mode == "shortcut" and risk == "read")
    if _has_format_flag(argv) or not needs_json or _is_control_or_diagnostic_command(argv):
        return argv
    return [*argv, "--format", "json"]


def _read_projection_hides_protocol(argv: list[str]) -> bool:
    if any(item.startswith("-q") or item == "--jq" or item.startswith("--jq=") for item in argv):
        return True
    for idx, item in enumerate(argv):
        if item == "--format" and idx + 1 < len(argv):
            return argv[idx + 1].strip().lower() in {"table", "csv", "ndjson"}
        if item.startswith("--format="):
            return item.split("=", 1)[1].strip().lower() in {"table", "csv", "ndjson"}
    return False


def _supports_identity_flag(argv: list[str], mode: str) -> bool:
    if not argv:
        return False
    if argv[0] in {"auth", "doctor", "schema"}:
        return False
    if _control_or_diagnostic_kind(argv) in {
        "help",
        "auth_status",
        "auth_list",
        "auth_check",
        "skills_list",
        "skills_read",
        "config_show",
        "config_default_as",
        "profile_list",
        "doctor",
        "schema",
    }:
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
# ponytail: fail-closed — actual keep-resource-delivery endpoints await sunke/plugin confirmation to narrow; anything not listed is intentionally denied.
_READONLY_API_READ_PREFIXES: tuple[str, ...] = (
    *_IM_READ_API_PREFIXES,
    "/open-apis/contact/v3/users",
    "/open-apis/contact/v3/departments",
    "/open-apis/contact/v3/scopes",
    "/open-apis/bitable/v1/apps",
    "/open-apis/sheets/v3/spreadsheets",
    "/open-apis/sheets/v2/spreadsheets",
    "/open-apis/drive/v1/files",
    "/open-apis/drive/v1/metas",
    "/open-apis/wiki/v2/spaces",
    "/open-apis/docx/v1/documents",
)
_READONLY_READ_SHORTCUTS = frozenset(
    {
        *_PERSONAL_IM_READ_SHORTCUTS,
        ("contact", "+users-get"),
        ("contact", "+users-batch"),
        ("contact", "+departments-get"),
        ("contact", "+search-user"),
    }
)


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
        and str(env.get("LARKSUITE_CLI_APP_ID") or "").strip()
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


def _feishu_expert_readonly_enabled() -> bool:
    return str(os.getenv("HERMES_MULTITENANCY_FEISHU_EXPERT_READONLY") or "").strip() == "1"


def _readonly_lark_cli_error(mode: str, argv: list[str], risk: str) -> str | None:
    if not _feishu_expert_readonly_enabled():
        return None
    if risk != "read":
        return "read-only lark-cli denied non-read risk"
    if mode == "api":
        request = _api_request_from_argv(argv)
        if not request:
            return "read-only lark-cli denied malformed api command"
        method, path = request
        if method != "GET":
            return "read-only lark-cli denied mutating OpenAPI method"
        if "/admin/" in path or "export" in path.strip("/").split("/"):
            return "read-only lark-cli denied OpenAPI path outside the read allowlist"
        if path.startswith(_READONLY_API_READ_PREFIXES):
            return None
        return "read-only lark-cli denied OpenAPI path outside the read allowlist"
    if _is_im_read_request(mode, argv) or _shortcut_prefix(argv) in _READONLY_READ_SHORTCUTS:
        return None
    return "read-only lark-cli denied command outside the read allowlist"


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
    # lark-cli embeds an `_notice` update block inside its JSON stdout when its
    # notifiers are on; suppress at the source so payloads stay pure business.
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
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
    if not _broker_proxy_configured(env):
        return "lark-cli auth broker is unavailable in the current profile runtime"
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


def _typed_resumable_result_ref(mode: str, argv: list[str], parsed: Any) -> str:
    if _shortcut_prefix(argv) not in _RESUMABLE_SHORTCUT_WRITES:
        return ""
    if not isinstance(parsed, dict):
        return ""
    data = parsed.get("data")
    if not isinstance(data, dict):
        data = parsed
    value = str(data.get("message_id") or "").strip()
    return value if 0 < len(value) <= 240 else ""


def _content_fingerprint(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _message_write_descriptor(argv: list[str]) -> dict[str, str] | None:
    shortcut = _shortcut_prefix(argv)
    if shortcut not in _RESUMABLE_SHORTCUT_WRITES:
        return None
    if shortcut == ("im", "+messages-send"):
        target_kind = "chat_id"
        target = _argv_option_value(argv, "--chat-id")
    else:
        target_kind = "parent_id"
        target = _argv_option_value(argv, "--message-id")
    if not target:
        return None

    text = _argv_option_value(argv, "--text")
    raw_content = _argv_option_value(argv, "--content")
    if text:
        msg_type = "text"
        content: Any = {"text": text}
    elif raw_content:
        try:
            content = json.loads(raw_content)
        except json.JSONDecodeError:
            return None
        msg_type = _argv_option_value(argv, "--msg-type") or "text"
    else:
        return None
    return {
        "target_kind": target_kind,
        "target": target,
        "msg_type": msg_type,
        "content_fp": _content_fingerprint(content),
    }


def _message_rows(parsed: Any) -> list[dict[str, Any]]:
    if not isinstance(parsed, dict):
        return []
    container = parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed
    rows = (
        container.get("items") or container.get("messages")
        if isinstance(container, dict)
        else None
    )
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _readback_resumable_message(
    *,
    binary: str,
    env: dict[str, str],
    cwd: str | None,
    timeout: int,
    identity: str,
    argv: list[str],
    message_id: str,
) -> str | None:
    descriptor = _message_write_descriptor(argv)
    if descriptor is None:
        return "FEISHU_OPERATION_READBACK_UNAVAILABLE"
    command = [
        binary,
        "api",
        "GET",
        "/open-apis/im/v1/messages/mget",
        "--params",
        json.dumps({"message_ids": message_id}, separators=(",", ":")),
        "--format",
        "json",
    ]
    if identity in {"user", "bot"}:
        command.extend(["--as", identity])
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
    except (subprocess.TimeoutExpired, PermissionError, OSError):
        return "FEISHU_OPERATION_READBACK_UNAVAILABLE"
    parsed = _parse_json_output(completed.stdout)
    if completed.returncode != 0 or parsed is None:
        return "FEISHU_OPERATION_READBACK_UNAVAILABLE"
    if _failure_fields(
        exit_code=completed.returncode,
        stderr=_redact(completed.stderr),
        business_payload=parsed,
    )["error_code"] is not None:
        return "FEISHU_OPERATION_READBACK_UNAVAILABLE"
    rows = [row for row in _message_rows(parsed) if str(row.get("message_id") or "") == message_id]
    if len(rows) != 1:
        return "FEISHU_OPERATION_READBACK_MISMATCH"
    row = rows[0]
    if descriptor["target_kind"] == "chat_id":
        target_matches = str(row.get("chat_id") or "") == descriptor["target"]
    else:
        target_matches = descriptor["target"] in {
            str(row.get("parent_id") or ""),
            str(row.get("root_id") or ""),
        }
    if (
        not target_matches
        or str(row.get("msg_type") or "") != descriptor["msg_type"]
        or _content_fingerprint(row.get("content")) != descriptor["content_fp"]
    ):
        return "FEISHU_OPERATION_READBACK_MISMATCH"
    return None


_READ_CURSOR_KEYS = ("page_token", "next_page_token", "cursor", "next_cursor")
_MAX_RECURSIVE_READ_REQUESTS = 1000


def _pagination_nodes(value: Any):
    if not isinstance(value, dict):
        return
    if isinstance(value.get("has_more"), bool):
        yield value
    data = value.get("data")
    if isinstance(data, dict) and isinstance(data.get("has_more"), bool):
        yield data


def _requested_read_cursors(argv: list[str]) -> set[str]:
    params = {**_argv_path_query_params(argv), **_argv_params_option(argv)}
    direct = _argv_option_value(argv, "--page-token")
    return {
        str(value).strip()
        for value in (direct, *(params.get(key) for key in _READ_CURSOR_KEYS))
        if str(value or "").strip()
    }


def _read_terminal_state(parsed: Any, argv: list[str]) -> tuple[bool, str | None, str | None]:
    pending = [node for node in _pagination_nodes(parsed) if node["has_more"]]
    if not pending:
        return True, None, None

    requested = _requested_read_cursors(argv)
    cursors_by_node = [
        [
            str(node.get(key) or "").strip()
            for key in _READ_CURSOR_KEYS
            if str(node.get(key) or "").strip()
        ]
        for node in pending
    ]
    if any(not cursors for cursors in cursors_by_node):
        return False, "FEISHU_READ_CURSOR_MISSING", "cursor_missing"
    cursors = [cursor for node_cursors in cursors_by_node for cursor in node_cursors]
    if requested.intersection(cursors):
        return False, "FEISHU_READ_CURSOR_LOOP", "cursor_loop"

    page_all = "--page-all" in argv
    page_limit = _argv_option_value(argv, "--page-limit")
    if page_all and page_limit != "0":
        return False, "FEISHU_READ_INCOMPLETE", "page_limit_reached"
    return False, "FEISHU_READ_INCOMPLETE", "pagination_remaining"


def _without_argv_options(argv: list[str], names: frozenset[str]) -> list[str]:
    cleaned: list[str] = []
    skip_value = False
    for item in argv:
        if skip_value:
            skip_value = False
            continue
        if item in names:
            skip_value = item not in {"--page-all"}
            continue
        if any(item.startswith(name + "=") for name in names):
            continue
        cleaned.append(item)
    return cleaned


def _tool_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        payload = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _recursive_read_failure(
    result: dict[str, Any],
    *,
    error_code: str,
    reason: str,
    requests: int,
    pending: int,
) -> str:
    result.update(
        ok=False,
        read_complete=False,
        failure_subsystem="lark_api",
        error_code=error_code,
        retryable=False,
        read_incomplete_reason=reason,
        read_requests=requests,
        read_pending_count=max(1, pending),
    )
    return tool_result(**result)


def _bounded_recursive_wiki_read(args: dict[str, Any], argv: list[str]) -> str:
    if _shortcut_prefix(argv) != ("wiki", "+node-list"):
        return _classified_tool_error(
            "recursive_read currently requires shortcut wiki +node-list",
            failure_hint="request_invalid",
        )
    if _argv_option_value(argv, "--page-token"):
        return _classified_tool_error(
            "recursive_read must start before the first page",
            failure_hint="request_invalid",
        )
    try:
        limit = int(args.get("recursive_read_limit") or 100)
    except (TypeError, ValueError):
        limit = 0
    if not 1 <= limit <= _MAX_RECURSIVE_READ_REQUESTS:
        return _classified_tool_error(
            f"recursive_read_limit must be between 1 and {_MAX_RECURSIVE_READ_REQUESTS}",
            failure_hint="request_invalid",
        )

    root_parent = _argv_option_value(argv, "--parent-node-token")
    base_argv = _without_argv_options(
        argv,
        frozenset({"--page-all", "--page-limit", "--page-token", "--parent-node-token"}),
    )
    queue: list[tuple[str, str]] = [(root_parent, "")]
    queued = set(queue)
    visited_pages: set[tuple[str, str]] = set()
    seen_nodes = {root_parent} if root_parent else set()
    nodes: list[dict[str, Any]] = []
    requests = 0
    final_result: dict[str, Any] = {}
    aggregate_json: dict[str, Any] = {}

    while queue:
        if requests >= limit:
            return _recursive_read_failure(
                final_result,
                error_code="FEISHU_READ_LIMIT_REACHED",
                reason="read_limit_reached",
                requests=requests,
                pending=len(queue),
            )

        parent_token, page_token = queue.pop(0)
        queued.discard((parent_token, page_token))
        page_key = (parent_token, page_token)
        if page_key in visited_pages:
            return _recursive_read_failure(
                final_result,
                error_code="FEISHU_READ_CURSOR_LOOP",
                reason="cursor_loop",
                requests=requests,
                pending=len(queue) + 1,
            )

        page_argv = list(base_argv)
        if parent_token:
            page_argv.extend(["--parent-node-token", parent_token])
        if page_token:
            page_argv.extend(["--page-token", page_token])
        page_args = {**args, "argv": page_argv, "recursive_read": False}
        result = _tool_payload(_handle_lark_cli_execute(page_args))
        requests += 1
        final_result = result
        page_json = result.get("json")
        data = page_json.get("data") if isinstance(page_json, dict) else None
        page_nodes = data.get("nodes") if isinstance(data, dict) else None
        pagination_only = (
            result.get("error_code") == "FEISHU_READ_INCOMPLETE"
            and result.get("read_incomplete_reason") == "pagination_remaining"
        )
        if not (result.get("ok") is True or pagination_only):
            result.update(
                read_complete=False,
                read_requests=requests,
                read_pending_count=max(1, len(queue) + 1),
            )
            return tool_result(**result)
        if not isinstance(page_nodes, list) or not all(isinstance(node, dict) for node in page_nodes):
            return _recursive_read_failure(
                result,
                error_code="FEISHU_READ_SHAPE_INVALID",
                reason="read_shape_invalid",
                requests=requests,
                pending=len(queue) + 1,
            )

        if not aggregate_json:
            aggregate_json = dict(page_json)
        visited_pages.add(page_key)
        for node in page_nodes:
            token = str(node.get("node_token") or "").strip()
            if token and token in seen_nodes:
                return _recursive_read_failure(
                    result,
                    error_code="FEISHU_READ_NODE_LOOP",
                    reason="node_loop",
                    requests=requests,
                    pending=len(queue) + 1,
                )
            if token:
                seen_nodes.add(token)
            nodes.append(node)

        if data.get("has_more") is True:
            next_cursor = str(data.get("page_token") or data.get("next_page_token") or "").strip()
            next_page = (parent_token, next_cursor)
            if not next_cursor:
                return _recursive_read_failure(
                    result,
                    error_code="FEISHU_READ_CURSOR_MISSING",
                    reason="cursor_missing",
                    requests=requests,
                    pending=len(queue) + 1,
                )
            if next_page in visited_pages or next_page in queued:
                return _recursive_read_failure(
                    result,
                    error_code="FEISHU_READ_CURSOR_LOOP",
                    reason="cursor_loop",
                    requests=requests,
                    pending=len(queue) + 1,
                )
            queue.append(next_page)
            queued.add(next_page)

        for node in page_nodes:
            if node.get("has_child") is not True:
                continue
            child_token = str(node.get("node_token") or "").strip()
            if not child_token:
                return _recursive_read_failure(
                    result,
                    error_code="FEISHU_READ_NODE_TOKEN_MISSING",
                    reason="node_token_missing",
                    requests=requests,
                    pending=len(queue) + 1,
                )
            child_page = (child_token, "")
            if child_page in visited_pages or child_page in queued:
                return _recursive_read_failure(
                    result,
                    error_code="FEISHU_READ_NODE_LOOP",
                    reason="node_loop",
                    requests=requests,
                    pending=len(queue) + 1,
                )
            queue.append(child_page)
            queued.add(child_page)

    aggregate_data = dict(aggregate_json.get("data") or {})
    aggregate_data.update(nodes=nodes, has_more=False, page_token="")
    aggregate_json["data"] = aggregate_data
    final_result.update(
        ok=True,
        command=argv,
        json=aggregate_json,
        stdout="",
        failure_subsystem=None,
        error_code=None,
        retryable=False,
        read_complete=True,
        read_requests=requests,
        read_pending_count=0,
    )
    final_result.pop("read_incomplete_reason", None)
    return tool_result(**final_result)


SCRIPT_DEFAULT_TIMEOUT_SECONDS = 300
SCRIPT_MAX_TIMEOUT_SECONDS = 900
_SCRIPT_OUTPUT_CLIP_CHARS = 20_000


def _clip_output_tail(text: str, limit: int = _SCRIPT_OUTPUT_CLIP_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"…[clipped {len(text) - limit} chars]…" + text[-limit:]


def _kill_process_group(proc: "subprocess.Popen[Any]") -> None:
    """Kill the whole process group of a start_new_session child, then reap.

    Timeout kills only the direct child otherwise; descendants would inherit the
    authorization env and keep writing. A setsid-escaping grandchild can still
    leave the group — a known residual noted in the SPEC.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def _aidock_trusted_roots() -> list[Path]:
    """The read-only AiDock / plugin distribution roots, resolved.

    These are exactly the shared-skill roots the sandbox ``--ro-bind`` mounts
    (see ``_shared_skill_symlink_bwrap_args`` in agent_real/_core): AiDock
    SkillHub releases, shared skills, skill-releases, and expert-plugin managed
    sources. A script whose real (symlink-resolved) bytes live under one of
    these is AiDock-distributed and internally reviewed — sunke's standing
    directive is to trust and run it without content restriction. The profile
    skills dir itself is RW inside the sandbox, so a file that resolves to the
    profile tree (or workspace/tmp) is NOT trusted — that is the one boundary
    that keeps run-authored terminal code from borrowing the credential grant.
    """
    shared_raw = str(os.environ.get("HERMES_SHARED_HOME") or "").strip()
    if not shared_raw:
        return []
    shared = Path(shared_raw).expanduser()
    return [
        (shared / "skills").resolve(strict=False),
        (shared / "skill-releases").resolve(strict=False),
        (shared / "_managed" / "aidock-skillhub").resolve(strict=False),
        (shared / ".hermes-plugin-managed" / ".sources").resolve(strict=False),
    ]


def _resolve_skill_script(argv0: str, env: dict[str, str]) -> tuple[Path | None, str | None]:
    """Resolve a mode=script path fail-closed to an AiDock-distributed file.

    The single gate: the symlink-RESOLVED target must be a regular file under
    one of the read-only AiDock distribution roots. Naming a profile skills
    symlink (the normal case), or the shared target directly, both pass; a
    symlink or path that resolves into the RW profile tree, workspace, or tmp
    does not — so terminal-planted code cannot ride the channel. There is no
    content or extension restriction: whatever the plugin distributes runs
    (sunke 2026-08-31, twice — the source check IS the whole gate).
    """
    if _profile_home(env) is None:
        return None, "script channel requires a routed profile runtime"
    raw = str(argv0 or "").strip()
    if not raw:
        return None, "mode=script requires argv[0] to be the script path"
    candidate = Path(raw).expanduser()
    candidates = [candidate]
    if not candidate.is_absolute():
        workspace = _workspace_root(env)
        if workspace is None:
            return None, "script channel requires a workspace to resolve relative paths"
        # A relative path is tried against the workspace first (historical
        # contract), then against the skills roots — the tool schema tells the
        # model to name a script "relative to its SKILL.md", and the natural
        # spelling of that is `<skill>/scripts/x.py` (2026-09-04: kep-ub-gen
        # injection died twice on "script not found" for exactly that form).
        # The trusted-roots gate below is unchanged, so this widens only how a
        # name is looked up, never what is allowed to run.
        candidates = [workspace / candidate]
        profile_home = _profile_home(env)
        if profile_home is not None:
            candidates.append(profile_home / "skills" / candidate)
        shared_raw = str(env.get("HERMES_SHARED_HOME") or os.environ.get("HERMES_SHARED_HOME") or "").strip()
        if shared_raw:
            candidates.append(Path(shared_raw).expanduser() / "skills" / candidate)
    roots = _aidock_trusted_roots()
    if not roots:
        return None, "script channel cannot resolve the shared AiDock roots"
    resolved: Path | None = None
    for option in candidates:
        try:
            resolved = option.resolve(strict=True)
            break
        except (OSError, RuntimeError):
            continue
    if resolved is None:
        return None, "script not found"
    if not any(resolved == root or root in resolved.parents for root in roots):
        # Codex reads a hardened copy under CODEX_HOME. Map it back to the
        # actor-bound Plugin source and require byte equality.
        try:
            codex_home = Path(env["CODEX_HOME"]).expanduser().resolve(strict=True)
            plugin_source = (
                Path(env["HERMES_CODEX_PLUGIN_SOURCE"])
                .expanduser()
                .resolve(strict=True)
            )
            if resolved.is_relative_to(codex_home / "skills"):
                relative = resolved.relative_to(codex_home / "skills")
                mapped = plugin_source / "skills" / relative
            elif resolved.is_relative_to(codex_home / "plugins"):
                relative = resolved.relative_to(codex_home / "plugins")
                mapped = plugin_source.joinpath(*relative.parts[1:])
            else:
                raise ValueError("not a Codex Plugin copy")
            mapped = mapped.resolve(strict=True)
            if not any(mapped == root or root in mapped.parents for root in roots):
                raise ValueError("mapped source is outside trusted roots")
            if not mapped.is_file() or mapped.read_bytes() != resolved.read_bytes():
                raise ValueError("Codex Plugin copy differs from trusted source")
            resolved = mapped
        except (KeyError, OSError, RuntimeError, ValueError):
            return None, "script must be an AiDock-distributed skill script"
    if not resolved.is_file():
        return None, "script path is not a regular file"
    return resolved, None


def _handle_script_channel(*, argv: list[str], risk: str, timeout_raw: Any) -> str:
    env = _safe_env()
    runtime_error = _profile_runtime_error(env)
    if runtime_error:
        hint = "dependency_unavailable" if "broker is unavailable" in runtime_error else "permission_denied"
        return _classified_tool_error(runtime_error, failure_hint=hint, mode="script", command=argv, risk=risk)
    run_token = str(os.environ.get(HERMES_LARK_CLI_RUN_TOKEN) or "")
    if not run_token:
        # The run token is minted ONLY by subprocess_env's strict build path
        # (and stripped for local_harness), so its presence is the strict-
        # runtime proof. The worker's own environ does NOT carry
        # HERMES_MULTITENANCY_STRICT_CONTEXT (2026-08-31 production incident:
        # gating on strict_context_enabled() here rejected every call), so the
        # token is the one signal that survives into this process. Fail closed
        # without it — no shim, no sanctioned way to hand the grant out. The
        # same token-only rule now governs the AUTHORIZED passthrough for
        # non-script modes further down in _handle_lark_cli_execute.
        return _classified_tool_error(
            "script channel requires the strict profile runtime",
            failure_hint="dependency_unavailable",
            mode="script",
            command=argv,
            risk=risk,
        )
    script, script_error = _resolve_skill_script(argv[0], env)
    if script is None:
        return _classified_tool_error(
            script_error or "invalid skill script path",
            failure_hint="request_invalid",
            mode="script",
            command=argv,
            risk=risk,
        )

    # argv bounds: a dirty packaged script must not crash the worker with E2BIG.
    if len(argv) > 256 or any(len(a) > 8192 for a in argv):
        return _classified_tool_error(
            "script channel argv too large",
            failure_hint="request_invalid",
            mode="script",
            command=argv[:8],
            risk=risk,
        )

    # Audit is a required control here: a full/unwritable log must fail the call
    # closed rather than run a grant with no record. force=True writes even under
    # a default-off deploy; the return signals a real write failure.
    from .security_audit import append_security_event

    audited = append_security_event(
        event_type="lark_cli.script_channel.granted",
        force=True,
        profile=str(env.get("HERMES_PROFILE") or ""),
        command_name=script.name,
        path=str(script),
        command_hash=hashlib.sha256(script.read_bytes()).hexdigest()[:16],
        reason=f"skill script exec: {script.name} args={len(argv) - 1}",
    )
    if not audited:
        return _classified_tool_error(
            "script channel audit write failed; refusing to run ungranted",
            failure_hint="dependency_unavailable",
            mode="script",
            command=argv,
            risk=risk,
        )

    # P0-2: pin a trusted interpreter and a freshly-written shim, never a PATH
    # lookup. The profile tree is RW inside the sandbox, so terminal code could
    # plant a `python3`/`lark-cli` on any inherited PATH entry; sys.executable
    # lives in the ro venv, and we write our own shim (pointing at the ro
    # authsidecar binary) into a private 0700 dir used only for this call.
    shim_tmp = Path(tempfile.mkdtemp(prefix="lark-script-", dir=str(_profile_home(env) / "tmp")))
    real_bin = str(os.environ.get(HERMES_LARK_CLI_REAL_BIN) or "").strip()
    try:
        if real_bin:
            from .lark_cli_guard import install_lark_cli_shim

            install_lark_cli_shim(shim_tmp, real_binary=Path(real_bin))
        env["PATH"] = os.pathsep.join([str(shim_tmp), "/usr/bin", "/bin"])
        # The grant: lark-cli children pass the freshly-written shim. The
        # credential never enters this env — calls still proxy through the
        # authsidecar/broker with unchanged identity, host and risk policy.
        env[HERMES_LARK_CLI_AUTHORIZED] = run_token
        env.pop("CODEX_HOME", None)
        env.pop("HERMES_CODEX_PLUGIN_SOURCE", None)

        try:
            timeout = int(timeout_raw or SCRIPT_DEFAULT_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            timeout = SCRIPT_DEFAULT_TIMEOUT_SECONDS
        timeout = max(1, min(timeout, SCRIPT_MAX_TIMEOUT_SECONDS))
        workspace = _workspace_root(env)
        cwd = str(workspace) if workspace is not None and workspace.exists() else None

        # Interpreter dispatch: .py rides the trusted sys.executable (P0-2);
        # an executable file runs as shipped (its env-shebang can only search
        # the narrowed PATH above); anything else goes through /bin/bash.
        if script.suffix == ".py":
            cmd = [sys.executable, str(script), *argv[1:]]
        elif os.access(script, os.X_OK):
            cmd = [str(script), *argv[1:]]
        else:
            cmd = ["/bin/bash", str(script), *argv[1:]]

        # P0-3: run in a fresh process group and kill the whole tree on timeout,
        # so descendants can't keep holding the grant after the parent exits.
        try:
            proc = subprocess.Popen(
                cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                errors="replace",
                env=env,
                cwd=cwd,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            return _classified_tool_error(
                f"skill script failed to start: {_redact(str(exc))}",
                failure_hint="permission_denied",
                mode="script",
                command=argv,
                risk=risk,
            )
        try:
            raw_out, raw_err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            raw_out, raw_err = proc.communicate()
            return tool_error(
                f"skill script timed out after {timeout}s",
                mode="script",
                command=argv,
                risk=risk,
                stdout_redacted=_clip_output_tail(_redact(raw_out or "")),
                stderr_redacted=_clip_output_tail(_redact(raw_err or "")),
                **_failure_fields(timed_out=True),
            )
    finally:
        shutil.rmtree(shim_tmp, ignore_errors=True)

    completed = subprocess.CompletedProcess(argv, proc.returncode, raw_out, raw_err)
    stdout = _clip_output_tail(_redact(completed.stdout or ""))
    stderr = _clip_output_tail(_strip_non_business_notices(_redact(completed.stderr or "")))
    payload: dict[str, Any] = {
        "mode": "script",
        "script": script.name,
        "command": argv,
        "risk": risk,
        "exit_code": completed.returncode,
        "stdout_redacted": stdout,
        "stderr_redacted": stderr,
    }
    if completed.returncode != 0:
        return tool_error(
            f"skill script exited {completed.returncode}",
            **payload,
            **_failure_fields(exit_code=completed.returncode, stderr=stderr),
        )
    return tool_result(**payload)


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
                "enum": ["shortcut", "schema", "api", "script"],
                "description": (
                    "Command family: shortcut, schema method, raw OpenAPI api call, or "
                    "script (run any packaged skill script/executable that may call "
                    "lark-cli itself). MANDATORY: when an installed Skill/Plugin instructs "
                    "running any file it distributes (through any interpreter or direct "
                    "execution, in any subdirectory), resolve that path relative to its "
                    "SKILL.md and call mode=script; do not use terminal/execute_code."
                ),
            },
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "lark-cli arguments excluding the binary name. For mode=script: "
                    "[installed_script_path, ...script_args]; preserve the Skill command's "
                    "remaining arguments. The script must be a file this "
                    "profile's installed skills/plugins distribute (any type: .py, "
                    ".sh, executables)."
                ),
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
            "recursive_read": {
                "type": "boolean",
                "description": "Boundedly traverse every page and child for wiki +node-list.",
            },
            "recursive_read_limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_RECURSIVE_READ_REQUESTS,
                "description": "Maximum page/subtree requests before the recursive read fails closed.",
            },
        },
        "required": ["mode", "argv", "risk", "reason"],
    },
}


def _handle_lark_cli_execute(args: dict, **_kwargs: Any) -> str:
    mode = str(args.get("mode") or "").strip()
    risk = str(args.get("risk") or "read").strip()
    argv_raw = args.get("argv")
    if mode not in {"shortcut", "schema", "api", "script"}:
        return _classified_tool_error(
            "mode must be one of shortcut, schema, api, script",
            failure_hint="request_invalid",
        )
    if risk not in {"read", "write", "export", "admin"}:
        return _classified_tool_error(
            "risk must be one of read, write, export, admin",
            failure_hint="request_invalid",
        )
    if not isinstance(argv_raw, list) or not all(isinstance(item, str) and item for item in argv_raw):
        return _classified_tool_error(
            "argv must be a non-empty list of strings",
            failure_hint="request_invalid",
        )
    if mode == "script":
        # P0-1: a packaged script is a general write channel; a read-only expert
        # session must not reach it (the per-command readonly allowlist below is
        # bypassed by the early return, so gate it here).
        if _feishu_expert_readonly_enabled():
            return _classified_tool_error(
                "read-only lark-cli denied script execution",
                failure_hint="permission_denied",
                mode="script",
                command=[str(item) for item in argv_raw][:8],
                risk=risk,
            )
        return _handle_script_channel(
            argv=[str(item) for item in argv_raw],
            risk=risk,
            timeout_raw=args.get("timeout_seconds"),
        )
    if any(item == "--" for item in argv_raw):
        return _classified_tool_error(
            "argv must not contain raw -- separators",
            failure_hint="request_invalid",
        )

    argv = list(argv_raw)
    if mode == "api":
        api_req = _api_request_from_argv(argv)
        if not api_req:
            return _classified_tool_error(
                "api mode requires argv like ['api', '<METHOD>', '<PATH>'] or ['<METHOD>', '<PATH>']",
                failure_hint="request_invalid",
            )
        path_for_command = _normalise_openapi_path_with_query(_api_path_arg_from_argv(argv))
        argv = (
            ["api", api_req[0], path_for_command, *argv[3:]]
            if argv and argv[0] == "api"
            else ["api", api_req[0], path_for_command, *argv[2:]]
        )
    elif argv and argv[0] == "api":
        return _classified_tool_error("api command must use mode=api", failure_hint="request_invalid")

    if mode in {"shortcut", "schema"} and is_headless_oauth_attempt(argv):
        return tool_result(
            ok=False,
            error="Interactive lark-cli OAuth is disabled in headless runs.",
            mode=mode,
            command=argv,
            risk=risk,
            auth_required=True,
            auth_method="official",
            auth_hint="Use /feishu_auth in a Feishu DM or Lark-cli in WebUI Connectors.",
            failure_subsystem="credential",
            error_code="FEISHU_AUTH_INTERACTIVE_BLOCKED",
            retryable=False,
        )

    if args.get("recursive_read") is True:
        if mode != "shortcut" or risk != "read":
            return _classified_tool_error(
                "recursive_read requires shortcut mode with read risk",
                failure_hint="request_invalid",
            )
        return _bounded_recursive_wiki_read(args, argv)

    readonly_error = _readonly_lark_cli_error(mode, argv, risk)
    if readonly_error:
        return _classified_tool_error(
            readonly_error,
            failure_hint="permission_denied",
            mode=mode,
            command=argv,
            risk=risk,
        )

    decision = _policy_decision(mode, argv, risk)
    if not decision.get("allowed"):
        return _classified_tool_error(
            decision["reason"],
            failure_hint="permission_denied",
            mode=mode,
            command=argv,
            risk=risk,
        )

    binary = _resolve_binary()
    if not binary:
        return _classified_tool_error(
            "lark-cli binary not found; set HERMES_LARK_CLI_BIN or install lark-cli",
            failure_hint="dependency_unavailable",
        )

    env = _safe_env()
    run_token = str(os.environ.get(HERMES_LARK_CLI_RUN_TOKEN) or "")
    if run_token:
        # Token presence is the strict-runtime proof (same rule as the script
        # channel): it is minted only by subprocess_env's strict build path and
        # the worker env never carries HERMES_MULTITENANCY_STRICT_CONTEXT, so
        # gating on strict_context_enabled() here left descendants of sanctioned
        # tool dispatches shim-denied in production while tests (which set the
        # var) stayed green.
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
    command = [binary, *_argv_with_json_format(argv, mode, risk)]
    if not _has_identity_flag(command) and identity in {"user", "bot"} and _supports_identity_flag(argv, mode):
        command.extend(["--as", identity])

    timeout = min(int(args.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)
    runtime_error = _profile_runtime_error(env)
    if runtime_error:
        hint = "dependency_unavailable" if "broker is unavailable" in runtime_error else "permission_denied"
        return _classified_tool_error(
            runtime_error,
            failure_hint=hint,
            mode=mode,
            command=argv,
            risk=risk,
        )
    workspace = _workspace_root(env)
    output_paths, output_error = _extract_output_paths(command, workspace)
    if output_error:
        return _classified_tool_error(
            output_error,
            failure_hint="request_invalid",
            mode=mode,
            command=argv,
            risk=risk,
        )
    identity_error = _personal_user_write_identity_error(env, mode, argv, risk, identity, requested_identity)
    if identity_error:
        return _classified_tool_error(
            identity_error,
            failure_hint="identity_mismatch",
            mode=mode,
            command=argv,
            risk=risk,
            identity=identity,
        )
    im_read_error = _feishu_im_read_identity_error(env, mode, argv, risk, identity)
    if im_read_error:
        hint = "identity_unbound" if im_read_error == _PERSONAL_FEISHU_IM_USER_AUTH_REQUIRED else "permission_denied"
        return _classified_tool_error(
            im_read_error,
            failure_hint=hint,
            mode=mode,
            command=argv,
            risk=risk,
            identity=identity,
        )

    visible_argv = list(argv)
    operation_intent = None
    if strict_context_enabled():
        argv, operation_intent, resume_error = _prepare_resumable_write(
            env=env,
            mode=mode,
            argv=argv,
            session_id=str(_kwargs.get("session_id") or ""),
            tool_call_id=str(_kwargs.get("tool_call_id") or ""),
        )
        if resume_error is not None:
            return resume_error
        command = [binary, *_argv_with_json_format(argv, mode, risk)]
        if not _has_identity_flag(command) and identity in {"user", "bot"} and _supports_identity_flag(argv, mode):
            command.extend(["--as", identity])

    operation, operation_decision = _begin_lark_cli_operation(
        env=env,
        mode=mode,
        argv=argv,
        identity=identity,
        risk=risk,
        task_id=str(_kwargs.get("task_id") or ""),
        intent_key=operation_intent,
    )
    if operation_decision is not None:
        return operation_decision

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
        timeout_result = tool_error(
            f"lark-cli timed out after {timeout}s",
            mode=mode,
            command=argv,
            risk=risk,
            stdout_redacted=_redact(str(exc.output or "")),
            stderr_redacted=_redact(str(exc.stderr or "")),
            **_failure_fields(timed_out=True),
        )
        if operation is not None:
            operation = {**operation, "state": "uncertain"}
            return _operation_result(timeout_result, operation)
        return timeout_result
    except PermissionError as exc:
        permission_result = _classified_tool_error(
            f"lark-cli failed in profile sandbox: {_redact(str(exc))}",
            failure_hint="permission_denied",
            mode=mode,
            command=argv,
            risk=risk,
        )
        if operation is not None:
            operation = {**operation, "state": "uncertain"}
            return _operation_result(permission_result, operation)
        return permission_result

    stdout = _redact(completed.stdout)
    stderr = _strip_non_business_notices(_redact(completed.stderr))
    parsed = _parse_json_output(stdout)
    if parsed is None:
        # The notice-strip patterns delete whole lines; on valid JSON they cut
        # lark-cli's embedded `_notice` lines and leave a trailing comma, so
        # they may only run on output that already failed to parse as JSON.
        stdout = _strip_non_business_notices(stdout)
        parsed = _parse_json_output(stdout)
    if isinstance(parsed, dict):
        parsed.pop("_notice", None)
    json_parse_failed = (
        parsed is None
        and completed.returncode == 0
        and bool(stdout.strip())
        and _requested_json_format(command)
    )
    fields = _failure_fields(
        exit_code=completed.returncode,
        stderr=stderr,
        business_payload=parsed if isinstance(parsed, dict) else None,
        failure_hint="output_unparseable" if json_parse_failed else None,
    )
    typed_result_ref = _typed_resumable_result_ref(mode, argv, parsed)
    typed_receipt_missing = bool(
        operation_intent and fields["error_code"] is None and not typed_result_ref
    )
    readback_error = None
    if operation_intent and fields["error_code"] is None and typed_result_ref:
        readback_error = _readback_resumable_message(
            binary=binary,
            env=env,
            cwd=cwd,
            timeout=timeout,
            identity=identity,
            argv=argv,
            message_id=typed_result_ref,
        )
    if typed_receipt_missing:
        fields = {
            "failure_subsystem": "lark_api",
            "error_code": "FEISHU_OPERATION_OUTCOME_UNCERTAIN",
            "retryable": False,
        }
    elif readback_error:
        fields = {
            "failure_subsystem": "lark_api",
            "error_code": readback_error,
            "retryable": False,
        }
    # Live assertion for the run-scoped auth broker: a refused dial to our own
    # localhost proxy means the broker died before the run that owns it did.
    # Silent until it happens, greppable/alertable when it does — this counter
    # is what proves the fix is holding in production, and the first thing to
    # watch on rollback. The proxy URL is host:port only; the key stays
    # redacted by _SECRET_PATTERNS.
    if "connection refused" in (stderr or "").lower():
        logger.warning(
            "[multitenancy] lark_cli auth broker dial refused proxy=%s mode=%s command=%s "
            "— broker closed before its run finished",
            env.get("LARKSUITE_CLI_AUTH_PROXY") or "<unset>",
            mode,
            argv[:3],
        )
    result = {
        "ok": fields["error_code"] is None,
        "approval_required": False,
        "mode": mode,
        "identity": identity,
        "command": visible_argv,
        "exit_code": completed.returncode,
        "json": parsed,
        "stdout": stdout if parsed is None else "",
        "stderr_redacted": stderr,
        "files": _existing_output_files(output_paths),
        **fields,
    }
    if typed_receipt_missing:
        result["error"] = "lark-cli write returned no typed connector receipt"
    elif readback_error:
        result["error"] = "lark-cli write could not be independently read back"
    if risk == "read":
        unstructured_read = (
            not _is_control_or_diagnostic_command(argv)
            and (not isinstance(parsed, dict) or _read_projection_hides_protocol(argv))
        )
        if unstructured_read:
            read_complete, read_error_code, read_reason = (
                False,
                "FEISHU_OUTPUT_UNPARSEABLE",
                "output_unparseable",
            )
        else:
            read_complete, read_error_code, read_reason = _read_terminal_state(parsed, argv)
        result["read_complete"] = bool(result["ok"] and read_complete)
        if result["ok"] and not read_complete:
            result.update(
                ok=False,
                failure_subsystem="lark_api",
                error_code=read_error_code,
                retryable=False,
                read_incomplete_reason=read_reason,
            )
    if json_parse_failed:
        result["json_parse_failed"] = True
    result = annotate_permission_error(result, app_id=env.get("LARKSUITE_CLI_APP_ID"))
    rendered = tool_result(**result)
    if operation is None:
        return rendered
    operation_state = (
        "confirmed"
        if fields["error_code"] is None
        else "waiting_auth"
        if fields["error_code"] == "FEISHU_AUTH_REAUTH_REQUIRED"
        else "uncertain"
    )
    return _operation_result(
        rendered,
        {
            **operation,
            "state": operation_state,
            **({"result_ref": typed_result_ref} if operation_state == "confirmed" else {}),
        },
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
        # 30K chars: force large payloads (e.g. 64KB sheets +csv-get) through the
        # core hermes-results offload (preview + sandbox file path) instead of
        # inline context — the model must never copy big data into write_file args.
        max_result_size_chars=30_000,
        description="Official lark-cli bridge for Feishu/Lark OpenAPI",
        emoji="Lark",
    )
