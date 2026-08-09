import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path
import time
from types import SimpleNamespace as NS

import pytest

from hermes_multitenancy import trusted_feishu_ingress as ingress
from hermes_multitenancy import router
from hermes_multitenancy.routing import RoutingTable


@dataclass(frozen=True)
class FakeTicket:
    actor_id: str
    event_key: str
    account_id: str = "cli_trusted"
    namespace: str = "feishu:test"
    signature: str = "signed"
    actor_id_type: str = "open_id"
    principal_kind: str = "human"
    event_kind: str = "message"
    chat_id: str = "oc_dm"
    message_id: str = "om_1"
    issued_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 299)
    valid: bool = True

    def is_valid(self, *, account_id: str) -> bool:
        return self.valid and self.account_id == account_id


@pytest.fixture
def routes(tmp_path, monkeypatch):
    table = RoutingTable(":memory:")
    table.upsert(user_id="u_a", profile_name="profile_a", open_id="ou_a", union_id="on_a")
    table.upsert(user_id="u_b", profile_name="profile_b", open_id="ou_b", union_id="on_b")
    for profile in ("profile_a", "profile_b"):
        (tmp_path / profile).mkdir()
    monkeypatch.setattr(router, "_routing_table", table)
    monkeypatch.setattr(router, "_profile_name_to_home", lambda profile: tmp_path / profile)
    monkeypatch.setattr(
        ingress,
        "load_feishu_module",
        lambda: NS(TrustedFeishuIngressTicket=FakeTicket, FeishuAdapter=FakeAdapter),
    )
    monkeypatch.setattr(
        ingress,
        "load_live_feishu_module",
        lambda: NS(TrustedFeishuIngressTicket=FakeTicket, FeishuAdapter=FakeAdapter),
    )
    ingress._reset_seen_for_tests()
    yield table
    table.close()


class FakeAdapter:
    _app_id = "cli_trusted"
    _trusted_ingress_admitter = None


def test_two_identities_bind_to_themselves_with_zero_cross_match(routes):
    first = ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket("ou_a", "evt_a"), adapter=FakeAdapter()
    )
    second = ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket("ou_b", "evt_b"), adapter=FakeAdapter()
    )

    assert first and second
    assert [(first.profile_name, first.credential_subject), (second.profile_name, second.credential_subject)] == [
        ("profile_a", "ou_a"),
        ("profile_b", "ou_b"),
    ]
    assert first.tool_scope == second.tool_scope == "feishu:user"


def test_group_route_binds_bot_scope(routes, tmp_path):
    routes.upsert_group(
        chat_id="oc_group",
        profile_name="profile_group",
        owner_open_id="ou_a",
        display_label="group",
    )
    (tmp_path / "profile_group").mkdir()

    admission = ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket("ou_b", "evt_group", chat_id="oc_group"),
        adapter=FakeAdapter(),
    )

    assert admission
    assert admission.profile_name == "profile_group"
    assert admission.credential_subject == "cli_trusted"
    assert admission.tool_scope == "feishu:bot"


@pytest.mark.parametrize(
    ("actor_id", "actor_id_type"),
    [("on_a", "union_id"), ("u_a", "user_id")],
)
def test_schema2_aliases_resolve_to_the_canonical_credential_subject(routes, actor_id, actor_id_type):
    admission = ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket(actor_id, f"evt_{actor_id_type}", actor_id_type=actor_id_type),
        adapter=FakeAdapter(),
    )

    assert admission
    assert admission.profile_name == "profile_a"
    assert admission.credential_subject == "ou_a"


@pytest.mark.parametrize(
    "ticket",
    [
        FakeTicket("ou_missing", "evt_missing"),
        FakeTicket("ou_a", "evt_bot", principal_kind="bot"),
        FakeTicket("ou_a", "evt_comment", event_kind="comment"),
        FakeTicket("ou_a", "evt_vc", event_kind="vc"),
        FakeTicket("ou_a", "evt_invalid", valid=False),
        FakeTicket("ou_a", "evt_account", account_id="cli_other"),
    ],
)
def test_missing_mismatched_or_unbridged_ticket_is_denied(routes, ticket):
    assert ingress.admit_trusted_feishu_ingress(ticket=ticket, adapter=FakeAdapter()) is None


def test_duplicate_event_is_denied(routes):
    ticket = FakeTicket("ou_a", "evt_duplicate")
    assert ingress.admit_trusted_feishu_ingress(ticket=ticket, adapter=FakeAdapter())
    assert ingress.admit_trusted_feishu_ingress(ticket=ticket, adapter=FakeAdapter()) is None


def test_stale_or_future_ticket_is_denied(routes):
    now = time.time()
    stale = FakeTicket(
        "ou_a",
        "evt_stale",
        issued_at=now - 301,
        expires_at=now + 1,
    )
    future = FakeTicket(
        "ou_a",
        "evt_future",
        issued_at=now + 31,
        expires_at=now + 60,
    )

    assert ingress.admit_trusted_feishu_ingress(ticket=stale, adapter=FakeAdapter()) is None
    assert ingress.admit_trusted_feishu_ingress(ticket=future, adapter=FakeAdapter()) is None


def test_pre_dispatch_rechecks_credential_and_tool_scope(routes):
    ticket = FakeTicket("ou_a", "evt_scope")
    admission = ingress.admit_trusted_feishu_ingress(ticket=ticket, adapter=FakeAdapter())
    event = NS(
        source=NS(
            platform="feishu",
            user_id="ou_a",
            user_id_alt=None,
            chat_id=ticket.chat_id,
            chat_type="p2p",
        ),
        message_id=ticket.message_id,
        trusted_feishu_ingress_ticket=ticket,
        trusted_feishu_ingress_admission=admission,
    )

    assert ingress.validate_admitted_feishu_event(event)
    event.trusted_feishu_ingress_admission = replace(admission, credential_subject="ou_b")
    assert not ingress.validate_admitted_feishu_event(event)
    event.trusted_feishu_ingress_admission = replace(admission, tool_scope="feishu:bot")
    assert not ingress.validate_admitted_feishu_event(event)


def test_cross_actor_source_is_denied(routes):
    ticket = FakeTicket("ou_a", "evt_cross_actor")
    admission = ingress.admit_trusted_feishu_ingress(ticket=ticket, adapter=FakeAdapter())
    event = NS(
        source=NS(
            platform="feishu",
            user_id="ou_b",
            user_id_alt=None,
            chat_id=ticket.chat_id,
            chat_type="p2p",
        ),
        message_id=ticket.message_id,
        trusted_feishu_ingress_ticket=ticket,
        trusted_feishu_ingress_admission=admission,
    )

    assert not ingress.validate_admitted_feishu_event(event)


def test_unknown_group_does_not_fall_back_to_user_route(routes):
    ticket = FakeTicket("ou_a", "evt_unknown_group", chat_id="oc_unknown")
    admission = ingress.admit_trusted_feishu_ingress(ticket=ticket, adapter=FakeAdapter())
    event = NS(
        source=NS(
            platform="feishu",
            user_id="ou_a",
            user_id_alt=None,
            chat_id=ticket.chat_id,
            chat_type="group",
        ),
        message_id=ticket.message_id,
        trusted_feishu_ingress_ticket=ticket,
        trusted_feishu_ingress_admission=admission,
    )

    assert not ingress.validate_admitted_feishu_event(event)


def test_known_group_still_requires_a_unique_actor(routes, tmp_path):
    routes.upsert_group(
        chat_id="oc_group_actor",
        profile_name="profile_group_actor",
        owner_open_id="ou_a",
        display_label="group",
    )
    (tmp_path / "profile_group_actor").mkdir()

    assert ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket("ou_missing", "evt_group_actor", chat_id="oc_group_actor"),
        adapter=FakeAdapter(),
    ) is None


def test_run_request_uses_sealed_admission_identity(routes, tmp_path):
    routes.upsert_group(
        chat_id="oc_request",
        profile_name="profile_request",
        owner_open_id="ou_a",
        display_label="group",
    )
    (tmp_path / "profile_request").mkdir()
    ticket = FakeTicket("ou_a", "evt_request", chat_id="oc_request")
    admission = ingress.admit_trusted_feishu_ingress(ticket=ticket, adapter=FakeAdapter())
    event = NS(
        source=NS(platform="feishu"),
        message_id=ticket.message_id,
        trusted_feishu_ingress_ticket=ticket,
        trusted_feishu_ingress_admission=admission,
    )

    request = router._run_request_for_routed_event(
        event=event,
        profile_name="profile_b",
        sender="ou_b",
        sender_alt=None,
        chat_id="oc_other",
        text="hello",
    )

    assert request.profile_name == "profile_request"
    assert request.user_key == "ou_a"
    assert request.chat_id == "oc_request"
    assert request.credential_subject == "cli_trusted"
    assert request.metadata["feishu_tool_scope"] == "feishu:bot"
    assert request.metadata["trusted_actor_subject"] == "ou_a"
    assert request.metadata["trusted_chat_type"] == "group"
    assert request.metadata["trusted_credential_subject"] == "cli_trusted"


def test_handle_async_keeps_sealed_profile_to_runtime_entry(routes, tmp_path, monkeypatch):
    routes.upsert_group(
        chat_id="oc_runtime",
        profile_name="profile_runtime",
        owner_open_id="ou_a",
        display_label="group",
    )
    (tmp_path / "profile_runtime").mkdir()
    ticket = FakeTicket("ou_a", "evt_runtime", chat_id="oc_runtime")
    admission = ingress.admit_trusted_feishu_ingress(ticket=ticket, adapter=FakeAdapter())
    event = NS(
        text="hello",
        source=NS(
            platform="feishu",
            user_id="ou_a",
            user_id_alt=None,
            chat_id="oc_runtime",
            chat_type="group",
        ),
        message_id=ticket.message_id,
        trusted_feishu_ingress_ticket=ticket,
        trusted_feishu_ingress_admission=admission,
    )
    captured = {}

    def stop_at_runtime(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after trusted route capture")

    monkeypatch.setattr(router, "_run_request_for_routed_event", stop_at_runtime)

    asyncio.run(router.handle_async(event=event, gateway=None))

    assert captured["profile_name"] == "profile_runtime"
    assert captured["sender"] == "ou_a"
    assert captured["chat_id"] == "oc_runtime"


def test_agent_runtime_enforces_sealed_tool_scope(tmp_path, monkeypatch):
    from hermes_multitenancy.agent_real import (
        _trusted_feishu_child_sender,
        _validate_trusted_feishu_tool_scope,
    )

    user_home = tmp_path / "profile_user"
    group_home = tmp_path / "feishu_group_team"
    user_home.mkdir()
    group_home.mkdir()
    user_event = NS(raw_event={"metadata": {
        "sender_open_id": "ou_user",
        "trusted_actor_subject": "ou_user",
        "trusted_chat_id": "oc_dm",
        "trusted_chat_type": "p2p",
        "trusted_credential_subject": "ou_user",
        "trusted_profile_name": "profile_user",
        "trusted_ticket_fingerprint": "fp",
        "feishu_tool_scope": "feishu:user",
    }})
    bot_event = NS(raw_event={"metadata": {
        "sender_open_id": "ou_user",
        "trusted_actor_subject": "ou_user",
        "trusted_chat_id": "oc_group",
        "trusted_chat_type": "group",
        "trusted_credential_subject": "cli_trusted",
        "trusted_profile_name": "feishu_group_team",
        "trusted_ticket_fingerprint": "fp",
        "feishu_tool_scope": "feishu:bot",
    }})

    _validate_trusted_feishu_tool_scope(user_event, user_home)
    _validate_trusted_feishu_tool_scope(bot_event, group_home)
    with pytest.raises(RuntimeError, match="profile"):
        _validate_trusted_feishu_tool_scope(bot_event, user_home)
    with pytest.raises(RuntimeError, match="profile"):
        _validate_trusted_feishu_tool_scope(user_event, group_home)
    monkeypatch.setenv("HERMES_TRUSTED_FEISHU_ACTOR", "ou_user")
    assert _trusted_feishu_child_sender(user_event, NS(get=lambda: None)) == "ou_user"
    monkeypatch.setenv("HERMES_TRUSTED_FEISHU_ACTOR", "ou_other")
    with pytest.raises(RuntimeError, match="child identity"):
        _trusted_feishu_child_sender(user_event, NS(get=lambda: None))


@pytest.mark.parametrize(
    ("profile_name", "tool_scope", "allowed_identity"),
    [
        ("profile_user", "feishu:user", "user"),
        ("feishu_group_team", "feishu:bot", "bot"),
    ],
)
def test_lark_broker_allows_only_the_sealed_identity(
    tmp_path,
    monkeypatch,
    profile_name,
    tool_scope,
    allowed_identity,
):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.agent_real import _core as agent_core

    profile_home = tmp_path / profile_name
    profile_home.mkdir()
    if allowed_identity == "bot":
        (profile_home / "group_profile.json").write_text('{"kind":"group"}', encoding="utf-8")
    binary = tmp_path / "lark-cli-authsidecar"
    binary.touch()
    captured = {}

    class Server:
        url = "http://127.0.0.1:19090"

        def close(self):
            pass

    monkeypatch.setattr(agent_core, "_resolve_lark_cli_app_id", lambda _home: "cli_trusted")
    monkeypatch.setattr(agent_core, "_resolve_lark_cli_authsidecar_binary", lambda _home: binary)
    monkeypatch.setattr(agent_core, "_owner_mapped_bot_chat_ids", lambda *_a: frozenset())
    monkeypatch.setattr(agent_core, "_lark_cli_default_identity", lambda *_a: "bot")
    def start_server(context):
        captured["context"] = context
        return Server()

    monkeypatch.setattr(agent_core, "start_lark_cli_auth_broker_server", start_server)

    with agent_real._lark_cli_auth_broker_scope(
        profile_home,
        "ou_a",
        tool_scope=tool_scope,
        chat_type="group" if allowed_identity == "bot" else "p2p",
        chat_id="oc_group" if allowed_identity == "bot" else "oc_dm",
    ) as env:
        assert env["LARKSUITE_CLI_DEFAULT_AS"] == allowed_identity
        assert captured["context"].allowed_identities == frozenset({allowed_identity})


def test_real_agent_ticket_crosses_mt_runtime_boundary(routes, tmp_path, monkeypatch, caplog):
    feishu = pytest.importorskip("gateway.platforms.feishu")
    if not hasattr(feishu.FeishuAdapter, "_trusted_ingress_admitter"):
        pytest.skip("installed hermes-agent lacks the trusted ingress contract")
    monkeypatch.setattr(ingress, "load_feishu_module", lambda: feishu)
    captured = {}

    def capture_envelope(_adapter, envelope):
        captured["envelope"] = envelope

    monkeypatch.setattr(
        feishu.FeishuAdapter,
        "_trusted_ingress_admitter",
        staticmethod(ingress.admit_trusted_feishu_ingress),
    )
    monkeypatch.setattr(feishu.FeishuAdapter, "_on_message_event", capture_envelope)
    real_adapter = object.__new__(feishu.FeishuAdapter)
    real_adapter._app_id = "cli_trusted"
    real_adapter._dispatch_trusted_ingress(
        "im.message.receive_v1",
        {
            "header": {"event_id": "evt_real"},
            "event": {
                "sender": {
                    "sender_id": {"open_id": "ou_a", "union_id": "on_a"},
                    "sender_type": "user",
                },
                "message": {"chat_id": "oc_dm", "message_id": "om_real"},
            },
        },
        transport="websocket",
    )
    envelope = captured["envelope"]
    ticket = envelope.trusted_feishu_ingress_ticket
    admission = envelope.trusted_feishu_ingress_admission
    event = NS(
        text="hello",
        source=NS(
            platform="feishu",
            user_id="ou_a",
            user_id_alt="on_a",
            chat_id="oc_dm",
            chat_type="p2p",
        ),
        message_id="om_real",
        trusted_feishu_ingress_ticket=ticket,
        trusted_feishu_ingress_admission=admission,
    )

    assert ingress.validate_admitted_feishu_event(event)
    runtime_adapter = NS(_app_id="cli_trusted")
    monkeypatch.setattr(router, "_get_feishu_adapter", lambda _gateway: runtime_adapter)

    class Seen:
        keys = set()

        def is_event_processed(self, key, _ttl):
            return key in self.keys

        def mark_event_processed(self, key, **_kwargs):
            if key in self.keys:
                return False
            self.keys.add(key)
            return True

    monkeypatch.setattr(router, "_get_session_store", lambda: Seen())
    monkeypatch.setattr(router, "_materialize_inbound_media_for_profile", lambda *_a, **_k: None)

    async def identity_request(request):
        return request

    async def no_enrichment(event, *_args, **_kwargs):
        return event.text

    async def no_vision(*_args, **_kwargs):
        return None

    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.agent_real import _core as agent_core
    from hermes_multitenancy import webui_broker_server
    from hermes_multitenancy.router import commands as router_commands
    from hermes_multitenancy.run_models import RunResult

    monkeypatch.setattr(billing_identity, "prepare_billing_request", identity_request)
    monkeypatch.setattr(router, "_call_enrich_via_hermes_pipeline", no_enrichment)
    monkeypatch.setattr(router_commands, "send_vision_block_before_admission", no_vision)
    binary = tmp_path / "lark-cli-authsidecar"
    binary.touch()
    broker_contexts = []

    class BrokerServer:
        url = "http://127.0.0.1:19090"

        def close(self):
            pass

    monkeypatch.setattr(agent_core, "_resolve_lark_cli_app_id", lambda _home: "cli_trusted")
    monkeypatch.setattr(agent_core, "_resolve_lark_cli_authsidecar_binary", lambda _home: binary)
    monkeypatch.setattr(agent_core, "_owner_mapped_bot_chat_ids", lambda *_a: frozenset())
    monkeypatch.setattr(agent_core, "strict_context_enabled", lambda: False)
    monkeypatch.setattr(
        agent_core,
        "_build_subprocess_env",
        lambda _home, *, approval_dir, event_stream=False, extra=None: dict(extra or {}),
    )
    monkeypatch.setattr(
        agent_core,
        "start_lark_cli_auth_broker_server",
        lambda context: broker_contexts.append(context) or BrokerServer(),
    )
    monkeypatch.setattr(webui_broker_server, "credential_broker_url", lambda: "http://broker")
    for name in (
        "register_session_search_broker_token",
        "unregister_session_search_broker_token",
        "register_run_broker_scoped_token",
        "unregister_run_broker_scoped_token",
        "register_credential_broker_token",
        "unregister_credential_broker_token",
    ):
        monkeypatch.setattr(webui_broker_server, name, lambda **_kwargs: None)

    async def execute_at_final_broker(admitted_run, *, event, profile_home, **_kwargs):
        run_event = router._event_with_run_metadata(event, admitted_run.request.metadata)
        with agent_real._aiagent_subprocess_env_scope(
            run_event,
            profile_home,
            approval_dir=tmp_path / "approval",
        ) as child_env:
            captured["child_env"] = child_env
        return RunResult(content="ok")

    monkeypatch.setattr(
        router_commands,
        "execute_admitted_feishu_run",
        execute_at_final_broker,
    )
    root = __import__("hermes_multitenancy")
    monkeypatch.setattr(root, "is_router_profile_runtime", lambda: False)
    monkeypatch.setattr(root, "may_own_cron_runtime", lambda: False)

    from tools.feishu_oapi_client import sender_open_id_scope

    with sender_open_id_scope("ou_a"):
        result = root._dispatch_with_worker_init(event=event, gateway=NS())

    assert result["action"] == "skip"
    assert captured["child_env"]["HERMES_TRUSTED_FEISHU_ACTOR"] == "ou_a"
    assert captured["child_env"]["LARKSUITE_CLI_DEFAULT_AS"] == "user"
    assert len(broker_contexts) == 1
    assert broker_contexts[0].user_open_id == "ou_a"
    assert broker_contexts[0].allowed_identities == frozenset({"user"})

    def issue_ticket(event_key, message_id):
        return feishu.TrustedFeishuIngressTicket.issue(
            transport="websocket",
            event_kind="message",
            event_type="im.message.receive_v1",
            event_key=event_key,
            account_id="cli_trusted",
            namespace=feishu._feishu_namespace("cli_trusted"),
            actor_id="ou_a",
            actor_id_type="open_id",
            principal_kind="human",
            chat_id="oc_dm",
            thread_id="",
            message_id=message_id,
        )

    crossed = issue_ticket("evt_crossed", "om_crossed")
    crossed_admission = ingress.admit_trusted_feishu_ingress(ticket=crossed, adapter=FakeAdapter())
    crossed_event = replace_event(event, crossed, crossed_admission)
    with sender_open_id_scope("ou_b"):
        root._dispatch_with_worker_init(event=crossed_event, gateway=NS())
    assert len(broker_contexts) == 1
    assert "ambient identity does not match admission" in caplog.text

    missing = issue_ticket("evt_missing_ambient", "om_missing_ambient")
    missing_admission = ingress.admit_trusted_feishu_ingress(ticket=missing, adapter=FakeAdapter())
    missing_event = replace_event(event, missing, missing_admission)
    monkeypatch.setenv("HERMES_TRUSTED_FEISHU_ACTOR", "ou_a")
    with sender_open_id_scope(None):
        root._dispatch_with_worker_init(event=missing_event, gateway=NS())
    assert len(broker_contexts) == 1
    assert "ambient identity is unavailable" in caplog.text
def replace_event(event, ticket, admission):
    return NS(
        **{
            **vars(event),
            "message_id": ticket.message_id,
            "source": NS(**{**vars(event.source), "message_id": ticket.message_id}),
            "trusted_feishu_ingress_ticket": ticket,
            "trusted_feishu_ingress_admission": admission,
        }
    )


def test_ambiguous_active_identity_is_denied(routes):
    routes._conn.execute(
        "INSERT INTO multitenancy_routing "
        "(user_id, profile_name, open_id, active, synced_at, version, created_at, updated_at, kind) "
        "VALUES (?, ?, ?, 1, 0, 1, 0, 0, 'user')",
        ("u_a_duplicate", "profile_b", "ou_a"),
    )
    routes._conn.commit()

    assert ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket("ou_a", "evt_ambiguous"), adapter=FakeAdapter()
    ) is None


def test_registered_hook_denies_before_router_or_model_work(monkeypatch):
    calls = []
    monkeypatch.setattr(ingress, "validate_admitted_feishu_event", lambda _event, _gateway=None: False)
    monkeypatch.setattr("hermes_multitenancy.on_pre_gateway_dispatch", lambda **_kwargs: calls.append("router"))

    result = __import__("hermes_multitenancy")._dispatch_with_worker_init(event=object())

    assert result == {"action": "skip", "reason": "trusted Feishu ingress denied"}
    assert calls == []


def test_installation_owns_the_adapter_edge(routes):
    ingress.install_trusted_feishu_ingress_admission()
    assert FakeAdapter._trusted_ingress_admitter is ingress.admit_trusted_feishu_ingress


def test_installation_rejects_adapter_clone(monkeypatch):
    class LiveAdapter:
        _trusted_ingress_admitter = None

    class CloneAdapter:
        _trusted_ingress_admitter = None

    modules = iter((NS(FeishuAdapter=CloneAdapter), NS(FeishuAdapter=LiveAdapter)))
    monkeypatch.setattr(ingress, "load_live_feishu_module", lambda: next(modules))

    with pytest.raises(RuntimeError, match="live Feishu adapter"):
        ingress.install_trusted_feishu_ingress_admission()
