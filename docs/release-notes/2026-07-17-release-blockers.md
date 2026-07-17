# 2026-07-17 release blocker fixes

Status: local ftask worktree only; not pushed or released to production.

## Fixed contracts

- `RunBroker` now checks sandbox policy, prepares the billing identity, and
  only then consumes the request idempotency key. A transient billing/profile
  apiserver failure therefore leaves the same request retryable. WebUI SSE uses
  the same ordering and preserves its existing `error` event plus EOF behavior.
- The public broker contract now names its three stages: `check_policy()` is
  non-consuming, `prepare()` issues an opaque `PreparedRun`,
  `admit_prepared()` accepts only a capability from the same preparation
  boundary, and `admit()` performs prepare-before-mark itself. A raw
  `RunRequest` can no longer claim that billing already happened.
- `SessionStore.is_event_processed()` checks recent admission state without a
  write. Sequential retries therefore skip billing preparation, while
  concurrent retries for the same key share one in-flight preparation task;
  atomic `mark_seen` still selects the only request allowed to dispatch.
  Feishu, WebUI SSE, and async ingest now carry the signed prepared capability
  through enrichment/admission/dispatch instead of using `admitted=True`.
- Feishu credential renewal skips a user while an authoritative invalid-refresh
  `.needs_reauth` marker exists. Removing the marker after successful user
  authorization restores normal proactive refresh. Non-authoritative markers
  continue through the ordinary retry/diagnostic path. Marker JSON is read as
  bounded UTF-8; invalid or oversized files are not trusted as authoritative
  and therefore cannot freeze the worker loop. Parser recursion and integer
  digit-limit failures are also treated as untrusted input, so hostile
  10,000-level JSON or a 50,000-digit number cannot crash the renewal tick.
- Async ingest first runs sandbox policy with a non-consuming admission, then
  performs billing preparation, and only then commits real admission. Any billing
  preparation exception (including `RunRejected`) returns a retryable 503 without
  consuming the key; sandbox rejection remains 403 and performs no billing I/O.
  Marker authority is strict JSON boolean `true`, so dirty strings/numbers cannot
  freeze credential renewal.

Known gotcha: never move billing preparation back behind `mark_seen`; a profile
apiserver outage would turn a safe retry into a permanent duplicate. Never skip
renewal for every `.needs_reauth` file; only Feishu-authoritative invalid refresh
markers are terminal until reauthorization.

## Local evidence

- Blocker-focused RunBroker, ingest, credential-renewal, SessionStore, WebUI
  broker, and Feishu routing selection: 292 passed.
- Full suite: 2346 passed, 1 skipped, 3 deselected.
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
