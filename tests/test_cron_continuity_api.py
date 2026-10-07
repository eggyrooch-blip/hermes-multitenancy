from pathlib import Path
import pytest
from hermes_multitenancy import cron_api


def test_create_and_update_preserve_explicit_continuity(monkeypatch, tmp_path: Path):
    home = tmp_path / "profiles" / "alice"
    home.mkdir(parents=True)
    monkeypatch.setattr(cron_api, "profile_home_for", lambda _: home)
    job = cron_api.create_job("alice", "ou_alice", {
        "name": "continuity test", "prompt": "Only new changes", "schedule": "0 * * * *",
        "deliver": "local", "continuity": True,
    })
    assert job["context_from"] == ["self"]
    assert job["owner_open_id"] == "ou_alice"
    disabled = cron_api.update_job("alice", job["id"], {"continuity": False})
    assert not disabled.get("context_from")
    enabled = cron_api.update_job("alice", job["id"], {"context_from": "self"})
    assert enabled["context_from"] == ["self"]


@pytest.mark.parametrize("payload", [
    {"continuity": "true"}, {"context_from": ["another-job"]},
    {"continuity": False, "context_from": ["self"]},
])
def test_invalid_or_cross_job_context_is_rejected(payload):
    with pytest.raises(cron_api.CronApiError):
        cron_api._continuity_fields(payload)
