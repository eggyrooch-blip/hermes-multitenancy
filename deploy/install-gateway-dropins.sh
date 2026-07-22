#!/usr/bin/env bash
# Install the multitenancy-owned gateway systemd drop-ins for the hermes user.
#
# The base hermes-gateway.service unit is written by hermes-agent CORE
# (hermes_cli/gateway.py). Multitenancy-owned drop-ins live in
# ~/.config/systemd/user/hermes-gateway.service.d/ — which is NOT in git, so a
# host rebuild / re-provision loses them. This committed, idempotent script is the
# canonical provisioner: run it on every provision/deploy (and after any box
# rebuild) so the multitenancy drop-ins come back without forking the core.
#
# Currently installs:
#   05-multitenancy-required.conf — fail startup when tenant isolation or the
#       authenticated Run Broker is unavailable.
#   45-meegle-bin.conf — pin a FAST meegle binary for the feishu-project connector
#       reader (avoids the ~11s `npx -y` path that tripped the Connectors panel
#       fail-safe). See deploy/README-meegle.md.
#
# Idempotent + safe to re-run. No gateway restart (drop-ins apply on next start;
# the binary is ensured now). Run as the hermes user.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DROPIN_DIR="${HOME}/.config/systemd/user/hermes-gateway.service.d"
HERMES_PYTHON="${HERMES_PYTHON:-${HOME}/.hermes/hermes-agent/venv/bin/python}"
mkdir -p "$DROPIN_DIR"

if [ ! -x "$HERMES_PYTHON" ]; then
  echo "install-gateway-dropins: Hermes Python is not executable" >&2
  exit 1
fi

# --- 05-multitenancy-required.conf -----------------------------------------
sed "s#@PYTHON@#${HERMES_PYTHON}#g" \
  "$REPO/deploy/hermes-gateway-multitenancy-required.conf" \
  > "$DROPIN_DIR/05-multitenancy-required.conf"
chmod 0644 "$DROPIN_DIR/05-multitenancy-required.conf"
echo "install-gateway-dropins: wrote ${DROPIN_DIR}/05-multitenancy-required.conf"

# --- 45-meegle-bin.conf (@REPO@ -> this checkout) ---------------------------
sed "s#@REPO@#${REPO}#g" "$REPO/deploy/hermes-gateway-meegle.conf" \
  > "$DROPIN_DIR/45-meegle-bin.conf"
echo "install-gateway-dropins: wrote ${DROPIN_DIR}/45-meegle-bin.conf"

# Ensure the pinned binary exists now (non-fatal — the drop-in's ExecStartPre also
# self-heals it on every gateway start).
"$REPO/deploy/ensure-meegle.sh" || true

# --- make systemd aware of the new/changed drop-ins -------------------------
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload && echo "install-gateway-dropins: daemon-reloaded"
else
  echo "install-gateway-dropins: systemctl not found; skipped daemon-reload" >&2
fi

echo "install-gateway-dropins: done (no gateway restart needed — drop-in applies on next start)"
