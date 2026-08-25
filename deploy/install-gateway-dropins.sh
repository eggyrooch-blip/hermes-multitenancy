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
#   55-litellm-billing.conf — load the optional, host-local billing environment;
#       billing remains off unless that file explicitly enables a finite cohort.
#   35-warm-worker.conf — keep the per-tenant AIAgent subprocess warm across turns
#       (installed into hermes-gateway.service.d AND hermes-gateway@.service.d,
#       because the expert bots run as templated instances that do not inherit
#       the plain unit's drop-ins)
#       so the background skill/memory review (a daemon thread) is not killed by
#       the one-shot subprocess exiting. See the file header for the measurements.
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

# --- 55-litellm-billing.conf ------------------------------------------------
install -m 0644 \
  "$REPO/deploy/hermes-gateway-litellm-billing.conf" \
  "$DROPIN_DIR/55-litellm-billing.conf"
echo "install-gateway-dropins: wrote ${DROPIN_DIR}/55-litellm-billing.conf"

# --- 35-warm-worker.conf ----------------------------------------------------
# Installed into BOTH the plain gateway unit and the templated one. The expert
# bots run as hermes-gateway@<profile>.service instances, and a drop-in under
# hermes-gateway.service.d/ does NOT apply to them — verified on production
# 2026-08-19: the expert_krd process had no HERMES_AIAGENT_WARM_WORKER at all.
# Without the template drop-in, every expert-bot tenant keeps the killed-review
# bug while gateway-only greps look green.
TEMPLATE_DROPIN_DIR="${HOME}/.config/systemd/user/hermes-gateway@.service.d"
mkdir -p "$TEMPLATE_DROPIN_DIR"
for _warm_dir in "$DROPIN_DIR" "$TEMPLATE_DROPIN_DIR"; do
  install -m 0644 \
    "$REPO/deploy/hermes-gateway-warm-worker.conf" \
    "$_warm_dir/35-warm-worker.conf"
  echo "install-gateway-dropins: wrote ${_warm_dir}/35-warm-worker.conf"
done

# --- 45-meegle-bin.conf (@REPO@ -> this checkout) ---------------------------
sed "s#@REPO@#${REPO}#g" "$REPO/deploy/hermes-gateway-meegle.conf" \
  > "$DROPIN_DIR/45-meegle-bin.conf"
echo "install-gateway-dropins: wrote ${DROPIN_DIR}/45-meegle-bin.conf"

# Provisioning installs while the gateway remains online. Release sets
# HERMES_MEEGLE_PREPARED=1 after its pre-stop prepare, so this post-stop phase
# can only perform the network-free exact-version check.
if [ "${HERMES_MEEGLE_PREPARED:-0}" != "1" ]; then
  "$REPO/deploy/ensure-meegle.sh"
fi
"$REPO/deploy/ensure-meegle.sh" --check

# --- make systemd aware of the new/changed drop-ins -------------------------
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload && echo "install-gateway-dropins: daemon-reloaded"
else
  echo "install-gateway-dropins: systemctl not found; skipped daemon-reload" >&2
fi

echo "install-gateway-dropins: done (no gateway restart needed — drop-in applies on next start)"
