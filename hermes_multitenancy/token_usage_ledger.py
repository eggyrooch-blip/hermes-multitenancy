"""Per-turn token-usage ledger.

工件 1a：在每个 Hermes 回合跑完那一刻，把「触发人 + 模型 + 本回合 token 数」追加成
一行 JSONL，供工件 1b 的每小时 uploader 读取、解析成企业邮箱并上报到排行榜。

设计照搬 ``conversation_audit.py``（同目录、同模式）：
  - env 开关默认关，prod ``.env`` 打开才写 → 可灰度上线。
  - 整段 try/except 包裹，任何失败都只 debug/log，绝不影响主回合。
  - 只记 token 计数与身份维度，绝不记 prompt / 回复 / 工具参数（合规底线）。

台账契约（工件 2 的 uploader 依赖，勿擅改字段名）::

    {"ts": "<上海时区 ISO,秒>", "sender_open_id": "ou_...", "profile": "<name>",
     "platform": "feishu|webui|cron", "chat_type": "p2p|group", "model": "<裸模型名>",
     "input_tokens": int, "output_tokens": int, "total_tokens": int,
     "cache_read_tokens": int, "cache_write_tokens": int, "api_calls": int}

后三个字段是「尺子」（2026-08-13 加）：缓存命中率与每轮模型调用次数。没有它们，
「换个便宜模型到底省不省」只能靠估算——缓存读价是输入价的 0.1x，换模型打掉前缀缓存的
代价可能反超降级省下的差价；而小模型能力弱导致多绕几轮，也只有 ``api_calls`` 能看见。
既有字段名与语义逐字不变，新增字段对下游 uploader 是可忽略的额外 key。

token 来源：上游核心 ``AIAgent`` 实例的累计计数器
``session_input_tokens / session_output_tokens / session_total_tokens``
（``run_agent.py`` 约 2021-2027 定义）。multitenancy 每个回合都新建一个 ``AIAgent``，
计数器从 0 起累加，所以跑完读到的就是「这一回合（含工具循环里多次模型调用）的合计」，
不含历史回合 → 不重复计数。

**写入发生在父进程**（重要）：token 计数器只在被沙箱化的 AIAgent 子进程里，但子进程沙箱
策略不允许写 ``/var/log/hermes``。因此子进程只「读出」usage 并透传给父进程
（``aiagent_subprocess`` 把 usage 放进最终 JSON），父进程
（``agent_real._write_token_ledger_from_child``，非沙箱）才调用本模块落盘 —— 与
``conversation_audit`` 同样的「父进程写」规避沙箱。开关 ``HERMES_TOKEN_USAGE_LEDGER_ENABLED``
由父进程（gateway）读自身环境，无需进子进程 env 白名单。
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
    """从 AIAgent 实例安全读出本回合 token 累计与调用次数。属性缺失一律兜底 0，不抛。

    ``cache_read_tokens`` / ``cache_write_tokens`` 来自核心的
    ``session_cache_read_tokens`` / ``session_cache_write_tokens``（``run_agent.py`` 约
    751-752 定义），``api_calls`` 来自 ``_api_call_count``（``agent/agent_init.py`` 初始化、
    ``agent/conversation_loop.py`` 每次模型调用后更新）。这三项是判断「换模型是否划算」
    的判据：缓存读价只有输入价的 0.1x，换模型打掉前缀缓存的代价可能反超降级省下的差价；
    而每轮调用次数决定了「小模型多绕几轮反而更慢/更贵」能不能被观测到。
    核心版本不同可能缺这些属性（MT 测试环境与生产跑的不是同一条 core 线），故一律 getattr 兜底。
    """
    return {
        "input_tokens": _int(getattr(agent, "session_input_tokens", 0)),
        "output_tokens": _int(getattr(agent, "session_output_tokens", 0)),
        "total_tokens": _int(getattr(agent, "session_total_tokens", 0)),
        "cache_read_tokens": _int(getattr(agent, "session_cache_read_tokens", 0)),
        "cache_write_tokens": _int(getattr(agent, "session_cache_write_tokens", 0)),
        "api_calls": _int(getattr(agent, "_api_call_count", 0)),
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
    chat_id: str | None = None,
    timestamp: str | None = None,
    cache_read_tokens: Any = 0,
    cache_write_tokens: Any = 0,
    api_calls: Any = 0,
) -> None:
    """追加一行 token 台账。开关关 / token 非正 / 任何异常 → 静默 no-op。

    记录的是「原始事实」：sender / profile / chat_type / chat_id。归属策略（群聊→拉群
    owner、个人→本人）由每小时的 uploader 用路由表解析，热路径不做策略判断。``chat_id``
    用于 uploader 把群聊回合按 ``lookup_by_chat_id`` 反查 owner_open_id。
    """
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
        "chat_id": str(chat_id or ""),
        "model": str(model or ""),
        "input_tokens": it,
        "output_tokens": ot,
        "total_tokens": tt,
        "cache_read_tokens": _int(cache_read_tokens),
        "cache_write_tokens": _int(cache_write_tokens),
        "api_calls": _int(api_calls),
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
        # debug-only：台账是 best-effort 旁路，写失败绝不该污染用户可见的 ERROR 日志，
        # 更不能影响主回合。exc_info 保留堆栈供排障。
        logger.debug("[multitenancy] token usage ledger append failed", exc_info=True)


# ── 只读聚合（Done 线的"能算出来"那一半）────────────────────────────────────
# 埋点只是把数写下来；判断"分级路由到底省不省"要的是这两个比率。放在本模块里而不是
# 单开脚本，是因为字段契约就在上面几十行，改字段时一眼能看到消费侧。
#   python -m hermes_multitenancy.token_usage_ledger [--date YYYY-MM-DD] [--path ...]


def summarize_rows(rows: Any) -> dict[str, dict[str, float]]:
    """按 platform 聚合出「每轮平均模型调用次数」与「缓存命中率」。

    缓存命中率 = cache_read / (cache_read + input)：分母是"本可以按全价付的输入量"，
    所以这个比率直接就是"换模型会打掉多少便宜 token"的上界。分母为 0 时记 0.0 而非
    抛除零 —— 老行没有这些字段，值一律读作 0。
    """
    acc: dict[str, dict[str, float]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        bucket = acc.setdefault(
            str(row.get("platform") or "<unknown>"),
            {"turns": 0, "api_calls": 0, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "input_tokens": 0, "output_tokens": 0},
        )
        bucket["turns"] += 1
        for key in ("api_calls", "cache_read_tokens", "cache_write_tokens",
                    "input_tokens", "output_tokens"):
            bucket[key] += _int(row.get(key))

    out: dict[str, dict[str, float]] = {}
    for platform, b in acc.items():
        turns = b["turns"]
        cacheable = b["cache_read_tokens"] + b["input_tokens"]
        out[platform] = dict(
            b,
            calls_per_turn=(b["api_calls"] / turns) if turns else 0.0,
            cache_hit_rate=(b["cache_read_tokens"] / cacheable) if cacheable else 0.0,
        )
    return out


def iter_ledger_rows(path: Path, date_prefix: str = "") -> Any:
    """逐行读台账，坏行跳过（台账是 append-only 旁路，半行/脏行不该让统计整个失败）。"""
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(row, dict):
                continue
            if date_prefix and not str(row.get("ts") or "").startswith(date_prefix):
                continue
            yield row


def _main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m hermes_multitenancy.token_usage_ledger",
        description="只读聚合 token 台账：每轮平均模型调用次数 + 缓存命中率，按平台分组。",
    )
    ap.add_argument("--date", default="", help="只看某天，YYYY-MM-DD（默认全部）")
    ap.add_argument("--path", default="", help="台账路径（默认取 env / 内置默认）")
    args = ap.parse_args(argv)

    path = Path(args.path).expanduser() if args.path else token_usage_ledger_path()
    if not path.exists():
        print(f"台账不存在: {path}")
        return 1

    stats = summarize_rows(iter_ledger_rows(path, args.date))
    if not stats:
        print(f"{path} 在该范围内没有数据（--date={args.date or '全部'}）")
        return 1

    print(f"{path}  range={args.date or 'all'}")
    print(f"{'platform':<12}{'turns':>8}{'calls/turn':>12}{'cache_hit':>11}"
          f"{'input':>16}{'cache_read':>16}")
    for platform in sorted(stats, key=lambda k: -stats[k]["turns"]):
        s = stats[platform]
        print(f"{platform:<12}{int(s['turns']):>8}{s['calls_per_turn']:>12.1f}"
              f"{s['cache_hit_rate'] * 100:>10.1f}%{int(s['input_tokens']):>16,}"
              f"{int(s['cache_read_tokens']):>16,}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(_main())
