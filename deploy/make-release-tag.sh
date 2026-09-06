#!/usr/bin/env bash
# make-release-tag.sh — 安全地打 release-* 标签（生产发布器每日 18:00 拉最新标签部署）
#
# 用法（在 hermes-multitenancy 仓里，需要 Maintainer 推受保护标签的权限）：
#   deploy/make-release-tag.sh [-m "说明"] [--mt <ref>] [--webui <sha>] [--dry-run]
#
# 标签正文是双仓全量快照，发布器只认它：
#   multitenancy: <40 位 SHA>   ← 只来自 git rev-parse（默认 origin/main），永不手写
#   webui: <40 位 SHA>          ← 默认照抄上一条 release 标签（= webui 不动）
# 拒绝两种标签：mt SHA 与上一条相同且 webui 未变（空发布）；mt 不是上一条的后代（回退方向）。
# 线上 ≠ 上一条标签时发布器自己会用漂移熔断拒发，本脚本不绕它。
set -euo pipefail

die() { printf 'make-release-tag: %s\n' "$*" >&2; exit 1; }

MT_REF="origin/main"
WEBUI_SHA=""
MESSAGE=""
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    -m|--message) [ $# -ge 2 ] || die "$1 需要参数"; MESSAGE="$2"; shift 2 ;;
    --mt)         [ $# -ge 2 ] || die "$1 需要参数"; MT_REF="$2"; shift 2 ;;
    --webui)      [ $# -ge 2 ] || die "$1 需要参数"; WEBUI_SHA="$2"; shift 2 ;;
    --dry-run)    DRY_RUN=1; shift ;;
    -h|--help)    sed -n '2,12p' "$0"; exit 0 ;;
    *) die "未知参数: $1" ;;
  esac
done

is_sha40() { printf '%s' "$1" | grep -Eq '^[0-9a-f]{40}$'; }

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "不在 git 仓库里"
git fetch -q --tags --prune origin || die "fetch origin 失败"

MT_SHA=$(git rev-parse --verify "${MT_REF}^{commit}" 2>/dev/null) \
  || die "解析不到 $MT_REF"

LAST_TAG=$(git tag -l 'release-*' --sort=-creatordate | head -1)
[ -n "$LAST_TAG" ] || die "仓里没有任何 release-* 标签，首个标签请手工打"
LAST_BODY=$(git tag -l --format='%(contents)' "$LAST_TAG")
LAST_MT=$(printf '%s\n' "$LAST_BODY" | sed -n 's/^multitenancy:[[:space:]]*//p' | head -1)
LAST_WEBUI=$(printf '%s\n' "$LAST_BODY" | sed -n 's/^webui:[[:space:]]*//p' | head -1)
is_sha40 "$LAST_MT"    || die "$LAST_TAG 正文缺 multitenancy: <40 位 SHA>，拒绝在残缺清单上续打"
is_sha40 "$LAST_WEBUI" || die "$LAST_TAG 正文缺 webui: <40 位 SHA>，拒绝在残缺清单上续打"

if [ -z "$WEBUI_SHA" ]; then
  WEBUI_SHA="$LAST_WEBUI"
else
  is_sha40 "$WEBUI_SHA" || die "--webui 必须是 40 位全 SHA（别用短 SHA 脑补）: $WEBUI_SHA"
fi

if [ "$MT_SHA" = "$LAST_MT" ] && [ "$WEBUI_SHA" = "$LAST_WEBUI" ]; then
  die "$LAST_TAG 已钉相同的 SHA（mt ${MT_SHA:0:12} / webui ${WEBUI_SHA:0:12}），无需新发布"
fi
git merge-base --is-ancestor "$LAST_MT" "$MT_SHA" 2>/dev/null \
  || die "拒绝回退方向的标签：$MT_REF (${MT_SHA:0:12}) 不是 $LAST_TAG 的 multitenancy (${LAST_MT:0:12}) 的后代。回滚是手动决策，不走本脚本"

TODAY=$(date +%Y%m%d)
N=1
while git rev-parse -q --verify "refs/tags/release-${TODAY}-$(printf '%02d' "$N")" >/dev/null 2>&1; do
  N=$((N + 1))
done
TAG="release-${TODAY}-$(printf '%02d' "$N")"

if [ -z "$MESSAGE" ]; then
  MESSAGE=$(git log --format='%s' "${LAST_MT}..${MT_SHA}" | grep -v '^Merge branch ' || true)
  [ -n "$MESSAGE" ] || MESSAGE=$(git log --format='%s' "${LAST_MT}..${MT_SHA}")
  [ -n "$MESSAGE" ] || MESSAGE="webui -> ${WEBUI_SHA:0:12}（multitenancy 无变更）"
fi
BODY=$(printf 'multitenancy: %s\nwebui: %s\n\n%s\n' "$MT_SHA" "$WEBUI_SHA" "$MESSAGE")

printf '%s\n' "$TAG" "$BODY"
if [ "$DRY_RUN" = 1 ]; then
  printf '\n[dry-run] 未创建、未推送\n'
  exit 0
fi

git tag -a "$TAG" "$MT_SHA" -m "$BODY" || die "打标签失败"
git push origin "refs/tags/$TAG" || { git tag -d "$TAG" >/dev/null; die "推送失败（需要 Maintainer 权限），本地标签已撤"; }
printf '\n已推送 %s。生产发布器每日 18:00 自动拉最新标签部署；需要立刻上线找 sunke 手动触发。\n' "$TAG"
