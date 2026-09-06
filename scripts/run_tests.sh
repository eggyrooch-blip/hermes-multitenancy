#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

# Plugin discovery must never read the developer's ambient ~/.hermes profile.
# Individual tests may still replace this with their own isolated home.
HERMES_CREATED_TEST_HOME=0
if [[ -z "${HERMES_HOME:-}" ]]; then
  export HERMES_HOME="$(mktemp -d "${TMPDIR:-/tmp}/hermes-mt-tests.XXXXXX")"
  HERMES_CREATED_TEST_HOME=1
fi

command=(uv run --extra test pytest -q)
if [[ -n "${CI:-}" ]]; then
  ci_args=(
    --ignore=tests/test_lark_cli_matrix_runner.py
    --ignore=tests/test_feishu_file_media_matrix_runner.py
    --ignore=tests/test_billing_readiness.py
    --deselect tests/test_plugin_ingest.py::test_install_clis_skips_when_present
    --deselect tests/test_aiagent_subprocess.py::test_session_search_proxy_covers_real_agent_tool_dispatch
  )
  # Explicit test paths (a tiered CI job passes its own file list) bypass pytest's
  # --ignore, so the CI-only exclusions above must be applied to the argument list
  # too — otherwise the local-only matrix runners come straight back (2026-09-03,
  # tiered gate probe: 17 failed on test_lark_cli_matrix_runner in CI).
  ci_paths=()
  for arg in "$@"; do
    case "$arg" in
      tests/test_lark_cli_matrix_runner.py|tests/test_feishu_file_media_matrix_runner.py|tests/test_billing_readiness.py) ;;
      *) ci_paths+=("$arg") ;;
    esac
  done
  if [[ $# -gt 0 && ${#ci_paths[@]} -eq 0 ]]; then
    echo "run_tests.sh: every requested path is CI-excluded; nothing to run" >&2
    exit 0
  fi
  if [[ "$(id -u)" == "0" ]]; then
    if [[ "$HERMES_CREATED_TEST_HOME" == "1" ]]; then
      chown ci "$HERMES_HOME"
    fi
    printf -v shell_command '%q ' "${command[@]}" "${ci_args[@]}" "${ci_paths[@]}"
    exec su ci -c "$shell_command"
  fi
  exec "${command[@]}" "${ci_args[@]}" "${ci_paths[@]}"
fi

exec "${command[@]}" "$@"
