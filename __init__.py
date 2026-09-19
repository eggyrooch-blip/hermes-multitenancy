"""Hermes directory-plugin entry point.

This root shim lets ``hermes plugins install owner/repo`` load the cloned
repository directly from ``~/.hermes/plugins/multitenancy``. The import points
at the package implementation used by editable/pip installs and tests.
"""

try:
    from .hermes_multitenancy import register
except ImportError:  # pytest imports repo-root __init__.py as a top-level module.
    from hermes_multitenancy import register


def __getattr__(name: str):
    if name != "on_pre_gateway_dispatch":
        raise AttributeError(name)
    try:
        from .hermes_multitenancy import _bootstrap_dispatch
    except ImportError:  # pragma: no cover - top-level pytest compatibility.
        from hermes_multitenancy import _bootstrap_dispatch
    return _bootstrap_dispatch

__all__ = ["register"]
