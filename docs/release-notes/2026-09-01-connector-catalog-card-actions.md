# Connector catalog card actions 2026-09-01

The frozen Registry now returns one action contract for every source row and a source-selection action for every
canonical row. Eleven `pass` public HTTP/SSE rows and all 112 `needs_auth` remote rows are connectable. The connect
route resolves the exact bundled row server-side and derives the owner from the trusted request. Auth rows use
their exact header schema or standard MCP OAuth discovery, dynamic client registration, PKCE/state and token
exchange. Tokens are encrypted under profile+subject, refresh automatically on expiry, and abandoned callbacks
expire after five minutes and cannot be replayed.

Every new installation remains `active` only during verification: fresh `tools/list` must expose at least one
explicitly read-only tool before promotion to `ready`; any failure removes both installation and credential.
The remaining stdio `pass`, 323 sandbox, 35 incompatible and 160 rejected rows stay unavailable until their
runtime/source requirements are repaired. Production is unchanged.
