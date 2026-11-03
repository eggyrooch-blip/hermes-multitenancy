#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
HERMES_PYTHON="${HERMES_PYTHON:-${HOME}/.hermes/hermes-agent/venv/bin/python}"

test -x "$HERMES_PYTHON"
mkdir -p "$UNIT_DIR"
sed "s#@PYTHON@#${HERMES_PYTHON}#g" "$REPO/deploy/hermes-curator-sweep.service" > "$UNIT_DIR/hermes-curator-sweep.service"
install -m 0644 "$REPO/deploy/hermes-curator-sweep.timer" "$UNIT_DIR/hermes-curator-sweep.timer"
chmod 0644 "$UNIT_DIR/hermes-curator-sweep.service"

systemctl --user daemon-reload
systemctl --user enable --now hermes-curator-sweep.timer
systemctl --user is-enabled --quiet hermes-curator-sweep.timer
systemctl --user is-active --quiet hermes-curator-sweep.timer
# 单元里没有残留的 @PYTHON@ 占位符（渲染漏了会让 ExecStart 指向不存在的路径）。
! grep -q '@PYTHON@' "$UNIT_DIR/hermes-curator-sweep.service"
