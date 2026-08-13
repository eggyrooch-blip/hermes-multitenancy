"""Shared wall-clock budget for test handshakes.

These waits are deadlock watchdogs, not performance assertions: the only thing
they must do is fail eventually instead of hanging the suite forever. Budgets
tuned to a fast laptop turn into random CI reds on a loaded single-runner box
(one interpreter cold start can outlast a 3s budget), so every handshake in the
subprocess/threading tests reads this one number.

ponytail: a plain module constant, not a fixture — the waits live inside plain
helper functions and threads that never see pytest's fixture machinery.
"""
from __future__ import annotations

import os

# Generous on purpose: a real deadlock still fails, just 30s later.
SYNC_TIMEOUT = float(os.environ.get("HERMES_TEST_SYNC_TIMEOUT", "30"))
