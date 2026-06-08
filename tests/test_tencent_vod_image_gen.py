from __future__ import annotations

import os
import time
from multiprocessing import Process
from pathlib import Path

import pytest


class FakeVODClient:
    def __init__(self, details):
        self.details = list(details)
        self.create_payloads = []
        self.describe_payloads = []

    def create_aigc_image_task(self, payload):
        self.create_payloads.append(payload)
        return {"TaskId": "vod-task-123", "RequestId": "req-123"}

    def describe_task_detail(self, payload):
        self.describe_payloads.append(payload)
        if self.details:
            return self.details.pop(0)
        return {
            "Status": "FINISH",
            "AigcImageTask": {
                "Status": "SUCCESS",
                "ErrCode": 0,
                "Output": {"FileInfos": [{"FileUrl": "https://example.invalid/fallback.png"}]},
            },
        }


def _success_detail(url="https://example.invalid/generated.png"):
    return {
        "TaskType": "AigcImageTask",
        "Status": "FINISH",
        "AigcImageTask": {
            "Status": "SUCCESS",
            "ErrCode": 0,
            "Message": "",
            "Output": {"FileInfos": [{"FileUrl": url}]},
        },
    }


def _wait_for_file(path: Path, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def _vod_lock_worker(lock_dir: str, marker_dir: str, worker: str) -> None:
    os.environ["VOD_SUBAPP_ID"] = "123456789"
    os.environ["HERMES_VOD_LOCK_DIR"] = lock_dir

    marker_root = Path(marker_dir)

    class BlockingVODClient:
        def create_aigc_image_task(self, payload):
            (marker_root / f"{worker}.entered").write_text(str(time.monotonic()), encoding="utf-8")
            if worker == "first":
                _wait_for_file(marker_root / "release-first", timeout_s=5.0)
            return {"TaskId": f"vod-task-{worker}", "RequestId": f"req-{worker}"}

        def describe_task_detail(self, payload):
            return _success_detail(f"https://example.invalid/{worker}.png")

    from hermes_multitenancy.tencent_vod_image_gen import TencentVODImageGenProvider

    result = TencentVODImageGenProvider(client=BlockingVODClient()).generate("一张图", "landscape")
    assert result["success"] is True
    (marker_root / f"{worker}.done").write_text(str(time.monotonic()), encoding="utf-8")


@pytest.fixture(autouse=True)
def vod_env(monkeypatch):
    monkeypatch.setenv("VOD_SUBAPP_ID", "123456789")
    monkeypatch.delenv("HERMES_VOD_IMAGE_MODEL_OVERRIDE", raising=False)
    monkeypatch.delenv("VOD_POLL_TIMEOUT", raising=False)
    monkeypatch.delenv("VOD_POLL_INTERVAL", raising=False)


def test_model_catalog_contains_expected_vod_models():
    from hermes_multitenancy.tencent_vod_image_gen import (
        DEFAULT_VOD_IMAGE_MODEL,
        VOD_IMAGE_MODELS,
    )

    assert DEFAULT_VOD_IMAGE_MODEL == "gem-3.1"
    assert len(VOD_IMAGE_MODELS) == 17
    assert VOD_IMAGE_MODELS["gem-3.1"].model_name == "GEM"
    assert VOD_IMAGE_MODELS["gem-3.1"].model_version == "3.1"
    assert VOD_IMAGE_MODELS["gpt-image2-high"].model_name == "OG"
    assert VOD_IMAGE_MODELS["gpt-image2-high"].model_version == "image2_high"
    assert VOD_IMAGE_MODELS["hunyuan-3d-panorama"].scene_type == "3d_panorama"


def test_default_gem31_generation_builds_vod_payload(monkeypatch):
    from hermes_multitenancy.tencent_vod_image_gen import TencentVODImageGenProvider

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"image_gen": {"provider": "tencent-vod", "model": "gem-3.1"}},
    )
    client = FakeVODClient([
        {"Status": "PROCESSING"},
        _success_detail("https://example.invalid/gem31.png"),
    ])
    provider = TencentVODImageGenProvider(client=client, sleep_fn=lambda _s: None)

    result = provider.generate("一张未来城市海报", aspect_ratio="portrait")

    assert result["success"] is True
    assert result["image"] == "https://example.invalid/gem31.png"
    assert result["model"] == "gem-3.1"
    payload = client.create_payloads[0]
    assert payload["SubAppId"] == 123456789
    assert payload["ModelName"] == "GEM"
    assert payload["ModelVersion"] == "3.1"
    assert payload["Prompt"] == "一张未来城市海报"
    assert payload["OutputConfig"]["AspectRatio"] == "9:16"
    assert client.describe_payloads == [
        {"TaskId": "vod-task-123", "SubAppId": 123456789},
        {"TaskId": "vod-task-123", "SubAppId": 123456789},
    ]


def test_prompt_model_override_uses_gpt_image2_high_and_cleans_prompt(monkeypatch):
    from hermes_multitenancy.tencent_vod_image_gen import TencentVODImageGenProvider

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"image_gen": {"provider": "tencent-vod", "model": "gem-3.1"}},
    )
    client = FakeVODClient([_success_detail()])
    provider = TencentVODImageGenProvider(client=client)

    result = provider.generate("用 gpt-image2-high 帮我生图：一张未来城市海报", "landscape")

    assert result["success"] is True
    assert result["model"] == "gpt-image2-high"
    assert result["prompt"] == "一张未来城市海报"
    payload = client.create_payloads[0]
    assert payload["ModelName"] == "OG"
    assert payload["ModelVersion"] == "image2_high"
    assert payload["Prompt"] == "一张未来城市海报"
    assert payload["OutputConfig"]["AspectRatio"] == "16:9"


def test_env_model_override_wins_without_changing_config(monkeypatch):
    from hermes_multitenancy.tencent_vod_image_gen import TencentVODImageGenProvider

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"image_gen": {"provider": "tencent-vod", "model": "gem-3.1"}},
    )
    monkeypatch.setenv("HERMES_VOD_IMAGE_MODEL_OVERRIDE", "mj-v7")
    client = FakeVODClient([_success_detail()])
    provider = TencentVODImageGenProvider(client=client)

    result = provider.generate("一张未来城市海报", "square")

    assert result["success"] is True
    assert result["model"] == "mj-v7"
    assert client.create_payloads[0]["ModelName"] == "MJ"
    assert client.create_payloads[0]["ModelVersion"] == "v7"
    assert client.create_payloads[0]["OutputConfig"]["AspectRatio"] == "1:1"


def test_missing_credentials_returns_configuration_error(monkeypatch):
    from hermes_multitenancy.tencent_vod_image_gen import TencentVODImageGenProvider

    monkeypatch.delenv("TENCENTCLOUD_SECRET_ID", raising=False)
    monkeypatch.delenv("TENCENTCLOUD_SECRET_KEY", raising=False)
    monkeypatch.delenv("VOD_SUBAPP_ID", raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"image_gen": {"provider": "tencent-vod", "model": "gem-3.1"}},
    )

    result = TencentVODImageGenProvider().generate("一张图", "landscape")

    assert result["success"] is False
    assert result["error_type"] == "configuration_error"
    assert "TENCENTCLOUD_SECRET_ID" in result["error"]
    assert "TENCENTCLOUD_SECRET_KEY" in result["error"]
    assert "VOD_SUBAPP_ID" in result["error"]


def test_task_failure_returns_provider_error_without_secret(monkeypatch):
    from hermes_multitenancy.tencent_vod_image_gen import TencentVODImageGenProvider

    monkeypatch.setenv("TENCENTCLOUD_SECRET_ID", "secret-id-value")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "secret-key-value")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"image_gen": {"provider": "tencent-vod", "model": "gem-3.1"}},
    )
    client = FakeVODClient([
        {
            "Status": "FINISH",
            "AigcImageTask": {
                "Status": "FAIL",
                "ErrCodeExt": "FailedOperation.BusinessNotOpen",
                "Message": "business not open",
            },
        }
    ])

    result = TencentVODImageGenProvider(client=client).generate("一张图", "landscape")

    assert result["success"] is False
    assert result["error_type"] == "TencentVODProviderError"
    assert "business not open" in result["error"]
    assert "secret-id-value" not in result["error"]
    assert "secret-key-value" not in result["error"]


def test_task_timeout_uses_poll_timeout(monkeypatch):
    from hermes_multitenancy.tencent_vod_image_gen import TencentVODImageGenProvider

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"image_gen": {"provider": "tencent-vod", "model": "gem-3.1"}},
    )
    monkeypatch.setenv("VOD_POLL_TIMEOUT", "2")
    monkeypatch.setenv("VOD_POLL_INTERVAL", "1")
    clock = {"now": 0.0}

    def sleep(seconds):
        clock["now"] += seconds

    client = FakeVODClient([{"Status": "PROCESSING"}] * 10)
    provider = TencentVODImageGenProvider(client=client, sleep_fn=sleep, time_fn=lambda: clock["now"])

    result = provider.generate("一张图", "landscape")

    assert result["success"] is False
    assert result["error_type"] == "timeout"
    assert "Timed out waiting for Tencent VOD task" in result["error"]
    assert len(client.describe_payloads) == 3


def test_is_available_is_true_when_configured_even_before_env(monkeypatch):
    from hermes_multitenancy.tencent_vod_image_gen import TencentVODImageGenProvider

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"image_gen": {"provider": "tencent-vod", "model": "gem-3.1"}},
    )
    monkeypatch.delenv("TENCENTCLOUD_SECRET_ID", raising=False)
    monkeypatch.delenv("TENCENTCLOUD_SECRET_KEY", raising=False)
    monkeypatch.delenv("VOD_SUBAPP_ID", raising=False)

    assert TencentVODImageGenProvider().is_available() is True


def test_static_fake_client_keeps_spec_smoke_script_working(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.tencent_vod_image_gen import TencentVODImageGenProvider

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "image_gen:\n  provider: tencent-vod\n  model: gem-3.1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    provider = TencentVODImageGenProvider(fake_client_result_url="https://example.invalid/static.png")
    result = provider.generate("一张测试图", "landscape")

    assert result["success"] is True
    assert result["image"] == "https://example.invalid/static.png"


def test_generate_serializes_vod_tasks_across_processes(tmp_path: Path):
    lock_dir = tmp_path / "locks"
    markers = tmp_path / "markers"
    lock_dir.mkdir()
    markers.mkdir()

    first = Process(target=_vod_lock_worker, args=(str(lock_dir), str(markers), "first"))
    second = Process(target=_vod_lock_worker, args=(str(lock_dir), str(markers), "second"))

    first.start()
    try:
        _wait_for_file(markers / "first.entered")
        second.start()
        time.sleep(0.35)

        assert not (markers / "second.entered").exists()

        (markers / "release-first").write_text("go", encoding="utf-8")
        first.join(5)
        second.join(5)
        assert first.exitcode == 0
        assert second.exitcode == 0
        assert (markers / "second.done").exists()
    finally:
        for process in (first, second):
            if process.is_alive():
                process.terminate()
                process.join(1)


# ── Regression: VOD returns plaintext http:// image URLs; the HTTPS WebUI blocks
#    them as mixed content (broken image). The same object is served over https
#    on Tencent's CDN, so the provider must upgrade the scheme. (2026-06-08) ──

def test_vod_http_image_url_upgraded_to_https():
    from hermes_multitenancy.tencent_vod_image_gen import _extract_image_urls

    detail = _success_detail(
        "http://251000800.vod2.myqcloud.com/abc/def/aigcImageGenFile.jpg"
    )
    assert _extract_image_urls(detail) == [
        "https://251000800.vod2.myqcloud.com/abc/def/aigcImageGenFile.jpg"
    ]


def test_generate_returns_https_image_url_for_tencent_cdn(monkeypatch):
    from hermes_multitenancy.tencent_vod_image_gen import TencentVODImageGenProvider

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"image_gen": {"provider": "tencent-vod", "model": "gem-3.1"}},
    )
    client = FakeVODClient([
        _success_detail("http://251000800.vod2.myqcloud.com/x/y/aigcImageGenFile.jpg"),
    ])
    provider = TencentVODImageGenProvider(client=client, sleep_fn=lambda _s: None)

    result = provider.generate("一张未来城市夜景海报", aspect_ratio="landscape")

    assert result["success"] is True
    assert result["image"].startswith("https://")
    assert result["image"] == (
        "https://251000800.vod2.myqcloud.com/x/y/aigcImageGenFile.jpg"
    )
    # extra["images"] should also be normalized
    assert all(u.startswith("https://") for u in result["images"])


def test_non_tencent_http_url_is_left_untouched():
    from hermes_multitenancy.tencent_vod_image_gen import _extract_image_urls

    # Never force https onto an arbitrary http-only third-party host.
    detail = _success_detail("http://images.example.com/path/pic.jpg")
    assert _extract_image_urls(detail) == ["http://images.example.com/path/pic.jpg"]


def test_already_https_tencent_url_unchanged():
    from hermes_multitenancy.tencent_vod_image_gen import _https_upgrade_tencent_url

    u = "https://251000800.vod2.myqcloud.com/x/y/aigcImageGenFile.jpg"
    assert _https_upgrade_tencent_url(u) == u
