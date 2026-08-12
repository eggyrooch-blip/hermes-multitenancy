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
# Idempotent: a no-op when the binary already exists. Non-fatal by design — a
# failed/slow install must NEVER block gateway start, so this always exits 0 and
# is wired as a `ExecStartPre=-` (leading dash) in the gateway drop-in.
set -u

PREFIX="${HERMES_MEEGLE_PREFIX:-${HOME}/.local}"
BIN="${PREFIX}/bin/meegle"
PKG="@lark-project/meegle@1.0.19"

if npm ls -g --prefix "${PREFIX}" --depth=0 "${PKG}" >/dev/null 2>&1 && [ -x "${BIN}" ]; then
  exit 0
fi

echo "ensure-meegle: installing ${PKG} into ${PREFIX} ..."
if ! npm i -g --prefix "${PREFIX}" "${PKG}" >/dev/null 2>&1; then
  echo "ensure-meegle: install failed (non-fatal) — reader falls back to npx -y" >&2
  exit 0
fi

if [ -x "${BIN}" ]; then
  echo "ensure-meegle: installed ${BIN}"
else
  echo "ensure-meegle: ${BIN} still missing after install (non-fatal)" >&2
fi
exit 0
