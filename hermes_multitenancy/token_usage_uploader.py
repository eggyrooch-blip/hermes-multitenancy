"""工件 1b：每小时把 token 台账聚合 + 解析成企业邮箱 + 上报到排行榜收集端。

跑在 hermes-1（systemd timer，见 deploy/hermes-token-uploader.{service,timer}）。

归属模型（sunke 2026-06-12）：**agent 属于谁，消耗就是谁的**，按 owner 记账，不按
每条消息的发送人。群聊里别人 @ 我拉进去的 bot 也算我的消耗。

流程：
  1. 读 token 台账 jsonl（工件 1a 写的），筛「今天」（上海时区）的行。
  2. 每行解析「归属人 open_id」（make_owner_resolver + 路由表）：
       群聊(chat_type=group) → lookup_by_chat_id(chat_id).owner_open_id（拉群的人）；
       个人/DM → sender 本人；sender 空(如 webui ingest) → profile 的 owner。
       解析不到（如路由表没这群）→ 丢弃，绝不把全群量硬安给某人（防误记护栏）。
  3. 按 (owner_open_id, model) 聚合 sum(input/output/total)。
  4. owner_open_id → {email, dept}：**纯路由表**（lookup_by_open_id → user_id），
     email = ``<user_id>@<域名>`` —— user_id 即 LDAP 即全公司排行榜的统一身份键
     （已在 prod 用现有数据验证 sunke→sunke@keep.com 精确匹配），故 **不需要飞书
     email scope**。合成/占位身份(ou_* / 空)解析不到 → 跳过并计数（不污染他人）。
  5. POST 到收集端 `POST <COLLECTOR>/v1/usage/report`（工件 2 的 additive 端点），
     `{"source":"hermes","client":"Hermes","date":..., "records":[...]}`，Bearer 鉴权。

幂等：收集端按 (source, period, ...) DELETE+INSERT；本脚本每小时重算当天全量上报，
连跑同日不翻倍。失败重试下小时（重算全量自动补当天）。

环境变量：
  HERMES_TOKEN_USAGE_LEDGER_PATH    台账路径（默认 /var/log/hermes/token-usage.jsonl）
  HERMES_TOKSCALE_COLLECTOR         收集端 base（默认 https://tokscale.gotokeep.com）
  HERMES_TOKSCALE_REPORT_KEY        上报 Bearer（必填，非 dry-run 时）
  HERMES_TOKEN_USAGE_EMAIL_DOMAIN   邮箱域名（必填，非 dry-run；如 keep.com）
  HERMES_MULTITENANCY_DB            路由表路径（默认 ~/.hermes/multitenancy.db）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

_SHANGHAI_TZ = timezone(timedelta(hours=8))
DEFAULT_LEDGER_PATH = Path("/var/log/hermes/token-usage.jsonl")
DEFAULT_COLLECTOR = "https://tokscale.gotokeep.com"
SOURCE = "hermes"
CLIENT = "Hermes"
# Feishu multi-person chat types — both bill to the group owner (mirror router._GROUP_CHAT_TYPES).
GROUP_CHAT_TYPES = frozenset({"group", "topic"})


# --------------------------------------------------------------------------- 纯逻辑（可测）

def today_str(now: datetime | None = None) -> str:
    return (now or datetime.now(tz=_SHANGHAI_TZ)).astimezone(_SHANGHAI_TZ).strftime("%Y-%m-%d")


def _line_date(ts: str) -> str:
    """台账行的 ts(上海 ISO) → YYYY-MM-DD。解析失败返回 ''。"""
    try:
        text = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        return datetime.fromisoformat(text).astimezone(_SHANGHAI_TZ).strftime("%Y-%m-%d")
    except Exception:
        return ""


def iter_ledger_rows(text: str) -> Iterable[dict]:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(row, dict):
            yield row


def distinct_dates(rows: Iterable[dict]) -> list[str]:
    """台账里出现过的全部日期（上海时区 YYYY-MM-DD），升序。用于 --backfill 全量回写。"""
    days = {d for d in (_line_date(str(r.get("ts") or "")) for r in rows) if d}
    return sorted(days)


def aggregate_day(
    rows: Iterable[dict],
    day: str,
    resolve_owner: Callable[[dict], str | None],
) -> dict[tuple[str, str], dict[str, int]]:
    """筛当天行，按 (owner_open_id, model) 聚合 token。

    归属身份由 ``resolve_owner(row)`` 决定（群聊→拉群 owner、个人→本人；见
    ``make_owner_resolver``）。返回 None 的行（无法可靠归属，如路由表查不到该群）被丢弃，
    绝不硬安给某人。返回 {(owner_open_id, model): {in,out,total}}。
    """
    agg: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    )
    for row in rows:
        if _line_date(str(row.get("ts") or "")) != day:
            continue
        owner = resolve_owner(row)
        if not owner:
            continue  # 无法可靠归属（空 sender 且查不到 profile/群 owner）→ 丢弃，不误记
        model = str(row.get("model") or "unknown")
        bucket = agg[(owner, model)]
        for k in ("input_tokens", "output_tokens", "total_tokens"):
            try:
                bucket[k] += int(row.get(k) or 0)
            except (TypeError, ValueError):
                pass
    return dict(agg)


def make_owner_resolver(
    lookup_group_owner: Callable[[str], str | None],
    lookup_profile_owner: Callable[[str], str | None],
) -> Callable[[dict], str | None]:
    """构造「台账行 → 归属人 open_id」解析器（纯逻辑，路由查询通过两个注入回调）。

    归属策略（sunke 2026-06-12）：agent 属于谁，消耗就是谁的。
      - 群聊（chat_type=='group'）→ 拉群的 owner：用 chat_id 反查群 owner_open_id。
        查不到该群 → None（丢弃，绝不把全群量硬安给某人 = 防误记的护栏）。
      - 个人/DM/webui → 发送人本人(sender_open_id)；若 sender 为空（如 webui ingest
        服务身份）→ 退而用 profile 的 owner（profile 名即 user_id，路由表有 owner）。
        都拿不到 → None（丢弃）。
    """
    def resolve(row: dict) -> str | None:
        chat_type = str(row.get("chat_type") or "").strip().lower()
        if chat_type in GROUP_CHAT_TYPES:  # 'group' or 'topic' — both → group owner
            chat_id = str(row.get("chat_id") or "").strip()
            if not chat_id:
                return None
            return lookup_group_owner(chat_id) or None
        sender = str(row.get("sender_open_id") or "").strip()
        if sender:
            return sender
        profile = str(row.get("profile") or "").strip()
        if profile:
            return lookup_profile_owner(profile) or None
        return None
    return resolve


def build_records(
    agg: dict[tuple[str, str], dict[str, int]],
    resolve: Callable[[str], dict[str, str] | None],
) -> tuple[list[dict], dict[str, int]]:
    """聚合 + 解析 → 上报 records。返回 (records, stats)。解析不到的 open_id 跳过并计数。"""
    records: list[dict] = []
    skipped_open_ids: set[str] = set()
    for (open_id, model), tok in sorted(agg.items()):
        ident = resolve(open_id)
        if not ident or not ident.get("email"):
            skipped_open_ids.add(open_id)
            continue
        records.append({
            "email": ident["email"],
            "dept": ident.get("dept") or "unknown",
            "provider": "",
            "model": model,
            "input_tokens": tok["input_tokens"],
            "output_tokens": tok["output_tokens"],
            "total_tokens": tok["total_tokens"] or (tok["input_tokens"] + tok["output_tokens"]),
        })
    stats = {
        "people_models": len(agg),
        "records": len(records),
        "skipped_open_ids": len(skipped_open_ids),
    }
    return records, stats


# --------------------------------------------------------------------------- 边缘 I/O

class RoutingOwnerLookup:
    """从 multitenancy 路由表解析归属人 open_id（群→拉群 owner、profile→owner）。

    复用 hermes_multitenancy.routing.RoutingTable（默认 ~/.hermes/multitenancy.db）。
    打不开路由表时所有查询返回 None（调用方丢弃，不误记）。给 make_owner_resolver 用。
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._table = None
        try:
            from .routing import RoutingTable

            self._table = RoutingTable(db_path) if db_path else RoutingTable()
        except Exception as exc:
            print(f"[uploader] routing table open failed: {exc}", file=sys.stderr)

    def group_owner(self, chat_id: str) -> str | None:
        if self._table is None:
            return None
        try:
            row = self._table.lookup_by_chat_id(chat_id)
        except Exception:
            return None
        return (getattr(row, "owner_open_id", None) or None) if row else None

    def profile_owner(self, profile_name: str) -> str | None:
        if self._table is None:
            return None
        try:
            row = self._table.lookup_by_profile_name(profile_name)
        except Exception:
            return None
        if not row:
            return None
        # user 行 owner_open_id 可能为空 → 退用 open_id 本人。
        return getattr(row, "owner_open_id", None) or getattr(row, "open_id", None) or None

    def email_dept_for_open_id(self, open_id: str, domain: str) -> dict[str, str] | None:
        """归属人 open_id → {email, dept}。

        email = ``<user_id>@<domain>`` —— user_id 即 LDAP 即全公司排行榜的统一身份键
        （已在 prod 用现有数据验证 sunke→sunke@keep.com 精确匹配），故无需飞书 email scope。
        合成/占位 user_id（``ou_*`` 或空）无法可靠归属 → 返回 None（调用方跳过计数）。
        dept 取路由表 display_label（次要；看板按 email JOIN people 表补全部门）。
        """
        if self._table is None:
            return None
        # 一个 open_id 在路由表可能有多行 kind='user'（sync 根 + 自动 provision 的合成兄弟，
        # 后者 user_id=ou_*）。优先 resolve_owner_root（只取 provenance='sync' 的唯一根），
        # 拿到真 LDAP user_id；非 sync 环境再退 lookup_by_open_id。取到合成/空 user_id → None。
        row = None
        for getter in ("resolve_owner_root", "lookup_by_open_id"):
            try:
                row = getattr(self._table, getter)(open_id)
            except Exception:
                row = None
            user_id = (getattr(row, "user_id", "") or "").strip() if row else ""
            if user_id and not user_id.startswith("ou_"):
                return {"email": f"{user_id}@{domain}",
                        "dept": str(getattr(row, "display_label", "") or "unknown")}
        return None  # 合成/占位身份或查不到，无法可靠映射成员工邮箱 → 跳过计数


def post_records(collector: str, key: str, day: str, records: list[dict]) -> dict:
    payload = json.dumps({
        "source": SOURCE, "client": CLIENT, "date": day, "records": records,
    }).encode("utf-8")
    req = urllib.request.Request(
        collector.rstrip("/") + "/v1/usage/report",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except Exception:
        return {"raw": body}


# --------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Upload Hermes token usage to the leaderboard collector.")
    ap.add_argument("--dry-run", action="store_true", help="只打印归属汇总与上报体，不联网")
    ap.add_argument("--date", default=None, help="覆盖统计日期 YYYY-MM-DD（默认今天/上海时区）")
    ap.add_argument("--backfill", action="store_true",
                    help="首次初始化：回写台账里所有日期（不只今天）。端点会从所有 day 行重算 month/lifetime。")
    args = ap.parse_args(argv)

    ledger = Path(os.getenv("HERMES_TOKEN_USAGE_LEDGER_PATH") or DEFAULT_LEDGER_PATH).expanduser()
    if not ledger.exists():
        print(f"[uploader] ledger absent: {ledger} (nothing to do)")
        return 0

    rows = list(iter_ledger_rows(ledger.read_text(encoding="utf-8")))
    if args.backfill:
        days = distinct_dates(rows)
        print(f"[uploader] backfill: {len(days)} day(s) in ledger: {days[:3]}{'...' if len(days) > 3 else ''}")
    else:
        days = [args.date or today_str()]
    if not days:
        print("[uploader] ledger has no dated rows; nothing to do")
        return 0

    # Owner attribution + email, both from the routing table (no Feishu email scope
    # needed — see email_dept_for_open_id). group -> inviter (lookup_by_chat_id),
    # DM -> sender, empty sender -> profile owner; then owner open_id -> user_id ->
    # <user_id>@<domain>, the company-wide leaderboard identity key.
    routing = RoutingOwnerLookup(os.getenv("HERMES_MULTITENANCY_DB") or None)
    owner_of = make_owner_resolver(routing.group_owner, routing.profile_owner)
    email_domain = (os.getenv("HERMES_TOKEN_USAGE_EMAIL_DOMAIN") or "").strip()
    if not args.dry_run and not email_domain:
        print("[uploader] ERROR: HERMES_TOKEN_USAGE_EMAIL_DOMAIN unset (e.g. keep.com)", file=sys.stderr)
        return 2
    email_domain = email_domain or "example.com"  # dry-run placeholder
    resolver = lambda open_id: routing.email_dept_for_open_id(open_id, email_domain)

    key = os.getenv("HERMES_TOKSCALE_REPORT_KEY") or ""
    collector = os.getenv("HERMES_TOKSCALE_COLLECTOR") or DEFAULT_COLLECTOR
    if not args.dry_run and not key:
        print("[uploader] ERROR: HERMES_TOKSCALE_REPORT_KEY unset", file=sys.stderr)
        return 2

    rc = 0
    for day in days:
        records, stats = build_records(aggregate_day(rows, day, owner_of), resolver)
        print(f"[uploader] day={day} {stats}")
        if args.dry_run:
            print(json.dumps({"source": SOURCE, "client": CLIENT, "date": day, "records": records},
                             ensure_ascii=False, indent=2))
            continue
        if not records:
            # 该日无可解析记录 → 不发空快照。这是有意的安全选择，不是契约漂移：
            # 台账是 append-only、日内只增不减，故「无记录」只可能是该日真没用量或全员
            # 解析不到（如 feishu-sync 临时失败）。发空快照会让端点 DELETE 掉上次的好数据；
            # 宁可保留旧快照、等下次 resolution 恢复后重算覆盖。真·清空交由端点 records=[] 路径。
            continue
        try:
            resp = post_records(collector, key, day, records)
            print(f"[uploader] uploaded {len(records)} record(s) for {day}: {resp}")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            print(f"[uploader] POST failed for {day} (will retry next run): {exc}", file=sys.stderr)
            rc = 1  # keep going with other days; non-zero so the run is flagged
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
