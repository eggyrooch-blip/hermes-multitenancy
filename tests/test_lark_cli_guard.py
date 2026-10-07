"""lark-cli shim lanes: self-serve exec, bot-identity denial, non-strict denial.

Design note (sunke 2026-09-11): the shim's old rule was "no AUTHORIZED grant →
exit 126", which read to agents as "this environment cannot reach Feishu" and
made them fabricate output instead of retrying through the registered tool (133
denials / 25 profiles in September 2026). Credentials were never in this env —
the real binary is the authsidecar and resolves them against the broker — so the
refusal protected nothing except lark_cli_tool's narrowing of bot-identity
escalation. That narrowing is now enforced here directly and everything else
runs, so the load-bearing gate stays and the path gate goes.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from hermes_multitenancy.lark_cli_guard import install_lark_cli_shim


def _write(path: Path, body: str, *, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.fixture()
def guard_env(tmp_path: Path) -> dict:
    # The real binary echoes argv plus the two env values the shim is
    # responsible for pinning, so the tests can assert on the exec'd env.
    real_binary = _write(
        tmp_path / "bin" / "lark-cli-authsidecar",
        "#!/bin/sh\n"
        "printf 'REAL:%s\\n' \"$*\"\n"
        'printf "DEFAULT_AS:%s\\n" "${LARKSUITE_CLI_DEFAULT_AS-unset}"\n'
        'printf "AUTHORIZED:%s\\n" "${HERMES_LARK_CLI_AUTHORIZED-unset}"\n',
        mode=0o755,
    )
    shim_dir = tmp_path / "profile" / "tmp" / "lark-cli-shim"
    install_lark_cli_shim(shim_dir, real_binary=real_binary)
    audit_path = tmp_path / "audit" / "security.jsonl"
    env = {
        "PATH": os.pathsep.join(["/usr/bin", "/bin"]),
        "HERMES_PROFILE": "alice",
        "HERMES_LARK_CLI_REAL_BIN": str(real_binary),
        "HERMES_LARK_CLI_RUN_TOKEN": "run-token-value",
        "HERMES_MT_SECURITY_AUDIT_PATH": str(audit_path),
    }
    return {"shim": shim_dir / "lark-cli", "audit_path": audit_path, "env": env}


def _run(guard_env: dict, *argv: str, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    env = {**guard_env["env"], **(env_overrides or {})}
    return subprocess.run(
        [str(guard_env["shim"]), *argv], capture_output=True, text=True, env=env, check=False
    )


def _events(audit_path: Path) -> list[dict]:
    if not audit_path.exists():
        return []
    return [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]


def test_run_token_alone_self_serves_and_reaches_the_real_binary(guard_env) -> None:
    completed = _run(guard_env, "docs", "+fetch", "--doc", "Rq51da4P5oBjc3xYzx4chMc7nFb")

    assert completed.returncode == 0, completed.stderr
    assert "REAL:docs +fetch --doc Rq51da4P5oBjc3xYzx4chMc7nFb" in completed.stdout
    events = _events(guard_env["audit_path"])
    assert [event["event_type"] for event in events] == ["lark_cli.direct_exec.self_served"]
    assert events[0]["command_name"] == "lark-cli"
    assert events[0]["argv_redacted"] == "docs +fetch --doc <redacted>"


def test_self_served_call_pins_user_identity_and_withholds_the_grant(guard_env) -> None:
    # The sandbox owns this env, so an inherited DEFAULT_AS=bot must not be
    # honoured, and the AUTHORIZED grant must not leak to the child (a nested
    # lark-cli re-enters this same check and stays under the bot rule).
    completed = _run(
        guard_env,
        # a forwarded (non-diagnostic) command, so the exec'd env is observable
        "docs",
        "+fetch",
        env_overrides={
            "LARKSUITE_CLI_DEFAULT_AS": "bot",
            "HERMES_LARK_CLI_AUTHORIZED": "stale-inherited-value",
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert "DEFAULT_AS:user" in completed.stdout
    assert "AUTHORIZED:unset" in completed.stdout


@pytest.mark.parametrize(
    "identity_argv",
    [
        ("--as", "bot"),
        ("--as=bot",),
        ("--as", "BOT"),
        # last flag wins downstream, so a leading `--as user` must not launder it
        ("--as", "user", "--as", "bot"),
        ("--as", "user", "--as=bot"),
    ],
)
def test_bot_identity_is_denied_on_the_direct_path(guard_env, identity_argv) -> None:
    completed = _run(guard_env, "im", "+messages-send", *identity_argv)

    assert completed.returncode == 126
    assert "Direct execution denied for bot identity" in completed.stderr
    assert "REAL:" not in completed.stdout
    events = _events(guard_env["audit_path"])
    assert [event["event_type"] for event in events] == ["lark_cli.direct_exec.denied"]
    assert "bot identity" in events[0]["reason"]


def test_explicit_user_identity_still_self_serves(guard_env) -> None:
    completed = _run(guard_env, "docs", "+fetch", "--as", "user")

    assert completed.returncode == 0, completed.stderr
    assert "REAL:docs +fetch --as user" in completed.stdout


def test_without_run_token_the_original_denial_is_unchanged(guard_env) -> None:
    env = {key: value for key, value in guard_env["env"].items() if key != "HERMES_LARK_CLI_RUN_TOKEN"}
    completed = subprocess.run(
        [str(guard_env["shim"]), "auth", "status"], capture_output=True, text=True, env=env, check=False
    )

    assert completed.returncode == 126
    assert "Direct execution denied. Use the registered lark_cli tool." in completed.stderr
    assert 'mode="script"' in completed.stderr
    events = _events(guard_env["audit_path"])
    assert [event["event_type"] for event in events] == ["lark_cli.direct_exec.denied"]
    assert "argv_redacted" not in events[0]


def test_matching_grant_still_takes_the_authorized_fast_path(guard_env) -> None:
    completed = _run(
        guard_env, "auth", "status", env_overrides={"HERMES_LARK_CLI_AUTHORIZED": "run-token-value"}
    )

    assert completed.returncode == 0, completed.stderr
    assert "REAL:auth status" in completed.stdout
    # The sanctioned dispatch keeps its own identity handling (lark_cli_tool
    # already resolved it) and writes no direct-exec event.
    assert "AUTHORIZED:run-token-value" in completed.stdout
    assert _events(guard_env["audit_path"]) == []


def test_flag_values_and_opaque_positionals_never_reach_the_audit(guard_env) -> None:
    # A hyphen-leading payload must be consumed as the value of --content, not
    # classified as a flag name, and a trailing opaque positional is redacted.
    completed = _run(
        guard_env,
        "im",
        "+messages-send",
        "--content",
        "-private-secret",
        "sk-live-positional-secret",
    )

    assert completed.returncode == 0, completed.stderr
    row = _events(guard_env["audit_path"])[0]
    assert row["argv_redacted"] == "im +messages-send --content <redacted> <redacted>"
    serialized = json.dumps(row, ensure_ascii=False)
    assert "private-secret" not in serialized
    assert "sk-live-positional-secret" not in serialized


def test_api_path_is_kept_but_its_query_string_is_not(guard_env) -> None:
    completed = _run(
        guard_env, "api", "GET", "/open-apis/authen/v1/user_info?access_token=token-secret-value"
    )

    assert completed.returncode == 0, completed.stderr
    row = _events(guard_env["audit_path"])[0]
    assert row["argv_redacted"] == "api GET /open-apis/authen/v1/user_info"
    assert "token-secret-value" not in json.dumps(row, ensure_ascii=False)


def test_self_serve_refuses_to_exec_when_the_audit_receipt_cannot_be_written(guard_env) -> None:
    # Unwritable audit sink: self-serve trades the path gate for the trail, so
    # losing the trail must stop the call instead of running it unaudited.
    blocked = guard_env["audit_path"].parent
    blocked.mkdir(parents=True, exist_ok=True)
    guard_env["audit_path"].write_text("", encoding="utf-8")
    guard_env["audit_path"].chmod(0o400)
    blocked.chmod(0o500)
    try:
        completed = _run(guard_env, "docs", "+fetch")
    finally:
        blocked.chmod(0o700)
        guard_env["audit_path"].chmod(0o600)

    assert completed.returncode == 126
    assert "security audit receipt could not be written" in completed.stderr
    assert "REAL:" not in completed.stdout


def test_marker_alone_self_serves_when_the_run_token_never_reaches_the_child(guard_env) -> None:
    # Production shape: terminal/code tools scrub secret-looking names, so the
    # child gets the marker and no run token. This is the lane that the
    # 2026-09-11 replay proved dead before the marker existed.
    env = {key: value for key, value in guard_env["env"].items() if key != "HERMES_LARK_CLI_RUN_TOKEN"}
    env["HERMES_LARK_CLI_SELF_SERVE"] = "1"
    completed = subprocess.run(
        [str(guard_env["shim"]), "docs", "+fetch", "--doc", "Rq51da4P5oBjc3xYzx4chMc7nFb"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "REAL:docs +fetch --doc Rq51da4P5oBjc3xYzx4chMc7nFb" in completed.stdout
    events = _events(guard_env["audit_path"])
    assert [event["event_type"] for event in events] == ["lark_cli.direct_exec.self_served"]


def test_marker_alone_still_denies_bot_identity(guard_env) -> None:
    env = {key: value for key, value in guard_env["env"].items() if key != "HERMES_LARK_CLI_RUN_TOKEN"}
    env["HERMES_LARK_CLI_SELF_SERVE"] = "1"
    completed = subprocess.run(
        [str(guard_env["shim"]), "im", "+messages-send", "--as", "bot"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert completed.returncode == 126
    assert "Direct execution denied for bot identity" in completed.stderr
    assert "REAL:" not in completed.stdout


def test_marker_cannot_be_traded_for_the_authorized_fast_path(guard_env) -> None:
    # The marker is forgeable by design; what must stay impossible is using it
    # to skip the bot narrowing. A child setting AUTHORIZED to the marker value
    # gets no fast path, because AUTHORIZED must equal the (unreachable) token.
    env = {key: value for key, value in guard_env["env"].items() if key != "HERMES_LARK_CLI_RUN_TOKEN"}
    env["HERMES_LARK_CLI_SELF_SERVE"] = "1"
    env["HERMES_LARK_CLI_AUTHORIZED"] = "1"
    completed = subprocess.run(
        [str(guard_env["shim"]), "im", "+messages-send", "--as", "bot"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert completed.returncode == 126
    assert "Direct execution denied for bot identity" in completed.stderr


def test_marker_disables_the_authorized_fast_path_even_with_a_real_token(guard_env) -> None:
    # Defense in depth for review finding p1: a terminal child that somehow held
    # the run token must still not be able to mint its own grant and skip the
    # bot narrowing. The marker marks the call as model-authored shell, and that
    # outranks any AUTHORIZED value present in the env.
    completed = _run(
        guard_env,
        "im",
        "+messages-send",
        "--as",
        "bot",
        env_overrides={
            "HERMES_LARK_CLI_SELF_SERVE": "1",
            "HERMES_LARK_CLI_AUTHORIZED": "run-token-value",
        },
    )

    assert completed.returncode == 126
    assert "Direct execution denied for bot identity" in completed.stderr
    assert "REAL:" not in completed.stdout


def test_registered_tool_dispatch_is_unaffected_because_it_carries_no_marker(guard_env) -> None:
    completed = _run(
        guard_env,
        "im",
        "+messages-send",
        "--as",
        "bot",
        env_overrides={"HERMES_LARK_CLI_AUTHORIZED": "run-token-value"},
    )

    assert completed.returncode == 0, completed.stderr
    assert "REAL:im +messages-send --as bot" in completed.stdout


def test_auth_status_is_answered_locally_without_touching_the_real_binary(guard_env) -> None:
    completed = _run(guard_env, "auth", "status", env_overrides={"HERMES_LARK_CLI_SELF_SERVE": "1"})

    assert completed.returncode == 0, completed.stderr
    assert "REAL:" not in completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["data"]["identity"] == "user"
    assert payload["data"]["credentials"] == "host-managed"
    assert payload["data"]["profile"] == "alice"
    events = _events(guard_env["audit_path"])
    assert [event["event_type"] for event in events] == ["lark_cli.diagnostic.answered"]


def test_version_is_answered_locally(guard_env) -> None:
    completed = _run(guard_env, "--version")

    assert completed.returncode == 0, completed.stderr
    assert "REAL:" not in completed.stdout
    assert json.loads(completed.stdout)["data"]["diagnostic"] == "version"


def test_real_api_calls_are_still_forwarded_untouched(guard_env) -> None:
    completed = _run(guard_env, "docs", "+fetch", "--doc", "Rq51da4P5oBjc3xYzx4chMc7nFb")

    assert completed.returncode == 0, completed.stderr
    assert "REAL:docs +fetch --doc Rq51da4P5oBjc3xYzx4chMc7nFb" in completed.stdout


def test_auth_login_is_not_intercepted(guard_env) -> None:
    # Only the closed diagnostic set is answered locally; anything that could
    # actually mutate auth state keeps going to the sidecar.
    completed = _run(guard_env, "auth", "login")

    assert completed.returncode == 0, completed.stderr
    assert "REAL:auth login" in completed.stdout


def test_diagnostics_are_not_a_side_door_around_the_bot_gate(guard_env) -> None:
    completed = _run(guard_env, "auth", "status", "--as", "bot")

    assert completed.returncode == 126
    assert "Direct execution denied for bot identity" in completed.stderr
    assert "REAL:" not in completed.stdout


def test_diagnostics_still_denied_outside_the_strict_runtime(guard_env) -> None:
    env = {key: value for key, value in guard_env["env"].items() if key != "HERMES_LARK_CLI_RUN_TOKEN"}
    completed = subprocess.run(
        [str(guard_env["shim"]), "auth", "status"], capture_output=True, text=True, env=env, check=False
    )

    assert completed.returncode == 126
    assert "Direct execution denied. Use the registered lark_cli tool." in completed.stderr


def test_diagnostic_answer_claims_nothing_about_api_health(guard_env) -> None:
    # The shim can prove the managed-state shape from its own env and nothing
    # else. It must not tell the caller that Feishu calls will succeed: an
    # unreachable broker or expired credentials would make that a lie, and the
    # caller would only find out on the first real API call (review finding p1).
    completed = _run(guard_env, "auth", "status", env_overrides={"HERMES_LARK_CLI_SELF_SERVE": "1"})

    assert completed.returncode == 0, completed.stderr
    note = json.loads(completed.stdout)["data"]["note"]
    assert "NOT been checked" in note
    assert "work normally" not in note
    assert "restricted environment" not in note
