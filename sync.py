"""Route-sync helper for Hermes directory-plugin installs.

When installed with ``hermes plugins install``, this repository is cloned into
``~/.hermes/plugins/multitenancy`` rather than installed as a Python package.
This wrapper makes route sync runnable without modifying PYTHONPATH:

    python ~/.hermes/plugins/multitenancy/sync.py apply users.json
"""

from __future__ import annotations

import sys
import os
from pathlib import Path

plugin_root = Path(__file__).resolve().parent
sys.path.insert(0, str(plugin_root))

hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
shared_home = hermes_home.parent.parent if hermes_home.parent.name == "profiles" else hermes_home
agent_checkout = shared_home / "hermes-agent"
if agent_checkout.exists():
    sys.path.insert(0, str(agent_checkout))

from hermes_multitenancy.sync.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
