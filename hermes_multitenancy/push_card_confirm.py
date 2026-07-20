"""Confirm callback + idempotent backend write (SPEC P5, design §2.5).

The 5th ``card.action.trigger`` patch on ``FeishuAdapter`` (after clarify /
auth_hub / group_valve / credential_hub). Like its siblings it is a chained
monkeypatch: a callback whose ``value.hermes_action`` is not ``push_confirm`` is
delegated **unchanged** to the original — the new patch must never吞 the other
four's events (SPEC regression guard).

The write path is the only place the backend is ever touched, and it stands on
three defences so kep holds **exactly one** record per confirmed card (design
§2.5 P0-2, the上线硬门):

1. **Atomic CAS claim** — ``clarifying → confirmed`` gated on the row's nonce;
   a lost CAS means someone already claimed it → no second write.
2. **write_idempotency_key = registry_id** — passed to the writer, which is
   contractually required to dedupe on it; this is what makes a double-tap /
   replay / crash-retry collapse to one record even inside the CAS window.
3. **nonce** — cleared on terminal ``committed`` so a replayed callback carrying
   the old nonce lands on the ``committed`` guard and no-ops.
   ponytail: the nonce is NOT cleared at the CAS (it is kept so a
   credential-expiry / write-failure retry can re-drive the same button); a
   pre-commit replay with the correct nonce therefore only re-issues an
   *idempotent* write — never a second record. Rotate the nonce per attempt only
   if strict single-use核销 before commit is ever required.

Credential expiry is fail-loud: the writer self-decodes the kep JWT ``exp`` and
reports ``credential_expired`` (``kep-auth status`` is a known false-positive);
the card then guides re-auth and the row stays writable for a retry — kep gets
**zero** writes (design §2.5 凭证分支, script step 6).
"""
from __future__ import annotations

import functools
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import push_card_metrics as _metrics
from . import push_fill_form as _form
from . import push_registry as _reg
from .push_scenes import SceneDefinition, get_scene

logger = logging.getLogger(__name__)

_HOOK_INSTALLED = False
_CARD_ACTION_FLAG = "_hermes_multitenancy_push_confirm_card_action_patched"


# --- writer contract -----------------------------------------------------

@dataclass
class WriteResult:
    ok: bool
    backend_id: Optional[str] = None
    #: True when the write could not proceed because the kep credential is
    #: expired — the confirm handler turns this into the re-auth card, NOT a
    #: generic failure (design §2.5 凭证分支).
    credential_expired: bool = False
    error: Optional[str] = None


class ClaimWriter:
    """Backend writer protocol. ``write`` MUST be idempotent on
    ``write_idempotency_key`` (design §2.5 P0-2 contract to the kep side)."""

    def write(
        self,
        *,
        scene: SceneDefinition,
        values: dict[str, Any],
        registry_id: str,
        write_idempotency_key: str,
        profile_name: str,
    ) -> WriteResult:  # pragma: no cover - interface
        raise NotImplementedError


def build_kep_cli_args(
    scene: SceneDefinition,
    values: dict[str, Any],
    *,
    registry_id: str,
    profile_name: str,
    reason_with_marker: str,
    env_name: str = "pre",
) -> list[str]:
    """Construct the kep-cli argv as a **list** — never a shell string.

    Every value is a separate argv element, so a reason like ``"; rm -rf /"``
    is an inert positional argument, not a shell token (larkcli injection
    precedent, design §2.5 P1-5). The real kep-pre writer will hand this list to
    ``credential_hub._run`` (array exec, no ``shell=True``)."""
    args = [
        "kep-cli", "--profile", profile_name, "--env", env_name,
        "claim", "create",
        "--idempotency-key", str(registry_id),
    ]
    for f in scene.fields:
        if f.key not in values:
            continue
        val = reason_with_marker if f.key == "reason" else values[f.key]
        args += [f"--{f.key}", str(val)]
    return args


class MockKepPreClaimWriter(ClaimWriter):
    """In-memory stand-in for the real kep-pre writer (endpoint待sunke指定).

    Same interface + same idempotency contract, so the real writer drops in
    without touching the confirm handler. Records are keyed by
    ``write_idempotency_key`` so a repeat write returns the first record —
    "恰好一条" (design §5.1). Set ``credential_expired`` to simulate script
    step 6 (expired pre token → zero write)."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.credential_expired = False
        self.write_calls = 0

    def write(
        self,
        *,
        scene: SceneDefinition,
        values: dict[str, Any],
        registry_id: str,
        write_idempotency_key: str,
        profile_name: str,
    ) -> WriteResult:
        self.write_calls += 1
        if self.credential_expired:
            return WriteResult(ok=False, credential_expired=True, error="kep pre token expired")
        marker = scene.deterministic_marker.format(registry_id=registry_id)
        reason = f"{values.get('reason', '')} {marker}".strip()
        # Build the argv the real writer would exec — asserts the array
        # discipline is exercised even though the mock does not shell out.
        _ = build_kep_cli_args(
            scene, values, registry_id=registry_id, profile_name=profile_name,
            reason_with_marker=reason,
        )
        key = str(write_idempotency_key)
        if key in self.records:
            existing = self.records[key]
            return WriteResult(ok=True, backend_id=existing["_backend_id"])
        record = dict(values)
        record["reason"] = reason
        record["_backend_id"] = f"kep_pre_{key}"
        record["_marker"] = marker
        self.records[key] = record
        return WriteResult(ok=True, backend_id=record["_backend_id"])

    def find_by_marker(self, marker: str) -> list[dict[str, Any]]:
        return [r for r in self.records.values() if r.get("_marker") == marker]


# --- writer registry -----------------------------------------------------

_writers: dict[str, ClaimWriter] = {}


def get_writer(name: str) -> Optional[ClaimWriter]:
    writer = _writers.get(name)
    if writer is None and name == "kep-pre-claim-writer":
        # ponytail: default to the mock until sunke gives the real kep pre
        # endpoint (design §6 前置条件). Same interface → hot-swap via
        # override_writer, no handler change.
        writer = MockKepPreClaimWriter()
        _writers[name] = writer
    return writer


def override_writer(name: str, writer: Optional[ClaimWriter]) -> None:
    if writer is None:
        _writers.pop(name, None)
    else:
        _writers[name] = writer


# --- pure confirm core (fully unit-testable) -----------------------------

@dataclass
class ConfirmResult:
    """What the confirm handler decided. ``card`` (when set) replaces the card
    in place via the callback response; ``toast`` shows a transient notice."""

    kind: str  # committed | reauth | failed | reject | noop | not_owner | invalid
    toast: Optional[dict[str, Any]] = None
    card: Optional[dict[str, Any]] = None
    registry_id: Optional[str] = None
    written: bool = False


def _toast(content: str, level: str = "info") -> dict[str, Any]:
    return {"toast": {"type": level, "content": content}}


def handle_confirm(
    *,
    registry_id: str,
    nonce: str,
    operator_open_ids: set[str],
    form_value: Any,
    store: _reg.PushRegistryStore,
    writer_lookup: Callable[[str], Optional[ClaimWriter]] = get_writer,
    scene_lookup: Callable[[str], Optional[SceneDefinition]] = get_scene,
    now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
) -> ConfirmResult:
    """Process one confirm click. Pure: registry reads/writes + an injected
    writer, no Feishu SDK. ``operator_open_ids`` is the SIGNED clicker's id set
    (open_id/union/user resolved to open_ids) — only the target本人 may submit."""
    registry_id = str(registry_id or "").strip()
    nonce = str(nonce or "").strip()
    if not registry_id or not nonce:
        return ConfirmResult("invalid", toast=_toast("提交无效，请重新操作。", "error"))

    row = store.get(registry_id)
    if row is None:
        return ConfirmResult("invalid", toast=_toast("该卡片已失效。", "error"), registry_id=registry_id)

    status = row["status"]
    if status == _reg.STATUS_COMMITTED:
        # Idempotent success — a replay / double-tap after commit.
        return ConfirmResult("noop", toast=_toast("已录入 ✅，请勿重复提交。"), registry_id=registry_id)
    if status == _reg.STATUS_EXPIRED:
        return ConfirmResult("reject", toast=_toast("该卡片已过期，请重新发起。", "error"), registry_id=registry_id)
    if status not in (_reg.STATUS_CLARIFYING, _reg.STATUS_CONFIRMED):
        return ConfirmResult("reject", toast=_toast("该卡片当前不可提交。", "error"), registry_id=registry_id)

    # Owner check — only target_open_id本人 (signed operator, never button payload).
    target = str(row.get("target_open_id") or "")
    if target and target not in operator_open_ids:
        return ConfirmResult("not_owner", toast=_toast("只有卡片的接收人本人可以提交。", "error"),
                             registry_id=registry_id)

    # Nonce — reject a mismatch (replay with a stale/forged nonce). A cleared
    # nonce (already committed path handled above) with status still open means
    # this row was reset; treat as replay.
    row_nonce = row.get("nonce")
    if not row_nonce or row_nonce != nonce:
        return ConfirmResult("reject", toast=_toast("该操作已失效，请在最新的卡片上确认。", "error"),
                             registry_id=registry_id)

    scene = scene_lookup(row["scene"])
    if scene is None:
        return ConfirmResult("invalid", toast=_toast("场景配置缺失。", "error"), registry_id=registry_id)

    # Final payload = the controls the user SAW and submitted (design §2.4 P1-5).
    final = _form.submission_from_form(scene, form_value)
    missing = _form.missing_fields(scene, final)
    if missing:
        labels = "、".join(f.label for f in missing)
        return ConfirmResult("reject", toast=_toast(f"请填写完整：{labels}。", "error"),
                             registry_id=registry_id)
    values = _form.submission_values(scene, final)

    writer = writer_lookup(scene.writer)
    if writer is None:
        return ConfirmResult("failed",
                             card=_result_card(scene, "写入器未就绪，请稍后重试。", ok=False,
                                               retryable=True, registry_id=registry_id,
                                               nonce=nonce, values=values),
                             registry_id=registry_id)

    # 1. Atomic CAS claim (nonce-gated). Kept-nonce so a retry can re-drive.
    if status == _reg.STATUS_CLARIFYING:
        claimed = store.advance_status(
            registry_id, expect=_reg.STATUS_CLARIFYING, to=_reg.STATUS_CONFIRMED,
            expect_nonce=nonce, submission=values,
        )
        if not claimed:
            # Lost the race — re-read to report the real outcome.
            fresh = store.get(registry_id) or row
            if fresh["status"] == _reg.STATUS_COMMITTED:
                return ConfirmResult("noop", toast=_toast("已录入 ✅，请勿重复提交。"), registry_id=registry_id)
            # Someone else advanced it to confirmed — fall through to write
            # (idempotent) so a concurrent double-tap still commits exactly once.

    # 2. Idempotent backend write (write_idempotency_key = registry_id).
    started = now_ms()
    result = writer.write(
        scene=scene, values=values, registry_id=registry_id,
        write_idempotency_key=registry_id, profile_name=str(row.get("profile_name") or ""),
    )

    if result.credential_expired:
        # fail-loud: guide re-auth, no state change, row stays writable → retry.
        return ConfirmResult("reauth",
                             card=_reauth_card(scene, registry_id=registry_id, nonce=nonce, values=values),
                             registry_id=registry_id)
    if not result.ok:
        store.advance_status(
            registry_id, expect=_reg.STATUS_CONFIRMED, to=_reg.STATUS_CONFIRMED,
            last_error=result.error or "write failed",
        )
        return ConfirmResult("failed",
                             card=_result_card(scene, f"录入失败：{result.error or '未知错误'}，请重试。",
                                               ok=False, retryable=True, registry_id=registry_id,
                                               nonce=nonce, values=values),
                             registry_id=registry_id, written=False)

    # 3. Success → commit + clear nonce (single-use核销 on terminal success).
    store.advance_status(
        registry_id, expect=_reg.STATUS_CONFIRMED, to=_reg.STATUS_COMMITTED,
        clear_nonce=True, submission=values,
    )
    _metrics.incr(_metrics.RECONCILE_COMMITTED)
    _metrics.observe_commit_latency_ms(max(0, now_ms() - started))
    return ConfirmResult("committed",
                         card=_result_card(scene, "已录入 ✅", ok=True, retryable=False,
                                           registry_id=registry_id, nonce=nonce, values=values),
                         registry_id=registry_id, written=True)


# --- terminal / retry / reauth cards -------------------------------------

def _result_card(
    scene: SceneDefinition, message: str, *, ok: bool, retryable: bool,
    registry_id: str, nonce: str, values: dict[str, Any],
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"**{scene.name}**"},
        {"tag": "markdown", "content": _summary_md(scene, values)},
        {"tag": "markdown", "content": ("✅ " if ok else "⚠️ ") + message},
    ]
    if retryable:
        # A retry button carries the same registry_id + nonce (the row is still
        # writable); pressing it re-drives the idempotent write.
        elements.append({"tag": "button", "name": "push_confirm_retry",
                         "text": {"tag": "plain_text", "content": "重试"}, "type": "primary",
                         "value": {"hermes_action": "push_confirm", "registry_id": registry_id,
                                   "nonce": nonce, "operation_id": secrets.token_hex(6)}})
    return {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": scene.name},
                   "template": "green" if ok else "red"},
        "body": {"elements": elements},
    }


def _reauth_card(
    scene: SceneDefinition, *, registry_id: str, nonce: str, values: dict[str, Any],
) -> dict[str, Any]:
    """凭证已过期 card — guides re-auth via the credential hub, keeps a retry
    button so录入 resumes after the user re-authenticates (design §2.5 凭证分支)."""
    return {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": scene.name}, "template": "orange"},
        "body": {"elements": [
            {"tag": "markdown", "content": _summary_md(scene, values)},
            {"tag": "markdown", "content": "⚠️ 凭证已过期，暂时无法录入。请先私聊我发送 `/auth` 完成认证，再点「重试」。"},
            {"tag": "button", "name": "push_confirm_reauth",
             "text": {"tag": "plain_text", "content": "去认证"}, "type": "primary",
             # reuse the existing cred_auth callback (feishu_auth_hub_actions).
             "value": {"hermes_action": "cred_auth", "cred": "kep-cli-pre"}},
            {"tag": "button", "name": "push_confirm_retry",
             "text": {"tag": "plain_text", "content": "重试"},
             "value": {"hermes_action": "push_confirm", "registry_id": registry_id,
                       "nonce": nonce, "operation_id": secrets.token_hex(6)}},
        ]},
    }


def _summary_md(scene: SceneDefinition, values: dict[str, Any]) -> str:
    lines = []
    for f in scene.fields:
        if f.key in values:
            lines.append(f"- {f.label}：{values[f.key]}")
    return "\n".join(lines) if lines else "（无内容）"


# --- live FeishuAdapter patch (5th card.action.trigger, undefined放行) ----

def install_feishu_push_card_confirm_patch() -> None:
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    try:
        from .feishu_adapter_compat import load_feishu_adapter, log_feishu_adapter_load_error
        FeishuAdapter = load_feishu_adapter()
    except Exception as exc:  # noqa: BLE001
        try:
            from .feishu_adapter_compat import log_feishu_adapter_load_error
            log_feishu_adapter_load_error(
                logger, "[push_card] FeishuAdapter not importable yet; confirm patch deferred", exc
            )
        except Exception:
            logger.debug("[push_card] confirm patch deferred", exc_info=True)
        return
    _HOOK_INSTALLED = _patch_card_action(FeishuAdapter)


def _patch_card_action(FeishuAdapter: Any) -> bool:
    original = getattr(FeishuAdapter, "_on_card_action_trigger", None)
    if original is None or getattr(original, _CARD_ACTION_FLAG, False):
        return bool(original is not None)

    @functools.wraps(original)
    def wrapped(self: Any, data: Any) -> Any:
        try:
            event = _read(data, "event")
            action = _read(event, "action")
            value = _read_action_value(_read(action, "value"))
            # undefined放行: not ours → delegate unchanged (never吞 the other 4).
            if not isinstance(value, dict) or value.get("hermes_action") != "push_confirm":
                return original(self, data)
            return _dispatch_confirm(self, event, action, value)
        except Exception:
            logger.debug("[push_card] confirm card action failed; delegating to original", exc_info=True)
            return original(self, data)

    setattr(wrapped, _CARD_ACTION_FLAG, True)
    FeishuAdapter._on_card_action_trigger = wrapped
    logger.info("[push_card] installed push-confirm card-action hook on %s.FeishuAdapter",
                FeishuAdapter.__module__)
    return True


def _dispatch_confirm(adapter: Any, event: Any, action: Any, value: dict[str, Any]) -> Any:
    registry_id = str(value.get("registry_id") or "")
    nonce = str(value.get("nonce") or "")
    form_value = _read(action, "form_value")
    operator_ids = _resolve_operator_open_ids(event)
    result = handle_confirm(
        registry_id=registry_id, nonce=nonce, operator_open_ids=operator_ids,
        form_value=form_value, store=_reg.get_registry_store(),
    )
    if result.card is not None:
        return _card_response(result.card)
    return _toast_response(result.toast or _toast("已处理。"))


def _resolve_operator_open_ids(event: Any) -> set[str]:
    """The SIGNED operator's open_id set (design §2.5: only target本人). Resolves
    a Schema-2 callback that carries only union_id/user_id to its open_id via the
    routing table (mirrors feishu_auth_hub_actions._resolve_operator_profile)."""
    operator = _read(event, "operator") or _read(event, "operator_id")

    def pick(*names: str) -> str:
        for n in names:
            v = _read(operator, n)
            if v:
                return str(v).strip()
        return ""

    open_id = pick("open_id", "operator_open_id")
    union_id = pick("union_id", "operator_union_id")
    user_id = pick("user_id", "operator_user_id")
    ids: set[str] = set()
    if open_id:
        ids.add(open_id)
    try:
        from .router import _get_routing_table
        table = _get_routing_table()
        if table is not None:
            row = None
            if open_id:
                row = table.lookup_by_open_id(open_id)
            if row is None and union_id:
                row = table.lookup_by_union_id(union_id)
            if row is None and user_id and hasattr(table, "lookup_by_user_id"):
                row = table.lookup_by_user_id(user_id)
            resolved = str(getattr(row, "open_id", "") or "").strip() if row is not None else ""
            if resolved:
                ids.add(resolved)
    except Exception:
        logger.debug("[push_card] operator routing resolve failed", exc_info=True)
    return ids


# --- SDK field readers / response builders (mirror the sibling patches) ---

def _read(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _read_action_value(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        import json
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return None


def _toast_response(toast: dict[str, Any]) -> Any:
    payload = toast.get("toast", toast)
    try:
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )
    except Exception:
        return {"kind": "toast", "toast": payload}
    response = P2CardActionTriggerResponse()
    response.toast = payload
    return response


def _card_response(card: dict[str, Any]) -> Any:
    try:
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            CallBackCard,
            P2CardActionTriggerResponse,
        )
    except Exception:
        return {"kind": "card", "card": {"type": "raw", "data": card}}
    response = P2CardActionTriggerResponse()
    callback_card = CallBackCard()
    callback_card.type = "raw"
    callback_card.data = card
    response.card = callback_card
    return response
