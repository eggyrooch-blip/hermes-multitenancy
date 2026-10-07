#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
HERMES_PYTHON="${HERMES_PYTHON:-${HOME}/.hermes/hermes-agent/venv/bin/python}"

test -x "$HERMES_PYTHON"
mkdir -p "$UNIT_DIR"
sed "s#@PYTHON@#${HERMES_PYTHON}#g" \
  "$REPO/deploy/hermes-kep-telemetry-export.service" > "$UNIT_DIR/hermes-kep-telemetry-export.service"
install -m 0644 \
  "$REPO/deploy/hermes-kep-telemetry-export.timer" "$UNIT_DIR/hermes-kep-telemetry-export.timer"
# 告警模板单元与 kep-sync 共用；它可能还没装（两个 install 脚本互不依赖）。
sed "s#@PYTHON@#${HERMES_PYTHON}#g" \
  "$REPO/deploy/hermes-update-center-alert@.service" > "$UNIT_DIR/hermes-update-center-alert@.service"
chmod 0644 "$UNIT_DIR/hermes-kep-telemetry-export.service" "$UNIT_DIR/hermes-update-center-alert@.service"

systemctl --user daemon-reload
systemctl --user enable --now hermes-kep-telemetry-export.timer
systemctl --user is-enabled --quiet hermes-kep-telemetry-export.timer
systemctl --user is-active --quiet hermes-kep-telemetry-export.timer
echo "installed: $(systemctl --user show hermes-kep-telemetry-export.timer -p NextElapseUSecRealtime --value)"
