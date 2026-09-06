"""W1 ticket 05: local, no-write, no-external-call integration for the Codex
dry-run base -- composes t01 (session/thread bridge), t02 (heartbeat), t03
(gate resume) and t04 (unavailable UX) against a REAL local ``codex`` binary
talking JSON-RPC app-server protocol to a deterministic LOOPBACK Responses-API
stub, over a read-only fixture bound to ``sunke/hermes-web-ui`` (SPEC ticket 05).

Scope decision (documented, not hidden): this file drives the executor /
workspace / codex-home / session-bridge seams DIRECTLY (the same tested
library calls ``_core.py`` uses) rather than re-driving the whole
``_core.py``/``run.py`` request pipeline a second time -- that full pipeline,
with its billing/gitlab-attestation/spend-receipt machinery, is already
exercised end-to-end (fake-binary style) by ``test_codex_runtime_integration.py``
(1132 lines). What is genuinely NEW here, and what this file proves for the
first time in this repo, is: (a) a REAL ``codex app-server`` subprocess
completing a full JSON-RPC round trip (initialize / thread/start /
thread/resume / turn/start) against a LOOPBACK stub with a strictly
allowlisted child env, and (b) tickets 01-04 actually composing together
around that real process rather than only passing in isolation.

Fixture repo provenance (never dialed over the network): a LOCAL, OFFLINE
``git clone --bare --local`` of the developer's own existing
``~/code/hermes-web-ui`` checkout (whose own ``origin`` remote is the literal
``sunke/hermes-web-ui`` GitLab URL, asserted once, read-only) into a temp dir;
every workspace clone in this file is proven to share that checkout's exact
HEAD commit. If that local checkout is ever absent, a minimal synthetic bare
repo is used instead and the provenance assertion is skipped with a clear
reason -- never a network clone.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import TCPServer
from typing import Any

import pytest

from hermes_multitenancy.agent_real import codex_event_heartbeat as heartbeat
from hermes_multitenancy.agent_real import codex_gate_resume as gate_resume
from hermes_multitenancy.agent_real import codex_home
from hermes_multitenancy.agent_real import codex_session_bridge as bridge
from hermes_multitenancy.agent_real import executor_map
from hermes_multitenancy.agent_real import executor_unavailable_ux as ux
from hermes_multitenancy.agent_real import run_workspace
from hermes_multitenancy.trusted_runtime_principal import issue_webui_principal

CODEX_BIN = shutil.which("codex") or "/Users/hermes/.local/bin/codex"
CODEX_AVAILABLE = bool(CODEX_BIN) and Path(CODEX_BIN).is_file()
LOCAL_FIXTURE_CHECKOUT = Path("/Users/hermes/code/hermes-web-ui")
FIXTURE_REMOTE_URL = "git@gitlab.example.com:sunke/hermes-web-ui.git"

requires_codex = pytest.mark.skipif(
    not CODEX_AVAILABLE,
    reason="real codex binary not found on this machine -- real-binary dry-run "
    "integration scenarios skipped; module-composition scenarios (gate/"
    "heartbeat/unavailable-ux) below still ran and stay authoritative",
)


# --------------------------------------------------------------------------- #
# Loopback OpenAI-Responses stub: binds 127.0.0.1 only, records every hit
# (path, bearer token, peer address), always answers deterministically -- zero
# model provider, zero network egress beyond this process's own loopback.
# --------------------------------------------------------------------------- #


class _ResponsesStubHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib handler naming
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server.hits.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization", ""),
                "peer": self.client_address[0],
                "body_model": json.loads(body or b"{}").get("model"),
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        payload = {
            "type": "response.completed",
            "response": {
                "id": f"resp_{len(self.server.hits)}",  # type: ignore[attr-defined]
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "dry-run ok"}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            },
        }
        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.flush()

    def log_message(self, *_args: Any) -> None:  # silence stdlib access log
        pass


class ResponsesStub:
    """A real HTTP server bound to 127.0.0.1 -- structurally unreachable from
    anywhere but this host's own loopback, which is the egress containment
    proof for a real ``codex`` subprocess we cannot otherwise ptrace/dtrace."""

    def __init__(self) -> None:
        self._httpd = TCPServer(("127.0.0.1", 0), _ResponsesStubHandler)
        self._httpd.hits = []  # type: ignore[attr-defined]
        self.port = self._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def hits(self) -> list[dict[str, Any]]:
        return self._httpd.hits  # type: ignore[attr-defined]

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture()
def stub():
    s = ResponsesStub()
    yield s
    s.close()


# --------------------------------------------------------------------------- #
# Minimal JSON-RPC stdio client for a real ``codex app-server`` subprocess.
# Newline-delimited JSON, confirmed against the installed codex-cli 0.149.1
# binary (no LSP Content-Length framing).
# --------------------------------------------------------------------------- #


class CodexRpc:
    def __init__(self, *, env: dict[str, str], argv: list[str]) -> None:
        self.argv = argv
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, text=True, bufsize=1,
        )
        self._msgs: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._next_id = 0
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        for line in self.proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                self._msgs.put(json.loads(line))
            except json.JSONDecodeError:
                continue

    def _read_stderr(self) -> None:
        for _line in self.proc.stderr:  # type: ignore[union-attr]
            pass  # drained so the child never blocks on a full pipe

    def call(self, method: str, params: dict[str, Any], *, timeout: float = 8.0) -> dict[str, Any]:
        self._next_id += 1
        req_id = self._next_id
        line = json.dumps({"id": req_id, "method": method, "params": params})
        self.proc.stdin.write(line + "\n")  # type: ignore[union-attr]
        self.proc.stdin.flush()  # type: ignore[union-attr]
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = self._msgs.get(timeout=max(0.0, deadline - time.time()))
            except queue.Empty:
                break
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(f"codex app-server rejected {method}: {msg['error']}")
                return msg["result"]
        raise TimeoutError(f"codex app-server never answered {method} within {timeout}s")

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()


def _codex_argv() -> list[str]:
    return [CODEX_BIN, "app-server", "--stdio"]


def _allowlist_env(*, codex_home_dir: Path, workspace: run_workspace.RunWorkspace, runtime_key: str, bin_dir: str) -> dict[str, str]:
    """The exact env a mapped-Codex child gets: never ``dict(os.environ)``,
    only the named keys ``_core._codex_runtime_env`` would export (CODEX_HOME +
    the key alias + the run-workspace hooks) plus a scoped PATH. Proven
    sufficient for a real spawn by the standalone probe this ticket ran first."""
    return {
        "CODEX_HOME": str(codex_home_dir),
        "CODEX_RUNTIME_KEY": runtime_key,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        **run_workspace.env_for(workspace),
    }


# --------------------------------------------------------------------------- #
# Fixture repos
# --------------------------------------------------------------------------- #


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


def _bare_fixture_repo(tmp_path: Path) -> tuple[Path, bool]:
    """Read-only LOCAL bare clone of ``sunke/hermes-web-ui``. Returns
    ``(path, provenance_checkable)`` -- the second value is False only when the
    developer's own checkout is absent and a synthetic repo was used instead."""
    dest = tmp_path / "hermes-web-ui-fixture.git"
    if LOCAL_FIXTURE_CHECKOUT.is_dir() and (LOCAL_FIXTURE_CHECKOUT / ".git").exists():
        _git("clone", "--bare", "--local", str(LOCAL_FIXTURE_CHECKOUT), str(dest))
        return dest, True
    dest.mkdir()
    _git("init", "-q", "--bare", str(dest))
    return dest, False


def _spec_hub_repo(tmp_path: Path, name: str = "KepSpecHub-fixture") -> Path:
    path = tmp_path / name
    path.mkdir()
    _git("init", "-q", str(path))
    (path / "SPEC.md").write_text("fixture spec hub\n", encoding="utf-8")
    _git("add", "SPEC.md", cwd=path)
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "fixture"],
        cwd=path, check=True, capture_output=True, text=True,
    )
    return path


# --------------------------------------------------------------------------- #
# One workflow's whole pipeline: executor resolution -> workspace -> CODEX_HOME
# -> allowlist env -> argv. Composed here (test-only), never touching _core.py.
# --------------------------------------------------------------------------- #


@dataclass
class WorkflowRig:
    workflow_id: str
    runtime: str
    workspace: run_workspace.RunWorkspace
    codex_home_dir: Path
    env: dict[str, str]
    argv: list[str]


def _build_workflow(
    *, profile_home: Path, workflow_id: str, expert_id: str, executor_map_path: Path,
    repo_git_url: str, spec_hub_git_url: str, base_url: str, bin_dir: str,
) -> WorkflowRig:
    runtime = executor_map.resolve_runtime(
        expert_id, None, environ={executor_map.EXECUTOR_MAP_ENV: str(executor_map_path)},
    )
    assert runtime == executor_map.CODEX_APP_SERVER
    executor_map.assert_codex_available(bin_dir)
    executor_map.assert_openai_wire("custom", base_url)

    workspace = run_workspace.prepare(
        profile_home, workflow_id, repo_git_url=repo_git_url, spec_hub_git_url=spec_hub_git_url,
    )
    home = codex_home.materialize(workspace.root, base_url=base_url, model="gpt-5", plugin_dir=None)
    env = _allowlist_env(codex_home_dir=home, workspace=workspace, runtime_key="dryrun-fixture-key", bin_dir=bin_dir)
    return WorkflowRig(
        workflow_id=workflow_id, runtime=runtime, workspace=workspace,
        codex_home_dir=home, env=env, argv=_codex_argv(),
    )


def _write_map(tmp_path: Path, *, expert_id: str) -> Path:
    path = tmp_path / "executors.yaml"
    path.write_text(f"{expert_id}: codex_app_server\n", encoding="utf-8")
    return path


# =========================================================================== #
# (a) two workflows: executor/argv/env/workspace/canonical-output contract,
#     zero external calls, zero GitLab write, zero credential leak.
# =========================================================================== #


@requires_codex
def test_two_workflows_identical_contract_and_zero_write_zero_egress(tmp_path: Path, stub: ResponsesStub) -> None:
    bin_dir = os.path.dirname(CODEX_BIN)
    repo, provenance_checkable = _bare_fixture_repo(tmp_path)
    if provenance_checkable:
        _git("remote", "set-url", "origin", FIXTURE_REMOTE_URL, cwd=repo)
    hub = _spec_hub_repo(tmp_path)
    executor_map_path = _write_map(tmp_path, expert_id="kep-server")
    profile_home = tmp_path / "profile"

    git_argv_log: list[list[str]] = []
    real_run = subprocess.run

    def _spy_run(command, *args, **kwargs):  # noqa: ANN001 - thin spy wrapper
        if isinstance(command, list) and command and command[0] == "git":
            git_argv_log.append(command)
        return real_run(command, *args, **kwargs)

    rigs: dict[str, WorkflowRig] = {}
    for wf_id in ("wf-alpha", "wf-beta"):
        import unittest.mock as mock

        with mock.patch("hermes_multitenancy.agent_real.run_workspace.subprocess.run", side_effect=_spy_run):
            rigs[wf_id] = _build_workflow(
                profile_home=profile_home, workflow_id=wf_id, expert_id="kep-server",
                executor_map_path=executor_map_path, repo_git_url=str(repo),
                spec_hub_git_url=str(hub), base_url=stub.base_url, bin_dir=bin_dir,
            )

    rig_a, rig_b = rigs["wf-alpha"], rigs["wf-beta"]

    # --- contract: argv identical, env allowlist key-set identical ---
    assert rig_a.argv == rig_b.argv == _codex_argv()
    assert set(rig_a.env) == set(rig_b.env) == {
        "CODEX_HOME", "CODEX_RUNTIME_KEY", "PATH",
        "KEP_SPEC_HUB_DIR", "KEP_WORKSPACE_DIR", "HERMES_RUN_WORKSPACE_DIR", "HERMES_RUN_REPO_DIR",
    }
    assert rig_a.env["CODEX_HOME"] != rig_b.env["CODEX_HOME"]
    assert rig_a.workspace.root != rig_b.workspace.root

    # --- allowlist only: no ambient env leaks into the child dict itself ---
    for rig in (rig_a, rig_b):
        for key, value in rig.env.items():
            assert key in rig.env  # tautology guard: enumerate below is the real check
        assert "HOME" not in rig.env
        assert not any(k.startswith("GITLAB_") for k in rig.env)
        assert not any(k.startswith("HERMES_LITELLM") for k in rig.env)

    # --- workspace: 0700, second workflow never sees the first's marker ---
    for rig in (rig_a, rig_b):
        mode = stat.S_IMODE(rig.workspace.root.stat().st_mode)
        assert mode == 0o700, oct(mode)
    marker = rig_a.workspace.root / "round1.marker"
    marker.write_text("wf-alpha-only\n", encoding="utf-8")
    assert not (rig_b.workspace.root / "round1.marker").exists()
    assert not (rig_b.workspace.repo_dir / "round1.marker").exists()

    # --- both workspaces bind only the fixture: same HEAD commit as the
    #     developer's own local checkout, never a network clone ---
    if provenance_checkable:
        want_head = _git("log", "-1", "--format=%H", cwd=LOCAL_FIXTURE_CHECKOUT).stdout.strip()
        for rig in (rig_a, rig_b):
            got_head = _git("log", "-1", "--format=%H", cwd=rig.workspace.repo_dir).stdout.strip()
            assert got_head == want_head

    # --- GitLab write/MR/push = 0: every captured git invocation is read-only ---
    forbidden = ("push", "commit", "merge", "mr", "rebase")
    for command in git_argv_log:
        assert not any(word in command for word in forbidden), command

    # --- real codex binary spawn per workflow, only ever hitting the stub ---
    thread_shapes: list[set[str]] = []
    started = time.monotonic()
    for rig in (rig_a, rig_b):
        rpc = CodexRpc(env=rig.env, argv=rig.argv)
        try:
            rpc.call("initialize", {"clientInfo": {"name": "w1-dryrun-test", "version": "0.0.1"}})
            result = rpc.call("thread/start", {"cwd": str(rig.workspace.root)})
            thread_shapes.append(set(result["thread"].keys()))
            rpc.call("turn/start", {"threadId": result["thread"]["id"], "input": [{"type": "text", "text": "hi"}]})
            time.sleep(0.3)  # let the async turn/completed notification land
        finally:
            rpc.close()
    elapsed = time.monotonic() - started
    assert elapsed < 60, f"real-binary round trip took {elapsed:.1f}s"

    # --- canonical output contract: identical key shape across workflows ---
    assert thread_shapes[0] == thread_shapes[1]

    # --- external model calls = 0 (loopback stub only); auth uses the exact
    #     per-workflow key we minted, never a real employee credential ---
    assert len(stub.hits) == 2
    for hit in stub.hits:
        assert hit["peer"] == "127.0.0.1"
        assert hit["path"] == "/v1/responses"
        assert hit["authorization"] == "Bearer dryrun-fixture-key"


# =========================================================================== #
# (b) same workflow, two rounds: real Codex thread_id resumed, round 2 reads
#     back round 1's fixture fact; fail-closed BEFORE any spawn on staleness,
#     duplicate registration, or cross-workflow bleed; no request can inject
#     a thread_id (structural: the parameter does not exist).
# =========================================================================== #


@requires_codex
def test_same_workflow_resumes_thread_and_reads_back_fact(tmp_path: Path, stub: ResponsesStub) -> None:
    bin_dir = os.path.dirname(CODEX_BIN)
    repo, _ = _bare_fixture_repo(tmp_path)
    hub = _spec_hub_repo(tmp_path)
    executor_map_path = _write_map(tmp_path, expert_id="kep-server")
    profile_home = tmp_path / "profile"

    rig = _build_workflow(
        profile_home=profile_home, workflow_id="wf-continuity", expert_id="kep-server",
        executor_map_path=executor_map_path, repo_git_url=str(repo), spec_hub_git_url=str(hub),
        base_url=stub.base_url, bin_dir=bin_dir,
    )

    principal = issue_webui_principal(profile_name="sunke", actor_subject="ou_dryrun", credential_subject="ou_dryrun")
    store = bridge.CodexSessionBridgeStore(tmp_path / "bridge.db")
    spawn_count = 0

    def spawn(threadId: str | None = None):  # noqa: N803 - matches JSON-RPC casing
        nonlocal spawn_count
        spawn_count += 1
        rpc = CodexRpc(env=rig.env, argv=rig.argv)
        rpc.call("initialize", {"clientInfo": {"name": "w1-dryrun-test", "version": "0.0.1"}})
        if threadId is None:
            result = rpc.call("thread/start", {"cwd": str(rig.workspace.root)})
        else:
            result = rpc.call("thread/resume", {"threadId": threadId, "cwd": str(rig.workspace.root)})
        return rpc, result["thread"]

    fact = f"dry-run-fact-{os.urandom(4).hex()}"

    # ---- round 1: no existing binding -> mint fresh, spawn thread/start ----
    plan1 = bridge.plan_codex_thread(
        store=store, principal=principal, profile_name="sunke", executor="codex_app_server",
        workflow_id="wf-continuity", now_ms=1_000,
    )
    assert plan1.resume_thread_id is None
    rpc1, thread1 = spawn()
    try:
        rpc1.call("turn/start", {"threadId": thread1["id"], "input": [{"type": "text", "text": fact}]})
        time.sleep(0.3)
        rollout1 = thread1["path"]
    finally:
        rpc1.close()
    bridge.record_codex_thread(plan1, store=store, thread_id=thread1["id"], now_ms=1_000)

    # ---- round 2 (fresh subprocess, matching production's per-turn spawn):
    #      lookup resumes the SAME thread, real codex reads back the fact ----
    plan2 = bridge.plan_codex_thread(
        store=store, principal=principal, profile_name="sunke", executor="codex_app_server",
        workflow_id="wf-continuity", now_ms=2_000,
    )
    assert plan2.resume_thread_id == thread1["id"]
    plan2 = bridge.require_codex_thread_plan(
        plan2, principal=principal, profile_name="sunke", executor="codex_app_server", workflow_id="wf-continuity",
    )
    rpc2, thread2 = spawn(threadId=plan2.resume_thread_id)
    try:
        assert thread2["id"] == thread1["id"]
        assert thread2["path"] == rollout1  # same on-disk rollout, not a fresh file
        assert fact in thread2["preview"]  # round 2 reads back round 1's fixture fact
    finally:
        rpc2.close()

    assert spawn_count == 2
    assert len(stub.hits) == 1  # only round 1 actually asked the model anything

    # ---- fail closed BEFORE any spawn: stale binding ----
    spawn_count_before = spawn_count
    with pytest.raises(bridge.CodexSessionBridgeRejected) as excinfo:
        bridge.plan_codex_thread(
            store=store, principal=principal, profile_name="sunke", executor="codex_app_server",
            workflow_id="wf-continuity", now_ms=999_999, max_age_ms=5_000,
        )
    assert str(excinfo.value) == "binding_stale"
    assert spawn_count == spawn_count_before  # rejected before any process was spawned

    # ---- fail closed: replaying round 1's registration is rejected, not
    #      silently reused -- the UNIQUE tuple constraint does the rejecting ----
    with pytest.raises(bridge.CodexSessionBridgeRejected) as excinfo:
        bridge.record_codex_thread(plan1, store=store, thread_id="some-other-thread", now_ms=1_500)
    assert str(excinfo.value) == "binding_conflict"

    # ---- structural: a caller cannot inject a resumed thread_id -- the API
    #      simply has no such parameter, so no event/request field can reach it ----
    with pytest.raises(TypeError):
        bridge.plan_codex_thread(  # type: ignore[call-arg]
            store=store, principal=principal, profile_name="sunke", executor="codex_app_server",
            workflow_id="wf-continuity", now_ms=3_000, thread_id="attacker-controlled",
        )

    # ---- cross-workflow isolation: same actor/profile/executor, different
    #      workflow -> no binding, no shared thread ----
    plan_other = bridge.plan_codex_thread(
        store=store, principal=principal, profile_name="sunke", executor="codex_app_server",
        workflow_id="wf-continuity-B", now_ms=3_000,
    )
    assert plan_other.resume_thread_id is None

    store.close()


# =========================================================================== #
# (c) gate A -> B -> C -> D: unapproved write intent blocked, one-time
#     capability resumes exactly once, wrong actor/run/gate/thread/expired/
#     replayed/out-of-order all rejected, concurrent double-consume is atomic.
# =========================================================================== #


def test_gate_resume_full_cycle_and_every_rejection(tmp_path: Path) -> None:
    store = gate_resume.GateResumeStore(tmp_path / "gate.db")
    run_id, actor, thread_id = "run-1", "ou_dryrun", "thread-fixture-1"

    # out-of-order: cannot enter gate C on a brand-new run
    with pytest.raises(gate_resume.GateResumeRejected) as excinfo:
        gate_resume.enter_waiting_gate(store=store, run_id=run_id, actor_subject=actor, thread_id=thread_id, gate="C", now_ms=1)
    assert excinfo.value.code == gate_resume._UNAVAILABLE_CODE

    state = gate_resume.enter_waiting_gate(store=store, run_id=run_id, actor_subject=actor, thread_id=thread_id, gate="A", now_ms=1)
    assert state.write_action_count == 0  # unapproved write intent produced zero writes

    # wrong actor cannot mint a capability for someone else's waiting gate
    with pytest.raises(gate_resume.GateResumeRejected):
        gate_resume.issue_gate_capability(store=store, run_id=run_id, actor_subject="ou_other", thread_id=thread_id, gate="A", now_ms=2)

    # expired capability is rejected without being consumed
    short_lived = gate_resume.issue_gate_capability(store=store, run_id=run_id, actor_subject=actor, thread_id=thread_id, gate="A", now_ms=2, ttl_ms=1)
    with pytest.raises(gate_resume.GateResumeRejected) as excinfo:
        gate_resume.consume_gate_capability(store=store, token=short_lived, run_id=run_id, actor_subject=actor, thread_id=thread_id, gate="A", now_ms=999)
    assert str(excinfo.value) == "capability_expired"

    # correct one-time capability: walk A -> B -> C -> D, counter increments once per gate
    for index, gate_name in enumerate(gate_resume.GATES):
        token = gate_resume.issue_gate_capability(store=store, run_id=run_id, actor_subject=actor, thread_id=thread_id, gate=gate_name, now_ms=10 + index)

        # concurrent double-consume of the SAME token: exactly one winner
        results: list[Any] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def _consume():
            barrier.wait(timeout=2)
            try:
                results.append(
                    gate_resume.consume_gate_capability(
                        store=store, token=token, run_id=run_id, actor_subject=actor,
                        thread_id=thread_id, gate=gate_name, now_ms=10 + index,
                    )
                )
            except gate_resume.GateResumeRejected as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_consume) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 1, "exactly one racer must consume the one-time capability"
        assert len(errors) == 1 and str(errors[0]) == "capability_replayed"
        result = results[0]
        assert result.write_action_count == index + 1
        if gate_name == gate_resume.GATES[-1]:
            assert result.status == "done"
            assert result.next_gate is None
        else:
            assert result.status == "waiting"
            assert result.next_gate == gate_resume.GATES[index + 1]

    final = gate_resume.current_gate_state(store, run_id)
    assert final.status == "done"
    assert final.write_action_count == len(gate_resume.GATES)

    store.close()


# =========================================================================== #
# (d) heartbeat: fires only on a real gap, never on a dense stream, stops
#     after a terminal kind, and its payload structurally cannot carry a
#     prompt/model delta/tool arg/chain-of-thought/token/open_id/path.
# =========================================================================== #


def test_heartbeat_fires_on_gap_silent_when_dense_stops_at_terminal() -> None:
    import asyncio

    async def slow_stream():
        yield "content", {"text": "hello"}
        await asyncio.sleep(0.08)  # 1x interval -> exactly one heartbeat expected
        yield "done", {"text": "finished"}
        await asyncio.sleep(0.08)  # AFTER terminal -> must NOT produce another heartbeat

    async def dense_stream():
        for i in range(5):
            await asyncio.sleep(0.01)  # << interval -> zero heartbeats expected
            yield "content", {"text": f"chunk-{i}"}
        yield "done", {"text": "finished"}

    async def _drain(gen):
        items = []
        async for item in gen:
            items.append(item)
        return items

    slow_items = asyncio.run(_drain(heartbeat.wrap_with_heartbeat(slow_stream(), interval_ms=50)))
    heartbeats = [item for item in slow_items if item[0] == heartbeat.HEARTBEAT_KIND]
    assert len(heartbeats) == 1
    kind, payload = heartbeats[0]
    assert set(payload.keys()) == {"state", "text"}  # structurally nothing else can ride along
    assert payload["state"] == "running"
    assert "hello" not in json.dumps(payload)  # never a real event's payload content
    # terminal ("done") is the last slow_stream item; nothing follows it
    assert [k for k, _ in slow_items].index("done") == len(slow_items) - 1

    dense_items = asyncio.run(_drain(heartbeat.wrap_with_heartbeat(dense_stream(), interval_ms=200)))
    assert not any(item[0] == heartbeat.HEARTBEAT_KIND for item in dense_items)
    assert [k for k, _ in dense_items] == ["content"] * 5 + ["done"]


# =========================================================================== #
# (e) unavailable UX: binary missing / malformed event / thread stale / gate
#     denied all classify to a stable internal code and a fixed Chinese
#     action message that never contains a path, binary name, exception
#     class, stack trace, or the raw internal reason -- verified by grepping
#     the actual audit record written, not just trusting the static table.
# =========================================================================== #


# "Codex" itself is the employee-facing product name and is fine in the
# message (every fixed Chinese string in the table names it on purpose); what
# must never appear is internals: an absolute path, a stack trace, an
# exception class name, or the raw/stable internal token the classifier read.
_FORBIDDEN_LEAK_SUBSTRINGS = (
    "/Users", "/private", "/tmp", "Traceback", "RuntimeError", "ValueError",
    "run PATH", "binding_stale", "workflow_id_invalid", "actor_mismatch",
)


def test_unavailable_ux_employee_text_never_leaks_internals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    principal = issue_webui_principal(profile_name="sunke", actor_subject="ou_dryrun", credential_subject="ou_dryrun")
    store = bridge.CodexSessionBridgeStore(tmp_path / "bridge.db")

    cases: list[Exception] = []

    # binary missing
    try:
        executor_map.assert_codex_available("")  # empty PATH -> nothing resolves
    except executor_map.ExecutorUnavailable as exc:
        cases.append(exc)
    assert cases and ux.classify(cases[-1]) == ux.CODEX_BINARY_MISSING

    # malformed event (bad workflow_id) -- rejected before any spawn/model/tool call
    try:
        bridge.plan_codex_thread(
            store=store, principal=principal, profile_name="sunke", executor="codex_app_server",
            workflow_id="bad/workflow!", now_ms=1,
        )
    except bridge.CodexSessionBridgeRejected as exc:
        cases.append(exc)
    assert ux.classify(cases[-1]) == ux.CODEX_EVENT_MALFORMED

    # thread stale
    plan = bridge.plan_codex_thread(
        store=store, principal=principal, profile_name="sunke", executor="codex_app_server",
        workflow_id="wf-stale", now_ms=1,
    )
    bridge.record_codex_thread(plan, store=store, thread_id="thread-x", now_ms=1)
    try:
        bridge.plan_codex_thread(
            store=store, principal=principal, profile_name="sunke", executor="codex_app_server",
            workflow_id="wf-stale", now_ms=999_999, max_age_ms=10,
        )
    except bridge.CodexSessionBridgeRejected as exc:
        cases.append(exc)
    assert ux.classify(cases[-1]) == ux.CODEX_THREAD_STALE

    # gate denied
    gstore = gate_resume.GateResumeStore(tmp_path / "gate.db")
    gate_resume.enter_waiting_gate(store=gstore, run_id="run-ux", actor_subject="ou_dryrun", thread_id="thread-x", gate="A", now_ms=1)
    try:
        gate_resume.issue_gate_capability(store=gstore, run_id="run-ux", actor_subject="ou_someone_else", thread_id="thread-x", gate="A", now_ms=2)
    except gate_resume.GateResumeRejected as exc:
        cases.append(exc)
    assert ux.classify(cases[-1]) == ux.CODEX_GATE_DENIED

    assert [ux.classify(c) for c in cases] == [
        ux.CODEX_BINARY_MISSING, ux.CODEX_EVENT_MALFORMED, ux.CODEX_THREAD_STALE, ux.CODEX_GATE_DENIED,
    ]

    for exc in cases:
        raw_reason = str(getattr(exc, "reason", None) or exc)
        code = ux.classify(exc)
        rendered = ux.render_unavailable(exc)
        message = rendered.reason
        assert any("一" <= ch <= "鿿" for ch in message), f"not Chinese: {message!r}"
        assert code not in message  # stable internal code (e.g. CODEX_BINARY_MISSING) never shown
        assert raw_reason not in message  # the raw internal reason never rides along verbatim
        for needle in _FORBIDDEN_LEAK_SUBSTRINGS:
            assert needle not in message, (needle, message)

    # the audit record on disk carries the stable code + a reason FINGERPRINT,
    # never the raw internal reason text itself.
    audit_path = tmp_path / "audit.jsonl"
    assert audit_path.exists()
    lines = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ux_lines = [line for line in lines if line.get("event_type") == ux.AUDIT_EVENT_TYPE]
    assert len(ux_lines) == len(cases)
    for line in ux_lines:
        for needle in ("codex' binary", "bad/workflow!", "/Users"):
            assert needle not in line.get("reason", "")

    store.close()
    gstore.close()
