"""Figma connector — status, OAuth start/complete, config injection, redaction.

No network: the authorization server, the registration endpoint and the Figma MCP
endpoint are all served by an in-process mock transport, and the post-auth
``whoami`` verification is injected.

The hermes-agent core is also faked. Some venvs still carry hermes-agent 0.14.0,
whose ``tools.mcp_oauth`` predates Figma support, so a test that imported the real
core would pass or skip depending on which tree it ran in. The fake reproduces the
core's on-disk layout exactly, and ``test_token_file_matches_core_storage_layout``
pins that layout against whatever real core IS importable.
"""
from __future__ import annotations

import json
import os
import sys
import time
import types
from pathlib import Path

import pytest
import yaml


# --- fake hermes-agent core --------------------------------------------------


def _build_fake_core() -> types.ModuleType:
    from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken

    module = types.ModuleType("tools.mcp_oauth")

    class FakeHermesTokenStorage:
        """Byte-compatible stand-in for the core's per-profile token storage."""

        def __init__(self, server_name: str, *, hermes_home=None):
            self._server_name = server_name
            self._home = Path(hermes_home)

        def _dir(self) -> Path:
            return self._home / "mcp-tokens"

        def _tokens_path(self) -> Path:
            return self._dir() / f"{self._server_name}.json"

        def _client_info_path(self) -> Path:
            return self._dir() / f"{self._server_name}.client.json"

        def _meta_path(self) -> Path:
            return self._dir() / f"{self._server_name}.meta.json"

        def _write(self, path: Path, payload: dict) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

        def _read(self, path: Path):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None

        async def get_tokens(self):
            data = self._read(self._tokens_path())
            if data is None:
                return None
            data.pop("expires_at", None)
            return OAuthToken.model_validate(data)

        async def set_tokens(self, tokens) -> None:
            payload = tokens.model_dump(mode="json", exclude_none=True)
            if payload.get("expires_in") is not None:
                payload["expires_at"] = time.time() + int(payload["expires_in"])
            self._write(self._tokens_path(), payload)

        async def get_client_info(self):
            data = self._read(self._client_info_path())
            if data is None:
                return None
            return OAuthClientInformationFull.model_validate(data)

        async def set_client_info(self, client_info) -> None:
            self._write(self._client_info_path(), client_info.model_dump(mode="json", exclude_none=True))

        def save_oauth_metadata(self, metadata) -> None:
            self._write(self._meta_path(), metadata.model_dump(mode="json", exclude_none=True))

        def load_oauth_metadata(self):
            data = self._read(self._meta_path())
            return OAuthMetadata.model_validate(data) if data else None

        def remove(self) -> None:
            for path in (self._tokens_path(), self._client_info_path(), self._meta_path()):
                path.unlink(missing_ok=True)

    def apply_oauth_provider_defaults(cfg: dict, *, server_name: str = "", server_url=None) -> dict:
        if "figma" in str(server_url or "").lower() or "figma" in str(server_name or "").lower():
            cfg.setdefault("client_name", "Claude Code")
            cfg.setdefault("scope", "mcp:connect")
            cfg.setdefault("token_endpoint_auth_method", "client_secret_post")
        return cfg

    module.HermesTokenStorage = FakeHermesTokenStorage
    module.apply_oauth_provider_defaults = apply_oauth_provider_defaults
    return module


@pytest.fixture
def fake_core(monkeypatch):
    module = _build_fake_core()
    tools_pkg = sys.modules.get("tools")
    if tools_pkg is None:
        tools_pkg = types.ModuleType("tools")
        tools_pkg.__path__ = []  # namespace-ish package so submodule import resolves
        monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.mcp_oauth", module)
    monkeypatch.setattr(tools_pkg, "mcp_oauth", module, raising=False)
    return module


# --- mock Figma authorization server + MCP endpoint --------------------------

AS_ISSUER = "https://api.figma.com"
MCP_URL = "https://mcp.figma.com/mcp"


class FigmaMock:
    """Serves PRM, AS metadata, DCR, the token endpoint and the MCP endpoint."""

    def __init__(self, *, register_status: int = 200, token_status: int = 200, token_body: dict | None = None):
        self.register_status = register_status
        self.token_status = token_status
        #: Overrides the 200 token payload — used to reproduce a malformed
        #: response whose body still carries a refresh token.
        self.token_body = token_body
        self.registered: list[dict] = []
        self.token_requests: list[dict] = []
        self.initialize_calls = 0

    def handler(self, request):
        http = _http_module()
        url = request.url
        host, path = url.host, url.path
        if host == "mcp.figma.com":
            if "well-known" in path:
                return http.Response(200, json=self._prm())
            return self._mcp(request, http)
        if host == "api.figma.com":
            if "well-known" in path:
                return http.Response(200, json=self._as_metadata())
            if path.endswith("/register"):
                return self._register(request, http)
            if path.endswith("/token"):
                return self._token(request, http)
        return http.Response(404, json={"error": "not_found", "path": path})

    def _prm(self) -> dict:
        return {
            "resource": MCP_URL,
            "authorization_servers": [AS_ISSUER],
            "scopes_supported": ["mcp:connect"],
        }

    def _as_metadata(self) -> dict:
        return {
            "issuer": AS_ISSUER,
            "authorization_endpoint": "https://www.figma.com/oauth/mcp",
            "token_endpoint": f"{AS_ISSUER}/v1/oauth/token",
            "registration_endpoint": f"{AS_ISSUER}/v1/oauth/mcp/register",
            "scopes_supported": ["mcp:connect"],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
            # Figma claims iss support and then never sends it — the exact
            # mismatch fill_figma_iss exists for.
            "authorization_response_iss_parameter_supported": True,
            "require_state_parameter": True,
        }

    def _register(self, request, http):
        body = json.loads(request.content.decode("utf-8") or "{}")
        self.registered.append(body)
        if self.register_status != 200:
            return http.Response(self.register_status, json={"error": "forbidden"})
        # Figma's real allowlist behaviour: any other client_name is refused.
        if body.get("client_name") not in ("Claude Code", "Codex"):
            return http.Response(403, json={"error": "forbidden"})
        return http.Response(
            201,
            json={
                **body,
                "client_id": "figma-client-id",
                "client_secret": "figma-client-secret",
                "client_id_issued_at": int(time.time()),
            },
        )

    def _token(self, request, http):
        self.token_requests.append(_form(request.content.decode("utf-8")))
        if self.token_status != 200:
            return http.Response(self.token_status, json={"error": "invalid_grant"})
        if self.token_body is not None:
            return http.Response(200, json=self.token_body)
        return http.Response(
            200,
            json={
                "access_token": "figma-access-token-SECRET",
                "refresh_token": "figma-refresh-token-SECRET",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "mcp:connect",
            },
        )

    def _mcp(self, request, http):
        if not request.headers.get("Authorization"):
            return http.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata='
                    '"https://mcp.figma.com/.well-known/oauth-protected-resource/mcp"'
                },
                json={"error": "unauthorized"},
            )
        self.initialize_calls += 1
        return http.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "figma", "version": "1"},
                },
            },
        )


def _form(raw: str) -> dict:
    from urllib.parse import parse_qs

    return {k: v[0] for k, v in parse_qs(raw).items()}


def _http_module():
    from hermes_multitenancy import figma_connector

    return figma_connector._sdk()[1]


def _mock_transport(mock: FigmaMock):
    return _http_module().MockTransport(mock.handler)


# --- helpers -----------------------------------------------------------------


def _mk_profile(tmp_path: Path, name: str = "owner") -> tuple[Path, Path]:
    home = tmp_path / "profiles" / name
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"model": {"default": "zai/glm-5.1"}}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return tmp_path, home


def _plant_tokens(home: Path, *, expires_in: int = 3600, refresh: bool = True) -> None:
    path = home / "mcp-tokens" / "figma.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "access_token": "planted-access-SECRET",
        "token_type": "Bearer",
        "expires_in": expires_in,
        "expires_at": time.time() + expires_in,
        "scope": "mcp:connect",
    }
    if refresh:
        payload["refresh_token"] = "planted-refresh-SECRET"
    path.write_text(json.dumps(payload), encoding="utf-8")


# --- layout pin --------------------------------------------------------------


def test_token_file_matches_core_storage_layout(tmp_path):
    """Our synchronous path must agree with the real core about where tokens live."""
    from hermes_multitenancy import figma_connector

    if not figma_connector.core_available():
        pytest.skip("no Figma-capable hermes-agent core importable in this environment")
    import tools.mcp_oauth as core  # noqa: PLC0415

    storage = core.HermesTokenStorage(figma_connector.MCP_SERVER_KEY, hermes_home=str(tmp_path))
    assert figma_connector.token_file(tmp_path) == storage._tokens_path()


def test_token_and_config_paths_are_profile_relative():
    """Always-on layout pin, for trees where no capable core is importable."""
    from hermes_multitenancy import figma_connector

    home = Path("/srv/profiles/alice")
    assert figma_connector.token_file(home) == home / "mcp-tokens" / "figma.json"
    assert figma_connector.account_file(home) == home / "connectors" / "figma.json"
    assert figma_connector.config_file(home) == home / "config.yaml"
    assert figma_connector.MCP_SERVER_KEY == "figma"


def test_profile_home_is_the_directory_multitenancy_pins_hermes_home_to():
    from hermes_multitenancy import figma_connector

    assert figma_connector.profile_home("/srv", "alice") == Path("/srv").resolve() / "profiles" / "alice"
    for bad in ("", ".", "..", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            figma_connector.profile_home("/srv", bad)


# --- status ------------------------------------------------------------------


def test_status_needs_auth_without_tokens(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, _home = _mk_profile(tmp_path)
    status = figma_connector.status(shared, "owner", "ou_owner")
    assert status.id == "figma"
    assert status.status == "needs_auth"
    assert status.installed is True
    assert status.action is not None and status.action.kind == "oauth_url"
    assert status.action.label == "授权"
    assert status.credential_owner == "owner"
    assert status.expires_at is None


def test_status_authenticated_reports_expiry_and_hint(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    _plant_tokens(home)
    (home / "connectors").mkdir(parents=True, exist_ok=True)
    (home / "connectors" / "figma.json").write_text(
        json.dumps({"account_hint": "su…@example.com"}), encoding="utf-8"
    )
    status = figma_connector.status(shared, "owner", "ou_owner")
    assert status.status == "authenticated"
    assert status.account_hint == "su…@example.com"
    assert status.expires_at is not None and status.expires_at > int(time.time() * 1000)
    assert status.action is not None and status.action.label == "重新授权"


def test_status_expired_without_refresh_token_is_needs_auth(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    _plant_tokens(home, expires_in=-60, refresh=False)
    status = figma_connector.status(shared, "owner", "ou_owner")
    assert status.status == "needs_auth"
    assert "过期" in (status.detail or "")


def test_status_expired_with_refresh_token_stays_authenticated(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    _plant_tokens(home, expires_in=-60, refresh=True)
    assert figma_connector.status(shared, "owner", "ou_owner").status == "authenticated"


def test_status_missing_when_core_cannot_do_mcp_oauth(tmp_path, monkeypatch):
    from hermes_multitenancy import figma_connector

    shared, _home = _mk_profile(tmp_path)
    monkeypatch.setattr(figma_connector, "core_available", lambda: False)
    status = figma_connector.status(shared, "owner", "ou_owner")
    assert status.status == "missing"
    assert status.installed is False
    assert status.action is None  # nothing the employee can do about it


def test_status_isolation_between_profiles(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, home_a = _mk_profile(tmp_path, "alice")
    _mk_profile(tmp_path, "bob")
    _plant_tokens(home_a)
    assert figma_connector.status(shared, "alice", "ou_a").status == "authenticated"
    assert figma_connector.status(shared, "bob", "ou_b").status == "needs_auth"
    assert not (tmp_path / "profiles" / "bob" / "mcp-tokens" / "figma.json").exists()


def test_status_never_serializes_a_secret(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    _plant_tokens(home)
    blob = json.dumps(figma_connector.status(shared, "owner", "ou_owner").to_dict(), ensure_ascii=False)
    for secret in ("planted-access-SECRET", "planted-refresh-SECRET", "SECRET"):
        assert secret not in blob


def test_status_rejects_a_traversing_profile_name(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, _home = _mk_profile(tmp_path)
    status = figma_connector.status(shared, "../escape", "ou_owner")
    assert status.status == "error"


# --- redirect URI ------------------------------------------------------------


def test_redirect_uri_requires_https_or_loopback(monkeypatch):
    from hermes_multitenancy import figma_connector

    monkeypatch.delenv(figma_connector.PUBLIC_ORIGIN_ENV, raising=False)
    assert figma_connector.resolve_redirect_uri("https://hermes.example.com").endswith(
        "/api/auth/skill-credentials/catalog/oauth/callback"
    )
    assert figma_connector.resolve_redirect_uri("http://127.0.0.1:8649").startswith("http://127.0.0.1:8649/")
    assert figma_connector.resolve_redirect_uri("http://localhost:8649").startswith("http://localhost:8649/")
    with pytest.raises(figma_connector.ConnectorUnavailable):
        figma_connector.resolve_redirect_uri("http://hermes.example.com")
    with pytest.raises(figma_connector.ConnectorUnavailable):
        figma_connector.resolve_redirect_uri("https://hermes.example.com/some/path")


def test_redirect_uri_unset_names_the_env_var(monkeypatch):
    from hermes_multitenancy import figma_connector

    monkeypatch.delenv(figma_connector.PUBLIC_ORIGIN_ENV, raising=False)
    with pytest.raises(figma_connector.ConnectorUnavailable) as excinfo:
        figma_connector.resolve_redirect_uri()
    assert figma_connector.PUBLIC_ORIGIN_ENV in str(excinfo.value)


def test_redirect_uri_reads_the_environment(monkeypatch):
    from hermes_multitenancy import figma_connector

    monkeypatch.setenv(figma_connector.PUBLIC_ORIGIN_ENV, "https://hermes.example.com/")
    assert figma_connector.resolve_redirect_uri() == (
        "https://hermes.example.com/api/auth/skill-credentials/catalog/oauth/callback"
    )


# --- RFC 9207 iss fill -------------------------------------------------------


class _Meta:
    def __init__(self, issuer):
        self.issuer = issuer


def test_fill_iss_only_for_figma_issuer():
    from hermes_multitenancy import figma_connector

    assert figma_connector.fill_figma_iss(None, _Meta("https://api.figma.com")) == "https://api.figma.com"
    assert figma_connector.fill_figma_iss(None, _Meta("https://api.figma.com/")) == "https://api.figma.com"
    assert figma_connector.fill_figma_iss(None, _Meta("https://evil.example")) is None
    assert figma_connector.fill_figma_iss(None, None) is None
    # A server that does send iss keeps its own value, always.
    assert figma_connector.fill_figma_iss("https://other.example", _Meta("https://api.figma.com")) == (
        "https://other.example"
    )


def test_callback_result_carries_iss_on_sdks_that_validate_it():
    from hermes_multitenancy import figma_connector

    oauth2, _http = figma_connector._sdk()
    result = figma_connector._callback_result(oauth2, "code-1", "state-1", "https://api.figma.com")
    if hasattr(oauth2, "AuthorizationCodeResult"):
        assert result.code == "code-1"
        assert result.state == "state-1"
        assert result.iss == "https://api.figma.com"
    else:
        assert result == ("code-1", "state-1")


# --- profile config ----------------------------------------------------------


def test_inject_profile_config_is_idempotent_and_preserves_other_keys(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    _shared, home = _mk_profile(tmp_path)
    assert figma_connector.inject_profile_config(home) is True
    assert figma_connector.inject_profile_config(home) is False
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert config["model"]["default"] == "zai/glm-5.1"
    assert config["mcp_servers"]["figma"] == {
        "url": "https://mcp.figma.com/mcp",
        "auth": "oauth",
        "enabled": True,
    }


def test_inject_profile_config_keeps_sibling_mcp_servers(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    _shared, home = _mk_profile(tmp_path)
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    config["mcp_servers"] = {"hermes-studio": {"command": "python3", "args": ["-m", "x"]}}
    (home / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    figma_connector.inject_profile_config(home)
    after = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert "hermes-studio" in after["mcp_servers"]
    assert "figma" in after["mcp_servers"]
    figma_connector.remove_profile_config(home)
    final = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert "figma" not in final["mcp_servers"]
    assert "hermes-studio" in final["mcp_servers"]


def test_inject_profile_config_refuses_a_foreign_figma_server(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    _shared, home = _mk_profile(tmp_path)
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    config["mcp_servers"] = {"figma": {"command": "npx", "args": ["figma-bridge"]}}
    (home / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    with pytest.raises(figma_connector.ConnectorUnavailable):
        figma_connector.inject_profile_config(home)


def test_remove_profile_config_is_idempotent(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    _shared, home = _mk_profile(tmp_path)
    assert figma_connector.remove_profile_config(home) is False
    figma_connector.inject_profile_config(home)
    assert figma_connector.remove_profile_config(home) is True
    assert figma_connector.remove_profile_config(home) is False


# --- account hint ------------------------------------------------------------


def test_account_hint_extraction_and_redaction():
    from hermes_multitenancy import figma_connector

    payload = {"content": [{"type": "text", "text": json.dumps({"email": "sunke@example.com"})}]}
    hint = figma_connector.extract_account_hint(payload)
    assert hint == "su…@example.com"
    assert "sunke@example.com" != hint
    assert figma_connector.extract_account_hint({"nothing": 1}) == ""


# --- OAuth start / complete --------------------------------------------------


async def _start(broker, profile_name="owner"):
    return await broker.start(profile_name, public_origin="https://hermes.example.com")


def _broker(tmp_path, mock, *, verify=None):
    from hermes_multitenancy import figma_connector

    async def default_verify(home):
        return {"content": [{"type": "text", "text": json.dumps({"email": "sunke@example.com"})}]}

    return figma_connector.FigmaOAuthBroker(
        tmp_path,
        transport=_mock_transport(mock),
        verify=verify or default_verify,
    )


async def test_start_registers_as_claude_code_and_returns_authorization_url(tmp_path, fake_core):
    shared, _home = _mk_profile(tmp_path)
    mock = FigmaMock()
    broker = _broker(shared, mock)
    started = await _start(broker)

    assert started["authorization_url"].startswith("https://www.figma.com/oauth/mcp?")
    assert "code_challenge=" in started["authorization_url"]
    assert "state=" in started["authorization_url"]
    assert started["state"] and broker.has_pending(started["state"])
    assert mock.registered and mock.registered[0]["client_name"] == "Claude Code"
    assert mock.registered[0]["scope"] == "mcp:connect"
    assert mock.registered[0]["token_endpoint_auth_method"] == "client_secret_post"
    assert mock.registered[0]["redirect_uris"] == [
        "https://hermes.example.com/api/auth/skill-credentials/catalog/oauth/callback"
    ]


async def test_start_honours_the_client_name_override(tmp_path, fake_core, monkeypatch):
    from hermes_multitenancy import figma_connector

    shared, _home = _mk_profile(tmp_path)
    monkeypatch.setenv(figma_connector.CLIENT_NAME_ENV, "Codex")
    mock = FigmaMock()
    await _start(_broker(shared, mock))
    assert mock.registered[0]["client_name"] == "Codex"


async def test_start_surfaces_a_registration_refusal(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, _home = _mk_profile(tmp_path)
    broker = _broker(shared, FigmaMock(register_status=403))
    with pytest.raises(Exception) as excinfo:
        await _start(broker)
    assert not isinstance(excinfo.value, AssertionError)
    # Nothing is left behind for a flow that never produced a URL.
    assert not (tmp_path / "profiles" / "owner" / "mcp-tokens" / "figma.json").exists()


async def test_start_requires_a_public_origin(tmp_path, fake_core, monkeypatch):
    from hermes_multitenancy import figma_connector

    shared, _home = _mk_profile(tmp_path)
    monkeypatch.delenv(figma_connector.PUBLIC_ORIGIN_ENV, raising=False)
    broker = _broker(shared, FigmaMock())
    with pytest.raises(figma_connector.ConnectorUnavailable):
        await broker.start("owner")


async def test_start_rejects_an_unknown_profile(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    _mk_profile(tmp_path)
    broker = _broker(tmp_path, FigmaMock())
    with pytest.raises(figma_connector.ConnectorUnavailable):
        await broker.start("nobody", public_origin="https://hermes.example.com")


async def test_complete_persists_tokens_verifies_and_injects_config(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    mock = FigmaMock()
    seen: list[Path] = []

    async def verify(profile_home_arg):
        seen.append(profile_home_arg)
        return {"content": [{"type": "text", "text": json.dumps({"email": "sunke@example.com"})}]}

    broker = _broker(shared, mock, verify=verify)
    started = await _start(broker)
    result = await broker.complete(started["state"], "auth-code-1")

    assert result["status"] == "authenticated"
    assert result["account_hint"] == "su…@example.com"
    assert seen == [home]

    tokens = json.loads((home / "mcp-tokens" / "figma.json").read_text(encoding="utf-8"))
    assert tokens["access_token"] == "figma-access-token-SECRET"
    assert tokens["refresh_token"] == "figma-refresh-token-SECRET"

    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert config["mcp_servers"]["figma"]["url"] == "https://mcp.figma.com/mcp"
    assert config["mcp_servers"]["figma"]["auth"] == "oauth"

    assert figma_connector.status(shared, "owner", "ou_owner").status == "authenticated"
    # PKCE actually happened, and the code we handed in is the one exchanged.
    exchange = mock.token_requests[-1]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code"] == "auth-code-1"
    assert exchange.get("code_verifier")
    assert not broker.has_pending(started["state"])


async def test_without_the_iss_fill_an_rfc9207_sdk_rejects_the_callback(tmp_path, fake_core, monkeypatch):
    """The fill is load-bearing, not defensive.

    On an SDK that enforces RFC 9207 (mcp >= 2.0) the mock server reproduces
    Figma's behaviour — advertise iss support, then omit iss — so neutering the
    fill must break the exchange. On an older SDK there is nothing to enforce and
    the flow still succeeds; the assertion follows the SDK rather than pinning one.
    """
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    oauth2, _http = figma_connector._sdk()
    enforces_iss = hasattr(oauth2, "AuthorizationCodeResult")

    monkeypatch.setattr(figma_connector, "fill_figma_iss", lambda iss, _metadata: iss)
    broker = _broker(shared, FigmaMock())
    started = await _start(broker)

    if enforces_iss:
        with pytest.raises(Exception):
            await broker.complete(started["state"], "auth-code-1")
        assert not (home / "mcp-tokens" / "figma.json").exists()
    else:
        result = await broker.complete(started["state"], "auth-code-1")
        assert result["status"] == "authenticated"


async def test_start_reuses_a_client_registered_for_the_same_redirect(tmp_path, fake_core):
    shared, _home = _mk_profile(tmp_path)
    mock = FigmaMock()
    broker = _broker(shared, mock)
    await _start(broker)
    assert len(mock.registered) == 1
    broker.pending.clear()
    await _start(broker)
    # Same origin: the stored registration is still valid, so no second DCR.
    assert len(mock.registered) == 1


async def test_start_reregisters_when_the_public_origin_changed(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    mock = FigmaMock()
    broker = _broker(shared, mock)
    await _start(broker)
    assert figma_connector.client_redirect_uris(home) == [
        "https://hermes.example.com/api/auth/skill-credentials/catalog/oauth/callback"
    ]
    broker.pending.clear()
    await broker.start("owner", public_origin="http://127.0.0.1:8886")
    assert len(mock.registered) == 2
    assert mock.registered[-1]["redirect_uris"] == [
        "http://127.0.0.1:8886/api/auth/skill-credentials/catalog/oauth/callback"
    ]


async def test_origin_change_does_not_destroy_a_valid_token(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    broker = _broker(shared, FigmaMock())
    started = await _start(broker)
    await broker.complete(started["state"], "auth-code-1")
    assert (home / "mcp-tokens" / "figma.json").exists()

    assert figma_connector._drop_client_if_redirect_changed(home, "https://elsewhere.example/cb") is True
    assert not (home / "mcp-tokens" / "figma.client.json").exists()
    # The token survived, so the employee is not logged out by an origin change.
    assert (home / "mcp-tokens" / "figma.json").exists()
    assert figma_connector.status(shared, "owner", "ou_owner").status == "authenticated"


async def test_reauthorization_works_on_an_already_authenticated_profile(tmp_path, fake_core):
    """Regression: 重新授权 must produce a URL instead of failing.

    Observed live 2026-09-21: with a valid token on disk the probe request to
    mcp.figma.com returned 200 rather than 401, so the SDK never started an
    authorization flow and start() raised "未能生成授权链接". That is exactly the
    path the card's 重新授权 button takes, and the only way to switch a profile
    from a wrong Figma account to the right one.
    """
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    mock = FigmaMock()
    broker = _broker(shared, mock)

    first = await _start(broker)
    await broker.complete(first["state"], "auth-code-1")
    assert figma_connector.status(shared, "owner", "ou_owner").status == "authenticated"
    registrations = len(mock.registered)

    # Re-authorize while authenticated: must hand back a real URL.
    broker.pending.clear()
    second = await _start(broker)
    assert second["authorization_url"].startswith("https://www.figma.com/oauth/mcp?")
    assert second["state"] and second["state"] != first["state"]
    # The grant is cleared while the flow is open, so the card reads needs_auth.
    assert not (home / "mcp-tokens" / "figma.json").exists()
    assert figma_connector.status(shared, "owner", "ou_owner").status == "needs_auth"
    # The registration is reused, not thrown away.
    assert (home / "mcp-tokens" / "figma.client.json").exists()
    assert len(mock.registered) == registrations
    # And the config block does not dangle without a token.
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert "figma" not in (config.get("mcp_servers") or {})

    await broker.complete(second["state"], "auth-code-2")
    assert figma_connector.status(shared, "owner", "ou_owner").status == "authenticated"
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert config["mcp_servers"]["figma"]["url"] == "https://mcp.figma.com/mcp"


def test_clear_grant_for_reauthorization_is_a_noop_without_a_grant(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    _shared, home = _mk_profile(tmp_path)
    assert figma_connector._clear_grant_for_reauthorization(home) is False


async def test_complete_is_single_use(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, _home = _mk_profile(tmp_path)
    broker = _broker(shared, FigmaMock())
    started = await _start(broker)
    await broker.complete(started["state"], "auth-code-1")
    with pytest.raises(figma_connector.ConnectorUnavailable):
        await broker.complete(started["state"], "auth-code-1")


async def test_complete_rejects_an_unknown_state(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, _home = _mk_profile(tmp_path)
    broker = _broker(shared, FigmaMock())
    await _start(broker)
    assert broker.has_pending("not-a-real-state") is False
    with pytest.raises(figma_connector.ConnectorUnavailable):
        await broker.complete("not-a-real-state", "auth-code-1")


async def test_pending_flow_expires(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, _home = _mk_profile(tmp_path)
    broker = _broker(shared, FigmaMock())
    broker.flow_timeout = 0.05
    started = await _start(broker)
    import asyncio

    await asyncio.sleep(0.2)
    assert broker.has_pending(started["state"]) is False
    with pytest.raises(figma_connector.ConnectorUnavailable):
        await broker.complete(started["state"], "auth-code-1")


async def test_failed_verification_purges_tokens_and_leaves_needs_auth(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)

    async def verify(_home):
        raise RuntimeError("whoami refused")

    broker = _broker(shared, FigmaMock(), verify=verify)
    started = await _start(broker)
    with pytest.raises(Exception):
        await broker.complete(started["state"], "auth-code-1")

    assert not (home / "mcp-tokens" / "figma.json").exists()
    # The DCR registration deliberately SURVIVES a verification failure; only an
    # invalid_client response drops it (test_invalid_client_does_drop_the_registration).
    assert (home / "mcp-tokens" / "figma.client.json").exists()
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert "figma" not in (config.get("mcp_servers") or {})
    assert figma_connector.status(shared, "owner", "ou_owner").status == "needs_auth"


async def test_failed_token_exchange_purges_tokens(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    broker = _broker(shared, FigmaMock(token_status=400))
    started = await _start(broker)
    with pytest.raises(Exception):
        await broker.complete(started["state"], "auth-code-1")
    assert not (home / "mcp-tokens" / "figma.json").exists()
    assert figma_connector.status(shared, "owner", "ou_owner").status == "needs_auth"


async def test_invalid_grant_keeps_the_client_registration(tmp_path, fake_core):
    """A stale or replayed code must not cost us the DCR registration.

    Regression: the first cut purged every figma.* file on ANY flow failure, so
    one expired authorization code threw away a client_id that Figma only hands
    out through an allowlisted registration endpoint (observed live 2026-09-21).
    """
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    mock = FigmaMock(token_status=400)  # Figma answers invalid_grant
    broker = _broker(shared, mock)
    started = await _start(broker)
    assert (home / "mcp-tokens" / "figma.client.json").exists()

    with pytest.raises(Exception):
        await broker.complete(started["state"], "stale-code")

    assert (home / "mcp-tokens" / "figma.client.json").exists(), "registration must survive invalid_grant"
    assert not (home / "mcp-tokens" / "figma.json").exists()
    assert figma_connector.status(shared, "owner", "ou_owner").status == "needs_auth"

    # And the retry reuses it rather than registering a second client.
    registrations_before = len(mock.registered)
    mock.token_status = 200
    broker.pending.clear()
    retried = await _start(broker)
    assert len(mock.registered) == registrations_before
    result = await broker.complete(retried["state"], "fresh-code")
    assert result["status"] == "authenticated"


async def test_failed_verification_keeps_the_client_registration(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)

    async def verify(_home):
        raise RuntimeError("whoami refused")

    broker = _broker(shared, FigmaMock(), verify=verify)
    started = await _start(broker)
    with pytest.raises(Exception):
        await broker.complete(started["state"], "auth-code-1")
    # The client is fine; the grant was not. Keep the registration.
    assert (home / "mcp-tokens" / "figma.client.json").exists()
    assert not (home / "mcp-tokens" / "figma.json").exists()


async def test_invalid_client_does_drop_the_registration(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)

    async def verify(_home):
        raise RuntimeError("Token exchange failed (401): invalid_client")

    broker = _broker(shared, FigmaMock(), verify=verify)
    started = await _start(broker)
    with pytest.raises(Exception):
        await broker.complete(started["state"], "auth-code-1")
    assert not (home / "mcp-tokens" / "figma.client.json").exists()
    assert not (home / "mcp-tokens" / "figma.json").exists()


def test_figma_is_exempt_from_the_headless_cli_gate_but_clis_are_not():
    """Regression: the figma row must not trip require_registered_oauth_cli_gates.

    Observed live 2026-09-21: adding the connector made _build_subprocess_env
    raise "OAuth connector 'figma' has no headless gate", which knocked the
    streaming AIAgent path into its legacy fallback. The agent then ran WITHOUT
    the profile's MCP servers and truthfully answered that it had no figma
    tools — a silent capability loss that no unit test covered.
    """
    from hermes_multitenancy.connectors.builtin import BUILTIN_CONNECTORS
    from hermes_multitenancy.oauth_cli_guard import (
        require_registered_oauth_cli_gates,
        requires_headless_cli_gate,
    )

    # The whole builtin table must pass the fail-closed guard.
    require_registered_oauth_cli_gates(BUILTIN_CONNECTORS)

    # figma is exempt because it fronts a remote endpoint, not a binary.
    assert requires_headless_cli_gate(BUILTIN_CONNECTORS["figma"]) is False
    # the CLI-backed OAuth connectors are still gated.
    for cid in ("lark-cli", "feishu-project", "kep-cli-online", "kep-cli-pre"):
        assert requires_headless_cli_gate(BUILTIN_CONNECTORS[cid]) is True, cid


def test_an_unmapped_oauth_cli_still_fails_closed():
    """The exemption is for remote endpoints only; a new CLI must still be gated."""
    from dataclasses import replace

    from hermes_multitenancy.connectors.builtin import BUILTIN_CONNECTORS
    from hermes_multitenancy.connectors.models import InvocationSpec
    from hermes_multitenancy.oauth_cli_guard import require_registered_oauth_cli_gates

    rogue = replace(
        BUILTIN_CONNECTORS["kep-cli-online"],
        id="rogue-cli",
        invocation=InvocationSpec(type="cli_command", detail="rogue-auth"),
    )
    with pytest.raises(RuntimeError, match="no headless gate"):
        require_registered_oauth_cli_gates({"rogue-cli": rogue})


def test_invalid_client_error_classifier():
    from hermes_multitenancy import figma_connector

    assert figma_connector.is_invalid_client_error("Token exchange failed (401): invalid_client")
    assert figma_connector.is_invalid_client_error(RuntimeError("unauthorized_client"))
    assert not figma_connector.is_invalid_client_error("Token exchange failed (400): invalid_grant")
    assert not figma_connector.is_invalid_client_error(RuntimeError("whoami refused"))


def test_revoke_still_drops_everything_including_the_registration(tmp_path, fake_core):
    """revoke() is the deliberate full wipe; only the failure path is selective."""
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    _plant_tokens(home)
    (home / "mcp-tokens" / "figma.client.json").write_text("{}", encoding="utf-8")
    (home / "mcp-tokens" / "figma.meta.json").write_text("{}", encoding="utf-8")

    assert figma_connector.revoke(shared, "owner") is True
    for suffix in (".json", ".client.json", ".meta.json"):
        assert not (home / "mcp-tokens" / f"figma{suffix}").exists(), suffix


async def test_two_profiles_do_not_share_tokens(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, home_a = _mk_profile(tmp_path, "alice")
    _, home_b = _mk_profile(tmp_path, "bob")
    broker = _broker(shared, FigmaMock())
    started = await broker.start("alice", public_origin="https://hermes.example.com")
    await broker.complete(started["state"], "auth-code-1")

    assert (home_a / "mcp-tokens" / "figma.json").exists()
    assert not (home_b / "mcp-tokens" / "figma.json").exists()
    assert figma_connector.status(shared, "bob", "ou_b").status == "needs_auth"


# --- revoke ------------------------------------------------------------------


def test_revoke_removes_tokens_hint_and_config(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    _plant_tokens(home)
    (home / "mcp-tokens" / "figma.client.json").write_text("{}", encoding="utf-8")
    (home / "connectors").mkdir(parents=True, exist_ok=True)
    (home / "connectors" / "figma.json").write_text(json.dumps({"account_hint": "x"}), encoding="utf-8")
    figma_connector.inject_profile_config(home)

    assert figma_connector.revoke(shared, "owner") is True
    assert not (home / "mcp-tokens" / "figma.json").exists()
    assert not (home / "mcp-tokens" / "figma.client.json").exists()
    assert not (home / "connectors" / "figma.json").exists()
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert "figma" not in (config.get("mcp_servers") or {})
    assert figma_connector.status(shared, "owner", "ou_owner").status == "needs_auth"
    assert figma_connector.revoke(shared, "owner") is False


# --- broker registry ---------------------------------------------------------


def test_get_broker_is_shared_per_shared_home(tmp_path):
    from hermes_multitenancy import figma_connector

    figma_connector.reset_brokers()
    try:
        first = figma_connector.get_broker(tmp_path)
        assert figma_connector.get_broker(tmp_path) is first
        other = tmp_path / "other"
        other.mkdir()
        assert figma_connector.get_broker(other) is not first
    finally:
        figma_connector.reset_brokers()


# =============================================================================
# Regressions for the 2026-09-21 cross-family (codex) review — one section per
# finding. Every test here fails on the pre-review implementation.
# =============================================================================


# --- connector_catalog_api.py:complete_catalog_oauth:issuer-callback-field-rejected

async def test_callback_route_accepts_the_iss_figma_actually_sends(tmp_path, fake_core, monkeypatch):
    """The public callback must take an optional ``iss`` for Figma states.

    Figma's redirect DOES carry ``iss`` (confirmed live 2026-09-21) and mcp >= 2
    validates it per RFC 9207. The route used to require the body to be exactly
    {state, code}, so every real Figma callback 400'd before it ever reached the
    broker. Non-Figma catalog callbacks keep the exact two-field contract.
    """
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import figma_connector
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared, home = _mk_profile(tmp_path)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    monkeypatch.setenv("HERMES_CREDENTIAL_KEY", "test-key")
    monkeypatch.setenv(figma_connector.PUBLIC_ORIGIN_ENV, "https://hermes.example.com")
    oauth2, _http = figma_connector._sdk()
    mock = FigmaMock()
    forwarded: list = []

    class _Recording(figma_connector.FigmaOAuthBroker):
        async def complete(self, state, code, *, iss=None):
            forwarded.append(iss)
            return await super().complete(state, code, iss=iss)

    async def verify(_home):
        return {"content": [{"type": "text", "text": json.dumps({"email": "sunke@example.com"})}]}

    broker = _Recording(shared, transport=_mock_transport(mock), verify=verify)
    figma_connector.reset_brokers()
    figma_connector._brokers[Path(shared).resolve()] = broker

    client = TestClient(TestServer(create_run_broker_app(mark_seen=lambda _r: True, sandbox_available=lambda: True)))
    await client.start_server()
    route = "/api/run-broker/connector-catalog/oauth/callback"
    try:
        # 1. the real Figma shape: state + code + iss
        first = await _start(broker)
        ok = await client.post(route, json={
            "state": first["state"], "code": "auth-code-1", "iss": "https://api.figma.com",
        })
        assert ok.status == 200, await ok.text()
        assert (await ok.json())["connector"]["status"] == "authenticated"
        assert forwarded == ["https://api.figma.com"]
        assert (home / "mcp-tokens" / "figma.json").exists()

        # 2. iss absent — filled from metadata, exactly as before
        figma_connector.revoke(shared, "owner")
        second = await _start(broker)
        ok = await client.post(route, json={"state": second["state"], "code": "auth-code-2"})
        assert ok.status == 200, await ok.text()
        assert forwarded[-1] is None

        # 3. a wrong issuer is forwarded verbatim and rejected by the SDK that
        #    enforces RFC 9207 — never silently accepted here.
        figma_connector.revoke(shared, "owner")
        third = await _start(broker)
        wrong = await client.post(route, json={
            "state": third["state"], "code": "auth-code-3", "iss": "https://evil.example",
        })
        assert forwarded[-1] == "https://evil.example"
        if hasattr(oauth2, "AuthorizationCodeResult"):
            assert wrong.status == 502
            assert not (home / "mcp-tokens" / "figma.json").exists()
        else:
            assert wrong.status == 200

        # 4. a non-string iss is refused before anything is dispatched
        figma_connector.revoke(shared, "owner")
        fourth = await _start(broker)
        bad = await client.post(route, json={"state": fourth["state"], "code": "c", "iss": 7})
        assert bad.status == 400
        assert broker.has_pending(fourth["state"]) is True

        # 5. a non-Figma state still has to be exactly {state, code}
        stray = await client.post(route, json={"state": "catalog-state", "code": "c", "iss": "https://x"})
        assert stray.status == 400
    finally:
        await client.close()
        figma_connector.reset_brokers()


# --- figma_connector.py:figmaoauthbroker:profile-flow-races

async def test_revoke_cancels_a_pending_flow_so_a_stale_callback_cannot_reauthorize(tmp_path, fake_core):
    """撤销 while the Figma tab is still open must win permanently.

    Without a per-profile flow registry the revoke had nothing to cancel, so the
    callback the employee's browser posted afterwards re-authorized the profile:
    tokens back on disk and mcp_servers.figma back in config.yaml.
    """
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    broker = _broker(shared, FigmaMock())
    started = await _start(broker)
    assert broker.is_active("owner") is True

    assert await broker.cancel_profile("owner") is True
    assert figma_connector.revoke(shared, "owner") is False
    assert broker.is_active("owner") is False
    assert broker.has_pending(started["state"]) is False

    with pytest.raises(figma_connector.ConnectorUnavailable):
        await broker.complete(started["state"], "auth-code-1")

    assert not (home / "mcp-tokens" / "figma.json").exists()
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert "figma" not in (config.get("mcp_servers") or {})
    assert figma_connector.status(shared, "owner", "ou_owner").status == "needs_auth"


async def test_a_superseded_flow_cannot_delete_the_winning_flows_token(tmp_path, fake_core):
    """Two 授权 clicks: the second supersedes the first, and the first is dead.

    Previously both flows stayed live under their own state, so when the
    abandoned one finally failed its rollback purged the token the successful
    one had just written.
    """
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    mock = FigmaMock()
    broker = _broker(shared, mock)

    first = await _start(broker)
    second = await _start(broker)
    assert second["state"] != first["state"]
    assert broker.has_pending(first["state"]) is False
    assert broker.has_pending(second["state"]) is True

    assert (await broker.complete(second["state"], "code-2"))["status"] == "authenticated"
    token = (home / "mcp-tokens" / "figma.json").read_text(encoding="utf-8")

    # The abandoned tab comes back with a code Figma now refuses.
    mock.token_status = 400
    with pytest.raises(figma_connector.ConnectorUnavailable):
        await broker.complete(first["state"], "code-1")

    assert (home / "mcp-tokens" / "figma.json").read_text(encoding="utf-8") == token
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert config["mcp_servers"]["figma"]["url"] == "https://mcp.figma.com/mcp"
    assert figma_connector.status(shared, "owner", "ou_owner").status == "authenticated"


# --- figma_connector.py:figmaoauthbroker.complete:timeout-leaves-live-flow

async def test_complete_timeout_kills_the_flow_instead_of_leaving_it_running(tmp_path, fake_core):
    """A timed-out callback must not inject config minutes later.

    ``wait_for(shield(task))`` left the task alive: the employee was told the
    authorization failed, and the flow then finished on its own and wrote both
    the token and mcp_servers.figma.
    """
    import asyncio

    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)

    async def slow_verify(_home):
        await asyncio.sleep(1.0)
        return {"content": [{"type": "text", "text": json.dumps({"email": "sunke@example.com"})}]}

    broker = _broker(shared, FigmaMock(), verify=slow_verify)
    broker.complete_timeout = 0.05
    started = await _start(broker)

    with pytest.raises(figma_connector.ConnectorUnavailable):
        await broker.complete(started["state"], "auth-code-1")

    await asyncio.sleep(0.3)  # well past the point the old flow would have finished
    assert not (home / "mcp-tokens" / "figma.json").exists()
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert "figma" not in (config.get("mcp_servers") or {})
    assert not (home / "connectors" / "figma.json").exists()
    assert figma_connector.status(shared, "owner", "ou_owner").status == "needs_auth"
    assert broker.pending == {}
    assert broker.is_active("owner") is False
    # The registration survives — the client was never the problem.
    assert (home / "mcp-tokens" / "figma.client.json").exists()


# --- figma_connector.py:verify_whoami:tool-errors-accepted

def _whoami_result(*, is_error: bool, text: str):
    from mcp.types import CallToolResult, TextContent

    return CallToolResult(content=[TextContent(type="text", text=text)], isError=is_error)


def _patch_whoami_session(monkeypatch, result):
    """Drive the real ``verify_whoami`` against a scripted MCP session."""
    import contextlib

    import mcp
    import mcp.client.streamable_http as streamable

    class _Session:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments):
            assert name == "whoami" and arguments == {}
            return result

    @contextlib.asynccontextmanager
    async def _streams(_url, **_kwargs):
        yield (None, None, None)

    monkeypatch.setattr(mcp, "ClientSession", _Session)
    monkeypatch.setattr(streamable, "streamable_http_client", _streams)


async def test_verify_whoami_refuses_a_tool_error_result(tmp_path, fake_core, monkeypatch):
    """``isError=True`` is a normal MCP response, not an exception.

    ``call_tool`` returns it happily, so a whoami that failed on permissions or
    a service error used to be read as proof of a working, correctly-owned
    token.
    """
    from hermes_multitenancy import figma_connector

    _shared, home = _mk_profile(tmp_path)
    _plant_tokens(home)
    _patch_whoami_session(monkeypatch, _whoami_result(is_error=True, text="permission denied for figma-access-token-SECRET"))

    with pytest.raises(figma_connector.ConnectorUnavailable) as excinfo:
        await figma_connector.verify_whoami(home)
    assert "SECRET" not in str(excinfo.value)


async def test_verify_whoami_refuses_a_result_without_an_identity(tmp_path, fake_core, monkeypatch):
    from hermes_multitenancy import figma_connector

    _shared, home = _mk_profile(tmp_path)
    _plant_tokens(home)
    _patch_whoami_session(monkeypatch, _whoami_result(is_error=False, text="ok"))
    with pytest.raises(figma_connector.ConnectorUnavailable):
        await figma_connector.verify_whoami(home)


async def test_verify_whoami_accepts_a_real_identity(tmp_path, fake_core, monkeypatch):
    from hermes_multitenancy import figma_connector

    _shared, home = _mk_profile(tmp_path)
    _plant_tokens(home)
    _patch_whoami_session(
        monkeypatch,
        _whoami_result(is_error=False, text=json.dumps({"email": "sunke@example.com"})),
    )
    payload = await figma_connector.verify_whoami(home)
    assert figma_connector.extract_account_hint(payload) == "su…@example.com"


async def test_a_whoami_tool_error_fails_the_whole_authorization(tmp_path, fake_core, monkeypatch):
    """End to end through the default verifier: the callback must NOT report ok."""
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    _patch_whoami_session(monkeypatch, _whoami_result(is_error=True, text="insufficient scope"))
    # No verify= override: this is the production path, verify_whoami.
    broker = figma_connector.FigmaOAuthBroker(shared, transport=_mock_transport(FigmaMock()))
    started = await _start(broker)

    with pytest.raises(figma_connector.ConnectorUnavailable):
        await broker.complete(started["state"], "auth-code-1")

    assert not (home / "mcp-tokens" / "figma.json").exists()
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert "figma" not in (config.get("mcp_servers") or {})
    assert figma_connector.status(shared, "owner", "ou_owner").status == "needs_auth"


# --- figma_connector.py:remove_profile_config:purges-foreign-server

def _plant_foreign_figma_server(home: Path) -> dict:
    """A user's own stdio figma bridge, sitting on the key we want."""
    foreign = {"command": "npx", "args": ["figma-bridge"], "enabled": True}
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    config["mcp_servers"] = {"figma": foreign}
    (home / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return foreign


def test_remove_profile_config_leaves_a_server_it_does_not_own(tmp_path, fake_core):
    from hermes_multitenancy import figma_connector

    _shared, home = _mk_profile(tmp_path)
    foreign = _plant_foreign_figma_server(home)
    assert figma_connector.remove_profile_config(home) is False
    after = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert after["mcp_servers"]["figma"] == foreign


def test_revoke_never_deletes_a_foreign_figma_server(tmp_path, fake_core):
    """撤销 must clear OUR grant without touching someone else's figma entry."""
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    _plant_tokens(home)
    foreign = _plant_foreign_figma_server(home)

    assert figma_connector.revoke(shared, "owner") is True
    assert not (home / "mcp-tokens" / "figma.json").exists()
    after = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert after["mcp_servers"]["figma"] == foreign


async def test_a_conflicting_figma_server_fails_before_the_browser_flow(tmp_path, fake_core):
    """The conflict check runs before any side effect, including DCR.

    It used to fire only at injection time, i.e. after the employee had already
    granted access at Figma and a token existed.
    """
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    _plant_foreign_figma_server(home)
    mock = FigmaMock()
    broker = _broker(shared, mock)

    with pytest.raises(figma_connector.ConnectorUnavailable):
        await _start(broker)

    assert mock.registered == []          # Figma never saw a registration
    assert not (home / "mcp-tokens").exists()
    assert broker.is_active("owner") is False


async def test_a_failed_flow_rolls_back_without_deleting_a_foreign_server(tmp_path, fake_core):
    """The rollback after a failed authorization must not purge foreign config."""
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    broker = _broker(shared, FigmaMock(token_status=400))
    started = await _start(broker)
    foreign = _plant_foreign_figma_server(home)   # the bridge is added mid-flow

    with pytest.raises(Exception):
        await broker.complete(started["state"], "auth-code-1")

    after = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert after["mcp_servers"]["figma"] == foreign
    assert not (home / "mcp-tokens" / "figma.json").exists()


# --- figma_connector.py:_purge:revoke-errors-swallowed

def test_revoke_reports_a_storage_failure_instead_of_success(tmp_path, fake_core, monkeypatch):
    """A revoke that could not delete the token must not answer ok.

    The exception from ``storage.remove()`` was swallowed and revoke still
    reported True off a stat taken *before* the removal, so the card went back
    to 未认证 while the token stayed on disk and the agent kept loading it.
    """
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    _plant_tokens(home)
    figma_connector.inject_profile_config(home)

    class _ReadOnlyStorage:
        def remove(self):
            raise PermissionError("Read-only file system")

    monkeypatch.setattr(figma_connector, "_token_storage", lambda _home: _ReadOnlyStorage())

    with pytest.raises(figma_connector.ConnectorUnavailable) as excinfo:
        figma_connector.revoke(shared, "owner")
    assert (home / "mcp-tokens" / "figma.json").exists()
    assert str(home) not in str(excinfo.value)
    assert "Read-only" not in str(excinfo.value)
    assert figma_connector.status(shared, "owner", "ou_owner").status == "authenticated"


def test_revoke_fails_when_the_token_survives_a_silent_removal(tmp_path, fake_core, monkeypatch):
    """Success is proved by the files being gone, not by remove() returning."""
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    _plant_tokens(home)

    class _NoOpStorage:
        def remove(self):
            return None

    monkeypatch.setattr(figma_connector, "_token_storage", lambda _home: _NoOpStorage())
    with pytest.raises(figma_connector.ConnectorUnavailable):
        figma_connector.revoke(shared, "owner")
    assert (home / "mcp-tokens" / "figma.json").exists()


def test_revoke_fails_when_our_config_block_cannot_be_removed(tmp_path, fake_core, monkeypatch):
    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    _plant_tokens(home)
    figma_connector.inject_profile_config(home)
    monkeypatch.setattr(figma_connector, "remove_profile_config", lambda _home: False)

    with pytest.raises(figma_connector.ConnectorUnavailable):
        figma_connector.revoke(shared, "owner")


# --- figma_connector.py:_run_flow:raw-sdk-errors-leak-tokens

def test_redact_secrets_masks_values_not_keys():
    from hermes_multitenancy import figma_connector

    masked = figma_connector.redact_secrets(
        '{"access_token": "at-SECRET", "refresh_token":"rt-SECRET"} client_secret=cs-SECRET&x=1'
    )
    assert "SECRET" not in masked
    assert masked.count("<redacted>") == 3
    assert "x=1" in masked


def test_sdk_oauth_logger_carries_the_redacting_filter(fake_core, caplog):
    import logging

    from hermes_multitenancy import figma_connector

    oauth2, _http = figma_connector._sdk()
    with caplog.at_level(logging.DEBUG, logger=oauth2.logger.name):
        oauth2.logger.error('token response {"refresh_token": "rt-SECRET"}')
    assert "SECRET" not in caplog.text
    assert "<redacted>" in caplog.text


async def test_a_malformed_token_response_never_reaches_logs_or_errors(tmp_path, fake_core, caplog):
    """The raw SDK provider logged and raised the token payload.

    Reproduction from the review: a 200 token response missing ``access_token``
    but carrying a ``refresh_token``. pydantic embeds the rejected input in its
    ValidationError, the SDK does ``logger.exception("OAuth flow error")``, and
    both the log and the exception chain ended up holding the secret.
    """
    import logging

    from hermes_multitenancy import figma_connector

    shared, home = _mk_profile(tmp_path)
    # Small body on purpose: pydantic elides the middle of a long ``input_value``
    # repr, and a shortened leak is still a leak — this one shows the whole value.
    mock = FigmaMock(token_body={"refresh_token": "rt-figma-SECRET"})
    broker = _broker(shared, mock)
    started = await _start(broker)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception) as excinfo:
            await broker.complete(started["state"], "auth-code-1")

    chain, seen = [], excinfo.value
    while seen is not None and len(chain) < 20:
        chain.append(f"{type(seen).__name__}: {seen}")
        seen = seen.__cause__ or seen.__context__
    joined = "\n".join(chain)
    assert "SECRET" not in joined, joined
    assert "SECRET" not in caplog.text
    assert not (home / "mcp-tokens" / "figma.json").exists()
