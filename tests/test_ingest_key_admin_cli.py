from __future__ import annotations

import json
import stat
import tomllib
import urllib.error
from io import BytesIO
from pathlib import Path


def _read_keys(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["keys"]


def test_grant_creates_key_file_and_masks_token_by_default(tmp_path, capsys):
    from hermes_multitenancy.ingest_key_admin_cli import main

    key_file = tmp_path / "ingest-keys.json"

    rc = main(
        [
            "grant",
            "--keys-file",
            str(key_file),
            "--owner",
            "ou_owner",
            "--profile",
            "daryu-agent",
            "--agent",
            "d3d59ea6ed55",
            "--name",
            "daryu broadcast",
            "--token",
            "hm-ingest-secret-token",
        ]
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "hm-ingest-secret-token" not in out
    assert "hm-i...oken" in out
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert _read_keys(key_file) == [
        {
            "token": "hm-ingest-secret-token",
            "owner": "ou_owner",
            "profile": "daryu-agent",
            "agent": "d3d59ea6ed55",
            "name": "daryu broadcast",
        }
    ]


def test_list_outputs_masked_json_without_full_token(tmp_path, capsys):
    from hermes_multitenancy.ingest_key_admin_cli import main

    key_file = tmp_path / "ingest-keys.json"
    key_file.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "token": "hm-ingest-secret-token",
                        "owner": "ou_owner",
                        "profile": "daryu-agent",
                        "agent": "d3d59ea6ed55",
                        "name": "daryu broadcast",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    rc = main(["list", "--keys-file", str(key_file), "--format", "json"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "hm-ingest-secret-token" not in out
    rows = json.loads(out)
    assert rows == [
        {
            "token": "hm-i...oken",
            "owner": "ou_owner",
            "profile": "daryu-agent",
            "agent": "d3d59ea6ed55",
            "name": "daryu broadcast",
            "source": "file",
            "index": 0,
        }
    ]


def test_list_default_table_includes_source_without_full_token(tmp_path, capsys):
    from hermes_multitenancy.ingest_key_admin_cli import main

    key_file = tmp_path / "ingest-keys.json"
    key_file.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "token": "hm-ingest-secret-token",
                        "owner": "ou_owner",
                        "profile": "daryu-agent",
                        "agent": "d3d59ea6ed55",
                        "name": "daryu broadcast",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    rc = main(["list", "--keys-file", str(key_file)])

    out = capsys.readouterr().out
    assert rc == 0
    assert "hm-ingest-secret-token" not in out
    assert "token=hm-i...oken" in out
    assert "source=file" in out


def test_rotate_replaces_selected_token_without_printing_secret(tmp_path, capsys):
    from hermes_multitenancy.ingest_key_admin_cli import main

    key_file = tmp_path / "ingest-keys.json"
    key_file.write_text(
        json.dumps(
            {
                "keys": [
                    {"token": "keep-token", "owner": "ou_other", "profile": "other-agent", "agent": "agent-other"},
                    {
                        "token": "old-secret-token",
                        "owner": "ou_owner",
                        "profile": "daryu-agent",
                        "agent": "d3d59ea6ed55",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "rotate",
            "--keys-file",
            str(key_file),
            "--profile",
            "daryu-agent",
            "--agent",
            "d3d59ea6ed55",
            "--token",
            "new-secret-token",
        ]
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "new-secret-token" not in out
    assert "old-secret-token" not in out
    keys = _read_keys(key_file)
    assert keys[0]["token"] == "keep-token"
    assert keys[1]["token"] == "new-secret-token"


def test_revoke_removes_only_selected_binding(tmp_path, capsys):
    from hermes_multitenancy.ingest_key_admin_cli import main

    key_file = tmp_path / "ingest-keys.json"
    key_file.write_text(
        json.dumps(
            {
                "keys": [
                    {"token": "keep-token", "owner": "ou_other", "profile": "other-agent", "agent": "agent-other"},
                    {
                        "token": "old-secret-token",
                        "owner": "ou_owner",
                        "profile": "daryu-agent",
                        "agent": "d3d59ea6ed55",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "revoke",
            "--keys-file",
            str(key_file),
            "--profile",
            "daryu-agent",
            "--agent",
            "d3d59ea6ed55",
        ]
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "old-secret-token" not in out
    assert _read_keys(key_file) == [
        {"token": "keep-token", "owner": "ou_other", "profile": "other-agent", "agent": "agent-other"}
    ]


def test_smoke_calls_ingest_agents_with_bearer_and_masks_output(monkeypatch, capsys):
    from hermes_multitenancy import ingest_key_admin_cli

    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"ok":true,"owner":"ou_owner","agents":[{"id":"d3d59ea6ed55","name":"daryu"}]}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.headers["Authorization"]
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(ingest_key_admin_cli.urllib.request, "urlopen", fake_urlopen)

    rc = ingest_key_admin_cli.main(
        [
            "smoke",
            "--base-url",
            "https://hermes.example",
            "--token",
            "smoke-secret-token",
            "--timeout",
            "9",
        ]
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert captured == {
        "url": "https://hermes.example/api/run-broker/ingest/agents",
        "authorization": "Bearer smoke-secret-token",
        "timeout": 9.0,
    }
    assert "smoke-secret-token" not in out
    assert "agents=1" in out
    assert "owner=ou_owner" in out


def test_smoke_http_error_redacts_echoed_token(monkeypatch, capsys):
    from hermes_multitenancy import ingest_key_admin_cli

    def fake_urlopen(_request, timeout):
        assert timeout == 15.0
        raise urllib.error.HTTPError(
            url="https://hermes.example/api/run-broker/ingest/agents",
            code=401,
            msg="unauthorized",
            hdrs=None,
            fp=BytesIO(b'{"error":"bad bearer smoke-secret-token"}'),
        )

    monkeypatch.setattr(ingest_key_admin_cli.urllib.request, "urlopen", fake_urlopen)

    rc = ingest_key_admin_cli.main(
        [
            "smoke",
            "--base-url",
            "https://hermes.example",
            "--token",
            "smoke-secret-token",
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "smoke-secret-token" not in err
    assert "smok...oken" in err


def test_pyproject_exposes_ingest_cli_script():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert (
        pyproject["project"]["scripts"]["hermes-multitenancy-ingest"]
        == "hermes_multitenancy.ingest_key_admin_cli:main"
    )
