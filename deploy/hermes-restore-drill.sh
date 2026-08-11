#!/usr/bin/env bash
# hermes-restore-drill.sh — 灾备演练：把最新一份备份真的还原一遍，并出一份人能读的报告。
#
# 为什么要有这个：没演练过的备份不叫备份，叫许愿。
# 报告要回答 sunke 的三个问题：备份是什么时候的？恢复花了多久？恢复出来的数据对不对？
#
# 判据说明（重要）：
#   通过条件 = 还原出来的行数 == 备份 MANIFEST 里记录的行数，差 0。
#   不能拿它跟“当下的生产”比 —— 生产一直在写，几小时前的快照必然落后。
#   与生产的差值只作为“落后了多少”的信息打印出来，不参与判定。
set -euo pipefail

BACKUP_ROOT="${BACKUP_ROOT:-$HOME/backups/daily}"
HERMES_HOME_DIR="${HERMES_HOME_DIR:-$HOME/.hermes}"
HERMES_WEBUI_DIR="${HERMES_WEBUI_DIR:-$HOME/.hermes-web-ui}"
DRILL_ROOT="${DRILL_ROOT:-/tmp}"
REPORT_DIR="${REPORT_DIR:-$BACKUP_ROOT/drill-reports}"
KEEP_RESTORE="${KEEP_RESTORE:-0}"   # 1 = 演练后保留还原目录（排障用）
MAX_RPO_SECONDS="${MAX_RPO_SECONDS:-86400}"
MAX_RTO_SECONDS="${MAX_RTO_SECONDS:-14400}"
RESTORE_HEADROOM_GB="${RESTORE_HEADROOM_GB:-5}"

STATE_ROOT="$BACKUP_ROOT/state"
TS="$(date +%Y%m%dT%H%M%S)"
WORK=""
WORK_PROBE="$DRILL_ROOT/hermes-drill-$TS.probe"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ -d "$STATE_ROOT" ] || die "找不到备份目录 $STATE_ROOT —— 先跑过一次 hermes-backup.sh"
# 只认带 COMPLETE 哨兵的目录。半途失败留下的残缺目录必须被跳过，
# 否则演练会拿一份缺数据的备份跑出"通过"——假绿比没有更危险。
SRC=""
while IFS= read -r cand; do
  [ -n "$cand" ] || continue
  [ -f "${cand%/}/COMPLETE" ] && SRC="${cand%/}"
done < <(ls -1d "$STATE_ROOT"/*/ 2>/dev/null | sort || true)
[ -n "$SRC" ] || die "$STATE_ROOT 下没有任何【完整】备份（缺 COMPLETE 哨兵）"
SRC="${SRC%/}"

# 顺序很重要：先校验目标合法，再创建任何东西。
# 反过来的话，一个指向生产的 DRILL_ROOT 会先把目录建出来（或报个无关的 mkdir 错），
# 守卫就永远轮不到执行 —— 我自己的测试就是这么抓到这个次序 bug 的。
#
# 演练永远写 scratch，绝不写回 ~/.hermes —— 这条是硬约束。
# 必须比较「真实路径」而不是字符串前缀：/tmp/../home/hermes/.hermes 或一条软链
# 都能骗过字符串比较，然后把生产覆盖掉。realpath -m 允许目标还不存在。
canon() { realpath -m "$1" 2>/dev/null || python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$1"; }
WORK_REAL="$(canon "$WORK_PROBE")"
DRILL_ROOT_REAL="$(canon "$DRILL_ROOT")"
for guarded in "$HERMES_HOME_DIR" "$HERMES_WEBUI_DIR"; do
  [ -e "$guarded" ] || continue
  G_REAL="$(canon "$guarded")"
  case "$WORK_REAL/" in
    "$G_REAL"/*|"$G_REAL"/) die "还原目标的真实路径落在生产目录里，拒绝执行：$WORK_REAL" ;;
  esac
  [ "$WORK_REAL" = "$G_REAL" ] && die "还原目标就是生产目录本身，拒绝执行：$WORK_REAL"
done

# 守卫过了才动手建目录；mktemp 避免可预测 /tmp 名被预建或软链劫持。
mkdir -p "$REPORT_DIR" "$DRILL_ROOT"
WORK="$(mktemp -d "$DRILL_ROOT/hermes-drill-$TS.XXXXXX")" \
  || die "无法创建隔离恢复目录"
WORK_REAL="$(canon "$WORK")"
REPORT="$REPORT_DIR/drill-$(basename "$WORK").md"
chmod 700 "$WORK"
cleanup_work() {
  if [ "$KEEP_RESTORE" != "1" ]; then
    case "$WORK_REAL" in "$DRILL_ROOT_REAL"/hermes-drill-*) rm -rf "$WORK_REAL" ;; esac
  fi
}
trap cleanup_work EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

backup_ts="$(grep '^backup_ts=' "$SRC/MANIFEST.txt" | cut -d= -f2)"
started=$(date +%s)
backup_epoch="$(python3 - "$backup_ts" <<'PY'
from datetime import datetime
import sys
print(int(datetime.strptime(sys.argv[1], "%Y%m%dT%H%M%S").timestamp()))
PY
)" || die "MANIFEST backup_ts 无法解析：$backup_ts"
rpo_seconds=$((started - backup_epoch))
rpo_fail=0
[ "$rpo_seconds" -ge 0 ] && [ "$rpo_seconds" -le "$MAX_RPO_SECONDS" ] || rpo_fail=1

# ── 还原 ────────────────────────────────────────────────────────────
cp -a "$SRC/." "$WORK/"

# ── 校验 1:文件完整性 ───────────────────────────────────────────────
sha_result="通过"
if command -v sha256sum >/dev/null 2>&1; then
  ( cd "$WORK" && sha256sum -c SHA256SUMS >/dev/null 2>&1 ) || sha_result="失败"
else
  ( cd "$WORK" && shasum -a 256 -c SHA256SUMS >/dev/null 2>&1 ) || sha_result="失败"
fi

# ── 校验 2+3:每库 integrity_check,每表行数对账 ──────────────────────
fail="$rpo_fail"
db_rows=""
for db in "$WORK"/db/*.db; do
  dbn="$(basename "$db")"
  chk="$(sqlite3 "$db" "pragma integrity_check;" 2>&1 | head -1)"
  [ "$chk" = "ok" ] || { chk="**$chk**"; fail=1; }

  mismatch=0; checked=0
  while IFS= read -r line; do
    t="${line#rows ${dbn}.}"; t="${t%%=*}"
    want="${line##*=}"
    got="$(sqlite3 "$db" "select count(*) from \"$t\";" 2>/dev/null || echo "ERR")"
    checked=$((checked + 1))
    [ "$got" = "$want" ] || { mismatch=$((mismatch + 1)); fail=1; }
  done < <(grep "^rows ${dbn}\." "$SRC/MANIFEST.txt" || true)

  db_rows="${db_rows}| \`${dbn}\` | ${chk} | ${checked} | ${mismatch} |
"
done

# ── 校验 3.5:六库清单对账 ───────────────────────────────────────────
# 只遍历 db/*.db 会漏掉"这次根本没备到的库"——那恰恰是最该被看见的。
# 从 MANIFEST 把缺失/空库读回来，在报告里逐个点名。
man_missing="$(grep '^db_missing=' "$SRC/MANIFEST.txt" | cut -d= -f2-)"
man_empty="$(grep '^db_empty_skipped=' "$SRC/MANIFEST.txt" | cut -d= -f2-)"
inventory=""
for known in multitenancy.db state.db kanban.db multitenancy_routing.db hermes-web-ui.db web-ui.db; do
  # 必须按逗号分词精确匹配：子串匹配下 `web-ui.db` 会命中 `hermes-web-ui.db`，
  # 于是"缺失"被误报成"空库"，一个真缺的库就这么混过去了。
  in_csv() { case ",$1," in *",$2,"*) return 0 ;; *) return 1 ;; esac; }
  if [ -f "$WORK/db/$known" ]; then
    st="已备份并核对"
  elif in_csv "$man_empty" "$known"; then
    st="0 字节，按设计跳过"
  elif in_csv "$man_missing" "$known"; then
    st="**缺失** —— 备份时源文件不存在"; fail=1
  else
    st="**未知** —— 既不在备份里也不在 MANIFEST 记录中"; fail=1
  fi
  inventory="${inventory}| \`${known}\` | ${st} |
"
done

# ── 校验 4:profiles 层也必须被演练到 ────────────────────────────────
# Done 是两层备份。只演练数据库、不碰那 40G，等于把 sunke 明确要求的那一层
# 留到真出事那天才第一次验证。这里做轻量但真实的检查：快照存在、非空、
# 抽样文件的内容与源一致（用校验和比，不是只看存在）。
PROFILES_ROOT="$BACKUP_ROOT/profiles"
prof_line=""
# 用哨兵里记下的那一份，不是"当前最新的一份"。
# 否则一次半途失败留下的更新的孤儿 profiles 目录，会被配到一份更老的 state 上，
# 演练对一个从未同时产出的组合报"通过"。两层必须是同一次备份的产物。
pinned="$(grep '^profiles_snapshot=' "$SRC/COMPLETE" 2>/dev/null | cut -d= -f2- || true)"
PSRC=""
if [ -n "$pinned" ] && [ "$pinned" != "(skipped)" ]; then
  PINNED_REAL="$(canon "$pinned")"
  PROFILES_REAL="$(canon "$PROFILES_ROOT")"
  if [ "$(dirname "$PINNED_REAL")" != "$PROFILES_REAL" ] || [ -L "$pinned" ]; then
    die "配套快照路径越界，拒绝读取"
  fi
  [ -d "$PINNED_REAL" ] && PSRC="$PINNED_REAL"
  if [ -z "$PSRC" ]; then
    prof_line="| **配套快照丢失** | 哨兵记的是 \`$(basename "$pinned")\`，现已不存在 | — |"
    fail=1
  fi
fi
if [ -z "$PSRC" ] && [ -z "$prof_line" ]; then
  if [ -d "$HERMES_HOME_DIR/profiles" ]; then
    prof_line="| **缺失** | 有 state 备份却没有配套 profiles 快照 | — |"
    fail=1
  else
    prof_line="| （本环境无 profiles） | — | — |"
  fi
elif [ -n "$PSRC" ]; then
  PSRC="${PSRC%/}"
  [ -s "$WORK/PROFILE_SHA256SUMS" ] || die "备份缺少 profiles 内容校验清单"
  [ -f "$WORK/PROFILE_SYMLINKS.json" ] || die "备份缺少 profiles 软链清单"
  set +e
  profile_kb="$(du -sk "$PSRC" 2>/dev/null | awk '{print $1}')"
  profile_size_rc=$?
  set -e
  [ "$profile_size_rc" -eq 0 ] && [ -n "$profile_kb" ] \
    || die "无法安全计算 profile 快照规模"
  free_kb="$(df -Pk "$WORK" | awk 'NR==2{print $4}')"
  need_kb=$((profile_kb + RESTORE_HEADROOM_GB * 1024 * 1024))
  [ "$free_kb" -ge "$need_kb" ] \
    || die "恢复空间不足：可用空间低于 profile 快照加 ${RESTORE_HEADROOM_GB}G 余量"
  mkdir -p "$WORK/profiles"
  if ! rsync -a "$PSRC/" "$WORK/profiles/"; then
    prof_line="| **恢复失败** | rsync 无法完整读取配套快照 | — |"
    fail=1
  fi
  # `2>/dev/null` 只吞掉了消息，没吞掉退出码：38 万个文件里只要有一个 find 读不动，
  # 它就返回非零，pipefail 把整条管道判失败，set -e 直接把脚本踢出去 ——
  # 生产上第一次跑就是死在这一行，而且一声不吭。统计类命令一律不参与 -e 判定。
  set +e +o pipefail
  pfiles=$(find "$PSRC" -type f 2>/dev/null | wc -l | tr -d ' ')
  restored_files=$(find "$WORK/profiles" -type f 2>/dev/null | wc -l | tr -d ' ')
  expected_profile_files=$(wc -l < "$WORK/PROFILE_SHA256SUMS" | tr -d ' ')
  set -e -o pipefail
  [ "${expected_profile_files:-0}" -gt 0 ] \
    && [ "$restored_files" = "$expected_profile_files" ] || fail=1
  # 抽 5 个文件跟源比内容。
  # 注意 `set -o pipefail` + `head` 的经典坑：head 读够就关管道，find 收到 SIGPIPE
  # 退出非零，pipefail 把整条管道判失败，再被 set -e 一脚踢出去 —— 生产上就是
  # 这么静默挂掉的。所以这里先关掉 -e/pipefail 取样，取完再开回来。
  set +e +o pipefail
  sample_list="$(cd "$PSRC" && find . -type f 2>/dev/null | head -400 | sed 's|^\./||' | awk 'NR%80==1' | head -5)"
  set -e -o pipefail

  sample_ok=0; sample_n=0
  while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    sample_n=$((sample_n + 1))
    if [ -f "$WORK/profiles/$rel" ] \
       && cmp -s "$PSRC/$rel" "$WORK/profiles/$rel"; then
      sample_ok=$((sample_ok + 1))
    fi
  done <<< "$sample_list"
  [ "$sample_ok" = "$sample_n" ] || fail=1
  set +e
  profile_delta="$(rsync -a -n -c --delete --out-format='%i' "$PSRC/" "$WORK/profiles/" 2>/dev/null | sed -n '1p')"
  profile_check_rc=$?
  set -e
  set +e
  if command -v sha256sum >/dev/null 2>&1; then
    ( cd "$WORK/profiles" && sha256sum -c "$WORK/PROFILE_SHA256SUMS" >/dev/null 2>&1 )
  else
    ( cd "$WORK/profiles" && shasum -a 256 -c "$WORK/PROFILE_SHA256SUMS" >/dev/null 2>&1 )
  fi
  manifest_check_rc=$?
  set -e
  python3 - "$WORK/profiles" > "$WORK/PROFILE_SYMLINKS.current.json" <<'PY'
import json, os, sys
root = sys.argv[1]
links = []
for base, dirs, files in os.walk(root, followlinks=False):
    for name in dirs + files:
        path = os.path.join(base, name)
        if os.path.islink(path):
            links.append([os.path.relpath(path, root), os.readlink(path)])
json.dump(sorted(links), sys.stdout, ensure_ascii=False, separators=(",", ":"))
PY
  link_check_rc=0
  cmp -s "$WORK/PROFILE_SYMLINKS.json" "$WORK/PROFILE_SYMLINKS.current.json" || link_check_rc=$?
  [ "$profile_check_rc" -eq 0 ] && [ -z "$profile_delta" ] \
    && [ "$manifest_check_rc" -eq 0 ] && [ "$link_check_rc" -eq 0 ] || fail=1
  prof_line="| \`$(basename "$PSRC")\` | ${restored_files}/${expected_profile_files} 个文件 | 固化 manifest $([ "$manifest_check_rc" -eq 0 ] && [ "$link_check_rc" -eq 0 ] && echo 通过 || echo 失败)；恢复副本 checksum $([ "$profile_check_rc" -eq 0 ] && [ -z "$profile_delta" ] && echo 通过 || echo 失败) |"
fi

restore_secs=$(( $(date +%s) - started ))

# ── 信息项:与当下生产的漂移(不参与判定) ─────────────────────────────
drift=""
for pair in "$HERMES_HOME_DIR/multitenancy.db:multitenancy.db" "$HERMES_WEBUI_DIR/hermes-web-ui.db:hermes-web-ui.db"; do
  live="${pair%%:*}"; dbn="${pair##*:}"
  [ -f "$live" ] && [ -f "$WORK/db/$dbn" ] || continue
  while IFS= read -r line; do
    t="${line#rows ${dbn}.}"; t="${t%%=*}"
    bak="${line##*=}"
    now="$(sqlite3 -cmd ".timeout 5000" "$live" "select count(*) from \"$t\";" 2>/dev/null || echo "-")"
    [ "$now" = "$bak" ] && continue
    drift="${drift}| \`${dbn}.${t}\` | ${bak} | ${now} |
"
  done < <(grep "^rows ${dbn}\." "$SRC/MANIFEST.txt" || true)
done
[ -n "$drift" ] || drift="| （无差异） | | |
"

[ "$restore_secs" -le "$MAX_RTO_SECONDS" ] || fail=1
verdict=$([ "$fail" = 0 ] && [ "$sha_result" = "通过" ] && echo "✅ 通过" || echo "❌ 未通过")

# ── 报告 ────────────────────────────────────────────────────────────
cat > "$REPORT" <<EOF
# 灾备演练报告 — $TS

> ⚠️ **这是本机备份，不是灾备。** 它能救误删、误改、发布搞砸；
> 救不了盘坏、机器炸、整机被删。异地备份尚未建立。

## 结论：$verdict

| 你的问题 | 答案 |
|---|---|
| 备份是什么时候的？ | **$backup_ts**（源目录 \`$(basename "$SRC")\`） |
| 恢复花了多久？ | **${restore_secs} 秒** |
| RPO 是否达标？ | **${rpo_seconds} 秒 / 上限 ${MAX_RPO_SECONDS} 秒** |
| RTO 是否达标？ | **${restore_secs} 秒 / 上限 ${MAX_RTO_SECONDS} 秒** |
| 恢复出来的数据对不对？ | 文件校验 **$sha_result**；逐库结果见下表 |

## 逐库核对

| 数据库 | 完整性检查 | 对账表数 | 行数不符 |
|---|---|---|---|
$db_rows
> 「行数不符」必须全是 0。判据是**还原结果 vs 备份当时记录的行数**，
> 不是 vs 当下生产 —— 生产一直在写，快照必然落后，那不是错误。

## 六库清单对账

| 已知数据库 | 本次状态 |
|---|---|
$inventory
> 这张表按**固定的六库清单**走，不是按备份目录里有什么走 ——
> 「这次根本没备到的库」恰恰是最需要被看见的那一类。

## 第二层：profiles（40G 那层）

| 快照 | 规模 | 抽样内容核对 |
|---|---|---|
$prof_line
> profiles 会真实还原到隔离演练目录，再与快照逐字节抽样核对；
> **快照缺失、文件数不符或抽样不符都会直接判失败**。

## 参考：这份备份比当下生产落后多少

| 表 | 备份时 | 现在 |
|---|---|---|
$drift
> 这一节只是信息，**不参与判定**。数字有差是正常的，说明业务在跑。

---
演练目录：\`$WORK\`（$([ "$KEEP_RESTORE" = 1 ] && echo "已保留" || echo "已清理")）
EOF

echo "演练完成：$verdict"
echo "报告：$REPORT"
status=$([ "$fail" = 0 ] && [ "$sha_result" = "通过" ] && echo PASS || echo FAIL)
echo "RESULT status=$status rpo_seconds=$rpo_seconds rto_seconds=$restore_secs"
[ "$status" = PASS ] || exit 1
