# kep-cli live identity status

Status: local candidate only; production unchanged.

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
The independent security review passed. The candidate remains unshipped until
explicit release approval.
