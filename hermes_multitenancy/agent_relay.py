"""Identity-bound Feishu relay for local agents; no model or profile access."""
from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

from aiohttp.web import AppKey

from .agent_relay_store import RelayConflict, RelayStore, _now_ms, _request_hash, _sha
from .agent_relay_feishu import (
    FeishuApiError,
    FeishuRelayClient,
    _card_with_actions,
    _valid_actor_id,
)

logger = logging.getLogger(__name__)
MAX_CONTENT_BYTES = 30 * 1024
ADMIN_TOKEN_ENV = "HERMES_AGENT_RELAY_ADMIN_TOKEN"
ADMIN_MAX_WINDOW_MS = 7 * 86_400_000
ADMIN_LOG_LIMIT = 5000
_AUDIT_PREFIX = "relay_audit "
_LOG_FIELD_RE = re.compile(r"\b(event|status|actor|card|msg)=(\S+)")
RELAY_STORE_KEY = AppKey("relay_store", object)
RELAY_EVENTS_KEY = AppKey("relay_events", object)
RELAY_RETENTION_TASK_KEY = AppKey("relay_retention_task", asyncio.Task)
RELAY_EVENT_STREAM_STOP_KEY = AppKey("relay_event_stream_stop", object)
FORBIDDEN_IDENTITY_FIELDS = frozenset(
    {"target", "recipient", "open_id", "user_id", "email", "employee_id", "profile", "profile_name", "agent"}
)


def _feishu_uuid(value: str) -> str:
    return _sha(value)[:50]


def _feishu_error_message(fallback: str, exc: FeishuApiError) -> str:
    details = []
    if exc.code is not None:
        details.append(f"code={exc.code}")
    if exc.message:
        details.append(f"msg={exc.message}")
    return f"{fallback}: Feishu {', '.join(details)}" if details else fallback


def _contains_forbidden_identity(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() in FORBIDDEN_IDENTITY_FIELDS or _contains_forbidden_identity(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_identity(item) for item in value)
    return False


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class RelayEvents:
    """Narrow Feishu event boundary; invalid identity or ownership fails closed."""

    def __init__(self, store: RelayStore, feishu: Any) -> None:
        self.store = store
        self.feishu = feishu

    async def ingest_text(
        self,
        *,
        event_id: str,
        actor_id: str,
        text: str,
        create_time: int,
        parent_message_id: str | None = None,
    ) -> bool:
        if not event_id or not _valid_actor_id(actor_id) or not text or create_time <= 0:
            logger.info("relay_audit event=reply status=invalid")
            return False
        assigned, ambiguous = self.store.assign_reply(
            event_id=event_id,
            actor_id=actor_id,
            text=text,
            create_time=create_time,
            parent_message_id=parent_message_id,
        )
        if ambiguous:
            logger.info(
                "relay_audit event=reply status=ambiguous actor=%s", _sha(actor_id)[:12]
            )
            await _await(self.feishu.send_ambiguity_notice(actor_id))
        else:
            logger.info(
                "relay_audit event=reply status=%s actor=%s",
                "assigned" if assigned else "unmatched",
                _sha(actor_id)[:12],
            )
        return assigned

    async def ingest_reaction(self, **event: Any) -> bool:
        if (
            any(not event.get(key) for key in ("event_id", "actor_id", "message_id", "emoji_type"))
            or not _valid_actor_id(str(event["actor_id"]))
        ):
            return False
        return self.store.change_reaction(**event)

    async def ingest_card_action(self, **event: Any) -> bool:
        if (
            any(not event.get(key) for key in ("actor_id", "card_id", "nonce", "message_id", "action_id"))
            or not _valid_actor_id(str(event["actor_id"]))
        ):
            return False
        changed = self.store.action_card(**event)
        if changed:
            # 首次点击不在这里改卡片外观：调用方拿到 action_id 后会用自己手里的原文
            # 做局部替换（只换按钮那一块、正文保留）。服务端拼不出那个内容，硬拼只会
            # 把仓库、命令、意图全抹成一行状态，对用户是负收益。
            # 重复点击则相反 —— 必须由 ack 带卡片回放最新内容，见 replay_card_content。
            logger.info(
                "relay_audit event=card_action status=actioned action=%s card=%s msg=%s actor=%s",
                event["action_id"],
                event["card_id"],
                event["message_id"],
                _sha(str(event["actor_id"]))[:12],
            )
        return changed

    async def replay_card_content(self, **event: Any) -> dict[str, Any] | None:
        """重复点击的 ack 卡片内容；守卫失败或内容从未更新过 → None（ack 退回纯 toast）。

        为什么必须回放：内容 PATCH 之后、客户端按钮消失之前的重复点击，其 ack 若不带
        卡片，飞书会把会话流渲染永久钉在点击时客户端手里的旧版本（跨设备、冷启动不
        恢复；2026-08-27 受控矩阵 4/4 复现）。ack 带上最新内容，钉住的就是正确版本。
        """
        if (
            any(not event.get(key) for key in ("actor_id", "card_id", "nonce", "message_id"))
            or not _valid_actor_id(str(event["actor_id"]))
        ):
            return None
        content = self.store.replay_card_content(
            actor_id=str(event["actor_id"]),
            card_id=str(event["card_id"]),
            nonce=str(event["nonce"]),
            message_id=str(event["message_id"]),
        )
        if content is not None:
            logger.info(
                "relay_audit event=card_action status=replayed card=%s msg=%s actor=%s",
                event["card_id"],
                event["message_id"],
                _sha(str(event["actor_id"]))[:12],
            )
        return content


def _error(
    code: str, message: str, status: int, retry_after: int | None = None
):
    from aiohttp import web

    error: dict[str, Any] = {"code": code, "message": message}
    headers = None
    if retry_after is not None:
        error["retry_after"] = retry_after
        headers = {"Retry-After": str(retry_after)}
    return web.json_response({"error": error}, status=status, headers=headers)


class RelayLogHandler(logging.Handler):
    """Mirror relay logs into the store so an admin can read them over HTTP.

    journald stays the primary sink and its lines are untouched — this only adds a
    second, queryable outlet. Every `relay_audit` line is kept at any level (they are
    already redacted at the call site); anything else only from WARNING up, so the
    admin sees real failures without the store becoming a firehose.
    """

    def __init__(self, store: Any) -> None:
        super().__init__(level=logging.NOTSET)
        self._store = store
        self._busy = threading.local()
        # raw must byte-match the journald line (basicConfig default format) so the
        # admin can correlate API evidence with the host log; format() also appends
        # tracebacks, which journald shows too.
        self.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._busy, "on", False):
            return
        self._busy.on = True
        try:
            message = record.getMessage()
            if not message.startswith(_AUDIT_PREFIX) and record.levelno < logging.WARNING:
                return
            fields = dict(_LOG_FIELD_RE.findall(message))
            self._store.append_log(
                ts=int(record.created * 1000),
                level=record.levelname,
                logger=record.name,
                event=fields.get("event", ""),
                status=fields.get("status", ""),
                actor=fields.get("actor", ""),
                card_id=fields.get("card", ""),
                message_id=fields.get("msg", ""),
                raw=self.format(record),
            )
        except Exception:
            # A broken log sink must never surface in an HTTP handler or the ws thread.
            self.handleError(record)
        finally:
            self._busy.on = False


def install_relay_log_handler(store: Any) -> RelayLogHandler:
    handler = RelayLogHandler(store)
    logging.getLogger().addHandler(handler)
    return handler


def create_agent_relay_app(
    *, db_path: Path | str, encryption_key: str | bytes | None, oauth: Any, feishu: Any
):
    from aiohttp import web

    store = RelayStore(db_path, encryption_key)

    def authenticate(request: Any) -> dict[str, Any] | None:
        header = str(request.headers.get("Authorization", "") or "")
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        return store.authenticate(token) if token else None

    def admin_denied(request: Any):
        """Fail closed: no header → 401, wrong or unset admin token → 403."""
        header = str(request.headers.get("Authorization", "") or "")
        if not header.startswith("Bearer "):
            return _error("unauthorized", "admin token required", 401)
        expected = os.environ.get(ADMIN_TOKEN_ENV, "").strip()
        # compare_digest raises TypeError on non-ASCII str — compare UTF-8 bytes.
        offered = header[7:].strip().encode("utf-8", "surrogatepass")
        if not expected or not hmac.compare_digest(offered, expected.encode("utf-8")):
            return _error("forbidden", "admin access is not configured for this token", 403)
        return None

    def admin_window(request: Any):
        """Auth then range; returns (window, error) with exactly one of them set."""
        denied = admin_denied(request)
        if denied is not None:
            return None, denied
        try:
            since = int(request.query["since"])
            until = int(request.query["until"])
        except (KeyError, ValueError):
            return None, _error("invalid_range", "since and until must be integer milliseconds", 400)
        if not (0 <= since < 2**63 and 0 <= until < 2**63):
            # SQLite binds int64 only; anything beyond is a 400, never an OverflowError 500.
            return None, _error("invalid_range", "since and until must be integer milliseconds", 400)
        if until <= since or until - since > ADMIN_MAX_WINDOW_MS:
            return None, _error("invalid_range", "until must be after since and within 7 days", 400)
        return (since, until), None

    async def admin_logs(request):
        window, denied = admin_window(request)
        if denied is not None:
            return denied
        try:
            after_id = int(request.query.get("after_id", "0"))
            if not 0 <= after_id < 2**63:
                raise ValueError
        except ValueError:
            return _error("invalid_range", "after_id must be a nonnegative integer", 400)
        items = store.list_logs(*window, limit=ADMIN_LOG_LIMIT + 1, after_id=after_id)
        return web.json_response(
            {"items": items[:ADMIN_LOG_LIMIT], "truncated": len(items) > ADMIN_LOG_LIMIT}
        )

    async def admin_stats(request):
        window, denied = admin_window(request)
        if denied is not None:
            return denied
        return web.json_response(store.usage_stats(*window))

    async def start_enrollment(_request):
        return web.json_response(store.create_enrollment(oauth), status=201)

    async def oauth_callback(request):
        state = str(request.query.get("state") or "")
        code = str(request.query.get("code") or "")
        if not state or not code:
            return _error("invalid_oauth_callback", "state and code are required", 400)
        if not store.begin_enrollment(state):
            return _error("invalid_enrollment", "enrollment is missing, expired, or already used", 400)
        try:
            identity = await _await(oauth.exchange(code))
        except Exception:
            store.fail_enrollment(state)
            return _error("oauth_unavailable", "Feishu OAuth failed", 502)
        actor_id = str((identity or {}).get("actor_id") or "").strip()
        display_name = str((identity or {}).get("display_name") or "").strip()
        if (
            not _valid_actor_id(actor_id)
            or not display_name
            or not store.complete_enrollment(state, actor_id, display_name)
        ):
            store.fail_enrollment(state)
            return _error("invalid_enrollment", "enrollment is missing, expired, or already used", 400)
        logger.info("relay_audit event=enrollment status=completed actor=%s", _sha(actor_id)[:12])
        return web.json_response({"status": "completed"})

    async def poll_enrollment(request):
        result = store.claim_enrollment(str(request.match_info["enroll_id"]))
        return (
            web.json_response(result)
            if result is not None
            else _error("not_found", "enrollment not found", 404)
        )

    async def whoami(request):
        actor = authenticate(request)
        if actor is None:
            return _error("unauthorized", "invalid or revoked token", 401)
        return web.json_response(
            {key: actor[key] for key in ("token_id", "user_name", "identity_fingerprint", "issued_at", "status")}
        )

    async def send_message(request):
        actor = authenticate(request)
        if actor is None:
            return _error("unauthorized", "invalid or revoked token", 401)
        try:
            payload = await request.json()
        except Exception:
            return _error("invalid_json", "body must be a JSON object", 400)
        if not isinstance(payload, dict):
            return _error("invalid_body", "body must be a JSON object", 400)
        if _contains_forbidden_identity(payload):
            logger.warning("relay_audit event=identity_reject status=denied actor=%s", actor["identity_fingerprint"])
            return _error("identity_field_forbidden", "recipient and identity fields are not accepted", 400)
        msg_type = str(payload.get("type") or "")
        content = payload.get("content")
        key = str(payload.get("idempotency_key") or "").strip()
        if msg_type not in {"text", "card"} or not isinstance(content, dict) or not key:
            return _error("invalid_message", "type, content, and idempotency_key are required", 400)
        if len(json.dumps(content, ensure_ascii=False).encode("utf-8")) > MAX_CONTENT_BYTES:
            return _error("content_too_large", "content exceeds 30720 bytes", 400)
        if msg_type == "card":
            try:
                _card_with_actions(content, [], "", "")
            except ValueError as exc:
                return _error("invalid_card", str(exc), 400)
        reply_window = payload.get("reply_window_seconds", 0)
        if not isinstance(reply_window, int) or (
            reply_window != 0 and not 300 <= reply_window <= 1800
        ):
            return _error("invalid_reply_window", "reply_window_seconds must be 0 or 300..1800", 400)
        digest = _request_hash(payload)
        try:
            row, created = store.reserve_message(
                token_id=actor["token_id"],
                idempotency_key=key,
                request_hash=digest,
                reply_expires_at=_now_ms() + reply_window * 1000 if reply_window else None,
                msg_type=msg_type,
            )
        except RelayConflict as exc:
            return _error("idempotency_conflict", str(exc), 409)
        existing = store.message_result(row)
        if existing is not None:
            return web.json_response(existing, status=200)
        uuid = _feishu_uuid(f"{actor['token_id']}:{key}")
        try:
            result = await asyncio.wait_for(
                _await(
                    feishu.send_message(
                        actor_id=actor["actor_id"],
                        msg_type=msg_type,
                        content=content,
                        uuid=uuid,
                    )
                ),
                timeout=5,
            )
        except TimeoutError:
            logger.warning("relay_audit event=send status=timeout actor=%s", actor["identity_fingerprint"])
            return _error("upstream_timeout", "Feishu send timed out", 504)
        except FeishuApiError as exc:
            if exc.status == 429:
                logger.warning("relay_audit event=send status=rate_limited actor=%s", actor["identity_fingerprint"])
                return _error(
                    "rate_limited",
                    _feishu_error_message("Feishu rate limited the request", exc),
                    429,
                    exc.retry_after or 1,
                )
            if exc.status == 400:
                logger.warning("relay_audit event=send status=rejected actor=%s", actor["identity_fingerprint"])
                return _error(
                    "invalid_message",
                    _feishu_error_message("Feishu rejected the payload", exc),
                    400,
                )
            logger.warning("relay_audit event=send status=upstream_error actor=%s", actor["identity_fingerprint"])
            return _error(
                "upstream_unavailable",
                _feishu_error_message("Feishu send failed", exc),
                502,
            )
        except Exception:
            logger.warning("relay_audit event=send status=upstream_error actor=%s", actor["identity_fingerprint"])
            return _error("upstream_unavailable", "Feishu send failed", 502)
        result = dict(result or {})
        if not str(result.get("message_id") or "").strip():
            return _error("missing_receipt", "Feishu returned no message_id", 502)
        public_result = {
            "message_id": str(result["message_id"]),
            "conversation_id": str(result.get("conversation_id") or ""),
        }
        store.finish_message(str(row["resource_id"]), public_result)
        logger.info(
            "relay_audit event=send kind=%s status=sent msg=%s actor=%s",
            msg_type,
            public_result["message_id"],
            actor["identity_fingerprint"],
        )
        return web.json_response(public_result, status=201 if created else 200)

    async def update_message(request):
        actor = authenticate(request)
        if actor is None:
            return _error("unauthorized", "invalid or revoked token", 401)
        try:
            payload = await request.json()
        except Exception:
            return _error("invalid_json", "body must be a JSON object", 400)
        if not isinstance(payload, dict):
            return _error("invalid_body", "body must be a JSON object", 400)
        if _contains_forbidden_identity(payload):
            logger.warning("relay_audit event=identity_reject status=denied actor=%s", actor["identity_fingerprint"])
            return _error("identity_field_forbidden", "recipient and identity fields are not accepted", 400)
        message_id = str(request.match_info["message_id"])
        row = store.owned_sent_message(actor["token_id"], message_id)
        if row is None:
            return _error("not_found", "message not found", 404)
        window = payload.get("reply_window_seconds")
        # ponytail: `False == 0` in Python — take the int 0 only, never a bool.
        if window == 0 and not isinstance(window, bool):
            if not store.close_reply_window(actor["token_id"], message_id):
                return _error("window_not_open", "no open reply window for this message", 409)
            logger.info(
                "relay_audit event=reply_window status=closed msg=%s actor=%s",
                message_id,
                actor["identity_fingerprint"],
            )
            return web.json_response({"message_id": message_id, "reply_window_seconds": 0})
        content = payload.get("content")
        if not isinstance(content, dict):
            return _error("invalid_message", "content is required", 400)
        if row.get("kind") != "card":
            return _error("invalid_message", "only card messages can be updated", 400)
        if len(json.dumps(content, ensure_ascii=False).encode("utf-8")) > MAX_CONTENT_BYTES:
            return _error("content_too_large", "content exceeds 30720 bytes", 400)
        try:
            _card_with_actions(content, [], "", "")
        except ValueError as exc:
            return _error("invalid_card", str(exc), 400)
        try:
            await asyncio.wait_for(
                _await(feishu.update_card(message_id=message_id, content=content)),
                timeout=5,
            )
        except TimeoutError:
            logger.warning("relay_audit event=update status=timeout msg=%s actor=%s", message_id, actor["identity_fingerprint"])
            return _error("upstream_timeout", "Feishu update timed out", 504)
        except FeishuApiError as exc:
            if exc.status == 429:
                logger.warning("relay_audit event=update status=rate_limited msg=%s actor=%s", message_id, actor["identity_fingerprint"])
                return _error(
                    "rate_limited",
                    _feishu_error_message("Feishu rate limited the request", exc),
                    429,
                    exc.retry_after or 1,
                )
            if exc.status == 400:
                logger.warning("relay_audit event=update status=rejected msg=%s actor=%s", message_id, actor["identity_fingerprint"])
                return _error(
                    "invalid_message",
                    _feishu_error_message("Feishu rejected the payload", exc),
                    400,
                )
            logger.warning("relay_audit event=update status=upstream_error msg=%s actor=%s", message_id, actor["identity_fingerprint"])
            return _error(
                "upstream_unavailable",
                _feishu_error_message("Feishu update failed", exc),
                502,
            )
        except Exception:
            logger.warning("relay_audit event=update status=upstream_error msg=%s actor=%s", message_id, actor["identity_fingerprint"])
            return _error("upstream_unavailable", "Feishu update failed", 502)
        # PATCH 成功才缓存 —— 重复点击的 ack 要回放这份内容（见 replay_card_content）。
        # 非卡片消息在上面已被拦掉，这里 rowcount=0 只说明这条 message 不是按钮卡。
        store.cache_card_content(actor["token_id"], message_id, content)
        logger.info("relay_audit event=update status=updated msg=%s actor=%s", message_id, actor["identity_fingerprint"])
        return web.json_response({"message_id": message_id})

    async def revoke_token(request):
        actor = authenticate(request)
        token_id = str(request.match_info["token_id"])
        if actor is None:
            return _error("unauthorized", "invalid or revoked token", 401)
        if token_id != actor["token_id"]:
            return _error("not_found", "token not found", 404)
        if not store.revoke_token(token_id):
            return _error("not_found", "token not found", 404)
        logger.info("relay_audit event=revoke status=revoked actor=%s", actor["identity_fingerprint"])
        return web.json_response({"token_id": token_id, "status": "revoked"})

    async def get_replies(request):
        actor = authenticate(request)
        if actor is None:
            return _error("unauthorized", "invalid or revoked token", 401)
        try:
            since_ts = int(request.query.get("since_ts", 0))
            limit = min(100, max(1, int(request.query.get("limit", 50))))
        except ValueError:
            return _error("invalid_query", "since_ts and limit must be integers", 400)
        replies = store.message_replies(
            actor["token_id"], str(request.match_info["message_id"]), since_ts, limit
        )
        return (
            web.json_response({"replies": replies})
            if replies is not None
            else _error("not_found", "message not found", 404)
        )

    async def get_reactions(request):
        actor = authenticate(request)
        if actor is None:
            return _error("unauthorized", "invalid or revoked token", 401)
        reactions = store.message_reactions(
            actor["token_id"], str(request.match_info["message_id"])
        )
        return (
            web.json_response({"reactions": reactions})
            if reactions is not None
            else _error("not_found", "message not found", 404)
        )

    async def create_card(request):
        actor = authenticate(request)
        if actor is None:
            return _error("unauthorized", "invalid or revoked token", 401)
        try:
            payload = await request.json()
        except Exception:
            return _error("invalid_json", "body must be a JSON object", 400)
        if not isinstance(payload, dict) or _contains_forbidden_identity(payload):
            return _error("invalid_card", "invalid card body or identity field", 400)
        content = payload.get("content")
        actions = payload.get("actions")
        key = str(payload.get("idempotency_key") or "").strip()
        if "expires_in" in payload and "expiry" in payload:
            return _error("invalid_card", "use only one of expires_in or expiry", 400)
        expires_in = payload.get("expires_in", payload.get("expiry"))
        valid_actions = (
            isinstance(actions, list)
            and 1 <= len(actions) <= 5
            and all(
                isinstance(action, dict)
                and set(action) == {"id", "label"}
                and isinstance(action["id"], str)
                and 0 < len(action["id"]) <= 64
                and isinstance(action["label"], str)
                and 0 < len(action["label"]) <= 64
                for action in actions
            )
        )
        if not isinstance(content, dict):
            return _error("invalid_card", "content must be a card object", 400)
        if len(json.dumps(content, ensure_ascii=False).encode("utf-8")) > MAX_CONTENT_BYTES:
            return _error("invalid_card", "content exceeds 30720 bytes", 400)
        if not valid_actions:
            return _error(
                "invalid_card",
                "actions must be 1..5 objects with exactly string id and label fields of 1..64 characters",
                400,
            )
        if not key:
            return _error("invalid_card", "idempotency_key is required", 400)
        if type(expires_in) is not int or not 300 <= expires_in <= 1800:
            return _error(
                "invalid_card",
                "expires_in or expiry must be integer TTL seconds from 300 to 1800",
                400,
            )
        try:
            _card_with_actions(content, actions, "validation", "validation")
        except ValueError as exc:
            return _error("invalid_card", str(exc), 400)
        action_ids = [action["id"] for action in actions]
        if len(set(action_ids)) != len(action_ids):
            return _error("invalid_card", "action ids must be unique", 400)
        normalized_payload = {**payload, "expires_in": expires_in}
        normalized_payload.pop("expiry", None)
        try:
            row, nonce, created = store.reserve_card(
                token_id=actor["token_id"],
                idempotency_key=key,
                request_hash=_request_hash(normalized_payload),
                action_ids=action_ids,
                expires_at=_now_ms() + expires_in * 1000,
            )
        except RelayConflict as exc:
            return _error("idempotency_conflict", str(exc), 409)
        if row["status"] != "sending":
            # ponytail: heal a missing text fallback on replay — the first attempt may
            # have died between finish_card and the register below.
            if str(row["message_id"] or ""):
                try:
                    store.register_card_message(
                        actor["token_id"], key, str(row["message_id"]), int(row["expires_at"])
                    )
                except Exception:
                    logger.warning(
                        "relay_audit event=card status=fallback_register_failed card=%s msg=%s actor=%s",
                        row["card_id"],
                        row["message_id"],
                        actor["identity_fingerprint"],
                    )
            return web.json_response(store._public_card(row))
        try:
            result = await asyncio.wait_for(
                _await(
                    feishu.send_card(
                        actor_id=actor["actor_id"],
                        content=content,
                        actions=actions,
                        card_id=row["card_id"],
                        nonce=nonce,
                        uuid=_feishu_uuid(f"{actor['token_id']}:{key}"),
                    )
                ),
                timeout=5,
            )
        except TimeoutError:
            logger.warning("relay_audit event=card status=timeout card=%s actor=%s", row["card_id"], actor["identity_fingerprint"])
            return _error("upstream_timeout", "Feishu send timed out", 504)
        except FeishuApiError as exc:
            if exc.status == 429:
                logger.warning("relay_audit event=card status=rate_limited card=%s actor=%s", row["card_id"], actor["identity_fingerprint"])
                return _error(
                    "rate_limited",
                    _feishu_error_message("Feishu rate limited the request", exc),
                    429,
                    exc.retry_after or 1,
                )
            if exc.status == 400:
                store.fail_card(str(row["card_id"]))
                logger.warning("relay_audit event=card status=rejected card=%s actor=%s", row["card_id"], actor["identity_fingerprint"])
                return _error(
                    "invalid_card",
                    _feishu_error_message("Feishu rejected the payload", exc),
                    400,
                )
            logger.warning("relay_audit event=card status=upstream_error card=%s actor=%s", row["card_id"], actor["identity_fingerprint"])
            return _error(
                "upstream_unavailable",
                _feishu_error_message("Feishu send failed", exc),
                502,
            )
        except Exception:
            logger.warning("relay_audit event=card status=upstream_error card=%s actor=%s", row["card_id"], actor["identity_fingerprint"])
            return _error("upstream_unavailable", "Feishu send failed", 502)
        result = dict(result or {})
        message_id = str(result.get("message_id") or "").strip()
        if not message_id:
            store.fail_card(str(row["card_id"]))
            logger.warning("relay_audit event=card status=missing_receipt card=%s actor=%s", row["card_id"], actor["identity_fingerprint"])
            return _error("missing_receipt", "Feishu returned no message_id", 502)
        store.finish_card(
            str(row["card_id"]), message_id, str(result.get("conversation_id") or "")
        )
        try:
            # ponytail: the window is the card's OWN expiry, not a fresh clock read —
            # a slow send would otherwise leave the text window alive past the card.
            store.register_card_message(
                actor["token_id"], key, message_id, int(row["expires_at"])
            )
        except Exception:
            # ponytail: the text fallback is a bonus — never fail a delivered card over it.
            logger.warning(
                "relay_audit event=card status=fallback_register_failed card=%s msg=%s actor=%s",
                row["card_id"],
                message_id,
                actor["identity_fingerprint"],
            )
        state = store.card_state(actor["token_id"], str(row["card_id"]))
        logger.info("relay_audit event=card status=pending card=%s msg=%s actor=%s",
                    row["card_id"], message_id, actor["identity_fingerprint"])
        return web.json_response(state, status=201 if created else 200)

    async def get_card(request):
        actor = authenticate(request)
        if actor is None:
            return _error("unauthorized", "invalid or revoked token", 401)
        state = store.card_state(actor["token_id"], str(request.match_info["card_id"]))
        return web.json_response(state) if state else _error("not_found", "card not found", 404)

    async def close_card(request):
        actor = authenticate(request)
        if actor is None:
            return _error("unauthorized", "invalid or revoked token", 401)
        try:
            payload = await request.json()
        except Exception:
            return _error("invalid_json", "body must be a JSON object", 400)
        if not isinstance(payload, dict):
            return _error("invalid_json", "body must be a JSON object", 400)
        if _contains_forbidden_identity(payload):
            logger.warning("relay_audit event=identity_reject status=denied actor=%s", actor["identity_fingerprint"])
            return _error("identity_field_forbidden", "recipient and identity fields are not accepted", 400)

        card_id = str(request.match_info["card_id"])
        content = payload.get("content")
        wants_close = "status" in payload or "reason" in payload

        # 两件事可以各自单独做，也可以一次做完（超时那一次就是「关闭 + 改成已超时」）
        if content is None and not wants_close:
            return _error("invalid_close",
                          "provide content, or status with a close reason, or both", 400)

        if content is not None:
            if not isinstance(content, dict):
                return _error("invalid_card", "content must be a card object", 400)
            if len(json.dumps(content, ensure_ascii=False).encode("utf-8")) > MAX_CONTENT_BYTES:
                return _error("content_too_large", "content exceeds 30720 bytes", 400)
            try:
                _card_with_actions(content, [], "", "")
            except ValueError as exc:
                return _error("invalid_card", str(exc), 400)

        if wants_close:
            reason = str(payload.get("reason") or "")
            if payload.get("status") != "closed" or reason not in {
                "local_resumed",
                "client_timeout",
                "cancelled",
            }:
                return _error("invalid_close", "status and a valid close reason are required", 400)
            state, changed = store.close_card(actor["token_id"], card_id, reason)
            if state is None:
                return _error("not_found", "card not found", 404)
            if not changed and not (
                state.get("status") == "closed" and state.get("reason") == reason
            ):
                return web.json_response(state, status=409)
        else:
            # 只改外观：卡片可能已经是 actioned（用户点过按钮），那时没有状态要改
            state = store.card_state(actor["token_id"], card_id)
            if state is None:
                return _error("not_found", "card not found", 404)

        if content is not None or wants_close:
            if not state.get("message_id"):
                return _error("not_delivered", "card has no delivered message to update", 409)
            # ponytail: no caller content on a close → server-composed terminal look,
            # which is what every pre-existing client relies on.
            update = (
                {"content": content}
                if content is not None
                else {"status": "closed"}
            )
            try:
                await asyncio.wait_for(
                    _await(feishu.update_card(message_id=state["message_id"], **update)),
                    timeout=5,
                )
            except TimeoutError:
                return _error("upstream_timeout", "Feishu update timed out", 504)
            except FeishuApiError as exc:
                if exc.status == 429:
                    return _error("rate_limited",
                                  _feishu_error_message("Feishu rate limited the request", exc),
                                  429, exc.retry_after or 1)
                if exc.status == 400:
                    return _error("invalid_card",
                                  _feishu_error_message("Feishu rejected the payload", exc), 400)
                return _error("upstream_unavailable",
                              _feishu_error_message("card state updated but Feishu update failed", exc),
                              502)
            except Exception:
                return _error("upstream_unavailable",
                              "card state updated but Feishu update failed", 502)

        if content is not None:
            # 与 update_message 同一条纪律：只有真的写出去了才缓存
            store.cache_card_content(actor["token_id"], str(state.get("message_id") or ""), content)
        logger.info("relay_audit event=card status=%s content=%s card=%s msg=%s actor=%s",
                    state.get("status"), "yes" if content is not None else "no",
                    card_id, state.get("message_id") or "",
                    actor["identity_fingerprint"])
        return web.json_response(state)

    async def cleanup(app):
        task = app.get(RELAY_RETENTION_TASK_KEY)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        stop = app.get(RELAY_EVENT_STREAM_STOP_KEY)
        if callable(stop):
            await _await(stop())
        store.close()

    events = RelayEvents(store, feishu)

    async def startup(_app):
        start = getattr(feishu, "start_event_stream", None)
        if callable(start):
            _app[RELAY_EVENT_STREAM_STOP_KEY] = start(
                events, asyncio.get_running_loop()
            )

        async def retention() -> None:
            while True:
                await asyncio.sleep(3600)
                store.prune()

        _app[RELAY_RETENTION_TASK_KEY] = asyncio.create_task(retention())

    app = web.Application(client_max_size=MAX_CONTENT_BYTES + 4096)
    app[RELAY_STORE_KEY] = store
    app[RELAY_EVENTS_KEY] = events
    app.router.add_post("/v1/enroll/sessions", start_enrollment)
    app.router.add_get("/v1/enroll/callback", oauth_callback)
    app.router.add_get("/v1/enroll/sessions/{enroll_id}", poll_enrollment)
    app.router.add_get("/v1/whoami", whoami)
    app.router.add_post("/v1/messages", send_message)
    app.router.add_patch("/v1/messages/{message_id}", update_message)
    app.router.add_get("/v1/messages/{message_id}/replies", get_replies)
    app.router.add_get("/v1/messages/{message_id}/reactions", get_reactions)
    app.router.add_post("/v1/cards", create_card)
    app.router.add_get("/v1/cards/{card_id}", get_card)
    app.router.add_patch("/v1/cards/{card_id}", close_card)
    app.router.add_post("/v1/tokens/{token_id}/revoke", revoke_token)
    app.router.add_get("/v1/admin/logs", admin_logs)
    app.router.add_get("/v1/admin/stats", admin_stats)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


def main() -> None:
    from aiohttp import web

    logging.basicConfig(level=logging.INFO)
    app_id = os.environ.get("HERMES_AGENT_RELAY_FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("HERMES_AGENT_RELAY_FEISHU_APP_SECRET", "").strip()
    redirect_uri = os.environ.get("HERMES_AGENT_RELAY_OAUTH_REDIRECT_URI", "").strip()
    encryption_key = os.environ.get("HERMES_AGENT_RELAY_ENCRYPTION_KEY", "").strip()
    db_path = os.environ.get("HERMES_AGENT_RELAY_DB", "/var/lib/hermes-agent-relay/relay.db")
    transport = FeishuRelayClient(app_id, app_secret, redirect_uri)
    app = create_agent_relay_app(
        db_path=db_path,
        encryption_key=encryption_key,
        oauth=transport,
        feishu=transport,
    )
    install_relay_log_handler(app[RELAY_STORE_KEY])
    web.run_app(
        app,
        host=os.environ.get("HERMES_AGENT_RELAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("HERMES_AGENT_RELAY_PORT", "8770")),
        access_log=None,
    )


if __name__ == "__main__":
    main()
