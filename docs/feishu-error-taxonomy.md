# Feishu structured error taxonomy

`lark_cli` failures expose exactly these additive machine fields:

- `failure_subsystem`: `credential | identity | permission | lark_api | transport`
- `error_code`: one registered `FEISHU_*` code, or `null` on success
- `retryable`: whether the existing bounded retry policy may retry; this field
  does not start or widen retries

Run-terminal consumers must copy these fields unchanged. They must not parse
`error`, `stdout`, `stderr_redacted`, command arguments, or response content.
The machine fields never contain tokens, identity values, employee data,
command arguments, or response bodies.

## Fixed fixture table

| Trusted signal | `failure_subsystem` | `error_code` | `retryable` |
|---|---|---|---|
| invalid refresh/token code | `credential` | `FEISHU_AUTH_REAUTH_REQUIRED` | `false` |
| principal not bound | `identity` | `FEISHU_IDENTITY_UNBOUND` | `false` |
| credential owner mismatch | `identity` | `FEISHU_IDENTITY_MISMATCH` | `false` |
| permission business code / HTTP 403 | `permission` | `FEISHU_PERMISSION_DENIED` | `false` |
| HTTP 429 / Feishu rate-limit code | `lark_api` | `FEISHU_RATE_LIMITED` | `true` |
| explicit timeout / HTTP 408 | `transport` | `FEISHU_DEPENDENCY_TIMEOUT` | `true` |
| temporary HTTP 5xx | `transport` | `FEISHU_DEPENDENCY_UNAVAILABLE` | `true` |
| HTTP 400/422 or local request rejection | `lark_api` | `FEISHU_REQUEST_INVALID` | `false` |
| other structured non-zero business code | `lark_api` | `FEISHU_BUSINESS_ERROR` | `false` |
| failed attempt without a known signal | `lark_api` | `FEISHU_UNKNOWN` | `false` |

Signal priority is deterministic: explicit trusted hint or HTTP status,
structured Feishu business code, then strict transport/auth text. Process exit
code only distinguishes otherwise-clean success from an unknown failure.

This taxonomy does not change identity selection, authorization, credential
refresh, retry counts, fallback, or user guidance.
