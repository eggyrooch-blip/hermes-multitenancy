"""Plugin-layer hook: register helpdesk ticket events onto the EXISTING Feishu
WS dispatcher built by core ``gateway/platforms/feishu.py``.

Why this shape:
* Feishu allows exactly ONE long-connection per app (core feishu.py itself refuses
  to start a second ws client). So we MUST NOT open our own ``lark.ws.Client`` — we
  reuse the connection the router profile already runs for the helpdesk app.
* We never edit core. We monkeypatch ``FeishuAdapter._build_event_handler`` from the
  multitenancy plugin (same pattern as feishu_reply_quote_api / feishu_merge_forward).
  After core builds its dispatcher, we add helpdesk customized-event processors into
  the dispatcher's ``_processorMap`` using the SDK's own builder to create them.
* The handler is thin: fast-ack, then forward the raw event to the local run-broker
  helpdesk endpoint in a background thread (Feishu's 3s event timeout must not wait
  on RAG/inference). Business logic + the test-helpdesk safety filter live behind the
  broker endpoint (see feishu_helpdesk_event.handle_helpdesk_event).
"""
from __future__ import annotations

import functools
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

logger = logging.getLogger(__name__)

# Only the message event carries a user question (fires for every guest message,
# including the first one that opens a ticket). ticket.created is just an open
# notification the handler doesn't act on, so we don't register it (no dead path).
HELPDESK_EVENT_TYPES = (
    "helpdesk.ticket_message.created_v1",
)
_PATCH_FLAG = "_hermes_mt_helpdesk_events_patched"
_executor = ThreadPoolExecutor(max_workers=4)


def _broker_url() -> str:
    configured = os.environ.get("HERMES_FEISHU_HELPDESK_WS_BROKER_URL", "").strip()
    if configured:
        return configured
    port = os.environ.get("HERMES_MULTITENANCY_RUN_BROKER_PORT", "8766").strip() or "8766"
    return f"http://127.0.0.1:{port}/api/run-broker/feishu/helpdesk/events"


def _broker_key() -> str:
    # Must be the key the broker's _authorized() accepts (the master run-broker key),
    # otherwise every forward 401s. An optional dedicated key is only honoured if the
    # broker is also configured to accept it.
    return str(os.environ.get("HERMES_MULTITENANCY_RUN_BROKER_KEY", "") or "").strip()


def _to_plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_plain(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, (list, tuple, set)):
        return [_to_plain(v) for v in value]
    if hasattr(value, "__dict__"):
        return {str(k): _to_plain(v) for k, v in vars(value).items() if not str(k).startswith("_") and v is not None}
    return str(value)


def _event_to_payload(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        return dict(event)
    try:
        import lark_oapi as lark

        payload = json.loads(lark.JSON.marshal(event))
        if isinstance(payload, dict):
            return payload
    except Exception:
        logger.debug("[multitenancy] helpdesk event marshal failed", exc_info=True)
    plain = _to_plain(event)
    return plain if isinstance(plain, dict) else {"event": plain}


def _forward(payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    key = _broker_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urlrequest.Request(_broker_url(), data=body, headers=headers, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=5) as resp:
            if int(resp.status) >= 300:
                logger.warning("[multitenancy] helpdesk event forward status=%s", resp.status)
    except urlerror.HTTPError as exc:
        logger.warning("[multitenancy] helpdesk event forward http=%s body=%s", exc.code, exc.read()[:300])
    except Exception:
        logger.exception("[multitenancy] helpdesk event forward failed")


def _make_handler(event_type: str):
    def _handle(event: Any) -> None:
        # fast-ack: never block the ws thread (Feishu 3s timeout) — forward in background
        payload = _event_to_payload(event)
        payload.setdefault("_hermes_event_type", event_type)
        logger.info("[multitenancy] helpdesk ws event received type=%s", event_type)
        _executor.submit(_forward, payload)

    return _handle


def _register_helpdesk_processors(handler: Any) -> None:
    """Add helpdesk customized-event processors to an already-built dispatcher.

    Uses the SDK's own builder to construct the processor objects (stable public
    API), then merges them into the live dispatcher's _processorMap. No second
    ws client is ever created.
    """
    if handler is None:
        return
    processor_map = getattr(handler, "_processorMap", None)
    if not isinstance(processor_map, dict):
        logger.warning("[multitenancy] dispatcher has no _processorMap; helpdesk events not attached")
        return
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

    tmp = EventDispatcherHandler.builder("", "")
    for event_type in HELPDESK_EVENT_TYPES:
        tmp.register_p2_customized_event(event_type, _make_handler(event_type))
    added = 0
    for key, processor in getattr(tmp, "_processorMap", {}).items():
        if key not in processor_map:
            processor_map[key] = processor
            added += 1
    logger.info("[multitenancy] attached %d helpdesk event processor(s) to existing dispatcher", added)


def install_feishu_helpdesk_events_patch() -> None:
    """Idempotent — safe to call from plugin register()."""
    try:
        from gateway.platforms.feishu import FeishuAdapter  # type: ignore
    except Exception:
        logger.info("[multitenancy] FeishuAdapter not importable yet; helpdesk events patch deferred")
        return
    original = getattr(FeishuAdapter, "_build_event_handler", None)
    if original is None or getattr(original, _PATCH_FLAG, False):
        return

    @functools.wraps(original)
    def patched(self: Any) -> Any:
        handler = original(self)
        try:
            _register_helpdesk_processors(handler)
        except Exception:
            logger.exception("[multitenancy] failed to attach helpdesk events (gateway still functional)")
        return handler

    setattr(patched, _PATCH_FLAG, True)
    FeishuAdapter._build_event_handler = patched
    logger.info("[multitenancy] helpdesk events patch installed on FeishuAdapter._build_event_handler")
