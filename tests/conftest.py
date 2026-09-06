"""Test isolation: reset module-level singletons before/after every test.

Without this, module-level RoutingTable / RuntimePool / SessionStore
default to disk paths (~/.hermes/multitenancy.db etc) and accumulate state
across tests — causing flaky test_session_memory_accumulates_across_turns
when an earlier test writes the same DB.
"""
from __future__ import annotations

import json
import itertools
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


def _cgroup_cpu_quota() -> int | None:
    """容器 CPU 配额(向上取整),读不到返回 None。

    CI runner(rootless podman)里 os.cpu_count() 报宿主机核数,xdist `-n auto`
    据此开 32+ worker 挤在几核配额上,线程/子进程饿死到 30s 预算都不够
    (2026-08-14 pipeline 538416)。cgroup v2 cpu.max 才是真实可用算力。
    """
    try:
        text = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if text and text[0] != "max":
            quota = int(text[0])
            period = int(text[1]) if len(text) > 1 else 100000
            if quota > 0 and period > 0:
                return max(1, -(-quota // period))
    except Exception:
        pass
    return None


_quota = _cgroup_cpu_quota()
if _quota is None and os.environ.get("CI"):
    # 配额读不到(cpu.max="max" 或 cgroup v1)但在共享 runner 上:host nproc
    # 永远不是正确答案 —— 别的 pipeline 在同机竞争。钉一个保守值。
    _quota = 8
if _quota is not None and not os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"):
    os.environ["PYTEST_XDIST_AUTO_NUM_WORKERS"] = str(_quota)
    import sys as _sys

    print(f"[conftest] xdist workers capped: quota={_quota}", file=_sys.stderr)


_CARD_MESSAGES = itertools.count()
_SYNTHETIC_FEISHU_ADAPTER = "hermes_plugins.feishu_platform.adapter"


class _CardTicket:
    def __init__(self, *, actor_id, actor_id_type, account_id, chat_id, message_id, thread_id):
        self.actor_id = actor_id
        self.actor_id_type = actor_id_type
        self.account_id = account_id
        self.chat_id = chat_id
        self.message_id = message_id
        self.thread_id = thread_id

    def is_valid(self, *, account_id):
        return account_id == self.account_id


def admit_card_callback(data, *, account_id="", chat_type="p2p"):
    """Attach the trusted edge result omitted by older business-action tests."""
    from hermes_multitenancy.trusted_feishu_ingress import TrustedFeishuAdmission

    event = data.event
    context = event.context
    if not hasattr(context, "open_chat_id"):
        context.open_chat_id = "oc_test"
    if not hasattr(context, "open_message_id"):
        context.open_message_id = f"om_test_{next(_CARD_MESSAGES)}"
    operator = event.operator
    actor_id_type, actor_id = next(
        ((key, getattr(operator, key, None)) for key in ("open_id", "union_id", "user_id")
         if getattr(operator, key, None)),
        ("open_id", ""),
    )
    thread_id = str(getattr(context, "open_thread_id", "") or "")
    ticket = _CardTicket(
        actor_id=actor_id,
        actor_id_type=actor_id_type,
        account_id=account_id,
        chat_id=context.open_chat_id,
        message_id=context.open_message_id,
        thread_id=thread_id,
    )
    data.trusted_feishu_ingress_ticket = ticket
    data.trusted_feishu_ingress_admission = TrustedFeishuAdmission(
        profile_name="profile_test",
        route_version=1,
        actor_id=actor_id,
        actor_id_type=actor_id_type,
        actor_subject=actor_id,
        chat_type=chat_type,
        chat_id=ticket.chat_id,
        message_id=ticket.message_id,
        credential_subject=actor_id,
        tool_scope="feishu:bot" if chat_type == "group" else "feishu:user",
        ticket_fingerprint="fp_test",
    )
    return data


# --- card-action response shape ------------------------------------------------
#
# `_toast_response` answers a card click with a plain dict when `lark_oapi` is
# absent (this venv / CI) and with a real `P2CardActionTriggerResponse` when it
# is installed (the gateway runtime, and the hermes-agent companion shipped as
# `71cb1a351`). Both are the same contract; a test that subscripts the response
# is asserting on which of the two it happened to get, and goes red the moment
# the SDK is present. These two readers are the shape-agnostic way to look.


def card_toast(response) -> dict:
    """The toast this card-action response carries, whichever shape it wears."""
    if isinstance(response, dict):
        assert response.get("kind") == "toast", f"not a toast response: {response!r}"
        return response["toast"]
    toast = getattr(response, "toast", None)
    assert toast is not None, f"not a toast response: {response!r}"
    return toast


def card_response_bytes(response) -> str:
    """Everything a caller can observe of a response, as one comparable string.

    Needed because two `P2CardActionTriggerResponse` instances are never `==`
    (the SDK class defines no `__eq__`), so "these two answers are
    indistinguishable" has to be asserted on the wire form, not on identity.
    """
    payload = response if isinstance(response, dict) else vars(response)
    return json.dumps(
        dict(payload, __type__=type(response).__name__),
        sort_keys=True,
        ensure_ascii=False,
        default=repr,
    )


@pytest.fixture(autouse=True)
def _enable_security_audit(monkeypatch):
    """Security audit now defaults OFF in prod (so a default-off deploy is
    byte-identical — no /var/log JSONL side-effect). The test suite asserts
    audit CONTENT (redaction/hashing/event presence), so it opts audit ON here.
    The dedicated default-off behavior test overrides this env explicitly."""
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_ENABLED", "1")


@pytest.fixture(autouse=True)
def _isolate_loaded_feishu_plugin(monkeypatch):
    """Tests must opt into Agent's process-global Feishu plugin state.

    Agent plugin discovery is process-global: besides loading the synthetic
    module it leaves a concrete or deferred ``feishu`` registry entry behind.
    Removing only the module lets ``load_feishu_module()`` immediately
    materialize it again, so fake legacy/plugin fixtures become order-dependent
    under xdist.  Hide all three pieces for each test; tests of deferred loading
    install their own isolated registry and synthetic module explicitly.
    """
    monkeypatch.delitem(sys.modules, _SYNTHETIC_FEISHU_ADAPTER, raising=False)
    registry_module = sys.modules.get("gateway.platform_registry")
    registry = getattr(registry_module, "platform_registry", None)
    entries = getattr(registry, "_entries", None)
    deferred = getattr(registry, "_deferred", None)
    if isinstance(entries, dict):
        monkeypatch.delitem(entries, "feishu", raising=False)
    if isinstance(deferred, dict):
        monkeypatch.delitem(deferred, "feishu", raising=False)


@pytest.fixture(autouse=True)
def _isolate_router_singletons():
    """Reset router-level singletons before and after each test."""
    from hermes_multitenancy import router

    # Pre: clean slate (covers tests that don't explicitly set up state)
    router.override_routing_table(":memory:")
    router.override_pool(None)
    router.override_session_store(":memory:")
    router._session_history.clear()
    router._session_loaded.clear()
    router._user_inflight_tasks.clear()

    yield

    # Post: same clean teardown
    router.override_routing_table(":memory:")
    router.override_pool(None)
    router.override_session_store(":memory:")
    router._session_history.clear()
    router._session_loaded.clear()
    router._user_inflight_tasks.clear()
