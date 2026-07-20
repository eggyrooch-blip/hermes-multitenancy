"""Static scene registry for the push-card fill loop (M1).

场景即配置：a scene is a schema (fields + risk policy + backend writer name) plus
the fill-skill slug the server injects when an inbound reply is matched to a
registry row. New scene = new ``SceneDefinition`` here — **no framework code
changes** (design §2.4). The endpoint derives ``skill`` from ``scene`` so an
external caller can never inject an arbitrary skill (design §2.1, P0-1).

M1 ships exactly one built-in scene, ``dev-acceptance-claim`` (design §5.1), a
deterministic fake reimbursement claim used to run the dev acceptance script
against a kep *pre* backend.

This mirrors the ``connectors/`` convention (id constants + ``*_ORDER`` tuple +
``dict[id -> dataclass]`` + accessor functions) but collapsed into one file —
one scene does not justify a package split.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Field risk levels. "high" fields (money / date / person) must NOT be silently
# pre-filled — low-confidence stays empty and is asked (design §2.4, P0-4).
RISK_LOW = "low"
RISK_HIGH = "high"

# Scene routing mode (how a matched reply is handled):
#   card — the SAFE default: framework drives the form闭环 (merge → clarify /
#          confirm card → button confirm → deterministic backend write). The
#          agent is short-circuited so it can never freelance the write.
#   yolo — the framework only ROUTES the reply into the scene's ``skill`` and
#          gets out of the way; the business skill self-drives compute → 请确认
#          → execute/写库 in its own agent turn. No framework form, no confirm
#          callback write.
MODE_CARD = "card"
MODE_YOLO = "yolo"
_VALID_MODES = frozenset({MODE_CARD, MODE_YOLO})


@dataclass(frozen=True)
class CallbackConfig:
    """Where a confirmed submission is written — the "回调地址/落库 endpoint".

    Endpoint is *data*, not framework code: a scene registers its own落库 endpoint
    (``scene.callback``), and a per-push ``notify-card`` payload may override it.
    Never store a plaintext secret here — ``auth_header`` is the header NAME
    (e.g. ``Authorization``) and ``auth_token_env`` is the env var NAME holding
    the token, resolved by the writer at request time."""

    url: str
    auth_header: Optional[str] = None
    auth_token_env: Optional[str] = None
    timeout_s: int = 10


@dataclass(frozen=True)
class SubmitBehaviors:
    """How the confirm/submit button behaves — a per-scene / per-push capability.

    Defaults reproduce M1's original hardcoded behavior exactly: submit once,
    then the card is terminal (a second click no-ops "已录入✅"). Making it data
    lets a scene (or a single push) opt into 改单 without a framework code change."""

    #: submit succeeds once → card terminal, re-click no-ops (current behavior).
    submit_once: bool = True
    #: a still-pre-commit row may be re-submitted (retry/edit) — default on.
    allow_resubmit_before_commit: bool = True
    #: cap on total accepted submits (None = unlimited). Only bites when
    #: ``submit_once`` is False (改单 enabled).
    max_submits: Optional[int] = None


@dataclass(frozen=True)
class SceneField:
    key: str
    label: str
    type: str  # number | date | enum | text
    required: bool = False
    risk: str = RISK_LOW
    options: tuple[str, ...] = ()
    rule: str = ""


@dataclass(frozen=True)
class SceneDefinition:
    scene: str
    name: str
    #: fill-skill slug injected via the broker's native slash rewriter on match.
    skill: str
    #: backend writer name (design §2.5); the confirm callback (P5) resolves it.
    writer: str
    fields: tuple[SceneField, ...]
    #: appended (invisibly) to the write payload so the backend can verify
    #: "exactly one" record by unique lookup (design §5.1). ``{registry_id}``
    #: is substituted at write time.
    deterministic_marker: str = ""
    #: this scene's default 落库 endpoint (design §config). ``None`` = defer to a
    #: per-push override or the dev env fallback (kept only as a dev shortcut).
    callback: Optional[CallbackConfig] = None
    #: default submit behavior for this scene; a push may override per-card.
    behaviors: SubmitBehaviors = SubmitBehaviors()
    #: routing mode — ``card`` (safe framework-driven form loop) or ``yolo``
    #: (route the reply into ``skill``; the skill self-drives). An invalid value
    #: falls back to ``card`` + logs, so a config typo can never crash plugin
    #: load or silently unlock the放权 path.
    mode: str = MODE_CARD

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            logger.warning(
                "[push_scenes] scene %r has invalid mode %r; falling back to %r",
                self.scene, self.mode, MODE_CARD,
            )
            object.__setattr__(self, "mode", MODE_CARD)  # frozen dataclass

    def field(self, key: str) -> Optional[SceneField]:
        for f in self.fields:
            if f.key == key:
                return f
        return None

    def required_keys(self) -> tuple[str, ...]:
        return tuple(f.key for f in self.fields if f.required)


DEV_ACCEPTANCE_CLAIM = SceneDefinition(
    scene="dev-acceptance-claim",
    name="开发验收·测试报销单",
    skill="push-fill-form",
    writer="kep-pre-claim-writer",
    fields=(
        SceneField("amount", "金额(元)", "number", required=True, risk=RISK_HIGH,
                   rule="不预填;提交前二次回显"),
        SceneField("date", "发生日期", "date", required=True, risk=RISK_HIGH,
                   rule="低置信留空追问;预填标注AI提取"),
        SceneField("category", "类目", "enum", required=True,
                   options=("打车", "餐饮", "办公用品")),
        SceneField("reason", "事由", "text", required=True),
    ),
    deterministic_marker="[PAI-ACC-{registry_id}]",
)

# A dev-only YOLO scene (design: card + yolo双模式). It exists purely to prove
# the "route the reply into the scene's skill" mechanism end to end — no real
# business logic, no framework form. ``skill`` names a simple echo/确认 skill;
# ``fields`` is empty because a yolo scene never renders a framework form (the
# skill owns compute/confirm/execute). Reply to the pushed card (a quote or an
# on-topic reply) and the framework injects ``/push-yolo-echo <你的原话>`` and
# hands the turn to the agent.
DEV_YOLO_ECHO = SceneDefinition(
    scene="dev-yolo-echo",
    name="开发验收·YOLO 回显",
    skill="push-yolo-echo",
    writer="",  # yolo: no framework writer — the skill self-drives写库.
    fields=(),
    mode=MODE_YOLO,
)

SCENE_ORDER: tuple[str, ...] = (DEV_ACCEPTANCE_CLAIM.scene, DEV_YOLO_ECHO.scene)
BUILTIN_SCENES: dict[str, SceneDefinition] = {
    DEV_ACCEPTANCE_CLAIM.scene: DEV_ACCEPTANCE_CLAIM,
    DEV_YOLO_ECHO.scene: DEV_YOLO_ECHO,
}


def get_scene(scene: str) -> Optional[SceneDefinition]:
    return BUILTIN_SCENES.get(str(scene or "").strip())


def scene_exists(scene: str) -> bool:
    return str(scene or "").strip() in BUILTIN_SCENES


def list_scenes() -> list[SceneDefinition]:
    return [BUILTIN_SCENES[s] for s in SCENE_ORDER]


# --- callback / behaviors (de)serialization -------------------------------
#
# ``notify-card`` may carry a per-push ``callback`` / ``behaviors`` override;
# these parse the untrusted request dict (fail-loud on a malformed override) and
# (de)serialize the stored registry JSON. Only whitelisted fields are stored —
# a plaintext token is never accepted or persisted (auth = header name + env
# var name only).

def callback_from_payload(raw: Any) -> Optional[CallbackConfig]:
    """Parse a notify-card ``callback`` override. ``None`` if absent; raises
    ``ValueError`` if present but malformed (a push override MUST carry a url)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("callback must be an object")
    url = str(raw.get("url") or "").strip()
    if not url:
        raise ValueError("callback.url is required")
    timeout = raw.get("timeout_s")
    auth_header = raw.get("auth_header")
    auth_token_env = raw.get("auth_token_env")
    return CallbackConfig(
        url=url,
        auth_header=str(auth_header).strip() or None if auth_header else None,
        auth_token_env=str(auth_token_env).strip() or None if auth_token_env else None,
        timeout_s=int(timeout) if isinstance(timeout, (int, float)) and int(timeout) > 0 else 10,
    )


def callback_to_json(cb: CallbackConfig) -> str:
    """Serialize for the registry ``callback_json`` column — whitelisted fields
    only, so no plaintext secret can ever leak into the row."""
    out: dict[str, Any] = {"url": cb.url, "timeout_s": cb.timeout_s}
    if cb.auth_header:
        out["auth_header"] = cb.auth_header
    if cb.auth_token_env:
        out["auth_token_env"] = cb.auth_token_env
    return json.dumps(out, ensure_ascii=False)


def callback_from_json(raw: Any) -> Optional[CallbackConfig]:
    """Tolerant read of a stored ``callback_json`` (``None`` on empty/bad/no-url)."""
    if not raw:
        return None
    data = raw if isinstance(raw, dict) else None
    if data is None:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(data, dict):
        return None
    try:
        return callback_from_payload(data)
    except ValueError:
        return None


def behaviors_from_payload(raw: Any) -> Optional[SubmitBehaviors]:
    """Parse a notify-card ``behaviors`` override on top of the framework
    defaults. ``None`` if absent; raises ``ValueError`` if not an object."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("behaviors must be an object")
    base = SubmitBehaviors()
    maxs = raw.get("max_submits", base.max_submits)
    return SubmitBehaviors(
        submit_once=bool(raw.get("submit_once", base.submit_once)),
        allow_resubmit_before_commit=bool(
            raw.get("allow_resubmit_before_commit", base.allow_resubmit_before_commit)
        ),
        max_submits=int(maxs) if isinstance(maxs, (int, float)) else None,
    )


def behaviors_to_json(b: SubmitBehaviors) -> str:
    return json.dumps(
        {
            "submit_once": b.submit_once,
            "allow_resubmit_before_commit": b.allow_resubmit_before_commit,
            "max_submits": b.max_submits,
        },
        ensure_ascii=False,
    )


def behaviors_from_json(raw: Any) -> Optional[SubmitBehaviors]:
    """Tolerant read of a stored ``behaviors_json`` (``None`` on empty/bad)."""
    if not raw:
        return None
    data = raw if isinstance(raw, dict) else None
    if data is None:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(data, dict):
        return None
    try:
        return behaviors_from_payload(data)
    except ValueError:
        return None


# --- inline / row-carried scene spec --------------------------------------
#
# A ``notify-card`` push may DESCRIBE its scene inline (skill/mode/fields/
# callback) instead of naming a pre-registered one — self-serve, no server-side
# registration (design route A). That EffectiveScene is serialized onto the
# registry row (``scene_spec_json``) so every later step — matcher, fill form,
# confirm — reconstructs the exact spec from the ROW via ``scene_def_for_row``
# instead of a name lookup that would miss for an unregistered inline scene.

def _fields_from_raw(raw_fields: Any) -> tuple[SceneField, ...]:
    """Parse a ``fields`` list (untrusted or stored) into ``SceneField`` tuple.
    Fail-loud on a malformed entry (missing key / not a list)."""
    if raw_fields is None:
        return ()
    if not isinstance(raw_fields, list):
        raise ValueError("fields must be a list")
    out: list[SceneField] = []
    for i, f in enumerate(raw_fields):
        if not isinstance(f, dict):
            raise ValueError(f"fields[{i}] must be an object")
        key = str(f.get("key") or "").strip()
        if not key:
            raise ValueError(f"fields[{i}].key is required")
        raw_opts = f.get("options")
        options = tuple(str(o) for o in raw_opts) if isinstance(raw_opts, list) else ()
        risk = RISK_HIGH if str(f.get("risk") or "").strip() == RISK_HIGH else RISK_LOW
        out.append(SceneField(
            key=key,
            label=str(f.get("label") or key),
            type=str(f.get("type") or "text").strip() or "text",
            required=bool(f.get("required", False)),
            risk=risk,
            options=options,
            rule=str(f.get("rule") or ""),
        ))
    return tuple(out)


def scene_from_payload(payload: Any) -> SceneDefinition:
    """Build an EffectiveScene from an INLINE notify-card description (skill/mode/
    fields/callback/behaviors) — self-serve, no server-side registration. Raises
    ``ValueError`` on a malformed description so the endpoint fail-louds (400).

    The write target is data: a card scene writes to its (per-push) ``callback``
    via the generic HTTP writer; a yolo scene has no framework writer (the skill
    self-drives). Reuses ``callback_from_payload`` / ``behaviors_from_payload``."""
    if not isinstance(payload, dict):
        raise ValueError("scene payload must be an object")
    skill = str(payload.get("skill") or "").strip()
    if not skill:
        raise ValueError("skill is required for an inline scene")
    mode = str(payload.get("mode") or MODE_CARD).strip() or MODE_CARD
    if mode not in _VALID_MODES:
        raise ValueError(f"mode must be one of {sorted(_VALID_MODES)}")
    fields = _fields_from_raw(payload.get("fields"))
    if mode == MODE_CARD and not fields:
        raise ValueError("a card-mode scene requires at least one field")
    callback = callback_from_payload(payload.get("callback"))
    behaviors = behaviors_from_payload(payload.get("behaviors")) or SubmitBehaviors()
    scene_name = str(payload.get("scene") or "").strip() or f"inline:{skill}"
    name = str(payload.get("name") or "").strip() or scene_name
    return SceneDefinition(
        scene=scene_name,
        name=name,
        skill=skill,
        writer="" if mode == MODE_YOLO else "kep-pre-claim-writer",
        fields=fields,
        deterministic_marker=str(payload.get("deterministic_marker") or ""),
        callback=callback,
        behaviors=behaviors,
        mode=mode,
    )


def scene_to_spec_json(scene: SceneDefinition) -> str:
    """Serialize an EffectiveScene for the registry ``scene_spec_json`` column.
    Whitelisted fields only (callback via ``callback_to_json`` → never a plaintext
    secret). ``scene_from_spec_json`` is the exact inverse."""
    return json.dumps(
        {
            "scene": scene.scene,
            "name": scene.name,
            "skill": scene.skill,
            "writer": scene.writer,
            "mode": scene.mode,
            "deterministic_marker": scene.deterministic_marker,
            "fields": [
                {
                    "key": f.key, "label": f.label, "type": f.type,
                    "required": f.required, "risk": f.risk,
                    "options": list(f.options), "rule": f.rule,
                }
                for f in scene.fields
            ],
            "callback": json.loads(callback_to_json(scene.callback)) if scene.callback else None,
            "behaviors": json.loads(behaviors_to_json(scene.behaviors)),
        },
        ensure_ascii=False,
    )


def scene_from_spec_json(raw: Any) -> Optional[SceneDefinition]:
    """Reconstruct an EffectiveScene stored on a registry row. Tolerant read:
    ``None`` on empty/malformed so the caller falls back to the named registry."""
    if not raw:
        return None
    data = raw if isinstance(raw, dict) else None
    if data is None:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(data, dict):
        return None
    try:
        skill = str(data.get("skill") or "").strip()
        if not skill:
            return None
        fields = _fields_from_raw(data.get("fields"))
        scene_name = str(data.get("scene") or "").strip() or f"inline:{skill}"
        return SceneDefinition(
            scene=scene_name,
            name=str(data.get("name") or scene_name),
            skill=skill,
            writer=str(data.get("writer") or ""),
            fields=fields,
            deterministic_marker=str(data.get("deterministic_marker") or ""),
            callback=callback_from_json(data.get("callback")),
            behaviors=behaviors_from_json(data.get("behaviors")) or SubmitBehaviors(),
            # SceneDefinition.__post_init__ normalizes an invalid mode → card.
            mode=str(data.get("mode") or MODE_CARD).strip() or MODE_CARD,
        )
    except (ValueError, TypeError):
        return None


def scene_def_for_row(row: Optional[dict[str, Any]]) -> Optional[SceneDefinition]:
    """The effective scene for a registry row. An inline row carries its full
    spec in ``scene_spec_json`` (self-serve, unregistered) → reconstruct it;
    otherwise fall back to the named builtin registry (backward compatible).

    Every reply/confirm-time scene resolution routes through here instead of
    ``get_scene(name)`` so an inline scene (no registered name) still resolves."""
    if not row:
        return None
    spec = scene_from_spec_json(row.get("scene_spec_json"))
    if spec is not None:
        return spec
    return get_scene(row.get("scene") or "")
