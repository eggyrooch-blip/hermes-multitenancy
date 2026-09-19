"""Phase A key-resolution fix.

`_run_with_aiagent` resolved the LLM api_key via `_resolve_api_key`, which only
checks env vars + auth.json inline tokens. After migrating every profile's model
to the litellm.sre gateway via a `custom_providers:` entry (key stored inline in
config.yaml; auth.json's credential_pool holds only an encrypted-vault fingerprint,
not an inline access_token), that resolver returned None and EVERY turn raised
"no API key for primary provider 'custom:litellm-sre'". The fix adds
`_resolve_custom_provider_api_key` as a fallback that reads the inline key.
"""
import hermes_multitenancy.agent_real as ar


def _cfg():
    return {
        "model": {
            "default": "custom:litellm-sre/tencent-sonnet-4-6",
            "provider": "custom:litellm-sre",
            "base_url": "https://litellm.sre.example.com/v1",
        },
        "custom_providers": [
            {
                "name": "litellm-sre",
                "base_url": "https://litellm.sre.example.com/v1",
                "api_key": "sk-test-key",
                "model": "tencent-sonnet-4-6",
            }
        ],
    }


def test_resolves_inline_key_by_slug():
    assert ar._resolve_custom_provider_api_key(_cfg(), "custom:litellm-sre") == "sk-test-key"


def test_resolves_inline_key_by_base_url_when_provider_bare_custom():
    # provider has no ":<name>" suffix → fall back to base_url match
    assert ar._resolve_custom_provider_api_key(_cfg(), "custom") == "sk-test-key"


def test_non_custom_provider_returns_none():
    assert ar._resolve_custom_provider_api_key(_cfg(), "anthropic") is None


def test_no_custom_providers_returns_none():
    assert ar._resolve_custom_provider_api_key({"model": {}}, "custom:x") is None


def test_entry_without_key_returns_none():
    cfg = _cfg()
    cfg["custom_providers"][0].pop("api_key")
    assert ar._resolve_custom_provider_api_key(cfg, "custom:litellm-sre") is None


def test_resolves_key_env_from_profile_environment():
    cfg = _cfg()
    cfg["custom_providers"][0].pop("api_key")
    cfg["custom_providers"][0]["key_env"] = "ZAI_API_KEY"

    assert ar._resolve_custom_provider_api_key(
        cfg, "custom:litellm-sre", {"ZAI_API_KEY": "zai-test-key"}
    ) == "zai-test-key"


def test_custom_key_env_does_not_read_untrusted_ambient_environment(monkeypatch):
    cfg = _cfg()
    cfg["custom_providers"][0].pop("api_key")
    cfg["custom_providers"][0]["key_env"] = "FEISHU_APP_SECRET"
    monkeypatch.setenv("FEISHU_APP_SECRET", "must-not-leave-host")

    assert ar._resolve_custom_provider_api_key(
        cfg, "custom:litellm-sre", {}
    ) is None


def test_named_custom_provider_resolves_its_registered_base_url():
    cfg = _cfg()
    cfg["model"].pop("base_url")

    assert ar._resolve_base_url(
        "custom:litellm-sre", True, cfg, {}
    ) == "https://litellm.sre.example.com/v1"


def test_slug_name_normalized_with_spaces():
    cfg = _cfg()
    cfg["custom_providers"][0]["name"] = "Lite LLM SRE"  # normalizes to "lite-llm-sre"
    assert ar._resolve_custom_provider_api_key(cfg, "custom:lite-llm-sre") == "sk-test-key"


def test_slug_picks_correct_entry_among_multiple():
    cfg = _cfg()
    cfg["custom_providers"].append(
        {"name": "other-gw", "base_url": "https://other.example/v1", "api_key": "sk-OTHER"}
    )
    # slug match must return litellm-sre's key, not the other entry's
    assert ar._resolve_custom_provider_api_key(cfg, "custom:litellm-sre") == "sk-test-key"
    assert ar._resolve_custom_provider_api_key(cfg, "custom:other-gw") == "sk-OTHER"


def test_customai_lookalike_not_matched_without_config():
    # a provider whose name merely starts with "custom" must not false-match
    assert ar._resolve_custom_provider_api_key(_cfg(), "customai") is None
