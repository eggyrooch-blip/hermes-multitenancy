# H04/H08 durable Lark operation resume

Local candidate only; not shipped or deployed.

- Lark send/reply is confirmed only after an actor-bound raw `messages/mget` readback matches the message id, target, type, and non-reversible content fingerprint.
- `im +messages-resources-download` follows the connector's `Risk: write` contract and fails closed before execution because no typed resumable descriptor exists.
- The complete strict-read shortcut set is checked against the installed connector's declared risk; `mail +watch` remains an exact read instead of being falsely denied.
- The real Agent host must forward `tool_call_id` to every `registry.dispatch` call. The MT seam test is a hard gate against the H06 Agent candidate, so Agent must land before MT.
- Authorization completion never replays a stored request; it returns a fixed resend instruction, and host-only operation metadata is stripped with a fail-closed fallback.
