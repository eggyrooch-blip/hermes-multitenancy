"""Exercise the installed upstream cache, including a fresh worker context."""
from concurrent.futures import ThreadPoolExecutor


def test_official_home_cache_survives_registration_and_isolates_workers(tmp_path, monkeypatch):
    from tools import env_passthrough
    from hermes_multitenancy.agent_real._core import _register_env_passthrough_process_wide

    monkeypatch.setattr(env_passthrough, '_config_passthrough', {})
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'profile-a'))
    _register_env_passthrough_process_wide(['TEST_OWNER_TOKEN', 'OPENAI_API_KEY'])
    with ThreadPoolExecutor(max_workers=1) as pool:
        allowed = pool.submit(env_passthrough.get_all_passthrough).result()
        assert 'TEST_OWNER_TOKEN' in allowed
        assert 'OPENAI_API_KEY' not in allowed
        monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'profile-b'))
        assert 'TEST_OWNER_TOKEN' not in pool.submit(env_passthrough.get_all_passthrough).result()
    assert isinstance(env_passthrough._config_passthrough, dict)
