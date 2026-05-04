"""Route-sync helper for Hermes directory-plugin installs.

When installed with ``hermes plugins install``, this repository is cloned into
``~/.hermes/plugins/multitenancy`` rather than installed as a Python package.
This wrapper makes route sync runnable without modifying PYTHONPATH:

    python ~/.hermes/plugins/multitenancy/sync.py apply users.json
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hermes_multitenancy.sync.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
