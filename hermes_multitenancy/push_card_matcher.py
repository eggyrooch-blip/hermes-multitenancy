"""Inbound matcher for the push-card fill loop (SPEC P3, design §2.3).

An employee's natural-language DM reply has to be routed to the right open
``push_registry`` row (and its scene's fill skill) — or left completely alone.
The responsibility chain, in order, is::

    1. quoted reply (parent_id/root_id) exactly hits a registry.message_id
         → route to that row; if the row is ``expired`` → explicit "已过期" exit
    2. no quote → the user's open (pending/clarifying, not-expired) rows:
         0 → grace-probe a fresh ``sending`` row (P1-2 send race), else passthrough
         1 → intent gate (cheap heuristic short-circuit; LLM only when ambiguous)
               related → route, with a逃生门 hint on first engagement
               unrelated / escape phrase → passthrough
         >1 → disambiguate by scene signal; exactly one positive → route,
               otherwise ask the user to quote a specific card
    3. anything else → passthrough, zero touch (the regression contract)

The matcher **only ever reads the registry table** — card text is never a
routing input (in-band markers have a forgery surface + the 7月"无不动点"
lesson). On a route it produces the slash-rewrite content (``/{skill} <text>``,
the same mechanism ingest uses) plus a registry-context dict the fill skill
(P4) reads.

The class is pure and fully unit-testable: inject a store, a heuristic, and an
optional LLM intent function. The live FeishuAdapter wiring at the bottom
mirrors ``feishu_reply_quote_api`` and is fail-open — any error falls through
to untouched core delivery.
"""
from __future__ import annotations

import functools
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from . import push_card_metrics as _metrics
from . import push_registry as _reg
from .push_scenes import MODE_CARD, MODE_YOLO, SceneDefinition, scene_def_for_row

logger = logging.getLogger(__name__)

# --- decision types ------------------------------------------------------

ROUTE = "route"
PASSTHROUGH = "passthrough"
EXPIRED_EXIT = "expired_exit"
DISAMBIGUATE = "disambiguate"
RETRY_GRACE = "retry_grace"

#: How long after create a ``sending`` row is still "fresh" enough that a reply
#: could have raced the message_id write-back (design §2.2 P1-2).
GRACE_WINDOW_S = 3

#: Stripped replies that mean "I'm not filling anything — talk to me normally".
#: This IS the逃生门 (design §2.3): "回复『不是』即可正常对话".
_ESCAPE_PHRASES = frozenset({
    "不是", "不", "不用", "不用了", "不填", "取消", "算了", "停", "退出",
    "no", "nope", "cancel", "stop",
})

_NUM_RE = re.compile(r"\d")
_DATE_RE = re.compile(
    r"(今天|昨天|前天|明天|上周|本周|这周|周[一二三四五六日天]|礼拜[一二三四五六日天]"
    r"|\d{1,2}\s*[月/-]\s*\d{1,2}|\d{4}\s*[年/-])"
)
_QUESTION_RE = re.compile(r"[?？]\s*$")
_CHITCHAT_HINTS = ("在吗", "在不在", "你好", "谢谢", "吃什么", "怎么样", "在么", "哈喽", "hi", "hello")


@dataclass
class MatchDecision:
    action: str  # route | passthrough | expired_exit | disambiguate | retry_grace
    registry_id: Optional[str] = None
    row: Optional[dict[str, Any]] = None
    scene: Optional[str] = None
    skill: Optional[str] = None
    #: routing mode of the matched scene — decides ROUTE handling downstream
    #: (``card`` = drive the form loop, ``yolo`` = inject skill + passthrough).
    mode: str = MODE_CARD
    slash_content: Optional[str] = None
    context: dict[str, Any] = field(default_factory=dict)
    reply_text: Optional[str] = None
    reason: Optional[str] = None


# --- heuristic intent gate ----------------------------------------------

def _scene_signal_count(text: str, scene: Optional[SceneDefinition]) -> int:
    """How many scene-shaped signals the reply carries. 0 = no evidence it is
    about this scene; ≥1 = plausibly on-topic. Cheap — no LLM."""
    if scene is None:
        return 0
    lowered = text.lower()
    signals = 0
    has_number_field = False
    has_date_field = False
    for f in scene.fields:
        if f.type == "number":
            has_number_field = True
        if f.type == "date":
            has_date_field = True
        # enum option mentioned verbatim (e.g. "打车")
        for opt in f.options:
            if opt and opt in text:
                signals += 1
        # field label core token (drop the "(元)" style qualifier)
        label = re.split(r"[(（]", f.label)[0].strip()
        if len(label) >= 2 and label in text:
            signals += 1
    if has_number_field and _NUM_RE.search(text):
        signals += 1
    if has_date_field and _DATE_RE.search(text):
        signals += 1
    return signals


def default_heuristic_intent(text: str, scene: Optional[SceneDefinition]) -> str:
    """Return ``'yes' | 'no' | 'maybe'`` cheaply (design §2.3, P1-7 降本).

    Only ``'maybe'`` should ever reach the LLM."""
    t = str(text or "").strip()
    if not t:
        return "no"
    if t.strip().lower() in _ESCAPE_PHRASES:
        return "no"
    signals = _scene_signal_count(t, scene)
    if signals >= 1:
        return "yes"
    lowered = t.lower()
    if _QUESTION_RE.search(t) or any(h in lowered for h in _CHITCHAT_HINTS):
        return "no"
    return "maybe"


# --- matcher -------------------------------------------------------------

class PushCardMatcher:
    def __init__(
        self,
        store: _reg.PushRegistryStore,
        *,
        scene_lookup: Callable[[Optional[dict[str, Any]]], Optional[SceneDefinition]] = scene_def_for_row,
        heuristic: Callable[[str, Optional[SceneDefinition]], str] = default_heuristic_intent,
        llm_intent: Optional[Callable[[str, SceneDefinition, dict[str, Any]], bool]] = None,
    ) -> None:
        self.store = store
        self.scene_lookup = scene_lookup
        self.heuristic = heuristic
        self.llm_intent = llm_intent

    # -- public -----------------------------------------------------------

    def match(
        self,
        *,
        open_id: str,
        text: str,
        quoted_message_id: Optional[str] = None,
        now: Optional[int] = None,
    ) -> MatchDecision:
        """Pure decision — no I/O beyond registry reads, no sleeping."""
        open_id = str(open_id or "")
        text = str(text or "")
        if not open_id:
            return MatchDecision(PASSTHROUGH, reason="no_open_id")

        # 1. Exact quoted hit (explicit intent — no gate needed).
        if quoted_message_id:
            row = self.store.get_by_message_id(str(quoted_message_id))
            if row is not None and row.get("target_open_id") == open_id:
                if row["status"] == _reg.STATUS_EXPIRED:
                    _metrics.incr(_metrics.MATCH_EXPIRED_EXIT)
                    return MatchDecision(
                        EXPIRED_EXIT, registry_id=row["registry_id"], row=row,
                        scene=row["scene"],
                        reply_text="这张卡已过期，需要重新发起吗？",
                        reason="quoted_expired",
                    )
                if row["status"] in _reg.OPEN_STATUSES:
                    return self._route(row, text, quoted=True)
                # sending/confirmed/committed/failed — quoted but not routable.
                return MatchDecision(PASSTHROUGH, reason=f"quoted_status={row['status']}")
            # Quoted something that is not one of this user's cards → normal msg.
            return MatchDecision(PASSTHROUGH, reason="quoted_unmatched")

        # 2. Unquoted → the user's open candidate set.
        open_rows = self.store.list_open_for_user(open_id, now=now)
        if not open_rows:
            fresh = self.store.get_fresh_sending(open_id, since=(now or _reg._now()) - GRACE_WINDOW_S)
            if fresh is not None:
                _metrics.incr(_metrics.GRACE_RETRY)
                return MatchDecision(RETRY_GRACE, row=fresh, reason="fresh_sending")
            _metrics.incr(_metrics.MATCH_PASSTHROUGH)
            return MatchDecision(PASSTHROUGH, reason="no_open_rows")

        if len(open_rows) == 1:
            return self._gate_single(open_rows[0], text)

        # >1 open rows → disambiguate by scene signal.
        return self._disambiguate(open_rows, text)

    async def match_with_grace(
        self,
        *,
        open_id: str,
        text: str,
        quoted_message_id: Optional[str] = None,
        now_fn: Callable[[], int] = _reg._now,
        sleep_fn: Optional[Callable[[float], Awaitable[None]]] = None,
        grace_sleep_s: float = 0.4,
    ) -> MatchDecision:
        """``match`` plus one short re-read when a fresh ``sending`` row exists —
        closes the "员工秒回早于 message_id 回填" window (design §2.2 P1-2)."""
        decision = self.match(
            open_id=open_id, text=text, quoted_message_id=quoted_message_id, now=now_fn()
        )
        if decision.action != RETRY_GRACE:
            return decision
        if sleep_fn is not None and grace_sleep_s > 0:
            await sleep_fn(grace_sleep_s)
        # Re-read: the send may have flipped the row to pending by now.
        return self.match(
            open_id=open_id, text=text, quoted_message_id=quoted_message_id, now=now_fn()
        )

    # -- internals --------------------------------------------------------

    def _gate_single(self, row: dict[str, Any], text: str) -> MatchDecision:
        scene = self.scene_lookup(row)
        verdict = self.heuristic(text, scene)
        if verdict == "maybe" and self.llm_intent is not None and scene is not None:
            try:
                verdict = "yes" if self.llm_intent(text, scene, row) else "no"
            except Exception:
                logger.debug("[push_card] llm intent gate failed; conservative", exc_info=True)
                verdict = "maybe"
        if verdict == "maybe":
            # ponytail: no LLM verdict — lean route only if the user already
            # engaged (clarifying); a just-delivered pending card does not
            # capture ambiguous chit-chat. Upgrade path: wire llm_intent.
            verdict = "yes" if row["status"] == _reg.STATUS_CLARIFYING else "no"
        if verdict == "yes":
            return self._route(row, text, quoted=False)
        # Escape / off-topic → passthrough. An escape phrase against an open row
        # is the逃生门 firing → track it as the 误命中 proxy.
        if str(text or "").strip().lower() in _ESCAPE_PHRASES:
            _metrics.incr(_metrics.ESCAPE_HATCH)
        else:
            _metrics.incr(_metrics.MATCH_PASSTHROUGH)
        return MatchDecision(PASSTHROUGH, reason="intent_no")

    def _disambiguate(self, open_rows: list[dict[str, Any]], text: str) -> MatchDecision:
        hits = [
            r for r in open_rows
            if _scene_signal_count(text, self.scene_lookup(r)) >= 1
        ]
        if len(hits) == 1:
            return self._route(hits[0], text, quoted=False)
        _metrics.incr(_metrics.MATCH_DISAMBIGUATE)
        return MatchDecision(
            DISAMBIGUATE, reply_text="你有多张待填卡片，请引用具体的卡片回复。",
            reason=f"ambiguous_{len(open_rows)}rows",
        )

    def _route(self, row: dict[str, Any], text: str, *, quoted: bool) -> MatchDecision:
        scene = row["scene"]
        skill = row["skill"]
        scene_def = self.scene_lookup(row)
        mode = scene_def.mode if scene_def is not None else MODE_CARD
        slash_content = f"/{str(skill).lstrip('/')} {text}".rstrip()
        context = {
            "registry_id": row["registry_id"],
            "scene": scene,
            "skill": skill,
            "status": row["status"],
            "business_key": row["business_key"],
            "payload": _loads(row.get("payload_json")),
            "submission": _loads(row.get("submission_json")),
            "quoted": quoted,
        }
        context["mode"] = mode
        # 逃生门 hint on first engagement so the fill skill can surface it.
        if row["status"] == _reg.STATUS_PENDING:
            what = scene_def.name if scene_def is not None else "这张卡"
            context["escape_hint"] = f"如果你不是在填「{what}」，回复『不是』即可正常对话。"
        _metrics.incr(_metrics.MATCH_HIT)
        return MatchDecision(
            ROUTE, registry_id=row["registry_id"], row=row, scene=scene, skill=skill,
            mode=mode, slash_content=slash_content, context=context,
            reason="quoted_hit" if quoted else "intent_yes",
        )


def handle_message_recall(
    store: _reg.PushRegistryStore, *, open_id: str, recalled_message_id: str
) -> Optional[str]:
    """A recall wipes the extracted submission of the user's open row so the
    fill skill re-extracts from the surviving messages — replay, not
    pure-accumulate (design §2.3 P1-4). Returns the reset registry_id, if any.

    ponytail: resets the single open row for the user; multi-open recall
    targeting is a P4 concern (it owns per-message extraction provenance)."""
    open_rows = store.list_open_for_user(str(open_id or ""))
    if len(open_rows) != 1:
        return None
    registry_id = open_rows[0]["registry_id"]
    if store.reset_submission(registry_id):
        _metrics.incr(_metrics.RECALL_RECOMPUTE)
        logger.info(
            "[push_card] recall recompute (mid=%s registry=%s)",
            recalled_message_id, registry_id,
        )
        return registry_id
    return None


def _loads(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return None


# --- module-level singleton ---------------------------------------------

_matcher: Optional[PushCardMatcher] = None


def get_matcher() -> PushCardMatcher:
    global _matcher
    if _matcher is None:
        _matcher = PushCardMatcher(_reg.get_registry_store())
    return _matcher


def override_matcher(matcher: Optional[PushCardMatcher]) -> None:
    global _matcher
    _matcher = matcher


# --- live FeishuAdapter wiring (fail-open, mirrors feishu_reply_quote_api) --

_HOOK_INSTALLED = False
_DISPATCH_FLAG = "_hermes_multitenancy_push_card_matcher_dispatch_patched"
_RECALL_FLAG = "_hermes_multitenancy_push_card_recall_patched"
_REGISTRY_CTX_ATTR = "_mt_push_registry_context"
_NOTICE_ATTR = "_mt_push_card_notice"
#: set on the event once a yolo match has rewritten its text into a /skill slash
#: — guards against a double-inject if both the router and dispatch-patch seams
#: reach the same event. Unlike the two above, a yolo event PASSES THROUGH, so a
#: re-entry must return False (passthrough), not True (short-circuit).
_YOLO_ROUTED_ATTR = "_mt_push_yolo_routed"


def _read(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _event_open_id(event: Any) -> str:
    for attr in ("sender_open_id", "open_id", "user_open_id", "user_id"):
        val = getattr(event, attr, None)
        if val:
            return str(val)
    # The live router event carries the sender under ``event.source`` (an
    # EventSource with .open_id/.user_id), NOT ``event.sender`` — the router's
    # own _resolve_sender_for_routing reads source.open_id. Without this the
    # open_id came back '' on the handle_async path and try_route bailed before
    # matching a perfectly-quoted reply. Check source first, then the legacy
    # sender/sender_id shapes used by the _dispatch_inbound_event path.
    for holder_attr in ("source", "sender", "sender_id"):
        holder = getattr(event, holder_attr, None)
        if holder is not None:
            for id_attr in ("open_id", "user_id"):
                val = getattr(holder, id_attr, None)
                if val:
                    return str(val)
    return ""


def _event_text(event: Any) -> str:
    for attr in ("text", "content", "message_text"):
        val = getattr(event, attr, None)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def install_feishu_push_card_matcher_patch(FeishuAdapter: Any = None) -> None:
    """Idempotently hook inbound dispatch so a matched reply drives its scene's
    fill loop (extract → clarify/confirm card) and a recall event recomputes the
    submission. Fail-open: any error → untouched delivery.

    ``FeishuAdapter`` may be passed explicitly (the live adapter's runtime class,
    supplied by the on-dispatch re-arm in ``push_send_queue`` when the register-
    time install deferred because the class was not yet importable). When omitted
    it is resolved via ``load_feishu_adapter`` (register-time path)."""
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    if FeishuAdapter is None:
        try:
            from .feishu_adapter_compat import load_feishu_adapter, log_feishu_adapter_load_error
            FeishuAdapter = load_feishu_adapter()
        except Exception as exc:  # noqa: BLE001
            try:
                from .feishu_adapter_compat import log_feishu_adapter_load_error
                log_feishu_adapter_load_error(
                    logger, "[push_card] FeishuAdapter not importable yet; matcher patch deferred", exc
                )
            except Exception:
                logger.debug("[push_card] matcher patch deferred", exc_info=True)
            return
    _patch_dispatch(FeishuAdapter)
    _patch_recall(FeishuAdapter)
    _HOOK_INSTALLED = True


# --- fill-loop runtime driver (the match→clarify→form loop, wired) -------

async def _drive_fill_step(adapter: Any, decision: MatchDecision, text: str) -> bool:
    """Run one deterministic fill step for a ROUTE decision and send the card.

    This is the load-bearing wiring the review flagged as missing: the matcher
    used to only rewrite ``event.text`` into a ``/skill`` string and hand it to
    the agent — nothing ran ``merge`` / ``build_confirm_card``, so no clarify /
    confirm card was ever produced (finding end-to-end-loop-unwired). Here the
    reply is merged onto the row's prior submission, the updated submission is
    persisted, and the clarify/confirm card is sent to the user's DM."""
    from . import push_fill_form as _form
    from . import push_send_queue as _sendq

    store = _reg.get_registry_store()
    registry_id = decision.registry_id
    row = store.get(registry_id) if registry_id else None
    if row is None:
        return False
    scene = scene_def_for_row(row)
    if scene is None:
        return False
    prior_values = _loads(row.get("submission_json")) or {}
    nonce = str(row.get("nonce") or "")
    step = _form.build_fill_step(
        scene, prior_values=prior_values, text=text, registry_id=registry_id,
        nonce=nonce, row=row, escape_hint=decision.context.get("escape_hint"),
    )
    status = row["status"]
    # Persist the merged submission and advance pending→clarifying (or stay
    # clarifying). CAS-guarded so a racing send/expire can't be clobbered.
    if status == _reg.STATUS_PENDING:
        moved = store.advance_status(
            registry_id, expect=_reg.STATUS_PENDING, to=_reg.STATUS_CLARIFYING,
            submission=step.values,
        )
    elif status == _reg.STATUS_CLARIFYING:
        moved = store.advance_status(
            registry_id, expect=_reg.STATUS_CLARIFYING, to=_reg.STATUS_CLARIFYING,
            submission=step.values,
        )
    else:
        return False
    if not moved:
        return False
    # Prefer an in-place card.update (DELIVER_UPDATE) so replying twice re-renders
    # the SAME confirm card instead of stacking N cards with stale "确认" buttons
    # (finding confirm-card-no-in-place-update). A failed patch falls back to a new
    # card; either way registry.message_id is CAS-pointed at the card the user is
    # now looking at so a later quoted reply routes to the latest form.
    mid = str(row.get("message_id") or "")
    delivered = False
    if step.delivery == _form.DELIVER_UPDATE and mid:
        try:
            delivered = await _sendq.update_push_card_via_adapter(
                adapter, message_id=mid, card=step.card
            )
        except Exception:
            logger.debug("[push_card] in-place card update failed; sending new", exc_info=True)
            delivered = False
    if not delivered:
        result = await _sendq.send_push_card_via_adapter(
            adapter, open_id=row["target_open_id"], card=step.card
        )
        new_mid = getattr(result, "message_id", None)
        if new_mid and str(new_mid) != mid:
            store.advance_status(
                registry_id, expect=_reg.STATUS_CLARIFYING, to=_reg.STATUS_CLARIFYING,
                message_id=str(new_mid),
            )
    _metrics.incr(_metrics.FILL_RENDERED)
    logger.info("[push_card] fill card rendered (registry=%s, missing=%d, in_place=%s)",
                registry_id, len(step.missing), delivered)
    return True


def _route_yolo_to_skill(event: Any, decision: MatchDecision) -> bool:
    """yolo ROUTE handling: keep the row OPEN and inject the scene's business
    skill slash into the event text so the passed-through message runs the skill
    in the normal agent turn (design: card + yolo双模式).

    Unlike ``_drive_fill_step`` this does NOT send a card, NOT touch a form, and
    NOT write a confirm callback — the skill owns compute/confirm/execute. It
    only (1) advances pending→clarifying (via CAS) so the row stays in
    ``OPEN_STATUSES`` — a multi-turn yolo conversation (rules → 计算 → 「覆盖」→
    execute) keeps matching, and expiry still closes an abandoned row — and
    (2) rewrites ``event.text`` to ``/{skill} <reply>``.

    Returns True when the slash was injected. On a raced/closed row it returns
    False and leaves the event untouched (fail-open passthrough)."""
    slash = decision.slash_content or ""
    if not slash:
        return False
    store = _reg.get_registry_store()
    registry_id = decision.registry_id
    row = store.get(registry_id) if registry_id else None
    if row is None:
        return False
    status = row["status"]
    if status == _reg.STATUS_PENDING:
        # CAS pending→clarifying; a racing send/expire that lost the row means we
        # must not inject (the card is no longer open).
        if not store.advance_status(
            registry_id, expect=_reg.STATUS_PENDING, to=_reg.STATUS_CLARIFYING
        ):
            return False
    elif status != _reg.STATUS_CLARIFYING:
        return False  # confirmed/committed/expired/failed — not routable
    setattr(event, "text", slash)
    setattr(event, _YOLO_ROUTED_ATTR, decision.context)
    _metrics.incr(_metrics.YOLO_ROUTED)
    logger.info("[push_card] yolo routed reply into skill (registry=%s skill=%s)",
                registry_id, decision.skill)
    return True


async def _send_notice(adapter: Any, open_id: str, text: str) -> bool:
    """Deliver an EXPIRED_EXIT / DISAMBIGUATE canned notice straight through the
    adapter and short-circuit the original dispatch — the text used to be only
    ``setattr`` on the event with no reader, so the user never saw it (finding
    expired-disambiguate-notice-undelivered)."""
    from . import push_send_queue as _sendq

    card = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": text}]}}
    await _sendq.send_push_card_via_adapter(adapter, open_id=open_id, card=card)
    _metrics.incr(_metrics.NOTICE_SENT)
    return True


async def try_route_push_card_reply(adapter: Any, event: Any) -> bool:
    """Run the inbound matcher against ``event`` and, on a routing decision, drive
    the fill loop (or deliver the canned expired/disambiguate notice) and return
    ``True`` so the caller SHORT-CIRCUITS — the reply is CONSUMED by the push-card
    loop and must not also become a free-form agent turn (design §2.3). Returns
    ``False`` for any passthrough (no open card, off-topic, escape phrase, error):
    the message continues to the normal agent, untouched.

    Two callers share this body:
      * the ``_dispatch_inbound_event`` patch below — covers non-router adapters.
      * the multitenancy router's ``handle_async`` — the LIVE path. The router
        owns inbound via ``pre_gateway_dispatch`` → ``handle_async`` and NEVER
        invokes ``_dispatch_inbound_event``, so without this entry a quoted reply
        to a pending card fell straight through to the streaming agent.
    Idempotent per event (``_REGISTRY_CTX_ATTR``/``_NOTICE_ATTR`` guard) so a
    message that somehow reaches both paths can't drive the loop or send twice.
    Fail-open — any error → ``False`` (untouched delivery)."""
    if adapter is None:
        return False
    if getattr(event, _YOLO_ROUTED_ATTR, None) is not None:
        return False  # already yolo-routed (text rewritten) — passthrough, don't re-inject
    if (getattr(event, _REGISTRY_CTX_ATTR, None) is not None
            or getattr(event, _NOTICE_ATTR, None) is not None):
        return True  # already routed by the sibling path — short-circuit, don't re-drive
    try:
        from . import push_send_queue as _sendq
        _sendq.note_live_adapter(adapter)  # capture adapter for proactive sends
        open_id = _event_open_id(event)
        # The router event's source usually carries a user_id-namespace id, but
        # push cards are keyed by open_id (target_open_id, e.g. ``ou_...``). Resolve
        # to the canonical open_id via the routing table so the matcher's
        # ``target_open_id == open_id`` guard sees the same namespace — otherwise a
        # correctly-quoted reply is rejected as a namespace mismatch.
        if open_id and not str(open_id).startswith("ou_"):
            try:
                from .routing import RoutingTable
                _ctx = RoutingTable().resolve_context(open_id, alt_id=open_id)
                if _ctx is not None and getattr(_ctx, "open_id", None):
                    open_id = str(_ctx.open_id)
            except Exception:
                logger.debug("[push_card] open_id resolve failed", exc_info=True)
        text = _event_text(event)
        quoted = str(getattr(event, "reply_to_message_id", "") or "")
        if not (open_id and text):
            return False
        import asyncio
        decision = await get_matcher().match_with_grace(
            open_id=open_id, text=text, quoted_message_id=quoted,
            sleep_fn=asyncio.sleep,
        )
        if decision.action == ROUTE:
            if decision.mode == MODE_YOLO:
                # yolo放权: do NOT drive the framework form. Inject the scene's
                # business-skill slash into the event text and PASS THROUGH so
                # handle_async runs it as a normal agent turn — the skill
                # self-drives compute → 请确认 → execute/写库. Returning False is
                # deliberate: the message is NOT consumed here, it flows on with
                # rewritten text. The row stays open (expiry still applies) so a
                # multi-turn yolo conversation keeps routing each reply in.
                _route_yolo_to_skill(event, decision)
                return False
            # Load-bearing path (card mode): run the fill loop and send the
            # clarify/confirm card. The reply is CONSUMED by the form loop, so
            # short-circuit.
            if await _drive_fill_step(adapter, decision, text):
                setattr(event, _REGISTRY_CTX_ATTR, decision.context)
                return True
        elif decision.action in (EXPIRED_EXIT, DISAMBIGUATE) and decision.reply_text:
            # Deliver the已过期/请引用 notice and short-circuit — the text must
            # never fall through to the agent unhandled.
            if await _send_notice(adapter, open_id, decision.reply_text):
                setattr(event, _NOTICE_ATTR, decision.reply_text)
                return True
    except Exception:
        logger.debug("[push_card] inbound matcher failed; passthrough", exc_info=True)
    return False


def _patch_dispatch(FeishuAdapter: Any) -> None:
    original = getattr(FeishuAdapter, "_dispatch_inbound_event", None)
    if original is None or getattr(original, _DISPATCH_FLAG, False):
        return

    @functools.wraps(original)
    async def wrapped(self: Any, event: Any, *args: Any, **kwargs: Any) -> Any:
        if await try_route_push_card_reply(self, event):
            return None
        return await original(self, event, *args, **kwargs)

    setattr(wrapped, _DISPATCH_FLAG, True)
    FeishuAdapter._dispatch_inbound_event = wrapped
    logger.info("[push_card] installed inbound matcher on FeishuAdapter")


def _patch_recall(FeishuAdapter: Any) -> None:
    """Hook ``_on_message_recalled`` so a recalled reply recomputes the user's
    open row's submission (replay, not pure-accumulate — design §2.3 P1-4). The
    core handler is a log-only stub; without this patch the recall event has no
    effect at all (finding recall-event-not-wired)."""
    original = getattr(FeishuAdapter, "_on_message_recalled", None)
    if original is None or getattr(original, _RECALL_FLAG, False):
        return

    @functools.wraps(original)
    def wrapped(self: Any, data: Any) -> Any:
        try:
            event = _read(data, "event")
            operator = _read(event, "operator_id") or _read(event, "operator")
            open_id = str(_read(operator, "open_id") or "")
            message_id = str(_read(event, "message_id") or "")
            if open_id and message_id:
                handle_message_recall(
                    _reg.get_registry_store(), open_id=open_id, recalled_message_id=message_id
                )
        except Exception:
            logger.debug("[push_card] recall hook failed; delegating", exc_info=True)
        return original(self, data)

    setattr(wrapped, _RECALL_FLAG, True)
    FeishuAdapter._on_message_recalled = wrapped
    logger.info("[push_card] installed message-recall hook on FeishuAdapter")
