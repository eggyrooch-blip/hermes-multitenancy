"""Hermes directory-plugin entry point.

This root shim lets ``hermes plugins install owner/repo`` load the cloned
repository directly from ``~/.hermes/plugins/multitenancy``. The import points
at the package implementation used by editable/pip installs and tests.
"""

try:
    from .hermes_multitenancy import on_pre_gateway_dispatch, register
except ImportError:  # pytest imports repo-root __init__.py as a top-level module.
    from hermes_multitenancy import on_pre_gateway_dispatch, register

__all__ = ["register", "on_pre_gateway_dispatch"]
