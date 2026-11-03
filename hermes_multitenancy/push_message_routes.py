"""``POST /api/run-broker/push`` — a pure message-send bypass (SPEC push-custom-message).

Wraps Feishu ``im/v1/messages`` as a thin endpoint: **send only, no agent run**.
Auth reuses the ingest-key体系 (fail-closed, ``_ingest_binding_for_request``) —
wangkejie uses the same token he already has for ``/ingest``. Three message
types: ``text`` (native plain text), ``post`` (rich text), and ``interactive``
(2.0 raw card JSON). Structured content is passed through verbatim;飞书
validates the schema, its error code/msg is surfaced to the caller.

Event-loop redline (design §Regression guards): this handler runs INSIDE the
multitenancy_router gateway process. A synchronous Feishu HTTP call here would
block the shared asyncio loop and stall every user's chat (gateway-deadlock
前科). So the blocking mint+send is offloaded to a thread via
``run_in_executor`` and capped by an explicit ``asyncio.wait_for`` — the loop
never blocks.

Deploy contract (hard ship steps, SPEC §部署契约): the public Caddy proxy is
single-path — add an explicit route for ``/api/run-broker/push`` or it 404s
externally; any new broker env must live in the systemd drop-in or the next
restart silently drops it.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from . import push_env_map as _env_map
from .webui_broker import periphery as _periphery

logger = logging.getLogger(__name__)

#: Feishu card size is on the order of tens of KB; 30KB is the caller-facing cap
#: on the serialized ``content`` string (design §边界).
MAX_CONTENT_BYTES = 30 * 1024

VALID_MSG_TYPES = ("text", "post", "interactive")
VALID_RECEIVE_ID_TYPES = ("user_id", "open_id", "chat_id")
VALID_ENVS = ("pre", "online")


def _push_timeout() -> float:
    """Loop-facing wall-clock cap (seconds) for the mint+send offload.

    The redline is that the gateway loop must never block; ``wait_for`` frees the
    loop at this bound regardless of the worker thread. Override via
    ``HERMES_PUSH_TIMEOUT``."""
    raw = os.getenv("HERMES_PUSH_TIMEOUT", "5")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 5.0
    return value if value > 0 else 5.0


class PushFeishuError(Exception):
    """A Feishu-side rejection — carries the original code/msg for passthrough."""

    def __init__(self, *, code: int, msg: str) -> None:
        super().__init__(f"feishu code={code} msg={msg}")
        self.code = code
        self.msg = msg


class PushBotNotRegistered(Exception):
    """The requested ``app_id`` is not a gateway-known bot (cannot mint a token)."""


class PushCredentialError(Exception):
    """The default/main bot credentials are not configured on this host."""


# --- credential resolution seam ------------------------------------------

def _default_bot_credentials(app_id: str) -> tuple[str, str]:
    """Resolve ``(client_id, client_secret)`` for the sending bot.

    No ``app_id`` (or the main bot's own id) → the main bot via
    ``_feishu_app_credentials``. Otherwise the app must be a registered bot whose
    ``app`` secret lives in the shared credential store under
    ``(__global__, <app_id>, feishu, app)`` — the same convention the main bot
    uses (subject ``feishu_app``), keyed per-app for experts. Not found → the
    caller gets a 400 (design §边界: "app_id 必须是网关已注册 bot，能 mint tenant token")."""
    from . import feishu_uat_auth as fa

    shared_home = _periphery._shared_home_from_env()
    try:
        main_id, main_secret = fa._feishu_app_credentials(shared_home)
    except fa.FeishuUatAuthError as exc:
        raise PushCredentialError(str(exc)) from exc

    app_id = str(app_id or "").strip()
    if not app_id or app_id == main_id:
        return main_id, main_secret

    from .credentials import CredentialStore

    store = None
    try:
        store = CredentialStore(shared_home / "multitenancy.db")
        status = store.get_status(
            profile_name="__global__",
            subject_id=app_id,
            provider="feishu",
            secret_kind="app",
        )
        if status.get("status") == "valid":
            payload = store.get_secret_for_runtime(
                profile_name="__global__",
                subject_id=app_id,
                provider="feishu",
                secret_kind="app",
            )
            secret = str(payload.get("app_secret") or payload.get("FEISHU_APP_SECRET") or "").strip()
            if secret:
                return app_id, secret
    except PushBotNotRegistered:
        raise
    except Exception as exc:  # store/decrypt failure is a config problem, not caller's
        raise PushCredentialError(f"app credential lookup failed: {exc}") from exc
    finally:
        if store is not None:
            store.close()
    raise PushBotNotRegistered(app_id)


_bot_credentials_resolver: Callable[[str], tuple[str, str]] = _default_bot_credentials


def override_bot_credentials_resolver(fn: Optional[Callable[[str], tuple[str, str]]]) -> None:
    global _bot_credentials_resolver
    _bot_credentials_resolver = fn if fn is not None else _default_bot_credentials


# --- send seam -----------------------------------------------------------

def _default_feishu_send(
    *,
    client_id: str,
    client_secret: str,
    receive_id_type: str,
    receive_id: str,
    msg_type: str,
    content_str: str,
    uuid: Optional[str] = None,
    timeout: float = 5.0,
) -> str:
    """Mint a tenant token and POST one message to Feishu ``im/v1/messages``.

    SYNCHRONOUS (urllib) — reuses the repo's existing token mint
    (``feishu_uat_auth._mint_tenant_access_token``) rather than reimplementing an
    async client. The caller runs this in an executor so the gateway loop is
    never blocked (see module docstring). Returns the Feishu ``message_id``;
    raises ``PushFeishuError`` on any non-zero Feishu code."""
    from . import feishu_uat_auth as fa

    token = fa._mint_tenant_access_token(client_id, client_secret, timeout=int(timeout) or 5)
    body: dict[str, Any] = {
        "receive_id": receive_id,
        "msg_type": msg_type,
        "content": content_str,
    }
    if uuid:
        body["uuid"] = uuid
    url = (
        f"{fa.FEISHU_OPEN_BASE_URL}/open-apis/im/v1/messages"
        f"?receive_id_type={urllib.parse.quote(receive_id_type)}"
    )
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed Feishu host.
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            out = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PushFeishuError(code=exc.code, msg=exc.reason or "feishu http error") from exc
        if not isinstance(out, dict):
            raise PushFeishuError(code=exc.code, msg="feishu http error") from exc

    code = int(out.get("code") or 0)
    if code != 0:
        raise PushFeishuError(code=code, msg=str(out.get("msg") or ""))
    message_id = str((out.get("data") or {}).get("message_id") or "").strip()
    if not message_id:
        # 拿到 message_id 才算成功 (design §反例) — a 0-code with no id is a failure.
        raise PushFeishuError(code=0, msg="feishu returned no message_id")
    return message_id


_feishu_sender: Callable[..., str] = _default_feishu_send


def override_feishu_sender(fn: Optional[Callable[..., str]]) -> None:
    global _feishu_sender
    _feishu_sender = fn if fn is not None else _default_feishu_send


# --- handler -------------------------------------------------------------

def register_push_message_routes(app: Any) -> None:
    """Register ``POST /api/run-broker/push`` on the run-broker app."""
    from aiohttp import web

    async def handle_push(request):
        binding = _periphery._ingest_binding_for_request(request)
        if binding is None:
            # Never echo the presented token (design §LEAK: only mask_token form).
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response(
                {"ok": False, "error": "body must be a JSON object"}, status=400
            )

        target = str(payload.get("target") or "").strip()
        if not target:
            return web.json_response({"ok": False, "error": "target is required"}, status=400)

        msg_type = str(payload.get("msg_type") or "").strip()
        if msg_type not in VALID_MSG_TYPES:
            return web.json_response(
                {"ok": False, "error": f"msg_type must be one of {list(VALID_MSG_TYPES)}"},
                status=400,
            )

        content = payload.get("content")
        if not isinstance(content, dict):
            return web.json_response(
                {"ok": False, "error": "content must be a JSON object"}, status=400
            )
        if msg_type == "text" and not isinstance(content.get("text"), str):
            return web.json_response(
                {"ok": False, "error": 'text content must be {"text": "<string>"}'},
                status=400,
            )

        # receive_id_type: explicit wins; else oc_ → chat_id, otherwise user_id.
        rid_type = str(payload.get("receive_id_type") or "").strip()
        if rid_type:
            if rid_type not in VALID_RECEIVE_ID_TYPES:
                return web.json_response(
                    {"ok": False,
                     "error": f"receive_id_type must be one of {list(VALID_RECEIVE_ID_TYPES)}"},
                    status=400,
                )
        else:
            rid_type = "chat_id" if target.startswith("oc_") else "user_id"

        env = str(payload.get("env") or "pre").strip()
        if env not in VALID_ENVS:
            return web.json_response(
                {"ok": False, "error": f"env must be one of {list(VALID_ENVS)}"}, status=400
            )

        content_str = json.dumps(content, ensure_ascii=False)
        if len(content_str.encode("utf-8")) > MAX_CONTENT_BYTES:
            return web.json_response(
                {"ok": False, "error": f"content exceeds {MAX_CONTENT_BYTES} bytes"}, status=400
            )

        app_id = str(payload.get("app_id") or "").strip()
        uuid = str(payload.get("uuid") or "").strip() or None

        try:
            client_id, client_secret = _bot_credentials_resolver(app_id)
        except PushBotNotRegistered:
            return web.json_response(
                {"ok": False, "error": f"app_id not registered: {app_id}"}, status=400
            )
        except PushCredentialError as exc:
            return web.json_response(
                {"ok": False, "error": f"bot credentials unavailable: {exc}"}, status=503
            )

        timeout = _push_timeout()
        loop = asyncio.get_running_loop()
        # ponytail: sync urllib mint+send offloaded to a thread so the gateway
        # loop never blocks (gateway-deadlock redline); wait_for is the ≤5s
        # loop-facing cap. Worst case the orphan thread lingers to ~2×timeout on
        # a stall (mint + send each get `timeout`); upgrade to an aiohttp client
        # only if that ever matters.
        try:
            message_id = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    functools.partial(
                        _feishu_sender,
                        client_id=client_id,
                        client_secret=client_secret,
                        receive_id_type=rid_type,
                        receive_id=target,
                        msg_type=msg_type,
                        content_str=content_str,
                        uuid=uuid,
                        timeout=timeout,
                    ),
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return web.json_response(
                {"ok": False, "status": "timeout", "error": "feishu send timed out"}, status=504
            )
        except PushFeishuError as exc:
            # 飞书侧失败绝不吞成 200；原始 code/msg 透传 (design §反例).
            return web.json_response(
                {"ok": False, "error": "feishu send failed",
                 "feishu_code": exc.code, "feishu_msg": exc.msg},
                status=502,
            )
        except Exception as exc:  # mint failure / unexpected — never a false 200
            logger.warning("[push] feishu send error: %s", exc)
            return web.json_response(
                {"ok": False, "error": "feishu send failed"}, status=502
            )

        # Record env → message_id mapping for the topic-reply consumer (later
        # slug). Never fail the response over a mapping write — the message已发出.
        key_owner = binding.get("owner") or binding.get("name") or binding.get("profile") or ""
        try:
            _env_map.get_env_map_store().record(
                message_id=message_id, env=env, key_owner=key_owner
            )
        except Exception:
            logger.warning(
                "[push] env-map record failed for message_id=%s (send succeeded)",
                message_id,
            )

        return web.json_response(
            {"ok": True, "message_id": message_id, "env": env}, status=200
        )

    app.router.add_post("/api/run-broker/push", handle_push)
