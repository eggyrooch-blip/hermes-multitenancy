from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
from typing import Any

from .feishu_helpdesk_client import HelpdeskClient


EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s-]{9,}\d)(?!\d)")
LONG_ID_RE = re.compile(r"(?<!\d)\d{7,}(?!\d)")


def scrub_pii(text: str | None) -> str:
    if not text:
        return ""
    masked = EMAIL_RE.sub("[EMAIL]", text)
    masked = PHONE_RE.sub("[PHONE]", masked)
    masked = LONG_ID_RE.sub("[ID]", masked)
    return masked


_CJK_RUN_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]+")
_LATIN_RE = re.compile(r"[A-Za-z0-9_]+")


# Interrogative / filler phrases that carry no IT signal. Stripped from the QUERY
# only (documents keep everything). Without this, common phrases like "怎么办" match
# the many "…怎么办？" FAQ titles and drown the rare content term (adobe / wifi / 报销).
_QUERY_STOPPHRASES = (
    "怎么办", "怎么样", "怎么", "怎样", "如何", "为什么", "为啥", "咋办", "咋整", "咋弄",
    "是什么", "什么", "请问", "求助", "帮忙", "帮我", "一下", "的话", "这个", "那个",
    "呢", "吗", "啊", "哈", "求解", "急", "在线等",
)


def strip_query_stopwords(query: str) -> str:
    out = query or ""
    for phrase in _QUERY_STOPPHRASES:
        out = out.replace(phrase, " ")
    return out


def index_tokens(text: str | None) -> str:
    """Space-join searchable tokens so FTS5's default tokenizer can match CJK.

    CJK has no spaces, so the default tokenizer indexes a whole run as ONE token
    and short queries never match. We emit single chars + overlapping bigrams for
    each CJK run, and keep latin/digit words. Query and document use the SAME
    transform, so an FTS MATCH over these tokens behaves like CJK substring search.
    """
    if not text:
        return ""
    out: list[str] = [w.lower() for w in _LATIN_RE.findall(text)]
    for run in _CJK_RUN_RE.findall(text):
        chars = list(run)
        out.extend(chars)
        out.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    return " ".join(out)


class HelpdeskRagIndex:
    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self._db_path = os.fspath(db_path)
        parent = os.path.dirname(self._db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # check_same_thread=False: the broker builds the index on the aiohttp thread but
        # search() runs in an executor thread. A lock serialises access (reads are the hot
        # path; ingest writes happen separately via the CLI).
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._initialize()

    def _initialize(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS docs (
                doc_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                tags TEXT NOT NULL,
                category TEXT NOT NULL,
                status TEXT NOT NULL,
                url TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(text);
            """
        )
        self._conn.commit()

    def upsert(self, doc: dict[str, Any]) -> None:
        values = (
            str(doc.get("doc_id") or ""),
            str(doc.get("source") or ""),
            str(doc.get("title") or ""),
            str(doc.get("body") or ""),
            str(doc.get("tags") or ""),
            str(doc.get("category") or ""),
            str(doc.get("status") or ""),
            str(doc.get("url") or ""),
            str(doc.get("updated_at") or ""),
        )
        doc_id = values[0]
        # title weighted x2, then body + tags; same tokenizer as the query
        fts_text = index_tokens(" ".join((values[2], values[2], values[3], values[4])))
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO docs (doc_id, source, title, body, tags, category, status, url, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_id) DO UPDATE SET
                    source = excluded.source,
                    title = excluded.title,
                    body = excluded.body,
                    tags = excluded.tags,
                    category = excluded.category,
                    status = excluded.status,
                    url = excluded.url,
                    updated_at = excluded.updated_at
                """,
                values,
            )
            rowid = self._conn.execute(
                "SELECT rowid FROM docs WHERE doc_id = ?", (doc_id,)
            ).fetchone()[0]
            self._conn.execute("DELETE FROM docs_fts WHERE rowid = ?", (rowid,))
            self._conn.execute(
                "INSERT INTO docs_fts(rowid, text) VALUES (?, ?)", (rowid, fts_text)
            )

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        if not query or not query.strip():
            return []
        stripped = index_tokens(strip_query_stopwords(query))
        # if stripping removed everything (query was all filler), fall back to raw query
        source = stripped if stripped.strip() else index_tokens(query)
        seen: list[str] = []
        for token in source.split():
            if token not in seen:
                seen.append(token)
        if not seen:
            return []
        # OR over bigram/char tokens; bm25 ranks the doc with the most (and rarest) overlap first
        fts_query = " OR ".join(f'"{token}"' for token in seen)
        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT
                    docs.doc_id,
                    docs.source,
                    docs.title,
                    docs.body,
                    docs.tags,
                    docs.category,
                    docs.status,
                    docs.url,
                    docs.updated_at,
                    bm25(docs_fts) AS score
                FROM docs_fts
                JOIN docs ON docs.rowid = docs_fts.rowid
                WHERE docs_fts MATCH ?
                ORDER BY score ASC
                LIMIT ?
                """,
                (fts_query, max(int(k), 0)),
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
        return [dict(row) for row in rows]

    def count(self) -> int:
        with self._lock:
            cursor = self._conn.execute("SELECT COUNT(*) FROM docs")
            try:
                row = cursor.fetchone()
            finally:
                cursor.close()
        return int(row[0] if row is not None else 0)

    def close(self) -> None:
        self._conn.close()


def faq_to_doc(faq: dict[str, Any]) -> dict[str, str]:
    question = str(faq.get("question") or "")
    answer = str(faq.get("answer") or "")
    categories = faq.get("categories")
    category = ""
    if isinstance(categories, list) and categories:
        first = categories[0]
        if isinstance(first, dict):
            category = str(first.get("name") or "")
    tags = faq.get("tags")
    tag_text = " ".join(str(tag) for tag in tags) if isinstance(tags, list) else ""
    return {
        "doc_id": str(faq.get("faq_id") or ""),
        "source": "faq",
        "title": scrub_pii(question),
        "body": scrub_pii(f"{question}\n{answer}"),
        "tags": tag_text,
        "category": category,
        "status": "",
        "url": "",
        "updated_at": str(faq.get("update_time") or ""),
    }


def _message_text(message: dict[str, Any]) -> str:
    # Feishu helpdesk text messages: content is a JSON string {"content": "..."}.
    # Some payloads use "text"; accept both, else fall back to raw string.
    content = message.get("content")
    if isinstance(content, dict):
        return str(content.get("content") or content.get("text") or "")
    if isinstance(content, str):
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            return content
        if isinstance(decoded, dict):
            return str(decoded.get("content") or decoded.get("text") or "")
        if isinstance(decoded, str):
            return decoded
        return content
    return ""


# Deterministic system/onboarding templates the helpdesk bot injects into every
# ticket. They carry no problem-specific signal and, if indexed, swamp BM25 so every
# query returns greeting noise. Markers are distinctive substrings of those templates.
_TICKET_BOILERPLATE_MARKERS = (
    "欢迎来到IT服务台",
    "AI 生成式对话服务",
    "马上输入试试吧",
    "请输入 [人工]",
)


def _is_ticket_boilerplate(text: str) -> bool:
    return any(marker in text for marker in _TICKET_BOILERPLATE_MARKERS)


def ticket_to_doc(ticket: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, str]:
    texts = [
        text
        for text in (_message_text(message) for message in messages)
        if text and not _is_ticket_boilerplate(text)
    ]
    fallback_title = " ".join(part for part in (str(ticket.get("channel") or ""), str(ticket.get("status") or "")) if part)
    title = texts[0] if texts else fallback_title
    # tags carry searchable metadata; body holds the full problem→resolution thread
    tag_parts = [str(ticket.get("channel") or ""), str(ticket.get("ticket_type") or "")]
    return {
        "doc_id": str(ticket.get("ticket_id") or ""),
        "source": "ticket",
        "title": scrub_pii(title),
        "body": scrub_pii("\n".join(texts)),
        "tags": " ".join(p for p in tag_parts if p),
        "category": str(ticket.get("ticket_type") or ""),
        "status": str(ticket.get("status") or ""),
        "url": "",
        "updated_at": str(ticket.get("updated_at") or ""),
    }


def ingest_faqs(client: Any, index: HelpdeskRagIndex) -> int:
    count = 0
    for faq in client.iter_faqs(page_size=100):
        index.upsert(faq_to_doc(faq))
        count += 1
    return count


def ingest_tickets(client: Any, index: HelpdeskRagIndex, limit: int | None = None) -> int:
    count = 0
    for ticket in client.iter_tickets(page_size=100):
        if limit is not None and count >= limit:
            break
        ticket_id = str(ticket.get("ticket_id") or "")
        messages = client.get_ticket_messages(ticket_id)
        index.upsert(ticket_to_doc(ticket, messages))
        count += 1
    return count


def _default_db_path() -> str:
    return os.path.expanduser("~/.hermes/profiles/helpdesk/ticket_index.db")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hermes_multitenancy.helpdesk_rag")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser("ingest")
    ingest_parser.add_argument("--db", default=_default_db_path())
    ingest_parser.add_argument("--faqs", action="store_true")
    ingest_parser.add_argument("--tickets", action="store_true")
    ingest_parser.add_argument("--limit", type=int, default=None)

    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("--db", default=_default_db_path())
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("-k", type=int, default=5)
    return parser


def _missing_credentials() -> list[str]:
    required = ("HD_APP_ID", "HD_APP_SECRET", "HD_ID", "HD_TOKEN")
    return [name for name in required if not os.environ.get(name, "").strip()]


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "search":
        index = HelpdeskRagIndex(args.db)
        try:
            for rank, result in enumerate(index.search(args.query, k=args.k), start=1):
                print(f"{rank}. [{result['score']}] {result['doc_id']} {result['title']}")
            return 0
        finally:
            index.close()

    missing = _missing_credentials()
    if missing:
        print(
            f"Missing required environment variables: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1

    client = HelpdeskClient(
        app_id=os.environ["HD_APP_ID"],
        app_secret=os.environ["HD_APP_SECRET"],
        helpdesk_id=os.environ["HD_ID"],
        helpdesk_token=os.environ["HD_TOKEN"],
    )
    index = HelpdeskRagIndex(args.db)
    try:
        ingest_faqs_selected = bool(args.faqs)
        ingest_tickets_selected = bool(args.tickets)
        if not ingest_faqs_selected and not ingest_tickets_selected:
            ingest_faqs_selected = True
            ingest_tickets_selected = True

        if ingest_faqs_selected:
            faq_count = ingest_faqs(client, index)
            print(f"FAQs ingested: {faq_count}")
        if ingest_tickets_selected:
            ticket_count = ingest_tickets(client, index, limit=args.limit)
            print(f"Tickets ingested: {ticket_count}")
        return 0
    finally:
        index.close()


if __name__ == "__main__":
    raise SystemExit(main())
