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
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import push_card_metrics as _metrics
from . import push_fill_form as _form
from . import push_registry as _reg
from . import push_scenes as _scenes
from .push_scenes import CallbackConfig, SceneDefinition, SubmitBehaviors, scene_def_for_row

#: Shown when a click lands on a card whose registry row is gone / already
#: committed / expired (an old card), distinct from a genuinely malformed submit.
_STALE_CARD_MSG = "该卡片已失效或已录入，请使用最新的卡片。"

logger = logging.getLogger(__name__)

_HOOK_INSTALLED = False
_CARD_ACTION_FLAG = "_hermes_multitenancy_push_confirm_card_action_patched"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow HTTP redirects. An allow-listed callback host that answers
    302 → ``http://169.254.169.254/`` (cloud metadata) or any internal address is
    an SSRF escape; returning None aborts the redirect so the 3xx surfaces as a
    clean HTTPError instead of being silently followed (design §2.5 SSRF guard)."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


#: Opener carrying the no-redirect handler so EVERY confirm-write POST is
#: redirect-proof. Module-level so a test can monkeypatch ``_urlopen`` (the seam
#: the fake ``urlopen`` swaps in) while production stays redirect-blocked.
_http_opener = urllib.request.build_opener(_NoRedirectHandler)


def _urlopen(req: Any, *, timeout: float) -> Any:
    return _http_opener.open(req, timeout=timeout)


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


class HttpKepPreClaimWriter(ClaimWriter):
    """HTTP writer → the resolved 回调地址 (kep-pre / fake-kep / any 落库 backend).

    The endpoint (url + optional auth + timeout) is resolved per push at confirm
    time — push override > scene default > env dev-fallback (see
    ``resolve_callback`` / ``_bind_endpoint``). A shell instance (empty ``url``)
    returned by ``get_writer`` is bound to the resolved ``CallbackConfig`` before
    any write. Same idempotency contract as the mock: the backend dedupes on
    ``write_idempotency_key`` (a ``deduped: true`` response is still a success).
    Any non-2xx / timeout / connection failure is a clean ``WriteResult(ok=False)``
    that routes to the existing failed-retry card path (design §2.5); it never
    raises out of ``write``. A 401/403 maps to ``credential_expired`` → the re-auth
    card. Redirects are NOT followed (``_NoRedirectHandler``) so an allow-listed
    host that 302s to an internal address cannot be used for SSRF.

    Auth is never a plaintext secret: ``auth_header`` names the header and
    ``auth_token_env`` names the env var whose value is sent — read here, never
    logged.

    ponytail: stdlib ``urllib.request`` (POST, JSON body, array-nothing — the
    payload is JSON, never a shell string), matching the repo's sync-HTTP idiom
    (token_usage_uploader). aiohttp is a dep but async-only; the confirm write
    path is synchronous, so spinning an event loop here would be the wrong tool.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 10.0,
        auth_header: Optional[str] = None,
        auth_token_env: Optional[str] = None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.auth_header = auth_header
        self.auth_token_env = auth_token_env

    def write(
        self,
        *,
        scene: SceneDefinition,
        values: dict[str, Any],
        registry_id: str,
        write_idempotency_key: str,
        profile_name: str,
    ) -> WriteResult:
        marker = scene.deterministic_marker.format(registry_id=registry_id)
        reason = f"{values.get('reason', '')} {marker}".strip()
        body = dict(values)
        body["reason"] = reason
        body["write_idempotency_key"] = str(write_idempotency_key)
        body["registry_id"] = str(registry_id)
        body["profile_name"] = str(profile_name)
        body["scene"] = scene.scene
        payload = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.auth_header and self.auth_token_env:
            token = os.environ.get(self.auth_token_env, "").strip()
            if token:
                headers[self.auth_header] = token
        req = urllib.request.Request(
            self.url, data=payload, headers=headers, method="POST",
        )
        try:
            with _urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:  # non-2xx (a blocked 3xx redirect included)
            if exc.code in (401, 403):
                # A dead kep credential must route to the RE-AUTH card (design §2.5
                # 凭证分支), not a generic failure — otherwise the employee is stuck
                # retrying a dead token with no /auth guidance.
                return WriteResult(
                    ok=False, credential_expired=True,
                    error=f"kep-pre credential expired (HTTP {exc.code})",
                )
            return WriteResult(ok=False, error=f"kep-pre HTTP {exc.code}: {exc.reason}")
        except (urllib.error.URLError, OSError) as exc:  # timeout / conn refused / DNS
            return WriteResult(ok=False, error=f"kep-pre unreachable: {type(exc).__name__}: {exc}")
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return WriteResult(ok=False, error=f"kep-pre bad response: {raw[:200]!r}")
        # 2xx + parseable → ok (deduped: true is still a successful idempotent write).
        rec = data.get("record") if isinstance(data.get("record"), dict) else {}
        backend_id = str(
            rec.get("_backend_id") or rec.get("backend_id") or rec.get("seq")
            or data.get("backend_id") or write_idempotency_key
        )
        return WriteResult(ok=True, backend_id=backend_id)


# --- writer registry -----------------------------------------------------

# Explicit overrides only (override_writer / a test's injected lookup). Absent an
# override, the kep-pre writer is an endpoint-LESS HTTP shell — the real 回调地址
# is resolved per push (push override > scene default > env) and bound in
# _bind_endpoint just before the write. Keeping the endpoint OUT of get_writer is
# what lets a per-push callback win over env (design §config priority).
_writers: dict[str, ClaimWriter] = {}


def get_writer(name: str) -> Optional[ClaimWriter]:
    override = _writers.get(name)
    if override is not None:
        return override
    if name == "kep-pre-claim-writer":
        # ponytail: an endpoint-less shell; _bind_endpoint fills in the resolved
        # CallbackConfig (or fail-louds "未配置回调地址") before it is ever used.
        return HttpKepPreClaimWriter("")
    return None


def override_writer(name: str, writer: Optional[ClaimWriter]) -> None:
    if writer is None:
        _writers.pop(name, None)
    else:
        _writers[name] = writer


# --- per-push endpoint / behavior resolution -----------------------------

def resolve_callback(
    scene: Optional[SceneDefinition], row: Optional[dict[str, Any]]
) -> Optional[CallbackConfig]:
    """The 回调地址 (落库 endpoint) for this confirm, by priority (design §config):

    1. per-push override — ``registry.callback_json`` (set by notify-card),
    2. scene default — ``scene.callback`` (registered with the scene),
    3. dev fallback — env ``HERMES_PUSH_CARD_WRITER_URL`` (kept only as a dev
       shortcut).

    ``None`` when nothing is configured → the caller fail-louds改卡"未配置回调地址"."""
    pushed = _scenes.callback_from_json(row.get("callback_json") if row else None)
    if pushed is not None:
        return pushed
    if scene is not None and scene.callback is not None:
        return scene.callback
    url = os.environ.get("HERMES_PUSH_CARD_WRITER_URL", "").strip()
    if url:
        return CallbackConfig(url=url)
    return None


def resolve_behaviors(
    scene: Optional[SceneDefinition], row: Optional[dict[str, Any]]
) -> SubmitBehaviors:
    """Submit behavior for this confirm: per-push override > scene default >
    framework default (design §config). Never env-driven."""
    pushed = _scenes.behaviors_from_json(row.get("behaviors_json") if row else None)
    if pushed is not None:
        return pushed
    if scene is not None:
        return scene.behaviors
    return SubmitBehaviors()


def _bind_endpoint(
    writer: Optional[ClaimWriter],
    scene: Optional[SceneDefinition],
    row: Optional[dict[str, Any]],
) -> tuple[Optional[ClaimWriter], Optional[str]]:
    """Bind the resolved 回调地址 to the real HTTP writer. A non-HTTP writer (mock
    / injected test writer) or an HTTP writer already carrying a url (explicitly
    constructed) is used as-is. Returns ``(writer, error)``; ``error`` non-None
    means no endpoint was configured → fail-loud."""
    if not isinstance(writer, HttpKepPreClaimWriter) or writer.url:
        return writer, None
    cb = resolve_callback(scene, row)
    if cb is None:
        return None, "未配置回调地址，请为该场景配置落库接口后重试。"
    return HttpKepPreClaimWriter(
        cb.url, timeout=float(cb.timeout_s),
        auth_header=cb.auth_header, auth_token_env=cb.auth_token_env,
    ), None


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


def _stored_submission_values(row: dict[str, Any]) -> dict[str, Any]:
    """The ``{key: value}`` submission persisted at the clarifying→confirmed CAS.
    A retry button is a plain button (no form), so the write path recovers the
    already-confirmed values from here rather than from an empty form_value
    (finding retry-card-has-no-form)."""
    raw = row.get("submission_json")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return dict(data) if isinstance(data, dict) else {}


def handle_confirm(
    *,
    registry_id: str,
    nonce: str,
    operator_open_ids: set[str],
    form_value: Any,
    store: _reg.PushRegistryStore,
    writer_lookup: Callable[[str], Optional[ClaimWriter]] = get_writer,
    scene_lookup: Callable[[Optional[dict[str, Any]]], Optional[SceneDefinition]] = scene_def_for_row,
    now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
) -> ConfirmResult:
    """Process one confirm click. Pure: registry reads/writes + an injected
    writer, no Feishu SDK. ``operator_open_ids`` is the SIGNED clicker's id set
    (open_id/union/user resolved to open_ids) — only the target本人 may submit."""
    registry_id = str(registry_id or "").strip()
    nonce = str(nonce or "").strip()
    if not registry_id or not nonce:
        # No routing survived recovery → an old card whose row is gone, not a
        # live param error (finding stale-card-generic-toast).
        return ConfirmResult("invalid", toast=_toast(_STALE_CARD_MSG, "error"))

    row = store.get(registry_id)
    if row is None:
        return ConfirmResult("invalid", toast=_toast(_STALE_CARD_MSG, "error"), registry_id=registry_id)

    scene = scene_lookup(row)
    behaviors = resolve_behaviors(scene, row)
    status = row["status"]

    if status == _reg.STATUS_COMMITTED and behaviors.submit_once:
        # Terminal (submit_once): a replay / double-tap after commit no-ops.
        return ConfirmResult("noop", toast=_toast("已录入 ✅，请勿重复提交。"), registry_id=registry_id)
    if status == _reg.STATUS_EXPIRED:
        return ConfirmResult("reject", toast=_toast("该卡片已过期，请重新发起。", "error"), registry_id=registry_id)
    if status not in (_reg.STATUS_CLARIFYING, _reg.STATUS_CONFIRMED, _reg.STATUS_COMMITTED):
        return ConfirmResult("reject", toast=_toast("该卡片当前不可提交。", "error"), registry_id=registry_id)
    # A COMMITTED row only reaches here when submit_once=False → 改单 is allowed.

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

    if scene is None:
        return ConfirmResult("invalid", toast=_toast("场景配置缺失。", "error"), registry_id=registry_id)

    # Final payload = the controls the user SAW and submitted (design §2.4 P1-5).
    final = _form.submission_from_form(scene, form_value)
    form_values = _form.submission_values(scene, final)
    values = form_values
    # Retry / reauth cards are PLAIN buttons (not inside a form), so a real retry
    # click carries no form_value → `values` is empty. Recover the payload
    # persisted at the clarifying→confirmed CAS so the credential-expiry /
    # write-failure retry (or a submit_once=False 改单 re-click) can actually
    # commit (finding retry-card-has-no-form; design §2.5 凭证分支 retry). Only when
    # already confirmed/committed — a clarifying row with an empty form is a
    # genuine "请填写完整".
    if not values and status in (_reg.STATUS_CONFIRMED, _reg.STATUS_COMMITTED):
        values = _stored_submission_values(row)

    # allow_resubmit_before_commit: a NEW form editing a still-pre-commit
    # (confirmed) row is refused when the scene/push locks edits after confirm.
    if (status == _reg.STATUS_CONFIRMED and form_values
            and not behaviors.allow_resubmit_before_commit):
        return ConfirmResult("reject", toast=_toast("提交已锁定，请勿在确认后修改。", "error"),
                             registry_id=registry_id)

    missing = [f for f in scene.fields
               if f.required and not str(values.get(f.key, "") or "").strip()]
    if missing:
        labels = "、".join(f.label for f in missing)
        return ConfirmResult("reject", toast=_toast(f"请填写完整：{labels}。", "error"),
                             registry_id=registry_id)

    # Resolve the writer and bind its 回调地址 (push override > scene > env). A
    # non-configured endpoint fail-louds改卡 (never a silent no-write).
    writer, cb_error = _bind_endpoint(writer_lookup(scene.writer), scene, row)
    if cb_error is not None:
        return ConfirmResult("failed",
                             card=_result_card(scene, cb_error, ok=False, retryable=False,
                                               registry_id=registry_id, nonce=nonce, values=values),
                             registry_id=registry_id)
    if writer is None:
        return ConfirmResult("failed",
                             card=_result_card(scene, "写入器未就绪，请稍后重试。", ok=False,
                                               retryable=True, registry_id=registry_id,
                                               nonce=nonce, values=values),
                             registry_id=registry_id)

    # submit_once=False 改单: the row is already committed — re-drive the
    # idempotent write (capped by max_submits) and stay committed.
    if status == _reg.STATUS_COMMITTED:
        return _resubmit_committed(scene, row, behaviors, values, writer, store,
                                   registry_id=registry_id, nonce=nonce, now_ms=now_ms)

    # 1. Atomic CAS claim (nonce-gated). Kept-nonce so a retry can re-drive.
    if status == _reg.STATUS_CLARIFYING:
        claimed = store.advance_status(
            registry_id, expect=_reg.STATUS_CLARIFYING, to=_reg.STATUS_CONFIRMED,
            expect_nonce=nonce, submission=values,
        )
        if not claimed:
            # Lost the CAS → a concurrent clicker already claimed this row. The
            # loser must NEVER call the writer (finding
            # lost-cas-falls-through-to-write — a double-click otherwise double-
            # writes). Re-read row state to decide what card to show:
            fresh = store.get(registry_id) or row
            if fresh["status"] == _reg.STATUS_COMMITTED:
                return ConfirmResult("noop", toast=_toast("已录入 ✅，请勿重复提交。"), registry_id=registry_id)
            if fresh["status"] == _reg.STATUS_CONFIRMED:
                # The winner owns the (idempotent) write. Offer a retry card so a
                # failed winner-write can still be re-driven — but do NOT write
                # here.
                return ConfirmResult(
                    "retry",
                    card=_result_card(scene, "正在处理你的提交，如未完成可点「重试」。", ok=False,
                                      retryable=True, registry_id=registry_id, nonce=nonce,
                                      values=_stored_submission_values(fresh) or values),
                    registry_id=registry_id, written=False,
                )
            return ConfirmResult("reject", toast=_toast("该操作已失效，请在最新的卡片上确认。", "error"),
                                 registry_id=registry_id)

    # 2. Idempotent backend write (write_idempotency_key = registry_id). A writer
    #    exception is caught and routed to the SAME failed-retry card as a clean
    #    result.ok=False — never swallowed leaving the row stuck confirmed with no
    #    visible failure (finding writer-exception-leaves-confirmed-silent).
    started = now_ms()
    try:
        result = writer.write(
            scene=scene, values=values, registry_id=registry_id,
            write_idempotency_key=registry_id, profile_name=str(row.get("profile_name") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[push_card] writer raised (registry=%s): %s", registry_id, exc, exc_info=True)
        result = WriteResult(ok=False, error=f"{type(exc).__name__}: {exc}")

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

    # 3. Success → commit + clear nonce (single-use核销 on terminal success). The
    #    commit CAS result is authoritative: if it fails, another actor
    #    (reconcile / concurrent) already moved the row, so never report a false
    #    green "written" card (finding commit-cas-result-ignored).
    # clear_nonce only when submit_once (terminal card, no re-click); when 改单
    # is allowed the nonce is kept so the 重新提交 button can re-drive the write.
    committed = store.advance_status(
        registry_id, expect=_reg.STATUS_CONFIRMED, to=_reg.STATUS_COMMITTED,
        clear_nonce=behaviors.submit_once, submission=values, bump_write_attempts=True,
    )
    if not committed:
        fresh = store.get(registry_id) or row
        if fresh["status"] == _reg.STATUS_COMMITTED:
            # Already committed by reconcile / a concurrent winner — backend holds
            # exactly one record; report success but do not claim WE wrote it.
            return ConfirmResult("noop", toast=_toast("已录入 ✅，请勿重复提交。"),
                                 registry_id=registry_id, written=False)
        # Not committed and not ours to finalize → leave it for the reconcile
        # worker and offer a retry; never a false green.
        return ConfirmResult(
            "retry",
            card=_result_card(scene, "录入结果确认中，如未完成可点「重试」。", ok=False,
                              retryable=True, registry_id=registry_id, nonce=nonce, values=values),
            registry_id=registry_id, written=False,
        )
    _metrics.incr(_metrics.RECONCILE_COMMITTED)
    _metrics.observe_commit_latency_ms(max(0, now_ms() - started))
    return ConfirmResult("committed",
                         card=_result_card(scene, "已录入 ✅", ok=True, retryable=False,
                                           resubmit=not behaviors.submit_once,
                                           registry_id=registry_id, nonce=nonce, values=values),
                         registry_id=registry_id, written=True)


def _resubmit_committed(
    scene: SceneDefinition,
    row: dict[str, Any],
    behaviors: SubmitBehaviors,
    values: dict[str, Any],
    writer: ClaimWriter,
    store: _reg.PushRegistryStore,
    *,
    registry_id: str,
    nonce: str,
    now_ms: Callable[[], int],
) -> ConfirmResult:
    """submit_once=False 改单: re-run the idempotent write on an already-committed
    row, capped by ``max_submits``. Idempotent on ``registry_id`` → still one
    backend record (a true multi-record amend is out of M1 scope).
    ponytail: rotate write_idempotency_key per amend if multi-record amends land."""
    submits = int(row.get("write_attempts") or 0)
    if behaviors.max_submits is not None and submits >= behaviors.max_submits:
        return ConfirmResult("reject",
                             toast=_toast(f"已达最大提交次数（{behaviors.max_submits}），无法再次修改。", "error"),
                             registry_id=registry_id)
    started = now_ms()
    try:
        result = writer.write(
            scene=scene, values=values, registry_id=registry_id,
            write_idempotency_key=registry_id, profile_name=str(row.get("profile_name") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[push_card] resubmit writer raised (registry=%s): %s", registry_id, exc, exc_info=True)
        result = WriteResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    if result.credential_expired:
        return ConfirmResult("reauth",
                             card=_reauth_card(scene, registry_id=registry_id, nonce=nonce, values=values),
                             registry_id=registry_id)
    if not result.ok:
        return ConfirmResult("failed",
                             card=_result_card(scene, f"更新失败：{result.error or '未知错误'}，请重试。",
                                               ok=False, retryable=True, resubmit=True,
                                               registry_id=registry_id, nonce=nonce, values=values),
                             registry_id=registry_id, written=False)
    store.advance_status(registry_id, expect=_reg.STATUS_COMMITTED, to=_reg.STATUS_COMMITTED,
                         submission=values, bump_write_attempts=True)
    _metrics.observe_commit_latency_ms(max(0, now_ms() - started))
    return ConfirmResult("committed",
                         card=_result_card(scene, "已更新 ✅", ok=True, retryable=False, resubmit=True,
                                           registry_id=registry_id, nonce=nonce, values=values),
                         registry_id=registry_id, written=True)


# --- terminal / retry / reauth cards -------------------------------------

def _result_card(
    scene: SceneDefinition, message: str, *, ok: bool, retryable: bool,
    registry_id: str, nonce: str, values: dict[str, Any], resubmit: bool = False,
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
    elif resubmit:
        # submit_once=False: a committed card keeps a 重新提交 button so the user
        # can 改单 (the nonce was NOT cleared on commit for this scene/push).
        elements.append({"tag": "button", "name": "push_confirm_resubmit",
                         "text": {"tag": "plain_text", "content": "重新提交"},
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

def install_feishu_push_card_confirm_patch(FeishuAdapter: Any = None) -> None:
    """Install the push-confirm card-action patch (5th card.action.trigger hook).

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
            from . import push_send_queue as _sendq
            _sendq.note_live_adapter(self)  # capture adapter for proactive sends
            event = _read(data, "event")
            action = _read(event, "action")
            value = _read_action_value(_read(action, "value"))
            form_value = _read(action, "form_value")
            # undefined放行: not ours → delegate unchanged (never吞 the other 4).
            if not _is_push_confirm_action(value, form_value, action):
                return original(self, data)
            return _dispatch_confirm(
                self, event, action, value if isinstance(value, dict) else {}, form_value
            )
        except Exception:
            logger.debug("[push_card] confirm card action failed; delegating to original", exc_info=True)
            return original(self, data)

    setattr(wrapped, _CARD_ACTION_FLAG, True)
    FeishuAdapter._on_card_action_trigger = wrapped
    logger.info("[push_card] installed push-confirm card-action hook on %s.FeishuAdapter",
                FeishuAdapter.__module__)
    return True


def _is_push_confirm_action(value: Any, form_value: Any, action: Any) -> bool:
    """True when a card action is a push-confirm submit.

    A plain-button click (retry / reauth) carries its routing dict in
    ``action.value`` — the cred_auth sibling proves that path works. But a FORM
    submit is delivered differently: Feishu drops the submit button's ``value``,
    so ``action.value`` comes back empty and only ``action.form_value`` (the user
    inputs) + ``action.name`` (``push_confirm_submit_<op>``) survive. Detect all
    three shapes so the form submit is claimed here instead of falling through to
    the core handler, which would synthesize a ``/card`` message (root cause of
    the "命令调度器" reply)."""
    if isinstance(value, dict) and value.get("hermes_action") == "push_confirm":
        return True
    fv = _read_action_value(form_value)
    if isinstance(fv, dict) and fv.get("hermes_action") == "push_confirm":
        return True
    name = _read(action, "name")
    return isinstance(name, str) and name.startswith("push_confirm_submit_")


def _routing_ids(value: Any, form_value: Any) -> tuple[str, str]:
    """(registry_id, nonce) from wherever Feishu delivered the button value —
    ``action.value`` for a plain button, or merged into ``action.form_value`` on
    some form-submit shapes. Empty when the value was stripped entirely."""
    fv = _read_action_value(form_value)
    for src in (value, fv):
        if isinstance(src, dict) and src.get("registry_id"):
            return str(src.get("registry_id") or ""), str(src.get("nonce") or "")
    return "", ""


def _recover_open_row(store: _reg.PushRegistryStore, operator_open_ids: set[str]) -> Optional[dict[str, Any]]:
    """Recover the clicker's open fill-form row when the form submit lost its
    routing value. Only a ``clarifying`` row renders a submit form, so restrict
    to those (a plain-button retry runs on a ``confirmed`` row but keeps its
    value, so it never needs this). Newest-first on ties.

    ponytail: picks the most-recent clarifying row per user — the one-active-card
    norm the rate limiter enforces. Persist the operation_id on the row to
    disambiguate if concurrent cards per user ever ship."""
    best: Optional[dict[str, Any]] = None
    for open_id in operator_open_ids:
        try:
            rows = store.list_open_for_user(open_id)
        except Exception:
            logger.debug("[push_card] open-row recovery lookup failed", exc_info=True)
            continue
        for row in rows:
            if row.get("status") != _reg.STATUS_CLARIFYING:
                continue
            if best is None or int(row.get("created_at") or 0) >= int(best.get("created_at") or 0):
                best = row
    return best


def _compute_confirm_result(
    event: Any, action: Any, value: dict[str, Any], form_value: Any = None
) -> ConfirmResult:
    """Pure decision half of a confirm click, shared by BOTH live seams: the
    ``_on_card_action_trigger`` wrapper (adapter path) and
    ``try_route_push_confirm_synthetic`` (this gateway's synthetic-/card path).
    Recovers registry_id+nonce (value → form_value → the clicker's open
    clarifying row) and runs the idempotent ``handle_confirm``. No Feishu SDK /
    no send — the caller decides how to render the result."""
    store = _reg.get_registry_store()
    if form_value is None:
        form_value = _read(action, "form_value")
    registry_id, nonce = _routing_ids(value, form_value)
    operator_ids = _resolve_operator_open_ids(event)
    # Form submit stripped the routing value → recover it from the user's open
    # clarifying row. registry_id + nonce taken from the SAME row stay
    # self-consistent, so handle_confirm's nonce guard still passes; the CAS +
    # write_idempotency_key(=registry_id) remain the real exactly-once defences.
    if not (registry_id and nonce):
        recovered = _recover_open_row(store, operator_ids)
        if recovered is not None:
            registry_id = str(recovered.get("registry_id") or "")
            nonce = str(recovered.get("nonce") or "")
    result = handle_confirm(
        registry_id=registry_id, nonce=nonce, operator_open_ids=operator_ids,
        form_value=form_value, store=store,
    )
    return result


def _dispatch_confirm(
    adapter: Any, event: Any, action: Any, value: dict[str, Any], form_value: Any = None
) -> Any:
    """SDK-response half (used by the ``_on_card_action_trigger`` wrapper): render
    the ConfirmResult as an inline card/toast callback response."""
    result = _compute_confirm_result(event, action, value, form_value)
    if result.card is not None:
        return _card_response(result.card)
    return _toast_response(result.toast or _toast("已处理。"))


def _synthetic_confirm_parts(event: Any) -> Optional[tuple[Any, Any, dict[str, Any], Any]]:
    """Cheap, adapter-free detector for a push-confirm delivered as a synthetic
    ``/card`` COMMAND event. Returns ``(card_event, action, value, form_value)``
    when the event's ``raw_message`` (the original card-action ``data``) is a
    push-confirm submit, else ``None``. No side effects, no adapter — so a normal
    message costs one attribute walk and adds ZERO adapter resolutions."""
    data = getattr(event, "raw_message", None)
    card_event = _read(data, "event")
    action = _read(card_event, "action")
    if action is None:
        return None
    value = _read_action_value(_read(action, "value"))
    form_value = _read(action, "form_value")
    if not _is_push_confirm_action(value, form_value, action):
        return None
    return card_event, action, value if isinstance(value, dict) else {}, form_value


def _confirm_reply_open_id(card_event: Any, result: ConfirmResult) -> str:
    """DM target for the confirm result card on the synthetic-command path: the
    registry row's ``target_open_id`` (where the original card went), else the
    signed operator's ou_ open_id. We read the RAW operator (``card_event``), not
    the synthetic event's ``source`` — the latter carries a user_id-namespace id,
    while ``send_push_card_via_adapter`` targets an open_id DM."""
    if result.registry_id:
        try:
            row = _reg.get_registry_store().get(result.registry_id)
        except Exception:
            row = None
        if row and row.get("target_open_id"):
            return str(row["target_open_id"])
    ids = _resolve_operator_open_ids(card_event)
    for oid in sorted(ids):
        if str(oid).startswith("ou_"):
            return str(oid)
    return next(iter(sorted(ids)), "")


async def try_route_push_confirm_synthetic(gateway: Any, event: Any) -> bool:
    """LIVE gateway seam. The multitenancy Feishu adapter routes a card BUTTON
    click as a synthetic ``/card`` COMMAND event (``_handle_card_action_event`` →
    ``handle_message`` → ``pre_gateway_dispatch`` → router ``handle_async``); it
    NEVER re-enters ``_on_card_action_trigger``, so the 5th-hook wrapper above
    cannot fire on this gateway (live log: ``Routing card action 'button' … as
    synthetic command``). The original card-action ``data`` survives on the
    synthetic event's ``raw_message``, so detect a push-confirm submit there and
    drive ``handle_confirm`` HERE — short-circuiting BEFORE ``parse_command``
    turns the ``/card`` text into the "命令但无调度器" reply (zero write,
    re-clickable button).

    Returns True when the event was a push-confirm and is now CONSUMED (caller
    must return without command dispatch). False = passthrough (not ours / any
    error). Idempotent: ``handle_confirm``'s CAS + write_idempotency_key make a
    double-click exactly one write, so this and the ``_on_card_action_trigger``
    wrapper can never double-write even if both fire. Adapter is resolved lazily
    (only on a real confirm) to keep normal-message dispatch untouched."""
    try:
        parts = _synthetic_confirm_parts(event)
        if parts is None:
            return False
        card_event, action, value, form_value = parts
        from . import push_send_queue as _sendq
        from .router import _get_feishu_adapter
        adapter = _get_feishu_adapter(gateway)
        if adapter is not None:
            _sendq.note_live_adapter(adapter)
        result = _compute_confirm_result(card_event, action, value, form_value)
        card = result.card
        if card is None and result.toast is not None:
            content = (result.toast.get("toast") or {}).get("content") or "已处理。"
            card = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": content}]}}
        open_id = _confirm_reply_open_id(card_event, result)
        if adapter is not None and card is not None and open_id:
            try:
                await _sendq.send_push_card_via_adapter(adapter, open_id=open_id, card=card)
            except Exception:
                logger.warning(
                    "[push_card] confirm result card send failed (registry=%s)",
                    result.registry_id, exc_info=True,
                )
        return True
    except Exception:
        logger.debug("[push_card] synthetic confirm route failed; passthrough", exc_info=True)
        return False


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
