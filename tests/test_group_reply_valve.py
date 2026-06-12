"""Reply-mode storage + Feishu group valve patch coverage.

reply_mode lives in its OWN table (multitenancy_group_reply_mode), decoupled
from the group routing row, so get/set work the instant the bot is added —
before the routing row is provisioned on the first group message.

The valve patches two gate shapes:
  - prod/upstream: _require_mention_for(chat_id) -> bool  ('all' => False)
  - fork/local:    _should_accept_group_message(message, sender_id, chat_id)
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest


@pytest.fixture
def table():
    from hermes_multitenancy.routing import RoutingTable

    t = RoutingTable(":memory:")
    yield t
    t.close()


# --------------------------------------------------------------------------- #
# Storage: dedicated table, decoupled from routing-row provisioning
# --------------------------------------------------------------------------- #

def test_set_group_reply_mode_round_trip(table):
    table.upsert_group(
        chat_id="oc_group",
        profile_name="group_profile",
        owner_open_id="ou_owner",
    )
    table.set_group_reply_mode("oc_group", "all")
    assert table.get_group_reply_mode("oc_group") == "all"


def test_get_group_reply_mode_defaults_to_mention_for_unknown_chat(table):
    assert table.get_group_reply_mode("oc_missing") == "mention"


def test_set_group_reply_mode_rejects_invalid_mode(table):
    with pytest.raises(ValueError):
        table.set_group_reply_mode("oc_group", "invalid")


def test_set_group_reply_mode_works_before_group_row_exists(table):
    """Regression (review finding 2): the owner can flip the valve from the
    welcome card right after bot-added — before any group message provisions
    the routing row. The old row-column design silently no-oped here."""
    assert table.lookup_by_chat_id("oc_unprovisioned") is None
    table.set_group_reply_mode("oc_unprovisioned", "all")
    assert table.get_group_reply_mode("oc_unprovisioned") == "all"


def test_set_group_reply_mode_upsert_overwrites(table):
    table.set_group_reply_mode("oc_group", "all")
    table.set_group_reply_mode("oc_group", "mention")
    assert table.get_group_reply_mode("oc_group") == "mention"


def test_get_group_reply_mode_coerces_garbage_to_default(table):
    table._conn.execute(
        "INSERT INTO multitenancy_group_reply_mode (chat_id, mode, updated_at) "
        "VALUES (?, ?, ?)",
        ("oc_group", "bogus", 123),
    )
    table._conn.commit()
    assert table.get_group_reply_mode("oc_group") == "mention"


def test_reply_mode_table_created_on_old_db_without_losing_rows(tmp_path):
    """Opening an old (pre-reply-mode) DB must create the new table via the
    idempotent CREATE TABLE IF NOT EXISTS schema and keep existing group rows."""
    db_path = tmp_path / "routing-old.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE multitenancy_routing (
            user_id TEXT PRIMARY KEY NOT NULL,
            profile_name TEXT NOT NULL,
            open_id TEXT,
            union_id TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            deleted_at INTEGER,
            synced_at INTEGER NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            last_active_at INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'user',
            chat_id TEXT,
            owner_open_id TEXT,
            display_label TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO multitenancy_routing (
            user_id, profile_name, open_id, union_id, active, deleted_at,
            synced_at, version, last_active_at, created_at, updated_at,
            kind, chat_id, owner_open_id, display_label
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "group:oc_migrated", "group_profile", "", None, 1, None,
            123, 1, None, 123, 123, "group", "oc_migrated", "ou_owner", "迁移前群组",
        ),
    )
    conn.commit()
    conn.close()

    from hermes_multitenancy.routing import RoutingTable

    migrated = RoutingTable(str(db_path))
    try:
        tables = {
            row["name"]
            for row in migrated._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        row = migrated.lookup_by_chat_id("oc_migrated")
        assert "multitenancy_group_reply_mode" in tables
        assert row is not None
        assert row.chat_id == "oc_migrated"
        assert migrated.get_group_reply_mode("oc_migrated") == "mention"
        # And the valve can be flipped on the migrated DB.
        migrated.set_group_reply_mode("oc_migrated", "all")
        assert migrated.get_group_reply_mode("oc_migrated") == "all"
    finally:
        migrated.close()


# --------------------------------------------------------------------------- #
# Valve: prod gate _require_mention_for(chat_id) -> bool
# --------------------------------------------------------------------------- #

def test_require_mention_for_all_mode_returns_false():
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.feishu_group_valve import _patch_require_mention_for

    class FakeAdapter:
        def _require_mention_for(self, chat_id=""):
            return True  # core default: require mention

    _patch_require_mention_for(FakeAdapter)
    router_mod.override_routing_table(":memory:")
    table = router_mod._get_routing_table()
    assert table is not None
    table.set_group_reply_mode("oc_group", "all")

    # 'all' group => valve makes require_mention False (=> _admit admits non-@).
    assert FakeAdapter()._require_mention_for("oc_group") is False
    # untouched chats keep the core's answer.
    assert FakeAdapter()._require_mention_for("oc_other") is True


def test_require_mention_for_mention_mode_delegates_to_original():
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.feishu_group_valve import _patch_require_mention_for

    class FakeAdapter:
        def _require_mention_for(self, chat_id=""):
            return True

    _patch_require_mention_for(FakeAdapter)
    router_mod.override_routing_table(":memory:")
    table = router_mod._get_routing_table()
    assert table is not None
    table.set_group_reply_mode("oc_group", "mention")
    # default mode (no row) and explicit 'mention' both keep core behavior.
    assert FakeAdapter()._require_mention_for("oc_group") is True
    assert FakeAdapter()._require_mention_for("oc_unknown") is True


def test_require_mention_for_fail_open_delegates_on_exception(monkeypatch):
    from hermes_multitenancy import feishu_group_valve

    class FakeAdapter:
        def _require_mention_for(self, chat_id=""):
            return True

    feishu_group_valve._patch_require_mention_for(FakeAdapter)
    monkeypatch.setattr(
        feishu_group_valve,
        "_get_routing_table",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    # On any internal failure the valve must yield the core's original answer.
    assert FakeAdapter()._require_mention_for("oc_group") is True


def test_require_mention_for_patch_idempotent():
    from hermes_multitenancy.feishu_group_valve import _patch_require_mention_for

    class FakeAdapter:
        def _require_mention_for(self, chat_id=""):
            return True

    _patch_require_mention_for(FakeAdapter)
    first = FakeAdapter._require_mention_for
    _patch_require_mention_for(FakeAdapter)
    assert FakeAdapter._require_mention_for is first


# --------------------------------------------------------------------------- #
# Valve: fork gate _should_accept_group_message (kept for local/fork cores)
# --------------------------------------------------------------------------- #

class _ForkAdapter:
    def __init__(self, *, allow: bool = True):
        self.allow = allow

    def _allow_group_message(self, sender_id, chat_id):
        return self.allow

    def _should_accept_group_message(self, message, sender_id, chat_id=""):
        if not self._allow_group_message(sender_id, chat_id):
            return False
        return bool(getattr(message, "mentioned", False))


def _fork_with_mode(mode: str):
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.feishu_group_valve import _patch_should_accept_group_message

    _patch_should_accept_group_message(_ForkAdapter)
    router_mod.override_routing_table(":memory:")
    table = router_mod._get_routing_table()
    assert table is not None
    if mode:
        table.set_group_reply_mode("oc_group", mode)
    return table


def test_should_accept_all_mode_allows_non_mention_when_policy_allows():
    _fork_with_mode("all")
    assert _ForkAdapter(allow=True)._should_accept_group_message(
        SimpleNamespace(mentioned=False), "ou_sender", "oc_group"
    ) is True


def test_should_accept_mention_mode_keeps_non_mention_rejection():
    from hermes_multitenancy.feishu_group_valve import _patch_should_accept_group_message

    _patch_should_accept_group_message(_ForkAdapter)
    assert _ForkAdapter()._should_accept_group_message(
        SimpleNamespace(mentioned=False), "ou_sender", "oc_group"
    ) is False


def test_should_accept_keeps_mention_acceptance():
    _fork_with_mode("all")
    assert _ForkAdapter(allow=True)._should_accept_group_message(
        SimpleNamespace(mentioned=True), "ou_sender", "oc_group"
    ) is True


def test_should_accept_all_mode_respects_policy_gate():
    _fork_with_mode("all")
    assert _ForkAdapter(allow=False)._should_accept_group_message(
        SimpleNamespace(mentioned=False), "ou_sender", "oc_group"
    ) is False


def test_should_accept_fail_open_delegates_on_exception(monkeypatch):
    from hermes_multitenancy import feishu_group_valve

    feishu_group_valve._patch_should_accept_group_message(_ForkAdapter)
    monkeypatch.setattr(
        feishu_group_valve,
        "_get_routing_table",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert _ForkAdapter()._should_accept_group_message(
        SimpleNamespace(mentioned=False), "ou_sender", "oc_group"
    ) is False


# --------------------------------------------------------------------------- #
# Valve: prod gate _mentions_self — @everyone (@_all) must NOT wake the bot
# --------------------------------------------------------------------------- #

class _MentionAdapter:
    """Mimics core _mentions_self: any @_all in raw content => True (the bug)."""

    def __init__(self, bot_open_id: str = "ou_bot"):
        self._bot_open_id = bot_open_id
        self._bot_user_id = ""
        self._bot_name = ""

    def _bot_identity(self):
        return SimpleNamespace(open_id=self._bot_open_id, user_id="", name="")

    def _message_mentions_bot(self, mentions):
        for m in mentions:
            mid = getattr(m, "id", None)
            if mid is not None and getattr(mid, "open_id", "") == self._bot_open_id:
                return True
        return False

    def _post_mentions_bot(self, mentions):
        return any(getattr(m, "is_self", False) for m in mentions)

    def _mentions_self(self, message):
        raw = getattr(message, "content", "") or ""
        if "@_all" in raw:  # core's @everyone shortcut — the bug we neutralize
            return True
        mentions = getattr(message, "mentions", None) or []
        return bool(mentions and self._message_mentions_bot(mentions))


def _bot_mention_ref(open_id: str = "ou_bot"):
    return SimpleNamespace(id=SimpleNamespace(open_id=open_id))


def _patch_mentions(monkeypatch):
    """Install the valve and stub the core post-normalizer (no gateway import)."""
    from hermes_multitenancy import feishu_group_valve

    feishu_group_valve._patch_mentions_self(_MentionAdapter)
    monkeypatch.setattr(
        feishu_group_valve,
        "_load_normalize",
        lambda: (lambda **_: SimpleNamespace(mentions=[])),
    )


def test_mentions_self_ignores_at_all_broadcast(monkeypatch):
    """Regression: an @所有人 (@_all) broadcast — e.g. a bot notification — must
    NOT count as mentioning the bot. Without the valve the core returns True and
    the bot wrongly replies in a mention-only group."""
    _patch_mentions(monkeypatch)
    msg = SimpleNamespace(content='{"text":"@_all 全员通知"}', mentions=[], message_type="text")
    assert _MentionAdapter()._mentions_self(msg) is False


def test_mentions_self_keeps_genuine_mention_even_with_at_all(monkeypatch):
    """A real @bot still wakes the bot, even if @_all is also present."""
    _patch_mentions(monkeypatch)
    msg = SimpleNamespace(
        content='{"text":"@_all @bot 看下"}',
        mentions=[_bot_mention_ref("ou_bot")],
        message_type="text",
    )
    assert _MentionAdapter()._mentions_self(msg) is True


def test_mentions_self_keeps_plain_bot_mention(monkeypatch):
    _patch_mentions(monkeypatch)
    msg = SimpleNamespace(
        content='{"text":"@bot hi"}',
        mentions=[_bot_mention_ref("ou_bot")],
        message_type="text",
    )
    assert _MentionAdapter()._mentions_self(msg) is True


def test_mentions_self_no_mention_stays_false(monkeypatch):
    _patch_mentions(monkeypatch)
    msg = SimpleNamespace(content='{"text":"普通消息"}', mentions=[], message_type="text")
    assert _MentionAdapter()._mentions_self(msg) is False


def test_mentions_self_fail_open_on_exception(monkeypatch):
    from hermes_multitenancy import feishu_group_valve

    feishu_group_valve._patch_mentions_self(_MentionAdapter)
    # Genuine-check blows up => valve must yield the core's original answer.
    monkeypatch.setattr(
        feishu_group_valve,
        "_genuinely_mentions_bot",
        lambda *_: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    msg = SimpleNamespace(content='{"text":"@_all x"}', mentions=[], message_type="text")
    assert _MentionAdapter()._mentions_self(msg) is True


def test_mentions_self_patch_idempotent():
    from hermes_multitenancy.feishu_group_valve import _patch_mentions_self

    class A:
        def _mentions_self(self, message):
            return True

    _patch_mentions_self(A)
    first = A._mentions_self
    _patch_mentions_self(A)
    assert A._mentions_self is first


# --------------------------------------------------------------------------- #
# Card action callback (owner-gated, via provisioned row OR pending inviter)
# --------------------------------------------------------------------------- #

class _CardAdapter:
    def _on_card_action_trigger(self, data):
        return "ORIGINAL_CALLED"


def _card_data(*, operator_open_id: str, mode: str = "all", chat_id: str = "oc_group"):
    return SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value={
                    "hermes_action": "group_reply_mode",
                    "mode": mode,
                    "chat_id": chat_id,
                }
            ),
            operator=SimpleNamespace(open_id=operator_open_id),
            context=SimpleNamespace(open_chat_id=chat_id),
        )
    )


def test_card_action_owner_can_switch_via_provisioned_row():
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.feishu_group_valve import _patch_on_card_action_trigger

    _patch_on_card_action_trigger(_CardAdapter)
    router_mod.override_routing_table(":memory:")
    table = router_mod._get_routing_table()
    assert table is not None
    table.upsert_group(
        chat_id="oc_group", profile_name="group_profile", owner_open_id="ou_owner"
    )

    response = _CardAdapter()._on_card_action_trigger(_card_data(operator_open_id="ou_owner"))
    assert table.get_group_reply_mode("oc_group") == "all"
    assert response["kind"] == "card"
    assert response["card"]["type"] == "raw"


def test_card_action_owner_can_switch_via_pending_inviter_before_provision():
    """Regression (review finding 2): owner taps the card immediately after
    bot-added, before the group row exists. The inviter is in the pending
    table; the owner check must honor it instead of denying the real owner."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.feishu_group_valve import _patch_on_card_action_trigger

    _patch_on_card_action_trigger(_CardAdapter)
    router_mod.override_routing_table(":memory:")
    table = router_mod._get_routing_table()
    assert table is not None
    # No provisioned group row yet — only the pending inviter capture exists.
    assert table.lookup_by_chat_id("oc_group") is None
    table.put_pending_inviter("oc_group", "ou_owner")

    response = _CardAdapter()._on_card_action_trigger(_card_data(operator_open_id="ou_owner"))
    assert table.get_group_reply_mode("oc_group") == "all"
    assert response["kind"] == "card"


def test_card_action_non_owner_gets_denied_toast():
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.feishu_group_valve import _patch_on_card_action_trigger

    _patch_on_card_action_trigger(_CardAdapter)
    router_mod.override_routing_table(":memory:")
    table = router_mod._get_routing_table()
    assert table is not None
    table.upsert_group(
        chat_id="oc_group", profile_name="group_profile", owner_open_id="ou_owner"
    )

    response = _CardAdapter()._on_card_action_trigger(_card_data(operator_open_id="ou_other"))
    assert table.get_group_reply_mode("oc_group") == "mention"
    assert response["kind"] == "toast"
    assert "无权" in response["toast"]["content"]


def test_card_action_denied_when_no_owner_known_at_all():
    """If neither a provisioned row nor a pending inviter is known, nobody is
    authorized (fail-closed on the permission check)."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.feishu_group_valve import _patch_on_card_action_trigger

    _patch_on_card_action_trigger(_CardAdapter)
    router_mod.override_routing_table(":memory:")
    table = router_mod._get_routing_table()
    assert table is not None

    response = _CardAdapter()._on_card_action_trigger(_card_data(operator_open_id="ou_someone"))
    assert table.get_group_reply_mode("oc_group") == "mention"
    assert response["kind"] == "toast"
    assert "无权" in response["toast"]["content"]


def test_card_action_other_hermes_action_delegates_to_original():
    from hermes_multitenancy.feishu_group_valve import _patch_on_card_action_trigger

    _patch_on_card_action_trigger(_CardAdapter)
    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(value={"hermes_action": "feishu_auth"}),
            operator=SimpleNamespace(open_id="ou_owner"),
            context=SimpleNamespace(open_chat_id="oc_group"),
        )
    )
    assert _CardAdapter()._on_card_action_trigger(data) == "ORIGINAL_CALLED"


@pytest.mark.parametrize(
    ("chat_id", "table_factory"),
    [("", "real"), ("oc_group", "none")],
)
def test_card_action_missing_chat_or_table_returns_toast(chat_id, table_factory, monkeypatch):
    from hermes_multitenancy import feishu_group_valve
    from hermes_multitenancy import router as router_mod

    feishu_group_valve._patch_on_card_action_trigger(_CardAdapter)
    router_mod.override_routing_table(":memory:")
    if table_factory == "none":
        monkeypatch.setattr(feishu_group_valve, "_get_routing_table", lambda: None)

    response = _CardAdapter()._on_card_action_trigger(
        _card_data(operator_open_id="ou_owner", chat_id=chat_id)
    )
    assert response["kind"] == "toast"
    assert "暂时无法保存设置" in response["toast"]["content"]
