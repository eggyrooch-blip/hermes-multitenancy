#!/usr/bin/env bash
# hermes-release.sh — 生产侧发布执行器（主动拉取，不是 CI 往里推）
#
# 半自动的准确含义：**触发是自动的**（每天 18:00 醒来看一眼），
# **决定是手动的**（sunke 打不打 release-* 标签）。没有新标签就什么都不做。
#
# 授权 = GitLab 上的受保护标签 `release-*`（只有 Maintainer 能打）。
# 一个标签同时钉死两个仓的 SHA —— webui 的 bridge 依赖插件约 20 个符号，
# 两仓分开发布等于第一天就内建版本错配竞态。
#
# 目录形态（agent 核心早就是这个形态，本脚本把两个业务仓也统一过来）：
#   ~/releases/.repo-mt, .repo-webui   canonical git 仓
#   ~/releases/mt-<sha>, webui-<sha>   每个版本一个目录
#   ~/code/hermes-multitenancy         → 软链 → releases/mt-<sha>
#   ~/code/hermes-web-ui               → 软链 → releases/webui-<sha>
# 回滚 = 把软链翻回去 + 重启，秒级。
set -uo pipefail

RELEASES="${RELEASES:-$HOME/releases}"
CODE="${CODE:-$HOME/code}"
STATE_FILE="${STATE_FILE:-$HOME/.hermes/deployed-release}"
BACKUP_ROOT="${BACKUP_ROOT:-$HOME/backups/pre-release}"
LOCK="${LOCK:-$HOME/.hermes/.release.lock}"
KEEP_RELEASES="${KEEP_RELEASES:-3}"
PROBES="${PROBES:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/hermes-release-probes.sh}"
BACKUP_SH="${BACKUP_SH:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/hermes-backup.sh}"
SYSTEMCTL="${SYSTEMCTL:-systemctl --user}"
# 生产上除了主 gateway 还跑着 hermes-gateway@<profile>.service（专家 bot）。
# 它的 WorkingDirectory 也走同一条软链，但进程不重启 = 内存里还是旧代码，
# 发布等于没对它生效。动态枚举，以后新增专家 profile 自动覆盖。
# 注意是【惰性】求值：无新标签时脚本必须完全静默，连一次 list-units 都不该跑。
# 在变量赋值处直接展开会让每次定时器空转都去问一遍 systemd。
_expert_units() { $SYSTEMCTL list-units --type=service --all --no-pager 2>/dev/null \
  | grep -oE 'hermes-gateway@[A-Za-z0-9_-]+\.service' | sort -u | tr '\n' ' '; }
_units() { printf '%s' "${UNITS:-hermes-gateway.service hermes-web-ui.service $(_expert_units)}"; }
DRY_RUN="${DRY_RUN:-0}"

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

mkdir -p "$RELEASES" "$BACKUP_ROOT" "$(dirname "$STATE_FILE")"
# Linux(生产)用 flock：进程被 kill 时内核自动释放，不会留死锁。
# macOS 没有 flock（只在本地跑测试时走 mkdir 分支）。
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  flock -n 9 || { log "另一个发布正在进行，本次退出"; exit 0; }
else
  LOCK_DIR="$LOCK.d"
  # 陈旧锁自动清理：一次硬崩不该让发布永久停摆
  [ -d "$LOCK_DIR" ] && [ -z "$(find "$LOCK_DIR" -maxdepth 0 -mmin -360 2>/dev/null)" ] && rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR" 2>/dev/null || { log "另一个发布正在进行，本次退出"; exit 0; }
  trap 'rm -rf "$LOCK_DIR"' EXIT
fi

# ── 生产上 .git 被 root 占是反复出现的坑，动 git 之前先归位 ──────────
for r in "$RELEASES/.repo-mt" "$RELEASES/.repo-webui"; do
  [ -d "$r" ] || continue
  if [ -n "$(find "$r" -not -user "$(id -un)" -print -quit 2>/dev/null)" ]; then
    log "修正 $r 的属主（root 占用是本机老坑）"
    chown -R "$(id -un):$(id -gn)" "$r" 2>/dev/null || true
    # `|| true` 吞掉失败等于放任下一次 18:00 部署撞在同一个坑上。
    # 必须核实真的能写，不能只是"试过了"。
    [ -w "$r/.git" ] || [ -w "$r" ] \
      || die "$r 仍不可写（属主修正失败）—— 拒绝在坏权限上跑 git"
  fi
done

# ── 找最新的 release-* 标签 ──────────────────────────────────────────
git -C "$RELEASES/.repo-mt" fetch -q --tags --prune origin 2>/dev/null \
  || git -C "$RELEASES/.repo-mt" fetch -q --tags --prune github 2>/dev/null \
  || log "警告：拉取标签失败，用本地已有的（GitLab/GitHub 不可用时不影响当前运行的版本）"

TAG=$(git -C "$RELEASES/.repo-mt" tag -l 'release-*' --sort=-creatordate | head -1)
[ -n "$TAG" ] || { log "没有任何 release-* 标签，什么都不做"; exit 0; }

CURRENT=$(cat "$STATE_FILE" 2>/dev/null || echo "")
if [ "$TAG" = "$CURRENT" ]; then
  log "已是最新（$TAG），什么都不做"
  exit 0
fi
log "发现新发布：$TAG（当前：${CURRENT:-无}）"

# ── 从标签 annotation 里读发布清单 ───────────────────────────────────
# 格式（每行一个仓）：
#   multitenancy: <full-sha>
#   webui: <full-sha>
BODY=$(git -C "$RELEASES/.repo-mt" tag -l --format='%(contents)' "$TAG")
MT_SHA=$(printf '%s\n' "$BODY" | sed -n 's/^multitenancy:[[:space:]]*//p' | head -1)
WEBUI_SHA=$(printf '%s\n' "$BODY" | sed -n 's/^webui:[[:space:]]*//p' | head -1)
[ -n "$MT_SHA" ] && [ -n "$WEBUI_SHA" ] \
  || die "$TAG 的 annotation 里缺 multitenancy/webui 的 SHA —— 拒绝发布残缺清单"
# 必须是完整 40 位 hex。手打标签时少几位、或写成分支名，都可能检出到别的东西。
case "$MT_SHA" in *[!0-9a-f]*|"") die "multitenancy SHA 不是 40 位 hex：$MT_SHA" ;; esac
case "$WEBUI_SHA" in *[!0-9a-f]*|"") die "webui SHA 不是 40 位 hex：$WEBUI_SHA" ;; esac
[ ${#MT_SHA} -eq 40 ] || die "multitenancy SHA 长度不是 40：$MT_SHA"
[ ${#WEBUI_SHA} -eq 40 ] || die "webui SHA 长度不是 40：$WEBUI_SHA"
log "  multitenancy: ${MT_SHA:0:12}"
log "  webui:        ${WEBUI_SHA:0:12}"

if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN=1，到此为止"; exit 0; fi

# ── 记下当前指向，回滚要用 ───────────────────────────────────────────
PREV_MT=$(readlink "$CODE/hermes-multitenancy" || true)
PREV_WEBUI=$(readlink "$CODE/hermes-web-ui" || true)
[ -n "$PREV_MT" ] && [ -n "$PREV_WEBUI" ] \
  || die "$CODE/hermes-* 还不是软链 —— 先做一次迁移再启用本执行器"

# ── 发布前回滚包（不是灾备，是「这次发错了能退回去」）────────────────
SNAP="$BACKUP_ROOT/$TAG"
log "做发布前回滚包 → $SNAP"
mkdir -p "$SNAP"
# 缺了备份脚本就静默跳过 = 悄悄失去「不带备份不发布」这条保护。
# 换机器或路径变动时最容易踩。缺失即拒绝。
[ -x "$BACKUP_SH" ] || die "找不到可执行的备份脚本（$BACKUP_SH）—— 不带备份不发布"
SKIP_PROFILES=1 BACKUP_ROOT="$SNAP" "$BACKUP_SH" >"$SNAP/backup.log" 2>&1 \
  || die "发布前备份失败 —— 不带备份不发布"
{ echo "tag=$TAG"; echo "prev_mt=$PREV_MT"; echo "prev_webui=$PREV_WEBUI"
  echo "new_mt=$MT_SHA"; echo "new_webui=$WEBUI_SHA"
  echo "state_snapshot=$SNAP/state"
  # 哪些库进了快照、哪些没有，直接指到备份自己的 MANIFEST，别让人去猜
  for m in "$SNAP"/state/*/MANIFEST.txt; do
    [ -f "$m" ] || continue
    grep -E '^(db_count|db_missing|db_empty_skipped)=' "$m" | sed 's/^/backup_/'
  done; } > "$SNAP/ROLLBACK.txt"
log "  回滚锚点已记录"

# 每个终局都要留下结论。只 exit 1 的话，运维手上只有一份"陈旧的锚点"，
# 根本看不出这次到底是成功了、回滚了、还是回滚也没成。
outcome() { printf '%s\n' "outcome=$1" "at=$(date -Is)" >> "$SNAP/ROLLBACK.txt"; }

# ── 构建新版本目录（不碰正在跑的目录）────────────────────────────────
build_worktree() {  # $1=canonical 仓  $2=目标目录  $3=sha
  local repo="$1" dest="$2" sha="$3"
  [ -d "$dest" ] && { log "  $dest 已存在，复用"; return 0; }
  git -C "$repo" fetch -q --all --tags 2>/dev/null || true
  git -C "$repo" worktree add -q --detach "$dest" "$sha" || return 1
}

# 对象必须真的存在于各自的仓里 —— 不然 worktree add 会报一个难懂的错，
# 而我们要的是「拒绝发布」这个明确结论。
git -C "$RELEASES/.repo-mt" fetch -q --all --tags 2>/dev/null || true
git -C "$RELEASES/.repo-webui" fetch -q --all --tags 2>/dev/null || true
git -C "$RELEASES/.repo-mt" cat-file -e "${MT_SHA}^{commit}" 2>/dev/null \
  || die "multitenancy 仓里没有这个提交：$MT_SHA"
git -C "$RELEASES/.repo-webui" cat-file -e "${WEBUI_SHA}^{commit}" 2>/dev/null \
  || die "webui 仓里没有这个提交：$WEBUI_SHA"

MT_DIR="$RELEASES/mt-${MT_SHA:0:7}"
WEBUI_DIR="$RELEASES/webui-${WEBUI_SHA:0:8}"

log "构建 multitenancy → $MT_DIR"
build_worktree "$RELEASES/.repo-mt" "$MT_DIR" "$MT_SHA" || die "multitenancy 检出失败"

log "构建 webui → $WEBUI_DIR（含 npm ci + build，要几分钟）"
if build_worktree "$RELEASES/.repo-webui" "$WEBUI_DIR" "$WEBUI_SHA"; then
  # .env 不在 git 里，指向跨版本稳定的那一份。
  # 先确认它真的在、且权限没被放宽 —— 缺了它 webui 起不来，
  # 权限松了等于把 22 行密钥摊开给同机其他用户。
  STABLE_ENV="$HOME/.hermes-web-ui/.env"
  [ -f "$STABLE_ENV" ] || die "缺少稳定的 webui .env（$STABLE_ENV）—— 拒绝构建"
  mode=$(stat -c '%a' "$STABLE_ENV" 2>/dev/null || stat -f '%Lp' "$STABLE_ENV" 2>/dev/null)
  [ "$mode" = "600" ] || die "$STABLE_ENV 权限是 $mode，必须是 600"
  ln -sfn "$STABLE_ENV" "$WEBUI_DIR/.env"
  if [ ! -f "$WEBUI_DIR/dist/server/index.js" ]; then
    ( cd "$WEBUI_DIR" && npm ci --no-audit --no-fund >/dev/null 2>&1 && npm run build >/dev/null 2>&1 ) \
      || die "webui 构建失败 —— 没切换任何东西，当前版本继续跑"
  fi
  [ -f "$WEBUI_DIR/dist/server/index.js" ] || die "webui 构建产物缺失 —— 拒绝切换"
else
  die "webui 检出失败"
fi
# 切之前先确认新版本自带探针。少了它就没有存活判据，
# 切过去只会立刻回滚 —— 白白让 1259 个人经历一次重启。
# （2026-08-01 首次实弹就撞上这个：新版本还没 ship 进探针脚本，
#   切过去 → 找不到探针 → 自动回滚。回滚是对的，但这次重启本可以避免。）
NEW_PROBES="$MT_DIR/deploy/$(basename "$PROBES")"
[ -x "$NEW_PROBES" ] || die "新版本 $MT_DIR 里没有可执行的探针（$NEW_PROBES）—— 拒绝切换，当前版本继续跑"
log "  两个版本目录都就绪且自带探针（此刻生产仍跑旧版）"

# ── 原子切换 ─────────────────────────────────────────────────────────
# `ln -sfn` 是「先 unlink 再 symlink」——中间有一瞬间这个路径根本不存在。
# 而 editable 导入(MAPPING 写死 /home/hermes/code/hermes-multitenancy/...)和
# webui 的 WorkingDirectory 都要走这条路径，落在窗口里的请求会直接失败。
# 唯一原子的做法是 rename(2)，也就是 `mv -T`（GNU 扩展）。
# 生产是 Linux，必须走原子路径；macOS 没有 -T，只在本地测试里退化，
# 并且这里会明说它非原子，免得以后有人以为两边行为一样。
if mv --help 2>&1 | grep -q -- "-T"; then ATOMIC_MV=1; else ATOMIC_MV=0; fi

relink() {  # $1=目标  $2=软链路径
  if [ "$ATOMIC_MV" = "1" ]; then
    ln -sfn "$1" "$2.new" || return 1
    mv -Tf "$2.new" "$2" || return 1      # rename(2)：路径任何一刻都指向一个有效版本
  else
    ln -sfn "$1" "$2" || return 1          # 非原子，仅本地测试
  fi
}

flip() {  # $1=mt 目标  $2=webui 目标
  relink "$1" "$CODE/hermes-multitenancy" || return 1
  relink "$2" "$CODE/hermes-web-ui" || return 1
  # 读回来核对：切换是这套系统里最不能"以为成功"的一步
  [ "$(readlink "$CODE/hermes-multitenancy")" = "$1" ] || return 1
  [ "$(readlink "$CODE/hermes-web-ui")" = "$2" ] || return 1
}

log "停服务 → 翻软链 → 起服务"
$SYSTEMCTL stop $(_units) 2>/dev/null || true
if ! flip "../releases/$(basename "$MT_DIR")" "../releases/$(basename "$WEBUI_DIR")"; then
  log "软链切换失败 —— 翻回原样并放弃本次发布"
  flip "$PREV_MT" "$PREV_WEBUI" || true
  $SYSTEMCTL start $(_units) 2>/dev/null || true
  outcome FLIP_FAILED
  die "切换失败，当前版本继续跑"
fi
$SYSTEMCTL start $(_units) 2>/dev/null || true
log "  已切到 $TAG，开始跑探针"

# ── 探针决定去留 ─────────────────────────────────────────────────────
# 用新版本自带的那份探针：判据要跟着被验的版本走
if [ ! -x "$NEW_PROBES" ]; then
  log "探针脚本消失了（$NEW_PROBES）—— 无法验证，按最坏情况回滚"
elif "$NEW_PROBES"; then
  echo "$TAG" > "$STATE_FILE"
  outcome SUCCESS
  log "RELEASE OK — $TAG 已生效"
  # 把执行器自身同步到「不会被翻转」的稳定路径，而且只在发布成功之后同步。
  # 否则执行器就住在它自己要改写的那棵树里：发一个坏版本，
  # 下次部署和回滚工具会一起坏掉 —— 连退路都没了。
  if [ -n "${STABLE_BIN:-}" ]; then
    mkdir -p "$STABLE_BIN"
    for f in hermes-release.sh hermes-release-probes.sh hermes_patch_probe.py hermes-backup.sh; do
      [ -f "$MT_DIR/deploy/$f" ] && install -m 755 "$MT_DIR/deploy/$f" "$STABLE_BIN/$f"
    done
    log "  执行器已同步到稳定路径 $STABLE_BIN（只在发布成功后同步）"
  fi
  # 保留最近 N 个版本目录，别把盘撑爆
  # 保护名单按【绝对路径精确比对】。子串匹配会误伤：
  # 比如 mt-df0 会被 mt-df07143 的链接串命中而永不裁剪，
  # 反过来也可能把该留的回滚目标当成无关目录删掉。
  KEEP_A=$(cd "$CODE" && cd "$(readlink hermes-multitenancy)" && pwd)
  KEEP_B=$(cd "$CODE" && cd "$(readlink hermes-web-ui)" && pwd)
  KEEP_C=$(cd "$RELEASES" && cd "$(basename "$PREV_MT")" 2>/dev/null && pwd || true)
  KEEP_D=$(cd "$RELEASES" && cd "$(basename "$PREV_WEBUI")" 2>/dev/null && pwd || true)
  for prefix in mt webui; do
    repo="$RELEASES/.repo-$prefix"
    mapfile -t olds < <(ls -1dt "$RELEASES/$prefix"-* 2>/dev/null | tail -n +$((KEEP_RELEASES + 1)))
    for d in "${olds[@]:-}"; do
      [ -n "$d" ] && [ -d "$d" ] || continue
      abs=$(cd "$d" && pwd)
      # 当前在用的、以及上一版（回滚目标）都不许删
      case "$abs" in
        "$KEEP_A"|"$KEEP_B"|"$KEEP_C"|"$KEEP_D") continue ;;
      esac
      git -C "$repo" worktree remove --force "$abs" 2>/dev/null || rm -rf "$abs"
      log "  裁剪旧版本 $(basename "$abs")"
    done
  done
  exit 0
fi

# ── 探针没过：自动翻回上一版 ─────────────────────────────────────────
log "探针失败 —— 自动回滚到上一版"
$SYSTEMCTL stop $(_units) 2>/dev/null || true
flip "$PREV_MT" "$PREV_WEBUI"
$SYSTEMCTL start $(_units) 2>/dev/null || true
# 回滚校验要用【旧版本自带】的探针 —— 现在跑的是旧版，判据就该跟着旧版走。
# （用新版的探针去验旧版是张冠李戴：新版可能改了期望值。）
OLD_PROBES="$CODE/hermes-multitenancy/deploy/$(basename "$PROBES")"
if [ -x "$OLD_PROBES" ] && "$OLD_PROBES"; then
  outcome ROLLED_BACK
  log "ROLLED BACK — 已退回 ${CURRENT:-上一版}，探针复检通过"
else
  outcome NEEDS_HUMAN
  log "ROLLED BACK 但复检仍未全过 —— 需要人工介入，回滚锚点在 $SNAP/ROLLBACK.txt"
fi
exit 1
