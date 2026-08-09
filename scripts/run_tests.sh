#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

command=(uv run --extra test pytest -q)
if [[ -n "${CI:-}" ]]; then
  ci_args=(
    --ignore=tests/test_lark_cli_matrix_runner.py
    --ignore=tests/test_feishu_file_media_matrix_runner.py
    --ignore=tests/test_billing_readiness.py
    --deselect tests/test_plugin_ingest.py::test_install_clis_skips_when_present
    --deselect tests/test_aiagent_subprocess.py::test_session_search_proxy_covers_real_agent_tool_dispatch
  )
  if [[ "$(id -u)" == "0" ]]; then
    printf -v shell_command '%q ' "${command[@]}" "${ci_args[@]}" "$@"
    exec su ci -c "$shell_command"
  fi
  exec "${command[@]}" "${ci_args[@]}" "$@"
fi

exec "${command[@]}" "$@"
