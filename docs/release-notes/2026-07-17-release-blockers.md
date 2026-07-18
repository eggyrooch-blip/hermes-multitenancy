# 2026-07-17 multitenancy release blockers

Status: local ftask candidate only. Not pushed or deployed; production is unchanged.

## Fixed contracts

- `RunBroker` validates sandbox policy and completes billing/profile-apiserver preparation before durable idempotency admission. A transient dependency failure does not consume the canonical key.
- Same-loop/process callers with the same broker authority and canonical request identity share preparation, optional transform/before-admit, atomic mark, and one stable execution. The production SessionStore uses a conditional SQLite UPSERT so separate connections still decide fresh-versus-duplicate atomically.
- A fully instrumented stable child is created behind a closed admission gate before durable mark. Mark success opens the gate synchronously; task-factory failure/cancellation leaves the key fresh and runs abandonment cleanup. Last-waiter cancellation before mark cancels the shared task, while cancellation or HTTP timeout after mark releases only the waiter and cannot cancel stable execution.
- A committed execution with zero HTTP waiters rejects a retry immediately as duplicate instead of making it await an unobserved long-running task. Synchronous ingest reports `duplicate_pending`; it never dispatches twice.
- `PreparedRun` is authority-bound. Public `run_prepared` consumes it once; `AdmittedRun` and `_run_admitted` remain the internal one-shot stable execution boundary. Durable admission is the final rejectable boundary, so execution does not re-run mutable sandbox policy.
- Shared-entry ownership transfers only after the task is created and its done callback is installed. A guarded shared-task finalizer runs `on_abandon` exactly once, including task creation rollback and cancellation before the coroutine's first step.
- Stable execution has its own task-done finalizer. Secret cleanup therefore runs after success, exception, normal cancellation, or cancellation before the execute coroutine starts; a coroutine-body `finally` is not relied upon for plaintext cleanup.
- SessionStore initialization and write exceptions now fail closed whenever a canonical idempotency record is required. Initialization unavailability is detected before billing/enrichment; a final atomic-mark write failure can occur after those preparations, but never consumes the key or dispatches. WebUI keeps its SSE `error` + EOF behavior and Feishu sends a retryable storage notice.
- Feishu enrichment preserves the original canonical admission request/key. A user-visible vision-block reply must send successfully before mark; failed send remains retryable and internal vision metadata never reaches normal dispatch.
- Feishu's deferred processing owner is bound to the shared broker entry rather than an individual waiter. A gated, prevalidated async finalizer closes exactly once on vision success/send failure, billing/store/mark failure, duplicate, cancellation, and normal stable execution; leader cancellation with a live peer and post-mark zero-waiter redelivery cannot issue extra FAILURE/SUCCESS completions. Every successful adapter defer also receives an ordered adapter/message generation, and completion snapshots its covered generation immediately before entering the adapter without an intervening await. A late defer that lands after the old snapshot but before shared-entry cleanup therefore completes as its own generation instead of leaking the deferred ID or double-completing the old one. Adapter completion has a five-second timeout.
- A custom `mark_seen`-only WebUI app maintains the existing bounded local duplicate mirror so sequential redelivery does not re-run billing.

## Ingest secret ownership

- Sync ingest claims the idempotency/fingerprint pair before its first shared await. Different credentials cannot join a leader blocked in billing.
- The claim is an object-identity generation with request refs. Pre-admission failure or cancellation releases only that generation; an old cleanup cannot delete a successor claim.
- Fingerprints enter the TTL result map only after durable stable execution exists. A failed secret write leaves no fingerprint or directory, so the same key may retry with corrected credentials.
- The shared-entry leader ref is released by `on_abandon` or execution finalization if its outer HTTP waiter exits before shared work settles. Peers release only their own refs.
- Sync HTTP timeout/outer cancellation after mark leaves the staged secret readable to dispatch, then removes it at stable task completion. Same-key retry is `duplicate_pending`; different fingerprint remains 409.
- Interactive outer cancellation explicitly cancels and settles its broker/interrupt child waiters. Even if billing delays cancellation, the old transient claim stays authoritative until that shared generation reaches a terminal state.
- Async ingest uses the same generation/ref principle plus job ownership. Staging happens after billing but before mark; handoff rollback, request cancellation, terminal job cleanup and TTL prune release exactly their own resources.
- Async ingest creates its background job task behind a closed handoff gate before billing/mark and installs a guarded done-finalizer immediately. Task-factory failure or immediate cancellation is retryable with `mark=0`; running cancellation becomes a pollable failed terminal, removes plaintext secrets, and lets TTL/capacity pruning release the generation.

## Credential renewal

- The renewal worker skips refresh only for a strict Feishu-authoritative marker with `reason=refresh_rejected`, JSON boolean `authoritative=true`, and `refresh_class=invalid`.
- A successful real `_store_uat` clears the exact user's profile-local and legacy marker only after both the credential vault and profile compatibility JSON are durable. Proactive L2 refresh then resumes normally.
- A profile JSON write failure leaves both markers intact. If either marker cannot be removed, authorization reports an explicit server error rather than claiming recovery while renewal remains frozen.
- Invalid UTF-8, oversized content, deep JSON, overlong integers, non-boolean authority and other malformed/non-authoritative markers are untrusted and do not freeze renewal or crash the tick.
- Refresh, authorization storage and authoritative failure-marker creation now share one exact-identity critical section. A stale refresh cannot finish after a new authorization and overwrite its UAT or recreate the marker that authorization just cleared.
- The critical section is re-entrant in one process and uses a persistent profile-local `flock` across gateway, WebUI and sandbox processes. The hashed `0600` lock file is placed under the profile's writable `feishu_uat` mount; shared-home-only lock inodes are deliberately avoided because Linux bwrap does not bind that directory and the macOS profile does not allow writing it.
- Lock acquisition, unlock, and failure-marker errors are isolated per route. One malformed or read-only profile counts as failed and does not prevent later identities in the same renewal tick from being scanned.

## Known gotchas

- Never put a fallible operation after durable mark but before creating the stable owner; create the child behind a closed gate, install its finalizer, then mark and open the gate.
- Never return secret cleanup to the HTTP handler after mark; the handler may time out while execution is still using the files.
- Never rely only on a coroutine-body `finally` for resources: a task cancelled before its first step never enters that body.
- Never set shared ownership before task creation and finalizer installation; task factory failure would strand the caller's transient claim.
- Never let Feishu waiters finish deferred processing independently once they join an entry with a shared completion finalizer. This includes the committed/zero-waiter duplicate path and the narrow finalizer-done/entry-cleanup window.
- Never infer late Feishu lifecycle ownership from entry presence alone. Match each defer generation against the finalizer's covered snapshot; a post-snapshot generation needs its own completion even if it briefly joins the old entry.
- Never create an async job only after durable mark; precreate it behind a gate so task-factory failure stays pre-admission, and let its done callback terminalize cancellation.
- A Feishu hook that defers gateway processing must close that lifecycle on every admitted early return; vision-block replies otherwise leave the Typing reaction and deferred message-id behind.
- Never write the persistent fingerprint before staging/admission. Conversely, do not delay the transient claim until after billing, or a different secret may join the shared entry.
- Claim deletion requires both `request_refs == 0` and map identity equality. Value-only deletion permits a stale generation to erase its successor.
- Coalescing is process-local. SQLite serializes durable admission across connections, but not billing/enrichment/send side effects before mark. Recheck the single same-channel router topology before deployment.
- SQLite `SELECT` followed by `INSERT OR REPLACE` is not an atomic fresh decision; retain the conditional UPSERT.
- A missing SessionStore is not an in-memory fallback for keyed requests; only a request with no dedupe record may proceed without it.
- Only strict authoritative invalid-refresh markers stop renewal. A blanket `.needs_reauth` skip would freeze recoverable credentials.
- Persisting a fresh UAT without clearing both exact reauth-marker paths leaves L2 permanently frozen; marker cleanup belongs after the vault and JSON writes and must fail visibly.
- Never release the identity lock between a refresh exception and `_record_failure`, or between a successful user authorization write and exact marker cleanup. That gap lets an older attempt permanently refreeze the replacement UAT.

## Local evidence

- RunBroker + sync/async ingest: 114 passed.
- Lifecycle-focused credential, hook, broker, ingest and streaming-card selection: 315 passed in 2.04s.
- Feishu UAT storage and renewal focused files: 81 passed; renewal/cron/WebUI-auth audit set: 84 passed.
- Repository TEST gate (`make test`): 2420 passed, 1 skipped, 3 deselected in 57.43s.
- Python compile checks and `git diff --check`: passed.
- Real aiohttp regressions cover billing/store failure, materialization failure with changed-secret retry, concurrent mismatch, timeout, interactive and non-interactive pre-admission cancellation, post-mark outer cancellation, shared/stable/job task-factory failure, first-step cancellation, running job cancellation, deferred Feishu completion, and capacity reuse.
- Independent review found the original four lifecycle/fail-closed gaps plus three Feishu completion ownership races. Every finding now has a failing-without-the-fix regression; two independent read-only rechecks replayed the original races and returned PASS. Refreshed ftask SIM and LEAK remain release gates.
- A later formal review caught the stale-refresh/concurrent-reauthorization marker race. Thread-barrier, same-thread re-entry, profile-path validation, persistent lock-location, and real cross-process `flock` regressions now cover that finding; a fresh non-failing formal review is still required before release.

No production service, database, model setting, Feishu credential, or user session was changed while collecting this evidence.

## Production acceptance

After local ftask ship and sunke's required verification on main, but before any production write:

1. Back up both deployed repos, WebUI and multitenancy databases, systemd/service environment, ingest/model configuration, and profile Feishu UAT/marker state.
2. Verify every backup exists, is readable, and has a recorded checksum.
3. Deploy through the documented ff-only install/build/restart path.
4. Re-read LiteLLM `/v1/models`, confirm exact `kimi/k3`, and switch the default model only after the services are healthy.
5. Verify ports, health, service state and logs; run a real WebUI employee-ticket path with screenshot/GIF; verify Feishu delivery, profile-apiserver dependency behavior, and `multitenancy_sessions` user/assistant context mirroring.

These are pending production canaries, not claims made by this local release note.
