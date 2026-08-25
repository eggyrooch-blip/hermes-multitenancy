#!/usr/bin/env bash
# relay 存活探针的等待逻辑 —— 只定义函数，没有顶层动作，所以可以被测试 source。
#
# 为什么单独一个文件：hermes-release.sh 是顶层直接执行的（没有 main()），
# source 它就等于跑一次真发布，于是这段判定逻辑过去没有任何自动化覆盖。
# 2026-08-15 release-20260815-01 就栽在这里：探针在 relay 重启后**立刻**打一次，
# 撞上「systemd 说 started、aiohttp 还没 bind」的一两秒拿到 000，判失败 → 还原 relay
# → 整个发布 exit 1 + 告警；而那次同步的文件与生产逐字节相同，是一次纯假失败。

# relay_probe_connect_failed <code>
#
# 「连不上」在真实输出里不止一种写法。生产 2026-08-15 记的是 **000000**：
# `curl -w '%{http_code}' ... || echo 000` 在连接失败时先由 -w 打出 000，curl 又以
# 非零退出触发 `|| echo 000`，两段拼成六个零。只认字面 "000" 的判断在生产上永远
# 匹配不上——第一版修复就是这么写的，评审实测抓出来的。
# 判据：全是 0 且非空 = 连接失败（000 / 000000 / 未来任何长度的零串）。
relay_probe_connect_failed() {
  case "${1:-}" in
    "") return 1 ;;
    *[!0]*) return 1 ;;
    *) return 0 ;;
  esac
}

# relay_probe_wait <probe_cmd> <timeout_seconds> [sleep_cmd]
#
# 反复调用 probe_cmd 直到它回 401（= 路由已注册且鉴权在位）。
#   401     → 0（通过），并把等待秒数写进 RELAY_PROBE_WAITED
#   全零串  → 继续等（连不上 = 还没 bind，是唯一值得等的码）
#   其它（404 路由没注册 / 405 方法错 / 5xx）→ 立刻返回 1，不拿超时换一个必然的结论
#   超时仍连不上 → 返回 1
# 最后一次实得码写进 RELAY_PROBE_LAST_CODE（原样，不改写），等待秒数写进
# RELAY_PROBE_WAITED；调用方负责记日志与还原。
relay_probe_wait() {
  local probe_cmd="$1" timeout="$2" sleep_cmd="${3:-sleep}"
  local waited=0 code
  RELAY_PROBE_WAITED=0
  RELAY_PROBE_LAST_CODE=""
  while :; do
    code=$($probe_cmd 2>/dev/null || echo 000)
    RELAY_PROBE_LAST_CODE="$code"
    if [ "$code" = "401" ]; then
      RELAY_PROBE_WAITED="$waited"
      return 0
    fi
    relay_probe_connect_failed "$code" || return 1
    [ "$waited" -lt "$timeout" ] || return 1
    $sleep_cmd 1
    waited=$((waited + 1))
    RELAY_PROBE_WAITED="$waited"
  done
}
