from __future__ import annotations

from hermes_multitenancy.agent_real import (
    _ProtocolPlaceholderStreamSanitizer,
    _finalize_aiagent_result,
    _strip_empty_message_protocol_placeholder,
)


PLACEHOLDER = "[System: Empty message content sanitised to satisfy protocol]"


def test_empty_message_protocol_placeholder_is_not_user_visible():
    assert _strip_empty_message_protocol_placeholder(PLACEHOLDER) == ""
    assert _strip_empty_message_protocol_placeholder(
        f"before {PLACEHOLDER} after"
    ) == "before  after"
    assert _finalize_aiagent_result({"final_response": PLACEHOLDER}) == ""


def test_empty_message_protocol_placeholder_is_removed_across_stream_chunks():
    sanitizer = _ProtocolPlaceholderStreamSanitizer()
    split = len(PLACEHOLDER) // 2

    assert sanitizer.feed(PLACEHOLDER[:split]) == ""
    assert sanitizer.feed(PLACEHOLDER[split:]) == ""
    assert sanitizer.finish() == ""


def test_incomplete_placeholder_prefix_is_preserved_when_stream_finishes():
    sanitizer = _ProtocolPlaceholderStreamSanitizer()

    assert sanitizer.feed("normal [System:") == "normal "
    assert sanitizer.finish() == "[System:"
