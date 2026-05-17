<!-- ftask:managed v1 — auto-generated; edit OUTSIDE this block -->
# Agent rules — hermes-multitenancy (managed by ftask)

- This repo is part of sunke's agent-OS. Agents NEVER run git directly here — use `bun ~/.claude/PAI/TOOLS/ftask.ts`.
- Base branch: `main`. Feature work happens in a `ftask new <slug>` worktree, never on `main` directly.
- Test gate: `ftask ship` runs `pytest -q` (auto-detected) in the rebased worktree and BLOCKS the merge if it fails.
- When you fix a bug found while troubleshooting (a 排障), add a regression test that FAILS without the fix BEFORE `ftask ship`, and record the root cause as one line under "Known gotchas" below.
- Global protocol: `~/.claude/CLAUDE.md` and `~/AGENTS.md` ("AGENT-OS" section). User cheatsheet: `~/code/AGENT-OS.md`.

## Known gotchas
- (root causes from 排障 sessions accrue here so the same bug is never debugged twice)
<!-- /ftask:managed -->
