# Expert release metadata and Hermes-hosted expert mode

Status: local candidate; production unchanged.

- Successful SkillHub plugin installs now persist the upstream `version`,
  `release_id`, and successful install time inside the existing atomic update
  transaction. Re-delivery of the same release keeps the original timestamp;
  failed candidates roll back to the previous metadata with the active plugin.
- `list_experts()` exposes only `release_version` and
  `release_installed_at`; the release id and repository path remain server-side.
  Legacy manifests remain visible without guessed metadata.
- Expert system guidance now presents the selected role as a Hermes-hosted
  expert mode. It no longer asks the model to ignore prior instructions, deny
  Hermes, or hide its provider. Credential ownership, audience filtering, and
  high-risk write confirmation are unchanged.

Targeted verification: `tests/test_skillhub_plugin.py` and
`tests/test_expert_overlay.py`.
