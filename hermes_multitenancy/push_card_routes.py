"""``POST /api/run-broker/notify-card`` + ``GET .../{registry_id}`` (SPEC P2).

External systems call ``notify-card`` to push a fill-card to one registered
user. Auth reuses the ingest owner-key体系 (fail-closed) and additionally binds
each key to an ``allowed_scenes`` whitelist — the request body carries **no
``skill``**; the skill is derived server-side from ``scene`` via the scene
registry, so a leaked key's blast radius is only its authorized scenes
(design §2.1, P0-1).

Semantics:
* 202 + ``registry_id`` on accept — delivery is async via the rate-limited queue.
* Repeat ``idempotency_key`` → 200 + the original registry (no second card).
* ``business_key`` collides with an in-flight row → 409 + the original registry.
* Unknown/blocked scene, or a target not in the routing table → 4xx.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from . import push_registry as _reg
from . import push_scenes as _scenes
from . import push_send_queue as _queue
from .webui_broker import periphery as _periphery

logger = logging.getLogger(__name__)


# --- target resolution seam (default reuses the routing table) -----------

def _default_target_resolver(*, open_id: str, union_id: str) -> Optional[dict[str, Any]]:
    """Resolve a push target to its sending profile via the routing table.

    A target not present (never onboarded / off-boarded) → None → 4xx."""
    from .routing import RoutingTable

    table = RoutingTable()
    row = None
    if open_id:
        row = table.lookup_by_open_id(open_id)
    if row is None and union_id:
        row = table.lookup_by_union_id(union_id)
    if row is None:
        return None
    return {
        "open_id": row.open_id or open_id,
        "union_id": row.union_id or union_id,
        "profile_name": row.profile_name,
        "chat_id": row.chat_id,
    }


_target_resolver: Callable[..., Optional[dict[str, Any]]] = _default_target_resolver

#: Send dispatch seam. Default fire-and-forget onto the running loop; tests
#: override with a recorder so the endpoint stays deterministic.
_send_dispatch: Callable[[str], None] = lambda registry_id: asyncio.ensure_future(
    _queue.get_send_queue().process(registry_id)
)


def override_target_resolver(fn: Optional[Callable[..., Optional[dict[str, Any]]]]) -> None:
    global _target_resolver
    _target_resolver = fn if fn is not None else _default_target_resolver


def override_send_dispatch(fn: Optional[Callable[[str], None]]) -> None:
    global _send_dispatch
    _send_dispatch = fn if fn is not None else (
        lambda registry_id: asyncio.ensure_future(_queue.get_send_queue().process(registry_id))
    )


# --- helpers -------------------------------------------------------------

def _next_working_day_eod(now: Optional[datetime] = None) -> int:
    """Default expiry: 23:59:59 of the next working day (Mon–Fri).

    Real employees reply the next day as a matter of course — same-day 23:59 is
    too short (design §2.1, P1-3)."""
    now = now or datetime.now()
    day = now + timedelta(days=1)
    while day.weekday() >= 5:  # 5=Sat, 6=Sun
        day = day + timedelta(days=1)
    eod = day.replace(hour=23, minute=59, second=59, microsecond=0)
    return int(eod.timestamp())


def _default_business_key(scene: str, open_id: str) -> str:
    return f"{scene}:{open_id}:{datetime.now().strftime('%Y-%m-%d')}"


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    """Caller-facing projection of a registry row — no nonce, no internal marker."""
    return {
        "registry_id": row.get("registry_id"),
        "scene": row.get("scene"),
        "status": row.get("status"),
        "target_open_id": row.get("target_open_id"),
        "message_id": row.get("message_id"),
        "business_key": row.get("business_key"),
        "idempotency_key": row.get("idempotency_key"),
        "expires_at": row.get("expires_at"),
        "last_error": row.get("last_error"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


# --- handlers ------------------------------------------------------------

def register_push_card_routes(app: Any) -> None:
    """Register the notify-card endpoints on the run-broker app."""
    from aiohttp import web

    async def handle_notify_card(request):
        binding = _periphery._ingest_binding_for_request(request)
        if binding is None:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response(
                {"ok": False, "error": "body must be a JSON object"}, status=400
            )

        scene = str(payload.get("scene") or "").strip()
        if not scene:
            return web.json_response({"ok": False, "error": "scene is required"}, status=400)
        scene_def = _scenes.get_scene(scene)
        if scene_def is None:
            return web.json_response(
                {"ok": False, "error": f"unknown scene: {scene}"}, status=400
            )
        allowed = binding.get("allowed_scenes") or []
        if scene not in allowed:
            # fail-closed: a key may only push scenes explicitly granted to it.
            return web.json_response(
                {"ok": False, "error": f"scene not authorized for this key: {scene}"},
                status=403,
            )

        open_id = str(payload.get("open_id") or "").strip()
        union_id = str(payload.get("union_id") or "").strip()
        if not open_id and not union_id:
            return web.json_response(
                {"ok": False, "error": "open_id or union_id is required"}, status=400
            )
        target = _target_resolver(open_id=open_id, union_id=union_id)
        if target is None:
            return web.json_response(
                {"ok": False, "error": "target not registered"}, status=404
            )
        resolved_open_id = str(target.get("open_id") or open_id)
        profile_name = str(target.get("profile_name") or "").strip()
        if not profile_name:
            return web.json_response(
                {"ok": False, "error": "target has no sending profile"}, status=404
            )

        body_payload = payload.get("payload")
        if body_payload is not None and not isinstance(body_payload, dict):
            return web.json_response(
                {"ok": False, "error": "payload must be an object"}, status=400
            )
        card = payload.get("card") if isinstance(payload.get("card"), dict) else None
        if card is None:
            # No card supplied → build a default one (title + scene name + a
            # "reply to fill" prompt). A None card would serialize to "null" and
            # fail the Feishu send (finding no-default-card-when-omitted).
            from . import push_fill_form as _fill

            card = _fill.default_scene_card(scene_def)
        idempotency_key = str(payload.get("idempotency_key") or "").strip() or None
        business_key = str(payload.get("business_key") or "").strip() or _default_business_key(
            scene, resolved_open_id
        )
        expires_at = payload.get("expires_at")
        expires_at = int(expires_at) if isinstance(expires_at, (int, float)) else _next_working_day_eod()

        store = _reg.get_registry_store()
        result = store.create(
            scene=scene,
            skill=scene_def.skill,
            target_open_id=resolved_open_id,
            target_union_id=str(target.get("union_id") or union_id) or None,
            profile_name=profile_name,
            chat_id=target.get("chat_id"),
            business_key=business_key,
            payload=body_payload,
            card=card,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
        )

        if result.conflict == "idempotent":
            return web.json_response(
                {"ok": True, "duplicate": True, "registry_id": result.row["registry_id"],
                 "status": result.row["status"]},
                status=200,
            )
        if result.conflict == "business_key":
            return web.json_response(
                {"ok": False, "error": "business_key already in flight",
                 "registry_id": result.row["registry_id"], "status": result.row["status"]},
                status=409,
            )

        registry_id = result.row["registry_id"]
        try:
            _send_dispatch(registry_id)
        except Exception:
            logger.exception("[push-card] failed to dispatch send for %s", registry_id)
        return web.json_response(
            {"ok": True, "registry_id": registry_id, "status": _reg.STATUS_SENDING},
            status=202,
        )

    async def handle_notify_card_status(request):
        binding = _periphery._ingest_binding_for_request(request)
        if binding is None:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        registry_id = str(request.match_info.get("registry_id") or "").strip()
        row = _reg.get_registry_store().get(registry_id)
        if row is None:
            return web.json_response({"ok": False, "error": "not found"}, status=404)
        allowed = binding.get("allowed_scenes") or []
        if row["scene"] not in allowed:
            return web.json_response({"ok": False, "error": "not found"}, status=404)
        return web.json_response({"ok": True, **_public_row(row)}, status=200)

    app.router.add_post("/api/run-broker/notify-card", handle_notify_card)
    app.router.add_get("/api/run-broker/notify-card/{registry_id}", handle_notify_card_status)
