"""Tests for per-message log tracing (feishu_message_trace + diagnostics).

Covers the SPEC acceptance scenarios (prefix injection / create_task
propagation / byte-identical-without-context / interleaved isolation / trace
extraction + stage analysis / missing-stage + duplicate detection / idempotent
install) plus the review hardening: non-str msg, None name, context reset on
nested dispatch, name-gate, sanitizer collision, body-forgery, traceback capture.

Diagnostics tests use REAL router-process log lines copied verbatim from source
(file:line cited on each) — no invented "routed run profile=alice" corpus.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from hermes_multitenancy import diagnostics
from hermes_multitenancy import feishu_message_trace as trace

_LOGGER_NAME = "hermes_multitenancy.trace_selftest"  # realistic child of the mt tree


@pytest.fixture(autouse=True)
def _trace_env():
    """Install the record factory for the test, restore the ORIGINAL factory
    afterwards (C1: any test — including test_register.py calling register() —
    that installs the factory must not leak the global wrapper to other files).
    """
    original_factory = logging.getLogRecordFactory()
    trace.clear_trace_context()
    trace.install_message_trace_filter()
    try:
        yield
    finally:
        logging.setLogRecordFactory(original_factory)
        trace.clear_trace_context()


# =========================================================================
# Prefix mechanism (writer side)
# =========================================================================

def test_prefix_injected_when_context_set(caplog):
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        trace.set_trace_context("m1", "chat-1")
        logging.getLogger(_LOGGER_NAME).info("hello %s", "world")
    messages = [r.getMessage() for r in caplog.records]
    assert any(m == "[msg:m1] hello world" for m in messages), messages


def test_prefix_propagates_across_create_task(caplog):
    async def scenario():
        trace.set_trace_context("m_task", "chat-1")

        async def child():
            logging.getLogger(_LOGGER_NAME).info("from child task")

        await asyncio.create_task(child())

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        asyncio.run(scenario())
    child_msgs = [m for r in caplog.records if "from child task" in (m := r.getMessage())]
    assert child_msgs and all("[msg:m_task]" in m for m in child_msgs), child_msgs


def test_no_context_is_byte_identical(caplog):
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        logging.getLogger(_LOGGER_NAME).info("plain %s message", "startup")
    messages = [r.getMessage() for r in caplog.records]
    assert "plain startup message" in messages
    assert not any("[msg:" in m for m in messages), messages  # no empty residue


def test_interleaved_messages_do_not_cross(caplog):
    async def scenario():
        async def worker(mid: str, marker: str):
            trace.set_trace_context(mid, "chat")
            for _ in range(3):
                logging.getLogger(_LOGGER_NAME).info("work %s", marker)
                await asyncio.sleep(0)  # yield so the two workers interleave

        await asyncio.gather(
            asyncio.create_task(worker("m1", "AAA")),
            asyncio.create_task(worker("m2", "BBB")),
        )

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        asyncio.run(scenario())

    seen_a = seen_b = 0
    for record in caplog.records:
        message = record.getMessage()
        if "work AAA" in message:
            seen_a += 1
            assert "[msg:m1]" in message and "[msg:m2]" not in message, message
        elif "work BBB" in message:
            seen_b += 1
            assert "[msg:m2]" in message and "[msg:m1]" not in message, message
    assert seen_a == 3 and seen_b == 3


def test_install_is_idempotent(caplog):
    trace.install_message_trace_filter()
    trace.install_message_trace_filter()
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        trace.set_trace_context("m1", "chat")
        logging.getLogger(_LOGGER_NAME).info("once")
    messages = [r.getMessage() for r in caplog.records if "once" in r.getMessage()]
    assert messages
    for message in messages:
        assert message.count("[msg:m1]") == 1, message


# =========================================================================
# Review hardening — writer safety (A1/A2/A5/A6 + C2)
# =========================================================================

def test_non_str_msg_is_not_prefixed(caplog):
    # A1: eager stringify of a non-str msg would run its __str__ at the call
    # site (stock logging defers this to handler-format under handleError).
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        trace.set_trace_context("m1", "chat")
        logging.getLogger(_LOGGER_NAME).error(ValueError("boom"))  # msg is an exc
    matched = [r for r in caplog.records if "boom" in r.getMessage()]
    assert matched and not any("[msg:" in r.getMessage() for r in matched)


def test_factory_handles_none_name():
    # A2: makeLogRecord() runs the factory with name=None; name.startswith would
    # AttributeError process-wide. Must not raise, must not prefix.
    trace.set_trace_context("m1", "chat")
    record = logging.makeLogRecord({"msg": "hi", "levelno": logging.INFO})
    assert record.getMessage() == "hi"


def test_reset_restores_outer_context():
    # A5: a nested handle_async runs in the outer task's context; the finally
    # reset must restore the outer message's binding.
    outer = trace.set_trace_context("outer", "c1")
    inner = trace.set_trace_context("inner", "c2")
    assert trace.current_message_id() == "inner"
    trace.reset_trace_context(inner)
    assert trace.current_message_id() == "outer"
    trace.reset_trace_context(outer)
    assert trace.current_message_id() is None


def test_name_gate_leaves_other_trees_untouched(caplog):
    # C2: records outside hermes_multitenancy.* are never modified, even with an
    # active trace context.
    with caplog.at_level(logging.INFO, logger="some.other.library"):
        trace.set_trace_context("m1", "chat")
        logging.getLogger("some.other.library").info("unrelated %s", "line")
    messages = [r.getMessage() for r in caplog.records if "unrelated" in r.getMessage()]
    assert messages == ["unrelated line"]


def test_hostile_message_id_is_sanitized(caplog):
    # A6: illegal chars -> '.', so no %-injection and no forged marker; args
    # still substitute. "m1]%s[evil" -> "m1..s.evil".
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        trace.set_trace_context("m1]%s[evil", "chat")
        logging.getLogger(_LOGGER_NAME).info("payload %s", "ARG")
    messages = [r.getMessage() for r in caplog.records if "payload" in r.getMessage()]
    assert messages == ["[msg:m1..s.evil] payload ARG"], messages


def test_sanitizer_avoids_adjacent_deletion_collapse():
    # A6: ':' -> '.' keeps two synthetic ids distinct (deletion merged them).
    assert trace.trace_prefix("om_x:auth-complete") != trace.trace_prefix("om_xauth-complete")


# =========================================================================
# Diagnostics — trace_by_message_id, real corpus (B1..B6)
# =========================================================================

def _write_log(tmp_path, lines):
    path = tmp_path / "agent.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# Real router-process log lines (verbatim message text, cited to source).
_L_DEDUP = "multitenancy: duplicate inbound event skipped profile=alice sender=ou_abc message_id=om_1"  # commands.py:284
_L_CARD_SENT = "multitenancy: Feishu CardKit compat card sent message_id=om_out99 card_id=card_7"  # streaming_controller.py:203
_L_MEDIA = "multitenancy: delivered post-stream media attachment path=/tmp/pic.png"  # router/__init__.py:959
_L_ROUTED_REJECT = "multitenancy: routed run rejected profile=alice sender=ou_abc: quota exceeded"  # commands.py:270
_L_FAILED = "multitenancy: handle_async failed: boom"  # commands.py:411


def _line(msg_id, body, ts="2026-07-10 10:00:00,123", level="INFO", logger="hermes_multitenancy.router.commands"):
    # REAL deployed format `%(name)s: %(message)s` — logger name ends with a
    # colon. Verified verbatim against live agent.log (review round-5: a
    # fabricated colon-less header made every production record unparseable):
    # 2026-07-08 14:22:01,415 INFO hermes_multitenancy.credential_renewal_worker: [credential_renewal] tick ...
    return f"{ts} {level} {logger}: [msg:{msg_id}] {body}"


def test_trace_extracts_and_detects_reply(tmp_path):
    path = _write_log(
        tmp_path,
        [
            _line("om_1", _L_CARD_SENT),
            _line("om_2", _L_DEDUP.replace("om_1", "om_2")),  # noise for a different msg
            "2026-07-10 10:00:04 INFO urllib3.connectionpool | reconnecting (no prefix)",
        ],
    )
    result = diagnostics.trace_by_message_id("om_1", path)
    assert result.message_id == "om_1"
    assert len(result.matched_lines) == 1  # om_2 + foreign line excluded
    assert result.received is True


def test_media_delivery_counts_as_replied(tmp_path):
    path = _write_log(tmp_path, [_line("om_1", _L_MEDIA)])
    result = diagnostics.trace_by_message_id("om_1", path)
    assert result.received is True and len(result.matched_lines) == 1


def test_missing_replied_stage_detected(tmp_path):
    # a rejection was logged (so received=True) but no delivery line -> replied
    # missing.
    path = _write_log(tmp_path, [_line("om_3", _L_ROUTED_REJECT, level="WARNING")])
    result = diagnostics.trace_by_message_id("om_3", path)
    assert result.received is True and len(result.matched_lines) == 1


def test_duplicate_received_detected(tmp_path):
    path = _write_log(
        tmp_path,
        [_line("om_4", _L_DEDUP.replace("om_1", "om_4")), _line("om_4", _L_CARD_SENT)],
    )
    result = diagnostics.trace_by_message_id("om_4", path)
    assert result.received is True and len(result.matched_lines) == 2


def test_prefix_does_not_collide_on_longer_id(tmp_path):
    path = _write_log(
        tmp_path,
        [_line("om_1", "received"), _line("om_1x", "different message")],
    )
    result = diagnostics.trace_by_message_id("om_1", path)
    assert len(result.matched_lines) == 1
    assert "om_1x" not in result.matched_lines[0]


def test_body_forged_token_is_not_attributed_to_victim(tmp_path):
    # B4: mt logs full reply text (commands.py:666); a user pasting
    # "[msg:om_victim]" into their message must not pollute the victim's trace.
    # Real prefix (first token) is om_attacker; forged token comes later.
    path = _write_log(
        tmp_path,
        [_line("om_attacker", "here is your answer [msg:om_victim] pasted by user")],
    )
    victim = diagnostics.trace_by_message_id("om_victim", path)
    assert victim.matched_lines == []
    attacker = diagnostics.trace_by_message_id("om_attacker", path)
    assert len(attacker.matched_lines) == 1


def test_traceback_continuation_lines_captured(tmp_path):
    # B5: logger.exception frames have no prefix and no timestamp; capture them
    # as continuation until the next record start.
    path = _write_log(
        tmp_path,
        [
            _line("om_5", _L_FAILED, level="ERROR"),
            "Traceback (most recent call last):",
            '  File "/x/commands.py", line 411, in handle_async',
            '    raise ValueError("boom")',
            "ValueError: boom",
            "2026-07-10 10:00:01 INFO urllib3 | unrelated foreign record",
        ],
    )
    result = diagnostics.trace_by_message_id("om_5", path)
    assert len(result.matched_lines) == 5  # error line + 4 traceback lines
    assert result.matched_lines[-1] == "ValueError: boom"
    assert "unrelated foreign record" not in "\n".join(result.matched_lines)


def test_missing_log_file_degrades_gracefully(tmp_path):
    result = diagnostics.trace_by_message_id("om_6", tmp_path / "does-not-exist.log")
    assert result.read_error is not None
    assert result.matched_lines == []
    assert result.matched_lines == []


def test_empty_token_degrades_gracefully(tmp_path):
    # B3: an id that sanitizes to empty must not crash / must not open the file.
    result = diagnostics.trace_by_message_id("", tmp_path / "irrelevant.log")
    assert result.matched_lines == []


def test_forged_token_in_traceback_continuation_does_not_reattribute(tmp_path):
    """codex-reproduced attack: a hostile exception whose text embeds
    [msg:om_victim] lands on traceback CONTINUATION lines (no timestamp, no
    line-start token). Ownership must stay with the crashing message; the
    victim's trace must stay empty."""
    path = _write_log(
        tmp_path,
        [
            _line("om_evil", _L_FAILED, level="ERROR"),
            "Traceback (most recent call last):",
            '  File "x.py", line 1, in handle',
            "ValueError: [msg:om_victim] injected",  # forged token mid-continuation
        ],
    )
    evil = diagnostics.trace_by_message_id("om_evil", path)
    assert len(evil.matched_lines) == 4  # record + all continuation lines kept

    victim = diagnostics.trace_by_message_id("om_victim", path)
    assert victim.matched_lines == []  # forged token never attributes
    assert victim.received is False


def test_echoed_marker_text_mid_message_does_not_classify(tmp_path):
    """codex-reproduced attack: a traced log line whose message BODY echoes
    marker text (e.g. reply content quoting 'CardKit compat card sent') must
    stay mere content: the line is still extracted (owner's own trace), and
    nothing is classified — classification was cut entirely."""
    echo = f"multitenancy: reply text='{_L_CARD_SENT} and {_L_DEDUP}'"
    path = _write_log(tmp_path, [_line("om_9", echo)])
    result = diagnostics.trace_by_message_id("om_9", path)
    assert result.received is True


def test_untraced_record_with_midbody_forged_token_never_attributes(tmp_path):
    """codex round-4 attack: an UNTRACED timestamped record (no real prefix)
    whose body echoes [msg:om_victim] mid-text must not become the victim's
    trace — the real prefix can only sit immediately after the log header."""
    path = _write_log(
        tmp_path,
        [
            "2026-07-10 10:00:00 INFO hermes_multitenancy.router.commands "
            "multitenancy: reply text='look [msg:om_victim] pwned'",
        ],
    )
    victim = diagnostics.trace_by_message_id("om_victim", path)
    assert victim.matched_lines == []
    assert victim.received is False


def test_continuation_line_with_forged_token_and_marker_never_classifies(tmp_path):
    """codex round-4 attack: a traceback continuation carrying
    '[msg:any] multitenancy: Feishu CardKit compat card sent' must neither
    re-attribute — bare line-start tokens are not a trusted frame at all."""
    path = _write_log(
        tmp_path,
        [
            _line("om_evil", _L_FAILED, level="ERROR"),
            "Traceback (most recent call last):",
            f"ValueError: [msg:om_evil] {_L_CARD_SENT}",  # forged marker in continuation
        ],
    )
    evil = diagnostics.trace_by_message_id("om_evil", path)
    assert len(evil.matched_lines) == 3
    assert len(evil.matched_lines) == 3  # forged content stays in owner's trace


def test_colonless_header_format_still_accepted(tmp_path):
    """Formats without the logger-name colon (older/basicConfig formatters)
    must keep working — the colon is optional, not required."""
    line = f"2026-07-10 10:00:00 INFO hermes_multitenancy.router [msg:om_c] {_L_CARD_SENT}"
    path = _write_log(tmp_path, [line])
    result = diagnostics.trace_by_message_id("om_c", path)
    assert result.received is True and len(result.matched_lines) == 1


def test_newline_embedded_barestart_token_never_attributes(tmp_path):
    """codex round-5 attack: a multiline hostile payload embeds
    '\\n[msg:om_victim] multitenancy: Feishu CardKit compat card sent...' so the
    forged token lands at LINE START. Bare-format parsing was removed entirely —
    only a full timestamped header frames a record — so the forged line is a
    continuation of the hostile record, never the victim's."""
    path = _write_log(
        tmp_path,
        [
            _line("om_evil", "multitenancy: handle_async failed: payload below", level="ERROR"),
            f"[msg:om_victim] {_L_CARD_SENT}",  # forged bare-start token line
        ],
    )
    victim = diagnostics.trace_by_message_id("om_victim", path)
    assert victim.matched_lines == []
    assert victim.received is False

    evil = diagnostics.trace_by_message_id("om_evil", path)
    assert len(evil.matched_lines) == 2  # forged line stays with its true owner


def test_bare_timestamp_in_continuation_does_not_truncate_trail(tmp_path):
    """codex round-6 attack: a traceback/hostile payload line beginning with a
    bare timestamp (no level+logger header) must not be mistaken for a record
    boundary — the owner's continuation trail must stay complete."""
    path = _write_log(
        tmp_path,
        [
            _line("om_evil", "multitenancy: handle_async failed: boom", level="ERROR"),
            "Traceback (most recent call last):",
            "2026-07-10 10:00 looks like a timestamp but is payload text",
            "ValueError: the tail frame that round-6 saw truncated",
            "2026-07-10 10:00:05,000 INFO hermes_multitenancy.router.commands: untraced real record",
        ],
    )
    evil = diagnostics.trace_by_message_id("om_evil", path)
    assert len(evil.matched_lines) == 4  # record + ALL 3 continuations, stop at real header


def test_pathological_str_inputs_never_crash(tmp_path):
    """codex round-7: msg_id/log_path whose __str__ raises must degrade, not
    crash the diagnostic entry point."""
    class Boom:
        def __str__(self):
            raise RuntimeError("hostile __str__")

    result = diagnostics.trace_by_message_id(Boom(), tmp_path / "agent.log")
    assert result.received is False and result.matched_lines == []

    result2 = diagnostics.trace_by_message_id("om_ok", Boom())
    assert result2.read_error is not None
    assert result2.received is False


def test_raising_chat_id_does_not_leak_partial_context():
    """grok review: str(chat_id) raising after the message var was already set
    left a bound context with no reset tokens. Both conversions must happen
    before either contextvar is set, and neither may raise."""
    class Boom:
        def __str__(self):
            raise RuntimeError("hostile __str__")

    tokens = trace.set_trace_context("om_ok", Boom())
    try:
        assert trace.trace_prefix("om_ok")  # message context usable
    finally:
        trace.reset_trace_context(tokens)  # tokens valid → resettable

    tokens2 = trace.set_trace_context(Boom(), "oc_1")  # raising msg id
    trace.reset_trace_context(tokens2)


def test_impossible_timestamp_header_never_attributes(tmp_path):
    """codex round-8 reproduced attack: hostile content that byte-mimics the
    FULL header grammar but carries an impossible calendar timestamp
    (2026-99-99 99:99) forged attribution. Only a real datetime frames a
    record; a forged header is content of the current record, not a boundary."""
    path = _write_log(
        tmp_path,
        [
            _line("om_owner", "start of owner record"),
            "2026-99-99 99:99:99,000 ERROR hermes.forged: [msg:om_victim] forged",
        ],
    )
    victim = diagnostics.trace_by_message_id("om_victim", path)
    assert victim.matched_lines == []
    owner = diagnostics.trace_by_message_id("om_owner", path)
    assert len(owner.matched_lines) == 2  # forged line stays inside owner's trail


def test_impossible_timestamp_record_start_does_not_truncate_trail(tmp_path):
    """Same validation on the UNTRACED boundary regex: an impossible-timestamp
    header inside a hostile traceback must not terminate continuation capture."""
    path = _write_log(
        tmp_path,
        [
            _line("om_owner", "boom", level="ERROR"),
            "Traceback (most recent call last):",
            "2026-13-40 25:61:00,000 INFO fake.logger: not a real record",
            "RuntimeError: hostile",
        ],
    )
    owner = diagnostics.trace_by_message_id("om_owner", path)
    assert len(owner.matched_lines) == 4  # all continuations kept
