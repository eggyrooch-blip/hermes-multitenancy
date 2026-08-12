# Local-agent Feishu relay candidate — 2026-08-10

Status: local ftask worktree only; production unchanged.

Added a standalone, identity-bound Feishu relay for local agents. Enrollment, self-only message/card delivery, deterministic reply assignment, reaction lifecycle, card first-writer-wins, idempotent retry, restart recovery, self-revocation, encrypted state, and retention are covered by `tests/test_agent_relay.py`.

Focused verification: 140 passed across the new relay plus existing push/card/credential suites. The canonical test runner completed with 3456 passed, 4 skipped, 3 deselected, and 7 unrelated local-environment failures: billing-readiness rejects macOS's writable `/private/tmp`, and the lark-cli matrix expects a missing task TMPDIR. No production or employee-visible UAT was attempted.

The relay has a dedicated application, database, key, port 8770, long connection, and systemd template. It has no route or fallback to Run Broker 8766, profile apiserver 8655, UAT, WebUI, cron, profiles, or `multitenancy_sessions`.

## 2026-08-12 text-ingress observability candidate

The relay now observes every background message/reaction callback Future and logs one redacted ERROR if processing fails. Text ingress logs one INFO outcome for invalid, unmatched, ambiguous, or assigned events; actor IDs are fingerprinted and message text is never logged. The process explicitly enables INFO logging, so these audits are visible under systemd. Reply assignment, identity checks, Feishu configuration, API contracts, and database state are unchanged. Local relay tests: 10 passed; not shipped or deployed.
