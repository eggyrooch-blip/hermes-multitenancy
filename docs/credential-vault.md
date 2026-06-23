# Multitenancy Credential Vault

`multitenancy_routing` remains the routing table. It maps a Feishu user or
business user id to a profile. It must not store token payloads.

Credentials live in `multitenancy_credentials` inside `multitenancy.db`:

- `profile_name`: owning Hermes profile
- `subject_id`: provider subject, for Feishu this is the `ou_*` open_id
- `provider`: `feishu`, `openai`, `anthropic`, or another provider key
- `secret_kind`: `uat`, `api_key`, `oauth_bundle`, etc.
- `scopes_json` / `scope_hash`: status metadata for scope checks
- `expires_at`: optional epoch milliseconds
- `encrypted_payload`: sealed JSON payload
- `active`: soft-disable flag

The model-visible broker tool is `multitenancy_credential_status`. It only
reports current-profile status:

```json
{
  "profile": "owner",
  "provider": "feishu",
  "subject_id": "ou_...",
  "credential_kind": "uat",
  "status": "valid",
  "storage": "multitenancy_db",
  "expires_at": 1770000000000,
  "scopes": ["contact:user.base:readonly"],
  "missing_scopes": [],
  "has_credential": true,
  "runtime_available": true,
  "sandbox_note": ".env/auth.json are masked by design"
}
```

It never returns `access_token`, `refresh_token`, API keys, or raw payloads.
Cross-profile status queries are rejected.

Redacted status reads are intentionally weaker than runtime secret reads:
`CredentialStore.get_status()` may read scope/expiry metadata without
`HERMES_MULTITENANCY_CREDENTIAL_KEY` / `HERMES_CREDENTIAL_KEY`, but
`put_credential()` and `get_secret_for_runtime()` still require the key.
For Feishu UAT, `has_credential` / `runtime_available` mean the selected source
is usable by the runtime for the requested scopes and current expiry, not
merely that a vault row contains encrypted bytes or a local JSON file contains
some payload. Keyless vault metadata may still be reported for diagnosis with
`runtime_available=false`, but it must not override a runtime-usable
`profiles/<profile>/feishu_uat/<open_id>.json` compatibility file. This lets
`multitenancy_credential_status`, Connector Registry, and the lark-cli canary
report a usable local connector when the profile-local UAT JSON is valid, while
still never exposing token fields to the model or WebUI.
Profile-local Feishu UAT fallback reads the same access-token expiry aliases as
runtime status (`expires_at`, `expire_at`, and `access_token_expires_at`), so
canary checks do not report an expired compatibility JSON as ready.

## Group credential materialization

Some existing skills are not vault-aware and expect a filesystem token, for
example `/workspace/credentials/gitlab.token`. For those cases, keep the raw
token in this vault once and materialize a profile-local compatibility file for
the authorized audience:

```yaml
# <shared HERMES_HOME>/credential-materialization.yaml
credentials:
  - subject_id: kep-prd-analysis
    provider: gitlab
    secret_kind: token
    target: workspace/credentials/gitlab.token
    profile_file: lists/kep-prd-analysis.txt
```

The source row is `profile_name=__shared__`, and the payload defaults to the
`token` field. `hermes-multitenancy-sync pull-feishu` runs this step after
profile sync when the config exists; operators can also run
`hermes-multitenancy-sync materialize-credentials --dry-run` before applying.
Generated files stay inside the profile, are written atomically, and are mode
`0600`.

Feishu UAT runtime migration is read-through:

1. The sandboxed AIAgent asks Feishu tools for the current user's UAT.
2. `agent_real._configure_feishu_uat_home()` has already rebound Feishu UAT
   lookup to `<PROFILE_HOME>/feishu_uat`.
3. The plugin now patches `_load_uat()` to check the credential vault first.
4. If a valid DB credential exists, runtime code receives that payload.
5. If not, the legacy profile-local JSON file is read and then imported into
   the vault as a sealed credential row.

This keeps existing profile-local JSON behavior working while moving the
long-term source of truth into the multitenancy broker. Operators should treat
the profile-local JSON as a compatibility mirror and fallback, not as a file the
model or status surfaces may inspect directly for secrets.

Operational rules:

- Set `HERMES_MULTITENANCY_CREDENTIAL_KEY` or `HERMES_CREDENTIAL_KEY` before
  enabling DB-backed credentials in production.
- Do not classify a missing credential key as user re-auth by itself. If a valid
  profile-local Feishu UAT JSON exists, lark-cli can still run through the
  authsidecar broker; status and canary checks should report that connector path
  as available.
- Keep `.env`, `auth.json`, and shared `feishu_uat` masked in bwrap. Seeing
  `/dev/null` for those files inside the sandbox is expected.
- Agent-visible diagnostics should call `multitenancy_credential_status`; they
  must not inspect `.env` or token files directly.

Follow-up hardening tracked outside this P0:

- Quarantine or audit unmanaged profile-local skill remnants so WebUI skill
  distribution cannot be confused with stale self-installed skill directories.
- Add an immutable denylist for subprocess env overrides that must not be
  supplied by model-visible arguments or connector payloads.
- Make profile/skill manifest writes atomic so interrupted connector or skill
  sync cannot leave partial state.
- Evaluate SQLite WAL and a write mutex for credential/profile sync paths that
  can run concurrently.
