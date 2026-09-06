"""Trusted, local-only WebUI admission for the Codex Harness runtime."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from ..trusted_runtime_principal import TrustedRuntimePrincipal
from .codex_session_bridge import (
    CodexSessionBridgeStore,
    CodexThreadPlan,
    plan_codex_thread,
    record_codex_thread,
)

logger = logging.getLogger(__name__)

ENABLED_ENV = "HERMES_WEBUI_HARNESS_ENABLED"
CODEX_BIN_ENV = "HERMES_WEBUI_HARNESS_CODEX_BIN"
CODEX_VERSION_ENV = "HERMES_WEBUI_HARNESS_CODEX_VERSION"
SOURCE_REV_ENV = "HERMES_WEBUI_HARNESS_SOURCE_REV"
READY_FILE_ENV = "HERMES_WEBUI_HARNESS_READY_FILE"
_PLATFORM = sys.platform
ENGINE = "harness"
FLOWS = ("server-dev", "server-dev-light", "server-bugfix")

_OPAQUE = re.compile(r"[A-Za-z0-9_.:-]{1,256}")


def _initialize_timeout_floor() -> float:
    """Floor for the codex app-server `initialize` handshake, in seconds.

    The core session calls `initialize` with its 10s default. On the production
    host (shared with CI runners) a cold bwrap + codex start under load crossed
    that (2026-09-02 19:43, load 12): the run surfaced as "Harness is
    unavailable" while the same session's retry seconds later succeeded.
    """
    try:
        return max(10.0, float(os.environ.get("HERMES_CODEX_INITIALIZE_TIMEOUT", "60")))
    except ValueError:
        return 60.0
_PROOF = object()

_HARNESS_READ_COMMANDS = frozenset({"rg", "head", "tail", "ls", "pwd", "wc"})


class HarnessAdmissionRejected(ValueError):
    pass


def harness_approval_command_allowed(command: Any) -> bool:
    """Keep Codex shell access local, reviewable and below operation gates."""
    text = command if isinstance(command, str) else " ".join(map(str, command or []))
    text = " ".join(text.split())
    if text.startswith("apply_patch"):
        return True
    try:
        argv = shlex.split(text)
    except ValueError:
        return False
    if not argv or any(part.startswith("/") or part == ".." or "../" in part for part in argv):
        return False
    if any(any(char in part for char in ";|&`$<>") for part in argv):
        return False
    if argv[:2] == ["git", "status"]:
        return True
    if argv[0] == "git":
        return len(argv) > 1 and argv[1] == "rev-parse"
    if argv[0] in _HARNESS_READ_COMMANDS:
        return True
    if argv[0] == "pytest":
        return True
    if argv[0] in {"bun", "npm", "pnpm"}:
        return "test" in argv[1:3]
    return False


class TrustedHarnessAdmission:
    __slots__ = (
        "profile_name",
        "actor_subject",
        "session_id",
        "workspace",
        "codex_bin",
        "workflow_id",
        "flow",
        "_proof",
    )

    def __init__(
        self,
        *,
        profile_name: str,
        actor_subject: str,
        session_id: str,
        workspace: str | None,
        codex_bin: Path,
        workflow_id: str,
        flow: str,
        _proof: object,
    ) -> None:
        if _proof is not _PROOF:
            raise TypeError("TrustedHarnessAdmission values are server-issued")
        self.profile_name = profile_name
        self.actor_subject = actor_subject
        self.session_id = session_id
        self.workspace = workspace
        self.codex_bin = codex_bin
        self.workflow_id = workflow_id
        self.flow = flow
        self._proof = _proof

    def is_authentic(self) -> bool:
        return self._proof is _PROOF


def is_harness_enabled(environ: Mapping[str, str]) -> bool:
    return str(environ.get(ENABLED_ENV) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def is_harness_profile_enabled(environ: Mapping[str, str], profile_name: str) -> bool:
    return is_harness_enabled(environ) and bool(str(profile_name or "").strip())


def is_harness_runtime_ready(environ: Mapping[str, str]) -> bool:
    revision = str(environ.get(SOURCE_REV_ENV) or "").strip().lower()
    configured = str(environ.get(READY_FILE_ENV) or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or not configured:
        return False
    try:
        return Path(configured).expanduser().read_text(encoding="utf-8").strip().lower() == revision
    except OSError:
        return False


def _opaque(name: str, value: Any) -> str:
    text = str(value or "").strip()
    if not _OPAQUE.fullmatch(text):
        raise HarnessAdmissionRejected(f"{name}_invalid")
    return text


def _require_production_readiness(
    profile_name: str,
    env: Mapping[str, str],
) -> Path:
    revision = str(env.get(SOURCE_REV_ENV) or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise HarnessAdmissionRejected("source_revision_unconfigured")
    ready_file = Path(str(env.get(READY_FILE_ENV) or "")).expanduser()
    try:
        if ready_file.read_text(encoding="utf-8").strip().lower() != revision:
            raise HarnessAdmissionRejected("readiness_mismatch")
    except OSError as exc:
        raise HarnessAdmissionRejected("readiness_unavailable") from exc

    configured_bin = str(env.get(CODEX_BIN_ENV) or "").strip()
    expected_version = str(env.get(CODEX_VERSION_ENV) or "").strip()
    if not configured_bin or not expected_version:
        raise HarnessAdmissionRejected("codex_unconfigured")
    try:
        codex_bin = Path(configured_bin).expanduser().resolve(strict=True)
        result = subprocess.run(
            [str(codex_bin), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HarnessAdmissionRejected("codex_unavailable") from exc
    if not codex_bin.is_file() or not os.access(codex_bin, os.X_OK):
        raise HarnessAdmissionRejected("codex_unavailable")
    if result.stdout.strip().split()[-1:] != [expected_version]:
        raise HarnessAdmissionRejected("codex_version_mismatch")
    if _PLATFORM != "linux":
        raise HarnessAdmissionRejected("sandbox_unavailable")
    try:
        subprocess.run(
            [str(codex_bin), "sandbox", "--", "/bin/true"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HarnessAdmissionRejected("sandbox_unavailable") from exc
    return codex_bin


def issue_webui_harness_admission(
    *,
    profile_name: str,
    actor_subject: str,
    session_id: str,
    engine: str,
    workspace: str | None = None,
    flow: str = "server-dev",
    environ: Mapping[str, str] | None = None,
) -> TrustedHarnessAdmission:
    """Issue from authenticated server state; the caller never supplies a path."""
    env = os.environ if environ is None else environ
    if str(engine or "").strip().lower() != ENGINE:
        raise HarnessAdmissionRejected("engine_invalid")
    if not is_harness_enabled(env):
        raise HarnessAdmissionRejected("harness_disabled")
    profile_name = _opaque("profile_name", profile_name)
    actor_subject = _opaque("actor_subject", actor_subject)
    session_id = _opaque("session_id", session_id)
    workspace = str(workspace or "").strip() or None
    if workspace is not None and (
        workspace.startswith("/")
        or "\\" in workspace
        or any(part in {"", ".", ".."} for part in workspace.split("/"))
    ):
        raise HarnessAdmissionRejected("workspace_invalid")
    flow = str(flow or "").strip()
    if flow not in FLOWS:
        raise HarnessAdmissionRejected("flow_invalid")

    codex_bin = _require_production_readiness(profile_name, env)

    workflow_id = workflow_id_for(profile_name, actor_subject, session_id)
    return TrustedHarnessAdmission(
        profile_name=profile_name,
        actor_subject=actor_subject,
        session_id=session_id,
        workspace=workspace,
        codex_bin=codex_bin,
        workflow_id=workflow_id,
        flow=flow,
        _proof=_PROOF,
    )


def harness_flow_for_content(content: Any) -> str:
    command = str(content or "").lstrip().split(maxsplit=1)[0].lower()
    selected = command.removeprefix("/")
    return selected if selected in FLOWS else "server-dev"


def workflow_id_for(profile_name: str, actor_subject: str, session_id: str) -> str:
    profile = _opaque("profile_name", profile_name)
    actor = _opaque("actor_subject", actor_subject)
    session = _opaque("session_id", session_id)
    digest = hashlib.sha256(f"webui\0{profile}\0{actor}\0{session}".encode()).hexdigest()[:32]
    return f"webui-harness-{digest}"


def require_event_admission(event: Any, profile_home: Path) -> TrustedHarnessAdmission | None:
    admission = getattr(event, "trusted_harness_admission", None)
    if admission is None:
        return None
    principal = getattr(event, "trusted_runtime_principal", None)
    raw_event = getattr(event, "raw_event", None)
    event_session = str(raw_event.get("session_id") or "").strip() if isinstance(raw_event, dict) else ""
    event_workspace = str(raw_event.get("workspace") or "").strip() or None if isinstance(raw_event, dict) else None
    if (
        not isinstance(admission, TrustedHarnessAdmission)
        or not admission.is_authentic()
        or not isinstance(principal, TrustedRuntimePrincipal)
        or not principal.is_authentic()
        or principal.channel != "webui"
        or principal.profile_name != admission.profile_name
        or principal.actor_subject != admission.actor_subject
        or principal.credential_subject != admission.actor_subject
        or Path(profile_home).name != admission.profile_name
        or event_session != admission.session_id
        or event_workspace != admission.workspace
    ):
        raise HarnessAdmissionRejected("principal_mismatch")
    return admission


def resolve_event_flow(event: Any, profile_home: Path) -> str:
    admission = require_event_admission(event, profile_home)
    if admission is None:
        raise HarnessAdmissionRejected("admission_missing")
    from .harness_workflow import HarnessWorkflowRejected, HarnessWorkflowStore

    store = HarnessWorkflowStore(Path(profile_home) / "harness-runtime.db")
    try:
        try:
            return str(
                store.snapshot(event.trusted_runtime_principal, admission.workflow_id)[
                    "flow"
                ]
            )
        except HarnessWorkflowRejected as exc:
            if str(exc) != "workflow_missing":
                raise
            return admission.flow
    finally:
        store.close()


def plan_event_thread(event: Any, profile_home: Path) -> tuple[CodexSessionBridgeStore, CodexThreadPlan]:
    admission = require_event_admission(event, profile_home)
    if admission is None:
        raise HarnessAdmissionRejected("admission_missing")
    store = CodexSessionBridgeStore(Path(profile_home) / "harness-runtime.db")
    plan = plan_codex_thread(
        store=store,
        principal=event.trusted_runtime_principal,
        profile_name=admission.profile_name,
        executor="codex_app_server",
        workflow_id=admission.workflow_id,
        now_ms=int(time.time() * 1000),
    )
    return store, plan


def record_event_thread(
    store: CodexSessionBridgeStore,
    plan: CodexThreadPlan,
    thread_id: str,
) -> None:
    if plan.resume_thread_id is None:
        record_codex_thread(
            plan,
            store=store,
            thread_id=thread_id,
            now_ms=int(time.time() * 1000),
        )


def codex_thread_id_for_agent(agent: Any) -> str:
    """Read the thread receipt before AIAgent.close() clears its session."""
    session = getattr(agent, "_codex_session", None)
    thread_id = str(getattr(session, "_thread_id", "") or "").strip()
    return thread_id if _OPAQUE.fullmatch(thread_id) else ""


@contextmanager
def codex_thread_resume_scope(
    thread_id: str | None,
    *,
    agent: Any,
    on_thread_bound=None,
    client_class=None,
    session_class=None,
    routing_class=None,
    event_bridge=None,
) -> Iterator[None]:
    """Install one run-owned Codex session whose first thread call resumes."""
    thread_id = _opaque("thread_id", thread_id) if thread_id else None
    if getattr(agent, "_codex_session", None) is not None:
        raise HarnessAdmissionRejected("resume_session_already_started")
    if client_class is None:
        try:
            from agent.transports.codex_app_server import CodexAppServerClient
        except Exception as exc:
            raise HarnessAdmissionRejected("codex_runtime_unavailable") from exc

        client_class = CodexAppServerClient

    class ResumingClient(client_class):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._harness_resume_used = False

        def request(self, method, params=None, timeout=30.0):
            if method == "initialize":
                timeout = max(float(timeout or 0.0), _initialize_timeout_floor())
            if not thread_id or method != "thread/start" or self._harness_resume_used:
                result = super().request(method, params, timeout)
                if method == "thread/start" and on_thread_bound:
                    result_thread = (
                        (result.get("thread") or {}).get("id")
                        if isinstance(result, dict)
                        and isinstance(result.get("thread"), dict)
                        else None
                    ) or (result.get("sessionId") if isinstance(result, dict) else None) \
                        or (result.get("threadId") if isinstance(result, dict) else None)
                    if result_thread:
                        on_thread_bound(str(result_thread))
                return result
            self._harness_resume_used = True
            start_params = dict(params or {})
            resume_params = {"threadId": thread_id}
            if start_params.get("cwd"):
                resume_params["cwd"] = start_params["cwd"]
            result = super().request("thread/resume", resume_params, timeout)
            if not isinstance(result, dict):
                raise HarnessAdmissionRejected("resume_result_invalid")
            result_thread = (
                (result.get("thread") or {}).get("id")
                if isinstance(result.get("thread"), dict)
                else None
            ) or result.get("sessionId") or result.get("threadId")
            if result_thread != thread_id:
                raise HarnessAdmissionRejected("resume_result_invalid")
            if on_thread_bound:
                on_thread_bound(thread_id)
            return result

    if session_class is None or routing_class is None:
        try:
            from agent.transports.codex_app_server_session import (
                CodexAppServerSession,
                _ServerRequestRouting,
            )
        except Exception as exc:
            raise HarnessAdmissionRejected("codex_runtime_unavailable") from exc

        session_class = session_class or CodexAppServerSession
        routing_class = routing_class or _ServerRequestRouting
    if event_bridge is None:
        try:
            from agent.codex_runtime import make_codex_app_server_event_bridge
        except Exception as exc:
            raise HarnessAdmissionRejected("codex_runtime_unavailable") from exc

        event_bridge = make_codex_app_server_event_bridge(agent)
    try:
        from tools.terminal_tool import _get_approval_callback

        approval_callback = _get_approval_callback()
    except Exception as exc:
        raise HarnessAdmissionRejected("approval_callback_unavailable") from exc
    cwd = str(getattr(agent, "session_cwd", "") or os.getcwd())
    session = session_class(
        cwd=cwd,
        codex_bin=os.environ.get("HERMES_CODEX_BIN", "codex"),
        # core's host-tools gate (HERMES_CODEX_HOST_TOOLS) refuses a session
        # without an explicit codex_home; _codex_runtime_env materialized this
        # run's CODEX_HOME and exported it, so hand it over rather than relying
        # on env inheritance (prod 2026-09-02 "need a managed CODEX_HOME").
        codex_home=os.environ.get("CODEX_HOME") or None,
        approval_callback=approval_callback,
        request_routing=routing_class(),
        on_event=event_bridge,
        client_factory=ResumingClient,
    )
    agent._codex_session = session
    try:
        yield
    finally:
        try:
            session.close()
        except Exception:
            logger.warning("Harness Codex session cleanup failed", exc_info=True)
        finally:
            if agent._codex_session is session:
                agent._codex_session = None
