#!/usr/bin/env bash
# hermes-backup.sh — hermes-1 每日本机备份（两层数据）
#
#   第一层 状态核心 (~126MB)：6 个 sqlite + config + 凭证。每日全量，保留 30 份。
#   第二层 用户数据 (~40GB)：profiles/。每日硬链增量快照，保留 7 份。
#
# 重要：本机备份不是灾备。它能救误删/误改/发布搞砸，救不了盘坏和机器炸。
#       异地那一步做完之前，不要把这套描述成“备份已完成”。
#
# 所有路径都可用环境变量覆盖，这样测试能在临时目录里跑真逻辑。
set -euo pipefail

HERMES_HOME_DIR="${HERMES_HOME_DIR:-$HOME/.hermes}"
HERMES_WEBUI_DIR="${HERMES_WEBUI_DIR:-$HOME/.hermes-web-ui}"
BACKUP_ROOT="${BACKUP_ROOT:-$HOME/backups/daily}"
MIN_FREE_GB="${MIN_FREE_GB:-30}"
KEEP_STATE="${KEEP_STATE:-30}"
KEEP_PROFILES="${KEEP_PROFILES:-7}"
SKIP_PROFILES="${SKIP_PROFILES:-0}"   # 测试用：跳过 40G 那一层
# 排除清单默认跟脚本放在一起（部署后是 deploy/hermes-backup-excludes.txt）
EXCLUDE_FILE="${EXCLUDE_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/hermes-backup-excludes.txt}"
LOCK_FILE="${LOCK_FILE:-$BACKUP_ROOT/.lock}"

TS="$(date +%Y%m%dT%H%M%S)"
STATE_ROOT="$BACKUP_ROOT/state"
PROFILES_ROOT="$BACKUP_ROOT/profiles"

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ── 单实例：备份自己不能并发跑 ───────────────────────────────────────
mkdir -p "$BACKUP_ROOT"
if command -v flock >/dev/null 2>&1; then
  # Linux(生产)走这条:进程被 kill 时内核自动释放,不会留死锁
  exec 9>"$LOCK_FILE"
  flock -n 9 || die "另一个备份正在运行（$LOCK_FILE 被占用），本次退出"
else
  # macOS 没有 flock(只在本地跑测试时走这条)。mkdir 是 POSIX 原子操作。
  # 加个陈旧锁自动清理,免得一次硬崩就让备份永久停摆 —— 静默停摆是备份系统最坏的失败模式。
  LOCK_DIR="$LOCK_FILE.d"
  if [ -d "$LOCK_DIR" ] && [ -z "$(find "$LOCK_DIR" -maxdepth 0 -mmin -360 2>/dev/null)" ]; then
    rm -rf "$LOCK_DIR"
  fi
  mkdir "$LOCK_DIR" 2>/dev/null || die "另一个备份正在运行（$LOCK_DIR 存在），本次退出"
  trap 'rm -rf "$LOCK_DIR"' EXIT
fi

# ── 磁盘前置检查:宁可不备,也不能把盘写满造成真故障 ──────────────────
# df -Pk 是 POSIX 的，Linux 和 macOS 都认 —— 测试才能在两边都跑
free_kb=$(df -Pk "$BACKUP_ROOT" | awk 'NR==2{print $4}')
[ -n "$free_kb" ] || die "无法读取 $BACKUP_ROOT 的可用空间"
free_gb=$((free_kb / 1024 / 1024))
if [ "$free_gb" -lt "$MIN_FREE_GB" ]; then
  die "可用空间 ${free_gb}G < 下限 ${MIN_FREE_GB}G —— 拒绝执行，未产生任何备份目录"
fi
log "磁盘检查通过：可用 ${free_gb}G ≥ ${MIN_FREE_GB}G"

# ── 第一层:状态核心 ─────────────────────────────────────────────────
# 先写 staging，全部校验通过才原子改名 —— 保证目录里不会出现半份备份。
STAGING="$STATE_ROOT/.staging-$TS"
rm -rf "$STAGING"
mkdir -p "$STAGING/db" "$STAGING/config"
# .env / auth.json / token 都在里面，目录必须自己就是 700
chmod 700 "$STAGING"

cleanup_staging() { rm -rf "$STAGING"; }
trap cleanup_staging ERR

DBS=(
  "$HERMES_HOME_DIR/multitenancy.db"
  "$HERMES_HOME_DIR/state.db"
  "$HERMES_HOME_DIR/kanban.db"
  "$HERMES_HOME_DIR/multitenancy_routing.db"
  "$HERMES_WEBUI_DIR/hermes-web-ui.db"
  "$HERMES_WEBUI_DIR/web-ui.db"
)

backed_up=0
for src in "${DBS[@]}"; do
  name="$(basename "$src")"
  if [ ! -f "$src" ]; then
    # 花括号是必须的:变量后紧跟中文时,老 bash(3.2,macOS 自带)会把多字节首字节
    # 当成变量名的一部分,报 unbound variable。生产的 bash 5 没这问题,但两边都要能跑。
    log "跳过 ${name}（不存在）"
    continue
  fi
  dst="$STAGING/db/$name"
  # .backup 是 sqlite 的在线备份：不阻塞写入者，且能正确带走 WAL 里的内容。
  # 绝不能用 cp —— WAL 模式下热拷贝会拷出损坏的库。
  # .timeout 让它在热写入下重试而不是直接放弃。
  sqlite3 -cmd ".timeout 60000" "$src" ".backup '$dst'" \
    || die "$name .backup 失败"

  # 副本落地时会继承 WAL 模式，于是旁边多出 -wal/-shm。实测主文件本身已自足，
  # 但把副本切回 delete 模式能 checkpoint 并删掉旁文件 —— 恢复时“只拷 .db”就绝不会漏数据，
  # 也不会有易变的旁文件混进 SHA256SUMS。
  sqlite3 "$dst" "pragma journal_mode=delete;" >/dev/null 2>&1 || true

  # 校验副本本身。未经校验的备份比没有备份更危险 —— 风险模型正压在它身上。
  chk="$(sqlite3 "$dst" "pragma integrity_check;" 2>&1 | head -1)"
  [ "$chk" = "ok" ] || die "$name 副本校验失败：$chk"
  # 副本已是 delete 模式，旁文件此刻只是打开时留下的空壳，删掉让备份目录里只剩纯 .db
  rm -f "$dst-wal" "$dst-shm"
  log "  $name integrity_check: ok"
  backed_up=$((backed_up + 1))
done
[ "$backed_up" -gt 0 ] || die "一个库都没备份到，检查 HERMES_HOME_DIR/HERMES_WEBUI_DIR"

# 配置与凭证（缺哪个都不算致命，逐个尽力拷）
for f in "$HERMES_HOME_DIR/config.yaml" "$HERMES_HOME_DIR/.env" "$HERMES_HOME_DIR/auth.json"; do
  [ -f "$f" ] && cp -p "$f" "$STAGING/config/" || true
done
[ -d "$HERMES_HOME_DIR/feishu_uat" ] && cp -rp "$HERMES_HOME_DIR/feishu_uat" "$STAGING/config/" || true

# MANIFEST + 校验和：演练时靠它判断“恢复出来的是不是当初那份”
{
  echo "backup_ts=$TS"
  echo "host=$(hostname)"
  echo "source_home=$HERMES_HOME_DIR"
  echo "source_webui=$HERMES_WEBUI_DIR"
  echo "db_count=$backed_up"
  echo "kind=local-only  # 本机备份，非灾备：异地未做"
  # 排除了什么必须写在案上。静默排除是备份系统最阴险的失败模式——
  # 等真要恢复时才发现某个目录从来没被备过。
  if [ -f "$EXCLUDE_FILE" ]; then
    echo "profiles_excludes=$(grep -v '^#' "$EXCLUDE_FILE" | grep -v '^$' | tr '\n' ',' | sed 's/,$//')"
  else
    echo "profiles_excludes=(none)"
  fi
  # 逐表行数。演练靠它判断“还原出来的和当初备下来的是不是一模一样”。
  # 注意不能拿它跟“当下的生产”比 —— 生产一直在写，几小时前的快照必然落后。
  # 能且只能要求：还原结果 == 备份时刻记录的行数，差 0。
  for d in "$STAGING"/db/*.db; do
    dbn="$(basename "$d")"
    echo "table_count ${dbn}=$(sqlite3 "$d" 'select count(*) from sqlite_master where type="table";')"
    sqlite3 "$d" "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name;" \
    | while read -r t; do
        [ -n "$t" ] || continue
        echo "rows ${dbn}.${t}=$(sqlite3 "$d" "select count(*) from \"$t\";")"
      done
  done
} > "$STAGING/MANIFEST.txt"
# Linux 是 sha256sum，macOS 只有 shasum。生产走前者，本地测试走后者。
if command -v sha256sum >/dev/null 2>&1; then SUM=(sha256sum); else SUM=(shasum -a 256); fi
( cd "$STAGING" && find . -type f ! -name SHA256SUMS -print0 | xargs -0 "${SUM[@]}" > SHA256SUMS )

trap - ERR
# 时间戳只精确到秒。同一秒内跑两次(手动触发撞上定时器、或发布前备份紧跟日备)时目标已存在，
# 而 `mv A B` 在 B 是已存在目录时会把 A 塞进 B 里，产生嵌套的烂摊子。加序号避开。
uniq_dest() {
  local base="$1" n=1 dest="$1"
  while [ -e "$dest" ]; do n=$((n + 1)); dest="${base}-${n}"; done
  printf '%s' "$dest"
}
STATE_DEST="$(uniq_dest "$STATE_ROOT/$TS")"
mv "$STAGING" "$STATE_DEST"
log "状态核心完成 → ${STATE_DEST}（${backed_up} 个库）"

# ── 第二层:profiles 硬链增量 ────────────────────────────────────────
# --link-dest 让没变过的文件在新快照里只是一个硬链接，不占额外空间。
# 首次全量约 40G，之后每天只增加变化量。
if [ "$SKIP_PROFILES" != "1" ] && [ -d "$HERMES_HOME_DIR/profiles" ]; then
  mkdir -p "$PROFILES_ROOT"
  prev="$(ls -1d "$PROFILES_ROOT"/*/ 2>/dev/null | sort | tail -1 || true)"
  link_arg=()
  [ -n "$prev" ] && link_arg=(--link-dest="${prev%/}")
  # 排除清单:只排"机器能重新生成"的(npm/pip 缓存、tmp、编译产物),员工文件一个不落。
  # 放在独立文件里,以后要调整不用改脚本。
  excl_arg=()
  [ -f "$EXCLUDE_FILE" ] && excl_arg=(--exclude-from="$EXCLUDE_FILE")

  # ${a[@]+"${a[@]}"} 而不是 "${a[@]}":bash<4.4(含 macOS 自带的 3.2)在 set -u 下
  # 会把空数组当成未绑定变量直接报错。生产 bash 5 无所谓,但测试要两边都能跑。
  # 备份进程读不到的文件会被 rsync 悄悄跳过 —— 这是备份系统最阴险的漏数据方式。
  # 这台机器上历史反复出现 root 跑出来的文件落在 profiles 里（2026-08-01 修过 276 个），
  # 所以每次都先数一遍，有就大声报出来，绝不让它静默通过。
  # `-readable` 是 GNU find 扩展（生产是 Linux，有）。macOS 的 BSD find 没有，
  # 本地跑测试时直接跳过这项检查，而不是让脚本报错。
  if find /dev/null -readable >/dev/null 2>&1; then
    unreadable=$(find "$HERMES_HOME_DIR/profiles" -type f ! -readable 2>/dev/null | wc -l | tr -d ' ')
  else
    unreadable=0
  fi
  if [ "$unreadable" -gt 0 ]; then
    log "⚠️  警告：profiles 下有 ${unreadable} 个文件当前用户读不到，它们不会进备份！"
    find "$HERMES_HOME_DIR/profiles" -type f ! -readable 2>/dev/null | head -5 | sed 's/^/      /'
    log "    修法：sudo chown -R hermes:hermes $HERMES_HOME_DIR/profiles"
  fi

  set +e
  rsync -a --delete ${link_arg[@]+"${link_arg[@]}"} ${excl_arg[@]+"${excl_arg[@]}"} \
    "$HERMES_HOME_DIR/profiles/" "$PROFILES_ROOT/.staging-$TS/"
  rc=$?
  set -e
  # rsync 退出码：0=全好；24=传输中有文件消失（agent 在跑，属正常）；其余都当失败。
  case "$rc" in
    0) ;;
    24) log "  （有文件在传输中消失，属正常：agent 一直在写）" ;;
    *) die "profiles 增量快照失败，rsync 退出码 $rc" ;;
  esac
  PROF_DEST="$(uniq_dest "$PROFILES_ROOT/$TS")"
  mv "$PROFILES_ROOT/.staging-$TS" "$PROF_DEST"
  log "profiles 快照完成 → ${PROF_DEST}$([ -n "$prev" ] && echo "（增量，基于 $(basename "${prev%/}")）" || echo "（首次全量）")"
fi

# ── 保留策略:安全机制不能自己把盘撑爆 ───────────────────────────────
prune() {
  local root="$1" keep="$2"
  [ -d "$root" ] || return 0
  local n; n=$(ls -1d "$root"/*/ 2>/dev/null | wc -l)
  if [ "$n" -gt "$keep" ]; then
    ls -1d "$root"/*/ | sort | head -n "$((n - keep))" | while read -r old; do
      rm -rf "$old"; log "  裁剪 $(basename "${old%/}")"
    done
  fi
}
prune "$STATE_ROOT" "$KEEP_STATE"
prune "$PROFILES_ROOT" "$KEEP_PROFILES"

log "备份完成。提醒：这是本机备份，不是灾备 —— 异地尚未建立。"
