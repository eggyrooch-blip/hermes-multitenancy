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
UNITS="${UNITS:-hermes-gateway.service hermes-web-ui.service}"
DRY_RUN="${DRY_RUN:-0}"

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

mkdir -p "$RELEASES" "$BACKUP_ROOT" "$(dirname "$STATE_FILE")"
exec 9>"$LOCK"
flock -n 9 || { log "另一个发布正在进行，本次退出"; exit 0; }

# ── 生产上 .git 被 root 占是反复出现的坑，动 git 之前先归位 ──────────
for r in "$RELEASES/.repo-mt" "$RELEASES/.repo-webui"; do
  [ -d "$r" ] || continue
  if [ -n "$(find "$r" -not -user "$(id -un)" -print -quit 2>/dev/null)" ]; then
    log "修正 $r 的属主（root 占用是本机老坑）"
    chown -R "$(id -un):$(id -gn)" "$r" 2>/dev/null || true
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
if [ -x "$BACKUP_SH" ]; then
  SKIP_PROFILES=1 BACKUP_ROOT="$SNAP" "$BACKUP_SH" >"$SNAP/backup.log" 2>&1 \
    || die "发布前备份失败 —— 不带备份不发布"
fi
{ echo "tag=$TAG"; echo "prev_mt=$PREV_MT"; echo "prev_webui=$PREV_WEBUI"
  echo "new_mt=$MT_SHA"; echo "new_webui=$WEBUI_SHA"; } > "$SNAP/ROLLBACK.txt"
log "  回滚锚点已记录"

# ── 构建新版本目录（不碰正在跑的目录）────────────────────────────────
build_worktree() {  # $1=canonical 仓  $2=目标目录  $3=sha
  local repo="$1" dest="$2" sha="$3"
  [ -d "$dest" ] && { log "  $dest 已存在，复用"; return 0; }
  git -C "$repo" fetch -q --all --tags 2>/dev/null || true
  git -C "$repo" worktree add -q --detach "$dest" "$sha" || return 1
}

MT_DIR="$RELEASES/mt-${MT_SHA:0:7}"
WEBUI_DIR="$RELEASES/webui-${WEBUI_SHA:0:8}"

log "构建 multitenancy → $MT_DIR"
build_worktree "$RELEASES/.repo-mt" "$MT_DIR" "$MT_SHA" || die "multitenancy 检出失败"

log "构建 webui → $WEBUI_DIR（含 npm ci + build，要几分钟）"
if build_worktree "$RELEASES/.repo-webui" "$WEBUI_DIR" "$WEBUI_SHA"; then
  # .env 不在 git 里，指向跨版本稳定的那一份
  ln -sfn "$HOME/.hermes-web-ui/.env" "$WEBUI_DIR/.env"
  if [ ! -f "$WEBUI_DIR/dist/server/index.js" ]; then
    ( cd "$WEBUI_DIR" && npm ci --no-audit --no-fund >/dev/null 2>&1 && npm run build >/dev/null 2>&1 ) \
      || die "webui 构建失败 —— 没切换任何东西，当前版本继续跑"
  fi
  [ -f "$WEBUI_DIR/dist/server/index.js" ] || die "webui 构建产物缺失 —— 拒绝切换"
else
  die "webui 检出失败"
fi
log "  两个版本目录都就绪（此刻生产仍跑旧版）"

# ── 原子切换 ─────────────────────────────────────────────────────────
flip() {  # $1=mt 目标  $2=webui 目标
  ln -sfn "$1" "$CODE/.hermes-multitenancy.new" && mv -Tf "$CODE/.hermes-multitenancy.new" "$CODE/hermes-multitenancy"
  ln -sfn "$2" "$CODE/.hermes-web-ui.new" && mv -Tf "$CODE/.hermes-web-ui.new" "$CODE/hermes-web-ui"
}

log "停服务 → 翻软链 → 起服务"
$SYSTEMCTL stop $UNITS 2>/dev/null || true
flip "../releases/$(basename "$MT_DIR")" "../releases/$(basename "$WEBUI_DIR")"
$SYSTEMCTL start $UNITS 2>/dev/null || true
log "  已切到 $TAG，开始跑探针"

# ── 探针决定去留 ─────────────────────────────────────────────────────
if [ -x "$PROBES" ] && "$PROBES"; then
  echo "$TAG" > "$STATE_FILE"
  log "RELEASE OK — $TAG 已生效"
  # 保留最近 N 个版本目录，别把盘撑爆
  for prefix in mt webui; do
    mapfile -t olds < <(ls -1dt "$RELEASES/$prefix"-* 2>/dev/null | tail -n +$((KEEP_RELEASES + 1)))
    for d in "${olds[@]:-}"; do
      [ -n "$d" ] || continue
      case "$(readlink "$CODE/hermes-multitenancy")$(readlink "$CODE/hermes-web-ui")" in
        *"$(basename "$d")"*) continue ;;   # 正在用的绝不删
      esac
      git -C "$RELEASES/.repo-${prefix/mt/mt}" worktree remove --force "$d" 2>/dev/null || rm -rf "$d"
      log "  裁剪旧版本 $(basename "$d")"
    done
  done
  exit 0
fi

# ── 探针没过：自动翻回上一版 ─────────────────────────────────────────
log "探针失败 —— 自动回滚到上一版"
$SYSTEMCTL stop $UNITS 2>/dev/null || true
flip "$PREV_MT" "$PREV_WEBUI"
$SYSTEMCTL start $UNITS 2>/dev/null || true
if [ -x "$PROBES" ] && "$PROBES"; then
  log "ROLLED BACK — 已退回 ${CURRENT:-上一版}，探针复检通过"
else
  log "ROLLED BACK 但复检仍未全过 —— 需要人工介入，回滚锚点在 $SNAP/ROLLBACK.txt"
fi
exit 1
