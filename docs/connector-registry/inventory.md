# Connector Inventory — Phase 0 baseline freeze

> Source plan: `2026-06-18-hermes-connector-registry-plan.md` (sunke-approved).
> This file freezes the baseline of the FIVE first-party connectors before the
> registry wraps them. No behavior changed in Phase 0/1 — this is documentation
> + the map from each connector to its existing reader/auth code and to the
> regression guards that pin it.

## The five connectors

| id | provider | invocation | auth_flow | reader (status) | auth start/poll/complete | runtime_policy_owner |
|---|---|---|---|---|---|---|
| `lark-cli` | lark | native_cli | existing_profile_secret | `credential_hub.lark_cli_status` → `feishu_uat_auth.credential_status` | `feishu_uat_auth.start_session` (router `/auth`) | **authsidecar_broker** |
| `feishu-project` | feishu-project | native_cli (meegle) | existing_profile_secret | `credential_hub.feishu_project_status` (meegle CLI) | meegle `auth login` (out of band) | connector_driver |
| `keep-record` | keep | skill_script | qr_poll | `credential_hub.keep_record_status` (`.keepai/.env` + verified marker) | `credential_hub_auth.start_keep_record_qr` / `poll_keep_record_once` | connector_driver |
| `kep-cli` | keep | cli_command (kep-auth) | oauth_callback | `credential_hub.kep_cli_status` (keyring + live `kep-auth status` + JWT exp) | `credential_hub_auth.start_kep_cli_login` / `kep_cli_logged_in` / `complete_kep_callback` | connector_driver |
| `gitlab` | gitlab | token | manual_token | `credential_hub.gitlab_status` (readable token file → configured) | operator-placed (no interactive flow) | connector_driver |

Display order is locked to `credential_hub.CREDENTIAL_ORDER` =
`(lark-cli, feishu-project, keep-record, kep-cli, gitlab)` and mirrored by
`connectors.builtin.CONNECTOR_ORDER`.

## Status vocabulary (unchanged)

Exactly the WebUI `SkillCredentialState` set — `authenticated / configured /
needs_auth / unknown / missing / error`. No `expired`: an expired credential
collapses to `needs_auth`; remaining validity rides the additive `expires_at`
(epoch ms).

## Capability-no-regression checklist → guard mapping

Every line of the plan's "能力不退化清单" maps to at least one pinned test/smoke:

| Must-not-break | Guard |
|---|---|
| lark-cli native tool registered, schema/shortcut/api modes | `tests/test_lark_cli_tool_registration.py` |
| lark-cli auth broker HMAC/run-token/identity allowlist | `tests/test_lark_cli_auth_broker.py` (+ retry) |
| lark-cli binary policy / terminal block | `tests/test_lark_cli_binary_policy.py`, `tests/test_lark_cli_terminal_block.py` |
| 5-reader status semantics (lark/feishu-project/keep/kep/gitlab) | `tests/test_credential_hub.py` |
| keep-record QR / kep-cli OAuth start+complete, callback rewrite | `tests/test_credential_hub_auth.py` |
| kep-cli expired JWT → needs_auth (not authenticated/unknown) | `tests/test_credential_hub.py` + `tests/test_connector_auth_contract.py` |
| legacy `/credentials/hub` output byte-identical | `tests/test_connector_auth_contract.py` (golden round-trip) |
| ConnectorStatus carries scope/profile/identity/owner; no secret leak | `tests/test_connector_registry.py` |
| cross-profile status cache isolation | `tests/test_connector_status_cache_isolation.py` |
| requirement detection parity (registry == legacy Python) | `tests/test_connector_requirement_parity.py` |
| Feishu `/auth` card behavior (router consumes collect_credential_statuses) | covered transitively by byte-identical output guard |

## What Phase 1 added (and did NOT change)

- ADDED: `connectors/` package; `GET /api/run-broker/connectors`; scope fields.
- UNCHANGED: every reader; `credential_hub_auth`; the `/credentials/hub` and
  `/credentials/kep-cli/callback/{sid}` endpoints; router `/auth`; the WebUI.
  `collect_credential_statuses()` now delegates through the registry but returns
  byte-identical `CredentialRow` objects (registry only adds fields; `compat`
  drops them for the legacy shape).
