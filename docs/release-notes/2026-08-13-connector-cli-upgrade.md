# Connector CLI upgrade 2026-08-13

Hermes connector runtimes are pinned to lark-cli/authsidecar 1.0.86 and
`@lark-project/meegle` 1.0.19. The existing Meegle provisioner now upgrades a
stale direct installation instead of accepting any executable version.

PRE intentionally has no sunke credential binding. Its release gate therefore
checks versions, embedded skills, the authsidecar protocol, unscoped fail-closed
behavior, profile-local Meegle status and WebUI health. Online additionally
requires the existing unique sunke route to pass a read-only identity canary,
with no employee-visible Feishu write.

Rollback restores the per-environment authsidecar backup and the npm package
versions recorded before installation.

Production is live on `release-20260813-04`: multitenancy
`2f46bf2822363072e776ec3a2133ed3c2ed878a6` contains the release guard and
WebUI is `6fd2037393027ce72b05884cfd83b88bc0790ec0`. The shared authsidecar,
ordinary CLI, and sunke npm CLI all report 1.0.86; Meegle reports 1.0.19; the
shared and routed sunke skill surfaces each expose 27 lark skills.

The production sunke canary used the registered lark_cli tool and run-scoped
auth broker for read-only user_info. It resolved one active sunke route, the
broker live-verified the token owner, and returned self=1/cross=0 without
printing an identifier or token. Executor probes were 12/12; current services,
ports, both SQLite databases, and 34290/34290 non-empty mirrored sessions are
healthy. Rollback anchor
`/home/hermes/backups/pre-release/release-20260813-04` records SUCCESS. The
release timer was restored enabled/active only after the remote latest tag and
deployed marker both read `release-20260813-04`.
