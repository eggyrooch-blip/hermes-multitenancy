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

## Skill Sync — central pool, not fan-out

Update Center refreshes each system's skill CONTENT at the shared source
`Keep/kep-<system>-cli` from the CLI's embedded `SKILL.md` (via
`kep-cli skills list/read`), so the source is always a matched pair with the
binary. Existing shared skills marked `.kep-cli-managed` are refreshed (new/
changed files written, files dropped upstream pruned); curated (un-marked)
skills are never overwritten. When a system CLI predates embedded skills it
degrades gracefully (keeps existing / writes a stub); a real read error is
surfaced as a quarantined skill row and fails the run's exit code.

**Deployment runs pool-only (`--no-profile-sync`).** Update Center's job is to
keep the central capability pool (shared binaries + shared skill sources) fresh
— it does NOT decide which profile sees which skill. That fan-out stays owned by
the existing distribution layer (`profile-skill-defaults.yaml` /
`skill-distribution.yaml` → `_sync_default_profile_skills`) and by expert
role-override (which scopes each expert to its curated subtractive skill set).
Keeping fan-out out of `kep-sync` is deliberate: pushing every business-CLI
skill into every profile would bloat agent context and dilute an expert's
focused toolset. Profiles/experts pull selectively from the always-fresh pool
via symlinks, so a source refresh propagates to exactly the profiles that
already link it — no extra context, no expert-role conflict.

The `--profile` / all-profiles fan-out path remains available for explicit,
scoped use, but is off in the standardized timer deployment.

## lark-cli Skill Sync — same pool model, binaries untouched

`lark-skill-sync` applies the identical pool-only mirror to the lark ecosystem:
lark-cli embeds every skill's SKILL.md + references in its binary
(`lark-cli skills list/read`), so the local binary is the authoritative,
version-matched source. The command mirrors each embedded skill into the shared
source `~/.hermes/skills/<name>` with the same semantics as kep
(`.lark-cli-managed` marker, idempotent writes, stale-file pruning, real read
errors quarantined with exit 2, old lark-cli without the `skills` verb degrades
to a no-op). It NEVER updates or replaces the lark-cli/authsidecar binaries —
that safety boundary is unchanged.

**Adoption (one-time):** shared sources that already exist without the marker —
notably the legacy `npx skills add larksuite/cli` snapshots, which have drifted
from the embedded truth — are skipped by default so nothing hand-curated is
clobbered silently. Run `lark-skill-sync --adopt` once (optionally scoped with
`--skill <name>`) to take them over; from then on the daily timer
(`deploy/hermes-lark-skill-sync.{service,timer}`) keeps them matched to the
installed lark-cli version.

## Safety Boundaries

The MVP does not auto-apply updates that:

- declare host CLIs or connector changes in a plugin manifest;
- remove or rename previously managed skills;
- require credential, database, or auth protocol migration;
- touch `lark-cli`/authsidecar binaries.

The JSONL ledger is redacted and secret-free. Claude or other LLM review may be
used as a veto/escalation signal, but deterministic gates decide whether an
update can apply automatically.
