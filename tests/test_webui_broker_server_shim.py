"""The webui_broker_server facade must expose the same surface regardless of import order."""
from __future__ import annotations

import subprocess
import sys

_PROBE = """
import hermes_multitenancy.webui_broker.periphery as periphery
import hermes_multitenancy.webui_broker_server as m

assert m.credential_broker_url is periphery.credential_broker_url, "credential_broker_url"
assert m._webui_streamable_media_text is periphery._webui_streamable_media_text, "_webui_streamable_media_text"

# The facade's OWN functions read re-exported names as bare globals, which the
# module __getattr__ hook cannot intercept — this raises NameError if the early
# snapshot was never retaken.
m.create_run_broker_app()
print("ok")
"""


def test_facade_resolves_periphery_names_when_periphery_is_imported_first() -> None:
    """periphery imported FIRST leaves the facade's bulk re-export snapshot incomplete."""
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout
