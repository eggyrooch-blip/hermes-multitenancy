import asyncio
import importlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
import logging
import sys
import time
from types import FunctionType, ModuleType, SimpleNamespace as NS

import pytest

from hermes_multitenancy import trusted_feishu_ingress as ingress
from hermes_multitenancy import router
from hermes_multitenancy.feishu_adapter_compat import load_live_feishu_module
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


@pytest.fixture
def sender_open_id_scope(monkeypatch):
    """Provide only the optional legacy ambient identity context under test."""
    current_sender_open_id = ContextVar("test_current_sender_open_id", default=None)

    @contextmanager
    def scope(value):
        token = current_sender_open_id.set(value)
        try:
            yield
        finally:
            current_sender_open_id.reset(token)

    module = ModuleType("tools.feishu_oapi_client")
    module.current_sender_open_id = current_sender_open_id
    module.sender_open_id_scope = scope
    monkeypatch.setitem(sys.modules, "tools.feishu_oapi_client", module)
    import tools

    monkeypatch.setattr(tools, "feishu_oapi_client", module, raising=False)
    yield scope
    assert current_sender_open_id.get() is None


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


def test_ticket_type_is_bound_to_issuing_adapter_not_reloaded_module(routes, monkeypatch):
    """A synthetic-module replacement must not invalidate an authentic ticket."""

    def issuer_template(self):
        return TrustedFeishuIngressTicket

    issuer = FunctionType(
        issuer_template.__code__,
        {"TrustedFeishuIngressTicket": FakeTicket},
        name=issuer_template.__name__,
    )
    bound_adapter_type = type(
        "BoundAdapter",
        (),
        {
            "_app_id": "cli_trusted",
            "_issue_trusted_ingress_ticket": issuer,
        },
    )
    replacement_ticket_type = type("ReplacementTicket", (), {})
    monkeypatch.setattr(
        ingress,
        "load_feishu_module",
        lambda: NS(TrustedFeishuIngressTicket=replacement_ticket_type),
    )

    admission = ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket("ou_a", "evt_module_replaced"),
        adapter=bound_adapter_type(),
    )

    assert admission is not None
    assert admission.profile_name == "profile_a"


def test_envelope_validation_keeps_issuing_adapter_after_module_replaced(routes, monkeypatch):
    """Review P1 (adapter-provenance-lost): validation after admission must use
    the issuing adapter's ticket class, not a re-materialized module's."""
    from hermes_multitenancy.feishu_ingress_compat import (
        _TrustedFeishuEnvelope,
        _envelope_allowed,
    )

    def issuer_template(self):
        return TrustedFeishuIngressTicket

    issuer = FunctionType(
        issuer_template.__code__,
        {"TrustedFeishuIngressTicket": FakeTicket},
        name=issuer_template.__name__,
    )
    adapter = type(
        "BoundAdapter",
        (),
        {"_app_id": "cli_trusted", "_issue_trusted_ingress_ticket": issuer},
    )()
    ticket = FakeTicket("ou_a", "evt_envelope_after_reload")
    admission = ingress.admit_trusted_feishu_ingress(ticket=ticket, adapter=adapter)
    assert admission is not None

    monkeypatch.setattr(
        ingress,
        "load_feishu_module",
        lambda: NS(TrustedFeishuIngressTicket=type("ReplacementTicket", (), {})),
    )
    envelope = _TrustedFeishuEnvelope(object(), ticket, admission)

    assert _envelope_allowed(adapter, envelope)
    event = NS(
        source=NS(platform="feishu", user_id=ticket.actor_id, chat_id=ticket.chat_id,
                  chat_type=admission.chat_type),
        message_id=ticket.message_id,
        trusted_feishu_ingress_ticket=ticket,
        trusted_feishu_ingress_admission=admission,
    )
    # Without the adapter the replaced module's class is used and denies.
    assert not ingress.validate_admitted_feishu_event(event)
    assert ingress.validate_admitted_feishu_event(event, adapter=adapter)


def test_human_denial_logs_only_reason_and_fingerprints(routes, caplog):
    caplog.set_level(logging.WARNING, logger=ingress.logger.name)
    actor_id = "ou_raw_identifier_must_not_leak"
    chat_id = "oc_raw_identifier_must_not_leak"

    assert ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket(actor_id, "evt_redacted", chat_id=chat_id),
        adapter=FakeAdapter(),
    ) is None

    assert "reason=no_route_context" in caplog.text
    assert "actor_fp=" in caplog.text
    assert "chat_fp=" in caplog.text
    assert actor_id not in caplog.text
    assert chat_id not in caplog.text


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


def test_real_agent_ticket_crosses_mt_runtime_boundary(
    routes, tmp_path, monkeypatch, caplog, sender_open_id_scope
):
    try:
        from gateway.platforms import feishu as legacy_feishu
    except ImportError:
        legacy_feishu = None
    if legacy_feishu is not None and not hasattr(legacy_feishu.FeishuAdapter, "_trusted_ingress_admitter"):
        pytest.skip("legacy installed hermes-agent lacks the trusted ingress contract")

    from hermes_cli.plugins import discover_plugins

    # This test requires a real Agent plugin boundary, not whichever discovery
    # state a previous test left in the xdist worker.  Force rediscovery after
    # the suite-wide Feishu registry isolation fixture clears that state.
    discover_plugins(force=True)
    feishu = load_live_feishu_module()
    monkeypatch.setattr(ingress, "load_live_feishu_module", lambda: feishu)
    ingress.install_trusted_feishu_ingress_admission()
    assert hasattr(feishu.FeishuAdapter, "_trusted_ingress_admitter")
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
    assert len(broker_contexts) == 2
    assert broker_contexts[-1].user_open_id == "ou_a"
    assert broker_contexts[-1].allowed_identities == frozenset({"user"})
    assert "ambient identity is unavailable" not in caplog.text
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


# ---------------------------------------------------------------------------
# Bot-actor controlled path (mt-trusted-ingress-bot-actor)
# ---------------------------------------------------------------------------


def _bot_group(routes, tmp_path, chat_id="oc_botgrp", profile="profile_botgrp"):
    routes.upsert_group(
        chat_id=chat_id,
        profile_name=profile,
        owner_open_id="ou_a",
        display_label="botgrp",
    )
    home = tmp_path / profile
    if not home.exists():
        home.mkdir()
    return routes.lookup_by_chat_id(chat_id)


def test_bot_message_in_routed_group_binds_group_profile(routes, tmp_path):
    row = _bot_group(routes, tmp_path)
    admission = ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket("ou_alertbot", "evt_bot_1", principal_kind="bot", chat_id="oc_botgrp"),
        adapter=FakeAdapter(),
    )

    assert admission is not None
    assert admission.actor_kind == "bot"
    assert admission.profile_name == "profile_botgrp"
    assert admission.route_version == int(row.version)
    assert admission.actor_subject == "bot:ou_alertbot"
    assert admission.credential_subject == "cli_trusted"
    assert admission.tool_scope == "feishu:bot"
    assert admission.chat_type == "group"


def test_bot_with_empty_actor_id_uses_unknown_sentinel(routes, tmp_path):
    _bot_group(routes, tmp_path)
    admission = ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket("", "evt_bot_noid", principal_kind="bot", chat_id="oc_botgrp"),
        adapter=FakeAdapter(),
    )

    assert admission is not None
    assert admission.actor_subject == "bot:unknown"


def test_bot_unrouted_chat_and_dm_stay_fail_closed(routes):
    # No group row for the chat (covers both DMs and unrouted groups).
    assert ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket("ou_alertbot", "evt_bot_dm", principal_kind="bot", chat_id="oc_dm"),
        adapter=FakeAdapter(),
    ) is None


def test_bot_non_message_event_kinds_denied(routes, tmp_path):
    _bot_group(routes, tmp_path)
    for kind in ("reaction", "button", "form"):
        assert ingress.admit_trusted_feishu_ingress(
            ticket=FakeTicket(
                "ou_alertbot",
                f"evt_bot_{kind}",
                principal_kind="bot",
                event_kind=kind,
                chat_id="oc_botgrp",
            ),
            adapter=FakeAdapter(),
        ) is None


def test_bot_per_chat_floor_throttles_second_message(routes, tmp_path):
    _bot_group(routes, tmp_path)
    _bot_group(routes, tmp_path, chat_id="oc_botgrp2", profile="profile_botgrp2")

    first = ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket("ou_alertbot", "evt_bot_t1", principal_kind="bot", chat_id="oc_botgrp"),
        adapter=FakeAdapter(),
    )
    second = ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket("ou_alertbot", "evt_bot_t2", principal_kind="bot", chat_id="oc_botgrp"),
        adapter=FakeAdapter(),
    )
    other_chat = ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket("ou_alertbot", "evt_bot_t3", principal_kind="bot", chat_id="oc_botgrp2"),
        adapter=FakeAdapter(),
    )

    assert first is not None
    assert second is None  # within the per-chat floor
    assert other_chat is not None  # floor is per chat, not global

    # Aged past the floor → admitted again.
    with ingress._seen_lock:
        ingress._bot_last_admit["oc_botgrp"] -= ingress._BOT_MIN_INTERVAL_SECONDS + 1
    third = ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket("ou_alertbot", "evt_bot_t4", principal_kind="bot", chat_id="oc_botgrp"),
        adapter=FakeAdapter(),
    )
    assert third is not None


def test_bot_duplicate_event_key_still_claim_once(routes, tmp_path):
    _bot_group(routes, tmp_path)
    assert ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket("ou_alertbot", "evt_bot_dup", principal_kind="bot", chat_id="oc_botgrp"),
        adapter=FakeAdapter(),
    ) is not None
    with ingress._seen_lock:
        ingress._bot_last_admit["oc_botgrp"] -= ingress._BOT_MIN_INTERVAL_SECONDS + 1
    assert ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket("ou_alertbot", "evt_bot_dup", principal_kind="bot", chat_id="oc_botgrp"),
        adapter=FakeAdapter(),
    ) is None


def _bot_event(ticket, admission, chat_type="group"):
    return NS(
        source=NS(
            platform="feishu",
            user_id=None,
            user_id_alt=None,
            chat_id=ticket.chat_id,
            chat_type=chat_type,
        ),
        message_id=ticket.message_id,
        trusted_feishu_ingress_ticket=ticket,
        trusted_feishu_ingress_admission=admission,
    )


def test_bot_admission_validates_end_to_end(routes, tmp_path):
    _bot_group(routes, tmp_path)
    ticket = FakeTicket("ou_alertbot", "evt_bot_v1", principal_kind="bot", chat_id="oc_botgrp")
    admission = ingress.admit_trusted_feishu_ingress(ticket=ticket, adapter=FakeAdapter())

    assert admission is not None
    assert ingress.validate_admitted_feishu_event(_bot_event(ticket, admission))


def test_bot_validation_fails_closed_on_route_change_or_mismatch(routes, tmp_path):
    _bot_group(routes, tmp_path)
    ticket = FakeTicket("ou_alertbot", "evt_bot_v2", principal_kind="bot", chat_id="oc_botgrp")
    admission = ingress.admit_trusted_feishu_ingress(ticket=ticket, adapter=FakeAdapter())
    assert admission is not None

    # DM-shaped event can't ride a group admission.
    assert not ingress.validate_admitted_feishu_event(_bot_event(ticket, admission, chat_type="p2p"))

    # Wrong message pinning.
    swapped = replace(ticket, message_id="om_other")
    assert not ingress.validate_admitted_feishu_event(_bot_event(swapped, admission))

    # Route re-pointed to another profile after admission → stale admission dies.
    routes.upsert_group(
        chat_id="oc_botgrp",
        profile_name="profile_botgrp_repointed",
        owner_open_id="ou_a",
        display_label="botgrp",
    )
    assert not ingress.validate_admitted_feishu_event(_bot_event(ticket, admission))


def test_human_ticket_cannot_ride_bot_branch(routes, tmp_path):
    _bot_group(routes, tmp_path)
    bot_ticket = FakeTicket("ou_alertbot", "evt_bot_v3", principal_kind="bot", chat_id="oc_botgrp")
    admission = ingress.admit_trusted_feishu_ingress(ticket=bot_ticket, adapter=FakeAdapter())
    assert admission is not None

    human_ticket = replace(bot_ticket, principal_kind="human")
    assert not ingress.validate_admitted_feishu_event(_bot_event(human_ticket, admission))


def test_bot_run_request_attribution_never_names_an_employee(routes, tmp_path):
    _bot_group(routes, tmp_path, chat_id="oc_botreq", profile="profile_botreq")
    ticket = FakeTicket("ou_alertbot", "evt_bot_req", principal_kind="bot", chat_id="oc_botreq")
    admission = ingress.admit_trusted_feishu_ingress(ticket=ticket, adapter=FakeAdapter())
    event = NS(
        source=NS(platform="feishu"),
        message_id=ticket.message_id,
        trusted_feishu_ingress_ticket=ticket,
        trusted_feishu_ingress_admission=admission,
    )

    request = router._run_request_for_routed_event(
        event=event,
        profile_name="profile_a",
        sender="ou_a",
        sender_alt=None,
        chat_id="oc_other",
        text="alert card",
    )

    assert request.profile_name == "profile_botreq"
    assert request.user_key == "bot:ou_alertbot"
    assert request.credential_subject == "cli_trusted"
    assert request.metadata["sender_open_id"] == "bot:ou_alertbot"
    assert "ou_a" not in (request.user_key, request.credential_subject)


def _runtime_event_from_admission(ticket, admission):
    """Metadata exactly as `_run_request_for_routed_event` seals it, wrapped in
    the raw_event shape `_event_metadata` reads on the run path."""
    request = router._run_request_for_routed_event(
        event=NS(
            source=NS(platform="feishu"),
            message_id=ticket.message_id,
            trusted_feishu_ingress_ticket=ticket,
            trusted_feishu_ingress_admission=admission,
        ),
        profile_name="ignored",
        sender="ou_ignored",
        sender_alt=None,
        chat_id="oc_ignored",
        text="alert card",
    )
    return NS(raw_event={"metadata": dict(request.metadata)})


def test_bot_trusted_runtime_identity_accepts_bot_subject(routes, tmp_path):
    """P0 (grok round 1): agent_real's runtime seal must accept the bot actor
    shape — otherwise every bot-admitted run aborts before model work."""
    from hermes_multitenancy.agent_real._core import _trusted_feishu_runtime_identity

    _bot_group(routes, tmp_path, chat_id="oc_rt", profile="profile_rt")
    ticket = FakeTicket("ou_alertbot", "evt_rt_1", principal_kind="bot", chat_id="oc_rt")
    admission = ingress.admit_trusted_feishu_ingress(ticket=ticket, adapter=FakeAdapter())
    event = _runtime_event_from_admission(ticket, admission)

    actor, credential, tool_scope = _trusted_feishu_runtime_identity(event)

    assert actor == "bot:ou_alertbot"
    assert credential == "cli_trusted"
    assert tool_scope == "feishu:bot"


def test_bot_runtime_identity_tool_scope_gate_passes_group_profile(routes, tmp_path):
    from hermes_multitenancy.agent_real.run import _validate_trusted_feishu_tool_scope

    _bot_group(routes, tmp_path, chat_id="oc_rt2", profile="profile_rt2")
    ticket = FakeTicket("ou_alertbot", "evt_rt_2", principal_kind="bot", chat_id="oc_rt2")
    admission = ingress.admit_trusted_feishu_ingress(ticket=ticket, adapter=FakeAdapter())
    event = _runtime_event_from_admission(ticket, admission)

    # Right profile: returns silently. Wrong profile: refuses.
    _validate_trusted_feishu_tool_scope(event, tmp_path / "profile_rt2")
    with pytest.raises(RuntimeError, match="profile"):
        _validate_trusted_feishu_tool_scope(event, tmp_path / "profile_other")


def test_bot_runtime_identity_rejects_bot_shape_outside_group_bot_scope(routes, tmp_path):
    """双向负控制：bot: 形状只在 (feishu:bot, group) 封印下合法；
    冒充 feishu:user 或 p2p 或带员工 credential 的一律拒。"""
    from hermes_multitenancy.agent_real._core import _trusted_feishu_runtime_identity

    _bot_group(routes, tmp_path, chat_id="oc_rt3", profile="profile_rt3")
    ticket = FakeTicket("ou_alertbot", "evt_rt_3", principal_kind="bot", chat_id="oc_rt3")
    admission = ingress.admit_trusted_feishu_ingress(ticket=ticket, adapter=FakeAdapter())
    good = _runtime_event_from_admission(ticket, admission).raw_event["metadata"]

    for corrupt in (
        {"feishu_tool_scope": "feishu:user"},
        {"trusted_chat_type": "p2p"},
        {"trusted_credential_subject": "ou_employee"},
        {"sender_open_id": "ou_someone"},
    ):
        meta = {**good, **corrupt}
        with pytest.raises(RuntimeError):
            _trusted_feishu_runtime_identity(NS(raw_event={"metadata": meta}))


def test_bot_self_echo_never_burns_the_throttle_slot(routes, tmp_path):
    """自家 bot 的回声在 ingress 就拒（reason=self_echo），且不占用该群的
    节流窗口 —— 否则每次 Hermes 自己回复都会饿死 30s 内的真实告警。"""

    class SelfAwareAdapter(FakeAdapter):
        _bot_open_id = "ou_hermes_self"

    _bot_group(routes, tmp_path, chat_id="oc_echo", profile="profile_echo")

    echo = ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket("ou_hermes_self", "evt_echo_1", principal_kind="bot", chat_id="oc_echo"),
        adapter=SelfAwareAdapter(),
    )
    real = ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket("ou_alertbot", "evt_echo_2", principal_kind="bot", chat_id="oc_echo"),
        adapter=SelfAwareAdapter(),
    )

    assert echo is None  # own reply refused at ingress
    assert real is not None  # and it did not consume the chat's floor


def test_bot_throttle_map_is_ttl_and_size_bounded(routes, tmp_path):
    """P1 (grok round 1)：节流表必须像 _seen 一样有 TTL/容量上界。"""
    _bot_group(routes, tmp_path, chat_id="oc_ttl", profile="profile_ttl")

    with ingress._seen_lock:
        ingress._bot_last_admit.clear()
        # expired entry → pruned on next claim
        ingress._bot_last_admit["oc_stale"] = time.time() - ingress._BOT_ADMIT_TTL_SECONDS - 1
        # overflow beyond the cap → oldest evicted
        base = time.time()
        for i in range(ingress._BOT_ADMIT_MAX):
            ingress._bot_last_admit[f"oc_bulk_{i}"] = base + i * 1e-6

    admission = ingress.admit_trusted_feishu_ingress(
        ticket=FakeTicket("ou_alertbot", "evt_ttl_1", principal_kind="bot", chat_id="oc_ttl"),
        adapter=FakeAdapter(),
    )

    assert admission is not None
    with ingress._seen_lock:
        assert "oc_stale" not in ingress._bot_last_admit
        assert len(ingress._bot_last_admit) <= ingress._BOT_ADMIT_MAX


@pytest.fixture
def stock_ingress(routes, monkeypatch):
    """Exercise MT wrappers on the real stock class, without SDK/network calls."""
    from hermes_cli import __version__

    if tuple(int(part) for part in __version__.split(".")[:3]) < (0, 21, 3):
        pytest.skip("stock 0.21.3 ingress adapter tests do not apply to this older core")
    from plugins.platforms.feishu import adapter as stock
    from hermes_multitenancy.feishu_ingress_compat import install_stock_feishu_ingress

    class Adapter(stock.FeishuAdapter):
        pass

    seen = []
    monkeypatch.setattr(Adapter, "_on_message_event", lambda self, data: seen.append(data))
    monkeypatch.setattr(Adapter, "_on_card_action_trigger", lambda self, data: seen.append(data))
    module = NS(FeishuAdapter=Adapter)
    install_stock_feishu_ingress(module)
    Adapter._trusted_ingress_admitter = staticmethod(ingress.admit_trusted_feishu_ingress)
    monkeypatch.setattr(ingress, "load_feishu_module", lambda: module)
    adapter = object.__new__(Adapter)
    adapter._app_id = "cli_trusted"
    adapter.platform = stock.Platform.FEISHU
    return adapter, seen, module


def _stock_callback(actor="ou_a", *, card=False, form=False, event_id="evt_stock"):
    return NS(header=NS(event_id=event_id), event=NS(
        sender=NS(sender_id=NS(open_id=actor), sender_type="user"),
        message=None if card else NS(chat_id="oc_dm", message_id="om_stock"),
        operator=NS(open_id=actor),
        context=NS(open_chat_id="oc_dm", open_message_id="om_stock"),
        action=NS(tag="button", form_value={} if form else None),
    ))


@pytest.mark.parametrize("card,form", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("transport", ["websocket", "webhook"])
def test_stock_callbacks_are_admitted_before_inline_handlers(stock_ingress, card, form, transport):
    from hermes_multitenancy.feishu_ingress_compat import _transport

    adapter, seen, _module = stock_ingress
    token = _transport.set(transport)
    try:
        callback = adapter._on_card_action_trigger if card else adapter._on_message_event
        callback(_stock_callback(card=card, form=form))
        assert len(seen) == 1
        envelope = seen[0]
        assert envelope.trusted_feishu_ingress_ticket.transport == transport
        assert envelope.trusted_feishu_ingress_ticket.event_kind == (
            "form" if form else "button" if card else "message"
        )
        assert envelope.trusted_feishu_ingress_admission.profile_name == "profile_a"
        callback(_stock_callback(card=card, form=form))  # replay
        callback(_stock_callback(actor="ou_unknown", card=card, form=form, event_id="bad"))
        assert len(seen) == 1
    finally:
        _transport.reset(token)


def test_stock_comment_and_meeting_callbacks_deny_before_side_effects(stock_ingress):
    adapter, seen, _module = stock_ingress
    adapter._on_drive_comment_event(_stock_callback())
    adapter._on_meeting_invited_event(_stock_callback())
    assert seen == []


@pytest.mark.asyncio
async def test_stock_guard_rechecks_actor_route_and_rejects_unstamped_events(stock_ingress, routes):
    adapter, seen, _module = stock_ingress
    adapter._on_message_event(_stock_callback())
    envelope = seen.pop()
    handled = []

    async def handle(event):
        handled.append(event)

    adapter.handle_message = handle
    adapter._get_chat_lock = lambda _chat: asyncio.Lock()
    def event(actor="ou_a", raw=envelope):
        return NS(source=NS(platform="feishu", user_id=actor, chat_id="oc_dm", chat_type="p2p"),
                  message_id="om_stock", raw_message=raw)

    await adapter._dispatch_inbound_event(event())
    await adapter._dispatch_inbound_event(event(actor="ou_b"))
    mixed_actor = event()
    mixed_actor.source.user_id_alt = "on_b"
    await adapter._dispatch_inbound_event(mixed_actor)
    await adapter._dispatch_inbound_event(event(raw=_stock_callback()))
    assert len(handled) == 1
    routes.upsert(user_id="u_a", profile_name="profile_b", open_id="ou_a", union_id="on_a")
    await adapter._dispatch_inbound_event(event())
    assert len(handled) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["cli_trusted", "cli_other"])
async def test_stock_reaction_uses_api_verified_chat_and_own_message(stock_ingress, owner):
    from hermes_multitenancy.feishu_ingress_compat import _PendingReaction

    adapter, seen, _module = stock_ingress
    adapter._client = NS(im=NS(v1=NS(message=NS(get=object()))))
    adapter._build_get_message_request = lambda ident: ident
    adapter._response_succeeded = lambda response: True
    async def run_blocking(_method, message_id):
        assert message_id == "om_reaction"
        return NS(data=NS(items=[NS(sender=NS(id=owner), chat_id="oc_dm", chat_type="p2p")]))
    adapter._run_blocking = run_blocking
    async def profile(_ident):
        return {"user_id": "u_a", "user_id_alt": "on_a", "user_name": "A"}
    async def chat(_chat):
        return {"name": "DM", "chat_type": "p2p"}
    async def handle(event):
        seen.append(event)
    adapter._resolve_sender_profile = profile
    adapter.get_chat_info = chat
    adapter._resolve_channel_prompt = lambda _chat: None
    adapter._get_chat_lock = lambda _chat: asyncio.Lock()
    adapter.handle_message = handle
    data = NS(header=NS(event_id="reaction_1"), event=NS(
        user_id=NS(open_id="ou_a", user_id="u_a", union_id="on_a"), operator_type="user", message_id="om_reaction",
        chat_id="oc_untrusted", reaction_type=NS(emoji_type="OK"),
    ))
    await adapter._handle_reaction_event(
        "im.message.reaction.created_v1",
        _PendingReaction(data, "im.message.reaction.created_v1", "websocket"),
    )
    assert len(seen) == (1 if owner == "cli_trusted" else 0)
    if seen:
        assert seen[0].trusted_feishu_ingress_ticket.chat_id == "oc_dm"
        assert seen[0].trusted_feishu_ingress_admission.profile_name == "profile_a"


def test_stock_unknown_adapter_fails_closed():
    from hermes_multitenancy.feishu_ingress_compat import install_stock_feishu_ingress

    with pytest.raises(RuntimeError, match="startup denied"):
        install_stock_feishu_ingress(NS(FeishuAdapter=type("UnknownAdapter", (), {})))


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
async def test_stock_webhook_authenticates_before_issuing_ticket(stock_ingress, valid):
    import hashlib
    import json

    adapter, seen, _module = stock_ingress
    adapter._verification_token = "test-verification"
    adapter._encrypt_key = "test-signature-key"
    adapter._webhook_path = "/feishu"
    adapter._check_webhook_rate_limit = lambda _key: True
    adapter._record_webhook_anomaly = lambda *_args: None
    adapter._clear_webhook_anomaly = lambda _remote: None
    payload = {
        "header": {"event_id": "webhook_1", "event_type": "im.message.receive_v1",
                   "token": "test-verification"},
        "event": {"sender": {"sender_id": {"open_id": "ou_a"}, "sender_type": "user"},
                  "message": {"chat_id": "oc_dm", "message_id": "om_webhook"}},
    }
    body = json.dumps(payload).encode()
    class Content:
        async def readexactly(self, size):
            raise asyncio.IncompleteReadError(body, size)
    timestamp, nonce = str(int(time.time())), "nonce"
    signature = hashlib.sha256((timestamp + nonce + adapter._encrypt_key).encode() + body).hexdigest()
    response = await adapter._handle_webhook_request(NS(
        remote="127.0.0.1", content=Content(), content_length=len(body),
        headers={"Content-Type": "application/json", "x-lark-request-timestamp": timestamp,
                 "x-lark-request-nonce": nonce, "x-lark-signature": signature if valid else "invalid"},
    ))
    assert response.status == (200 if valid else 401)
    assert len(seen) == (1 if valid else 0)
    if seen:
        assert seen[0].trusted_feishu_ingress_ticket.transport == "webhook"


def test_stock_sdk_registers_admitted_callbacks(stock_ingress, monkeypatch):
    from plugins.platforms.feishu import adapter as stock

    adapter, seen, _module = stock_ingress
    callbacks = {}
    class Builder:
        def __getattr__(self, name):
            def register(*args):
                callbacks[args[0] if name == "register_p2_customized_event" else name] = args[-1]
                return self
            return register
        def build(self):
            return self
    monkeypatch.setattr(stock, "EventDispatcherHandler", NS(builder=lambda *_args: Builder()))
    adapter._encrypt_key = ""
    adapter._verification_token = ""
    adapter._build_event_handler()
    callbacks["register_p2_im_message_receive_v1"](_stock_callback())
    callbacks["register_p2_card_action_trigger"](_stock_callback(card=True, event_id="card_1"))
    callbacks["drive.notice.comment_add_v1"](_stock_callback(event_id="comment_1"))
    callbacks["vc.bot.meeting_invited_v1"](_stock_callback(event_id="meeting_1"))
    assert len(seen) == 2
    assert all(item.trusted_feishu_ingress_ticket.transport == "websocket" for item in seen)


@pytest.mark.asyncio
async def test_stock_three_tier_message_identity_is_preserved(stock_ingress):
    from plugins.platforms.feishu.adapter import MessageType

    adapter, seen, _module = stock_ingress
    data = _stock_callback()
    data.event.sender.sender_id = NS(open_id="ou_a", user_id="u_a", union_id="on_a")
    adapter._on_message_event(data)
    envelope = seen.pop()
    async def content(_message):
        return "hello", MessageType.TEXT, [], [], [], []
    async def chat(_chat):
        return {"name": "DM", "chat_type": "p2p"}
    async def name(*_args, **_kwargs):
        return "A"
    async def handle(event):
        seen.append(event)
    adapter._extract_message_content = content
    adapter.get_chat_info = chat
    adapter._resolve_sender_name_from_api = name
    adapter._resolve_channel_prompt = lambda *_args: None
    adapter._get_chat_lock = lambda _chat: asyncio.Lock()
    adapter.handle_message = handle
    await adapter._process_inbound_message(
        data=envelope, message=data.event.message, sender_id=data.event.sender.sender_id,
        chat_type="p2p", message_id="om_stock",
    )
    assert len(seen) == 1
    assert seen[0].source.user_id == "u_a"
    assert seen[0].source.user_id_alt == "on_a"
    assert seen[0].sender_open_id == "ou_a"
    assert ingress.validate_admitted_feishu_event(seen[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["approval", "update"])
@pytest.mark.parametrize("attack", ["other_actor", "unknown_actor", "message", "chat", "route", "late_route", "unadmitted", "valid"])
async def test_stock_prompt_binds_original_actor_and_rechecks_on_loop(routes, monkeypatch, kind, attack):
    from gateway.config import PlatformConfig
    from gateway.platforms.base import SendResult
    from plugins.platforms.feishu import adapter as stock
    from hermes_multitenancy.feishu_ingress_compat import (
        _TrustedFeishuEnvelope, install_stock_feishu_ingress,
    )
    import tools.approval

    class Adapter(stock.FeishuAdapter):
        pass

    module = NS(FeishuAdapter=Adapter)
    install_stock_feishu_ingress(module)
    Adapter._trusted_ingress_admitter = staticmethod(ingress.admit_trusted_feishu_ingress)
    monkeypatch.setattr(ingress, "load_feishu_module", lambda: module)
    adapter = Adapter(PlatformConfig(enabled=True, extra={"app_id": "cli_trusted"}))
    adapter._app_id = "cli_trusted"
    adapter._client = object()
    adapter._loop = asyncio.get_running_loop()
    adapter._admins = adapter._allowed_group_users = set()
    tasks, resolved = [], []

    def submit(_loop, coro):
        tasks.append(asyncio.create_task(coro))
        return True

    adapter._submit_on_loop = submit
    monkeypatch.setattr(tools.approval, "resolve_gateway_approval", lambda session, choice: resolved.append(choice) or 1)
    adapter._write_update_prompt_response = lambda answer: resolved.append(answer)

    async def send(**kwargs):
        return object()

    adapter._feishu_send_with_retry = send
    adapter._finalize_send_result = lambda *args: SendResult(success=True, message_id="om_card")

    def prompt():
        if kind == "approval":
            return adapter.send_exec_approval("oc_dm", "echo safe", "owner_a")
        return adapter.send_update_prompt("oc_dm", "safe?", session_key="owner_a")

    # An ambient caller cannot provide its own actor/profile strings and mint a card.
    assert not (await prompt()).success
    ticket = adapter._issue_trusted_ingress_ticket("im.message.receive_v1", _stock_callback(), transport="websocket")
    admission = ingress.admit_trusted_feishu_ingress(ticket=ticket, adapter=adapter)
    envelope = _TrustedFeishuEnvelope(_stock_callback(), ticket, admission)
    event = NS(source=NS(platform="feishu", user_id="ou_a", chat_id="oc_dm", chat_type="p2p"),
               message_id="om_stock", raw_message=envelope)

    async def handle(_event):
        # Core copies ContextVars to its worker, then submits the card coroutine
        # back onto the gateway loop. Exercise both hops with the real sender.
        result = await asyncio.to_thread(lambda: asyncio.run_coroutine_threadsafe(prompt(), adapter._loop).result(5))
        assert result.success

    adapter.handle_message = handle
    await adapter._dispatch_inbound_event(event)
    states = adapter._approval_state if kind == "approval" else adapter._update_prompt_state
    ident = next(iter(states))
    assert states[ident]["actor_id"] == "ou_a"
    assert states[ident]["profile_name"] == "profile_a"
    value = ({"hermes_action": "approve_once", "approval_id": ident} if kind == "approval"
             else {"hermes_update_prompt_action": "y", "update_prompt_id": ident})
    actor = "ou_b" if attack == "other_actor" else "ou_unknown" if attack == "unknown_actor" else "ou_a"
    data = _stock_callback(actor, card=True, event_id="card_answer")
    data.event.action.value = value
    data.event.context.open_message_id = "om_wrong" if attack == "message" else "om_card"
    if attack == "chat":
        data.event.context.open_chat_id = "oc_other"
    if attack == "route":
        routes.upsert(user_id="u_a", profile_name="profile_b", open_id="ou_a", union_id="on_a")
    if attack == "unadmitted":
        handler = adapter._handle_approval_card_action if kind == "approval" else adapter._handle_update_prompt_card_action
        handler(event=data.event, action_value=value, loop=adapter._loop)
    else:
        adapter._on_card_action_trigger(data)
    if attack == "late_route":
        routes.upsert(user_id="u_a", profile_name="profile_b", open_id="ou_a", union_id="on_a")
    if tasks:
        await asyncio.gather(*tasks)
    assert resolved == (["once" if kind == "approval" else "y"] if attack == "valid" else [])
    assert (ident not in states) == (attack == "valid")


# ── Round 5: owner binding outlives the 300s ticket; text answers need the owner ──

def _group_callback(actor, *, chat_id="oc_group", message_id="om_group", event_id="evt_group_msg"):
    return NS(header=NS(event_id=event_id), event=NS(
        sender=NS(sender_id=NS(open_id=actor), sender_type="user"),
        message=NS(chat_id=chat_id, message_id=message_id),
        operator=NS(open_id=actor),
        context=NS(open_chat_id=chat_id, open_message_id=message_id),
        action=NS(tag="button", form_value=None),
    ))


def _real_prompt_adapter(monkeypatch):
    from gateway.config import PlatformConfig
    from gateway.platforms.base import SendResult
    from plugins.platforms.feishu import adapter as stock
    from hermes_multitenancy.feishu_ingress_compat import install_stock_feishu_ingress
    import tools.approval

    # install() wraps register_gateway_notify process-wide; restore it after the test.
    monkeypatch.setattr(tools.approval, "register_gateway_notify", tools.approval.register_gateway_notify)

    class Adapter(stock.FeishuAdapter):
        pass

    module = NS(FeishuAdapter=Adapter)
    install_stock_feishu_ingress(module)
    Adapter._trusted_ingress_admitter = staticmethod(ingress.admit_trusted_feishu_ingress)
    monkeypatch.setattr(ingress, "load_feishu_module", lambda: module)
    adapter = Adapter(PlatformConfig(enabled=True, extra={"app_id": "cli_trusted"}))
    adapter._app_id = "cli_trusted"
    adapter._client = object()
    adapter._loop = asyncio.get_running_loop()
    adapter._admins = adapter._allowed_group_users = set()
    tasks, sent = [], []

    def submit(_loop, coro):
        tasks.append(asyncio.create_task(coro))
        return True

    async def send(**kwargs):
        sent.append(kwargs)
        return object()

    adapter._submit_on_loop = submit
    adapter._feishu_send_with_retry = send
    adapter._finalize_send_result = lambda *args: SendResult(success=True, message_id="om_card")
    return adapter, tasks, sent


def _admitted_event(adapter, actor, *, chat_id="oc_dm", chat_type="p2p", message_id="om_stock",
                    event_id="evt_stock", text=""):
    from hermes_multitenancy.feishu_ingress_compat import _TrustedFeishuEnvelope

    callback = (_stock_callback(actor, event_id=event_id) if chat_id == "oc_dm"
                else _group_callback(actor, chat_id=chat_id, message_id=message_id, event_id=event_id))
    ticket = adapter._issue_trusted_ingress_ticket("im.message.receive_v1", callback, transport="websocket")
    admission = ingress.admit_trusted_feishu_ingress(ticket=ticket, adapter=adapter)
    assert admission is not None
    envelope = _TrustedFeishuEnvelope(callback, ticket, admission)
    args = text.split(maxsplit=1)[1] if " " in text else ""
    # Stamped the way the MT guard stamps an admitted event before core sees it.
    return NS(source=NS(platform="feishu", user_id=actor, chat_id=chat_id, chat_type=chat_type),
              message_id=ticket.message_id, raw_message=envelope, text=text,
              get_command_args=lambda: args, trusted_feishu_ingress_ticket=ticket,
              trusted_feishu_ingress_admission=admission)


@pytest.mark.asyncio
@pytest.mark.parametrize("clicker", ["ou_a", "ou_b"])
async def test_long_turn_approval_card_stays_bound_to_original_actor(routes, monkeypatch, clicker):
    """A turn that needs approval after the 300s ticket expired still gets an owner-bound card."""
    import tools.approval

    adapter, tasks, _sent = _real_prompt_adapter(monkeypatch)
    resolved = []
    monkeypatch.setattr(tools.approval, "resolve_gateway_approval", lambda session, choice: resolved.append(choice) or 1)
    real_time = time.time
    offset = [0.0]
    monkeypatch.setattr(time, "time", lambda: real_time() + offset[0])
    event = _admitted_event(adapter, "ou_a")

    async def handle(_event):
        offset[0] = 400.0  # the agent turn outlived the originating ticket
        result = await asyncio.to_thread(lambda: asyncio.run_coroutine_threadsafe(
            adapter.send_exec_approval("oc_dm", "echo safe", "owner_a"), adapter._loop).result(5))
        assert result.success

    adapter.handle_message = handle
    await adapter._dispatch_inbound_event(event)
    ident = next(iter(adapter._approval_state))
    assert adapter._approval_state[ident]["actor_id"] == "ou_a"
    data = _stock_callback(clicker, card=True, event_id=f"late_click_{clicker}")
    data.event.action.value = {"hermes_action": "approve_once", "approval_id": ident}
    data.event.context.open_message_id = "om_card"
    adapter._on_card_action_trigger(data)
    if tasks:
        await asyncio.gather(*tasks)
    assert resolved == (["once"] if clicker == "ou_a" else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["ambient", "route_changed"])
async def test_unbound_approval_is_declined_with_notice_never_text(routes, monkeypatch, case):
    from gateway.relay.egress import declined_send
    from hermes_multitenancy.feishu_ingress_compat import _APPROVAL_OWNER_UNAVAILABLE_NOTICE

    adapter, _tasks, sent = _real_prompt_adapter(monkeypatch)
    results = []

    async def prompt():
        return await adapter.send_exec_approval("oc_dm", "echo safe", "owner_a")

    if case == "ambient":
        results.append(await prompt())
    else:
        async def handle(_event):
            routes.upsert(user_id="u_a", profile_name="profile_b", open_id="ou_a", union_id="on_a")
            results.append(await asyncio.to_thread(
                lambda: asyncio.run_coroutine_threadsafe(prompt(), adapter._loop).result(5)))

        adapter.handle_message = handle
        await adapter._dispatch_inbound_event(_admitted_event(adapter, "ou_a"))
    [result] = results
    assert not result.success
    # "declined" makes core raise instead of re-sending the prompt as answerable text.
    assert declined_send(result)
    assert adapter._approval_state == {}
    assert [json.loads(call["payload"]).get("text") for call in sent] == [_APPROVAL_OWNER_UNAVAILABLE_NOTICE]


_THREAD_SESSION = "agent:main:feishu:group:oc_group:om_topic_root"


def _shared_thread_runner():
    from gateway.slash_commands import GatewaySlashCommandsMixin
    from hermes_multitenancy.gateway_ownership import _patch_gateway_text_approval_owner

    class Runner(GatewaySlashCommandsMixin):
        _pending_approvals: dict = {}

        def _session_key_for_source(self, source):
            return _THREAD_SESSION  # thread_sessions_per_user=False: one session per topic thread

        async def _deliver_approval_confirmation(self, event, confirmation_text, verb):
            return confirmation_text

    _patch_gateway_text_approval_owner(Runner)
    return Runner()


@pytest.mark.asyncio
async def test_shared_thread_text_approval_only_by_the_requesting_actor(routes, monkeypatch, tmp_path):
    import contextvars
    import threading
    import tools.approval
    from tools.approval_gateway_wait import _await_gateway_decision
    from hermes_multitenancy.gateway_ownership import _TEXT_APPROVAL_NOT_OWNER

    routes.upsert_group(chat_id="oc_group", profile_name="profile_group", owner_open_id="ou_a",
                        display_label="group")
    (tmp_path / "profile_group").mkdir()
    adapter, _tasks, _sent = _real_prompt_adapter(monkeypatch)
    runner = _shared_thread_runner()
    notified, decisions = [], []
    started = threading.Event()

    async def handle(_event):
        # Core registers the turn's notify callback and the agent thread (a copy of this
        # admitted context) blocks on the dangerous-command approval.
        tools.approval.register_gateway_notify(_THREAD_SESSION, lambda data: (notified.append(data), started.set()))
        notify = tools.approval._gateway_notify_cbs[_THREAD_SESSION]
        ctx = contextvars.copy_context()
        threading.Thread(target=ctx.run, args=(lambda: decisions.append(_await_gateway_decision(
            _THREAD_SESSION, notify, {"command": "rm -rf /tmp/r5", "description": "d", "pattern_key": "k"})),),
            daemon=True).start()

    adapter.handle_message = handle
    try:
        await adapter._dispatch_inbound_event(_admitted_event(
            adapter, "ou_a", chat_id="oc_group", chat_type="group", message_id="om_owner", event_id="evt_owner"))
        assert await asyncio.to_thread(started.wait, 5)

        for n, text in enumerate(("/approve", "/approve all", "/deny", "/deny all no")):
            other = _admitted_event(adapter, "ou_b", chat_id="oc_group", chat_type="group",
                                    message_id=f"om_b{n}", event_id=f"evt_b{n}", text=text)
            handler = runner._handle_approve_command if text.startswith("/approve") else runner._handle_deny_command
            assert await handler(other) == _TEXT_APPROVAL_NOT_OWNER
        assert tools.approval.has_blocking_approval(_THREAD_SESSION)
        assert decisions == []

        owner = _admitted_event(adapter, "ou_a", chat_id="oc_group", chat_type="group",
                                message_id="om_a2", event_id="evt_a2", text="/approve")
        assert await runner._handle_approve_command(owner) != _TEXT_APPROVAL_NOT_OWNER
        for _ in range(50):
            if decisions:
                break
            await asyncio.sleep(0.05)
        assert decisions and decisions[0]["choice"] == "once"
    finally:
        tools.approval.unregister_gateway_notify(_THREAD_SESSION)


@pytest.mark.asyncio
async def test_text_approval_without_recorded_owner_is_refused(routes, monkeypatch):
    """An approval raised outside any admitted Feishu turn has no owner: text cannot answer it."""
    import threading
    import tools.approval
    from tools.approval_gateway_wait import _await_gateway_decision
    from hermes_multitenancy.gateway_ownership import _TEXT_APPROVAL_NOT_OWNER

    adapter, _tasks, _sent = _real_prompt_adapter(monkeypatch)
    runner = _shared_thread_runner()
    started = threading.Event()
    tools.approval.register_gateway_notify(_THREAD_SESSION, lambda data: started.set())
    notify = tools.approval._gateway_notify_cbs[_THREAD_SESSION]
    threading.Thread(target=lambda: _await_gateway_decision(
        _THREAD_SESSION, notify, {"command": "rm -rf /tmp/r5", "description": "d", "pattern_key": "k"}),
        daemon=True).start()
    try:
        assert await asyncio.to_thread(started.wait, 5)
        event = _admitted_event(adapter, "ou_a", text="/approve")
        assert await runner._handle_approve_command(event) == _TEXT_APPROVAL_NOT_OWNER
        assert tools.approval.has_blocking_approval(_THREAD_SESSION)
    finally:
        tools.approval.unregister_gateway_notify(_THREAD_SESSION)


# ---------------------------------------------------------------------------
# Round 6: a second MT copy (another HERMES_HOME's PluginManager loading the
# entry point) must not take over ingress.
# ---------------------------------------------------------------------------

_MT_OWNER_ATTR = "_hermes_multitenancy_registered_module"


@contextmanager
def _second_mt_copy(name="hermes_mt_dup_home_b"):
    import importlib.util

    import hermes_multitenancy as owner

    package_dir = Path(owner.__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        name, package_dir / "__init__.py", submodule_search_locations=[str(package_dir)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        for loaded in list(sys.modules):
            if loaded == name or loaded.startswith(name + "."):
                sys.modules.pop(loaded, None)


def test_second_mt_copy_keeps_admitter_and_inbound_still_admitted(
    stock_ingress, monkeypatch, tmp_path,
):
    import hermes_multitenancy
    from hermes_multitenancy import plugin_entry

    adapter, seen, module = stock_ingress
    Adapter = type(adapter)
    # The first (directory-plugin) copy registered and owns the live adapter.
    monkeypatch.setattr(sys, _MT_OWNER_ATTR, hermes_multitenancy.__name__, raising=False)
    owner_admitter = Adapter._trusted_ingress_admitter
    assert owner_admitter is ingress.admit_trusted_feishu_ingress

    hooks = []

    class Ctx:
        def register_hook(self, name, callback):
            hooks.append((name, callback))

    with _second_mt_copy() as dup:
        dup_ingress = importlib.import_module(f"{dup.__name__}.trusted_feishu_ingress")
        monkeypatch.setattr(dup_ingress, "load_live_feishu_module", lambda: module)
        monkeypatch.setattr(dup_ingress, "load_feishu_module", lambda: module)
        # What the copy's _register would do first: install its own ingress.
        monkeypatch.setattr(dup, "_register", lambda ctx: dup_ingress.install_trusted_feishu_ingress_admission())
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "tenant_b"))
        dup.register(Ctx())

    assert Adapter._trusted_ingress_admitter is owner_admitter
    assert getattr(sys, _MT_OWNER_ATTR) == hermes_multitenancy.__name__
    # The other home's manager still carries MT's hooks — the owner's callables.
    assert dict(hooks)["pre_gateway_dispatch"] is plugin_entry._dispatch_with_worker_init
    assert [name for name, _ in hooks] == ["post_tool_call", "transform_tool_result", "pre_gateway_dispatch"]

    adapter._on_message_event(_stock_callback(event_id="evt_after_dup"))
    assert len(seen) == 1
    assert seen[0].trusted_feishu_ingress_admission.profile_name == "profile_a"


def test_second_copy_admitter_install_refuses_to_replace_owner(stock_ingress, monkeypatch):
    adapter, _seen, module = stock_ingress
    Adapter = type(adapter)
    with _second_mt_copy("hermes_mt_dup_install") as dup:
        dup_ingress = importlib.import_module(f"{dup.__name__}.trusted_feishu_ingress")
        monkeypatch.setattr(dup_ingress, "load_live_feishu_module", lambda: module)
        with pytest.raises(RuntimeError, match="already installed by hermes_multitenancy.trusted_feishu_ingress"):
            dup_ingress.install_trusted_feishu_ingress_admission()
    assert Adapter._trusted_ingress_admitter is ingress.admit_trusted_feishu_ingress
    # Re-installing from the owner copy stays idempotent.
    ingress.install_trusted_feishu_ingress_admission()
    assert Adapter._trusted_ingress_admitter is ingress.admit_trusted_feishu_ingress


def test_rejected_envelope_is_logged_with_message_id_and_reason(stock_ingress, caplog):
    adapter, seen, _module = stock_ingress
    Adapter = type(adapter)

    def foreign_admitter(*, ticket, adapter):
        admission = ingress.admit_trusted_feishu_ingress(ticket=ticket, adapter=adapter)
        return replace(admission, _seal=object())  # what another copy's admission looks like

    Adapter._trusted_ingress_admitter = staticmethod(foreign_admitter)
    with caplog.at_level("WARNING"):
        adapter._on_message_event(_stock_callback(event_id="evt_foreign"))
    assert seen == []
    assert (
        "envelope rejected callback=_on_message_event message_id=om_stock "
        "reason=admission_not_from_this_copy"
    ) in caplog.text
