# 2026-07-17 release blocker fixes

Status: local ftask worktree only; not pushed or released to production.

## Fixed contracts

- `RunBroker` now checks sandbox policy, prepares the billing identity, and
  only then consumes the request idempotency key. A transient billing/profile
  apiserver failure therefore leaves the same request retryable. WebUI SSE uses
  the same ordering and preserves its existing `error` event plus EOF behavior.
- The public broker contract now separates preparation, admission, and
  execution. `prepare()` issues an opaque `PreparedRun`; successful
  `admit_prepared()` / `prepare_and_admit()` returns a broker-bound
  `AdmittedRun`; and `run_admitted()` atomically claims that capability once.
  Raw requests, capabilities from another preparation boundary or broker, and
  replayed capabilities cannot bypass billing or dispatch a second time.
- `SessionStore.is_event_processed()` checks recent admission state without a
  write. `mark_event_processed()` now uses one conditional SQLite UPSERT;
  per-store connection access remains serialized by its lock, while SQLite's
  single statement decides races across independent connections. Two callers
  racing on a new key therefore return exactly one fresh admission.
  The former SELECT-then-`INSERT OR REPLACE` sequence could let both callers
  report fresh before either observed the other's write.
- Sequential retries skip billing preparation. Concurrent retries for the same
  canonical key now share the whole prepare + optional transform window, not
  just the billing call. The first live waiter performs admission and receives
  one `AdmittedRun`; the rest observe duplicate. `asyncio.shield()` protects
  work shared by other waiters, but the shared task contains no admission: if
  the final waiter is cancelled it is cancelled too, so no idempotency key is
  consumed in the background.
- Feishu vision enrichment changes only the dispatch request. The original
  canonical admission request/key remains stable, including events without a
  `message_id`, so content enrichment cannot split one event between
  `content:` and `idem:` dedupe namespaces. Feishu uses the same broker for
  admission and one-shot dispatch.
- Feishu credential renewal skips a user while an authoritative invalid-refresh
  `.needs_reauth` marker exists. Removing the marker after successful user
  authorization restores normal proactive refresh. Non-authoritative markers
  continue through the ordinary retry/diagnostic path. Marker JSON is read as
  bounded UTF-8; invalid or oversized files are not trusted as authoritative
  and therefore cannot freeze the worker loop. Parser recursion and integer
  digit-limit failures are also treated as untrusted input, so hostile
  10,000-level JSON or a 50,000-digit number cannot crash the renewal tick.
- Async ingest first checks sandbox policy, then materializes its secret
  directory before billing preparation or admission. Partial writes,
  preparation failures, duplicate admission, and request cancellation remove
  that directory. A local write or profile-apiserver failure therefore leaves
  the canonical key retryable; sandbox rejection remains 403 and performs no
  billing I/O. Marker authority is strict JSON boolean `true`, so dirty
  strings/numbers cannot freeze credential renewal.

## Known gotchas

- Never put admission inside a task awaited through `asyncio.shield()`. When
  the final waiter is cancelled, shield keeps that task alive; it can consume
  the key even though no caller remains to receive and execute the capability.
  Keep the shared task limited to prepare/transform, then admit synchronously
  from the first still-active waiter.
- SQLite SELECT followed by `INSERT OR REPLACE` is not an atomic fresh/duplicate
  decision across connections. Both readers can observe absence and both
  return fresh. Keep the conditional UPSERT as the single admission statement.
- Never move billing preparation back behind `mark_seen`; a profile apiserver
  outage would turn a safe retry into a permanent duplicate. Never skip renewal
  for every `.needs_reauth` file; only Feishu-authoritative invalid refresh
  markers are terminal until reauthorization.

## Local evidence

- Current blocker-focused RunBroker, ingest, credential-renewal, SessionStore,
  WebUI broker, and Feishu routing selection: 303 passed.
- Final full suite after the capability, atomic-admission, cancellation, and
  secret-cleanup changes: 2357 passed, 1 skipped, 3 deselected in 58.93s.
- Python compileall and `git diff --check` both passed.
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
