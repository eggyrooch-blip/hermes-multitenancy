# Run terminal audit

Run Broker appends one content-free `run_terminal` JSONL row for each admitted
execution. It uses the existing conversation-audit switch and path:

- `HERMES_CONVERSATION_AUDIT_ENABLED`
- `HERMES_CONVERSATION_AUDIT_PATH`

The writer is best-effort and never changes an already-authorized run result.
Expert resolution is separate: an explicit expert must resolve for the trusted
principal, including every declared skill directory, before model or tool
execution starts.

Schema version 1 follows the shared terminal/error contract:

- `terminal_status`: `completed`, `failed`, `cancelled`, or `rejected`
- `failure_subsystem`: `expert_resolution`, `credential`, `identity`,
  `permission`, `lark_api`, `transport`, `tool`, `model`, `runtime`, or `output`
- `error_code`: only a registered shared-contract code
- `retryable`, `retried`, and `answer_completed`
- `expert_requested`, managed `expert_id`, and `expert_resolution`
- content-free execution metadata: terminal id, redacted profile, channel,
  chat type, source, timestamp, and duration

`answer_completed` means the broker execution generated the complete answer. It
does not mean a WebUI or Feishu client received it. Analytics deduplicates by
`terminal_event_id`; legacy `conversation_message` turn reconstruction remains
unchanged and reports terminal metrics as unavailable when no version-1 rows
exist.
