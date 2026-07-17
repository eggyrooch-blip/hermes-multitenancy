# 2026-07-17 release blocker fixes

Status: local ftask worktree only; not pushed or released to production.

## Fixed contracts

- `RunBroker` now checks sandbox policy, prepares the billing identity, and
  only then consumes the request idempotency key. A transient billing/profile
  apiserver failure therefore leaves the same request retryable. WebUI SSE uses
  the same ordering and preserves its existing `error` event plus EOF behavior.
- Feishu credential renewal skips a user while an authoritative invalid-refresh
  `.needs_reauth` marker exists. Removing the marker after successful user
  authorization restores normal proactive refresh. Non-authoritative markers
  continue through the ordinary retry/diagnostic path.

Known gotcha: never move billing preparation back behind `mark_seen`; a profile
apiserver outage would turn a safe retry into a permanent duplicate. Never skip
renewal for every `.needs_reauth` file; only Feishu-authoritative invalid refresh
markers are terminal until reauthorization.

## Local evidence

- Focused RunBroker, credential-renewal, and WebUI broker tests: 132 passed.
- Full suite: 2331 passed, 1 skipped, 3 deselected.
- No real Feishu message was sent and no production service or database was
  changed during this task.

## Production acceptance still required

Before release, take a recoverable backup of the production multitenancy repo,
`/home/hermes/.hermes/multitenancy.db`, relevant profile Feishu UAT/marker
state, and service configuration. After the normal ff-only install/restart
path, verify all of the following:

1. WebUI Run Broker SSE succeeds and a forced preparation failure remains
   retryable with the same idempotency key.
2. Feishu delivery still works without repeated refresh calls for a user whose
   authoritative marker is present; a completed reauthorization resumes it.
3. The profile apiserver dependency has the expected health/failure behavior.
4. `multitenancy_sessions` continues mirroring user/assistant context and row
   counts advance for a controlled canary.

These checks are release prerequisites; this note is not production evidence.
