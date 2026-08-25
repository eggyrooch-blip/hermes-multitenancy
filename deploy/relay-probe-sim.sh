#!/usr/bin/env bash
# Simulation driver — relay 探针等待逻辑，用**真 curl 打真端口**跑。
#
# 与单测的区别：单测把状态码作为字符串喂进去，这里让 curl 自己产生码，
# 所以「连不上到底打印成什么」是被观测出来的，而不是被假设的 ——
# round-1 评审的 P0（生产实得 000000，代码只认 000）正是假设造成的。
#
# 用法: deploy/relay-probe-sim.sh <1|2|3|4|5>   （在仓库根跑，或用 REPO=<repo根>）
#
# 住在仓库里而不是 .ftask/ 下：评审沙箱和后来的人只看得到 worktree，
# 把唯一能复现「连不上到底打印成什么」的驱动藏在任务目录里，等于让下一个人
# 重犯同一个 P0。
set -uo pipefail
# REPO 指向被测的工作副本（worktree 或主仓）；默认取当前目录，便于 ftask --capture 直接跑。
REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
LIB="$REPO/deploy/relay-probe-lib.sh"
[ -f "$LIB" ] || { echo "FATAL: 找不到 $LIB（用 REPO=<repo根> 指定）"; exit 2; }
RELEASE_SH="$REPO/deploy/hermes-release.sh"
# shellcheck source=/dev/null
. "$LIB"

PORT="${PORT:-18770}"
S="${1:-1}"
SRV_PID=""

cleanup() { [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null; wait 2>/dev/null; }
trap cleanup EXIT

start_server() {  # $1=status code, $2=delay before listening
  local code="$1" delay="${2:-0}"
  python3 - "$PORT" "$code" "$delay" <<'PY' &
import sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
port, code, delay = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])
time.sleep(delay)                      # 模拟「systemd 说 started、还没 bind」
class H(BaseHTTPRequestHandler):
    def do_PATCH(self):
        self.send_response(code); self.send_header("Content-Length", "0"); self.end_headers()
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", port), H).serve_forever()
PY
  SRV_PID=$!
}

# 与生产同款探针（deploy/hermes-release.sh 的 _relay_curl_probe 逐字同形）
probe() {
  curl -s --noproxy '*' -o /dev/null -w '%{http_code}' -m 2 \
    -X PATCH "http://127.0.0.1:$PORT/v1/messages/__release_probe__" \
    -H 'Content-Type: application/json' -d '{"reply_window_seconds":0}' 2>/dev/null || echo 000
}

echo "SCENARIO $S"
case "$S" in
  1)
    echo "ACTION: 端口先关着（模拟重启窗口），2 秒后才 bind 并返回 401"
    echo "OBSERVED raw probe while down: $(probe)"   # ← 这行就是 P0 的证据来源
    start_server 401 2
    relay_probe_wait probe 15
    rc=$?
    echo "OBSERVED rc=$rc waited=${RELAY_PROBE_WAITED}s last=$RELAY_PROBE_LAST_CODE"
    [ "$rc" = "0" ] || { echo "VERDICT FAIL: 重启窗口没被等过去"; exit 1; }
    [ "$RELAY_PROBE_WAITED" -ge 1 ] || { echo "VERDICT FAIL: 没有真的等待"; exit 1; }
    echo "VERDICT scenario-1 PASS: 真 curl 的连不上码被识别、等到 401 才通过"
    ;;
  2)
    echo "ACTION: 端口始终关着，timeout=3"
    relay_probe_wait probe 3
    rc=$?
    echo "OBSERVED rc=$rc waited=${RELAY_PROBE_WAITED}s last=$RELAY_PROBE_LAST_CODE"
    [ "$rc" = "1" ] || { echo "VERDICT FAIL: relay 真没起来却放行了"; exit 1; }
    [ "$RELAY_PROBE_WAITED" = "3" ] || { echo "VERDICT FAIL: 没有在 timeout 处停手"; exit 1; }
    echo "VERDICT scenario-2 PASS: 超时后失败，拦截力保留"
    ;;
  3)
    echo "ACTION: 服务已在监听并返回 401"
    start_server 401 0
    sleep 1
    relay_probe_wait probe 15
    rc=$?
    echo "OBSERVED rc=$rc waited=${RELAY_PROBE_WAITED}s last=$RELAY_PROBE_LAST_CODE"
    [ "$rc" = "0" ] && [ "$RELAY_PROBE_WAITED" = "0" ] || { echo "VERDICT FAIL: 健康时不该有等待"; exit 1; }
    echo "VERDICT scenario-3 PASS: 与改动前一致，零等待通过"
    ;;
  4)
    echo "ACTION: 服务在监听但路由没注册（返回 404），timeout=30"
    start_server 404 0
    sleep 1
    t0=$(date +%s)
    relay_probe_wait probe 30
    rc=$?
    t1=$(date +%s)
    echo "OBSERVED rc=$rc waited=${RELAY_PROBE_WAITED}s last=$RELAY_PROBE_LAST_CODE elapsed=$((t1-t0))s"
    [ "$rc" = "1" ] || { echo "VERDICT FAIL: 代码坏却放行"; exit 1; }
    [ $((t1-t0)) -lt 5 ] || { echo "VERDICT FAIL: 确定性坏不该烧超时"; exit 1; }
    echo "VERDICT scenario-4 PASS: 立即失败，没烧掉 30s"
    ;;
  5)
    echo "ACTION: PATH 里没有 curl（sync_relay 应跳过探针并明确记录）"
    out=$(PATH=/nonexistent bash -c 'command -v curl >/dev/null 2>&1 && echo has || echo none')
    echo "OBSERVED command -v curl => $out"
    grep -q '没有 curl，跳过 relay 存活探针' "$RELEASE_SH" \
      || { echo "VERDICT FAIL: 主脚本缺少 no-curl 的显式记录"; exit 1; }
    echo "VERDICT scenario-5 PASS: 无 curl 时跳过且不静默（分支在主脚本中保留）"
    ;;
esac
