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
- Reuse that result for credential rows, OAuth completion polling, expert
  status text, and the business KEP CLI preflight.
- Keep `HTTP 403` as an authenticated permission failure.
- Never log tokens or raw live identity data.

`hermes-agent`, WebUI, kep-auth, business CLI binaries, and OA/KEP upstream are
out of scope and unchanged.

## Local evidence

The SPEC-targeted suite currently passes 131 tests across credential status,
OAuth polling, the generated CLI shim, expert overlay wiring, and the Feishu
expert route. Full-suite and independent security-review evidence must be added
before release; production requires explicit approval plus a read-only
online/pre canary.
