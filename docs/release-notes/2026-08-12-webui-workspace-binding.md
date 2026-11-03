# WebUI workspace session binding — local main

- Scope: WebUI `RunRequest` only; no Project semantics, database migration, Feishu or cron behavior change.
- Contract: optional profile-relative directory; omitted means profile workspace root.
- Enforcement: owner/profile routing first, then existing-directory and realpath containment; invalid values fail before dispatch.
- Runtime: `WORKSPACE=<profile>/workspace`, `TERMINAL_CWD=<profile>/workspace/<selection>`, and the AIAgent child runs from that cwd for one turn; the selected cwd is re-pinned after AIAgent initialization so tool config cannot reset it to the profile root.
- Status: merged into local main at `6b5a65d`; the protected branch rejected push, so no production pull, install, restart, employee-visible message, or model canary has occurred.
