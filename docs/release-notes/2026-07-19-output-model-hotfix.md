# 2026-07-19 assistant output and model transport hotfix

Status: local ftask candidate only. Production is still running `hermes-multitenancy@6eeca227`; nothing has been pushed, pulled, restarted, or sent to Feishu for this candidate.

## Production evidence

- The current `custom:litellm-sre/kimi/k3[1m]` selector reaches LiteLLM as `kimi/k3[1m]` and returns HTTP 401 `invalid_model`; the same credential and endpoint return HTTP 200 with non-empty content for `kimi/k3`.
- The WebUI database contains three assistant rows with the exact empty-message protocol placeholder. The multitenancy context table contains no matching row at the time of the probe.
- One `halo` attempt creates one persisted assistant error row and one server flush per run. The duplicate visible WebUI answer is therefore a client hydration issue, handled in the WebUI repository.

## Candidate contract

- Preserve `[1m]` in configuration and UI selection, but strip a trailing numeric `k`/`m` context label only when resolving a `custom:` provider's upstream model name.
- Remove only `[System: Empty message content sanitised to satisfy protocol]` from assistant `content` and `done` results before parent mirroring and delivery. Mixed normal text is preserved; other System text is untouched.
- Keep ordinary provider/model specs, thinking, tool, approval, and clarification events unchanged.

## Known gotchas

- A model catalog/display selector is not necessarily a valid transport model ID. Validate the exact payload accepted by the OpenAI-compatible endpoint.
- Filtering only the final response is too late for streaming delivery; sanitize assistant deltas before mirror upsert and yield.
- An empty sanitized event must not create an empty mirror row.

## Local evidence

- Red tests reproduced both failures before implementation.
- Focused normalization/output tests: 9 passed.
- AIAgent subprocess plus focused tests under the canonical Hermes venv: 197 passed.
- Official `uv run --extra test pytest -q`: 2509 passed, 1 skipped, 3 deselected.
- System Python is not a valid full-suite environment for this repository because it lacks Hermes `tools`/`agent` packages and pytest-asyncio; the canonical Hermes venv is the required test runtime.

## Production acceptance after explicit authorization

1. Back up both repositories and message/session databases.
2. Publish both repository changes through the standard ff-only path and restart the affected services.
3. Send one WebUI message, complete it, then reconnect/resume and confirm one assistant bubble.
4. Send a Feishu message through the affected profile and confirm normal content without the protocol placeholder.
5. Confirm the default selector remains `[1m]`, the upstream request succeeds as `kimi/k3`, and no new `streaming exhausted`/`invalid_model` appears.
6. Recheck WebUI, Feishu delivery, profile apiserver dependency, and `multitenancy_sessions` context mirroring.
