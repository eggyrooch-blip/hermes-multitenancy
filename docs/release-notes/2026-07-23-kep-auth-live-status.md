# kep-cli live identity status

Status: released to production at `2a4fbd1d31e7`.

## Root cause

`kep-auth status=valid` only proves that local profile material can be read. A
server-stale token can therefore look authenticated in the connector while OA
or a business CLI rejects it.

## Contract

- Probe the OA online/pre `/ldap/authjwt` endpoint with the current profile
  token.
- Authenticate only when the response has `errorCode=0`, `ok=true`, a future
  `exp`, and an exact `payload.name == KEP_PROFILE`.
- Treat an explicit rejection as `needs_auth`; treat network/protocol failures
  and identity mismatch as unavailable and fail closed.
- Refuse all HTTP redirects so a Bearer token cannot be forwarded to a
  `Location` host; classify 408/429 and unrelated 4xx as unavailable.
- Reuse that result for credential rows, OAuth completion polling, expert
  status text, and the business KEP CLI preflight.
- Embed the trusted profile into each run's generated shim so child env/argv
  overrides cannot select another profile.
- Keep `HTTP 403` as an authenticated permission failure.
- Never log tokens or raw live identity data.

`hermes-agent`, WebUI, kep-auth, business CLI binaries, and OA/KEP upstream are
out of scope and unchanged.

## Local evidence

The targeted suite passes 316 tests across the shared live verifier, credential
status, OAuth polling, the generated CLI shim and its run-scoped wiring, the
registry contract, and the Feishu expert route. The full suite passes 2773
tests with 1 skipped and 3 deselected. After user-owned pre reauthentication,
a read-only production probe found both online and pre valid for the routed
`sunke` identity; no production code, service, or configuration was changed.
The independent security review passed.

## Production evidence

Production pulled `7342655fe96f..2a4fbd1d31e7` with `--ff-only`, completed
editable install and `compileall`, and passed 43 focused KEP tests. The router
and WebUI services are active; ports 8648/8652 and public health return 200,
while unauthenticated Run Broker health returns the expected 401. The live
credential row reports both online and pre authenticated with matching account
and future expiry. Database integrity is `ok`, and all 20,660
`multitenancy_sessions` rows have non-empty mirrored content. Restart-window
error markers are zero; Feishu startup is present with no failure marker.

A timestamped rollback bundle was created in the production backup directory.
No employee message was sent. Chrome could not be claimed for screenshot
evidence, so visual WebUI confirmation remains user-visible rather than
machine-verified.
