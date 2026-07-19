from __future__ import annotations

from hermes_multitenancy.agent_real import (
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
