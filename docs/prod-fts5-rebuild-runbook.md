# Runbook：生产 18 库 `messages_fts_trigram` 重建

> 单：`mt-prod-fts5-rebuild-18dbs`。脚本 `scripts/prod_fts5_rebuild.py`，测试 `tests/test_prod_fts5_rebuild.py`。
> 生产执行必须由 sunke 当场批准；本 runbook 只写步骤，不代表已批准。
> 所有命令都在 **bash** 下执行（依赖 `set -o pipefail` 与 `PIPESTATUS`）。

## 0. 这个脚本做什么、不做什么

- 只处理清单里列出的库（A13 基线文件格式：`#` 头行 + 每行一个 `{"path": ...}`），严格逐库串行；空清单直接拒绝（退出码 2）。
- 每库：`quick_check` + trigram `integrity-check`（rank=1，放在一个必然回滚的事务里）→ 健康就记 `skipped_healthy`，不写 →
  剩余空间 ≥ 3×(库+WAL) → 5 秒写延迟门 → SQLite online backup 快照（`O_EXCL` 新建文件，每步 256 页、步间 sleep 50ms、
  整体期限 30 分钟）+ 快照 `quick_check` + 快照上的搜索命中测量（不占原库写锁）→
  - `--dry-run`：在快照上的事务里跑与 apply 相同的临界区（行 hash → 重建 → 复测 → 行 hash），记
    `critical_section_seconds_on_snapshot`，然后 ROLLBACK（快照保持原样，原库只读打开）；
  - `--apply`：core `fts_rebuild_admission` 锁 → `BEGIN IMMEDIATE` → 行 hash → 一条
    `INSERT INTO messages_fts_trigram(messages_fts_trigram) VALUES('rebuild')` → 事务内复测 quick_check / integrity-check /
    messages、sessions 全列 hash → 一致才 COMMIT，否则 ROLLBACK 并整轮停止；
    COMMIT 后新连接只复测健康（员工提交后写入是正常的，不再比行数）。**提交后复测失败不会自动写回快照**
    （在线写回会吞掉并发写入），记 `failed_needs_manual`，整轮停止，按第 6 节人工处理。
- 库里有 `messages_fts_cjk` 时，所有连接只读侧加载 core 的 `cjk_unicode61` 扩展（默认取 core
  `fts5_cjk_so_path()`，即 `$HERMES_HOME/lib/libfts5_cjk.so`，可 `--cjk-so` 指定）；加载不了就记 `failed_cjk_tokenizer`，不碰该库。
- 不删任何文件，不 VACUUM，不 drop/recreate，不碰 `messages_fts` / `messages_fts_cjk`。
- 写延迟判据同 `A-stage-ops/io_guard.py`：`/sys/block/<dev>/stat` 5 秒窗口，写延迟 ≥100ms 或
  `hermes-feishu-sync.service` 为 active/activating 视为忙；窗口内 0 次写只有在 in_flight=0（设备确实空闲）时才算安静，
  否则算忙。忙就暂停，连续 3 次安静才继续；每次采样后都检查累计暂停，>30 分钟退出码 3，剩余库记 `aborted_io`。
- `--ledger` 不能指向清单、任何库及其 `-wal`/`-shm`/锁文件、`.db` 或快照文件；已存在的 ledger 必须是 JSONL，否则拒绝（退出码 2）。

### ledger 状态

| status | 含义 | 是否写过原库 |
|---|---|---|
| `rebuilt` | 重建成功，事务内与提交后两次复测都通过 | 是（仅 trigram 索引） |
| `skipped_healthy` | quick_check 与 integrity-check 都 ok | 否 |
| `would_rebuild` / `would_skip_healthy` | dry-run 结论（在快照上） | 否 |
| `would_fail` | dry-run 在快照上重建后仍不健康；整轮停止 | 否 |
| `deferred_lock` | core 重建锁被别的进程持有（fail closed） | 否 |
| `deferred_busy` | 10 秒内拿不到写锁 | 否 |
| `skipped_missing` / `skipped_no_trigram` | 路径不存在 / 库里没有 trigram 表；整轮停止 | 否 |
| `failed_cjk_tokenizer` | 有 cjk 索引但 tokenizer 扩展加载不了；整轮停止 | 否 |
| `failed_snapshot` | 快照超时、快照 quick_check 与原库不一致或快照阶段异常；整轮停止 | 否 |
| `failed_verify` / `failed_rebuild` / `failed_<阶段>` | 事务内复测不一致、重建报错或提交前异常，已 ROLLBACK；整轮停止 | 否（已回滚） |
| `failed_needs_manual` | 已 COMMIT，之后复测失败或异常；整轮停止 | 是，需人工看 |
| `failed_space` | 快照空间不足；整轮停止 | 否 |
| `aborted_io` / `not_started` | IO 暂停超上限 / 前面停止导致未处理 | 否 |

每行带 `stage` 与 `committed`，异常也会落 ledger，最后一行总是 `kind=summary` 与退出码。
退出码：0 全部成功；2 用法错误、解释器不是批准的 core venv、IO 门不可用、ledger 路径不安全或空清单；
3 IO 暂停超上限；4 失败停止（含上表所有非成功状态）；5 只剩 `deferred_*`，需要重跑。

## 1. 前置核对（全部只读）

以下命令在 hermes-1 上以 root 执行；`H` 前缀让命令以 hermes 身份、带 user bus 运行（`systemctl --user` 需要）。

```bash
set -o pipefail
H='runuser -u hermes -- env HERMES_HOME=/home/hermes/.hermes XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus'
REL=/home/hermes/releases/hermes-agent-v0214-2082ff0c17
PY_A=$REL/.venv/bin/python                          # A13 头行记录的解释器
PY_B=$REL/.hermes-runtime/python/bin/python3.11     # 交接 §3.4 A4 记录的解释器
IN=/home/hermes/backups/fts5-rebuild-input
```

1. **解释器**：两种说法都查。

   ```bash
   ls -l $PY_A $PY_B; readlink -f $PY_A $PY_B
   for PY in $PY_A $PY_B; do $H $PY -c 'import sys,sqlite3; print(sys.executable, sys.prefix, sqlite3.sqlite_version)
   try:
       import hermes_state_common as h; print("core", h.__file__, hasattr(h, "fts_rebuild_admission"))
   except Exception as e: print("no-core", e)'; done
   for pid in $(pgrep -u hermes -f 'hermes.*gateway' | head -3); do readlink -f /proc/$pid/exe; tr '\0' ' ' < /proc/$pid/cmdline; echo; done
   ```

   判定：`--apply` 只接受 `sys.prefix` = `$REL/.venv`、`hermes_state_common` 来自 `$REL`、SQLite = 3.53.1 的解释器
   （脚本内置校验，不符退出码 2；换 release 时用 `--core-release` / `--require-sqlite` 显式传入）。
   `.venv/bin/python` 一般是指向 `.hermes-runtime/python/bin/python3.11` 的符号链接，realpath 相同，但只有经
   `.venv/bin/python` 启动时 `sys.prefix` 才是 `.venv`。取打印 `core $REL/hermes_state_common.py True`、`sqlite 3.53.1`、
   prefix 为 `.venv` 的那个作为 `PY`；它应与 gateway `cmdline` 第一段一致。A13、dry-run、apply、复扫都用同一个 `PY`。

2. **CJK 扩展**：`$H $PY -c 'from hermes_state_fts import fts5_cjk_so_path as f; print(f())'` 打印的文件存在，
   或者确认 18 库都没有 `messages_fts_cjk`（dry-run ledger 的 `table.cjk_index`）。
3. **脚本与清单**：把本单合入 main 的 `scripts/prod_fts5_rebuild.py` 和本机的 `A13-fts-baseline.txt`
   放到 `$IN/`（hermes 属主），两边 `sha256sum` 一致；`grep -c '"path"' $IN/A13-fts-baseline.txt` = 18。
4. **盘**：`cat /sys/block/vda/queue/wbt_lat_usec` 必须是 `0`（否则按 hermes-1 wbt 止血配方先处理）；`df -h /home` 余量 ≥ 10G。
5. **时间**：避开整点前后 5 分钟（`hermes-feishu-sync` 在跑时脚本会自己暂停）。
6. **写延迟基线**：`for i in 1 2 3; do a=$(awk '{print $5,$8}' /sys/block/vda/stat); sleep 5; b=$(awk '{print $5,$8,$9}' /sys/block/vda/stat); echo "$a $b" | awk '{w=$3-$1; print (w? ($4-$2)/w " ms/write" : "no writes, in_flight=" $5)}'; done`，三次都 <100。

## 2. dry-run（原库零写入）

```bash
SNAP=/home/hermes/backups/fts5-rebuild-$(date -u +%Y%m%dT%H%M%SZ)-dry
$H $PY $IN/prod_fts5_rebuild.py --list $IN/A13-fts-baseline.txt --snapshot-dir $SNAP --dry-run 2>&1 | tee $SNAP.log
rc=${PIPESTATUS[0]}; echo "exit=$rc"
```

`exit=0` 才继续。看 ledger：

```bash
$H $PY - $SNAP/ledger.jsonl <<'EOF'
import json, sys
for r in map(json.loads, open(sys.argv[1])):
    if "status" not in r: continue
    probe = [(p.get("match", "ERR"), p["like"]) for p in r.get("search_before", [])]
    print(r["status"], r["db"].split("/")[-2], "critical=%s" % r.get("critical_section_seconds_on_snapshot"),
          "rebuild=%s" % r.get("rebuild_seconds_on_snapshot"), "cjk=%s" % r.get("table", {}).get("cjk_index"),
          r.get("snapshot", {}).get("bytes"), probe)
EOF
```

- 18 行都是 `would_rebuild` 或 `would_skip_healthy`；
- `search_before` 的 `match`/`like` 差异回答「坏索引对搜索的实际影响」（`ERR` = 搜索直接报错）；
- 记下每库 `critical_section_seconds_on_snapshot`：这是 apply 时原库写锁的主要持有时长（行 hash ×2 + 重建 + quick_check +
  integrity-check），机械盘上的原库只会更慢，按 2 倍估。

dry-run 的快照目录保留，不删。

## 3. 决定在线还是停服（sunke 当场选）

- **在线**：只有 `2 × critical_section_seconds_on_snapshot ≤ 5 秒` 的库放在线清单。写锁持有期间，该员工 gateway 的写入会等待；
  等待超过它自己的 busy_timeout 就会报错，脚本无法替它兜底，所以在线预算按保守值取。
- **停服窗口**：其余库（预期含 `fengshiyi`，88MB）放进窗口清单：
  1. 拆清单：`grep -v -e fengshiyi $IN/A13-fts-baseline.txt > $IN/online.txt`；
     `grep -e '^#' -e fengshiyi $IN/A13-fts-baseline.txt > $IN/window.txt`（按 dry-run 结果调整名单）。
  2. 停写入方：`$H systemctl --user list-units 'hermes*' --no-legend` 列出服务，停掉会写这些库的服务
     （router gateway、专家 gateway、WebUI 后端，按当时列表逐个 `$H systemctl --user stop <unit>`，停哪些由 sunke 当场确认）。
  3. 确认没有进程还开着这些库：`for d in $(grep -o '"path": "[^"]*"' $IN/window.txt | cut -d'"' -f4); do fuser "$d" "$d-wal" 2>/dev/null; done`
     输出为空才继续。
  4. 跑第 4 节 apply（`--list $IN/window.txt`）。
  5. 恢复服务：按第 2 步的列表 `$H systemctl --user start <unit>`，再 `$H systemctl --user is-active <unit>` 逐个确认 `active`。

## 4. apply

```bash
SNAP=/home/hermes/backups/fts5-rebuild-$(date -u +%Y%m%dT%H%M%SZ)
$H $PY $IN/prod_fts5_rebuild.py --list $IN/online.txt --snapshot-dir $SNAP --apply 2>&1 | tee $SNAP.log
rc=${PIPESTATUS[0]}; echo "exit=$rc"
```

- `exit=0`：全部 `rebuilt` / `skipped_healthy`。
- `exit=5`（只剩 `deferred_*`）：过几分钟用同一命令、新 `SNAP` 重跑；已修好的库会记 `skipped_healthy`。
- `exit=3`：IO 一直忙，换时间重跑。
- `exit=4`：停下，看 ledger 最后一个非成功行的 `status`/`stage`/`committed`/`error`，不要重跑，先找 sunke。
- `exit=2`：前置条件不满足（stderr 有原因），什么都没写。

## 5. 复扫（完成线的生产部分）

用**同一个 `PY`**，对 18 库只读 quick_check + 回滚式 integrity-check + 行数：

```bash
$H $PY - $IN/A13-fts-baseline.txt <<'EOF'
import sys; sys.path.insert(0, "/home/hermes/backups/fts5-rebuild-input")
from pathlib import Path
import prod_fts5_rebuild as m
_, dbs = m.read_list(Path(sys.argv[1]))
c = m.Connector(m.default_cjk_so(), 10_000)
bad = 0
for db in dbs:
    ro = c.open(db, "ro"); qc = m.quick_check(ro); fp = m.fingerprint(ro); ro.close()
    rw = c.open(db, "rw"); h = m.health(rw, in_txn=False); rw.close()
    bad += not h["healthy"] or qc != ["ok"]
    print(db.parent.name, qc, h["integrity"], fp["messages"]["rows"], fp["sessions"]["rows"])
print("total", len(dbs), "bad", bad)
EOF
```

通过条件：`total 18 bad 0`；apply ledger 18 行都是 `rebuilt` 或 `skipped_healthy`，0 行 `failed_*`；
行数不少于 apply ledger 里的 `rows`（员工可能新增消息）。结果写回交接文档 §9-E 与风险表第 42 条。

## 6. 人工回退（单库，仅停服下做）

脚本不会在线自动写回。确需回退某个库（例如 `failed_needs_manual` 且判断索引写坏了）：

1. 按第 3 节第 2–3 步停掉会写该库的所有服务，并用 `fuser` 确认没有进程打开该库。
2. 先给当前库再做一次快照（新文件名，不覆盖任何旧快照）：

   ```bash
   P=/home/hermes/.hermes/profiles/<p>/state.db
   $H $PY -c 'import sys,sqlite3; from pathlib import Path; u=lambda p,m: Path(p).absolute().as_uri()+"?mode="+m
   s=sqlite3.connect(u(sys.argv[1],"ro"),uri=True); d=sqlite3.connect(sys.argv[2]); s.backup(d,pages=256); d.close(); s.close()' \
     "$P" "$SNAP/<p>.before-rollback.$(date -u +%H%M%S).db"
   ```

3. 核实回退范围：对比要写回的快照与当前库的 `fingerprint`（行数、hash）。快照之后的新消息会随写回丢失；有差异先找 sunke 决定。
4. 用 SQLite backup API 写回（WAL/SHM 由 SQLite 自己处理，不要 `cp` 覆盖 `state.db`，也不要动 `-wal`/`-shm`）：

   ```bash
   $H $PY -c 'import sys,sqlite3; from pathlib import Path; u=lambda p,m: Path(p).absolute().as_uri()+"?mode="+m
   s=sqlite3.connect(u(sys.argv[1],"ro"),uri=True); d=sqlite3.connect(u(sys.argv[2],"rw"),uri=True,timeout=10); s.backup(d,pages=256); d.close(); s.close()' \
     "$SNAP/<快照文件名>.snapshot.db" "$P"
   ```

5. 按第 3 节第 5 步恢复服务。

## 7. 7 天后复扫

用同一个 `PY` 跑第 5 节的复扫脚本。再出现 `integrity-check` 不通过（quick_check 可能仍是 ok），说明 v23 外部内容
trigram 的 delete/update 触发器按「当前」session 条件删索引造成了内容漂移，另开单查触发器，不要直接再跑本脚本了事。
