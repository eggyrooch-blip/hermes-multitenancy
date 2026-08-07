# Feishu todo progress card — 2026-08-08

Status: local candidate; production unchanged.

The first candidate rendered each todo write twice: once into the reply body via
`update_streaming_card_status`, then again into `tool_calls`. It also appended
every todo write as a new completed tool row. A three-step Hermes-PRE probe ended
with four Todo rows and visibly replaced the body during execution.

The revised candidate uses the existing OpenClaw-style tool path only. Repeated
todo writes update one live row in `_TOOLS_ELEMENT_ID`; the reply body remains
reasoning/answer content, the final rich panel retains one progress row, and the
existing write lock, recovery, and fallback paths remain authoritative. Malformed
or ambiguous tool-scope evidence fails closed.

Verification: final focused regression is 283 passed. In the correct Lark desktop app,
a slow three-step Hermes-PRE probe showed one stable row changing
`0/3 → 1/3 → 2/3 → 3/3`; the final rich panel contained one Todo entry. The two
post-fix runs completed successfully, emitted no todo body-status log, and had no
isolation error. PRE gateway/WebUI health passed; profile apiserver 8655 was absent
and is not a dependency of this path. A final post-review probe visibly rendered
`任务进度 0/3 ●○○` separately from reasoning, completed with returncode 0, and
emitted no body-status or isolation error; all 20 mirrored sessions had non-empty
content. The frozen full suite passed: 3411 passed, 3 skipped, 3 deselected.
Production has not been touched.
