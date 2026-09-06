#!/usr/bin/env bash
# release-cut — 生成 release-* 双仓清单标签的唯一入口
#
# 为什么存在:release-* 标签的 annotation 同时钉死 multitenancy 和 webui 两个仓
# 的 SHA,发布器(deploy/hermes-release.sh)照单全收。手打标签出过真实事故:复制
# 上一个标签里没动的那个仓的 SHA,而生产实际在跑更新的版本 → 发布即静默回退。
# 本工具把打标收口:
#   - 默认取两仓 fetch 后的 origin/main 最新提交
#   - 打印每个仓相对当前标签前进/后退多少个提交
#   - 任一仓后退或历史分叉,默认硬拒;显式 --allow-rollback 才放行
#   - 落盘后用发布器同款解析逻辑自校验,解析不出就删标签退出
#
# 用法:
#   release-cut.sh [--repo-mt <path>] [--repo-webui <path>]
#                  [--mt-ref <ref>] [--webui-ref <ref>]
#                  [--summary "一句话"] [--allow-rollback] [--push] [--dry-run]
# 默认 repo 路径可用环境变量 RELEASE_CUT_REPO_MT / RELEASE_CUT_REPO_WEBUI 覆盖。
set -euo pipefail

log() { printf '[release-cut] %s\n' "$*"; }
die() { printf '[release-cut] FATAL: %s\n' "$*" >&2; exit 1; }

REPO_MT="${RELEASE_CUT_REPO_MT:-$HOME/code/hermes-multitenancy}"
REPO_WEBUI="${RELEASE_CUT_REPO_WEBUI:-$HOME/code/hermes-web-ui}"
MT_REF="origin/main"
WEBUI_REF="origin/main"
SUMMARY=""
ALLOW_ROLLBACK=0
PUSH=0
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --repo-mt)        REPO_MT="$2"; shift 2 ;;
    --repo-webui)     REPO_WEBUI="$2"; shift 2 ;;
    --mt-ref)         MT_REF="$2"; shift 2 ;;
    --webui-ref)      WEBUI_REF="$2"; shift 2 ;;
    --summary)        SUMMARY="$2"; shift 2 ;;
    --allow-rollback) ALLOW_ROLLBACK=1; shift ;;
    --push)           PUSH=1; shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
    -h|--help)        sed -n '2,20p' "$0"; exit 0 ;;
    *) die "未知参数:$1" ;;
  esac
done

[ -d "$REPO_MT/.git" ] || [ -f "$REPO_MT/.git" ] || die "不是 git 仓:$REPO_MT"
[ -d "$REPO_WEBUI/.git" ] || [ -f "$REPO_WEBUI/.git" ] || die "不是 git 仓:$REPO_WEBUI"

# 打标必须基于最新远端状态 —— 基于陈旧本地打标,就是"复制旧标签"事故的另一种形状。
for r in "$REPO_MT" "$REPO_WEBUI"; do
  git -C "$r" fetch -q --tags --prune origin || die "fetch origin 失败($r)"
done

MT_SHA=$(git -C "$REPO_MT" rev-parse --verify --quiet "${MT_REF}^{commit}") \
  || die "multitenancy ref 无法解析:$MT_REF"
WEBUI_SHA=$(git -C "$REPO_WEBUI" rev-parse --verify --quiet "${WEBUI_REF}^{commit}") \
  || die "webui ref 无法解析:$WEBUI_REF"

# 与发布器同款:最新 release-* 标签就是"当前版本"基线。
# 主键仍是 -creatordate(发布器同款);同一秒打的两个标签 creatordate 相同,
# 平局顺序不稳会把 CURRENT 指到旧标签,所以加 -v:refname 做平局决胜键
# (git 多 --sort 时最后一个是主键,前面的是决胜键)。
CURRENT=$(git -C "$REPO_MT" tag -l 'release-*' --sort=-v:refname --sort=-creatordate | head -1)

# 返回新旧提交的相对移动:"0" / "+N" / "-N" / "±分叉"
movement() { # $1=repo $2=old $3=new
  if [ "$2" = "$3" ]; then printf '0'
  elif git -C "$1" merge-base --is-ancestor "$2" "$3" 2>/dev/null; then
    printf '+%s' "$(git -C "$1" rev-list --count "$2..$3")"
  elif git -C "$1" merge-base --is-ancestor "$3" "$2" 2>/dev/null; then
    printf '%s' "-$(git -C "$1" rev-list --count "$3..$2")"
  else printf '±分叉'
  fi
}

MT_MOV="(首个标签)"; WEBUI_MOV="(首个标签)"
if [ -n "$CURRENT" ]; then
  BODY=$(git -C "$REPO_MT" tag -l --format='%(contents)' "$CURRENT")
  CUR_MT=$(printf '%s\n' "$BODY" | sed -n 's/^multitenancy:[[:space:]]*//p' | head -1)
  CUR_WEBUI=$(printf '%s\n' "$BODY" | sed -n 's/^webui:[[:space:]]*//p' | head -1)
  # 坏基线上不打新标 —— --allow-rollback 也不放行,残缺清单只能人工修
  { [ ${#CUR_MT} -eq 40 ] && [ ${#CUR_WEBUI} -eq 40 ]; } \
    || die "当前标签 $CURRENT 的清单残缺(mt='${CUR_MT}' webui='${CUR_WEBUI}')—— 先人工修复基线"
  git -C "$REPO_MT" cat-file -e "$CUR_MT^{commit}" 2>/dev/null \
    || die "当前标签钉的 multitenancy SHA 本地不可达($CUR_MT)"
  git -C "$REPO_WEBUI" cat-file -e "$CUR_WEBUI^{commit}" 2>/dev/null \
    || die "当前标签钉的 webui SHA 本地不可达($CUR_WEBUI)"

  MT_MOV=$(movement "$REPO_MT" "$CUR_MT" "$MT_SHA")
  WEBUI_MOV=$(movement "$REPO_WEBUI" "$CUR_WEBUI" "$WEBUI_SHA")

  if [ "$MT_MOV" = "0" ] && [ "$WEBUI_MOV" = "0" ]; then
    die "无可发布内容:两仓相对 $CURRENT 都没有新提交"
  fi
  for pair in "multitenancy:$MT_MOV" "webui:$WEBUI_MOV"; do
    name=${pair%%:*}; mov=${pair#*:}
    case "$mov" in
      -*|*分叉*)
        if [ "$ALLOW_ROLLBACK" != "1" ]; then
          die "拒绝:$name 相对 $CURRENT 将回退/分叉($mov 个提交)。这正是「复制旧标签」类事故的形状;确认是有意回滚就加 --allow-rollback 重跑"
        fi
        log "!! $name 回退/分叉($mov)—— --allow-rollback 已确认,放行" ;;
    esac
  done
fi

# 标签名沿用现网约定:release-YYYYMMDD-NN(NN 两位、当日自增)
DAY=$(date +%Y%m%d)
LAST_N=$(git -C "$REPO_MT" tag -l "release-$DAY-*" | sed "s/^release-$DAY-//" | sort -n | tail -1)
TAG=$(printf 'release-%s-%02d' "$DAY" $(( 10#${LAST_N:-0} + 1 )))

[ -n "$SUMMARY" ] || SUMMARY="release-cut: multitenancy $MT_MOV, webui $WEBUI_MOV"
ANNOT="multitenancy: $MT_SHA
webui: $WEBUI_SHA

$SUMMARY"

log "将创建 $TAG(当前:${CURRENT:-无})"
log "  multitenancy: ${MT_SHA:0:12} ($MT_MOV)"
log "  webui:        ${WEBUI_SHA:0:12} ($WEBUI_MOV)"
if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN=1,到此为止"; exit 0; fi

git -C "$REPO_MT" tag -a "$TAG" -m "$ANNOT" "$MT_SHA"

# 自校验:用发布器一字不差的解析逻辑读回来。解析不出 = 发布器也解析不出。
CHK_BODY=$(git -C "$REPO_MT" tag -l --format='%(contents)' "$TAG")
CHK_MT=$(printf '%s\n' "$CHK_BODY" | sed -n 's/^multitenancy:[[:space:]]*//p' | head -1)
CHK_WEBUI=$(printf '%s\n' "$CHK_BODY" | sed -n 's/^webui:[[:space:]]*//p' | head -1)
if [ "$CHK_MT" != "$MT_SHA" ] || [ "$CHK_WEBUI" != "$WEBUI_SHA" ]; then
  git -C "$REPO_MT" tag -d "$TAG" >/dev/null 2>&1 || true
  die "自校验失败:发布器将解析不出正确清单 —— 标签已删除,不落盘"
fi
log "✔ $TAG 已创建,清单自校验通过"

if [ "$PUSH" = "1" ]; then
  git -C "$REPO_MT" push origin "$TAG" || die "push 失败 —— 标签仍在本地($TAG),修好网络后手动 push"
  log "✔ 已推送 origin $TAG"
else
  log "下一步(确认无误后):git -C $REPO_MT push origin $TAG"
fi
