"""Encrypted SQLite state for the standalone local-agent relay."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .credentials import _open_json, _seal_json

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS relay_enrollments (
    enroll_hash TEXT PRIMARY KEY,
    state_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    token_payload TEXT,
    token_id TEXT,
    user_name TEXT,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS relay_tokens (
    token_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    actor_payload TEXT NOT NULL,
    actor_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    issued_at INTEGER NOT NULL,
    last_used_at INTEGER,
    revoked_at INTEGER
);
CREATE TABLE IF NOT EXISTS relay_messages (
    resource_id TEXT PRIMARY KEY,
    token_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    message_id TEXT,
    result_payload TEXT,
    reply_expires_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(token_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS relay_idempotency (
    token_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(token_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS relay_replies (
    event_id TEXT PRIMARY KEY,
    token_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    content_payload TEXT NOT NULL,
    create_time INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    purged_at INTEGER
);
CREATE TABLE IF NOT EXISTS relay_reactions (
    token_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    emoji_type TEXT NOT NULL,
    event_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(token_id, message_id, emoji_type)
);
CREATE TABLE IF NOT EXISTS relay_cards (
    card_id TEXT PRIMARY KEY,
    token_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    nonce_hash TEXT NOT NULL,
    nonce_payload TEXT NOT NULL,
    action_ids TEXT NOT NULL,
    status TEXT NOT NULL,
    action_id TEXT,
    close_reason TEXT,
    message_id TEXT,
    conversation_payload TEXT,
    content_payload TEXT,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(token_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS relay_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    level TEXT NOT NULL,
    logger TEXT NOT NULL,
    event TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    card_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS relay_logs_ts ON relay_logs(ts);
"""

LOG_RETENTION_MS = 30 * 86_400_000
LOG_FIELDS = ("ts", "level", "logger", "event", "status", "actor", "card_id", "message_id", "raw")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _token_hash(token: str, token_id: str) -> str:
    return hashlib.scrypt(
        token.encode("utf-8"), salt=token_id.encode("utf-8"), n=2**14, r=8, p=1
    ).hex()


def _request_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha(raw)


class RelayConflict(ValueError):
    pass


class RelayStore:
    def __init__(self, db_path: Path | str, encryption_key: str | bytes | None) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        if not encryption_key:
            raise RuntimeError("agent relay encryption key is required")
        raw_key = encryption_key.encode("utf-8") if isinstance(encryption_key, str) else encryption_key
        self._key = hashlib.sha256(raw_key).digest()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # 老库升级：CREATE TABLE IF NOT EXISTS 不会给已存在的表加新列
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(relay_cards)")}
            if "content_payload" not in cols:
                self._conn.execute("ALTER TABLE relay_cards ADD COLUMN content_payload TEXT")
            self._conn.commit()
        self._secure_files()

    def _secure_files(self) -> None:
        if self.db_path == ":memory:":
            return
        for suffix in ("", "-wal", "-shm"):
            path = Path(self.db_path + suffix)
            if path.exists():
                os.chmod(path, 0o600)

    def close(self) -> None:
        self._conn.close()

    def create_enrollment(self, oauth: Any) -> dict[str, Any]:
        now = _now_ms()
        enroll_id = "en_" + secrets.token_urlsafe(24)
        state = secrets.token_urlsafe(32)
        with self._lock:
            self._conn.execute(
                "INSERT INTO relay_enrollments VALUES (?, ?, 'pending', NULL, NULL, NULL, ?, ?, ?)",
                (_sha(enroll_id), _sha(state), now + 600_000, now, now),
            )
            self._conn.commit()
        return {
            "enroll_id": enroll_id,
            "authorize_url": oauth.authorize_url(state=state),
            "expires_in": 600,
            "status": "pending",
        }

    def begin_enrollment(self, state: str) -> bool:
        now = _now_ms()
        with self._lock:
            changed = self._conn.execute(
                "UPDATE relay_enrollments SET status='authorizing', updated_at=? "
                "WHERE state_hash=? AND status='pending' AND expires_at>?",
                (now, _sha(state), now),
            ).rowcount
            self._conn.commit()
        return changed == 1

    def fail_enrollment(self, state: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE relay_enrollments SET status='failed', updated_at=? "
                "WHERE state_hash=? AND status='authorizing'",
                (_now_ms(), _sha(state)),
            )
            self._conn.commit()

    def complete_enrollment(self, state: str, actor_id: str, display_name: str) -> bool:
        if not actor_id.startswith("ou_") or not display_name:
            return False
        now = _now_ms()
        token_id = "tk_" + secrets.token_hex(12)
        token = f"hm-relay-{token_id}.{secrets.token_urlsafe(32)}"
        actor_payload = _seal_json(
            {"actor_id": actor_id, "display_name": display_name}, self._key
        )
        token_payload = _seal_json({"token": token, "user_name": display_name}, self._key)
        with self._lock:
            row = self._conn.execute(
                "SELECT status, expires_at FROM relay_enrollments WHERE state_hash = ?",
                (_sha(state),),
            ).fetchone()
            if row is None or row["status"] != "authorizing" or int(row["expires_at"]) <= now:
                return False
            self._conn.execute(
                """
                INSERT INTO relay_tokens
                    (token_id, token_hash, actor_payload, actor_fingerprint, status, issued_at)
                VALUES (?, ?, ?, ?, 'active', ?)
                """,
                (token_id, _token_hash(token, token_id), actor_payload, _sha(actor_id)[:12], now),
            )
            self._conn.execute(
                """
                UPDATE relay_enrollments
                SET status='completed', token_payload=?, token_id=?, user_name=?, updated_at=?
                WHERE state_hash=? AND status='authorizing'
                """,
                (token_payload, token_id, None, now, _sha(state)),
            )
            self._conn.commit()
        return True

    def claim_enrollment(self, enroll_id: str) -> dict[str, Any] | None:
        now = _now_ms()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM relay_enrollments WHERE enroll_hash = ?", (_sha(enroll_id),)
            ).fetchone()
            if row is None:
                return None
            status = str(row["status"])
            if int(row["expires_at"]) <= now and status == "pending":
                self._conn.execute(
                    "UPDATE relay_enrollments SET status='expired', updated_at=? WHERE enroll_hash=?",
                    (now, _sha(enroll_id)),
                )
                self._conn.commit()
                return {"status": "expired"}
            if status != "completed":
                return {"status": status}
            claimed = _open_json(str(row["token_payload"]), self._key)
            result = {
                "status": "completed",
                "token": claimed["token"],
                "token_id": row["token_id"],
                "user_name": claimed["user_name"],
            }
            self._conn.execute(
                """
                UPDATE relay_enrollments
                SET status='claimed', token_payload=NULL, updated_at=?
                WHERE enroll_hash=? AND status='completed'
                """,
                (now, _sha(enroll_id)),
            )
            self._conn.commit()
            return result

    def authenticate(self, token: str) -> dict[str, Any] | None:
        prefix = "hm-relay-"
        if not token.startswith(prefix) or "." not in token:
            return None
        token_id = token[len(prefix) :].split(".", 1)[0]
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM relay_tokens WHERE token_id=? AND status='active'", (token_id,)
            ).fetchone()
            if row is None or not hmac.compare_digest(
                str(row["token_hash"]), _token_hash(token, token_id)
            ):
                return None
            now = _now_ms()
            self._conn.execute(
                "UPDATE relay_tokens SET last_used_at=? WHERE token_id=?", (now, token_id)
            )
            self._conn.commit()
        actor = _open_json(str(row["actor_payload"]), self._key)
        return {
            "token_id": token_id,
            "actor_id": actor["actor_id"],
            "user_name": actor["display_name"],
            "identity_fingerprint": row["actor_fingerprint"],
            "issued_at": int(row["issued_at"]),
            "status": "active",
        }

    def reserve_message(
        self,
        *,
        token_id: str,
        idempotency_key: str,
        request_hash: str,
        reply_expires_at: int | None,
        msg_type: str = "message",
    ) -> tuple[dict[str, Any], bool]:
        now = _now_ms()
        with self._lock:
            idem = self._conn.execute(
                "SELECT * FROM relay_idempotency WHERE token_id=? AND idempotency_key=?",
                (token_id, idempotency_key),
            ).fetchone()
            if idem is not None:
                if idem["request_hash"] != request_hash or idem["kind"] != "message":
                    raise RelayConflict("idempotency key was already used with another request")
                row = self._conn.execute(
                    "SELECT * FROM relay_messages WHERE resource_id=?",
                    (idem["resource_id"],),
                ).fetchone()
                return dict(row), False
            resource_id = "msg_" + secrets.token_hex(12)
            self._conn.execute(
                "INSERT INTO relay_idempotency VALUES (?, ?, ?, 'message', ?, ?)",
                (token_id, idempotency_key, request_hash, resource_id, now),
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO relay_messages
                    (resource_id, token_id, idempotency_key, request_hash, kind, status,
                     reply_expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'sending', ?, ?, ?)
                """,
                (
                    resource_id,
                    token_id,
                    idempotency_key,
                    request_hash,
                    msg_type,
                    reply_expires_at,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM relay_messages WHERE resource_id=?", (resource_id,)
            ).fetchone()
            return dict(row), True

    def finish_message(self, resource_id: str, result: dict[str, str]) -> None:
        now = _now_ms()
        with self._lock:
            self._conn.execute(
                """
                UPDATE relay_messages SET status='sent', message_id=?, result_payload=?, updated_at=?
                WHERE resource_id=? AND status='sending'
                """,
                (
                    result["message_id"],
                    _seal_json(result, self._key),
                    now,
                    resource_id,
                ),
            )
            self._conn.commit()

    def message_result(self, row: dict[str, Any]) -> dict[str, str] | None:
        raw = row.get("result_payload")
        return _open_json(str(raw), self._key) if raw else None

    def _token_ids_for_actor(self, actor_id: str) -> list[str]:
        if not actor_id.startswith("ou_"):
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT token_id, actor_payload FROM relay_tokens WHERE status='active'"
            ).fetchall()
        return [
            str(row["token_id"])
            for row in rows
            if hmac.compare_digest(
                str(_open_json(str(row["actor_payload"]), self._key)["actor_id"]),
                actor_id,
            )
        ]

    def assign_reply(
        self,
        *,
        event_id: str,
        actor_id: str,
        text: str,
        create_time: int,
        parent_message_id: str | None,
    ) -> tuple[bool, bool]:
        token_ids = self._token_ids_for_actor(actor_id)
        if not token_ids:
            return False, False
        now = _now_ms()
        marks = ",".join("?" for _ in token_ids)
        with self._lock:
            if parent_message_id:
                rows = self._conn.execute(
                    f"SELECT token_id, message_id FROM relay_messages "
                    f"WHERE token_id IN ({marks}) AND message_id=? AND status='sent' "
                    "AND reply_expires_at>?",
                    (*token_ids, parent_message_id, now),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT token_id, message_id FROM relay_messages "
                    f"WHERE token_id IN ({marks}) AND status='sent' AND reply_expires_at>?",
                    (*token_ids, now),
                ).fetchall()
            if len(rows) != 1:
                return False, not parent_message_id and len(rows) > 1
            row = rows[0]
            self._conn.execute(
                "INSERT OR IGNORE INTO relay_replies "
                "(event_id, token_id, message_id, content_payload, create_time, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    row["token_id"],
                    row["message_id"],
                    _seal_json({"text": text}, self._key),
                    create_time,
                    now,
                ),
            )
            self._conn.commit()
        return True, False

    def owned_sent_message(self, token_id: str, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM relay_messages WHERE token_id=? AND message_id=? AND status='sent'",
                (token_id, message_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def message_replies(
        self, token_id: str, message_id: str, since_ts: int, limit: int
    ) -> list[dict[str, Any]] | None:
        with self._lock:
            owner = self._conn.execute(
                "SELECT 1 FROM relay_messages WHERE token_id=? AND message_id=? AND status='sent'",
                (token_id, message_id),
            ).fetchone()
            if owner is None:
                return None
            rows = self._conn.execute(
                "SELECT event_id, content_payload, create_time FROM relay_replies "
                "WHERE token_id=? AND message_id=? AND create_time>? AND purged_at IS NULL "
                "ORDER BY create_time, event_id LIMIT ?",
                (token_id, message_id, since_ts, limit),
            ).fetchall()
        return [
            {
                "id": row["event_id"],
                "text": _open_json(str(row["content_payload"]), self._key)["text"],
                "create_time": int(row["create_time"]),
            }
            for row in rows
        ]

    def change_reaction(
        self,
        *,
        event_id: str,
        actor_id: str,
        message_id: str,
        emoji_type: str,
        operation: str,
    ) -> bool:
        token_ids = self._token_ids_for_actor(actor_id)
        if not token_ids or operation not in {"create", "delete"}:
            return False
        marks = ",".join("?" for _ in token_ids)
        with self._lock:
            row = self._conn.execute(
                f"SELECT token_id FROM relay_messages WHERE token_id IN ({marks}) "
                "AND message_id=? AND status='sent'",
                (*token_ids, message_id),
            ).fetchone()
            if row is None:
                return False
            token_id = str(row["token_id"])
            if operation == "create":
                self._conn.execute(
                    "INSERT OR REPLACE INTO relay_reactions VALUES (?, ?, ?, ?, ?)",
                    (token_id, message_id, emoji_type, event_id, _now_ms()),
                )
            else:
                self._conn.execute(
                    "DELETE FROM relay_reactions WHERE token_id=? AND message_id=? AND emoji_type=?",
                    (token_id, message_id, emoji_type),
                )
            self._conn.commit()
        return True

    def message_reactions(self, token_id: str, message_id: str) -> list[str] | None:
        with self._lock:
            owner = self._conn.execute(
                "SELECT 1 FROM relay_messages WHERE token_id=? AND message_id=? AND status='sent'",
                (token_id, message_id),
            ).fetchone()
            if owner is None:
                return None
            rows = self._conn.execute(
                "SELECT emoji_type FROM relay_reactions WHERE token_id=? AND message_id=? "
                "ORDER BY emoji_type",
                (token_id, message_id),
            ).fetchall()
        return [str(row["emoji_type"]) for row in rows]

    def reserve_card(
        self,
        *,
        token_id: str,
        idempotency_key: str,
        request_hash: str,
        action_ids: list[str],
        expires_at: int,
    ) -> tuple[dict[str, Any], str, bool]:
        now = _now_ms()
        with self._lock:
            idem = self._conn.execute(
                "SELECT * FROM relay_idempotency WHERE token_id=? AND idempotency_key=?",
                (token_id, idempotency_key),
            ).fetchone()
            if idem is not None:
                if idem["request_hash"] != request_hash or idem["kind"] != "card":
                    raise RelayConflict("idempotency key was already used with another request")
                row = self._conn.execute(
                    "SELECT * FROM relay_cards WHERE card_id=?", (idem["resource_id"],)
                ).fetchone()
                nonce = _open_json(str(row["nonce_payload"]), self._key)["nonce"]
                return dict(row), str(nonce), False
            card_id = "card_" + secrets.token_hex(12)
            nonce = secrets.token_urlsafe(24)
            self._conn.execute(
                "INSERT INTO relay_idempotency VALUES (?, ?, ?, 'card', ?, ?)",
                (token_id, idempotency_key, request_hash, card_id, now),
            )
            self._conn.execute(
                """
                INSERT INTO relay_cards
                    (card_id, token_id, idempotency_key, request_hash, nonce_hash,
                     nonce_payload, action_ids, status, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'sending', ?, ?, ?)
                """,
                (
                    card_id,
                    token_id,
                    idempotency_key,
                    request_hash,
                    _sha(nonce),
                    _seal_json({"nonce": nonce}, self._key),
                    json.dumps(action_ids),
                    expires_at,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM relay_cards WHERE card_id=?", (card_id,)
            ).fetchone()
        return dict(row), nonce, True

    def register_card_message(
        self, token_id: str, idempotency_key: str, message_id: str, reply_expires_at: int
    ) -> None:
        """Give a button card a text-reply fallback by also listing it as a message.

        ponytail: the reply matcher only reads relay_messages, so without this row a
        typed reply to a card is either dropped or misattributed to another message.
        """
        now = _now_ms()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO relay_messages
                    (resource_id, token_id, idempotency_key, request_hash, kind, status,
                     message_id, reply_expires_at, created_at, updated_at)
                VALUES (?, ?, ?, '', 'card', 'sent', ?, ?, ?, ?)
                """,
                (
                    "msg_" + secrets.token_hex(12),
                    token_id,
                    idempotency_key,
                    message_id,
                    reply_expires_at,
                    now,
                    now,
                ),
            )
            self._conn.commit()

    def finish_card(self, card_id: str, message_id: str, conversation_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE relay_cards SET status='pending', message_id=?, conversation_payload=?, "
                "updated_at=? WHERE card_id=? AND status='sending'",
                (
                    message_id,
                    _seal_json({"conversation_id": conversation_id}, self._key),
                    _now_ms(),
                    card_id,
                ),
            )
            self._conn.commit()

    def fail_card(self, card_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE relay_cards SET status='failed', updated_at=? "
                "WHERE card_id=? AND status='sending'",
                (_now_ms(), card_id),
            )
            self._conn.commit()

    def card_state(self, token_id: str, card_id: str) -> dict[str, Any] | None:
        now = _now_ms()
        with self._lock:
            self._conn.execute(
                "UPDATE relay_cards SET status='expired', updated_at=? "
                "WHERE card_id=? AND token_id=? AND status='pending' AND expires_at<=?",
                (now, card_id, token_id, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM relay_cards WHERE card_id=? AND token_id=?",
                (card_id, token_id),
            ).fetchone()
        return self._public_card(dict(row)) if row else None

    @staticmethod
    def _public_card(row: dict[str, Any]) -> dict[str, Any]:
        result = {
            "card_id": str(row["card_id"]),
            "status": str(row["status"]),
            "message_id": str(row.get("message_id") or ""),
            "expires_at": int(row["expires_at"]),
        }
        if row.get("action_id"):
            result["action_id"] = str(row["action_id"])
        if row.get("close_reason"):
            result["reason"] = str(row["close_reason"])
        return result

    def action_card(
        self,
        *,
        actor_id: str,
        card_id: str,
        nonce: str,
        message_id: str,
        action_id: str,
    ) -> bool:
        token_ids = self._token_ids_for_actor(actor_id)
        if not token_ids:
            return False
        marks = ",".join("?" for _ in token_ids)
        now = _now_ms()
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM relay_cards WHERE card_id=? AND token_id IN ({marks})",
                (card_id, *token_ids),
            ).fetchone()
            if (
                row is None
                or row["status"] != "pending"
                or int(row["expires_at"]) <= now
                or not hmac.compare_digest(str(row["nonce_hash"]), _sha(nonce))
                or not hmac.compare_digest(str(row["message_id"]), message_id)
                or action_id not in json.loads(str(row["action_ids"]))
            ):
                return False
            changed = self._conn.execute(
                "UPDATE relay_cards SET status='actioned', action_id=?, updated_at=? "
                "WHERE card_id=? AND status='pending'",
                (action_id, now, card_id),
            ).rowcount
            self._conn.commit()
        return changed == 1

    def cache_card_content(self, token_id: str, message_id: str, content: dict[str, Any]) -> bool:
        """记住这条卡片消息最后一次成功 PATCH 的内容，重复点击的 ack 靠它回放。

        只在调用方 PATCH 成功之后调用 —— 于是「缓存里有内容」就等价于「内容更新过」，
        这正是 ack 带不带卡片的判断条件。发卡原文从不入缓存：终态没回来时的重复点击
        维持纯 toast（那条路被 2026-08-27 的实验矩阵证明是好的），此时贸然把原文
        塞进 ack 反而可能把旧样钉进会话流。
        """
        now = _now_ms()
        with self._lock:
            changed = self._conn.execute(
                "UPDATE relay_cards SET content_payload=?, updated_at=? "
                "WHERE message_id=? AND token_id=?",
                (_seal_json(content, self._key), now, message_id, token_id),
            ).rowcount
            self._conn.commit()
        return changed == 1

    def replay_card_content(
        self, *, actor_id: str, card_id: str, nonce: str, message_id: str
    ) -> dict[str, Any] | None:
        """重复点击时 ack 里要回放的最新内容；没有可回放的就返回 None（ack 退回纯 toast）。

        守卫与 action_card 一致（actor 归属、nonce、message_id 三重比对）——
        ingest_card_action 返回 False 也可能是伪造回调，不能凭 card_id 就把内容吐出去。
        不再要求 status=pending：重复点击发生时状态必然已经流转过了。
        """
        token_ids = self._token_ids_for_actor(actor_id)
        if not token_ids:
            return None
        marks = ",".join("?" for _ in token_ids)
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM relay_cards WHERE card_id=? AND token_id IN ({marks})",
                (card_id, *token_ids),
            ).fetchone()
        if (
            row is None
            or not row["content_payload"]
            or not hmac.compare_digest(str(row["nonce_hash"]), _sha(nonce))
            or not hmac.compare_digest(str(row["message_id"] or ""), message_id)
        ):
            return None
        try:
            return _open_json(str(row["content_payload"]), self._key)
        except Exception:
            return None

    def close_reply_window(self, token_id: str, message_id: str) -> bool:
        """Expire an open reply window now so the next window is unambiguous.

        ponytail: writes _now_ms() rather than NULL/0 — prune keys retention off
        `reply_expires_at + 24h`, and a NULL there would strand reply plaintext
        forever.
        """
        now = _now_ms()
        with self._lock:
            changed = self._conn.execute(
                "UPDATE relay_messages SET reply_expires_at=?, updated_at=? "
                "WHERE token_id=? AND message_id=? AND status='sent' AND reply_expires_at>?",
                (now, now, token_id, message_id, now),
            ).rowcount
            self._conn.commit()
        return changed == 1

    def close_card(self, token_id: str, card_id: str, reason: str) -> tuple[dict[str, Any] | None, bool]:
        now = _now_ms()
        with self._lock:
            changed = self._conn.execute(
                "UPDATE relay_cards SET status='closed', close_reason=?, updated_at=? "
                "WHERE card_id=? AND token_id=? AND status='pending' AND expires_at>?",
                (reason, now, card_id, token_id, now),
            ).rowcount
            self._conn.commit()
        return self.card_state(token_id, card_id), changed == 1

    def append_log(
        self,
        *,
        ts: int,
        level: str,
        logger: str,
        raw: str,
        event: str = "",
        status: str = "",
        actor: str = "",
        card_id: str = "",
        message_id: str = "",
    ) -> None:
        """Persist one already-redacted log line; `raw` is the journald text verbatim."""
        with self._lock:
            self._conn.execute(
                f"INSERT INTO relay_logs ({','.join(LOG_FIELDS)}) "
                f"VALUES ({','.join('?' for _ in LOG_FIELDS)})",
                (ts, level, logger, event, status, actor, card_id, message_id, raw),
            )
            self._conn.commit()

    def list_logs(
        self, since: int, until: int, limit: int = 5000, after_id: int = 0
    ) -> list[dict[str, Any]]:
        # Keyset pagination on (ts, id): a page boundary inside one millisecond can
        # still make progress — inclusive-ts-only continuation loops forever on ties.
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, {','.join(LOG_FIELDS)} FROM relay_logs "
                "WHERE (ts>? OR (ts=? AND id>?)) AND ts<=? "
                "ORDER BY ts, id LIMIT ?",
                (since, since, after_id, until, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def usage_stats(self, since: int, until: int) -> dict[str, Any]:
        """Message / card / people counts for one window, straight off the existing tables."""
        # ponytail: `request_hash != ''` drops the text-fallback rows register_card_message
        # writes for button cards — those are one card, not a second message.
        sent = "status='sent' AND request_hash!='' AND created_at>=? AND created_at<=?"
        made = "created_at>=? AND created_at<=?"
        with self._lock:
            by_kind = {
                str(row["kind"]): int(row["n"])
                for row in self._conn.execute(
                    f"SELECT kind, COUNT(*) AS n FROM relay_messages WHERE {sent} GROUP BY kind",
                    (since, until),
                ).fetchall()
            }
            by_status = {
                str(row["status"]): int(row["n"])
                for row in self._conn.execute(
                    f"SELECT status, COUNT(*) AS n FROM relay_cards WHERE {made} GROUP BY status",
                    (since, until),
                ).fetchall()
            }
            active = self._conn.execute(
                "SELECT COUNT(DISTINCT actor_fingerprint) AS n FROM relay_tokens WHERE token_id IN ("
                f"SELECT token_id FROM relay_messages WHERE {sent} "
                f"UNION SELECT token_id FROM relay_cards WHERE {made})",
                (since, until, since, until),
            ).fetchone()["n"]
            enrolled = self._conn.execute(
                "SELECT COUNT(DISTINCT actor_fingerprint) AS n FROM relay_tokens WHERE status='active'"
            ).fetchone()["n"]
        return {
            "messages": {"total": sum(by_kind.values()), "by_kind": by_kind},
            "cards": {"total": sum(by_status.values()), "by_status": by_status},
            "active_users": int(active),
            "enrolled_users": int(enrolled),
        }

    def prune(self, now_ms: int | None = None) -> int:
        now = _now_ms() if now_ms is None else now_ms
        with self._lock:
            self._conn.execute(
                "UPDATE relay_cards SET status='expired', updated_at=? "
                "WHERE status='pending' AND expires_at<=?",
                (now, now),
            )
            changed = self._conn.execute(
                """
                UPDATE relay_replies
                SET content_payload=?, purged_at=?
                WHERE purged_at IS NULL AND EXISTS (
                    SELECT 1 FROM relay_messages m
                    WHERE m.token_id=relay_replies.token_id
                      AND m.message_id=relay_replies.message_id
                      AND m.reply_expires_at + 86400000 <= ?
                )
                """,
                (_seal_json({"text": ""}, self._key), now, now),
            ).rowcount
            self._conn.execute(
                "DELETE FROM relay_logs WHERE ts < ?", (now - LOG_RETENTION_MS,)
            )
            self._conn.commit()
        return changed

    def revoke_token(self, token_id: str) -> bool:
        now = _now_ms()
        with self._lock:
            changed = self._conn.execute(
                "UPDATE relay_tokens SET status='revoked', revoked_at=? "
                "WHERE token_id=? AND status='active'",
                (now, token_id),
            ).rowcount
            self._conn.commit()
        return changed == 1
