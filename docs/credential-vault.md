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
  "profile": "sunke",
  "provider": "feishu",
  "subject_id": "ou_...",
  "credential_kind": "uat",
  "status": "valid",
  "storage": "multitenancy_db",
  "expires_at": 1770000000000,
  "scopes": ["contact:user.base:readonly"],
  "missing_scopes": [],
  "has_credential": true,
  "sandbox_note": ".env/auth.json are masked by design"
}
```

It never returns `access_token`, `refresh_token`, API keys, or raw payloads.
Cross-profile status queries are rejected.

Feishu UAT migration is read-through:

1. The sandboxed AIAgent asks Feishu tools for the current user's UAT.
2. `agent_real._configure_feishu_uat_home()` has already rebound Feishu UAT
   lookup to `<PROFILE_HOME>/feishu_uat`.
3. The plugin now patches `_load_uat()` to check the credential vault first.
4. If a valid DB credential exists, runtime code receives that payload.
5. If not, the legacy profile-local JSON file is read and then imported into
   the vault as a sealed credential row.

This keeps existing profile-local JSON behavior working while moving the
long-term source of truth into the multitenancy broker.

Operational rules:

- Set `HERMES_MULTITENANCY_CREDENTIAL_KEY` or `HERMES_CREDENTIAL_KEY` before
  enabling DB-backed credentials in production.
- Keep `.env`, `auth.json`, and shared `feishu_uat` masked in bwrap. Seeing
  `/dev/null` for those files inside the sandbox is expected.
- Agent-visible diagnostics should call `multitenancy_credential_status`; they
  must not inspect `.env` or token files directly.
