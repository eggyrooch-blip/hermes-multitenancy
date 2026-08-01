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

STATE_ROOT="$BACKUP_ROOT/state"
TS="$(date +%Y%m%dT%H%M%S)"
WORK="$DRILL_ROOT/hermes-drill-$TS"
REPORT="$REPORT_DIR/drill-$TS.md"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ -d "$STATE_ROOT" ] || die "找不到备份目录 $STATE_ROOT —— 先跑过一次 hermes-backup.sh"
SRC="$(ls -1d "$STATE_ROOT"/*/ 2>/dev/null | sort | tail -1 || true)"
[ -n "$SRC" ] || die "$STATE_ROOT 下没有任何备份"
SRC="${SRC%/}"

# 顺序很重要：先校验目标合法，再创建任何东西。
# 反过来的话，一个指向生产的 DRILL_ROOT 会先把目录建出来（或报个无关的 mkdir 错），
# 守卫就永远轮不到执行 —— 我自己的测试就是这么抓到这个次序 bug 的。
#
# 演练永远写 scratch，绝不写回 ~/.hermes —— 这条是硬约束。
# 必须比较「真实路径」而不是字符串前缀：/tmp/../home/hermes/.hermes 或一条软链
# 都能骗过字符串比较，然后把生产覆盖掉。realpath -m 允许目标还不存在。
canon() { realpath -m "$1" 2>/dev/null || python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$1"; }
WORK_REAL="$(canon "$WORK")"
for guarded in "$HERMES_HOME_DIR" "$HERMES_WEBUI_DIR"; do
  [ -e "$guarded" ] || continue
  G_REAL="$(canon "$guarded")"
  case "$WORK_REAL/" in
    "$G_REAL"/*|"$G_REAL"/) die "还原目标的真实路径落在生产目录里，拒绝执行：$WORK_REAL" ;;
  esac
  [ "$WORK_REAL" = "$G_REAL" ] && die "还原目标就是生产目录本身，拒绝执行：$WORK_REAL"
done

# 守卫过了才动手建目录
mkdir -p "$REPORT_DIR" "$WORK"
chmod 700 "$WORK"

backup_ts="$(grep '^backup_ts=' "$SRC/MANIFEST.txt" | cut -d= -f2)"
started=$(date +%s)

# ── 还原 ────────────────────────────────────────────────────────────
cp -a "$SRC/." "$WORK/"
restore_secs=$(( $(date +%s) - started ))

# ── 校验 1:文件完整性 ───────────────────────────────────────────────
sha_result="通过"
if command -v sha256sum >/dev/null 2>&1; then
  ( cd "$WORK" && sha256sum -c SHA256SUMS >/dev/null 2>&1 ) || sha_result="失败"
else
  ( cd "$WORK" && shasum -a 256 -c SHA256SUMS >/dev/null 2>&1 ) || sha_result="失败"
fi

# ── 校验 2+3:每库 integrity_check,每表行数对账 ──────────────────────
fail=0
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

# ── 校验 4:profiles 层也必须被演练到 ────────────────────────────────
# Done 是两层备份。只演练数据库、不碰那 40G，等于把 sunke 明确要求的那一层
# 留到真出事那天才第一次验证。这里做轻量但真实的检查：快照存在、非空、
# 抽样文件的内容与源一致（用校验和比，不是只看存在）。
PROFILES_ROOT="$BACKUP_ROOT/profiles"
prof_line=""
PSRC="$(ls -1d "$PROFILES_ROOT"/*/ 2>/dev/null | sort | tail -1 || true)"
if [ -z "$PSRC" ]; then
  if [ -d "$HERMES_HOME_DIR/profiles" ]; then
    prof_line="| **缺失** | 有 state 备份却没有 profiles 快照 | — |"
    fail=1
  else
    prof_line="| （本环境无 profiles） | — | — |"
  fi
else
  PSRC="${PSRC%/}"
  # `2>/dev/null` 只吞掉了消息，没吞掉退出码：38 万个文件里只要有一个 find 读不动，
  # 它就返回非零，pipefail 把整条管道判失败，set -e 直接把脚本踢出去 ——
  # 生产上第一次跑就是死在这一行，而且一声不吭。统计类命令一律不参与 -e 判定。
  set +e +o pipefail
  pfiles=$(find "$PSRC" -type f 2>/dev/null | wc -l | tr -d ' ')
  set -e -o pipefail
  [ "${pfiles:-0}" -gt 0 ] || fail=1
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
    if [ -f "$HERMES_HOME_DIR/profiles/$rel" ] \
       && cmp -s "$PSRC/$rel" "$HERMES_HOME_DIR/profiles/$rel"; then
      sample_ok=$((sample_ok + 1))
    fi
  done <<< "$sample_list"
  # 抽样只作信息，不判失败：快照拍完之后员工继续改文件，内容不一致是正常的。
  # 真正的失败条件只有两个 —— 快照缺失、或快照 0 个文件（上面已置 fail=1）。
  prof_line="| \`$(basename "$PSRC")\` | ${pfiles} 个文件 | 抽样 ${sample_ok}/${sample_n} 内容一致（仅供参考） |"
fi

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
| 恢复出来的数据对不对？ | 文件校验 **$sha_result**；逐库结果见下表 |

## 逐库核对

| 数据库 | 完整性检查 | 对账表数 | 行数不符 |
|---|---|---|---|
$db_rows
> 「行数不符」必须全是 0。判据是**还原结果 vs 备份当时记录的行数**，
> 不是 vs 当下生产 —— 生产一直在写，快照必然落后，那不是错误。

## 第二层：profiles（40G 那层）

| 快照 | 规模 | 抽样内容核对 |
|---|---|---|
$prof_line
> 抽样是拿快照里的文件跟当前源文件逐字节比。有差异不一定是错（备份之后员工又改了），
> 但**快照缺失或 0 个文件一定是错**，会直接判失败。

## 参考：这份备份比当下生产落后多少

| 表 | 备份时 | 现在 |
|---|---|---|
$drift
> 这一节只是信息，**不参与判定**。数字有差是正常的，说明业务在跑。

---
演练目录：\`$WORK\`（$([ "$KEEP_RESTORE" = 1 ] && echo "已保留" || echo "已清理")）
EOF

[ "$KEEP_RESTORE" = 1 ] || rm -rf "$WORK"

echo "演练完成：$verdict"
echo "报告：$REPORT"
[ "$fail" = 0 ] && [ "$sha_result" = "通过" ] || exit 1
