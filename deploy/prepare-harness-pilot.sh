#!/usr/bin/env bash
set -euo pipefail

MODE="${1:---check}"
PROFILE="${HARNESS_PROFILE:-sunke}"
SOURCE_DIR="${HARNESS_SOURCE_DIR:-$HOME/code/hermes-web-ui}"
CODEX_VERSION="${HARNESS_CODEX_VERSION:-0.150.1}"
CODEX_ROOT="${HARNESS_CODEX_ROOT:-$HOME/.hermes/bin/hermes-codex-$CODEX_VERSION}"
CODEX_BIN="${HARNESS_CODEX_BIN:-$CODEX_ROOT/node_modules/.bin/codex}"
ENV_FILE="${HARNESS_ENV_FILE:-$HOME/.hermes/harness-pilot.env}"
READY_FILE="${HARNESS_READY_FILE:-$HOME/.hermes/harness-pilot.ready}"
USER_UNIT_DIR="${HARNESS_USER_UNIT_DIR:-$HOME/.config/systemd/user}"
SYSTEMCTL="${SYSTEMCTL:-systemctl --user}"
UNITS="${HARNESS_UNITS:-hermes-gateway.service hermes-web-ui.service}"

die() { echo "harness-pilot: $*" >&2; exit 1; }
case "$PROFILE" in *[!A-Za-z0-9_.-]*|"") die "invalid pilot profile" ;; esac
[ "${HARNESS_PLATFORM:-$(uname -s)}" = Linux ] || die "Linux sandbox is required"

revision() { git -C "$SOURCE_DIR" rev-parse HEAD; }
validate() {
  local rev version
  rev=$(revision)
  [ ${#rev} -eq 40 ] || die "source revision is not a full SHA"
  [ -z "$(git -C "$SOURCE_DIR" status --porcelain)" ] || die "source repository is dirty"
  [ -x "$CODEX_BIN" ] || die "managed Codex is unavailable"
  version=$($CODEX_BIN --version | awk '{print $NF}')
  [ "$version" = "$CODEX_VERSION" ] || die "managed Codex version drift: $version"
  "$CODEX_BIN" sandbox -- /bin/true >/dev/null
  [ -f "$READY_FILE" ] || die "readiness marker is missing"
  [ "$(tr -d '[:space:]' < "$READY_FILE")" = "$rev" ] || die "readiness marker drift"
  grep -qx 'HERMES_WEBUI_HARNESS_ENABLED=1' "$ENV_FILE" || die "Harness is not enabled"
  grep -qx "HERMES_WEBUI_HARNESS_PROFILES=$PROFILE" "$ENV_FILE" || die "pilot allowlist drift"
  grep -qx "HERMES_WEBUI_HARNESS_SOURCE_REV=$rev" "$ENV_FILE" || die "source revision env drift"
  for unit in $UNITS; do $SYSTEMCTL is-active --quiet "$unit" || die "$unit is not active"; done
  echo "harness-pilot: ready profile=$PROFILE revision=${rev:0:12} codex=$CODEX_VERSION"
}

write_env() {
  local enabled=$1 rev=$2 tmp
  mkdir -p "$(dirname "$ENV_FILE")"
  tmp="$ENV_FILE.tmp.$$"
  umask 077
  {
    echo "HERMES_WEBUI_HARNESS_ENABLED=$enabled"
    echo "HERMES_WEBUI_HARNESS_PROFILES=$PROFILE"
    echo "HERMES_WEBUI_HARNESS_REPO=$SOURCE_DIR"
    echo "HERMES_WEBUI_HARNESS_SPEC_HUB=$SOURCE_DIR"
    echo "HERMES_WEBUI_HARNESS_SOURCE_REV=$rev"
    echo "HERMES_WEBUI_HARNESS_READY_FILE=$READY_FILE"
    echo "HERMES_WEBUI_HARNESS_CODEX_BIN=$CODEX_BIN"
    echo "HERMES_WEBUI_HARNESS_CODEX_VERSION=$CODEX_VERSION"
  } > "$tmp"
  mv "$tmp" "$ENV_FILE"
}

install_dropins() {
  local unit dir
  for unit in $UNITS; do
    dir="$USER_UNIT_DIR/$unit.d"
    mkdir -p "$dir"
    printf '[Service]\nEnvironmentFile=-%s\n' "$ENV_FILE" > "$dir/40-harness-pilot.conf"
  done
  $SYSTEMCTL daemon-reload
}

disable() {
  set +e
  rm -f "$READY_FILE"
  write_env 0 "$(revision 2>/dev/null || printf '%040d' 0)"
  install_dropins
  $SYSTEMCTL restart $UNITS
  set -e
}

[ "$MODE" = "--check" ] && { validate; exit 0; }
[ "$MODE" = "--prepare" ] || die "usage: $0 --check|--prepare"

if [ -f "$ENV_FILE" ] && grep -qx 'HERMES_WEBUI_HARNESS_ENABLED=1' "$ENV_FILE"; then
  disable
fi
trap 'disable' ERR

mkdir -p "$CODEX_ROOT"
npm install --prefix "$CODEX_ROOT" --no-audit --no-fund "@openai/codex@$CODEX_VERSION"
rev=$(revision)
[ ${#rev} -eq 40 ] || die "source revision is not a full SHA"
[ -z "$(git -C "$SOURCE_DIR" status --porcelain)" ] || die "source repository is dirty"
[ "$($CODEX_BIN --version | awk '{print $NF}')" = "$CODEX_VERSION" ] || die "managed Codex version check failed"
"$CODEX_BIN" sandbox -- /bin/true >/dev/null
mkdir -p "$(dirname "$READY_FILE")"
ready_tmp="$READY_FILE.tmp.$$"
printf '%s\n' "$rev" > "$ready_tmp"
mv "$ready_tmp" "$READY_FILE"
write_env 1 "$rev"
install_dropins
$SYSTEMCTL restart $UNITS
validate
trap - ERR
