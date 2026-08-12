#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
HERMES_PYTHON="${HERMES_PYTHON:-${HOME}/.hermes/hermes-agent/venv/bin/python}"

test -x "$HERMES_PYTHON"
mkdir -p "$UNIT_DIR"
sed "s#@PYTHON@#${HERMES_PYTHON}#g" "$REPO/deploy/hermes-kep-sync.service" > "$UNIT_DIR/hermes-kep-sync.service"
install -m 0644 "$REPO/deploy/hermes-kep-sync.timer" "$UNIT_DIR/hermes-kep-sync.timer"
sed "s#@PYTHON@#${HERMES_PYTHON}#g" "$REPO/deploy/hermes-update-center-alert@.service" > "$UNIT_DIR/hermes-update-center-alert@.service"
chmod 0644 "$UNIT_DIR/hermes-kep-sync.service" "$UNIT_DIR/hermes-update-center-alert@.service"

if systemctl --user cat hermes-update-center-kep.timer >/dev/null 2>&1; then
  systemctl --user disable --now hermes-update-center-kep.timer
  ! systemctl --user is-active --quiet hermes-update-center-kep.timer
  ! systemctl --user is-enabled --quiet hermes-update-center-kep.timer
else
  test "$(systemctl --user show hermes-update-center-kep.timer -p LoadState --value)" = "not-found"
fi
systemctl --user daemon-reload
systemctl --user enable --now hermes-kep-sync.timer
systemctl --user is-enabled --quiet hermes-kep-sync.timer
systemctl --user is-active --quiet hermes-kep-sync.timer
