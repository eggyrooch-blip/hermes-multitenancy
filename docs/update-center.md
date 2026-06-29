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
hermes-multitenancy-update-center kep-sync --systems-file kep-systems.json
```

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
