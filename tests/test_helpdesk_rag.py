from __future__ import annotations

from pathlib import Path

import pytest

from hermes_multitenancy.helpdesk_rag import (
    HelpdeskRagIndex,
    faq_to_doc,
    ingest_faqs,
    ingest_tickets,
    main,
    scrub_pii,
    ticket_to_doc,
)


class FakeClient:
    def __init__(self) -> None:
        self.ticket_ids: list[str] = []

    def iter_faqs(self, page_size: int = 100) -> list[dict]:
        assert page_size == 100
        return [
            {
                "faq_id": "faq-1",
                "question": "How do I reset VPN?",
                "answer": "Email help@example.com or call 13800138000 with ticket 123456789.",
                "categories": [{"name": "VPN"}],
                "tags": ["vpn", "reset"],
                "update_time": "2024-06-01T00:00:00Z",
            }
        ]

    def iter_tickets(self, page_size: int = 100) -> list[dict]:
        assert page_size == 100
        return [
            {
                "ticket_id": "ticket-1",
                "status": "closed",
                "channel": "feishu",
                "updated_at": "2024-06-02T00:00:00Z",
            }
        ]

    def get_ticket_messages(self, ticket_id: str) -> list[dict]:
        self.ticket_ids.append(ticket_id)
        return [
            {"message_id": "m-1", "content": '{"text":"VPN reset steps"}'},
            {"message_id": "m-2", "content": "Call me at 13800138000"},
        ]


def test_scrub_pii_masks_email_phone_and_long_ids() -> None:
    masked = scrub_pii("mail help@example.com phone 13800138000 order 123456789")

    assert "help@example.com" not in masked
    assert "13800138000" not in masked
    assert "123456789" not in masked
    assert "[EMAIL]" in masked
    assert "[PHONE]" in masked
    assert "[ID]" in masked


def test_upsert_search_and_idempotent_update(tmp_path: Path) -> None:
    index = HelpdeskRagIndex(tmp_path / "tickets.db")
    try:
        index.upsert(
            {
                "doc_id": "faq-1",
                "source": "faq",
                "title": "Reset VPN",
                "body": "Use the VPN portal to reset your password.",
                "tags": "vpn reset",
                "category": "Network",
                "status": "",
                "url": "https://example.invalid/faq-1",
                "updated_at": "2024-06-01T00:00:00Z",
            }
        )
        index.upsert(
            {
                "doc_id": "ticket-1",
                "source": "ticket",
                "title": "Laptop cannot connect",
                "body": "The laptop fails wifi login after password reset.",
                "tags": "wifi laptop",
                "category": "",
                "status": "open",
                "url": "",
                "updated_at": "2024-06-02T00:00:00Z",
            }
        )

        results = index.search("vpn password reset", k=2)
        assert results[0]["doc_id"] == "faq-1"
        assert "score" in results[0]

        index.upsert(
            {
                "doc_id": "faq-1",
                "source": "faq",
                "title": "Reset VPN",
                "body": "Updated VPN reset procedure.",
                "tags": "vpn reset",
                "category": "Network",
                "status": "",
                "url": "https://example.invalid/faq-1",
                "updated_at": "2024-06-03T00:00:00Z",
            }
        )

        assert index.count() == 2
        updated = index.search("updated procedure", k=1)
        assert updated[0]["body"] == "Updated VPN reset procedure."
    finally:
        index.close()


def test_search_handles_empty_query_and_no_results(tmp_path: Path) -> None:
    index = HelpdeskRagIndex(tmp_path / "tickets.db")
    try:
        assert index.search("") == []
        assert index.search("nothing here") == []
    finally:
        index.close()


def test_faq_to_doc_and_ticket_to_doc_shapes() -> None:
    faq_doc = faq_to_doc(
        {
            "faq_id": "faq-1",
            "question": "Reset VPN",
            "answer": "Email help@example.com",
            "categories": [{"name": "VPN"}],
            "tags": ["vpn", "reset"],
            "update_time": "2024-06-01T00:00:00Z",
        }
    )
    ticket_doc = ticket_to_doc(
        {"ticket_id": "ticket-1", "status": "closed", "channel": "feishu", "updated_at": "2024-06-02T00:00:00Z"},
        [
            {"message_id": "m-1", "content": '{"text":"Reset VPN now"}'},
            {"message_id": "m-2", "content": "email help@example.com"},
        ],
    )

    assert faq_doc["doc_id"] == "faq-1"
    assert faq_doc["source"] == "faq"
    assert faq_doc["category"] == "VPN"
    assert "[EMAIL]" in faq_doc["body"]
    assert ticket_doc["doc_id"] == "ticket-1"
    assert ticket_doc["source"] == "ticket"
    assert ticket_doc["status"] == "closed"
    assert "[EMAIL]" in ticket_doc["body"]


def test_ingest_functions_pull_transform_and_upsert(tmp_path: Path) -> None:
    client = FakeClient()
    index = HelpdeskRagIndex(tmp_path / "tickets.db")
    try:
        faq_count = ingest_faqs(client, index)
        ticket_count = ingest_tickets(client, index, limit=1)

        assert faq_count == 1
        assert ticket_count == 1
        assert client.ticket_ids == ["ticket-1"]
        assert index.count() == 2
    finally:
        index.close()


def test_cli_help_prints_usage_without_env(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    captured = capsys.readouterr()
    assert excinfo.value.code == 0
    assert "usage:" in captured.out
    assert "ingest" in captured.out
    assert "search" in captured.out


def test_cli_ingest_requires_credentials(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    for key in ("HD_APP_ID", "HD_APP_SECRET", "HD_ID", "HD_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    exit_code = main(["ingest", "--db", str(tmp_path / "tickets.db"), "--faqs"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Missing required environment variables" in captured.err


def test_search_matches_chinese_faq(tmp_path):
    """CJK regression: bigram index must return the relevant Chinese FAQ."""
    from hermes_multitenancy.helpdesk_rag import HelpdeskRagIndex
    idx = HelpdeskRagIndex(str(tmp_path / "cjk.db"))
    idx.upsert({"doc_id": "f1", "source": "faq", "title": "如何申请办公、研发、设计软件？",
                "body": "在 IT 服务台提交软件申请单，审批通过后安装。", "tags": "软件,申请",
                "category": "IT", "status": "", "url": "", "updated_at": ""})
    idx.upsert({"doc_id": "f2", "source": "faq", "title": "如何重置密码？",
                "body": "访问账号中心，点击忘记密码重置。", "tags": "密码,账号",
                "category": "IT", "status": "", "url": "", "updated_at": ""})
    idx.upsert({"doc_id": "f3", "source": "faq", "title": "VPN 连不上怎么办？",
                "body": "检查网络后重连 VPN 客户端。", "tags": "VPN,网络",
                "category": "IT", "status": "", "url": "", "updated_at": ""})
    assert idx.count() == 3
    r1 = idx.search("办公软件申请", k=3)
    assert r1 and r1[0]["doc_id"] == "f1"
    r2 = idx.search("密码重置", k=3)
    assert r2 and r2[0]["doc_id"] == "f2"
    r3 = idx.search("VPN", k=3)
    assert r3 and r3[0]["doc_id"] == "f3"


def test_ticket_to_doc_parses_content_and_strips_boilerplate():
    """Real Feishu msg content is JSON {"content": ...}; bot greeting is dropped."""
    from hermes_multitenancy.helpdesk_rag import ticket_to_doc
    messages = [
        {"content": '{"content":"HI，欢迎来到IT服务台，AI 生成式对话服务，马上输入试试吧！"}'},
        {"content": '{"content":"adboe安装问题"}'},
        {"content": '{"content":"已发送安装包，请重启后安装。"}'},
    ]
    doc = ticket_to_doc({"ticket_id": "t1", "status": "closed", "channel": "feishu"}, messages)
    assert doc["title"] == "adboe安装问题"            # boilerplate greeting NOT the title
    assert "欢迎来到IT服务台" not in doc["body"]        # boilerplate stripped from body
    assert "安装包" in doc["body"]                      # real content kept, clean (not JSON wrapper)
    assert '{"content"' not in doc["body"]


def test_query_stopwords_dont_drown_rare_term(tmp_path):
    """'adobe装不上怎么办' must still rank the adobe doc first despite filler '怎么办'."""
    from hermes_multitenancy.helpdesk_rag import HelpdeskRagIndex
    idx = HelpdeskRagIndex(str(tmp_path / "sw.db"))
    idx.upsert({"doc_id": "a", "source": "faq", "title": "adobe全家桶软件安装失败", "body": "替换旧版本即可", "tags": "adobe", "category": "", "status": "", "url": "", "updated_at": ""})
    # decoys share ONLY the filler '怎么办' with the query — no content-word overlap
    for i, t in enumerate(["会议室扫码签到失败怎么办？", "食堂几点开门怎么办？", "班车时刻表查询怎么办？"]):
        idx.upsert({"doc_id": f"d{i}", "source": "faq", "title": t, "body": t, "tags": "", "category": "", "status": "", "url": "", "updated_at": ""})
    # filler '怎么办' + rare content 'adobe/安装' — stopword strip keeps adobe on top
    r = idx.search("adobe 安装失败怎么办", k=3)
    assert r and r[0]["doc_id"] == "a"


def test_event_handler_drops_non_allowed_helpdesk(tmp_path):
    """Hard safety: events from a non-allowlisted helpdesk are dropped, never answered."""
    import json
    from hermes_multitenancy.helpdesk_rag import HelpdeskRagIndex
    from hermes_multitenancy import feishu_helpdesk_event as evt
    idx = HelpdeskRagIndex(str(tmp_path / "e.db"))
    idx.upsert({"doc_id": "a", "source": "faq", "title": "x", "body": "y", "tags": "", "category": "", "status": "", "url": "", "updated_at": ""})
    def mk(hid):
        return {"header": {"event_type": "helpdesk.ticket_message.created_v1"},
                "event": {"ticket": {"ticket_id": "T", "helpdesk_id": hid},
                          "message": {"content": json.dumps({"content": "hi"}), "user_id": "u"}}}
    # real IT helpdesk -> drop, composer never called
    called = {"n": 0}
    def composer(q, h): called["n"] += 1; return "X"
    r = evt.handle_helpdesk_event(mk("6909040876777979905"), index=idx, composer=composer, post=False)
    assert r["action"] == "drop"
    assert called["n"] == 0  # never composed/answered for the wrong helpdesk
    # allowed test helpdesk -> draft, composer called
    r2 = evt.handle_helpdesk_event(mk("7651445701632691164"), index=idx, composer=composer, post=False)
    assert r2["action"] == "draft" and called["n"] == 1 and r2["posted"] is False
