from __future__ import annotations

import sys as _sys
_pkg = _sys.modules[__package__]

import json
import logging
import os
import sys
import time
import hashlib
import tempfile
import uuid
import re
import secrets
import importlib
import threading
from contextlib import closing, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional


class _CredentialExpirySignal:
    """Thread-safe one-shot holder for a lark-cli credential-expiry signal.

    The lark auth sidecar records into this from its http.server handler
    thread while the agent subprocess runs; the async run scope reads it
    afterwards. Lives per ``stream_run_agent`` call via a ContextVar, so
    there is no cross-run bleed.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: Optional[dict] = None

    def set(self, payload: dict) -> None:
        with self._lock:
            if self._value is None:
                self._value = dict(payload or {})

    def get(self) -> Optional[dict]:
        with self._lock:
            return dict(self._value) if self._value is not None else None
