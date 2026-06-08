from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    success_response,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "tencent-vod"
DEFAULT_VOD_IMAGE_MODEL = "gem-3.1"
VOD_MODEL_OVERRIDE_ENV = "HERMES_VOD_IMAGE_MODEL_OVERRIDE"

_VOD_ENV_KEYS = (
    "TENCENTCLOUD_SECRET_ID",
    "TENCENTCLOUD_SECRET_KEY",
    "VOD_SUBAPP_ID",
)
_OPTIONAL_VOD_ENV_KEYS = (
    "VOD_REGION",
    "VOD_STORAGE_MODE",
    "VOD_POLL_TIMEOUT",
    "VOD_POLL_INTERVAL",
    "VOD_ENDPOINT",
    "HERMES_VOD_LOCK_DIR",
)
_VOD_PROCESS_LOCK = threading.Lock()


@dataclass(frozen=True)
class VODImageModel:
    id: str
    display: str
    model_name: str
    model_version: str
    speed: str = ""
    strengths: str = ""
    price: str = "Tencent VOD billing"
    scene_type: Optional[str] = None
    output_config_extra: dict[str, Any] = field(default_factory=dict)


VOD_IMAGE_MODELS: dict[str, VODImageModel] = {
    "gem-2.5": VODImageModel(
        id="gem-2.5",
        display="GEM 2.5",
        model_name="GEM",
        model_version="2.5",
        strengths="Tencent VOD GEM image generation",
    ),
    "gem-3.0": VODImageModel(
        id="gem-3.0",
        display="GEM 3.0",
        model_name="GEM",
        model_version="3.0",
        strengths="Tencent VOD GEM image generation",
    ),
    "gem-3.1": VODImageModel(
        id="gem-3.1",
        display="GEM 3.1",
        model_name="GEM",
        model_version="3.1",
        strengths="Default Tencent VOD GEM image generation",
    ),
    "gpt-image2-low": VODImageModel(
        id="gpt-image2-low",
        display="GPT Image 2 Low",
        model_name="OG",
        model_version="image2_low",
        strengths="Fast GPT Image 2 tier via Tencent VOD",
    ),
    "gpt-image2-medium": VODImageModel(
        id="gpt-image2-medium",
        display="GPT Image 2 Medium",
        model_name="OG",
        model_version="image2_medium",
        strengths="Balanced GPT Image 2 tier via Tencent VOD",
    ),
    "gpt-image2-high": VODImageModel(
        id="gpt-image2-high",
        display="GPT Image 2 High",
        model_name="OG",
        model_version="image2_high",
        strengths="Highest GPT Image 2 tier via Tencent VOD",
    ),
    "qwen-image-0925": VODImageModel(
        id="qwen-image-0925",
        display="Qwen Image 0925",
        model_name="Qwen",
        model_version="0925",
        strengths="Qwen image generation via Tencent VOD",
    ),
    "si-4.5": VODImageModel(
        id="si-4.5",
        display="SI 4.5",
        model_name="SI",
        model_version="4.5",
        strengths="SI image generation; business open may be required",
    ),
    "si-5.0-lite": VODImageModel(
        id="si-5.0-lite",
        display="SI 5.0 Lite",
        model_name="SI",
        model_version="5.0-lite",
        strengths="SI lite image generation; business open may be required",
    ),
    "kling-image-2.1": VODImageModel(
        id="kling-image-2.1",
        display="Kling Image 2.1",
        model_name="Kling",
        model_version="2.1",
        strengths="Kling image generation via Tencent VOD",
    ),
    "kling-image-3.0": VODImageModel(
        id="kling-image-3.0",
        display="Kling Image 3.0",
        model_name="Kling",
        model_version="3.0",
        strengths="Kling image generation via Tencent VOD",
    ),
    "kling-image-3.0-omni": VODImageModel(
        id="kling-image-3.0-omni",
        display="Kling Image 3.0 Omni",
        model_name="Kling",
        model_version="3.0-Omni",
        strengths="Kling omni image generation via Tencent VOD",
    ),
    "vidu-image-q2": VODImageModel(
        id="vidu-image-q2",
        display="Vidu Image Q2",
        model_name="Vidu",
        model_version="q2",
        strengths="Vidu image generation via Tencent VOD",
    ),
    "jimeng-4.0": VODImageModel(
        id="jimeng-4.0",
        display="Jimeng 4.0",
        model_name="Jimeng",
        model_version="4.0",
        strengths="Jimeng image generation via Tencent VOD",
    ),
    "mj-v7": VODImageModel(
        id="mj-v7",
        display="MJ v7",
        model_name="MJ",
        model_version="v7",
        strengths="MJ v7 image generation via Tencent VOD",
    ),
    "hunyuan-3.0": VODImageModel(
        id="hunyuan-3.0",
        display="Hunyuan 3.0",
        model_name="Hunyuan",
        model_version="3.0",
        strengths="Hunyuan image generation via Tencent VOD",
    ),
    "hunyuan-3d-panorama": VODImageModel(
        id="hunyuan-3d-panorama",
        display="Hunyuan 3D Panorama",
        model_name="Hunyuan",
        model_version="3.0",
        scene_type="3d_panorama",
        strengths="Hunyuan panoramic scene generation via Tencent VOD",
    ),
}

_MODEL_PATTERN = re.compile(
    r"(?P<model>"
    + "|".join(re.escape(model_id) for model_id in sorted(VOD_IMAGE_MODELS, key=len, reverse=True))
    + r")",
    re.IGNORECASE,
)


class TencentVODProviderError(RuntimeError):
    pass


class TencentVODConfigurationError(TencentVODProviderError):
    pass


class TencentVODDependencyError(TencentVODProviderError):
    pass


class TencentVODTimeoutError(TencentVODProviderError):
    pass


def register_vod_image_gen_provider(ctx: Any) -> bool:
    register = getattr(ctx, "register_image_gen_provider", None)
    if not callable(register):
        logger.debug("[tencent-vod] image_gen provider registration unavailable on ctx")
        return False
    register(TencentVODImageGenProvider())
    return True


def detect_vod_model_override(text: str) -> Optional[str]:
    if not isinstance(text, str):
        return None
    match = _MODEL_PATTERN.search(text)
    if not match:
        return None
    model = match.group("model").lower()
    return model if model in VOD_IMAGE_MODELS else None


def clean_vod_prompt_directive(prompt: str, model_id: Optional[str] = None) -> str:
    text = str(prompt or "").strip()
    if not text:
        return text
    model = model_id or detect_vod_model_override(text)
    if not model:
        return text

    escaped = re.escape(model)
    patterns = [
        rf"^\s*(?:请)?(?:用|使用)\s*`?{escaped}`?\s*(?:这个)?(?:模型)?\s*(?:帮我)?(?:生图|画图|生成图片|出图|画一张|生成一张)\s*[:：,，-]*\s*",
        rf"^\s*(?:请)?(?:帮我)?(?:用|使用)\s*`?{escaped}`?\s*(?:这个)?(?:模型)?\s*[:：,，-]*\s*",
        rf"\bwith\s+`?{escaped}`?\s*(?:model)?\s*[:：,，-]*\s*",
    ]
    cleaned = text
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(rf"`?{escaped}`?\s*(?:模型)?", "", cleaned, count=1, flags=re.IGNORECASE).strip()
    cleaned = cleaned.lstrip(":：,，- ").strip()
    return cleaned or text


def _load_image_gen_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        config = load_config()
    except Exception as exc:
        logger.debug("[tencent-vod] could not load Hermes config: %s", exc)
        return {}
    section = config.get("image_gen") if isinstance(config, dict) else None
    return dict(section) if isinstance(section, dict) else {}


def _read_config_model() -> str:
    section = _load_image_gen_config()
    value = section.get("model")
    return value.strip() if isinstance(value, str) and value.strip() else DEFAULT_VOD_IMAGE_MODEL


def _configured_provider_is_tencent_vod() -> bool:
    section = _load_image_gen_config()
    value = section.get("provider")
    return isinstance(value, str) and value.strip() == PROVIDER_NAME


def _resolve_aspect_ratio_for_vod(aspect_ratio: str) -> tuple[str, str]:
    raw = str(aspect_ratio or "").strip()
    if ":" in raw and raw.replace(":", "").replace(".", "").isdigit():
        return raw, raw
    aspect = resolve_aspect_ratio(raw)
    return {
        "landscape": ("landscape", "16:9"),
        "square": ("square", "1:1"),
        "portrait": ("portrait", "9:16"),
    }.get(aspect, (DEFAULT_ASPECT_RATIO, "16:9"))


def _secret_values() -> list[str]:
    values = []
    for key in _VOD_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            values.append(value)
    return values


def _redact(text: Any) -> str:
    rendered = str(text)
    for secret in _secret_values():
        if secret:
            rendered = rendered.replace(secret, "[redacted]")
    return rendered


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _vod_sub_app_id() -> int:
    raw = os.environ.get("VOD_SUBAPP_ID", "").strip()
    if not raw:
        raise TencentVODConfigurationError("VOD_SUBAPP_ID is required")
    try:
        value = int(raw)
    except ValueError as exc:
        raise TencentVODConfigurationError("VOD_SUBAPP_ID must be an integer") from exc
    if value <= 0:
        raise TencentVODConfigurationError("VOD_SUBAPP_ID must be positive")
    return value


def _missing_required_env() -> list[str]:
    return [key for key in _VOD_ENV_KEYS if not os.environ.get(key)]


def _sdk_available() -> bool:
    try:
        import tencentcloud.common.credential  # noqa: F401
        import tencentcloud.vod.v20180717.vod_client  # noqa: F401
        import tencentcloud.vod.v20180717.models  # noqa: F401
    except ImportError:
        return False
    return True


def _response_dict(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        data = response
    elif hasattr(response, "to_json_string"):
        data = json.loads(response.to_json_string())
    else:
        data = dict(response)
    nested = data.get("Response")
    return dict(nested) if isinstance(nested, dict) else dict(data)


def _vod_lock_dir() -> Path:
    configured = os.environ.get("HERMES_VOD_LOCK_DIR", "").strip()
    base = Path(configured).expanduser() if configured else Path(tempfile.gettempdir()) / "hermes-vod-locks"
    base.mkdir(parents=True, exist_ok=True)
    return base


@contextlib.contextmanager
def _vod_generation_lock(sub_app_id: int):
    lock_path = _vod_lock_dir() / f"tencent-vod-image-{sub_app_id}.lock"
    with _VOD_PROCESS_LOCK:
        with lock_path.open("a", encoding="utf-8") as lock_file:
            logger.info("[tencent-vod] waiting for generation lock sub_app_id=%s", sub_app_id)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                logger.info("[tencent-vod] acquired generation lock sub_app_id=%s", sub_app_id)
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class TencentVODSDKClient:
    def __init__(self, *, secret_id: str, secret_key: str, region: str, endpoint: str = ""):
        try:
            from tencentcloud.common import credential
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            from tencentcloud.vod.v20180717 import models, vod_client
        except ImportError as exc:
            raise TencentVODDependencyError(
                "Tencent VOD SDK is not installed. Install tencentcloud-sdk-python-vod."
            ) from exc

        http_profile = HttpProfile()
        if endpoint:
            http_profile.endpoint = endpoint
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile
        cred = credential.Credential(secret_id, secret_key)
        self._client = vod_client.VodClient(cred, region, client_profile)
        self._models = models

    def create_aigc_image_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = self._models.CreateAigcImageTaskRequest()
        request.from_json_string(json.dumps(payload, ensure_ascii=False))
        return _response_dict(self._client.CreateAigcImageTask(request))

    def describe_task_detail(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = self._models.DescribeTaskDetailRequest()
        request.from_json_string(json.dumps(payload, ensure_ascii=False))
        return _response_dict(self._client.DescribeTaskDetail(request))


class _StaticVODClient:
    def __init__(self, result_url: str):
        self.result_url = result_url
        self.create_payloads: list[dict[str, Any]] = []
        self.describe_payloads: list[dict[str, Any]] = []

    def create_aigc_image_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.create_payloads.append(dict(payload))
        return {"TaskId": "static-vod-task", "RequestId": "static-request"}

    def describe_task_detail(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.describe_payloads.append(dict(payload))
        return {
            "TaskType": "AigcImageTask",
            "Status": "FINISH",
            "AigcImageTask": {
                "Status": "SUCCESS",
                "ErrCode": 0,
                "Message": "",
                "Output": {"FileInfos": [{"FileUrl": self.result_url}]},
            },
        }


class TencentVODImageGenProvider(ImageGenProvider):
    def __init__(
        self,
        *,
        client: Any = None,
        client_factory: Optional[Callable[[], Any]] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        time_fn: Callable[[], float] = time.monotonic,
        fake_client_result_url: str = "",
    ):
        self._client = client or (_StaticVODClient(fake_client_result_url) if fake_client_result_url else None)
        self._client_factory = client_factory
        self._sleep = sleep_fn
        self._time = time_fn

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def display_name(self) -> str:
        return "Tencent VOD AIGC"

    def is_available(self) -> bool:
        if _configured_provider_is_tencent_vod():
            return True
        return not _missing_required_env() and _sdk_available()

    def list_models(self) -> list[dict[str, Any]]:
        return [
            {
                "id": model.id,
                "display": model.display,
                "speed": model.speed,
                "strengths": model.strengths,
                "price": model.price,
            }
            for model in VOD_IMAGE_MODELS.values()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_VOD_IMAGE_MODEL

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "paid",
            "tag": "Tencent VOD AIGC image generation",
            "env_vars": [
                {"key": "TENCENTCLOUD_SECRET_ID", "prompt": "Tencent Cloud SecretId"},
                {"key": "TENCENTCLOUD_SECRET_KEY", "prompt": "Tencent Cloud SecretKey"},
                {"key": "VOD_SUBAPP_ID", "prompt": "Tencent VOD SubAppId"},
                {"key": "VOD_REGION", "prompt": "Tencent VOD region, e.g. ap-guangzhou"},
            ],
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        **kwargs: Any,
    ) -> dict[str, Any]:
        model_id = ""
        cleaned_prompt = str(prompt or "").strip()
        aspect_label, vod_aspect = _resolve_aspect_ratio_for_vod(aspect_ratio)
        try:
            if not cleaned_prompt:
                raise ValueError("Prompt is required and must be a non-empty string")

            model_id = self._resolve_model_id(cleaned_prompt)
            cleaned_prompt = clean_vod_prompt_directive(cleaned_prompt, model_id)
            model = VOD_IMAGE_MODELS[model_id]
            client = self._resolve_client()
            payload = self._build_create_payload(
                model=model,
                prompt=cleaned_prompt,
                vod_aspect_ratio=vod_aspect,
                seed=kwargs.get("seed"),
            )
            with _vod_generation_lock(payload["SubAppId"]):
                logger.info(
                    "[tencent-vod] creating image task model=%s aspect=%s prompt=%s",
                    model_id,
                    vod_aspect,
                    cleaned_prompt[:80],
                )
                created = client.create_aigc_image_task(payload)
                task_id = str(created.get("TaskId") or "").strip()
                if not task_id:
                    raise TencentVODProviderError("CreateAigcImageTask returned no TaskId")
                detail = self._poll_task(client, task_id=task_id, sub_app_id=payload["SubAppId"])
            image_urls = _extract_image_urls(detail)
            if not image_urls:
                raise TencentVODProviderError("Tencent VOD task finished without image URLs")
            return success_response(
                image=image_urls[0],
                model=model_id,
                prompt=cleaned_prompt,
                aspect_ratio=aspect_label,
                provider=self.name,
                extra={
                    "task_id": task_id,
                    "images": image_urls,
                    "vod_model_name": model.model_name,
                    "vod_model_version": model.model_version,
                },
            )
        except ValueError as exc:
            return error_response(
                error=_redact(exc),
                error_type="invalid_argument",
                provider=self.name,
                model=model_id,
                prompt=cleaned_prompt,
                aspect_ratio=aspect_label,
            )
        except TencentVODConfigurationError as exc:
            return error_response(
                error=_redact(exc),
                error_type="configuration_error",
                provider=self.name,
                model=model_id,
                prompt=cleaned_prompt,
                aspect_ratio=aspect_label,
            )
        except TencentVODDependencyError as exc:
            return error_response(
                error=_redact(exc),
                error_type="missing_dependency",
                provider=self.name,
                model=model_id,
                prompt=cleaned_prompt,
                aspect_ratio=aspect_label,
            )
        except TencentVODTimeoutError as exc:
            return error_response(
                error=_redact(exc),
                error_type="timeout",
                provider=self.name,
                model=model_id,
                prompt=cleaned_prompt,
                aspect_ratio=aspect_label,
            )
        except Exception as exc:
            logger.warning("[tencent-vod] image generation failed: %s", _redact(exc), exc_info=True)
            return error_response(
                error=_redact(exc),
                error_type=exc.__class__.__name__,
                provider=self.name,
                model=model_id,
                prompt=cleaned_prompt,
                aspect_ratio=aspect_label,
            )

    def _resolve_model_id(self, prompt: str) -> str:
        override = os.environ.get(VOD_MODEL_OVERRIDE_ENV, "").strip()
        prompt_override = detect_vod_model_override(prompt)
        model_id = (override or prompt_override or _read_config_model() or DEFAULT_VOD_IMAGE_MODEL).lower()
        if model_id not in VOD_IMAGE_MODELS:
            raise ValueError(
                f"Unknown Tencent VOD image model {model_id!r}. "
                f"Supported models: {', '.join(VOD_IMAGE_MODELS)}"
            )
        return model_id

    def _resolve_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory()
            return self._client
        missing = _missing_required_env()
        if missing:
            raise TencentVODConfigurationError(
                "Missing Tencent VOD environment variables: " + ", ".join(missing)
            )
        self._client = TencentVODSDKClient(
            secret_id=os.environ["TENCENTCLOUD_SECRET_ID"],
            secret_key=os.environ["TENCENTCLOUD_SECRET_KEY"],
            region=os.environ.get("VOD_REGION", "ap-guangzhou").strip() or "ap-guangzhou",
            endpoint=os.environ.get("VOD_ENDPOINT", "").strip(),
        )
        return self._client

    def _build_create_payload(
        self,
        *,
        model: VODImageModel,
        prompt: str,
        vod_aspect_ratio: str,
        seed: Any = None,
    ) -> dict[str, Any]:
        output_config: dict[str, Any] = {
            "StorageMode": os.environ.get("VOD_STORAGE_MODE", "Temporary").strip() or "Temporary",
            "AspectRatio": vod_aspect_ratio,
            "PersonGeneration": "AllowAdult",
            "InputComplianceCheck": "Enabled",
            "OutputComplianceCheck": "Enabled",
        }
        output_config.update(model.output_config_extra)
        payload: dict[str, Any] = {
            "SubAppId": _vod_sub_app_id(),
            "ModelName": model.model_name,
            "ModelVersion": model.model_version,
            "Prompt": prompt,
            "OutputConfig": output_config,
        }
        if model.scene_type:
            payload["SceneType"] = model.scene_type
        if isinstance(seed, int):
            payload["Seed"] = seed
        return payload

    def _poll_task(self, client: Any, *, task_id: str, sub_app_id: int) -> dict[str, Any]:
        timeout_s = _env_float("VOD_POLL_TIMEOUT", 300.0)
        interval_s = _env_float("VOD_POLL_INTERVAL", 3.0)
        deadline = self._time() + timeout_s
        last_detail: dict[str, Any] = {}
        while True:
            detail = client.describe_task_detail({"TaskId": task_id, "SubAppId": sub_app_id})
            last_detail = detail if isinstance(detail, dict) else _response_dict(detail)
            status = _detail_status(last_detail)
            if status in {"FINISH", "SUCCESS"}:
                _raise_if_detail_failed(last_detail)
                return last_detail
            if status in {"FAIL", "FAILED", "ABORTED"}:
                raise TencentVODProviderError(_detail_error_message(last_detail, fallback=f"task {status}"))
            now = self._time()
            if now >= deadline:
                raise TencentVODTimeoutError(
                    f"Timed out waiting for Tencent VOD task {task_id} after {timeout_s:.0f}s; "
                    f"last status={status or 'unknown'}"
                )
            self._sleep(min(interval_s, max(0.0, deadline - now)))


def _detail_task(detail: dict[str, Any]) -> dict[str, Any]:
    for key in ("AigcImageTask", "SceneAigcImageTask"):
        value = detail.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _detail_status(detail: dict[str, Any]) -> str:
    task = _detail_task(detail)
    for container in (task, detail):
        status = container.get("Status") if isinstance(container, dict) else None
        if isinstance(status, str) and status.strip():
            return status.strip().upper()
    return ""


def _detail_error_message(detail: dict[str, Any], *, fallback: str = "Tencent VOD task failed") -> str:
    task = _detail_task(detail)
    message = task.get("Message") or detail.get("Message") or fallback
    err_code = task.get("ErrCodeExt") or task.get("ErrCode") or detail.get("ErrCodeExt") or detail.get("ErrCode")
    if err_code not in (None, "", 0, "0"):
        return f"{message} ({err_code})"
    return str(message)


def _raise_if_detail_failed(detail: dict[str, Any]) -> None:
    task = _detail_task(detail)
    status = str(task.get("Status") or "").upper()
    err_code = task.get("ErrCodeExt") or task.get("ErrCode")
    if status in {"FAIL", "FAILED", "ABORTED"} or err_code not in (None, "", 0, "0"):
        raise TencentVODProviderError(_detail_error_message(detail))


# Tencent CDN hosts that serve every object over TLS. VOD returns plaintext
# ``http://`` AIGC image URLs, but the WebUI is served over HTTPS and browsers
# block an http image on an https page as mixed content (the image renders as
# broken). The same object is reachable over https on these hosts, so upgrade
# the scheme before the URL ever leaves the provider. Scoped to Tencent's own
# CDN domains so we never force https onto an http-only third party.
_TENCENT_TLS_HOST_SUFFIXES = (".myqcloud.com", ".qcloud.com", ".tencentcos.cn")


def _https_upgrade_tencent_url(url: str) -> str:
    if not isinstance(url, str) or not url.startswith("http://"):
        return url
    host = urllib.parse.urlparse(url).hostname or ""
    host = host.strip().lower().rstrip(".")
    if host.endswith(_TENCENT_TLS_HOST_SUFFIXES):
        return "https://" + url[len("http://"):]
    return url


def _extract_image_urls(detail: dict[str, Any]) -> list[str]:
    task = _detail_task(detail)
    output = task.get("Output") if isinstance(task, dict) else None
    candidates: list[str] = []
    if isinstance(output, dict):
        for key in ("FileInfos", "FileInfoSet", "ImageInfos", "ImageInfoSet"):
            values = output.get(key)
            if isinstance(values, list):
                for item in values:
                    if not isinstance(item, dict):
                        continue
                    for url_key in ("FileUrl", "Url", "ImageUrl", "MediaUrl"):
                        url = item.get(url_key)
                        if isinstance(url, str) and url.startswith(("http://", "https://")):
                            candidates.append(_https_upgrade_tencent_url(url))
        for url_key in ("FileUrl", "Url", "ImageUrl", "MediaUrl"):
            url = output.get(url_key)
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                candidates.append(_https_upgrade_tencent_url(url))
    return list(dict.fromkeys(candidates))


__all__ = [
    "DEFAULT_VOD_IMAGE_MODEL",
    "PROVIDER_NAME",
    "VOD_IMAGE_MODELS",
    "VOD_MODEL_OVERRIDE_ENV",
    "TencentVODImageGenProvider",
    "clean_vod_prompt_directive",
    "detect_vod_model_override",
    "register_vod_image_gen_provider",
]
