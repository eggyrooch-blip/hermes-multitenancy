# Codex Harness W0 technical evidence (local-only)

Contract: `97e1ec3cff75`. This is a sanitized, verbatim read-back from the managed
session transcript, not production-authority evidence. Source transcript sha256:
`e025e0364519ac77405cdb507cd0d06a909231aecc709367c1b2d1835c59f905`.

## 2026-08-31 bounded multi-response receipt candidate

The run-scoped provider proxy now accepts at most eight serial Responses requests,
rejects concurrent and ninth requests, and sums usage for every completed response.
The receipt verifier releases output only when accepted, completed, and usage-bearing
response counts match the parent-selected bound. Targeted tests are 65/65 and SIM
scenarios 1–3 pass; production browser and health scenarios remain pending publish.

## 2026-08-31 production pilot resume candidate

`release-20260831-09` moved the pinned Codex 0.150.1 default under the existing
`.hermes/bin` sandbox mount and closed the Harness-owned app-server session when
each run scope exits. A real first round returned `HARNESS_T1_OK` and left zero
app-server processes. The second round no longer hit an active writer, but the
upstream Responses endpoint rejected LiteLLM's replayed assistant message id.
The replacement candidate removes provider ids only from replayed message input;
tool `call_id` and non-message items are unchanged. Targeted tests are 90/90 and
the repository suite is 4581 passed / 4 skipped. Harness remains fail-closed until
the replacement is published and the two-round canary passes.

## macOS technical chain — source line 14346

```json
{
  "phase": "complete",
  "cleanup": true,
  "gitlab_clone": true,
  "repo_head": true,
  "hub_standin_head": true,
  "gate_a": "LOCAL TEST ATTESTATION",
  "principal": "LOCAL SEALED TEST PRINCIPAL",
  "expert_overlay": "LOCAL STAND-IN; REAL CARRIED-PAYLOAD VALIDATOR",
  "billing_authority": "LOCAL STAND-IN",
  "model": "gpt-5.4",
  "authoritative_model_calls": 1,
  "proxy_ok": true,
  "proxy_request_count": 1,
  "proxy_input_tokens": 14125,
  "proxy_output_tokens": 139,
  "remote_plugin_engine_disabled": true,
  "remote_catalog_clone_dirs": 0,
  "receipt_ok": true,
  "exact_key_unique_spend": true,
  "spend_positive": true,
  "spend_http_requests": 20,
  "store_forced_by_proxy": true,
  "requires_openai_auth_false": true,
  "raw_key_in_codex_home": false,
  "repo_clean": true
}
```

Source line 14364 contains the successful WebUI accessibility tree and the released
gate-A text: `分级`, `UB场景`, `待澄清`, followed by `CODEX_HARNESS_DEMO_OK`.
The local child-env raw-key absence is independently enforced by the OS-child tests.
The GitLab credential has read-only scopes, so it cannot push or create an MR.

## Linux nested sandbox — source lines 15061, 15179-15180, 15188

```text
host=hermes-1
kernel=Linux 5.14.0-701.el9.x86_64
bubblewrap=0.6.3
codex-cli=0.149.1
permission_profile=:workspace
outer_env_mask=pass
host_marker_unmounted=pass
workspace_write=pass
outside_write=denied
network_socket=denied
nested_bwrap_codex=pass
remote_cleanup=pass
leftover_probe_dirs=0
leftover_probe_processes=0
prod_head=eb5d2ac015bd
service_failed_units=0
```

The production GitLab authority, production actor broker, macOS sandbox and production
model execution are outside this W0 technical-feasibility contract and are not claimed.
