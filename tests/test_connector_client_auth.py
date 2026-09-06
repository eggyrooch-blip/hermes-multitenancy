import asyncio
from pathlib import Path

import pytest


def test_client_token_is_owner_bound_persistent_and_revocable(tmp_path: Path):
    from hermes_multitenancy.connector_client_auth import ClientTokenStore
    from hermes_multitenancy.trusted_runtime_principal import issue_webui_principal

    db_path = tmp_path / "multitenancy.db"
    principal = issue_webui_principal(
        profile_name="alice",
        actor_subject="subject-alice",
        credential_subject="subject-alice",
    )

    store = ClientTokenStore(
        db_path,
        issuer="http://127.0.0.1:8767",
        resource="http://127.0.0.1:8767/mcp",
    )
    token = store.mint(
        principal=principal,
        client_id="cursor-local",
        scopes=["mcp:tools"],
        ttl_seconds=300,
    )
    store.close()

    assert token not in db_path.read_text(encoding="utf-8", errors="ignore")

    reopened = ClientTokenStore(
        db_path,
        issuer="http://127.0.0.1:8767",
        resource="http://127.0.0.1:8767/mcp",
    )
    access = asyncio.run(reopened.verify_token(token))
    assert access is not None
    assert access.client_id == "cursor-local"
    assert access.subject == "subject-alice"
    assert access.resource == "http://127.0.0.1:8767/mcp"
    assert access.scopes == ["mcp:tools"]
    assert access.claims == {"iss": "http://127.0.0.1:8767", "profile": "alice"}

    assert reopened.revoke(token) is True
    assert asyncio.run(reopened.verify_token(token)) is None
    assert reopened.revoke(token) is False
    reopened.close()


def test_client_tokens_fail_closed_across_owner_audience_expiry_and_forged_principal(tmp_path: Path):
    import sqlite3

    from hermes_multitenancy.connector_client_auth import ClientTokenStore
    from hermes_multitenancy.trusted_runtime_principal import (
        TrustedRuntimePrincipal,
        issue_webui_principal,
    )

    db_path = tmp_path / "multitenancy.db"
    issuer = "http://127.0.0.1:8767"
    resource = f"{issuer}/mcp"
    store = ClientTokenStore(db_path, issuer=issuer, resource=resource)
    alice = store.mint(
        principal=issue_webui_principal(
            profile_name="alice",
            actor_subject="subject-alice",
            credential_subject="subject-alice",
        ),
        client_id="client-a",
        scopes=["mcp:tools"],
    )
    bob = store.mint(
        principal=issue_webui_principal(
            profile_name="bob",
            actor_subject="subject-bob",
            credential_subject="subject-bob",
        ),
        client_id="client-b",
        scopes=["mcp:tools"],
    )
    alice_access = asyncio.run(store.verify_token(alice))
    bob_access = asyncio.run(store.verify_token(bob))
    assert (alice_access.claims["profile"], alice_access.subject) == ("alice", "subject-alice")
    assert (bob_access.claims["profile"], bob_access.subject) == ("bob", "subject-bob")
    with pytest.raises(PermissionError):
        store.mint(
            principal=TrustedRuntimePrincipal(
                channel="webui",
                profile_name="alice",
                actor_subject="subject-alice",
                credential_subject="subject-alice",
            ),
            client_id="forged",
            scopes=["mcp:tools"],
        )
    store.close()

    wrong_audience = ClientTokenStore(
        db_path,
        issuer=issuer,
        resource=f"{issuer}/different-mcp",
    )
    assert asyncio.run(wrong_audience.verify_token(alice)) is None
    wrong_audience.close()

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE multitenancy_mcp_client_tokens SET expires_at=0 WHERE client_id='client-b'"
        )
    reopened = ClientTokenStore(db_path, issuer=issuer, resource=resource)
    assert asyncio.run(reopened.verify_token(bob)) is None
    reopened.close()


def test_mint_rejects_unknown_scopes_and_purges_old_tokens(tmp_path: Path):
    import sqlite3
    import time

    from mcp.server.auth.provider import TokenError

    from hermes_multitenancy.connector_client_auth import ClientTokenStore, HermesOAuthProvider
    from hermes_multitenancy.trusted_runtime_principal import issue_webui_principal

    db_path = tmp_path / "multitenancy.db"
    store = ClientTokenStore(
        db_path,
        issuer="http://127.0.0.1:8767",
        resource="http://127.0.0.1:8767/mcp",
    )
    principal = issue_webui_principal(
        profile_name="alice",
        actor_subject="subject-alice",
        credential_subject="subject-alice",
    )
    with pytest.raises(ValueError, match="scope"):
        store.mint(principal=principal, client_id="bad", scopes=["mcp:admin"])
    provider = HermesOAuthProvider(store)
    with pytest.raises(TokenError, match="scope"):
        provider._issue_pair(
            client_id="stale-grant",
            profile_name="alice",
            subject_id="subject-alice",
            scopes=["mcp:admin"],
        )
    provider.close()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO multitenancy_mcp_client_tokens
            (token_sha256, client_id, profile_name, subject_id, scopes_json,
             audience, expires_at, revoked_at, created_at)
            VALUES ('expired', 'old', 'alice', 'subject-alice', '[\"mcp:tools\"]', ?, ?, NULL, ?)
            """,
            (store.resource, int(time.time()) - 90000, int(time.time()) - 90000),
        )
    store.mint(principal=principal, client_id="good", scopes=["mcp:tools"])
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM multitenancy_mcp_client_tokens WHERE token_sha256='expired'"
        ).fetchone()[0] == 0
    store.close()
