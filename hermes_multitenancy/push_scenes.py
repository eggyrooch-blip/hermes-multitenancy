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

from dataclasses import dataclass, field
from typing import Optional

# Field risk levels. "high" fields (money / date / person) must NOT be silently
# pre-filled — low-confidence stays empty and is asked (design §2.4, P0-4).
RISK_LOW = "low"
RISK_HIGH = "high"


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
