# Multitenancy Update Center

Update Center is the controlled update lane for Multitenancy-owned skills and
host CLI capabilities. Its first MVP is intentionally conservative:

- ordinary users must not see host-maintenance prompts such as `lark-cli update`
  or "reopen AI Agent";
- `lark-cli` updates are recorded as internal candidates and can run preflight,
  but the MVP does not replace authsidecar/lark-cli binaries automatically;
- `kep-cli` systems can be installed/updated from an operator-provided system
  manifest and copied into the shared sandbox `bin` directory with atomic file
  replacement;
- skill-only plugin updates may auto-apply only when they are additive and
  deterministic gates pass.

## CLI

The package exposes:

```bash
hermes-multitenancy-update-center scan-lark-notice --file notice.txt
hermes-multitenancy-update-center lark-preflight --profile alice --open-id ou_xxx --target-version v1.0.59
hermes-multitenancy-update-center kep-sync --from-registry            # recommended: live manifest
hermes-multitenancy-update-center kep-sync --systems-file kep-systems.json   # pinned manifest
```

## Daily kep-cli Sync

Production deployments should run `kep-sync` from an operator-owned scheduler
such as a user-level systemd timer (see `deploy/hermes-kep-sync.{service,timer}`).
The scheduler should not depend on an interactive shell profile; set
`HERMES_HOME`, `HOME`, and `PATH` explicitly so both `kep-cli` and the Hermes
virtualenv entry point resolve the same way they do for sandboxed agent runs.

Prefer `--from-registry`: it builds the manifest live from `kep-cli list --json`
instead of a hand-maintained file, so a system newly registered in the aidock
registry is picked up automatically on the next run — no code or config change.
For each active row it sets `installed_version` from the local
`~/.kep-cli/systems/<system>/version` file (empty when not installed) and pins
`target_version` to a `latest` marker, so installed systems run
`kep-cli update <system>` while newly registered systems run
`kep-cli install <system>`. Pass `--include-developing` to also sync
`status=developing` systems. A pinned `--systems-file` remains available when a
deployment needs an explicit, reviewed system set.

If `kep-cli list --json` fails (e.g. not logged in), `--from-registry` aborts
with a non-zero exit and syncs nothing, leaving active binaries untouched.

The timer should keep full JSON output in an internal report file and print
only a short summary to the journal, for example system action counts, skill
action counts, and quarantined system names. Do not print tokens, signed
download URLs, or per-profile credential material.

`kep-systems.json` accepts a list or `{ "systems": [...] }`:

```json
{
  "systems": [
    {
      "system": "hades",
      "binary": "hades-cli",
      "target_version": "1.2.0",
      "installed_version": "1.1.0"
    }
  ]
}
```

When `installed_version` is empty, Update Center runs `kep-cli install <system>`.
When it differs from `target_version`, it runs `kep-cli update <system>`. After a
successful command it resolves the real binary and atomically copies it to
`$HERMES_HOME/bin/<binary>` with mode `0755`. A failed command or missing binary
quarantines the item and leaves the active binary untouched.

For profile skill sync, the CLI defaults to all existing profiles under the
shared home. Existing profile-local or organization-managed skills are not
overwritten; they are reported as quarantined so an operator can inspect them
without breaking a user's current skill surface.

## Skill Sync

For new `kep-cli` systems, Update Center can ensure wrapper skills named
`Keep/kep-<system>-cli`. Existing shared skills are preserved; generated wrapper
skills are only a fallback. Profile installs are symlinks managed through the
existing skill registry, so personal/foreign skill ownership rules continue to
apply.

## Safety Boundaries

The MVP does not auto-apply updates that:

- declare host CLIs or connector changes in a plugin manifest;
- remove or rename previously managed skills;
- require credential, database, or auth protocol migration;
- touch `lark-cli`/authsidecar binaries.

The JSONL ledger is redacted and secret-free. Claude or other LLM review may be
used as a veto/escalation signal, but deterministic gates decide whether an
update can apply automatically.
