"""Subprocess entry point for running AIAgent in isolation.

Avoids the gateway-async-loop ↔ AIAgent-sync deadlock that occurs when
``_run_with_aiagent`` is called via ``asyncio.to_thread`` from the gateway's
event loop. Hermes' ``agent.run_conversation`` internally uses sync HTTP /
nested loops that conflict with the parent async context.

The fix: shell out to a fresh Python process with no parent event loop.
Cost: ~0.5-1s extra startup per message.

I/O contract:
  stdin:  JSON {"event": {...}, "profile_home": "/path/to/profile", "messages": [...]}
  stdout: JSON {"result": "...", "error": null}  on success
          JSON {"result": "", "error": "...", "error_code": "...",
                "failure_subsystem": "...", "retryable": false} on typed failure
  exit:   0 always (errors are reported via JSON)

The event dict must contain at minimum: text, message_id, source.* fields
(open_id, user_id, user_name, chat_id, chat_name, chat_type, platform).
"""

import inspect
import json
import os
import sys
import threading
import traceback
import importlib.util
from contextlib import contextmanager
from pathlib import Path


class _ReplayedSource:
    """Reconstruct event.source from a flat dict."""
    def __init__(self, d: dict):
        for k, v in (d or {}).items():
            setattr(self, k, v)


class _ReplayedEvent:
    """Reconstruct a MessageEvent-shaped object from a flat dict."""
    def __init__(self, d: dict):
        self.text = d.get("text", "")
        self.message_id = d.get("message_id", "")
        self.sender_open_id = d.get("sender_open_id", "")
        self.source = _ReplayedSource(d.get("source") or {})
        self.raw_event = d.get("raw_event") if isinstance(d.get("raw_event"), dict) else {}
        broker_role_override = d.get("broker_role_override")
        self.broker_role_override = broker_role_override if isinstance(broker_role_override, dict) else {}


def _load_run_with_aiagent():
    """Load sibling agent_real regardless of package name used by Hermes."""
    package_root = Path(__file__).resolve().parents[1]
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
    try:
        from hermes_multitenancy.agent_real import _run_with_aiagent
        return _run_with_aiagent
    except ModuleNotFoundError as exc:
        if exc.name != "hermes_multitenancy":
            raise

    agent_real_path = Path(__file__).with_name("agent_real.py")
    spec = importlib.util.spec_from_file_location(
        "_hermes_multitenancy_agent_real",
        agent_real_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load agent_real from {agent_real_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module._run_with_aiagent


@contextmanager
def _temporary_environ(env: dict[str, str]):
    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update({str(key): str(value) for key, value in env.items()})
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def _run_payload(
    payload: dict,
    _run_with_aiagent,
    protocol_stdout,
    *,
    force_newline: bool = False,
) -> None:
    event = _ReplayedEvent(payload["event"])
    profile_home = Path(payload["profile_home"])
    messages = payload.get("messages")
    if not isinstance(messages, list):
        messages = None

    event_stream = os.getenv("HERMES_AIAGENT_EVENT_STREAM") == "1"
    parameters = inspect.signature(_run_with_aiagent).parameters
    supports_event_sink = "event_sink" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    billing_retry_safe = supports_event_sink

    # The tool heartbeat thread (agent_real/tool_heartbeat.py) writes lines
    # concurrently with the main thread; one lock keeps every line whole.
    emit_lock = threading.Lock()

    def emit(event: str, **payload) -> None:
        line = json.dumps({"event": event, **payload}, ensure_ascii=False) + "\n"
        with emit_lock:
            protocol_stdout.write(line)
            protocol_stdout.flush()

    def track(event_name: str, **event_payload) -> None:
        nonlocal billing_retry_safe
        if event_name in {"content", "tool_started", "tool_completed"}:
            billing_retry_safe = False
        if event_stream:
            emit(event_name, **event_payload)

    def typed_failure_fields(exc: Exception) -> dict:
        code = str(getattr(exc, "error_code", "") or "").strip()
        subsystem = str(getattr(exc, "failure_subsystem", "") or "").strip()
        if not code or not subsystem:
            return {}
        fields = {
            "error_code": code,
            "failure_subsystem": subsystem,
        }
        retryable = getattr(exc, "retryable", None)
        if isinstance(retryable, bool):
            fields["retryable"] = retryable
        return fields

    previous_cwd = Path.cwd()
    try:
        sys.stdout = sys.stderr
        selected_cwd = os.environ.get("TERMINAL_CWD") or os.environ.get("WORKSPACE")
        run_cwd = Path(selected_cwd) if selected_cwd else profile_home / "workspace"
        if not selected_cwd:
            run_cwd.mkdir(parents=True, exist_ok=True)
        os.chdir(run_cwd)
        usage: dict = {}
        run_kwargs = {"usage_sink": usage}
        if supports_event_sink:
            run_kwargs["event_sink"] = track
        # Previous turns' tool transcript, already rendered + sanitized by the
        # parent. Arrives on the stdin pipe only; neither side writes it to disk.
        carried_context = payload.get("turn_tool_context")
        if isinstance(carried_context, dict):
            run_kwargs["turn_tool_context"] = str(carried_context.get("text") or "")
            run_kwargs["turn_tool_attempt_id"] = str(
                carried_context.get("attempt_id") or ""
            )
        if messages is None:
            result = _run_with_aiagent(
                event,
                profile_home,
                **run_kwargs,
            )
        else:
            result = _run_with_aiagent(
                event,
                profile_home,
                messages=messages,
                **run_kwargs,
            )
        if event_stream:
            out = {"event": "done", "result": result or "", "error": None}
        else:
            out = {"result": result or "", "error": None}
        if usage:
            out["usage"] = usage
    except Exception as exc:
        if event_stream:
            out = {
                "event": "done",
                "result": "",
                "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
                "billing_retry_safe": billing_retry_safe,
                **typed_failure_fields(exc),
            }
        else:
            out = {
                "result": "",
                "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
                "billing_retry_safe": billing_retry_safe,
                **typed_failure_fields(exc),
            }
    finally:
        os.chdir(previous_cwd)
        sys.stdout = protocol_stdout

    protocol_stdout.write(json.dumps(out, ensure_ascii=False))
    if event_stream or force_newline:
        protocol_stdout.write("\n")
    protocol_stdout.flush()


def main() -> None:
    payload = json.loads(sys.stdin.read())

    # Lazy import so import errors are reported as JSON, not crash
    _run_with_aiagent = _load_run_with_aiagent()

    protocol_stdout = sys.stdout
    _run_payload(payload, _run_with_aiagent, protocol_stdout)


def worker_main() -> None:
    protocol_stdout = sys.stdout
    _run_with_aiagent = _load_run_with_aiagent()
    protocol_stdout.write(json.dumps({"event": "ready"}, ensure_ascii=False) + "\n")
    protocol_stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            if request.get("type") == "shutdown":
                break
            if request.get("type") != "run":
                raise ValueError("unknown worker request type")
            payload = request.get("payload")
            env = request.get("env")
            if not isinstance(payload, dict):
                raise ValueError("worker request missing payload")
            if not isinstance(env, dict):
                raise ValueError("worker request missing env")
            with _temporary_environ(env):
                _run_payload(payload, _run_with_aiagent, protocol_stdout, force_newline=True)
        except Exception as exc:
            protocol_stdout.write(
                json.dumps(
                    {
                        "event": "done",
                        "result": "",
                        "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            protocol_stdout.flush()


if __name__ == "__main__":
    if sys.argv[1:] == ["--worker"]:
        worker_main()
    else:
        main()
