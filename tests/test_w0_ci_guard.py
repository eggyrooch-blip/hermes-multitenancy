from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ci_runs_w0_harness_and_pins_closed_security_gaps():
    workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
    for target in (
        "tests/test_w0_ci_guard.py",
        "tests/test_executor_runtime_map.py",
        "tests/test_codex_provider_proxy.py",
        "tests/test_codex_home.py",
        "tests/test_run_workspace_env.py",
        "tests/test_codex_runtime_key_env.py",
        "tests/test_codex_runtime_integration.py",
        "tests/test_agent_mode_env.py",
        "tests/test_git_auth_env.py",
        "tests/test_single_actor_spend_receipt.py",
        "tests/test_gitlab_owner_scope_attestation.py",
        "tests/test_webui_broker_server.py::test_webui_authenticated_owner_is_sealed_before_default_dispatch",
        "tests/test_webui_broker_server.py::test_default_dispatch_attaches_only_server_prepared_codex_evidence",
        "tests/test_aiagent_subprocess.py::test_build_subprocess_env_drops_non_allowlisted_parent_env",
        "tests/test_aiagent_subprocess.py::test_mapped_stream_commits_state_only_after_receipt",
    ):
        assert target in workflow, f"W0 CI target removed: {target}"

    codex_home = (ROOT / "hermes_multitenancy/agent_real/codex_home.py").read_text(
        encoding="utf-8"
    )
    workspace = (ROOT / "hermes_multitenancy/agent_real/run_workspace.py").read_text(
        encoding="utf-8"
    )
    proxy = (ROOT / "hermes_multitenancy/agent_real/codex_provider_proxy.py").read_text(
        encoding="utf-8"
    )
    receipt_gate = (ROOT / "hermes_multitenancy/agent_real/_core.py").read_text(
        encoding="utf-8"
    )
    assert '"plugins = false"' in codex_home
    assert '"requires_openai_auth = false"' in codex_home
    assert 'payload["store"] = False' in proxy
    assert 'proxy_audit.get("store_forced") is not True' in receipt_gate
    assert "CLONE_TIMEOUT_SECONDS = 120" in workspace
