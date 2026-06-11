"""工件 1b：每小时把 token 台账聚合 + 解析成企业邮箱 + 上报到排行榜收集端。

跑在 hermes-1（systemd timer，见 deploy/hermes-token-uploader.{service,timer}）。

流程：
  1. 读 token 台账 jsonl（工件 1a 写的），筛「今天」（上海时区）的行。
  2. 按 (sender_open_id, model) 聚合 sum(input/output/total)。
  3. sender_open_id → {email, dept}：经 **feishu-sync**（hermes_multitenancy.sync.fetch_contact_directory，
     复用其租户 token，与花名册同一条认证路径）一次性拉全量通讯录目录，按天缓存到本地后逐人查。
     解析不到的 open_id → 跳过并计数（log，不静默吞、不污染他人）。
  4. POST 到收集端 `POST <COLLECTOR>/v1/usage/report`（工件 2 的 additive 端点），
     `{"source":"hermes","client":"Hermes","date":..., "records":[...]}`，Bearer 鉴权。

幂等：收集端按 (source, period, ...) DELETE+INSERT；本脚本每小时重算当天全量上报，
连跑同日不翻倍。失败重试下小时（重算全量自动补当天）。

测试友好：解析台账→当日聚合→拼上报体 全是纯函数（test_token_usage_uploader.py 覆盖）；
lark-cli 解析与 HTTP POST 在边缘，--dry-run 不联网、解析器可注入。

环境变量：
  HERMES_TOKEN_USAGE_LEDGER_PATH   台账路径（默认 /var/log/hermes/token-usage.jsonl）
  HERMES_TOKSCALE_COLLECTOR        收集端 base（默认 https://tokscale.gotokeep.com）
  HERMES_TOKSCALE_REPORT_KEY       上报 Bearer（必填，非 dry-run 时）
  HERMES_TOKEN_USAGE_RESOLVE_CACHE 通讯录目录缓存（默认 ~/.hermes/token-usage-resolve-cache.json）
  HERMES_HOME                      指向 feishu-sync 的 sync-root profile；FeishuContactClient
                                   .for_current_home 从该 profile 的 vault 取租户 app 凭据。
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


def aggregate_day(rows: Iterable[dict], day: str) -> dict[tuple[str, str], dict[str, int]]:
    """筛当天行，按 (sender_open_id, model) 聚合 token。返回 {(open_id, model): {in,out,total}}。"""
    agg: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    )
    for row in rows:
        if _line_date(str(row.get("ts") or "")) != day:
            continue
        open_id = str(row.get("sender_open_id") or "").strip()
        if not open_id:
            continue  # 无触发人身份，留给调用方计数
        model = str(row.get("model") or "unknown")
        bucket = agg[(open_id, model)]
        for k in ("input_tokens", "output_tokens", "total_tokens"):
            try:
                bucket[k] += int(row.get(k) or 0)
            except (TypeError, ValueError):
                pass
    return dict(agg)


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

class FeishuSyncResolver:
    """open_id → {email, dept}，经 feishu-sync 的 ``fetch_contact_directory``（复用其租户
    token，与花名册同一条认证路径）。一次拉全量目录、按天缓存到本地，之后逐人 O(1) 查。
    拉取失败时退回缓存；缓存也无则全部解析不到（调用方跳过计数）。返回 None = 未解析。"""

    def __init__(self, cache_path: Path, *, day: str) -> None:
        self.cache_path = cache_path
        self.day = day
        self._directory: dict[str, dict[str, str]] = {}
        self._ready = False

    def _load_cache(self) -> dict[str, dict[str, str]] | None:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if isinstance(data, dict) and data.get("day") == self.day:
            return {k: v for k, v in (data.get("map") or {}).items() if isinstance(v, dict)}
        return None

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps({"day": self.day, "map": self._directory}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _ensure_directory(self) -> None:
        if self._ready:
            return
        cached = self._load_cache()
        if cached is not None:
            self._directory = cached
            self._ready = True
            return
        try:
            from .sync import fetch_contact_directory

            self._directory = fetch_contact_directory()
            self._save_cache()
        except Exception as exc:  # network / scope / auth — degrade, don't crash the run
            print(f"[uploader] feishu-sync directory fetch failed: {exc}", file=sys.stderr)
            # fall back to any stale cache from a previous day so we still attribute.
            try:
                stale = json.loads(self.cache_path.read_text(encoding="utf-8"))
                self._directory = {k: v for k, v in (stale.get("map") or {}).items() if isinstance(v, dict)}
            except Exception:
                self._directory = {}
        self._ready = True

    def __call__(self, open_id: str) -> dict[str, str] | None:
        self._ensure_directory()
        return self._directory.get(open_id)


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

    cache_path = Path(
        os.getenv("HERMES_TOKEN_USAGE_RESOLVE_CACHE")
        or (Path.home() / ".hermes" / "token-usage-resolve-cache.json")
    ).expanduser()
    # One resolver shared across all days — the org directory is date-independent,
    # so a single feishu-sync pull (cached) serves the whole backfill.
    resolver = FeishuSyncResolver(cache_path, day=today_str())

    key = os.getenv("HERMES_TOKSCALE_REPORT_KEY") or ""
    collector = os.getenv("HERMES_TOKSCALE_COLLECTOR") or DEFAULT_COLLECTOR
    if not args.dry_run and not key:
        print("[uploader] ERROR: HERMES_TOKSCALE_REPORT_KEY unset", file=sys.stderr)
        return 2

    rc = 0
    for day in days:
        records, stats = build_records(aggregate_day(rows, day), resolver)
        print(f"[uploader] day={day} {stats}")
        if args.dry_run:
            print(json.dumps({"source": SOURCE, "client": CLIENT, "date": day, "records": records},
                             ensure_ascii=False, indent=2))
            continue
        if not records:
            continue  # nothing resolvable for this day; leave any existing snapshot intact
        try:
            resp = post_records(collector, key, day, records)
            print(f"[uploader] uploaded {len(records)} record(s) for {day}: {resp}")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            print(f"[uploader] POST failed for {day} (will retry next run): {exc}", file=sys.stderr)
            rc = 1  # keep going with other days; non-zero so the run is flagged
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
