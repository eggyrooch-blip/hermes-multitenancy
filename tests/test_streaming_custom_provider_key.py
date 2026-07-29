"""Regression: streaming fallback must resolve custom_providers inline keys.

``_stream_loop`` only consulted ``_resolve_api_key`` (env vars + auth.json),
so a ``custom:<name>`` profile whose key lives inline in config.yaml's
``custom_providers`` list had every candidate silently skipped and raised
"streaming exhausted (no usable provider returned content)" — masking the
real upstream error (observed live: LiteLLM 429 for a delisted model name).
``run.py`` already combined both resolvers; streaming.py and _core.py now match.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

import hermes_multitenancy.agent_real as ar
from hermes_multitenancy.agent_real import streaming as streaming_mod


class _Evt:
    text = "hi"


_CONFIG = {
    "model": {
        "default": "custom:lite/tencent/claude-sonnet-5",
        "provider": "custom:lite",
        "base_url": "https://litellm.example/v1",
    },
    "custom_providers": [
        {
            "name": "lite",
            "base_url": "https://litellm.example/v1",
            "api_key": "sk-inline",
        }
    ],
}


def _setup(monkeypatch, tmp_path, client_cls):
    (tmp_path / "auth.json").write_text(json.dumps({"credential_pool": {}}))
    monkeypatch.setattr(ar, "_load_profile_config", lambda home: dict(_CONFIG))
    monkeypatch.setattr(streaming_mod, "_compose_system_text", lambda *a: "system")
    monkeypatch.setattr("openai.AsyncOpenAI", client_cls)


async def _collect(tmp_path):
    out = []
    async for kind, text in ar._stream_loop(_Evt(), tmp_path):
        out.append((kind, text))
    return out


def test_inline_custom_key_surfaces_upstream_error(monkeypatch, tmp_path):
    """auth.json empty, key only inline in custom_providers → upstream 429 must
    surface as "streaming failed; last error", NOT "streaming exhausted"."""
    seen = {}

    class _BoomClient:
        def __init__(self, api_key=None, base_url=None):
            seen["api_key"] = api_key
            seen["base_url"] = base_url
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create)
            )

        async def _create(self, **kw):
            seen["model"] = kw.get("model")
            raise RuntimeError("HTTP 429: No deployments available")

    _setup(monkeypatch, tmp_path, _BoomClient)
    with pytest.raises(RuntimeError, match="streaming failed; last error.*429"):
        asyncio.run(_collect(tmp_path))
    assert seen["api_key"] == "sk-inline"  # inline key was actually used
    assert seen["model"] == "tencent/claude-sonnet-5"


def test_inline_custom_key_streams_content(monkeypatch, tmp_path):
    """Same profile shape, healthy upstream → chunks stream normally."""

    class _OkClient:
        def __init__(self, api_key=None, base_url=None):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create)
            )

        async def _create(self, **kw):
            async def _gen():
                for piece in ("成", "功"):
                    yield SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content=piece, reasoning_content=None
                                )
                            )
                        ]
                    )

            return _gen()

    _setup(monkeypatch, tmp_path, _OkClient)
    assert asyncio.run(_collect(tmp_path)) == [("content", "成"), ("content", "功")]
