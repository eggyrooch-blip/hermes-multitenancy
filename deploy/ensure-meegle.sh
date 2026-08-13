#!/usr/bin/env bash
# Ensure the feishu-project (meegle) connector reader has a FAST direct binary.
#
# Why: credential_hub.feishu_project_status() resolves meegle via
# _meegle_invocation(). With no direct `meegle` on the meegle search path it
# falls back to `npx -y @lark-project/meegle`, which RE-RESOLVES the package on
# every call (~10-32s measured on prod). That single reader dominated the Run
# Broker's cold /connectors wall-clock (~11s) and pushed the WebUI Connectors
# panel into the "凭证状态服务暂时不可用 / 检测失败" fail-safe. A direct binary
# runs in ~0.2s.
#
# _meegle_invocation() prefers a direct `meegle` found on _meegle_search_path(),
# which includes ~/.local/bin — and ~/.local/bin is on the hermes-gateway unit
# PATH. So installing meegle there makes the reader use it automatically, with
# NO code change.
#
# Install mode downloads into a staging prefix and atomically replaces only the
# current platform binary. `--check` is network-free and is the only mode used
# by gateway ExecStartPre.
set -u

PREFIX="${HERMES_MEEGLE_PREFIX:-${HOME}/.local}"
BIN="${PREFIX}/bin/meegle"
PKG="@lark-project/meegle@1.0.19"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

if [ -x "${BIN}" ] && [ "$("${BIN}" version 2>/dev/null | head -1)" = "1.0.19" ]; then
  exit 0
fi

if [ "$CHECK_ONLY" = "1" ]; then
  echo "ensure-meegle: ${PKG} is not ready in ${PREFIX}" >&2
  exit 1
fi

echo "ensure-meegle: installing ${PKG} into ${PREFIX} ..."
OWN_STAGE=0
if [ -n "${HERMES_MEEGLE_STAGE:-}" ]; then
  STAGE="$HERMES_MEEGLE_STAGE"
  mkdir -p "$STAGE"
else
  TMP_BASE="${TMPDIR:-/tmp}"
  [ -d "$TMP_BASE" ] || TMP_BASE=/tmp
  STAGE=$(mktemp -d "${TMP_BASE}/hermes-meegle.XXXXXX") || exit 1
  OWN_STAGE=1
fi
cleanup() { [ "$OWN_STAGE" = "1" ] && rm -rf "$STAGE"; }
trap cleanup EXIT

if ! npm i -g --prefix "${STAGE}" "${PKG}" >/dev/null 2>&1; then
  echo "ensure-meegle: install failed (non-fatal) — reader falls back to npx -y" >&2
  exit 0
fi

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) NATIVE="meegle-linux-x64" ;;
  Linux-aarch64|Linux-arm64) NATIVE="meegle-linux-arm64" ;;
  Darwin-x86_64) NATIVE="meegle-darwin-x64" ;;
  Darwin-arm64) NATIVE="meegle-darwin-arm64" ;;
  *) echo "ensure-meegle: unsupported platform" >&2; exit 0 ;;
esac
SOURCE="${STAGE}/lib/node_modules/@lark-project/meegle/bin/${NATIVE}"
mkdir -p "${PREFIX}/bin"
if [ -x "$SOURCE" ] && [ "$("${SOURCE}" version 2>/dev/null | head -1)" = "1.0.19" ]; then
  install -m 0755 "$SOURCE" "${PREFIX}/bin/.meegle.tmp.$$" &&
    mv -f "${PREFIX}/bin/.meegle.tmp.$$" "$BIN"
fi

if [ -x "${BIN}" ] && [ "$("${BIN}" version 2>/dev/null | head -1)" = "1.0.19" ]; then
  echo "ensure-meegle: installed ${BIN}"
else
  echo "ensure-meegle: exact binary still missing after install (non-fatal)" >&2
fi
exit 0
