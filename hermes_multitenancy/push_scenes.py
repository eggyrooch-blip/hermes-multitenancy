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
from dataclasses import dataclass
from typing import Any, Optional

# Field risk levels. "high" fields (money / date / person) must NOT be silently
# pre-filled — low-confidence stays empty and is asked (design §2.4, P0-4).
RISK_LOW = "low"
RISK_HIGH = "high"


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

SCENE_ORDER: tuple[str, ...] = (DEV_ACCEPTANCE_CLAIM.scene,)
BUILTIN_SCENES: dict[str, SceneDefinition] = {
    DEV_ACCEPTANCE_CLAIM.scene: DEV_ACCEPTANCE_CLAIM,
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
