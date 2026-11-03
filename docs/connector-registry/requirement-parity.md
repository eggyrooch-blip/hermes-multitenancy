# Requirement detection — Python vs TS parity table (Phase 0)

> Compares `hermes_multitenancy/credential_hub.py:detect_skill_requirements`
> (Python, the registry's Phase-1 source of truth) against
> `hermes-web-ui/.../skill-credentials.ts:detectSkillCredentialRequirements`
> (TS, the WebUI's current independent detector).
>
> Phase 1 changes NOTHING: the registry wraps the Python detector verbatim
> (`connectors.requirements.detect_for_skill` → `credential_hub.detect_skill_requirements`),
> so registry == Python by construction (pinned by `test_connector_requirement_parity.py`).
> This table records the Python↔TS deltas so the convergence in Phase 1.5/2
> (registry becomes the single source of truth) is a deliberate, reviewed move —
> NOT a silent "whatever Run Broker returns is correct".

## Per-connector regex comparison

| connector | Python | TS | verdict |
|---|---|---|---|
| lark-cli | `lark-cli`,`larksuite`,`open.feishu.cn`,`feishu.cn/(docx\|docs\|sheets\|wiki\|base\|minutes\|file)`,`wiki:wiki:readonly`,`(feishu\|lark\|larksuite)…(docx…)`,reverse,`(im:message\|contact:user\|drive:drive\|wiki:wiki)` | identical | ✅ MATCH |
| feishu-project | `meegle`,`meego`,`feishu-project`,`project.feishu.cn`,`飞书项目` | identical | ✅ MATCH |
| keep-record | `keep-record`,`keep_auth_token`,`get_qrcode`,`persist_auth` | identical | ✅ MATCH |
| kep-cli | `kep-cli`,`kep-auth`,`aidock`,`skillhub`,`keep-login`,`proxy-cms`,`skill/zipfile`,`kep_profile`,`kep_no_auto_login` + `HERMES_MT_KEP_DOMAINS` patterns; hubSourced short-circuit | same base + **hardcoded** `proxy.cms.(pre.)?example.com`, `ark.example.com/aidock-cms`, **`bearer\s+token.*example`**; hubSourced short-circuit | ⚠️ DIFF — see D1, D2 |
| gitlab | `gitlab_token`,`oauth2:${gitlab_token}@` + `HERMES_MT_GITLAB_DOMAINS` patterns | same + **hardcoded** `gitlab.example.com` | ⚠️ DIFF — see D2 |

## Deltas + decisions

### D1 — TS-only pattern `bearer\s+token.*example` (kep-cli)
- **What**: TS flags a skill needing kep-cli if its text matches `bearer token … example`. Python has no equivalent.
- **Risk**: LOW. Real kep skills also match `kep-cli`/`kep-auth`/`aidock`/`skillhub`/`proxy-cms` or are SkillHub-sourced (hubSourced short-circuit), all of which Python already catches. A skill that mentions only "bearer token … example" and none of those is unlikely.
- **Decision**: TS behavior is the *correct, broader* one. It is internal-domain-coupled (`example`), so it must NOT be hardcoded into the public-repo Python source. **Convergence plan (Phase 1.5/2)**: add a configurable `bearer\s+token.*<domain>` pattern to `connectors.requirements`, parameterized by `HERMES_MT_KEP_DOMAINS`, and move it into the registry fixture BEFORE the WebUI switches to the registry. Until then, Python detection is unchanged and the delta is documented, not silently dropped.

### D2 — Hardcoded `*.example.com` domains (kep-cli + gitlab)
- **What**: TS hardcodes `proxy.cms.(pre.)?example.com`, `ark.example.com/aidock-cms`, `gitlab.example.com`. Python externalized these to `HERMES_MT_KEP_DOMAINS` / `HERMES_MT_GITLAB_DOMAINS` (the 2026-06-09 public-repo privacy scrub).
- **Risk**: NONE when the env vars are set; the matched set is then equal. If the env vars are MISSING on a deployment, Python under-detects vs TS.
- **Decision**: This is an INTENTIONAL divergence (scrub), not a bug. Per memory, `HERMES_MT_*_DOMAINS` were written to prod `.env`. **Gate before Phase 2 flips WebUI traffic to the registry**: verify on prod-hermes (`hermes-1`) that `HERMES_MT_KEP_DOMAINS` and `HERMES_MT_GITLAB_DOMAINS` are present in the run-broker env; if absent, the registry would under-detect relative to today's WebUI. Documented here as a hard pre-Phase-2 check.

## hubSourced short-circuit
Both sides force kep-cli when `source ∈ {hub, aidock-skillhub}`. Python parity is
pinned in `test_connector_requirement_parity.py` (a `source='hub'` skill requires
`kep-cli` even with no keyword match).

## kep-cli callback migration decision (Phase 0 requirement)

The plan requires the kep-cli callback ownership + compat strategy be settled in
Phase 0 (not first-run during a traffic flip). **Decision (matches plan §kep-cli
callback 迁移设计)**:

1. The public callback path stays `/api/run-broker/credentials/kep-cli/callback/{session_id}`
   (already served by `webui_broker_server.handle_kep_cli_callback`). The id and
   route do NOT change — already-open OAuth browser tabs keep working.
2. The session store + localhost-forward logic stays in
   `credential_hub_auth.complete_kep_callback` for Phase 1. The registry exposes
   it via `connectors.auth_flows.complete_oauth_callback` / `registry.complete_auth`
   as a thin pass-through (no behavior change).
3. WebUI keeps its own public `/api/auth/kep-cli/callback/{sessionId}` route; in
   Phase 2 it becomes a thin-forward to the Run Broker route (feature-flagged
   `HERMES_CONNECTOR_KEP_CALLBACK_FORWARD`). The old WebUI-local complete stays as
   the rollback path.
4. **Verification (NOT shadow-only)**: before Phase 2, a synthetic callback
   contract test (WebUI thin-forward → Run Broker complete) plus one real
   test-profile OAuth smoke. Recorded as the Phase 1.5 exit criterion.
