#!/usr/bin/env bash
# hermes-health-check.sh — Five-probe health check for Hermes production.
#
# Runs five read-only probes against known paths, sends Feishu alerts on
# threshold breaches with a 30-minute dedup window, and recovery messages
# when a previously-alerting probe returns to pass.
#
# Reuses the same Feishu webhook env var (HERMES_UPDATE_CENTER_WEBHOOK)
# and HTTP POST shape as update_center_alert.py.
#
# Production IO safety: all probes use known SQLite paths (no find /).
# Install as hermes user systemd unit, fires every 5 minutes.
set -uo pipefail

# ---------------------------------------------------------------------------
# Configuration (override via environment)
# ---------------------------------------------------------------------------
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
KANBAN_DB="${HERMES_HOME}/kanban/current"
MULTITENANCY_DB="${HERMES_HOME}/multitenancy.db"
GATEWAY_LOGS_GLOB="${HERMES_HOME}/profiles/*/logs/gateway.log"

UNIT_DIR="${HERMES_UNIT_DIR:-$HOME/.config/systemd/user}"

STATE_DIR="${HERMES_HOME}/health-check"
LOG_FILE="${STATE_DIR}/health-check.log"
DEDUP_WINDOW_SEC=1800  # 30 minutes

WEBHOOK_URL="${HERMES_UPDATE_CENTER_WEBHOOK:-}"
HOST=$(uname -n)
# Auto-detect Python: prefer production venv, fall back to system python3.
PYTHON="${HERMES_HEALTH_PYTHON:-}"
if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
    if [ -x "${HERMES_HOME}/hermes-agent/venv/bin/python" ]; then
        PYTHON="${HERMES_HOME}/hermes-agent/venv/bin/python"
    else
        PYTHON="$(command -v python3 || command -v python)"
    fi
fi
PROBES_SCRIPT="$(dirname "$0")/health_probes.py"

ts() { date "+%Y-%m-%d %H:%M:%S"; }

mkdir -p "$STATE_DIR"

# ---------------------------------------------------------------------------
# Run probes via Python and parse JSON output
# ---------------------------------------------------------------------------
# Collect gateway log paths (known glob, not find /)
GATEWAY_LOGS=""
for f in $GATEWAY_LOGS_GLOB; do
    [ -f "$f" ] && GATEWAY_LOGS="$GATEWAY_LOGS --gateway-log $f"
done

if [ ! -f "$PROBES_SCRIPT" ]; then
    echo "$(ts) ERROR: health_probes.py not found at $PROBES_SCRIPT" >> "$LOG_FILE"
    exit 1
fi

# Run probes and capture JSON
PROBE_JSON=$("$PYTHON" "$PROBES_SCRIPT" \
    --kanban-db "$KANBAN_DB" \
    --multitenancy-db "$MULTITENANCY_DB" \
    $GATEWAY_LOGS \
    --unit-dir "$UNIT_DIR" \
    --json 2>>"$LOG_FILE")

if [ $? -ne 0 ]; then
    echo "$(ts) ERROR: probe execution failed" >> "$LOG_FILE"
    exit 1
fi

# ---------------------------------------------------------------------------
# Process each probe result: dedup + alert/recovery
# ---------------------------------------------------------------------------

# Parse JSON with Python (avoids jq dependency)
ALERTS_SENT=0
PROBES_ALERTING=0

# Process each probe: name, status, value, threshold, detail
PROBE_COUNT=$(echo "$PROBE_JSON" | "$PYTHON" -c "
import json, sys
data = json.load(sys.stdin)
print(len(data))
" 2>/dev/null || echo "0")

for i in $(seq 0 $((PROBE_COUNT - 1))); do
    # Extract probe fields
    eval "$(
        echo "$PROBE_JSON" | "$PYTHON" -c "
import json, sys, shlex
data = json.load(sys.stdin)
p = data[$i]
for k in ('name', 'status', 'value', 'threshold', 'detail'):
    v = str(p.get(k, ''))
    # Shell-safe quoting
    print(f'{k.upper()}={shlex.quote(v)}')
" 2>/dev/null
    )"

    STATE_FILE="$STATE_DIR/probe_${NAME}"
    NOW_EPOCH=$(date +%s)

    if [ "$STATUS" = "alert" ]; then
        PROBES_ALERTING=$((PROBES_ALERTING + 1))

        # Check dedup window
        LAST_ALERT=$(cat "$STATE_FILE" 2>/dev/null || echo "0")
        AGE=$((NOW_EPOCH - LAST_ALERT))

        if [ "$AGE" -lt "$DEDUP_WINDOW_SEC" ]; then
            # Still in dedup window — log but don't re-alert
            echo "$(ts) ALERT $NAME (suppressed, ${AGE}s since last alert): value=$VALUE threshold=$THRESHOLD" >> "$LOG_FILE"
            continue
        fi

        # Send alert
        ALERT_TEXT="🔴 [P1] $NAME 告警
host: $HOST
value: $VALUE (threshold: $THRESHOLD)
detail: $DETAIL
time: $(ts)"

        SEND_OK=1
        if [ -n "$WEBHOOK_URL" ]; then
            "$PYTHON" -c "
import json, urllib.request, sys
payload = json.dumps({'msg_type': 'text', 'content': {'text': sys.argv[1]}}, ensure_ascii=False).encode('utf-8')
req = urllib.request.Request(sys.argv[2], data=payload, headers={'Content-Type': 'application/json'})
try:
    resp = urllib.request.urlopen(req, timeout=10)
    body = json.loads(resp.read().decode('utf-8'))
except Exception as e:
    print(f'webhook post failed: {e}', file=sys.stderr)
    sys.exit(1)
# 飞书 webhook 拒收也返回 HTTP 200，成功判据是 body 的 code/StatusCode 为 0
if body.get('code', 0) != 0 or body.get('StatusCode', 0) != 0:
    print(f'webhook rejected: {body}', file=sys.stderr)
    sys.exit(1)
" "$ALERT_TEXT" "$WEBHOOK_URL" 2>>"$LOG_FILE" || SEND_OK=0
        fi

        # 只有确认送达（或未配置 webhook 的纯日志模式）才盖 dedup 章；
        # 发送失败不 stamp，下一轮 5 分钟重试，绝不静默压制告警
        if [ "$SEND_OK" = "1" ]; then
            echo "$NOW_EPOCH" > "$STATE_FILE"
            echo "$(ts) ALERT $NAME: value=$VALUE threshold=$THRESHOLD — alert sent" >> "$LOG_FILE"
            ALERTS_SENT=$((ALERTS_SENT + 1))
        else
            echo "$(ts) ALERT $NAME: value=$VALUE threshold=$THRESHOLD — webhook FAILED, dedup not stamped" >> "$LOG_FILE"
        fi

    else
        # Probe is passing — check if it was previously alerting
        if [ -f "$STATE_FILE" ]; then
            # Recovery notification
            RECOVERY_TEXT="🟢 [RECOVERED] $NAME 恢复正常
host: $HOST
time: $(ts)"

            SEND_OK=1
            if [ -n "$WEBHOOK_URL" ]; then
                "$PYTHON" -c "
import json, urllib.request, sys
payload = json.dumps({'msg_type': 'text', 'content': {'text': sys.argv[1]}}, ensure_ascii=False).encode('utf-8')
req = urllib.request.Request(sys.argv[2], data=payload, headers={'Content-Type': 'application/json'})
try:
    resp = urllib.request.urlopen(req, timeout=10)
    body = json.loads(resp.read().decode('utf-8'))
except Exception as e:
    print(f'webhook post failed: {e}', file=sys.stderr)
    sys.exit(1)
# 飞书 webhook 拒收也返回 HTTP 200，成功判据是 body 的 code/StatusCode 为 0
if body.get('code', 0) != 0 or body.get('StatusCode', 0) != 0:
    print(f'webhook rejected: {body}', file=sys.stderr)
    sys.exit(1)
" "$RECOVERY_TEXT" "$WEBHOOK_URL" 2>>"$LOG_FILE" || SEND_OK=0
            fi

            # 同 alert：恢复播报没送达就保留状态文件，下一轮重试
            if [ "$SEND_OK" = "1" ]; then
                rm -f "$STATE_FILE"
                echo "$(ts) RECOVERED $NAME — recovery notification sent" >> "$LOG_FILE"
            else
                echo "$(ts) RECOVERED $NAME — webhook FAILED, state kept for retry" >> "$LOG_FILE"
            fi
        fi
    fi
done

echo "$(ts) check complete: $PROBES_ALERTING alerting, $ALERTS_SENT alerts sent" >> "$LOG_FILE"
exit 0
