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
