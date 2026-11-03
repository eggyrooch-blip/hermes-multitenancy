#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${LARK_CLI_SOURCE_DIR:-${1:-/tmp/codex-feishu-uat-compare/larksuite-cli}}"
OUTPUT_BIN="${HERMES_LARK_CLI_BIN:-${HOME}/.hermes/bin/lark-cli-authsidecar}"
EXPECTED_VERSION="${LARK_CLI_EXPECTED_VERSION:-1.0.86}"
EXPECTED_SOURCE_HEAD="${LARK_CLI_EXPECTED_SOURCE_HEAD:-defd27b9d2f976fe35337b83071d7f11ee0cb1d3}"

if [[ ! -d "${SOURCE_DIR}" ]]; then
  cat >&2 <<EOF
lark-cli source directory not found: ${SOURCE_DIR}

Set LARK_CLI_SOURCE_DIR to a checked-out larksuite/cli repo, for example:
  git clone https://github.com/larksuite/cli /tmp/codex-feishu-uat-compare/larksuite-cli
EOF
  exit 2
fi

if [[ ! -f "${SOURCE_DIR}/go.mod" ]]; then
  echo "lark-cli source directory has no go.mod: ${SOURCE_DIR}" >&2
  exit 2
fi

actual_source_head="$(git -C "${SOURCE_DIR}" rev-parse HEAD 2>/dev/null || true)"
if [[ -z "${EXPECTED_SOURCE_HEAD}" || "${actual_source_head}" != "${EXPECTED_SOURCE_HEAD}" ]]; then
  echo "lark-cli source HEAD mismatch" >&2
  exit 3
fi

mkdir -p "$(dirname "${OUTPUT_BIN}")"

(
  cd "${SOURCE_DIR}"
  version="${EXPECTED_VERSION:-${EXPECTED_SOURCE_HEAD:-DEV}}"
  go build -trimpath -tags authsidecar \
    -ldflags "-s -w -X github.com/larksuite/cli/internal/build.Version=${version}" \
    -o "${OUTPUT_BIN}" .
)

chmod 0755 "${OUTPUT_BIN}"

version_output="$("${OUTPUT_BIN}" --version 2>&1 || true)"
if [[ -n "${EXPECTED_VERSION}" && "${version_output}" != *"${EXPECTED_VERSION}"* ]]; then
  echo "built lark-cli version mismatch: expected ${EXPECTED_VERSION} or source ${EXPECTED_SOURCE_HEAD}, got: ${version_output}" >&2
  exit 3
fi

sidecar_probe="$(
  env \
    LARKSUITE_CLI_AUTH_PROXY="http://127.0.0.1:9" \
    LARKSUITE_CLI_PROXY_KEY="probe-key" \
    LARKSUITE_CLI_APP_ID="cli_probe" \
    LARKSUITE_CLI_BRAND="feishu" \
    "${OUTPUT_BIN}" --version 2>&1 || true
)"

if [[ "${sidecar_probe}" == *"WITHOUT the 'authsidecar' build tag"* ]]; then
  echo "built lark-cli is missing authsidecar support" >&2
  exit 4
fi

echo "built ${OUTPUT_BIN}"
echo "${version_output}"
