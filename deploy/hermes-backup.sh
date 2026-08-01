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
# 首次全量 profiles 需要额外空间（生产实测排除缓存后约 18G，留余量按 25G 记）。
# 有上一份快照时走硬链增量，只需要变化量，不再吃这一笔。
FIRST_FULL_GB="${FIRST_FULL_GB:-25}"
# profiles 下有读不到的文件时是否允许继续。默认不允许 —— 备份静默漏数据是最坏的失败模式。
ALLOW_UNREADABLE="${ALLOW_UNREADABLE:-0}"
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
  # 注意：这里不单独挂 EXIT trap —— 下面 cleanup_all 是唯一的 EXIT 处理者，
  # 多挂一次会把前一个覆盖掉（bash 的 trap 是覆盖不是叠加）。
fi

# ── 磁盘前置检查:宁可不备,也不能把盘写满造成真故障 ──────────────────
# df -Pk 是 POSIX 的，Linux 和 macOS 都认 —— 测试才能在两边都跑
free_kb=$(df -Pk "$BACKUP_ROOT" | awk 'NR==2{print $4}')
[ -n "$free_kb" ] || die "无法读取 $BACKUP_ROOT 的可用空间"
free_gb=$((free_kb / 1024 / 1024))
# 门槛要随本次真正会写多少而变：首次 profiles 全量要吃掉一整份（~18G 实测），
# 之后走硬链增量只写变化量。用固定 30G 门槛去挡一次 25G 的写入是挡不住的。
need_gb="$MIN_FREE_GB"
if [ "$SKIP_PROFILES" != "1" ] && [ -d "$HERMES_HOME_DIR/profiles" ] \
   && [ -z "$(ls -1d "$PROFILES_ROOT"/*/ 2>/dev/null | head -1)" ]; then
  need_gb=$((MIN_FREE_GB + FIRST_FULL_GB))
  log "本次是 profiles 首次全量，磁盘门槛提高到 ${need_gb}G"
fi
if [ "$free_gb" -lt "$need_gb" ]; then
  die "可用空间 ${free_gb}G < 下限 ${need_gb}G —— 拒绝执行，未产生任何备份目录"
fi
log "磁盘检查通过：可用 ${free_gb}G ≥ ${need_gb}G"

# ── 第一层:状态核心 ─────────────────────────────────────────────────
# 先写 staging，全部校验通过才原子改名 —— 保证目录里不会出现半份备份。
STAGING="$STATE_ROOT/.staging-$TS"
rm -rf "$STAGING"
mkdir -p "$STAGING/db" "$STAGING/config"
# .env / auth.json / token 都在里面，目录必须自己就是 700
chmod 700 "$STAGING"

# staging 必须在任何退出路径上清干净：报错、被 kill、被 systemd 停都算。
# 只挂 ERR 的话，SIGTERM/SIGINT 会留下半份 staging，下次跑还可能把它当成"上一份"。
# 两层各有各的 staging，都要收。
STAGING_PROF=""
cleanup_all() {
  rm -rf "${STAGING:-}" ${STAGING_PROF:+"$STAGING_PROF"}
  [ -n "${LOCK_DIR:-}" ] && rm -rf "$LOCK_DIR"
  return 0
}
trap cleanup_all EXIT
trap 'cleanup_all; exit 143' TERM
trap 'cleanup_all; exit 130' INT

DBS=(
  "$HERMES_HOME_DIR/multitenancy.db"
  "$HERMES_HOME_DIR/state.db"
  "$HERMES_HOME_DIR/kanban.db"
  "$HERMES_HOME_DIR/multitenancy_routing.db"
  "$HERMES_WEBUI_DIR/hermes-web-ui.db"
  "$HERMES_WEBUI_DIR/web-ui.db"
)

backed_up=0
missing=""
empty=""
for src in "${DBS[@]}"; do
  name="$(basename "$src")"
  if [ ! -f "$src" ]; then
    # 这 6 个库是写死的已知清单，缺一个就是异常，不能像"可选文件"那样悄悄跳过。
    # 记进 MANIFEST 并在结尾大声报，让人能看见"这次没备到什么"。
    # 花括号是必须的:变量后紧跟中文时,老 bash(3.2,macOS 自带)会把多字节首字节
    # 当成变量名的一部分,报 unbound variable。生产的 bash 5 没这问题,但两边都要能跑。
    log "⚠️  ${name} 不存在 —— 未纳入本次备份"
    missing="${missing}${name},"
    continue
  fi
  if [ ! -s "$src" ]; then
    # 0 字节的"库"（生产上 multitenancy_routing.db / web-ui.db 就是）。
    # 关键：绝不能对它执行 sqlite3 打开 —— sqlite 会给空文件写入头部，
    # 那就违反了"备份只读生产、绝不写"这条硬约束。直接记账跳过。
    log "  ${name} 是 0 字节，跳过（避免 sqlite 初始化它 = 写生产）"
    empty="${empty}${name},"
    continue
  fi
  dst="$STAGING/db/$name"
  # .backup 是 sqlite 的在线备份：不阻塞写入者，且能正确带走 WAL 里的内容。
  # 绝不能用 cp —— WAL 模式下热拷贝会拷出损坏的库。
  # .timeout 让它在热写入下重试而不是直接放弃。
  #
  # 为什么不用评审建议的 `file:...?immutable=1`：immutable 是在向 sqlite 断言
  # "这个文件不会变"。生产库一直在写，这个断言是假的，sqlite 会因此忽略 WAL，
  # 读出撕裂或过期的镜像 —— 那是比"可能写一个字节"严重得多的正确性问题。
  # 真正的风险（对空文件写头部）已在上面用 `! -s` 挡掉了。
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
  # 没备到什么必须写在案上，跟 profiles_excludes 同理
  echo "db_missing=${missing:-(none)}"
  echo "db_empty_skipped=${empty:-(none)}"
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
STAGING=""   # 已落地，别让 cleanup 再去动它
log "状态核心完成 → ${STATE_DEST}（${backed_up} 个库）"

# ── 第二层:profiles 硬链增量 ────────────────────────────────────────
# --link-dest 让没变过的文件在新快照里只是一个硬链接，不占额外空间。
# 首次全量约 40G，之后每天只增加变化量。
if [ "$SKIP_PROFILES" = "1" ]; then
  log "按 SKIP_PROFILES=1 跳过 profiles 层（仅测试用）"
elif [ ! -d "$HERMES_HOME_DIR/profiles" ]; then
  # profiles 是 sunke 明确要求必须备的那 40G。目录不在 = 环境不对，
  # 不能像"可选项"一样静默跳过，否则会天天产出"成功"的半份备份。
  die "profiles 目录不存在：$HERMES_HOME_DIR/profiles —— 拒绝产出只有状态核心的半份备份"
else
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
    log "⚠️  profiles 下有 ${unreadable} 个文件当前用户读不到，它们不会进备份："
    # 同样的 pipefail+head 坑：head 提前关管道会让 find 拿 SIGPIPE，整条判失败
    set +e +o pipefail
    find "$HERMES_HOME_DIR/profiles" -type f ! -readable 2>/dev/null | head -5 | sed 's/^/      /'
    set -e -o pipefail
    log "    修法：sudo chown -R hermes:hermes $HERMES_HOME_DIR/profiles"
    # 只警告不拦是不够的：一份"成功"但静默漏了几百个文件的备份，
    # 会在真要恢复的那天才暴露。默认硬拦；确实要带病跑就显式 ALLOW_UNREADABLE=1。
    [ "$ALLOW_UNREADABLE" = "1" ] \
      || die "拒绝产出会静默漏数据的备份（如确认可接受，用 ALLOW_UNREADABLE=1 显式放行）"
  fi

  STAGING_PROF="$PROFILES_ROOT/.staging-$TS"
  set +e
  rsync -a --delete ${link_arg[@]+"${link_arg[@]}"} ${excl_arg[@]+"${excl_arg[@]}"} \
    "$HERMES_HOME_DIR/profiles/" "$STAGING_PROF/"
  rc=$?
  set -e
  # rsync 退出码：0=全好；24=传输中有文件消失（agent 在跑，属正常）；其余都当失败。
  case "$rc" in
    0) ;;
    24) log "  （有文件在传输中消失，属正常：agent 一直在写）" ;;
    *) die "profiles 增量快照失败，rsync 退出码 $rc" ;;
  esac
  PROF_DEST="$(uniq_dest "$PROFILES_ROOT/$TS")"
  mv "$STAGING_PROF" "$PROF_DEST"
  STAGING_PROF=""   # 已落地，别让 cleanup 再去动它
  # 同 pipefail 坑:统计命令不参与 -e 判定(生产上演练脚本已被这个坑咬过一次)
  set +e +o pipefail
  nprof=$(find "$PROF_DEST" -type f 2>/dev/null | wc -l | tr -d ' ')
  set -e -o pipefail
  [ "$nprof" -gt 0 ] || die "profiles 快照产出 0 个文件 —— 空备份不算成功"
  log "profiles 快照完成 → ${PROF_DEST}（${nprof} 个文件$([ -n "$prev" ] && echo "，增量，基于 $(basename "${prev%/}")" || echo "，首次全量")）"
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

[ -z "$missing" ] || log "⚠️  本次未备到的库：${missing%,}"
[ -z "$empty" ] || log "  （0 字节跳过：${empty%,}）"
log "备份完成。提醒：这是本机备份，不是灾备 —— 异地尚未建立。"
