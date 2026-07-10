"""Per-message log tracing for the multitenancy plugin (router process only).

Ported (heavily trimmed) from the openclaw-lark reference
``src/core/lark-logger.ts`` — the idea: stamp every log line emitted while
handling one inbound Feishu message with a ``[msg:<id>]`` prefix, so a whole
message's lifecycle can be grepped out of ``agent.log`` and diagnosed with one
command (see :func:`hermes_multitenancy.diagnostics.trace_by_message_id`).

Mechanism note (why a record factory, not a ``Logger``/``Handler`` filter)
--------------------------------------------------------------------------
Every mt module logs through a *child* logger (``logging.getLogger(__name__)``
== ``hermes_multitenancy.<module>``). Filters attached to the
``hermes_multitenancy`` logger are **only** consulted for records logged
*directly* on that logger — CPython never re-applies an ancestor logger's
filters to records propagating up from a descendant (verified empirically).
So a filter on the tree-root logger would miss ~100% of real mt logs. A
filter on a handler works but depends on handler topology/order (and pytest's
``caplog`` adds its own handler), which is brittle.

The robust, topology-independent point is the **log record factory**: it runs
exactly once, at record creation, before any handler/formatter/caplog sees the
record — so every consumer observes the same prefixed message. The wrapper is
name-gated (only ``hermes_multitenancy.*`` records) and context-gated (only
when a trace message_id is set), so records with no active trace context are
byte-for-byte unchanged.

Safety: the factory is process-global, so this code must NEVER raise into the
logging pipeline — ``TraceContextFilter.apply`` swallows everything, only
touches ``str`` messages (leaving stock logging's lazy non-``str``/error
fallback intact), and guards against ``record.name`` being ``None``
(``logging.makeLogRecord`` / socket receivers).

Hard boundary: ``contextvars`` do not cross process boundaries. Each inbound
message's model inference runs in an isolated AIAgent subprocess
(``real_run_agent`` / ``asyncio.create_subprocess_exec``); its internal logs
are NOT covered here by design. This module only stamps router-process mt logs
(card / streaming / credential / routing) — the layers where the historical
incidents this feature targets actually occurred.
"""
from __future__ import annotations

import contextvars
import logging
import re
from typing import Optional, Tuple

# Root of the mt logger tree. All mt modules use ``getLogger(__name__)`` which
# is ``hermes_multitenancy`` or a ``hermes_multitenancy.*`` descendant.
TRACE_LOGGER_ROOT = "hermes_multitenancy"

# Set at the dispatch entry (router.commands.handle_async) and read by the log
# record factory. Task-local: ``loop.create_task`` copies the context at
# creation, so the prefix follows the message through child tasks/awaits, and
# two interleaved messages never bleed into each other (each handle_async task
# owns its own context copy).
_message_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "hermes_mt_trace_message_id", default=None
)
_chat_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "hermes_mt_trace_chat_id", default=None
)

TraceTokens = Tuple[contextvars.Token, contextvars.Token]

# Keep the id token %-safe (the factory prepends into ``record.msg`` which is
# still ``% record.args`` formatted downstream) and bracket-safe (so a hostile
# message body cannot forge a ``[msg:...]`` marker the trace extractor would
# mis-attribute). Illegal chars are REPLACED with ``.`` — not deleted — so that
# ``om_x:auth-complete`` -> ``om_x.auth-complete`` stays distinct from a real
# ``om_xauth-complete`` (deletion collapsed those two into one token and merged
# their traces). Not a perfect injection, but it kills the adjacent-deletion
# collapse class. Feishu message ids are ``om_`` + alnum/underscore/dash.
_ID_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_.\-]")


def _sanitize_id(raw: object) -> str:
    """Reduce an arbitrary id to a %-safe, marker-safe token (may be empty)."""
    return _ID_SANITIZE_RE.sub(".", str(raw or ""))


def trace_prefix(message_id: object) -> str:
    """Return the ``[msg:<id>]`` token for ``message_id`` (``""`` when empty).

    The single source of truth for the prefix format, shared by the log record
    factory (writer) and :func:`diagnostics.trace_by_message_id` (reader) so
    the two can never drift apart.
    """
    ident = _sanitize_id(message_id)
    if not ident:
        return ""
    return f"[msg:{ident}]"


def set_trace_context(message_id: object, chat_id: object) -> TraceTokens:
    """Bind the current task's trace context and return reset tokens.

    Called once at dispatch entry. ``message_id`` drives the log prefix;
    ``chat_id`` is captured for future diagnostics use. A falsy/garbage
    ``message_id`` yields no prefix (logs stay byte-identical) rather than an
    empty ``[msg:]`` marker.

    Returns tokens so the caller can ``reset_trace_context`` in a ``finally``:
    a nested ``await handle_async(...)`` (auth-complete replay) runs in the
    *outer* task's context, so without a reset it would permanently overwrite
    the outer message's binding.
    """
    return (
        _message_id_var.set(_sanitize_id(message_id) or None),
        _chat_id_var.set(str(chat_id or "") or None),
    )


def reset_trace_context(tokens: TraceTokens) -> None:
    """Restore the context to what it was before the matching
    :func:`set_trace_context`. Never raises (defensive — trace teardown must
    not break message handling)."""
    try:
        message_token, chat_token = tokens
        _message_id_var.reset(message_token)
        _chat_id_var.reset(chat_token)
    except Exception:
        pass


def clear_trace_context() -> None:
    """Drop the current context's trace binding (test hygiene; prod tasks are
    isolated so this is not needed on the hot path)."""
    _message_id_var.set(None)
    _chat_id_var.set(None)


def current_message_id() -> Optional[str]:
    """The active trace message id in this context, if any."""
    return _message_id_var.get()


class TraceContextFilter(logging.Filter):
    """Prefixes ``hermes_multitenancy.*`` records with the active ``[msg:<id>]``.

    Implemented as a ``logging.Filter`` (per spec) but the real coverage comes
    from being invoked by the log record factory in
    :func:`install_message_trace_filter` — see the module docstring. ``apply``
    is idempotent per record (guarded by ``_msg_trace_applied``) so it is safe
    even if the same instance is also attached to a handler, and it never
    raises into the logging pipeline.
    """

    def apply(self, record: logging.LogRecord) -> None:
        try:
            if getattr(record, "_msg_trace_applied", False):
                return
            msg = record.msg
            if not isinstance(msg, str):
                # Non-str msg (exception object, lazy proxy, ...). Stock logging
                # defers str() to handler-format time and Handler.handleError
                # catches failures there; stringifying eagerly here would raise
                # straight into the business call site. Dropping the prefix on
                # these records is acceptable; breaking their semantics is not.
                return
            name = record.name
            if not isinstance(name, str):
                # record.name can be None (logging.makeLogRecord / SocketHandler
                # receivers). name.startswith would AttributeError process-wide.
                return
            if name != TRACE_LOGGER_ROOT and not name.startswith(TRACE_LOGGER_ROOT + "."):
                return  # other logger trees are never modified
            prefix = trace_prefix(_message_id_var.get())
            if not prefix:
                return  # no context -> record untouched, byte-identical output
            # Prepend into ``msg`` BEFORE ``% args`` formatting. ``prefix`` is
            # sanitized to contain no ``%`` so it cannot corrupt substitution.
            record.msg = "%s %s" % (prefix, msg)
            record._msg_trace_applied = True
        except Exception:
            # The record factory is global; a raise here would break every log
            # consumer in the process. Tracing is best-effort — never fatal.
            pass

    def filter(self, record: logging.LogRecord) -> bool:
        self.apply(record)
        return True


def install_message_trace_filter() -> None:
    """Idempotently wrap the log record factory to stamp mt records.

    Repeated calls (e.g. ``register()`` running more than once) are a no-op —
    the wrapped factory carries a marker attribute we check for.
    """
    current_factory = logging.getLogRecordFactory()
    if getattr(current_factory, "_hermes_msg_trace", False):
        return

    trace_filter = TraceContextFilter()

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = current_factory(*args, **kwargs)  # type: ignore[arg-type]
        trace_filter.apply(record)
        return record

    factory._hermes_msg_trace = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)
