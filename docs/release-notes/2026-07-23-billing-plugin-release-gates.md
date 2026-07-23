# 2026-07-23 dormant billing and plugin release gates

Status: deployed to production at `9792665`; production billing remains off.

- Readiness CLI trust now covers the complete root-to-leaf path and executes only the pinned file descriptor. Missing `/proc/self/fd`, symlink/path replacement, non-root ownership or writable ancestors fail before subprocess launch.
- The preprovisioned nonce store remains pinned, owner-only, bounded, locked, append-only and fsynced.
- Multitenancy passes only the AI Gateway broker token and process/TLS settings to readiness; LiteLLM base URL and management key stay in AI Gateway.
- Explicit Plugin inactive events now persist revocation before removing ownership-proven profile entries, distribution entries and existing org-managed fanout.
- If cleanup fails, RunBroker refuses the affected profile before dispatch and inactive Plugin skills are excluded from slash, prompt and tool discovery; modified foreign targets are preserved. Status-less events and org sync cannot reinstall an explicitly inactive Plugin, while missing/invalid state fails the operation instead of silently revoking.
- Manual active Plugin state is unchanged by org sync, release, startup and billing readiness. No inactive callback or production org sync was triggered by this change.

Verification: SPEC-targeted `489 passed`; full `2805 passed, 1 skipped, 3 deselected`; package compileall and `git diff --check` passed.

Production verification: the same 12 SPEC-targeted files passed `489/489` with an isolated root-only pytest temp directory and the production service PATH. Gateway/WebUI/profile health returned 200; authenticated Run Broker health returned 200 and unauthenticated access returned the expected 401. SQLite integrity was `ok`, and `multitenancy_sessions` content mirroring was `20569/20569`. The manually active Plugin snapshot was identical before and after restart (`active=1`, `missing=2`, aggregate fingerprint `329867b14c157866`); billing remained `HERMES_LITELLM_BILLING_ENABLED=false`. No employee message, model call, org sync or inactive callback was triggered.

Known gotcha: filesystem cleanup can fail after an inactive callback; the authoritative inactive state must gate every skill discovery path before cleanup is attempted.

Enable blocker: the current AI Gateway snapshot command still needs direct LiteLLM administration environment. Billing must remain off until AI Gateway exposes the equivalent broker URL/token-only readiness client; Multitenancy intentionally fails closed instead of receiving the management key.

Follow-up candidate (not deployed): `deploy/install-gateway-dropins.sh` now installs a versioned
`55-litellm-billing.conf` that optionally loads `%h/.hermes/litellm-billing.env` into the main Router.
The environment file stays host-managed and absent is harmless; this change does not create secrets,
enable billing, choose a cohort, or alter manually active Plugins. Installer/startup/readiness focused
verification is `34 passed`.
