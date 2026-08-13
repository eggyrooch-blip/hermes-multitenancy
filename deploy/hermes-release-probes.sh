#!/usr/bin/env bash
# hermes-release-probes.sh — 部署后的存活判据。这套探针决定「保留新版」还是「自动回滚」。
#
# 判据全部来自踩过的坑，不是通用模板：
#   - webui 启动要 ~20 秒 → 必须轮询，只查一次会误判成失败（2026-08-01 差点栽在这）
#   - /api/health 期望 401 不是 200 → 写成 200 会永远误报失败
#   - 判补丁死活必须用 co_filename，__module__ 是假阴性
#     （functools.wraps 会抄 __module__，2026-08-01 实锤：13 个活补丁被判 0/13）
#   - 补丁要在「运行时真正在用的那个类」上数 → v0190 的懒加载让补丁落在克隆类上，
#     两个类长得一样但不是同一个，服务照常 active、日志照常干净，5 天没人发现
#   - Traceback 必须按「本次启动之后」算 → 那个日志 30 万行跨好几天，
#     直接 tail 会把历史噪声当成本次发布的问题（实测 26 个全是旧的，本次 0 个）
#
# 退出码：0 = 全过；非 0 = 有失败项，调用方应当回滚。
set -uo pipefail

HERMES_HOME_DIR="${HERMES_HOME_DIR:-$HOME/.hermes}"
HERMES_WEBUI_DIR="${HERMES_WEBUI_DIR:-$HOME/.hermes-web-ui}"
VENV_PY="${VENV_PY:-$HOME/.hermes/hermes-agent/venv/bin/python}"
# 专家 bot 的 gateway 也要探 —— 它跑着同一份代码，发布后没起来就是发布没成功。
_expert_units() { $SYSTEMCTL list-units --type=service --all --no-pager 2>/dev/null \
  | grep -oE 'hermes-gateway@[A-Za-z0-9_-]+\.service' | sort -u | tr '\n' ' '; }
WEBUI_URL="${WEBUI_URL:-http://127.0.0.1:8648}"
BROKER_URL="${BROKER_URL:-http://127.0.0.1:8766}"
APISERVER_PORT="${APISERVER_PORT:-8652}"
BOOT_WAIT="${BOOT_WAIT:-90}"          # webui 冷启动实测 ~20 秒，给 90 秒余量
SYSTEMCTL="${SYSTEMCTL:-systemctl --user}"
GATEWAY_UNIT="${GATEWAY_UNIT:-hermes-gateway.service}"
PATCH_MIN="${PATCH_MIN:-1}"           # 首次部署实测多少就钉多少，低于它即判失败
PROBE_PY="${PROBE_PY:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/hermes_patch_probe.py}"

pass=0; fail=0
ok()  { printf '  ✓ %s\n' "$*"; pass=$((pass+1)); }
bad() { printf '  ✗ %s\n' "$*"; fail=$((fail+1)); }

# ── 1. systemd 单元 ──────────────────────────────────────────────────
# 探针本来就要跑 systemctl，这里展开无所谓；专家 bot 的 gateway 也必须探 ——
# 它跑着同一份代码，发布后没起来就是发布没成功。
# activating 是「还在起」不是「起不来」：2026-08-13 release-20260813-02 首次发布
# 就因为 gateway 恰好还在 activating 被判死并自动回滚，重跑同一版本 12/12 全绿。
# 和 webui 一样轮询等待，只有等满 BOOT_WAIT 仍不 active 才算失败。
USER_UNITS="${USER_UNITS:-hermes-gateway.service hermes-web-ui.service ai-gateway-broker.service $(_expert_units)}"
for u in $USER_UNITS; do
  st=""
  for _ in $(seq 1 $((BOOT_WAIT / 5))); do
    st=$($SYSTEMCTL is-active "$u" 2>/dev/null)
    # failed/inactive 是终态，等下去也不会变好 —— 立刻判死，别白等 90 秒
    [ "$st" = "active" ] || [ "$st" = "failed" ] || [ "$st" = "inactive" ] && break
    sleep 5
  done
  [ "$st" = "active" ] && ok "$u = active" || bad "$u = ${st:-unknown}（等了最多 ${BOOT_WAIT}s）"
done

# ── 2. webui：轮询等它起来，别一次就判死 ─────────────────────────────
code=""
for _ in $(seq 1 $((BOOT_WAIT / 5))); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$WEBUI_URL/" 2>/dev/null)
  [ "$code" = "200" ] && break
  sleep 5
done
[ "$code" = "200" ] && ok "webui / = 200" || bad "webui / = ${code:-无响应}（等了 ${BOOT_WAIT}s）"

# 401 才是对的：这个端点要鉴权。写成期望 200 会永远误报。
h=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$WEBUI_URL/api/health" 2>/dev/null)
[ "$h" = "401" ] && ok "webui /api/health = 401（要鉴权，正确）" || bad "webui /api/health = ${h:-无响应}，期望 401"

# ── 3. run-broker 未鉴权必须被挡 ─────────────────────────────────────
b=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$BROKER_URL/api/run-broker/ingest" 2>/dev/null)
case "$b" in
  401|403|405) ok "broker 未鉴权 = $b（被挡，正确）" ;;
  200) bad "broker 未鉴权返回 200 —— 鉴权失效，这是安全问题" ;;
  *)   bad "broker 未鉴权 = ${b:-无响应}" ;;
esac

# ── 4. profile apiserver 端口 ────────────────────────────────────────
if ss -ltn 2>/dev/null | grep -q ":${APISERVER_PORT}\b"; then
  ok "apiserver :${APISERVER_PORT} 在听"
else
  bad "apiserver :${APISERVER_PORT} 没在听"
fi

# ── 5. 数据库完好 ────────────────────────────────────────────────────
for db in "$HERMES_HOME_DIR/multitenancy.db" "$HERMES_WEBUI_DIR/hermes-web-ui.db"; do
  [ -s "$db" ] || continue
  r=$(sqlite3 -cmd ".timeout 10000" "$db" "pragma quick_check;" 2>&1 | head -1)
  [ "$r" = "ok" ] && ok "$(basename "$db") quick_check ok" || bad "$(basename "$db") quick_check: $r"
done

# ── 6. 飞书类补丁是否真的还活着（co_filename 判据）────────────────────
if [ -f "$PROBE_PY" ]; then
  probe=$("$VENV_PY" "$PROBE_PY" 2>&1 | tail -1)
  case "$probe" in
    PATCHPROBE\ *)
      n=${probe#PATCHPROBE }
      patched=${n%%/*}; total=${n##*/}
      if [ "${patched:-0}" -ge "$PATCH_MIN" ]; then
        ok "飞书补丁存活 ${patched}/${total}（co_filename 判据，下限 ${PATCH_MIN}）"
      else
        bad "飞书补丁只剩 ${patched}/${total}，低于下限 ${PATCH_MIN} —— 补丁掉了"
      fi ;;
    *) bad "补丁探针跑不起来：$probe" ;;
  esac
fi

# ── 7. 本次启动之后不能有 Traceback ──────────────────────────────────
START=$($SYSTEMCTL show "$GATEWAY_UNIT" -p ActiveEnterTimestamp --value 2>/dev/null)
if [ -n "$START" ]; then
  n=$(journalctl --user -u "$GATEWAY_UNIT" --since "$START" --no-pager 2>/dev/null \
      | grep -c "Traceback (most recent call last)")
  [ "${n:-0}" -eq 0 ] && ok "gateway 本次启动后 0 个 Traceback" || bad "gateway 本次启动后有 $n 个 Traceback"
fi

printf '\nPROBES: %d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
