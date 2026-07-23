# 2026-07-23 dormant billing and plugin release gates

Status: local candidate only; not shipped; production billing remains off.

- Readiness CLI trust now covers the complete root-to-leaf path and executes only the pinned file descriptor. Missing `/proc/self/fd`, symlink/path replacement, non-root ownership or writable ancestors fail before subprocess launch.
- The preprovisioned nonce store remains pinned, owner-only, bounded, locked, append-only and fsynced.
- Explicit Plugin inactive events now remove ownership-proven profile entries, distribution entries and existing org-managed fanout before recording inactive.
- Status-less events cannot reinstall an inactive Plugin. Org sync also excludes skills whose registered Plugin owner is inactive or cannot be proven.
- Manual active Plugin state is unchanged by org sync, release, startup and billing readiness. No inactive callback or production org sync was triggered by this change.

Verification: targeted `198 passed, 4 skipped`; full `2800 passed, 1 skipped, 3 deselected`; package compileall passed.

Known gotcha: a Plugin status flag alone does not revoke filesystem-discovered slash skills; revocation must remove owned profile entries and prevent org-sync reinstallation at the shared ownership boundary.
