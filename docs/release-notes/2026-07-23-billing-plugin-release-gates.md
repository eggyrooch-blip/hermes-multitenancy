# 2026-07-23 dormant billing and plugin release gates

Status: local candidate only; not shipped; production billing remains off.

- Readiness CLI trust now covers the complete root-to-leaf path and executes only the pinned file descriptor. Missing `/proc/self/fd`, symlink/path replacement, non-root ownership or writable ancestors fail before subprocess launch.
- The preprovisioned nonce store remains pinned, owner-only, bounded, locked, append-only and fsynced.
- Multitenancy passes only the AI Gateway broker token and process/TLS settings to readiness; LiteLLM base URL and management key stay in AI Gateway.
- Explicit Plugin inactive events now persist revocation before removing ownership-proven profile entries, distribution entries and existing org-managed fanout.
- If cleanup fails, inactive Plugin skills are still excluded from slash, prompt and tool discovery; modified foreign targets are preserved. Status-less events and org sync cannot reinstall an explicitly inactive Plugin, while missing/invalid state fails the operation instead of silently revoking.
- Manual active Plugin state is unchanged by org sync, release, startup and billing readiness. No inactive callback or production org sync was triggered by this change.

Verification: SPEC-targeted `489 passed`; full `2805 passed, 1 skipped, 3 deselected`; package compileall and `git diff --check` passed.

Known gotcha: filesystem cleanup can fail after an inactive callback; the authoritative inactive state must gate every skill discovery path before cleanup is attempted.

Enable blocker: the current AI Gateway snapshot command still needs direct LiteLLM administration environment. Billing must remain off until AI Gateway exposes the equivalent broker URL/token-only readiness client; Multitenancy intentionally fails closed instead of receiving the management key.
