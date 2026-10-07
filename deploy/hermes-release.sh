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
# 发布前回滚包保留几份。一份 ~170MB，133 份 = 22G（2026-09-21 实测），
# 而 hermes-backup.sh 自己的 prune() 够不着这一层（见 prune_prerelease_backups）。
# 不设 = 一份都不裁：「不删备份」是红线，上限只能由 sunke 显式打开（2026-09-24）。
KEEP_PRERELEASE_BACKUPS="${KEEP_PRERELEASE_BACKUPS:-}"
# 版本目录裁剪除了「当前 + 上一版」还要认两类依赖（2026-09-24 core 0.21.4 切换）：
#   钉住清单 $RELEASES/.keep-pins —— 每行一个目录名，# 注释；发布器只读不写，缺文件 = 空
#   活配置引用 —— 固定位置的配置里写死的 $RELEASES/<目录>（router/profile config.yaml
#   的 MCP 命令、gateway drop-in）。只读这几个已知路径，不做全盘扫描（机械盘 IOPS）。
KEEP_PINS="${KEEP_PINS:-$RELEASES/.keep-pins}"
RELEASE_REF_HERMES_HOME="${RELEASE_REF_HERMES_HOME:-$HOME/.hermes}"
RELEASE_REF_SYSTEMD_DIR="${RELEASE_REF_SYSTEMD_DIR:-$HOME/.config/systemd/user}"
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
# ── relay：唯一不走软链的组件 ────────────────────────────────────────
# hermes-agent-relay 是 root 装的【系统级】unit，代码被拷成另一个包名
# hermes_agent_relay_runtime 装进 /opt 下自己的 venv，跟 mt/webui 的
# releases/<sha> + 软链形态完全不同，所以它必须在这里被显式地跟一次。
# 在此之前它根本不在流水线里：2026-08-13 合进 main 的四个 relay 提交在生产上
# 整整两天没生效，进程还是 8-12 起的那个，靠人手拷文件才止血。
# relay 的 User=hermes，所以把包目录交给 hermes 不产生任何提权；唯一需要 root
# 的只有重启，用一条无参 sudoers drop-in 放行（见 .ftask SPEC 的部署前置）。
RELAY_PKG_DIR="${RELAY_PKG_DIR:-/opt/hermes-agent-relay/venv/lib/python3.11/site-packages/hermes_agent_relay_runtime}"
RELAY_RESTART="${RELAY_RESTART:-sudo -n systemctl restart hermes-agent-relay.service}"
RELAY_IS_ACTIVE="${RELAY_IS_ACTIVE:-systemctl is-active --quiet hermes-agent-relay.service}"
RELAY_PROBE_URL="${RELAY_PROBE_URL:-http://127.0.0.1:8770/v1/messages/__release_probe__}"
# relay 自己 venv 里的解释器：拷完文件、重启之前先用它 import 一次。2026-09-17
# release-20260917-02：agent_relay_store.py 新增 `from .shared_db import …`，
# shared_db.py 不在同步清单里，重启后新进程 ModuleNotFoundError 崩溃循环 16 次，
# 探针等满 90s 才判失败 —— 那 90 秒 relay 已经死了。预检在老进程还活着时把它拦下。
RELAY_PY="${RELAY_PY:-/opt/hermes-agent-relay/venv/bin/python}"
# 扫依赖用的解释器（只做 ast 静态分析，不 import 任何东西），跟 relay 的 venv 无关。
RELAY_SCAN_PY="${RELAY_SCAN_PY:-python3}"
RELAY_IMPORT_TARGET="${RELAY_IMPORT_TARGET:-hermes_agent_relay_runtime.agent_relay}"
# 打一次已注册的路由，把 HTTP 状态码打到 stdout。--noproxy 是硬性的：
# 本机的 http_proxy 已经把这类探针坑成 000 好几次了。
_relay_curl_probe() {
  curl -s --noproxy '*' -o /dev/null -w '%{http_code}' -m 5 \
    -X PATCH "$RELAY_PROBE_URL" -H 'Content-Type: application/json' \
    -d '{"reply_window_seconds":0}' 2>/dev/null || echo 000
}
RELAY_PROBE_CMD="${RELAY_PROBE_CMD:-_relay_curl_probe}"
# 覆盖「重启后还没 bind」那一段。15s 是错的，两次假失败（2026-08-15
# release-20260815-01、2026-08-27 release-20260827-01）都栽在同一个可算的错配上：
# relay 的 aiohttp on_startup 里 start_event_stream 同步阻塞等飞书 ws 连上
# （agent_relay_feishu.py: EVENT_STREAM_START_TIMEOUT_SECONDS = 10），失败路径再
# thread.join(timeout=5)，而 aiohttp 是【跑完 on_startup 才 bind socket】。所以飞书
# 连接不顺时 relay 要 10+5=15s 才放弃 ws、之后才可能 bind —— 上限恰好等于探针的上限，
# 探针必然先超时。下界因此是 ws timeout + join；再乘余量盖住冷启动（lark_oapi /
# aiohttp / cryptography 的 import）与飞书抖动。tests/test_release_relay_probe.py
# 里有守卫：relay 侧把 ws 上限调大而这里没跟上就红。
# 代价只有「relay 真死时晚 75s 报红」，换掉的是一整类假失败 + 无谓的 relay 还原。
RELAY_PROBE_TIMEOUT="${RELAY_PROBE_TIMEOUT:-90}"
# 探针等待逻辑住在自己的文件里：本脚本是顶层直接执行的，source 它就等于跑一次真发布，
# 所以那段判定过去无法自动化覆盖（2026-08-15 的假失败就出在那里）。
# shellcheck source=deploy/relay-probe-lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/relay-probe-lib.sh"

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ── 与 core 配对切换的两个开关（默认 1 = 行为不变）─────────────────────
# core 与 MT 在同一次停服里一起换时（2026-09-24 core 0.21.4），自动回滚会把旧 MT
# 装进【新 core 的 venv】再起服务 —— 旧 MT 在新 core 上已知会坏，而旧版探针只查端口
# 之类，可能判 ROLLED_BACK 假绿。所以配对切换时：
#   AUTO_ROLLBACK=0  任何一处失败都不翻回 MT/WebUI、不重装旧 editable，只停服务、
#                    记 outcome=NEEDS_HUMAN_CORE_PAIRED、非零退出，由人整体回退
#                    core + MT + WebUI + 数据库（停服而不是保持运行：不让半套新版本
#                    或「旧 MT + 新 core」对外服务）。
#   DEP_HEAL=0       uv pip check 不过直接判失败，不带依赖重装（那会让 MT 重新解析、
#                    改写 core 的依赖环境）。
# 只认 0/1：拼错成 false/no 不能被静默当成「开着」。
AUTO_ROLLBACK="${AUTO_ROLLBACK:-1}"
DEP_HEAL="${DEP_HEAL:-1}"
case "$AUTO_ROLLBACK" in 0|1) ;; *) die "AUTO_ROLLBACK 只能是 0 或 1（现在是 '$AUTO_ROLLBACK'）" ;; esac
case "$DEP_HEAL" in 0|1) ;; *) die "DEP_HEAL 只能是 0 或 1（现在是 '$DEP_HEAL'）" ;; esac

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

TAG=$(git -C "$RELEASES/.repo-mt" tag -l 'release-*' --sort=-refname --sort=-creatordate | head -1)
CURRENT=$(cat "$STATE_FILE" 2>/dev/null || echo "")

# ── 漂移探针：活的软链，是不是真等于当前标签钉的 SHA ──────────────────
# 本执行器判断「当前已部署版本」用的是状态文件里的一个**标签名**，不是活的
# 软链。于是任何带外部署（ssh 上去 build + ln -sfn + restart）都没有任何东西
# 会发现，而且会一直挂着，直到下一个新标签把两个仓一起翻掉。2026-08-04 生产
# webui 就是这么漂的：软链指着 webui-6aca93cc，而当时最新标签 -02 的
# annotation 记的是 290c1152，直到 -03 才被动钉回一致，全程零告警。
#
# 三条设计决定，都是评审推翻第一版后定的：
#  1) fail-closed。只有「从没部署过」才允许跳过。取不到清单、软链悬空、名字
#     判读不出，一律算「无法证明一致」并失败 —— 否则自定义目录名就是绕过探针
#     的方法，而「取不到清单时发布路径自己会拒」在 TAG==CURRENT 时根本不成立
#     （那条路径在校验清单之前就早退了）。
#  2) 挡在所有分支前面，不只是无新标签那条。带着未确认的漂移继续发布，会把
#     别人手工部上去的止血补丁静默盖掉。
#  3) 只报不改。带外部署可能正是当时唯一的止血手段，静默翻回去等于二次事故。
DRIFT_LOG="${DRIFT_LOG:-$HOME/.hermes/release-drift.log}"
DRIFT_ACK="${DRIFT_ACK:-$STATE_FILE.drift-ack}"
DRIFT_FINGERPRINT=""

# 软链名 → 短 sha。发布器两个仓的宽度**不一样**（mt 取 7 位、webui 取 8 位，
# 见下方 MT_DIR/WEBUI_DIR），所以这里两种都认，并按实际位数去截期望值比。
# 写死 8 位的第一版在生产上 mt 永远判读不出 → 整条探针空转，评审实测抓到。
_link_sha() {
  local name="$1" prefix="$2" rest
  rest="${name#"$prefix"-}"
  [ "$rest" != "$name" ] || return 0
  case "${#rest}" in 7|8) ;; *) return 0 ;; esac
  case "$rest" in *[!0-9a-f]*) return 0 ;; esac
  printf '%s' "$rest"
}

# 返回 0 = 已证明一致（或确认过）；1 = 漂了 / 无法证明。
drift_check() {
  [ -n "$CURRENT" ] || return 0   # 从没部署过，无从比较，是唯一允许跳过的情形

  local body cur_mt cur_webui live_mt live_webui live_mt_sha live_webui_sha
  local want_mt want_webui reason
  reason=""; want_mt="?"; want_webui="?"

  # 先把「实际在跑什么」问清楚，且**无条件**问 —— 它与标签读不读得到无关。
  # 早期版本把这段塞在 `[ -z "$reason" ]` 后面，于是标签一旦读不到，指纹就退化成
  # `<标签> ? ? <无> <无>`：ack 掉这一条之后，只要标签仍读不到，软链随便怎么换都
  # 静默放行。评审 round-2 实测出来的洞 —— 指纹里必须永远带真实的那一对。
  live_mt=$(readlink "$CODE/hermes-multitenancy" 2>/dev/null || true)
  live_webui=$(readlink "$CODE/hermes-web-ui" 2>/dev/null || true)
  live_mt=$(basename "${live_mt:-}")
  live_webui=$(basename "${live_webui:-}")

  body=$(git -C "$RELEASES/.repo-mt" tag -l --format='%(contents)' "$CURRENT" 2>/dev/null)
  cur_mt=$(printf '%s\n' "$body" | sed -n 's/^multitenancy:[[:space:]]*//p' | head -1)
  cur_webui=$(printf '%s\n' "$body" | sed -n 's/^webui:[[:space:]]*//p' | head -1)
  if [ ${#cur_mt} -ne 40 ] || [ ${#cur_webui} -ne 40 ]; then
    reason="无法证明一致：取不到 $CURRENT 的完整发布清单（标签被删，或 annotation 残缺）"
  fi

  # 悬空软链 readlink 照样回显目标名，只比名字会把「目标已被删」判成一致。
  if [ -z "$reason" ]; then
    if [ ! -L "$CODE/hermes-multitenancy" ] || [ ! -d "$CODE/hermes-multitenancy/." ]; then
      reason="无法证明一致：$CODE/hermes-multitenancy 不是软链，或已悬空"
    elif [ ! -L "$CODE/hermes-web-ui" ] || [ ! -d "$CODE/hermes-web-ui/." ]; then
      reason="无法证明一致：$CODE/hermes-web-ui 不是软链，或已悬空"
    fi
  fi

  if [ -z "$reason" ]; then
    live_mt_sha=$(_link_sha "$live_mt" mt)
    live_webui_sha=$(_link_sha "$live_webui" webui)
    if [ -z "$live_mt_sha" ] || [ -z "$live_webui_sha" ]; then
      reason="无法证明一致：软链名不是发布器的 <仓>-<短sha> 命名（mt=$live_mt webui=$live_webui）"
    else
      want_mt="mt-${cur_mt:0:${#live_mt_sha}}"
      want_webui="webui-${cur_webui:0:${#live_webui_sha}}"
      [ "$live_mt" = "$want_mt" ] && [ "$live_webui" = "$want_webui" ] && return 0
      reason="发布漂移：$CURRENT 钉的是 $want_mt / $want_webui，实际在跑的是 $live_mt / $live_webui"
    fi
  fi

  log "!! $reason"
  mkdir -p "$(dirname "$DRIFT_LOG")" 2>/dev/null || true
  printf '%s\t%s\n' "$(date -Is)" "$reason" >> "$DRIFT_LOG" 2>/dev/null || true

  # 确认过的不再天天告警（告警疲劳的终点是有人把定时器关了）。指纹绑死
  # 「基准标签 + 期望的那一对 + 实际的那一对」：换了标签、或又漂到别处，
  # 旧 ack 一律失效。只绑 live 那一对的第一版会跨发布永久静默，评审抓到。
  DRIFT_FINGERPRINT="$CURRENT $want_mt $want_webui ${live_mt:-<无>} ${live_webui:-<无>}"
  if [ "$(cat "$DRIFT_ACK" 2>/dev/null)" = "$DRIFT_FINGERPRINT" ]; then
    log "   （已在 $DRIFT_ACK 里确认过这一条，不再告警）"
    return 0
  fi
  return 1
}

if ! drift_check; then
  log "!! 生产在跑的东西无法证明等于 $CURRENT 钉的版本 —— 不发布、不动软链，以失败退出让 OnFailure 捅出来。"
  log "!! 确认这次是有意的（例如紧急止血），把下面这行原样写进 ack 文件后重跑："
  log "     printf '%s' '$DRIFT_FINGERPRINT' > $DRIFT_ACK"
  exit 1
fi

[ -n "$TAG" ] || { log "没有任何 release-* 标签，什么都不做"; exit 0; }
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
# 回滚目标必须真的存在。悬空的话，一旦新版本探针失败，回滚会把两个对外路径
# 指到不存在的目录上 —— 比不回滚还糟。要拒就在动任何东西之前拒。
[ -d "$CODE/hermes-multitenancy/." ] || die "当前 multitenancy 软链悬空（$PREV_MT）—— 没有可用的回滚目标，拒绝发布"
[ -d "$CODE/hermes-web-ui/." ] || die "当前 webui 软链悬空（$PREV_WEBUI）—— 没有可用的回滚目标，拒绝发布"
PREV_MT_ABS=$(cd "$CODE/hermes-multitenancy/." && pwd -P)

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

# ── 裁剪历史回滚包 ───────────────────────────────────────────────────
# hermes-backup.sh 自己的 prune() 是相对 BACKUP_ROOT 的，而这条路径把
# BACKUP_ROOT 顶成了 $SNAP（每个 tag 一个目录）—— 于是它只在单个 tag 内裁剪，
# 从不删旧 tag，133 份、22G 只增不减。保留规则只能落在这一层。
#
# 保护名单跟版本目录裁剪同源：按【绝对路径精确比对】，当前 tag 与回滚锚点
# （= 此刻真正在跑的那个 tag，ROLLBACK.txt 里 tag= 的那一行）永不删。
# 只动 $BACKUP_ROOT 的【直接子目录】里名字形如 release-* 的；别的一律留着 ——
# 误删一次就是把出事时唯一的退路删掉，宁可留垃圾。
#
# 受保护的那两份【先占名额】，剩下的名额才按 mtime 从新到旧分给普通目录。
# 第一版是"数够 keep_n 就开始删、遇到受保护的额外放过"，于是回滚锚点一旦是
# 最老的那份，结果就是 keep_n+1 份 —— 上限形同虚设，盘照样慢慢涨。
prune_prerelease_backups() {
  local root="$1" keep_tag="$2" prev_tag="$3"
  local keep_n="${KEEP_PRERELEASE_BACKUPS:-}"
  [ -d "$root" ] || return 0
  if [ -z "$keep_n" ]; then
    log "  未设 KEEP_PRERELEASE_BACKUPS，不裁回滚包（不删备份；要裁须显式设正整数）"
    return 0
  fi
  case "$keep_n" in
    ''|*[!0-9]*) log "  KEEP_PRERELEASE_BACKUPS=$keep_n 不是正整数，跳过裁剪"; return 0 ;;
  esac
  [ "$keep_n" -ge 1 ] || { log "  KEEP_PRERELEASE_BACKUPS=$keep_n < 1，跳过裁剪"; return 0; }
  local root_abs
  root_abs=$(cd "$root" 2>/dev/null && pwd -P) || return 0
  local listing
  # -t = 按 mtime 从新到旧；尾部斜杠让它只列目录。
  listing=$(ls -1dt "$root_abs"/*/ 2>/dev/null || true)
  [ -n "$listing" ] || return 0
  # 先把真实存在的受保护目录点清楚（去重；不存在的不占名额）
  local prot_a="" prot_b=""
  case "$keep_tag" in
    release-*) [ -d "$root_abs/$keep_tag" ] && prot_a="$keep_tag" ;;
  esac
  case "$prev_tag" in
    release-*) [ -d "$root_abs/$prev_tag" ] && [ "$prev_tag" != "$prot_a" ] \
                 && prot_b="$prev_tag" ;;
  esac
  local n_prot=0
  [ -n "$prot_a" ] && n_prot=$((n_prot + 1))
  [ -n "$prot_b" ] && n_prot=$((n_prot + 1))
  local ordinary_slots=$((keep_n - n_prot))
  if [ "$ordinary_slots" -lt 0 ]; then
    # 名额比受保护的还少：保护优先，上限让路，但必须说出来，
    # 否则"为什么还剩 2 份"下次又要有人去翻代码。
    log "  KEEP_PRERELEASE_BACKUPS=$keep_n 小于受保护的 $n_prot 份（当前 tag + 回滚锚点）—— 只留受保护的这 $n_prot 份"
    ordinary_slots=0
  fi
  local kept=0 removed=0 ordinary_kept=0 d name abs
  while IFS= read -r d; do
    [ -n "$d" ] || continue
    d="${d%/}"
    name=$(basename "$d")
    case "$name" in
      release-*) ;;
      *) log "  保留 $name（不是 release-* 目录，不裁）"; continue ;;
    esac
    # 受保护的两份：名额已经预留好了，永远不删
    if [ -n "$prot_a" ] && [ "$name" = "$prot_a" ]; then
      kept=$((kept + 1)); continue
    fi
    if [ -n "$prot_b" ] && [ "$name" = "$prot_b" ]; then
      kept=$((kept + 1)); continue
    fi
    if [ "$ordinary_kept" -lt "$ordinary_slots" ]; then
      ordinary_kept=$((ordinary_kept + 1)); kept=$((kept + 1)); continue
    fi
    abs=$(cd "$d" 2>/dev/null && pwd -P) || { log "  跳过 $name（进不去）"; continue; }
    # 防御：只删直接子目录。软链指到别处时这里会对不上，直接拒。
    if [ "$abs" != "$root_abs/$name" ]; then
      log "  拒删 $name —— 解析出的 $abs 不是 $root_abs 的直接子目录"
      continue
    fi
    # 备份目录里可能有 0444 的只读文件，rm -rf 会 permission denied（backup 侧同款坑）
    chmod -R u+w "$abs" 2>/dev/null || true
    if rm -rf "$abs"; then
      removed=$((removed + 1)); log "  裁剪回滚包 $name"
    else
      log "  裁剪 $name 失败（保留）"
    fi
  done <<< "$listing"
  log "pre-release 备份保留 ${kept} 份，删除 ${removed} 份"
}
prune_prerelease_backups "$BACKUP_ROOT" "$TAG" "$CURRENT"

# 钉住清单：每行一个 $RELEASES 下的目录名，# 起为注释，空白忽略。缺文件 = 空清单。
release_keep_pins() {
  [ -f "$KEEP_PINS" ] && [ -r "$KEEP_PINS" ] || return 0
  sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$KEEP_PINS" 2>/dev/null \
    | grep -E '^[A-Za-z0-9._-]+$' || true
}

# 活配置引用：只读固定位置，抽出写死的 $RELEASES/<目录>，输出「目录名<TAB>文件」。
# 文件缺失、读不了都跳过，不报错、不中止发布。
release_config_refs() {
  local hh="$RELEASE_REF_HERMES_HOME" sd="$RELEASE_REF_SYSTEMD_DIR"
  local files=() f p rel re="" esc
  for f in "$hh/config.yaml" "$hh"/profiles/*/config.yaml \
           "$sd"/*.service.d/*.conf "$sd"/*.service; do
    [ -f "$f" ] && [ -r "$f" ] && files+=("$f")
  done
  [ "${#files[@]}" -gt 0 ] || return 0
  # 同一个目录在配置里可能写成绝对路径、解析后的真实路径、systemd 的 %h 或 ~
  local prefixes=("$RELEASES")
  p=$(cd "$RELEASES" 2>/dev/null && pwd -P) && [ "$p" != "$RELEASES" ] && prefixes+=("$p")
  case "$RELEASES" in
    "$HOME"/*) rel="${RELEASES#"$HOME"/}"; prefixes+=("%h/$rel" "~/$rel") ;;
  esac
  for p in "${prefixes[@]}"; do
    esc=$(printf '%s' "$p" | sed 's/[][\.*^$+?(){}|]/\\&/g')
    re="${re:+$re|}$esc"
  done
  # 逐文件 grep：文件名直接来自循环变量，不从 grep 输出里解析（路径可能带冒号）
  for f in "${files[@]}"; do
    grep -oE "($re)/[A-Za-z0-9._-]+" -- "$f" 2>/dev/null \
      | while IFS= read -r line; do printf '%s\t%s\n' "${line##*/}" "$f"; done
  done
  return 0
}

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

# ── webui 产物：CI 构建好，生产只解压 ────────────────────────────────
# 2026-09-21 事故：生产机上 `npm ci + build` 跑了 9.5 分钟，把机械盘的 IO 拖死，
# 1259 个人在那段时间里干什么都卡。这次构建没有任何理由发生在这台机器上 ——
# 它是纯函数：同一个 sha 产出同一份 dist。CI（hermes-web-ui 侧）已经把
# webui-<sha40>.tar.gz 推进 GitLab 的 generic 包仓库，这里只负责取回、校验、解开。
# 用 gzip 不用 zstd：生产 hermes-1 上没有 zstd 二进制，只有 GNU tar 1.34 + gzip。
#
# 三条边界，都是故意的：
#  1) 失败一律【退回原地构建】而不是 die。包仓库抖一下就发不出版本，比慢更糟。
#  2) 校验不过 = 当没拿到。删掉下载文件再退回去构建 —— 半个产物比没有产物更危险。
#  3) token 只从 $HOME/.hermes/release.env 读，且那个文件必须 600 + 本人属主。
#     文件权限松了就拒用产物（即使 unit 的 EnvironmentFile 已经把 token 灌进环境），
#     否则「放宽权限」就成了让同机其他用户借走这个 token 的办法。
WEBUI_PKG_BASE="${WEBUI_PKG_BASE:-https://gitlab.example.com/api/v4/projects/2829/packages/generic/webui-release}"
RELEASE_ENV="${RELEASE_ENV:-$HOME/.hermes/release.env}"

# 算一个文件的 sha256，只吐 64 位小写 hex。生产是 GNU coreutils 的 sha256sum；
# 本地跑测试的 mac 上可能只有 BSD 的 shasum。两个都认。
_sha256_of() {  # $1=文件
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else return 2; fi
}

# 边车清单严格解析：必须【正好一行】`<64位hex>  <期望的包名>`，文件名还得对得上。
# 绝不把边车喂给 `sha256sum -c` —— 那样校验的是边车自己写的那个路径，边车里写
# `/dev/null` 就能让一个从没校验过的包一路通过（codex review P1）。
_expected_digest() {  # $1=边车文件  $2=期望的包文件名 → 打印 hex
  local lines digest name rest
  lines=$(grep -c '[^[:space:]]' "$1" 2>/dev/null) || return 1
  [ "$lines" = "1" ] || return 1
  read -r digest name rest < <(grep '[^[:space:]]' "$1")
  [ -z "$rest" ] || return 1
  [ "$name" = "$2" ] || return 1
  digest=$(printf '%s' "$digest" | tr 'A-F' 'a-f')
  [ "${#digest}" = "64" ] || return 1
  case "$digest" in *[!0-9a-f]*) return 1 ;; esac
  printf '%s' "$digest"
}

# 链接目标是否仍落在包内。纯词法解析，不碰盘。
# 不能一刀切拒绝 `..`：npm 的 node_modules/.bin/xxx -> ../pkg/bin/xxx 全是这种，
# 一刀切等于永远用不上产物。要算的是「从链接所在目录出发，规范化之后有没有跑出根」。
_link_stays_inside() {  # $1=链接在包里的路径  $2=链接目标
  local base="$1" target="$2" combined part resolved="" IFS=/
  case "$target" in /*) return 1 ;; esac          # 绝对路径一律拒
  case "$base" in */*) base="${base%/*}" ;; *) base="" ;; esac
  combined="${base:+$base/}$target"
  for part in $combined; do
    case "$part" in
      ""|.) continue ;;
      ..)
        [ -n "$resolved" ] || return 1            # 已经在根上还要往上 = 跑出去了
        case "$resolved" in */*) resolved="${resolved%/*}" ;; *) resolved="" ;; esac ;;
      *) resolved="${resolved:+$resolved/}$part" ;;
    esac
  done
  return 0
}

# 成员白名单 + 链接目标校验，全部在解包【之前】做。
# 解包到空目录也挡不住经典的软链穿越（先放 x -> /etc，再放 x/y），所以这道必须前置。
# 两份清单并排读：-tzf 给干净路径，-tvzf 只用来取类型和链接目标 ——
# 只解析 -tvzf 的话，带空格的路径会把字段切错。任何解析不出来的情况一律判不合法。
_validate_archive_members() {  # $1=tar 文件  $2=暂存目录 → 0=全部合法
  local tarf="$1" plain="$2/.members" verbose="$2/.members-v"
  local e v typ target norm bad=0
  tar -tzf "$tarf" > "$plain" 2>/dev/null || return 1
  tar -tvzf "$tarf" > "$verbose" 2>/dev/null || return 1
  [ -s "$plain" ] || return 1
  # 6/7 号 fd：9 号被发布锁占着（exec 9>"$LOCK"），不能碰。
  exec 6<"$plain" 7<"$verbose"
  while IFS= read -r e <&6; do
    IFS= read -r v <&7 || { bad=1; break; }
    norm="${e#./}"
    [ -z "$norm" ] && continue                    # 归档根自己
    case "$norm" in
      /*) bad=1; break ;;
      ..|../*|*/../*|*/..) bad=1; break ;;
    esac
    case "$norm" in
      dist|dist/*|node_modules|node_modules/*|package.json|package-lock.json|ARTIFACT.json) ;;
      *) bad=1; break ;;
    esac
    typ="${v:0:1}"
    case "$typ" in
      l) target="${v#* -> }";     _link_stays_inside "$norm" "$target" || { bad=1; break; } ;;
      # 硬链接的目标是相对【归档根】的路径，所以从根上算
      h) target="${v#* link to }"; _link_stays_inside "ROOT" "$target" || { bad=1; break; } ;;
    esac
  done
  exec 6<&- 7<&-
  return $bad
}

# 失败统一出口：说一句、清掉暂存和下载文件。调用方自己 return 1。
_artifact_abort() {  # $1=日志  $2=暂存目录（可空）  $3..=要删的文件
  local msg="$1" stage="$2"
  shift 2
  log "  $msg"
  [ -n "$stage" ] && rm -rf "$stage"
  [ "$#" -gt 0 ] && rm -f "$@"
  return 1
}

# 包仓库下载：一个文件，最多两跳。
#
# 两条硬约束逼出了这个形状，缺一条都会把 deploy token 送出去：
#  1) token 不进 argv。`curl -H "DEPLOY-TOKEN: $t"` 会让同机任何用户一条 ps 就读到它
#     （下载窗口最长 300s）。改成从 stdin 喂 curl 的 config，argv 里只剩一个 `-`。
#     本脚本任何位置都没有 set -x —— 有的话这份 config 会被原样打进日志。
#  2) 不用 -L。GitLab 可能把下载 302 到对象存储，而 curl 跟随重定向时会把
#     --config 里的 header 原样带去那个域名 = 把 token 交给 S3。所以自己走两步：
#     第一跳带 token 问 GitLab；拿到 3xx 就【不带任何 auth】去取预签名 URL
#     —— 那个 URL 自己就是凭据，既不该再叠 token，也不该进日志（只记 host）。
_fetch_pkg_file() {  # $1=url  $2=落地文件  $3=失败说明文件 → 0=拿到了
  local url="$1" out="$2" err="$3" meta code redirect rhost rc=0
  : > "$err"
  # 第一跳故意不带 --fail：要的是 http_code 本身，好把 3xx 和 4xx 分开处理。
  meta=$(curl --config - --silent --show-error --retry 2 --max-time 300 \
           -o "$out" -w '%{http_code} %{redirect_url}' "$url" \
           < <(printf 'header = "DEPLOY-TOKEN: %s"\n' "${HERMES_RELEASE_PKG_TOKEN:-}") \
           2>>"$err") || rc=$?
  if [ "$rc" != 0 ]; then
    printf 'curl %s' "$rc" >> "$err"
    rm -f "$out"
    return 1
  fi
  code="${meta%% *}"
  redirect="${meta#* }"
  case "$code" in
    2??) return 0 ;;
    301|302|303|307|308)
      # 3xx 的响应体是一段重定向说明，不是产物 —— 先删掉再谈第二跳。
      rm -f "$out"
      if [ -z "$redirect" ]; then
        printf 'HTTP %s 但没有 Location' "$code" >> "$err"
        return 1
      fi
      rhost="${redirect#*://}"; rhost="${rhost%%/*}"
      : > "$err"
      # 第二跳：没有 --config、没有任何 header。curl 的报错可能带上预签名 URL，
      # 所以这一跳的 stderr 整个丢掉，只留退出码和目标 host。
      curl --fail --silent --show-error --retry 2 --max-time 300 -o "$out" "$redirect" 2>/dev/null || rc=$?
      if [ "$rc" != 0 ]; then
        printf 'HTTP %s → %s 取失败（curl %s）' "$code" "$rhost" "$rc" >> "$err"
        rm -f "$out"
        return 1
      fi
      return 0 ;;
    *)
      printf 'HTTP %s' "$code" >> "$err"
      rm -f "$out"
      return 1 ;;
  esac
}

fetch_webui_artifact() {  # $1=webui sha40  $2=目标目录
  local sha="$1" dest="$2" short="${1:0:8}"
  local dir="$RELEASES/.artifacts"
  local tarball="webui-$sha.tar.gz" sums="webui-$sha.sha256"
  local marker="$dest/.artifact-installing"
  local stage="" want got emode eowner rc=0

  # 上一次装到一半（进程被杀在两次 mv 中间）留下的树，一个字节都不能复用：
  # dist/server/index.js 可能已经就位而 node_modules 还缺一半，下面那道
  # 「dist 在就不构建」的门会被它骗过去，然后带着半套依赖上线（codex review P1）。
  if [ -f "$marker" ]; then
    log "  上次产物安装没走完（$marker 还在）—— 清掉 dist/node_modules 重新取"
    rm -rf "$dest/dist" "$dest/node_modules"
    rm -f "$marker"
  fi

  # 校验器先于下载检查：校验不了就别先花掉几百兆下行和一轮盘 IO。
  # 解包器不用查：gzip 是 tar 自带的，生产和 CI 容器上都在。
  if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
    log "  ARTIFACT SKIPPED: 本机没有 sha256 校验工具 —— 退回原地构建（慢）"
    return 1
  fi

  if [ -f "$RELEASE_ENV" ]; then
    emode=$(stat -c '%a' "$RELEASE_ENV" 2>/dev/null || stat -f '%Lp' "$RELEASE_ENV" 2>/dev/null)
    eowner=$(stat -c '%U' "$RELEASE_ENV" 2>/dev/null || stat -f '%Su' "$RELEASE_ENV" 2>/dev/null)
    if [ "$emode" != "600" ]; then
      log "  ARTIFACT SKIPPED: $RELEASE_ENV 权限是 $emode，必须是 600 —— 退回原地构建（慢）"
      return 1
    fi
    if [ "$eowner" != "$(id -un)" ]; then
      log "  ARTIFACT SKIPPED: $RELEASE_ENV 属主是 $eowner，必须是 $(id -un) —— 退回原地构建（慢）"
      return 1
    fi
    # shellcheck source=/dev/null
    . "$RELEASE_ENV" || true
  fi
  if [ -z "${HERMES_RELEASE_PKG_TOKEN:-}" ]; then
    log "  ARTIFACT SKIPPED: no HERMES_RELEASE_PKG_TOKEN —— 退回原地构建（慢）"
    return 1
  fi

  mkdir -p "$dir" || { log "  ARTIFACT SKIPPED: 建不出下载目录 $dir —— 退回原地构建（慢）"; return 1; }
  rm -f "$dir/$tarball" "$dir/$sums" "$dir/curl.err"
  local base="${WEBUI_PKG_BASE%/}/$sha" f
  for f in "$tarball" "$sums"; do
    if ! _fetch_pkg_file "$base/$f" "$dir/$f" "$dir/curl.err"; then
      log "  ARTIFACT MISSING: $short 取不到 $f（$(tr -d '\n' < "$dir/curl.err" 2>/dev/null)）—— 退回原地构建（慢）"
      rm -f "$dir/$tarball" "$dir/$sums" "$dir/curl.err"
      return 1
    fi
  done
  rm -f "$dir/curl.err"

  # 校验：边车自己说了算的东西一个都不信，只信「边车声明的 hex」对上
  # 「我们自己对这个受控路径算出来的 hex」。
  want=$(_expected_digest "$dir/$sums" "$tarball") || {
    _artifact_abort "ARTIFACT CHECKSUM MISMATCH: $short 边车清单不合法（要正好一行 <hex>  $tarball）—— 退回原地构建（慢）" \
      "" "$dir/$tarball" "$dir/$sums"
    return 1
  }
  got=$(_sha256_of "$dir/$tarball") || {
    _artifact_abort "ARTIFACT CHECKSUM MISMATCH: $short 算不出 sha256 —— 退回原地构建（慢）" \
      "" "$dir/$tarball" "$dir/$sums"
    return 1
  }
  if [ "$want" != "$got" ]; then
    _artifact_abort "ARTIFACT CHECKSUM MISMATCH: $short —— 已删除下载文件，退回原地构建（慢）" \
      "" "$dir/$tarball" "$dir/$sums"
    return 1
  fi

  # 解包进一次性暂存目录，绝不直接落到 checkout 上：被拒的包不许在源码树里
  # 留下任何东西，否则退回去跑的 npm ci 读的就是攻击者的 package-lock（codex review P1）。
  stage=$(mktemp -d "$dir/stage.XXXXXX") || {
    _artifact_abort "ARTIFACT SKIPPED: $short 建不出暂存目录 —— 退回原地构建（慢）" "" "$dir/$tarball" "$dir/$sums"
    return 1
  }

  if ! _validate_archive_members "$dir/$tarball" "$stage"; then
    _artifact_abort "ARTIFACT REJECTED: $short 包里有越界成员（白名单外的路径、.. 或跑出包的链接）—— 退回原地构建（慢）" \
      "$stage" "$dir/$tarball" "$dir/$sums"
    return 1
  fi

  rc=0
  tar -xzf "$dir/$tarball" -C "$stage" 2>"$dir/tar.err" || rc=$?
  if [ "$rc" != 0 ]; then
    _artifact_abort "ARTIFACT SKIPPED: $short 解包失败（tar $rc: $(tail -1 "$dir/tar.err" 2>/dev/null)）—— 退回原地构建（慢）" \
      "$stage" "$dir/$tarball" "$dir/$sums" "$dir/tar.err"
    return 1
  fi
  rm -f "$dir/tar.err"

  # 解完再照着落盘的实际软链复查一遍（清单校验漏解析了也能兜住）。
  local l lt
  while IFS= read -r l; do
    [ -n "$l" ] || continue
    lt=$(readlink "$l" 2>/dev/null) || lt=""
    if ! _link_stays_inside "${l#"$stage"/}" "$lt"; then
      _artifact_abort "ARTIFACT REJECTED: $short 解包后有跑出包的软链 —— 退回原地构建（慢）" \
        "$stage" "$dir/$tarball" "$dir/$sums"
      return 1
    fi
  done < <(find "$stage" -type l 2>/dev/null)

  if [ ! -f "$stage/dist/server/index.js" ] || [ ! -e "$stage/node_modules/node-pty" ]; then
    _artifact_abort "ARTIFACT REJECTED: $short 包里缺 dist/server/index.js 或 node_modules/node-pty —— 退回原地构建（慢）" \
      "$stage" "$dir/$tarball" "$dir/$sums"
    return 1
  fi

  # 把产物钉死在【这个 sha 的依赖清单】上：package-lock 必须和 checkout 里的逐字节一致。
  # 少了这条，一个为别的 sha 打的（或被人换过依赖的）包也能装进来。
  if [ ! -f "$dest/package-lock.json" ] || ! cmp -s "$stage/package-lock.json" "$dest/package-lock.json"; then
    _artifact_abort "ARTIFACT REJECTED: $short package-lock.json 与 checkout 不一致 —— 退回原地构建（慢）" \
      "$stage" "$dir/$tarball" "$dir/$sums"
    return 1
  fi
  # ARTIFACT.json 得自报同一个 sha。字段名 sha / webui_sha 都认（CI 打包侧的写法），
  # 但值必须等于本次要发的 sha。
  if ! grep -Eq "\"(webui_)?sha\"[[:space:]]*:[[:space:]]*\"$sha\"" "$stage/ARTIFACT.json" 2>/dev/null; then
    _artifact_abort "ARTIFACT REJECTED: $short ARTIFACT.json 里的 sha 对不上 —— 退回原地构建（慢）" \
      "$stage" "$dir/$tarball" "$dir/$sums"
    return 1
  fi

  # 安装：两次 rename 之间被杀就会留下半棵树，所以先立标记再动手。
  # 标记还在 = 下次进来无条件清掉重取，绝不让半套依赖被当成装好了。
  : > "$marker" || {
    _artifact_abort "ARTIFACT SKIPPED: $short 写不出安装标记 —— 退回原地构建（慢）" "$stage" "$dir/$tarball" "$dir/$sums"
    return 1
  }
  rm -rf "$dest/dist" "$dest/node_modules"
  if ! mv "$stage/dist" "$dest/dist" || ! mv "$stage/node_modules" "$dest/node_modules"; then
    rm -rf "$dest/dist" "$dest/node_modules"
    rm -f "$marker"
    _artifact_abort "ARTIFACT SKIPPED: $short 安装失败（mv）—— 退回原地构建（慢）" "$stage" "$dir/$tarball" "$dir/$sums"
    return 1
  fi
  if [ ! -f "$dest/dist/server/index.js" ] || [ ! -e "$dest/node_modules/node-pty" ]; then
    rm -rf "$dest/dist" "$dest/node_modules"
    rm -f "$marker"
    _artifact_abort "ARTIFACT SKIPPED: $short 装完自检没过 —— 退回原地构建（慢）" "$stage" "$dir/$tarball" "$dir/$sums"
    return 1
  fi
  rm -f "$marker"
  rm -rf "$stage"
  # 留着没用：下次同 sha 重跑时 dist 已经在目标目录里，根本不会再走到这里。
  rm -f "$dir/$tarball" "$dir/$sums"
  log "  webui 产物 $short 下载校验通过，跳过 npm ci/build"
  return 0
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

log "构建 webui → $WEBUI_DIR"
if build_worktree "$RELEASES/.repo-webui" "$WEBUI_DIR" "$WEBUI_SHA"; then
  # .env 不在 git 里，指向跨版本稳定的那一份。
  # 先确认它真的在、且权限没被放宽 —— 缺了它 webui 起不来，
  # 权限松了等于把 22 行密钥摊开给同机其他用户。
  STABLE_ENV="$HOME/.hermes-web-ui/.env"
  [ -f "$STABLE_ENV" ] || die "缺少稳定的 webui .env（$STABLE_ENV）—— 拒绝构建"
  mode=$(stat -c '%a' "$STABLE_ENV" 2>/dev/null || stat -f '%Lp' "$STABLE_ENV" 2>/dev/null)
  [ "$mode" = "600" ] || die "$STABLE_ENV 权限是 $mode，必须是 600"
  ln -sfn "$STABLE_ENV" "$WEBUI_DIR/.env"
  # 先试 CI 产物。它成不成，都由下面那道「dist 还缺不缺」的门做最终决定 ——
  # 产物路径没有任何自己的否决权，拿不到就是慢一点，发布照走。
  # 目录已经存在（build_worktree 复用）且 dist 已就位时连下载都不发起。
  if [ ! -f "$WEBUI_DIR/dist/server/index.js" ] || [ -f "$WEBUI_DIR/.artifact-installing" ]; then
    fetch_webui_artifact "$WEBUI_SHA" "$WEBUI_DIR" || true
  fi
  if [ ! -f "$WEBUI_DIR/dist/server/index.js" ]; then
    log "  原地构建 webui（含 npm ci + build，要几分钟）"
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

# Connector packages are prepared while the old gateway is still serving.
# Gateway ExecStartPre is check-only, so no network/package extraction can turn
# a routine restart into downtime.
CONNECTOR_PREPARE="$MT_DIR/deploy/ensure-meegle.sh"
if [ -x "$CONNECTOR_PREPARE" ]; then
  log "准备 connector CLI（旧版服务仍在线）"
  "$CONNECTOR_PREPARE" || die "connector CLI 准备失败 —— 未停止服务"
  "$CONNECTOR_PREPARE" --check || die "connector CLI readiness 未通过 —— 未停止服务"
fi

# Admission must be writable before stopping a healthy gateway. This transaction
# changes no rows; a stale writer fails the release while the current service is
# still online instead of surfacing only after restart.
SESSION_DB="${SESSION_DB:-$HOME/.hermes/multitenancy.db}"
[ -f "$SESSION_DB" ] || die "session DB 不存在 —— 未停止服务"
sqlite3 -cmd '.timeout 10000' "$SESSION_DB" 'BEGIN IMMEDIATE; ROLLBACK;' \
  || die "session DB 写锁探针失败 —— 未停止服务"
log "session DB 写锁探针通过（未改数据）"

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

# ── editable 重装：翻软链不等于换 import ─────────────────────────────
# gateway venv 的 editable finder 把 hermes_multitenancy 的映射写死在【解析后】
# 的 releases/mt-<sha> 路径上（uv/pip 都会 canonicalize 软链）。只翻软链不重装，
# 服务重启后照跑上一个版本 —— release-20260803-02/-03 连续两次实锤，
# RELEASE OK + 探针全绿都发现不了。所以每次切换（前进和回滚）都必须朝目标
# 目录重装一次，并用同一解释器读回真实 import 路径核对：「装过了」不算数。
VENV_PY="${VENV_PY:-$HOME/.hermes/hermes-agent/venv/bin/python}"
UV_BIN="${UV_BIN:-$HOME/.local/bin/uv}"
reinstall_editable() {  # $1=目标 mt 版本目录（绝对路径）
  local target="$1"
  [ -x "$UV_BIN" ] || { log "  ✗ 找不到 uv（$UV_BIN）—— 无法重装 editable"; return 1; }
  [ -x "$VENV_PY" ] || { log "  ✗ 找不到 gateway venv python（$VENV_PY）"; return 1; }
  ( cd /tmp && "$UV_BIN" pip install -p "$VENV_PY" --no-deps -q -e "$target" ) \
    || { log "  ✗ editable 重装失败（目标 $target）"; return 1; }
  # --no-deps 是刻意的漂移控制（不让发布顺手升级无关依赖），但它也意味着
  # pyproject 新增的运行时依赖永远装不上：tencentcloud-sdk-python-vod 就这样
  # 缺了 19 天，生图全挂而发布次次报绿。装完做一次依赖体检，缺了自愈一次，
  # 仍缺就让本次发布红——发布当场红，好过用户来报。
  if ! ( cd /tmp && "$UV_BIN" pip check -p "$VENV_PY" >/dev/null 2>&1 ); then
    if [ "$DEP_HEAL" = "0" ]; then
      # 配对切换：venv 是人预先按 core frozen lock 装好并 pip check 过的，这里不过
      # 说明环境跟预检时不一样了。带依赖重装会改写 core 环境，只报不修。
      log "  ✗ 依赖体检未过，DEP_HEAL=0 —— 不带依赖重装，判本次失败："
      ( cd /tmp && "$UV_BIN" pip check -p "$VENV_PY" 2>&1 ) | sed 's/^/      /'
      return 1
    fi
    log "  依赖体检未过 —— 带依赖重装一次自愈"
    ( cd /tmp && "$UV_BIN" pip install -p "$VENV_PY" -q -e "$target" ) \
      || { log "  ✗ 依赖自愈重装失败（目标 $target）"; return 1; }
    ( cd /tmp && "$UV_BIN" pip check -p "$VENV_PY" ) \
      || { log "  ✗ 依赖体检仍未过 —— 放弃本次发布"; return 1; }
    log "  依赖自愈完成（uv pip check 通过）"
  fi
  _editable_points_to "$target" || { log "  ✗ editable 读回核对失败（import 未指向 $target）"; return 1; }
  log "  editable -> $target（读回核对通过）"
}

# venv 里 hermes_multitenancy 真实 import 自哪个目录，是否在 $1 之下。
_editable_points_to() {  # $1=mt 版本目录（绝对路径）
  "$VENV_PY" - "$1" <<'PY'
import importlib, sys
m = importlib.import_module("hermes_multitenancy")
t = sys.argv[1].rstrip("/")
sys.exit(0 if str(getattr(m, "__file__", "") or "").startswith(t + "/") else 1)
PY
}

# ── core 配对：旧 MT 只能配它当初跑的那个 core ─────────────────────────
# core 身份 = venv 里实际 import 到的 hermes-agent 发行版本 + hermes_cli 所在目录的
# 真实路径（core 软链翻过去，这个路径就跟着变）。不从目录名猜版本。
# 取不到打印空串。stdin 接 /dev/null：这里不是 heredoc，别让解释器去读终端。
core_identity() {
  "$VENV_PY" -c '
import importlib.metadata as md, os, hermes_cli
root = os.path.realpath(os.path.dirname(os.path.dirname(hermes_cli.__file__)))
print(md.version("hermes-agent"), root)
' </dev/null 2>/dev/null | tail -n 1 || true
}

# 停服前记一次基线（服务仍在跑、venv 还没被本次发布动过）：
#   CORE_AT_START       此刻的 core 身份
#   PREV_MT_IN_VENV=1   venv 此刻 import 的正是上一版 MT = 上一版 MT 就是跑在这个 core 上的
# 同一次停服先翻 core 再跑发布器时，venv 已经是新 core（A4 里预装了新 MT），
# PREV_MT_IN_VENV=0 —— 这正是「回滚会把旧 MT 装进新 core」的判据。
record_core_baseline() {
  CORE_AT_START=$(core_identity)
  if _editable_points_to "$PREV_MT_ABS" </dev/null >/dev/null 2>&1; then PREV_MT_IN_VENV=1; else PREV_MT_IN_VENV=0; fi
  { echo "core_at_start=${CORE_AT_START:-<unknown>}"; echo "prev_mt_in_venv=$PREV_MT_IN_VENV"
    echo "auto_rollback=$AUTO_ROLLBACK"; echo "dep_heal=$DEP_HEAL"; } >> "$SNAP/ROLLBACK.txt"
  log "  core 基线：${CORE_AT_START:-<取不到>}；venv 里的 MT 是否上一版：$PREV_MT_IN_VENV"
}

# 返回 0 = 可以回滚到上一版 MT（与当前 core 配对）；1 = 不配对或无法证明，原因写进 PAIR_REASON。
rollback_is_paired() {
  local now
  PAIR_REASON=""
  if [ "$PREV_MT_IN_VENV" != "1" ]; then
    PAIR_REASON="发布前 venv（$VENV_PY）import 的 hermes_multitenancy 不是上一版 $PREV_MT_ABS —— core/venv 已不是上一版 MT 跑过的那一套"
    return 1
  fi
  # 发布前就取不到 core 身份 = venv 里连 hermes_cli 都 import 不了。生产上 gateway
  # 就跑在这个 venv 里，正常时不可能取不到；取不到说明 core/venv 已经坏了或被换过，
  # 「core 没变」这半条判据就证明不了。无法证明配对，按不配对处理。
  if [ -z "$CORE_AT_START" ]; then
    PAIR_REASON="发布前取不到 core 身份（$VENV_PY 里 import hermes_cli / 读 hermes-agent 版本失败）—— 无法证明 core 没变"
    return 1
  fi
  now=$(core_identity)
  if [ "$now" != "$CORE_AT_START" ]; then
    PAIR_REASON="core 在发布过程中变了：发布前 ${CORE_AT_START}，现在 ${now:-<取不到>}"
    return 1
  fi
  return 0
}

# 不回滚、停服务、交给人。只在 AUTO_ROLLBACK=0，或默认模式下回滚会不配对时走这里。
# exit 1 = 已停服交人；exit 2 = 连停服都没停下来（still_active 里的 unit 还在对外服务，
# unknown_state 里的 unit 读不出状态、可能还在跑）。
hold_for_human() {  # $1=在哪一步失败  $2=为什么不自动回滚
  local u units still="" unknown="" state rc stop_rc=0
  log "$1 —— 不自动回滚：$2"
  units=$(_units)
  $SYSTEMCTL stop $units 2>/dev/null || stop_rc=$?
  # 「停过了」不算数：逐个读回。stop 超时/失败时半套新版本还在对外服务，
  # 日志要是照样写「服务已停」，按它去整体回退的人会被误导。
  # 只有 exit 3 且读到 inactive/failed 才算已停；exit 0 = 仍在跑；其余（D-Bus 断开、
  # 权限拒绝、unit 查不到……）一律「状态未知」，与仍在跑一样按停服失败处理。
  for u in $units; do
    rc=0; state=$($SYSTEMCTL is-active "$u" 2>/dev/null) || rc=$?
    if [ "$rc" -eq 0 ]; then still="$still $u"
    elif [ "$rc" -eq 3 ] && { [ "$state" = inactive ] || [ "$state" = failed ]; }; then :
    else unknown="$unknown $u"
    fi
  done
  still="${still# }"; unknown="${unknown# }"
  outcome NEEDS_HUMAN_CORE_PAIRED
  { echo "failed_step=$1"; echo "hold_reason=$2"
    echo "stop_rc=$stop_rc"; echo "still_active=${still:-<none>}"
    echo "unknown_state=${unknown:-<none>}"
    echo "live_mt=$(readlink "$CODE/hermes-multitenancy" 2>/dev/null)"
    echo "live_webui=$(readlink "$CODE/hermes-web-ui" 2>/dev/null)"
    echo "core_now=$(core_identity)"; } >> "$SNAP/ROLLBACK.txt"
  if [ -n "$still" ] || [ -n "$unknown" ]; then
    log "NEEDS_HUMAN_CORE_PAIRED — 停服失败（stop 退出码 $stop_rc），仍在运行：${still:-<none>}；状态未知（仍可能在跑）：${unknown:-<none>}"
    log "   先手工停掉这些 unit 并读回 is-active，再整体回退 core 软链 + MT + WebUI + 数据库快照（锚点：$SNAP/ROLLBACK.txt）"
    exit 2
  fi
  log "NEEDS_HUMAN_CORE_PAIRED — 服务已停（逐个读回 is-active 确认），软链与 editable 保持失败现场，未翻回 ${CURRENT:-上一版}；"
  log "   在同一次停服里整体回退 core 软链 + MT + WebUI + 数据库快照（锚点：$SNAP/ROLLBACK.txt）"
  exit 1
}

# 每个失败点在动手回滚之前调用：不许回滚就直接停服交人（不返回）。
guard_rollback() {  # $1=在哪一步失败
  if [ "$AUTO_ROLLBACK" = "0" ]; then
    hold_for_human "$1" "AUTO_ROLLBACK=0（与 core 配对切换）"
  fi
  if ! rollback_is_paired; then
    hold_for_human "$1" "回滚目标与当前 core 不配对：$PAIR_REASON"
  fi
}

# ── relay 同步：拷文件 + 重启 + 读回核对 ─────────────────────────────
# 文件清单用通配枚举，不写死。写死的清单在下次新增 relay 模块时会静默漏拷 ——
# 那正是本次要根治的失败类型（漏发一个模块比漏发全部更难发现）。
# 通配只覆盖文件名模式，覆盖不了这些文件 import 的其它包内模块（2026-09-17 漏拷
# shared_db.py 就是这样发生的）。所以从通配种子出发，用 ast 沿所有层级的相对 import
# （含函数体内的延迟 import）递归收敛到不动点。扫描器不可用时退回 glob + shared_db.py：
# 退回是显式记日志的，不是静默降级。
_relay_files() {  # $1=源目录（$MT_DIR/hermes_multitenancy）
  local src="$1" f seeds="" out
  for f in "$src"/agent_relay*.py "$src"/credentials.py; do
    [ -f "$f" ] && seeds="$seeds $(basename "$f")"
  done
  seeds="${seeds# }"
  [ -n "$seeds" ] || { printf ''; return 0; }
  out=$("$RELAY_SCAN_PY" - "$src" $seeds <<'PYEOF' 2>/dev/null
import ast, os, sys
src, seeds = sys.argv[1], sys.argv[2:]
done, queue = [], list(seeds)
while queue:
    name = queue.pop(0)
    if name in done:
        continue
    done.append(name)
    try:
        tree = ast.parse(open(os.path.join(src, name), encoding="utf-8").read())
    except (OSError, SyntaxError):
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            dep = node.module.split(".")[0] + ".py"
            if os.path.isfile(os.path.join(src, dep)) and dep not in done and dep not in queue:
                queue.append(dep)
print(" ".join(done))
PYEOF
) || out=""
  if [ -z "$out" ]; then
    log "  ! relay 依赖扫描不可用（$RELAY_SCAN_PY）—— 退回 glob 清单 + shared_db.py"
    out="$seeds"
    [ -f "$src/shared_db.py" ] && out="$out shared_db.py"
  fi
  printf '%s' "$out"
}

# 拷完、重启前：在 relay 自己的解释器里 import 一次。失败 = 清单还是漏了什么，
# 或新代码本身 import 就炸 —— 两种都不该拿到运行中的进程上去试。
_relay_import_preflight() {
  # 解释器找不到 = 没法证明新文件能 import，不是「不用证明」。放行会把本次要拦的
  # 启动崩溃原样放回去（codex r1 #p1）。
  [ -x "$RELAY_PY" ] || { log "  ✗ relay 解释器不存在或不可执行（$RELAY_PY），import 预检无法进行"; return 1; }
  local err
  if err=$(timeout 30 "$RELAY_PY" -c "import $RELAY_IMPORT_TARGET" 2>&1); then
    return 0
  fi
  log "  ✗ relay import 预检失败（$RELAY_IMPORT_TARGET）：$(printf '%s' "$err" | tail -n 1)"
  return 1
}

# 返回 0 = relay 已确认跟上；1 = 失败（调用方负责记 outcome 并非零退出）。
sync_relay() {  # $1=目标 mt 版本目录（绝对路径）  $2=备份目录
  # 分开写：同一条 `local` 里的后一个赋值看不见前一个（整行先展开再赋值），
  # `src="$mt/…"` 写在同一行会在 set -u 下直接炸 unbound variable。
  local mt="$1" bak="$2" files f restarted=0 created=""
  local src="$mt/hermes_multitenancy"
  [ -d "$RELAY_PKG_DIR" ] || { log "  relay 包目录不存在（$RELAY_PKG_DIR）—— 跳过 relay 同步"; return 0; }
  files=$(_relay_files "$src")
  [ -n "$files" ] || { log "  ✗ 新版本里找不到 relay 源文件（$src）"; return 1; }

  mkdir -p "$bak" || return 1
  for f in $files; do
    if [ -f "$RELAY_PKG_DIR/$f" ]; then
      cp -p "$RELAY_PKG_DIR/$f" "$bak/$f" || return 1
    else
      # 本次首次带进来的文件：还原时必须删掉，不然失败版本的新模块残留在包里，
      # 下次发布可能靳靠它把 import 蒙混过去，跑成混合版本（codex r1 #p1）。
      created="$created $f"
    fi
  done

  # $1=files_only：只动文件、绝不碰进程。预检失败时用 —— 老进程还在跑，任何一次
  # restart 都是把坏版本或半还原状态推给它（codex r1 #p1）。
  _relay_restore() {
    local f mode="${1:-}"
    for f in $files; do
      [ -f "$bak/$f" ] && cp -p "$bak/$f" "$RELAY_PKG_DIR/$f"
    done
    for f in $created; do
      rm -f "$RELAY_PKG_DIR/$f"
    done
    rm -rf "$RELAY_PKG_DIR/__pycache__"
    [ "$mode" = "files_only" ] && return 0
    # sudo 被拒时老进程根本没停过：还原了文件 + 老进程还在跑 = 已经自洽，
    # 不该再多踢一脚。只有它真的 down、或本次已经重启成功过，才需要再起一次。
    if [ "$restarted" = "1" ] || ! $RELAY_IS_ACTIVE 2>/dev/null; then
      $RELAY_RESTART 2>/dev/null || log "  ✗ relay 还原后重启失败 —— 需要人工介入"
    fi
  }

  for f in $files; do
    cp -p "$src/$f" "$RELAY_PKG_DIR/$f" || { _relay_restore; return 1; }
  done
  rm -rf "$RELAY_PKG_DIR/__pycache__"

  # 老进程此刻还在跑：预检失败只还原文件，不碰进程。
  if ! _relay_import_preflight; then
    _relay_restore files_only
    return 1
  fi

  # `sudo -n` 是刻意的：缺 sudoers drop-in 时当场红，绝不能挂在密码提示上把
  # 每天 18:00 的定时器吊死。
  if ! $RELAY_RESTART; then
    log "  ✗ relay 重启失败（$RELAY_RESTART）—— 还原并放弃 relay 同步"
    _relay_restore
    return 1
  fi
  restarted=1

  # 读回核对：跟 reinstall_editable 同一套哲学，「拷过了」不算数。
  for f in $files; do
    if ! cmp -s "$src/$f" "$RELAY_PKG_DIR/$f"; then
      log "  ✗ relay 读回核对失败（$f 与新版本不一致）"
      _relay_restore
      return 1
    fi
  done

  # 活体判据：路由注册过 → 撞鉴权返回 401；没注册 → aiohttp 纯文本 404。
  #
  # 重试的理由（2026-08-15 release-20260815-01 现场）：重启后立刻打一次探针会撞上
  # 「systemd 说 started、aiohttp 还没 bind」那一两秒，拿到 000 就还原 relay 并让整个
  # 发布 exit 1 —— 那次同步的文件与生产逐字节相同，是一次纯假失败。
  # 只对「连不上」(000) 等待：404 = 服务起来了但路由没注册（代码真坏），等下去也不会变，
  # 立即失败保住原有拦截力。
  if [ "$RELAY_PROBE_CMD" = "_relay_curl_probe" ] && ! command -v curl >/dev/null 2>&1; then
    # 静默降级会让「探针没跑」看起来跟「探针过了」一样。
    log "  ! 没有 curl，跳过 relay 存活探针（只做了文件读回核对）"
  elif ! relay_probe_wait "$RELAY_PROBE_CMD" "$RELAY_PROBE_TIMEOUT"; then
    # 两种失败读起来必须不一样，否则排障时分不清「等过了还是没等」：
    # 连不上 → 报等了多久 + 最后实得码；确定性坏 → 报实得码（等待恒为 0）。
    if relay_probe_connect_failed "$RELAY_PROBE_LAST_CODE"; then
      log "  ✗ relay 存活探针未过（期望 401，等待 ${RELAY_PROBE_WAITED}s/${RELAY_PROBE_TIMEOUT}s 内始终连不上，最后实得 $RELAY_PROBE_LAST_CODE）"
    else
      log "  ✗ relay 存活探针未过（期望 401，实得 $RELAY_PROBE_LAST_CODE，等待 ${RELAY_PROBE_WAITED}s）"
    fi
    _relay_restore
    return 1
  elif [ "$RELAY_PROBE_WAITED" -gt 0 ]; then
    log "  relay 探针通过（401 = 路由已注册且鉴权在位；重启后等待 ${RELAY_PROBE_WAITED}s）"
  else
    log "  relay 探针通过（401 = 路由已注册且鉴权在位）"
  fi

  log "  relay -> $mt（$files，已重启并读回核对）"
}

install_dropins() {  # $1=目标 mt 版本目录（绝对路径）
  local target="$1" installer
  installer="${DROPIN_INSTALLER:-$target/deploy/install-gateway-dropins.sh}"
  [ -x "$installer" ] || { log "  ✗ 缺少可执行 drop-in installer（$installer）"; return 1; }
  HERMES_MEEGLE_PREPARED=1 HERMES_PYTHON="$VENV_PY" "$installer"
}

record_core_baseline
log "停服务 → 翻软链 → 重装 editable → 起服务"
$SYSTEMCTL stop $(_units) 2>/dev/null || true
if ! flip "../releases/$(basename "$MT_DIR")" "../releases/$(basename "$WEBUI_DIR")"; then
  # 默认模式下这里的回滚本身无害（venv 还没动过，翻回去就是发布前那一套），但不配对
  # 时照样停服交人：不配对说明发布前的状态就已经证明不了，宁可停也不带着它重启。
  guard_rollback "软链切换失败"
  log "软链切换失败 —— 翻回原样并放弃本次发布"
  flip "$PREV_MT" "$PREV_WEBUI" || true
  $SYSTEMCTL start $(_units) 2>/dev/null || true
  outcome FLIP_FAILED
  die "切换失败，当前版本继续跑"
fi
if ! reinstall_editable "$MT_DIR"; then
  guard_rollback "editable 重装/读回失败"
  log "editable 未指向新版本 —— 翻回原样并放弃本次发布"
  flip "$PREV_MT" "$PREV_WEBUI" || true
  reinstall_editable "$PREV_MT_ABS" || true
  $SYSTEMCTL start $(_units) 2>/dev/null || true
  outcome EDITABLE_FAILED
  die "editable 重装/读回失败，当前版本继续跑"
fi
if ! install_dropins "$MT_DIR"; then
  guard_rollback "gateway drop-in 安装失败"
  log "gateway drop-in 安装失败 —— 翻回原样并放弃本次发布"
  flip "$PREV_MT" "$PREV_WEBUI" || true
  reinstall_editable "$PREV_MT_ABS" || true
  install_dropins "$PREV_MT_ABS" || true
  $SYSTEMCTL start $(_units) 2>/dev/null || true
  outcome DROPIN_FAILED
  die "gateway drop-in 安装失败，当前版本继续跑"
fi
$SYSTEMCTL start $(_units) 2>/dev/null
NEW_START_OK=$?
log "  已切到 $TAG，开始跑探针"

# ── 探针决定去留 ─────────────────────────────────────────────────────
# 用新版本自带的那份探针：判据要跟着被验的版本走
if [ "$NEW_START_OK" != "0" ]; then
  log "新版本服务启动失败 —— 不跑新版本探针，立即回滚"
elif [ ! -x "$NEW_PROBES" ]; then
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
  KEEP_PINNED=$(release_keep_pins)
  KEEP_REFS=$(release_config_refs)
  for prefix in mt webui; do
    repo="$RELEASES/.repo-$prefix"
    # while-read 而不是 mapfile：bash 3.2 没有 mapfile，报错后列表为空 = 静默从不裁剪
    while IFS= read -r d <&3; do
      [ -n "$d" ] && [ -d "$d" ] || continue
      abs=$(cd "$d" && pwd)
      name=$(basename "$abs")
      # 当前在用的、以及上一版（回滚目标）都不许删
      case "$abs" in
        "$KEEP_A"|"$KEEP_B") log "  保留 $name（current）"; continue ;;
        "$KEEP_C"|"$KEEP_D") log "  保留 $name（prev）"; continue ;;
      esac
      # 钉住清单与活配置引用：都在 $RELEASES 直接子目录这一层按目录名整名比对
      if printf '%s\n' "$KEEP_PINNED" | grep -qxF -- "$name"; then
        log "  保留 $name（pinned）"; continue
      fi
      ref_file=$(printf '%s\n' "$KEEP_REFS" | awk -F'\t' -v n="$name" '$1 == n { print $2; exit }')
      if [ -n "$ref_file" ]; then
        log "  保留 $name（referenced-by $ref_file）"; continue
      fi
      git -C "$repo" worktree remove --force "$abs" 2>/dev/null || rm -rf "$abs"
      log "  裁剪旧版本 $name"
    done 3< <(ls -1dt "$RELEASES/$prefix"-* 2>/dev/null | tail -n +$((KEEP_RELEASES + 1)))
  done
  # relay 放在【探针通过之后】，不是翻软链之后：mt/webui 一旦回滚，抢先升级的
  # relay 就成了反向漂移 —— 生产跑着比主线更新的 relay，而没有任何东西记得。
  log "同步 relay（不走软链，单独一套）"
  if ! sync_relay "$MT_DIR" "$SNAP/relay"; then
    # 刻意不回滚 mt/webui：relay 是独立进程，为它让 1259 个人再吃一次重启
    # 不成比例。relay 已还原到发布前的字节，本次发布对 mt/webui 仍然有效。
    outcome RELAY_FAILED
    log "RELAY FAILED — $TAG 的 mt/webui 已生效，relay 未跟上（已还原，需人工介入）"
    exit 1
  fi
  exit 0
fi

# ── 探针没过：自动翻回上一版 ─────────────────────────────────────────
# 启动失败、探针消失、探针未过三种都落到这里。
guard_rollback "新版本启动或探针失败"
log "探针失败 —— 自动回滚到上一版"
$SYSTEMCTL stop $(_units) 2>/dev/null || true
# 回滚本身也可能失败（第二个 rename 挂了 = 两个路径指向不同版本）。
# 不检查就可能在半坏状态上打印 ROLLED_BACK —— 那是最恶劣的假绿。
if ! flip "$PREV_MT" "$PREV_WEBUI"; then
  $SYSTEMCTL start $(_units) 2>/dev/null || true
  outcome NEEDS_HUMAN
  die "回滚时软链切换失败 —— 两个路径可能指向不同版本，必须人工介入（锚点：$SNAP/ROLLBACK.txt）"
fi
# 回滚方向同样要重装 editable —— 软链回去了、import 还钉在坏版本上，回滚就是假的
EDITABLE_ROLLBACK_OK=1
reinstall_editable "$PREV_MT_ABS" || EDITABLE_ROLLBACK_OK=0
install_dropins "$PREV_MT_ABS" || EDITABLE_ROLLBACK_OK=0
$SYSTEMCTL start $(_units) 2>/dev/null || true
# 回滚校验要用【旧版本自带】的探针 —— 现在跑的是旧版，判据就该跟着旧版走。
# （用新版的探针去验旧版是张冠李戴：新版可能改了期望值。）
OLD_PROBES="$CODE/hermes-multitenancy/deploy/$(basename "$PROBES")"
if [ "$EDITABLE_ROLLBACK_OK" != "1" ]; then
  outcome NEEDS_HUMAN
  log "ROLLED BACK 但 editable 重装失败 —— import 可能仍指向新版本，需人工介入（锚点：$SNAP/ROLLBACK.txt）"
elif [ -x "$OLD_PROBES" ] && "$OLD_PROBES"; then
  outcome ROLLED_BACK
  log "ROLLED BACK — 已退回 ${CURRENT:-上一版}，探针复检通过"
else
  outcome NEEDS_HUMAN
  log "ROLLED BACK 但复检仍未全过 —— 需要人工介入，回滚锚点在 $SNAP/ROLLBACK.txt"
fi
exit 1
