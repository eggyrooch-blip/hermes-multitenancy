# WebUI Feishu UAT Ensure Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make every Feishu-authenticated WebUI user either already have a valid Feishu UAT credential or be guided through the same device-flow authorization semantics as `/feishu_auth` before using Feishu tools.

**Architecture:** Keep WebUI login as identity/profile binding only. Add a router-owned Run Broker credential/auth seam that checks and creates Feishu UAT sessions against `profile_name + open_id`, then let WebUI BFF proxy those status/start/poll calls using the signed server-side session. Store resulting UAT payloads in `multitenancy_credentials` and the existing profile-local JSON compatibility location; do not store raw UAT in WebUI cookies, localStorage, or WebUI DB.

**Tech Stack:** Python `aiohttp` sidecar in `hermes-multitenancy`, existing `CredentialStore`, existing `hermes_cli.feishu_auth` device-flow helpers, Koa/TypeScript BFF in `hermes-web-ui`, Vue login flow, pytest and Vitest.

### Task 1: Multitenancy UAT Auth Session API

**Files:**
- Create: `hermes_multitenancy/feishu_uat_auth.py`
- Modify: `hermes_multitenancy/webui_broker_server.py`
- Test: `tests/test_webui_feishu_uat_auth.py`

**Step 1: Write failing tests**

Test the following:
- `GET /api/run-broker/credentials/feishu/uat/status` returns redacted `missing`, `valid`, `expired`, or `scope_missing`.
- `POST /api/run-broker/feishu-auth/sessions` starts a session only for a valid profile/open_id route and returns `session_id`, `verification_uri`, `user_code`, and `expires_at`.
- polling a successful session saves the token for exact `profile_name + open_id` and rejects a mismatched authorized `open_id`.

**Step 2: Run tests to verify failure**

Run:

```bash
cd /Users/kite/code/hermes-multitenancy
pytest tests/test_webui_feishu_uat_auth.py -q
```

Expected: fail because the test file targets missing module/routes.

**Step 3: Implement minimal server support**

Create `feishu_uat_auth.py` with:
- `credential_status(shared_home, profile_name, open_id, required_scopes=None)`
- `start_session(profile_name, open_id, scope=None, ...)`
- `poll_session(session_id)`
- `cancel_session(session_id)`

Add broker routes:
- `GET /api/run-broker/credentials/feishu/uat/status`
- `POST /api/run-broker/feishu-auth/sessions`
- `GET /api/run-broker/feishu-auth/sessions/{session_id}`
- `DELETE /api/run-broker/feishu-auth/sessions/{session_id}`

**Step 4: Verify green**

Run:

```bash
pytest tests/test_webui_feishu_uat_auth.py tests/test_webui_broker_server.py tests/test_credentials.py -q
```

Expected: all pass.

### Task 2: WebUI BFF UAT Proxy

**Files:**
- Modify: `/Users/kite/code/hermes-web-ui/packages/server/src/controllers/auth.ts`
- Modify: `/Users/kite/code/hermes-web-ui/packages/server/src/routes/auth.ts`
- Test: `/Users/kite/code/hermes-web-ui/tests/server/feishu-oauth.test.ts`

**Step 1: Write failing tests**

Add tests that an authenticated Feishu WebUI request:
- Proxies `GET /api/auth/feishu/uat/status` to the broker using `ctx.state.user.profile/openid`.
- Proxies `POST /api/auth/feishu/uat/start` without trusting profile/open_id from the browser.
- Keeps these routes protected, not public.

**Step 2: Run red test**

Run:

```bash
cd /Users/kite/code/hermes-web-ui
pnpm test tests/server/feishu-oauth.test.ts
```

Expected: fail because routes/controllers are missing.

**Step 3: Implement minimal BFF proxy**

Use `config.runBrokerUrl` and `config.runBrokerKey`; send `Authorization: Bearer ...` to Run Broker when configured. Return broker JSON/status directly. Never return raw tokens; the broker endpoints should never expose them.

**Step 4: Verify green**

Run:

```bash
pnpm test tests/server/feishu-oauth.test.ts
pnpm build
```

Expected: tests and TypeScript build pass.

### Task 3: WebUI Login Ensure UX

**Files:**
- Modify: `/Users/kite/code/hermes-web-ui/packages/client/src/api/auth.ts`
- Modify: `/Users/kite/code/hermes-web-ui/packages/client/src/views/LoginView.vue`
- Test: `/Users/kite/code/hermes-web-ui/tests/client/login-view.test.ts`

**Step 1: Write failing tests**

Cover:
- Existing valid WebUI session with valid UAT goes directly to `/hermes/chat`.
- Existing WebUI session with missing UAT starts a UAT session and shows an authorization link state.
- Clicking Feishu login still redirects to `/api/auth/feishu/login` when no session cookie exists.

**Step 2: Run red test**

Run:

```bash
pnpm test tests/client/login-view.test.ts
```

Expected: fail because UAT API and UI state are missing.

**Step 3: Implement minimal UX**

After `fetchCurrentUser()`, call `fetchFeishuUatStatus()`.
- If `valid`, route to chat.
- If not valid, call `startFeishuUatAuth()` and render verification URL/user code.
- Poll until success, then route to chat.
- Allow retry on error.

**Step 4: Verify green**

Run:

```bash
pnpm test tests/client/login-view.test.ts tests/client/api.test.ts
pnpm build
```

Expected: tests and build pass.

### Task 4: Docs and Release Notes

**Files:**
- Modify: `/Users/kite/code/hermes-multitenancy/ARCHITECTURE-GUIDE.md`
- Modify: `/Users/kite/code/hermes-web-ui/ARCHITECTURE-GUIDE.md`
- Modify: `/Users/kite/Library/Mobile Documents/iCloud~md~obsidian/Documents/My-Second-Brain/hermes/ARCHITECTURE-GUIDE.md`
- Modify: `/Users/kite/Library/Mobile Documents/iCloud~md~obsidian/Documents/My-Second-Brain/hermes/生产环境的实况.md`

**Step 1: Document current semantics**

Record:
- WebUI login remains identity/profile binding.
- UAT ensure is a second tool-authorization layer.
- UAT status and device-flow sessions live in Run Broker sidecar.
- UAT payloads remain in credential vault/profile-local compatibility storage.

**Step 2: Verify docs mention both WebUI and Feishu surfaces**

Run:

```bash
rg -n "WebUI.*UAT|UAT.*WebUI|feishu_auth|credential vault" \
  /Users/kite/code/hermes-multitenancy/ARCHITECTURE-GUIDE.md \
  /Users/kite/code/hermes-web-ui/ARCHITECTURE-GUIDE.md \
  "/Users/kite/Library/Mobile Documents/iCloud~md~obsidian/Documents/My-Second-Brain/hermes/ARCHITECTURE-GUIDE.md" \
  "/Users/kite/Library/Mobile Documents/iCloud~md~obsidian/Documents/My-Second-Brain/hermes/生产环境的实况.md"
```

Expected: each document has a current note.

### Task 5: Final Verification

Run:

```bash
cd /Users/kite/code/hermes-multitenancy
pytest tests/test_webui_feishu_uat_auth.py tests/test_webui_broker_server.py tests/test_credentials.py -q

cd /Users/kite/code/hermes-web-ui
pnpm test tests/server/feishu-oauth.test.ts tests/client/login-view.test.ts tests/client/api.test.ts
pnpm build
```

If deploying later, follow the Hermes production path: local canonical repo verification, commit/push, production `git pull --ff-only`, build/restart systemd, then verify WebUI login, UAT status, one Feishu wiki/doc tool call, and a second-profile canary.
