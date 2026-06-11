"""Per-turn token-usage ledger.

工件 1a：在每个 Hermes 回合跑完那一刻，把「触发人 + 模型 + 本回合 token 数」追加成
一行 JSONL，供工件 1b 的每小时 uploader 读取、解析成企业邮箱并上报到排行榜。

设计照搬 ``conversation_audit.py``（同目录、同模式）：
  - env 开关默认关，prod ``.env`` 打开才写 → 可灰度上线。
  - 整段 try/except 包裹，任何失败都只 debug/log，绝不影响主回合。
  - 只记 token 计数与身份维度，绝不记 prompt / 回复 / 工具参数（合规底线）。

台账契约（工件 2 的 uploader 依赖，勿擅改字段名）::

    {"ts": "<上海时区 ISO,秒>", "sender_open_id": "ou_...", "profile": "<name>",
     "platform": "feishu", "chat_type": "p2p|group", "model": "<裸模型名>",
     "input_tokens": int, "output_tokens": int, "total_tokens": int}

token 来源：上游核心 ``AIAgent`` 实例的累计计数器
``session_input_tokens / session_output_tokens / session_total_tokens``
（``run_agent.py`` 约 2021-2027 定义）。multitenancy 每个回合都新建一个 ``AIAgent``
（``agent_real.py`` 约 3717），计数器从 0 起累加，所以跑完读到的就是「这一回合
（含工具循环里多次模型调用）的合计」，不含历史回合 → 不重复计数。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LEDGER_PATH = Path("/var/log/hermes/token-usage.jsonl")
_SHANGHAI_TZ = timezone(timedelta(hours=8))
_ENSURED_PARENT_DIRS: set[Path] = set()


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def token_usage_ledger_enabled() -> bool:
    return _truthy(os.getenv("HERMES_TOKEN_USAGE_LEDGER_ENABLED"))


def token_usage_ledger_path() -> Path:
    raw = os.getenv("HERMES_TOKEN_USAGE_LEDGER_PATH")
    if raw and raw.strip():
        return Path(raw).expanduser()
    return DEFAULT_LEDGER_PATH


def _now_iso() -> str:
    return datetime.now(tz=_SHANGHAI_TZ).isoformat(timespec="seconds")


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def read_agent_session_tokens(agent: Any) -> dict[str, int]:
    """从 AIAgent 实例安全读出本回合 token 累计。属性缺失一律兜底 0，不抛。"""
    return {
        "input_tokens": _int(getattr(agent, "session_input_tokens", 0)),
        "output_tokens": _int(getattr(agent, "session_output_tokens", 0)),
        "total_tokens": _int(getattr(agent, "session_total_tokens", 0)),
    }


def append_token_usage(
    *,
    sender_open_id: str | None,
    profile: str | None,
    platform: str | None,
    chat_type: str | None,
    model: str | None,
    input_tokens: Any,
    output_tokens: Any,
    total_tokens: Any,
    timestamp: str | None = None,
) -> None:
    """追加一行 token 台账。开关关 / token 非正 / 任何异常 → 静默 no-op。"""
    if not token_usage_ledger_enabled():
        return

    it = _int(input_tokens)
    ot = _int(output_tokens)
    tt = _int(total_tokens) or (it + ot)
    if tt <= 0:
        return  # 没有可计量的消耗，不落噪音行

    event = {
        "ts": timestamp or _now_iso(),
        "sender_open_id": str(sender_open_id or ""),
        "profile": str(profile or ""),
        "platform": str(platform or ""),
        "chat_type": str(chat_type or ""),
        "model": str(model or ""),
        "input_tokens": it,
        "output_tokens": ot,
        "total_tokens": tt,
    }

    try:
        path = token_usage_ledger_path()
        parent = path.parent
        if parent not in _ENSURED_PARENT_DIRS:
            parent.mkdir(parents=True, exist_ok=True)
            _ENSURED_PARENT_DIRS.add(parent)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            fh.write("\n")
    except Exception:
        logger.exception("[multitenancy] token usage ledger append failed")
