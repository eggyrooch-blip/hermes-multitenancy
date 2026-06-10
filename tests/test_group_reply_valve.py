"""Reply-mode routing + Feishu group valve patch coverage."""
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
    table.upsert_group(
        chat_id="oc_group",
        profile_name="group_profile",
        owner_open_id="ou_owner",
    )

    with pytest.raises(ValueError):
        table.set_group_reply_mode("oc_group", "invalid")


def test_set_group_reply_mode_missing_row_is_noop(table):
    table.set_group_reply_mode("oc_missing", "all")

    assert table.get_group_reply_mode("oc_missing") == "mention"


def test_get_group_reply_mode_coerces_garbage_to_default(table):
    table.upsert_group(
        chat_id="oc_group",
        profile_name="group_profile",
        owner_open_id="ou_owner",
    )
    table._conn.execute(
        "UPDATE multitenancy_routing SET reply_mode = ? WHERE chat_id = ?",
        ("bogus", "oc_group"),
    )
    table._conn.commit()
    assert table.get_group_reply_mode("oc_group") == "mention"


def test_reply_mode_migration_adds_column_without_losing_rows(tmp_path):
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
            "group:oc_migrated",
            "group_profile",
            "",
            None,
            1,
            None,
            123,
            1,
            None,
            123,
            123,
            "group",
            "oc_migrated",
            "ou_owner",
            "迁移前群组",
        ),
    )
    conn.commit()
    conn.close()

    from hermes_multitenancy.routing import RoutingTable

    migrated = RoutingTable(str(db_path))
    try:
        cols = {
            row["name"]
            for row in migrated._conn.execute("PRAGMA table_info(multitenancy_routing)")
        }
        row = migrated.lookup_by_chat_id("oc_migrated")

        assert "reply_mode" in cols
        assert row is not None
        assert row.chat_id == "oc_migrated"
        assert migrated.get_group_reply_mode("oc_migrated") == "mention"
    finally:
        migrated.close()


def test_get_group_reply_mode_coerces_nullable_legacy_value_to_default(tmp_path):
    db_path = tmp_path / "routing-nullable.db"
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
            display_label TEXT,
            reply_mode TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO multitenancy_routing (
            user_id, profile_name, open_id, union_id, active, deleted_at,
            synced_at, version, last_active_at, created_at, updated_at,
            kind, chat_id, owner_open_id, display_label, reply_mode
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "group:oc_nullable",
            "group_profile",
            "",
            None,
            1,
            None,
            123,
            1,
            None,
            123,
            123,
            "group",
            "oc_nullable",
            "ou_owner",
            "nullable legacy row",
            None,
        ),
    )
    conn.commit()
    conn.close()

    from hermes_multitenancy.routing import RoutingTable

    migrated = RoutingTable(str(db_path))
    try:
        assert migrated.get_group_reply_mode("oc_nullable") == "mention"
    finally:
        migrated.close()


def test_should_accept_group_message_all_mode_allows_non_mention_when_policy_allows():
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.feishu_group_valve import _patch_should_accept_group_message

    class FakeAdapter:
        def __init__(self, *, allow: bool):
            self.allow = allow

        def _allow_group_message(self, sender_id, chat_id):
            return self.allow

        def _should_accept_group_message(self, message, sender_id, chat_id=""):
            if not self._allow_group_message(sender_id, chat_id):
                return False
            return bool(getattr(message, "mentioned", False))

    _patch_should_accept_group_message(FakeAdapter)
    router_mod.override_routing_table(":memory:")
    table = router_mod._get_routing_table()
    assert table is not None
    table.upsert_group(
        chat_id="oc_group",
        profile_name="group_profile",
        owner_open_id="ou_owner",
    )
    table.set_group_reply_mode("oc_group", "all")

    adapter = FakeAdapter(allow=True)
    message = SimpleNamespace(mentioned=False)

    assert adapter._should_accept_group_message(message, "ou_sender", "oc_group") is True


def test_should_accept_group_message_mention_mode_keeps_original_non_mention_rejection():
    from hermes_multitenancy.feishu_group_valve import _patch_should_accept_group_message

    class FakeAdapter:
        def _allow_group_message(self, sender_id, chat_id):
            return True

        def _should_accept_group_message(self, message, sender_id, chat_id=""):
            if not self._allow_group_message(sender_id, chat_id):
                return False
            return bool(getattr(message, "mentioned", False))

    _patch_should_accept_group_message(FakeAdapter)

    assert (
        FakeAdapter()._should_accept_group_message(
            SimpleNamespace(mentioned=False),
            "ou_sender",
            "oc_group",
        )
        is False
    )


def test_should_accept_group_message_keeps_mention_acceptance():
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.feishu_group_valve import _patch_should_accept_group_message

    class FakeAdapter:
        def __init__(self, *, allow: bool):
            self.allow = allow

        def _allow_group_message(self, sender_id, chat_id):
            return self.allow

        def _should_accept_group_message(self, message, sender_id, chat_id=""):
            if not self._allow_group_message(sender_id, chat_id):
                return False
            return bool(getattr(message, "mentioned", False))

    _patch_should_accept_group_message(FakeAdapter)
    router_mod.override_routing_table(":memory:")
    table = router_mod._get_routing_table()
    assert table is not None
    table.upsert_group(
        chat_id="oc_group",
        profile_name="group_profile",
        owner_open_id="ou_owner",
    )
    table.set_group_reply_mode("oc_group", "all")

    assert (
        FakeAdapter(allow=True)._should_accept_group_message(
            SimpleNamespace(mentioned=True),
            "ou_sender",
            "oc_group",
        )
        is True
    )
    assert (
        FakeAdapter(allow=True)._should_accept_group_message(
            SimpleNamespace(mentioned=True),
            "ou_sender",
            "oc_other",
        )
        is True
    )


def test_should_accept_group_message_all_mode_respects_policy_gate():
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.feishu_group_valve import _patch_should_accept_group_message

    class FakeAdapter:
        def __init__(self, *, allow: bool):
            self.allow = allow

        def _allow_group_message(self, sender_id, chat_id):
            return self.allow

        def _should_accept_group_message(self, message, sender_id, chat_id=""):
            if not self._allow_group_message(sender_id, chat_id):
                return False
            return bool(getattr(message, "mentioned", False))

    _patch_should_accept_group_message(FakeAdapter)
    router_mod.override_routing_table(":memory:")
    table = router_mod._get_routing_table()
    assert table is not None
    table.upsert_group(
        chat_id="oc_group",
        profile_name="group_profile",
        owner_open_id="ou_owner",
    )
    table.set_group_reply_mode("oc_group", "all")

    assert (
        FakeAdapter(allow=False)._should_accept_group_message(
            SimpleNamespace(mentioned=False),
            "ou_sender",
            "oc_group",
        )
        is False
    )


def test_should_accept_group_message_fail_open_delegates_on_exception(monkeypatch):
    from hermes_multitenancy import feishu_group_valve

    class FakeAdapter:
        def _allow_group_message(self, sender_id, chat_id):
            return True

        def _should_accept_group_message(self, message, sender_id, chat_id=""):
            if not self._allow_group_message(sender_id, chat_id):
                return False
            return bool(getattr(message, "mentioned", False))

    feishu_group_valve._patch_should_accept_group_message(FakeAdapter)
    monkeypatch.setattr(
        feishu_group_valve,
        "_get_routing_table",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert (
        FakeAdapter()._should_accept_group_message(
            SimpleNamespace(mentioned=False),
            "ou_sender",
            "oc_group",
        )
        is False
    )


def test_card_action_owner_can_switch_reply_mode_to_all():
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.feishu_group_valve import _patch_on_card_action_trigger

    class FakeAdapter:
        def _on_card_action_trigger(self, data):
            return "ORIGINAL_CALLED"

    _patch_on_card_action_trigger(FakeAdapter)
    router_mod.override_routing_table(":memory:")
    table = router_mod._get_routing_table()
    assert table is not None
    table.upsert_group(
        chat_id="oc_group",
        profile_name="group_profile",
        owner_open_id="ou_owner",
    )

    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value={
                    "hermes_action": "group_reply_mode",
                    "mode": "all",
                    "chat_id": "oc_group",
                }
            ),
            operator=SimpleNamespace(open_id="ou_owner"),
            context=SimpleNamespace(open_chat_id="oc_group"),
        )
    )

    response = FakeAdapter()._on_card_action_trigger(data)

    assert table.get_group_reply_mode("oc_group") == "all"
    assert response["kind"] == "card"
    assert response["card"]["type"] == "raw"


def test_card_action_non_owner_gets_denied_toast():
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.feishu_group_valve import _patch_on_card_action_trigger

    class FakeAdapter:
        def _on_card_action_trigger(self, data):
            return "ORIGINAL_CALLED"

    _patch_on_card_action_trigger(FakeAdapter)
    router_mod.override_routing_table(":memory:")
    table = router_mod._get_routing_table()
    assert table is not None
    table.upsert_group(
        chat_id="oc_group",
        profile_name="group_profile",
        owner_open_id="ou_owner",
    )

    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value={
                    "hermes_action": "group_reply_mode",
                    "mode": "all",
                    "chat_id": "oc_group",
                }
            ),
            operator=SimpleNamespace(open_id="ou_other"),
            context=SimpleNamespace(open_chat_id="oc_group"),
        )
    )

    response = FakeAdapter()._on_card_action_trigger(data)

    assert table.get_group_reply_mode("oc_group") == "mention"
    assert response["kind"] == "toast"
    assert "无权" in response["toast"]["content"]


def test_card_action_other_hermes_action_delegates_to_original():
    from hermes_multitenancy.feishu_group_valve import _patch_on_card_action_trigger

    class FakeAdapter:
        def _on_card_action_trigger(self, data):
            return "ORIGINAL_CALLED"

    _patch_on_card_action_trigger(FakeAdapter)
    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(value={"hermes_action": "feishu_auth"}),
            operator=SimpleNamespace(open_id="ou_owner"),
            context=SimpleNamespace(open_chat_id="oc_group"),
        )
    )

    assert FakeAdapter()._on_card_action_trigger(data) == "ORIGINAL_CALLED"


@pytest.mark.parametrize(
    ("chat_id", "table_factory"),
    [
        ("", "real"),
        ("oc_group", "none"),
    ],
)
def test_card_action_missing_chat_or_table_returns_toast(chat_id, table_factory, monkeypatch):
    from hermes_multitenancy import feishu_group_valve
    from hermes_multitenancy import router as router_mod

    class FakeAdapter:
        def _on_card_action_trigger(self, data):
            return "ORIGINAL_CALLED"

    feishu_group_valve._patch_on_card_action_trigger(FakeAdapter)
    router_mod.override_routing_table(":memory:")
    if table_factory == "none":
        monkeypatch.setattr(feishu_group_valve, "_get_routing_table", lambda: None)

    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value={
                    "hermes_action": "group_reply_mode",
                    "mode": "all",
                    "chat_id": chat_id,
                }
            ),
            operator=SimpleNamespace(open_id="ou_owner"),
            context=SimpleNamespace(open_chat_id=chat_id),
        )
    )

    response = FakeAdapter()._on_card_action_trigger(data)

    assert response["kind"] == "toast"
    assert "暂时无法保存设置" in response["toast"]["content"]
