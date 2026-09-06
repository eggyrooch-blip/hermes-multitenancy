"""Deploy smoke target — importing this must exercise the whole lazy contract.

``hermes_multitenancy/__init__`` resolves everything lazily so that importing
one submodule does not drag 187 files into every test's import closure. That
makes ``python -c "import hermes_multitenancy"`` a vacuous wheel check: it
succeeds even if half the plugin is missing from the image. Deploy/bundle
smokes import THIS instead.

Resolving every mapped name is what makes the check real — it loads
``plugin_entry`` and the six formerly-eager submodules, AND asserts each mapped
symbol still exists, so a dropped file or a renamed ``_register`` fails the
build instead of failing at Hermes startup with ``SystemExit(1)``.

Not imported by anything at runtime.
"""
from __future__ import annotations

import sys

_pkg = sys.modules[__name__.rsplit(".", 1)[0]]

# getattr, not import: an unresolvable name must raise here, in the smoke.
for _name in (*_pkg._LAZY_ATTRS, *_pkg._LAZY_SUBMODULES, "register", "__all__"):
    getattr(_pkg, _name)

del _name
