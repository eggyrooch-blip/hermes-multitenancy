"""Generic fill-skill skeleton + confirm form card (SPEC P4, design §2.4).

Scene即配置: every behaviour here is driven by a ``SceneDefinition``'s field
schema (key/type/required/risk/rule) — a new scene adds a ``SceneDefinition``
in ``push_scenes`` and needs **zero** change here.

Two jobs:

1. **Extraction with prefill discipline (P0-4, the most important修订).** The
   model's role is compressed to "prefill suggestion", never a silent write:
   * high-risk fields (money/date/person) are NOT silently pre-filled — a
     low-confidence extraction stays empty and is asked;
   * a field whose rule says ``不预填`` (money) NEVER pre-fills its input — the
     extracted number is only echoed for the user to actively re-enter/confirm;
   * extraction honours 否定/清空 semantics ("不是5000" / "重新来" → clear and
     re-ask), it is a replay-merge, not a pure accumulate.

2. **Confirm form card** following the openclaw ask-user-question 四坑:
   * ONE ``form`` container — Feishu only回传 ``form_value`` for controls inside
     a single form on submit;
   * the submit button MUST carry a ``value`` (a value-less submit is silently
     dropped with error 200340);
   * ``operation_id`` is written twice (button ``name`` suffix + ``value``) so a
     double-tap collapses to one dedupe key;
   * a typing修改 re-renders in place via ``card.update``; with no message_id yet
     (send race) the caller falls back to sending a new card — see
     ``delivery_mode``.

Extraction of arbitrary Chinese NL is genuinely an LLM job; the default
extractor is a best-effort heuristic tuned for the deterministic
``dev-acceptance-claim`` script (design §5.1). Production scenes wire a real
extractor via the ``extractor`` seam.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .push_scenes import RISK_HIGH, SceneDefinition, SceneField

# --- confidence policy ---------------------------------------------------

#: A high-risk field is only pre-filled into its input when the extractor is at
#: least this confident; below it the input stays empty and the field is asked
#: (design §2.4). Low-risk fields use the lower bar below.
HIGH_RISK_PREFILL_CONF = 0.80
LOW_RISK_PREFILL_CONF = 0.55

#: Cap sizes — a hostile/huge reply must never bloat the card or a stored value.
_MAX_TEXT_CHARS = 500
_MAX_REASON_CHARS = 500


@dataclass
class FieldState:
    """One field's current extracted/entered state.

    ``ai`` marks a value the *model* proposed (renders a "AI 提取，请核对"
    annotation); a value the user typed into the form arrives with ``ai=False``
    and full confidence."""

    value: Any = None
    confidence: float = 0.0
    ai: bool = True

    def has_value(self) -> bool:
        return self.value is not None and str(self.value).strip() != ""


Submission = dict[str, FieldState]

#: extractor seam: ``(scene, text) -> dict[key -> (value, confidence)]``. A key
#: mapped to ``(None, conf)`` is an explicit clear signal (否定/重来).
Extractor = Callable[[SceneDefinition, str], dict[str, tuple[Any, float]]]


# --- 否定/清空 detection --------------------------------------------------

_CLEAR_ALL_RE = re.compile(r"(重新来|重来|全部清空|重填|作废|清空|重新填)")
_NEGATE_RE = re.compile(r"(不是|不对|错了|填错|写错)")
_NUM_RE = re.compile(r"(?<![\d.])\d+(?:\.\d+)?")
_MONEY_CTX_RE = re.compile(r"(金额|花了|花费|付了|支付|报销|¥|块|元|钱|费用|花销)")
_DATE_RE = re.compile(
    r"(今天|昨天|前天|明天|大前天|上周[一二三四五六日天]?|本周[一二三四五六日天]?"
    r"|这周[一二三四五六日天]?|周[一二三四五六日天]|礼拜[一二三四五六日天]"
    r"|\d{1,2}\s*[月/-]\s*\d{1,2}\s*[日号]?|\d{4}\s*[年/-]\s*\d{1,2})"
)
_REPLACE_RE = re.compile(r"(改成|改为|换成|应该是|其实是)")
_NEG_CHARS = ("不", "否", "非", "错", "别")


def _negated_before(text: str, idx: int, *, window: int = 3) -> bool:
    """True when a negation char sits just before ``idx`` — "不是打车" / "不是58"
    means clear that field, not (re)set it to the value that literally follows
    the 不是."""
    return any(ch in text[max(0, idx - window):idx] for ch in _NEG_CHARS)


def default_extractor(scene: SceneDefinition, text: str) -> dict[str, tuple[Any, float]]:
    """Heuristic extraction for the dev-acceptance-claim script.

    ponytail: regex heuristic, good enough for the deterministic dev scene
    (58→68 / 上周五 / 打车 / 去客户现场). Upgrade path: pass an LLM ``extractor``
    for production scenes — the discipline in ``merge`` is extractor-agnostic.
    """
    t = str(text or "").strip()
    out: dict[str, tuple[Any, float]] = {}
    if not t:
        return out

    clear_all = bool(_CLEAR_ALL_RE.search(t))
    negating = bool(_NEGATE_RE.search(t))

    for f in scene.fields:
        if clear_all:
            out[f.key] = (None, 1.0)
            continue
        if f.type == "number":
            m = _NUM_RE.search(t)
            if m and _negated_before(t, m.start()):
                out[f.key] = (None, 1.0)  # "不是58" → clear this field
            elif m:
                num = m.group(0)
                # money confidence: high when money context or an explicit
                # replace verb ("改成68") accompanies the number.
                has_ctx = bool(_MONEY_CTX_RE.search(t))
                has_replace = bool(_REPLACE_RE.search(t)) and (f.label.split("(")[0] in t or has_ctx)
                conf = 0.9 if (has_ctx or has_replace) else 0.5
                try:
                    val: Any = float(num) if "." in num else int(num)
                except ValueError:
                    val = num
                out[f.key] = (val, conf)
            elif negating:
                out[f.key] = (None, 1.0)
        elif f.type == "date":
            m = _DATE_RE.search(t)
            if m and _negated_before(t, m.start()):
                out[f.key] = (None, 1.0)
            elif m:
                out[f.key] = (m.group(0), 0.85)
            elif negating:
                out[f.key] = (None, 1.0)
        elif f.type == "enum":
            matched = next((opt for opt in f.options if opt and opt in t), None)
            if matched is not None and _negated_before(t, t.find(matched)):
                out[f.key] = (None, 1.0)  # "不是打车" → clear, not re-set
            elif matched is not None:
                out[f.key] = (matched, 0.9)
            elif negating:
                out[f.key] = (None, 1.0)
        elif f.type == "text":
            reason = _guess_reason(t, scene)
            if reason:
                out[f.key] = (reason, 0.6)
            elif negating:
                out[f.key] = (None, 1.0)
    return out


def _guess_reason(text: str, scene: SceneDefinition) -> Optional[str]:
    """Strip date/enum/amount tokens; the remaining action phrase is the reason.

    ponytail: crude token-strip heuristic for the dev scene ("去客户现场"). An
    LLM extractor supersedes it for real scenes."""
    stripped = text
    stripped = _DATE_RE.sub(" ", stripped)
    for f in scene.fields:
        for opt in f.options:
            if opt:
                stripped = stripped.replace(opt, " ")
    # drop the amount clause ("花了58" / "58元" / "¥58")
    stripped = re.sub(r"[¥]?\s*\d+(?:\.\d+)?\s*(元|块|钱)?", " ", stripped)
    stripped = re.sub(r"(花了|花费|付了|支付|报销|花销|金额|费用|是|改成|改为|了|的)", " ", stripped)
    candidate = re.sub(r"\s+", "", stripped).strip("，,。.；; ")
    if len(candidate) >= 2:
        return candidate[:_MAX_REASON_CHARS]
    return None


# --- merge / replay ------------------------------------------------------

def new_submission() -> Submission:
    return {}


def merge(
    scene: SceneDefinition,
    prior: Optional[Submission],
    text: str,
    *,
    extractor: Extractor = default_extractor,
) -> Submission:
    """Replay one reply onto the prior submission with prefill discipline.

    * a clear signal (``value is None``) wipes the field (否定/清空 semantics);
    * a field whose rule contains ``不预填`` never fills its input from
      extraction — its extracted value is retained only as an ``echo`` hint;
    * a high-risk field only fills when confidence ≥ ``HIGH_RISK_PREFILL_CONF``;
      below the bar it stays empty (to be asked / typed);
    * a user-entered value (``ai=False`` already in ``prior``) is never
      overwritten by a lower-confidence AI extraction.
    """
    sub: Submission = dict(prior or {})
    extracted = extractor(scene, text)
    for f in scene.fields:
        if f.key not in extracted:
            continue
        value, conf = extracted[f.key]
        if value is None:
            sub.pop(f.key, None)  # explicit clear
            continue
        value = _coerce(f, value)
        existing = sub.get(f.key)
        if existing is not None and not existing.ai and existing.has_value():
            # Never let AI clobber a value the user typed themselves.
            continue
        if _no_prefill(f):
            # Money: keep only as an echo suggestion; the input stays empty so
            # the user actively re-enters what they see (design §2.4 "不预填").
            sub.pop(f.key, None)
            _set_echo(sub, f.key, value)
            continue
        bar = HIGH_RISK_PREFILL_CONF if f.risk == RISK_HIGH else LOW_RISK_PREFILL_CONF
        if conf >= bar:
            sub[f.key] = FieldState(value=value, confidence=conf, ai=True)
        # below the bar: leave the field empty (ask it), do not silent-prefill.
    return sub


# echo values (money 二次回显 hints) ride alongside the submission under a
# reserved sentinel key so a single dict flows through the whole pipeline.
# ponytail: sentinel key in the FieldState dict — every reader iterates
# scene.fields (never bare .items()) so it is inert to them. Split into its own
# object only if a scene ever needs a field literally named "__echo__".
_ECHO_KEY = "__echo__"


def _set_echo(sub: Submission, key: str, value: Any) -> None:
    store = sub.get(_ECHO_KEY)  # type: ignore[assignment]
    if not isinstance(store, dict):
        store = {}
    store[key] = value
    sub[_ECHO_KEY] = store  # type: ignore[assignment]


def _echo_of(sub: Submission, key: str) -> Any:
    store = sub.get(_ECHO_KEY)
    if isinstance(store, dict):
        return store.get(key)
    return None


def _coerce(f: SceneField, value: Any) -> Any:
    if f.type == "number":
        if isinstance(value, (int, float)):
            return value
        try:
            s = str(value)
            return float(s) if "." in s else int(s)
        except (TypeError, ValueError):
            return value
    if f.type == "enum":
        v = str(value).strip()
        return v if (not f.options or v in f.options) else v
    return str(value).strip()[:_MAX_TEXT_CHARS]


def _no_prefill(f: SceneField) -> bool:
    return "不预填" in (f.rule or "")


# --- state → next action -------------------------------------------------

def missing_fields(scene: SceneDefinition, sub: Submission) -> list[SceneField]:
    """Required fields that still have no value (empty high-risk fields land
    here so the skill asks / the form shows an empty required input)."""
    out = []
    for f in scene.fields:
        if not f.required:
            continue
        st = sub.get(f.key)
        if st is None or not st.has_value():
            out.append(f)
    return out


def money_echo(scene: SceneDefinition, sub: Submission) -> Optional[str]:
    """The 二次回显 line for money fields — "将提交 ¥68，确认吗" (design §2.4).

    Prefers the value the user entered (``ai=False``); falls back to the AI echo
    hint so the card can prompt even before the user re-enters the amount."""
    for f in scene.fields:
        if f.type != "number":
            continue
        st = sub.get(f.key)
        amount: Any = None
        if st is not None and st.has_value():
            amount = st.value
        else:
            amount = _echo_of(sub, f.key)
        if amount is not None and str(amount).strip():
            return f"将提交 {f.label}：¥{amount}，请确认。"
    return None


# --- confirm form card (openclaw 四坑) -----------------------------------

def build_confirm_card(
    scene: SceneDefinition,
    sub: Submission,
    *,
    registry_id: str,
    nonce: str,
    operation_id: str,
    escape_hint: Optional[str] = None,
) -> dict[str, Any]:
    """Render the single-form confirm card for ``scene``.

    Every field is a control inside ONE ``form``; the submit button carries the
    routing ``value`` (registry_id + nonce + operation_id) — a value-less submit
    is dropped by Feishu (200340)."""
    elements: list[dict[str, Any]] = []
    intro = f"请核对并确认「{scene.name}」，确认后将录入后端系统。"
    elements.append({"tag": "markdown", "content": intro})

    form_elements: list[dict[str, Any]] = []
    for f in scene.fields:
        st = sub.get(f.key)
        prefilled = st.value if (st is not None and st.has_value()) else None
        ai = bool(st.ai) if st is not None else False
        label = f.label + ("（必填）" if f.required else "")
        if ai and prefilled is not None:
            label += "  ·AI 提取，请核对"
        echo = _echo_of(sub, f.key)
        if _no_prefill(f) and echo is not None and prefilled is None:
            label += f"  ·AI 听到：{echo}，请确认后填写"
        form_elements.append({"tag": "markdown", "content": f"**{label}**"})
        form_elements.append(_field_control(f, prefilled))

    money = money_echo(scene, sub)
    if money:
        form_elements.append({"tag": "markdown", "content": f"⚠️ {money}"})

    submit_label = "确认提交"
    if money:
        # fold the 二次回显 into the button so the amount is on the final tap.
        for f in scene.fields:
            if f.type == "number":
                st = sub.get(f.key)
                amt = st.value if (st is not None and st.has_value()) else _echo_of(sub, f.key)
                if amt is not None:
                    submit_label = f"确认提交 ¥{amt}"
                break

    form_elements.append({
        "tag": "button",
        # operation_id 双写 #1: encoded into the control name.
        "name": f"push_confirm_submit_{operation_id}",
        "text": {"tag": "plain_text", "content": submit_label},
        "type": "primary",
        "form_action_type": "submit",
        "value": {
            "hermes_action": "push_confirm",
            "registry_id": registry_id,
            "nonce": nonce,
            # operation_id 双写 #2: in the callback value (dedupe key source).
            "operation_id": operation_id,
        },
    })

    elements.append({"tag": "form", "name": "push_fill_form", "elements": form_elements})
    if escape_hint:
        elements.append({"tag": "markdown", "content": f"_{escape_hint}_"})

    return {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": scene.name}, "template": "blue"},
        "body": {"elements": elements},
    }


def _field_control(f: SceneField, prefilled: Any) -> dict[str, Any]:
    if f.type == "enum" and f.options:
        control: dict[str, Any] = {
            "tag": "select_static",
            "name": f.key,
            "required": f.required,
            "placeholder": {"tag": "plain_text", "content": f"请选择{f.label}"},
            "options": [
                {"text": {"tag": "plain_text", "content": o}, "value": o}
                for o in f.options
            ],
        }
        if prefilled is not None and str(prefilled) in f.options:
            control["initial_option"] = str(prefilled)
        return control
    control = {
        "tag": "input",
        "name": f.key,
        "required": f.required,
        "placeholder": {"tag": "plain_text", "content": f"请输入{f.label}"},
    }
    # High-risk-不预填 (money) intentionally omits default_value — the input
    # starts empty so the user actively enters what they see echoed.
    if prefilled is not None:
        control["default_value"] = str(prefilled)
    return control


def build_confirm_toast(content: str, *, level: str = "info") -> dict[str, Any]:
    return {"toast": {"type": level, "content": content}}


# --- delivery mode (openclaw 就地改卡降级链) -----------------------------

DELIVER_UPDATE = "update"
DELIVER_NEW = "new"


def delivery_mode(row: dict[str, Any]) -> str:
    """A re-render updates the existing card in place when we already have its
    ``message_id``; if the row raced ahead of the message_id write-back, fall
    back to sending a NEW card (design §2.4 openclaw降级链)."""
    return DELIVER_UPDATE if str(row.get("message_id") or "").strip() else DELIVER_NEW


# --- final submission from a submitted form ------------------------------

def submission_from_form(scene: SceneDefinition, form_value: Any) -> Submission:
    """The user's confirmed controls (``ai=False``, trusted) → a Submission.

    This is the payload that will be written — it is exactly the set of controls
    the user SAW and submitted; hidden/derived fields are never read from here
    (design §2.4 P1-5)."""
    src = form_value if isinstance(form_value, dict) else {}
    sub: Submission = {}
    for f in scene.fields:
        if f.key not in src:
            continue
        raw = src.get(f.key)
        if raw is None or str(raw).strip() == "":
            continue
        sub[f.key] = FieldState(value=_coerce(f, raw), confidence=1.0, ai=False)
    return sub


def submission_values(scene: SceneDefinition, sub: Submission) -> dict[str, Any]:
    """Plain ``{key: value}`` of filled fields — the writer's input payload."""
    out: dict[str, Any] = {}
    for f in scene.fields:
        st = sub.get(f.key)
        if st is not None and st.has_value():
            out[f.key] = st.value
    return out
