# 2026-07-17 multitenancy release blockers

Status: local ftask candidate only. Nothing was pushed or deployed and production is unchanged; final-hash SIM and fresh review remain release gates.

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
- WebUI per-run model metadata now treats an explicit provider as the selector boundary. A slash-bearing raw model ID such as `kimi/k3` therefore resolves to `custom:litellm-sre/kimi/k3`; a full selector without a separate provider and an already same-provider-prefixed selector remain unchanged.
- CardKit stream recovery now sends the required nested settings envelope (`config.streaming_mode=true`) before retrying the same failed frame exactly once. Final close uses the same envelope with `false`; the retry keeps the original card and strictly advances sequence numbers.

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
- Recovery now reports success only when both exact canonical marker paths are observably absent. A read-only/unlink failure or a newer authoritative sibling therefore leaves the result false instead of treating an attempted unlink as recovery.
- A legacy marker can resolve a profile only through exactly one active user route for the same `open_id`; an optional safe `profile` in the marker must agree with that route. Missing, duplicate, unsafe, or disagreeing routes fail closed.
- Invalid UTF-8, oversized content, deep JSON, overlong integers, non-boolean authority and other malformed/non-authoritative markers are untrusted and do not freeze renewal or crash the tick.
- Refresh, authorization storage and authoritative failure-marker creation now share one exact-identity critical section. A stale refresh cannot finish after a new authorization and overwrite its UAT or recreate the marker that authorization just cleared.
- The critical section is re-entrant in one process and uses a persistent profile-local `flock` across gateway, WebUI and sandbox processes. The hashed `0600` lock file is placed under the profile's writable `feishu_uat` mount; shared-home-only lock inodes are deliberately avoided because Linux bwrap does not bind that directory and the macOS profile does not allow writing it.
- Lock acquisition, unlock, and failure-marker errors are isolated per route. One malformed or read-only profile counts as failed and does not prevent later identities in the same renewal tick from being scanned.
- Public refresh normalizes and authorizes the route before it creates a profile-local lock artifact, then rechecks after acquisition. Its in-process lock registry counts owners and waiters and evicts the entry after the last user exits, avoiding state allocation for rejected identities and unbounded memory growth under route churn.
- Once OAuth has exchanged a one-time device token, poll keeps that payload only for an explicit transient store condition: SQLite `BUSY/LOCKED`, or an `OSError` carrying `EAGAIN`, `EBUSY`, `EINTR`, or `ETIMEDOUT`. The next poll retries storage without another token exchange. Schema/readonly/capacity and unknown failures clear the in-memory payload and still raise; they are not hidden as retryable vault failures.
- A terminal `FeishuUatAuthError` from storage clears the cached payload and moves the session to a stable `error` state before it is re-raised. Later polls return the original error without polling the consumed device code again. Public session data never contains the cached token.
- Each authorization session owns one private poll lock. One caller exclusively covers status/expiry check, token exchange, storage, and terminal transition; a concurrent duplicate returns redacted `pending` without exchanging or storing. The lock is released on every return/exception and has no global side registry; cancel uses the same lock.
- Cached payloads do not extend the device-flow lifetime. At `expires_at`, poll clears the payload and returns stable `expired` without either another exchange or a late store. The absolute deadline is checked again after the optional user-info request, immediately before storage, after the identity lock is acquired, and after SQLite obtains the writer but before credential commit; a late writer rolls back. Every non-transient store exception similarly clears the payload, persists a safe terminal error, then re-raises once.
- Device authorization and access/refresh token TTLs must parse to positive seconds bounded by protocol limits. Invalid, negative, or implausibly large values fail closed; post-exchange poll rechecks the original device deadline before interpreting or storing a token, and protocol failures become a stable redacted session error without re-exchange.
- Marker recovery resolves and validates one profile/open_id before taking the existing re-entrant identity lock, then re-resolves and performs the complete marker/evidence/stat/unlink decision inside it. A writer that replaces the marker while owning that lock wins; recovery sees the new generation and leaves it. An authoritative legacy marker with no unique active route, or whose claimed profile disagrees with that route, is never deleted and does not create a guessed profile lock directory.
- OAuth storage revalidates the unique active route inside the exact identity lock. User-route `upsert` and `soft_delete` now acquire the same old/new `(profile, open_id)` lock keys in stable order, take SQLite's immediate writer boundary, and retry if the binding changed before that transaction. A route mutation therefore cannot pass between validation and the vault/profile/marker writes.
- Identity-lock setup explicitly hardens the exact profile and `feishu_uat` directories to `0700` without changing the deployment-owned `profiles` parent, which is outside the macOS sandbox write allowlist.

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
- Do not classify every `OperationalError` or `OSError` as retryable. Only concrete lock/busy or retry errno evidence may retain the exchanged token; a missing schema, readonly/corrupt database, full filesystem, or unknown exception must fail closed and clear it.
- Never make device-token exchange a check-then-act outside the session lock; duplicate WebUI polls otherwise consume the same one-time code twice. A cached token is still bounded by the original session expiry.
- Never carry a pre-lock marker stat into deletion. Resolve a safe identity, take its lock, then re-read both marker generation and recovery evidence; an ambiguous legacy marker remains authoritative.
- Marker recovery success is an observed postcondition, not an unlink attempt: both exact canonical markers must be absent, including any newer authoritative sibling.
- A route check before waiting on the identity lock is stale evidence. Revalidate the unique active user route inside `_store_uat` before any vault or compatibility-JSON write.
- The in-lock route check is still a TOCTOU if route writers ignore that lock. User-route mutation must acquire the exact old/new identity keys in stable order and re-read the binding after obtaining SQLite's writer boundary.
- Device exchange is not the last fallible OAuth hop. User-info lookup and credential-lock wait can cross the original session deadline, so persistence must receive and enforce that absolute deadline itself.
- A pre-write deadline check is not enough when SQLite can wait for a writer. Recheck after the write statement acquires the transaction and roll back before commit when the absolute deadline has passed.
- Never trust Feishu TTL fields as scheduling input. Reject non-positive, invalid, or over-limit values and recheck the device deadline after the network exchange before persisting its result.
- A routing write must not provision or re-permission an unprovisioned profile just to acquire a credential fence. It may create the private `feishu_uat` lock directory only under an already-provisioned profile, must leave the profile mode unchanged, and must time out visibly instead of waiting forever.
- Retaining a one-time OAuth payload for a retry also creates a secret-lifecycle owner. Expired abandoned sessions must be swept without racing an active poll, and error/pending responses must not validate success-only TTL fields.
- A slash inside a WebUI `model` field does not prove that the value already contains its provider. When `provider` is present, preserve that boundary and only treat an exact same-provider prefix as already assembled.
- CardKit `card.settings` does not accept `streaming_mode` at the top level. Keep it under `config` for both reopen and close; a successful settings call is required before the one same-frame retry.

## Local evidence

- RunBroker + sync/async ingest: 114 passed.
- Lifecycle-focused credential, hook, broker, ingest and streaming-card selection: 315 passed in 2.04s.
- Final Feishu UAT-auth + renewal files: 114 passed. The latest reviewer selection had 14 failures before these fixes and 14 passes afterward (unlink/read-only, newer sibling, wrong safe profile, route swap, exchange deadline, and invalid/negative/huge TTL cases).
- Adjacent credential/cron/WebUI-auth focused set: 144 passed.
- Repository TEST gate on the current working tree: 2478 passed, 1 skipped, 3 deselected in 76.36s.
- Python compile checks and `git diff --check`: passed.
- Real aiohttp regressions cover billing/store failure, materialization failure with changed-secret retry, concurrent mismatch, timeout, interactive and non-interactive pre-admission cancellation, post-mark outer cancellation, shared/stable/job task-factory failure, first-step cancellation, running job cancellation, deferred Feishu completion, and capacity reuse.
- Independent review found the original four lifecycle/fail-closed gaps plus three Feishu completion ownership races. Every finding now has a failing-without-the-fix regression; two independent read-only rechecks replayed the original races and returned PASS. Refreshed ftask SIM and LEAK remain release gates.
- A later formal review caught the stale-refresh/concurrent-reauthorization marker race. Thread-barrier, same-thread re-entry, profile-path validation, persistent lock-location, and real cross-process `flock` regressions now cover that finding; a fresh non-failing formal review is still required before release.
- Focused K3/CardKit regression: 67 passed.
- Manual-review P1 repairs: deterministic deadline/route-mutation barriers, vault rollback, permission hardening and strict doctor coverage passed; the final focused selection passed 29 tests. Routing, sync, WebUI UAT auth and credential renewal adjacent coverage previously passed 210 tests; three user auto-provision call paths also passed.
- Fresh Opus review confirmed duplicate billing/device-token exchange is closed, then found route-lock provisioning/unbounded wait plus abandoned-token and pending-TTL lifecycle gaps. The repairs now have 225 adjacent routing/renewal/sync/WebUI-auth passes: no unprovisioned profile creation or existing-profile chmod, a five-second typed route-lock timeout, capped route-generation retries, expired-session secret eviction, success-only TTL validation, and terminal-state preservation for competing polls.
- The existing SIM predates this final working tree. It must be recaptured and rechecked after `ftask save` so evidence binds the final committed code hash.

No production service, database, model setting, Feishu credential, or user session was changed while collecting this evidence.

## Production acceptance

After local ftask ship and sunke's required verification on main, but before any production write:

1. Back up both deployed repos, WebUI and multitenancy databases, systemd/service environment, ingest/model configuration, and profile Feishu UAT/marker state.
2. Verify every backup exists, is readable, and has a recorded checksum.
3. Deploy through the documented ff-only install/build/restart path.
4. Re-read LiteLLM `/v1/models`, confirm exact `kimi/k3`, and switch the default model only after the services are healthy.
5. Verify ports, health, service state and logs; run a real WebUI employee-ticket path with screenshot/GIF; verify Feishu delivery, profile-apiserver dependency behavior, and `multitenancy_sessions` user/assistant context mirroring.

These are pending production canaries, not claims made by this local release note.
