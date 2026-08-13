"""Dedicated Feishu OAuth, OpenAPI, and long-connection adapter for agent relay."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

EVENT_STREAM_START_TIMEOUT_SECONDS = 10
logger = logging.getLogger(__name__)


def _valid_actor_id(value: str) -> bool:
    return value.startswith("ou_") and 4 <= len(value) <= 128 and not any(ch.isspace() for ch in value)


def _card_with_actions(
    content: dict[str, Any], actions: list[dict[str, str]], card_id: str, nonce: str
) -> dict[str, Any]:
    card = json.loads(json.dumps(content, ensure_ascii=False))
    if card.get("schema") == "2.0":
        body = card.get("body")
        elements = body.get("elements") if isinstance(body, dict) else None
    else:
        elements = card.get("elements")
    if not isinstance(elements, list):
        raise ValueError("card content must contain an elements list")
    if actions:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": action["label"]},
                        "value": {
                            "relay_action": True,
                            "card_id": card_id,
                            "nonce": nonce,
                            "action_id": action["id"],
                        },
                    }
                    for action in actions
                ],
            }
        )
    return card


class FeishuApiError(RuntimeError):
    def __init__(
        self,
        status: int,
        retry_after: int | None = None,
        *,
        code: Any = None,
        message: Any = None,
    ) -> None:
        super().__init__(f"Feishu HTTP error {status}")
        self.status = status
        self.retry_after = retry_after
        self.code = (
            code
            if isinstance(code, int) or code is None
            else " ".join(str(code).split())[:64]
        )
        self.message = " ".join(str(message).split())[:512] if message is not None else ""


def _error_details(payload: Any) -> tuple[Any, Any]:
    if not isinstance(payload, dict):
        return None, None
    error = payload.get("error")
    nested = error if isinstance(error, dict) else {}
    code = payload.get("code", nested.get("code"))
    message = payload.get("msg") or payload.get("message") or nested.get("msg") or nested.get("message")
    return code, message or (error if isinstance(error, str) else None)


class FeishuRelayClient:
    """Dedicated-app OAuth, outbound transport, and one Feishu event stream."""

    def __init__(self, app_id: str, app_secret: str, redirect_uri: str) -> None:
        if not app_id or not app_secret or not redirect_uri:
            raise RuntimeError("relay Feishu app id, secret, and redirect URI are required")
        self.app_id = app_id
        self.app_secret = app_secret
        self.redirect_uri = redirect_uri
        self._tenant_token = ""
        self._tenant_token_expires_at = 0.0
        self._token_lock = threading.Lock()

    def authorize_url(self, *, state: str) -> str:
        query = urllib.parse.urlencode(
            {
                "client_id": self.app_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "state": state,
            }
        )
        return f"https://accounts.feishu.cn/open-apis/authen/v1/authorize?{query}"

    async def exchange(self, code: str) -> dict[str, str]:
        return await asyncio.to_thread(self._exchange, code)

    def _exchange(self, code: str) -> dict[str, str]:
        token = self._request_json(
            "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
            method="POST",
            body={
                "grant_type": "authorization_code",
                "client_id": self.app_id,
                "client_secret": self.app_secret,
                "code": code,
                "redirect_uri": self.redirect_uri,
            },
        )
        access_token = str(token.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError("Feishu OAuth returned no user access token")
        info = self._request_json(
            "https://open.feishu.cn/open-apis/authen/v1/user_info",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        data = info.get("data") if isinstance(info.get("data"), dict) else info
        actor_id = str(data.get("open_id") or "").strip()
        display_name = str(data.get("name") or "").strip()
        if not _valid_actor_id(actor_id) or not display_name:
            raise RuntimeError("Feishu user info returned no identity")
        return {"actor_id": actor_id, "display_name": display_name}

    async def send_message(self, **request: Any) -> dict[str, str]:
        return await asyncio.wait_for(
            asyncio.to_thread(self._send_message, **request), timeout=5
        )

    def _send_message(
        self,
        *,
        actor_id: str,
        msg_type: str,
        content: dict[str, Any],
        uuid: str,
    ) -> dict[str, str]:
        feishu_type = "interactive" if msg_type == "card" else msg_type
        data = self._request_json(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
            method="POST",
            headers={"Authorization": f"Bearer {self._tenant_access()}"},
            body={
                "receive_id": actor_id,
                "msg_type": feishu_type,
                "content": json.dumps(content, ensure_ascii=False),
                "uuid": uuid,
            },
        ).get("data") or {}
        return {
            "message_id": str(data.get("message_id") or ""),
            "conversation_id": str(data.get("chat_id") or ""),
        }

    async def send_card(self, **request: Any) -> dict[str, str]:
        return await asyncio.wait_for(
            asyncio.to_thread(self._send_card, **request), timeout=5
        )

    def _send_card(
        self,
        *,
        actor_id: str,
        content: dict[str, Any],
        actions: list[dict[str, str]],
        card_id: str,
        nonce: str,
        uuid: str,
    ) -> dict[str, str]:
        card = _card_with_actions(content, actions, card_id, nonce)
        return self._send_message(
            actor_id=actor_id,
            msg_type="card",
            content=card,
            uuid=uuid,
        )

    async def update_card(self, **request: Any) -> None:
        await asyncio.wait_for(
            asyncio.to_thread(self._update_card, **request), timeout=5
        )

    def _update_card(
        self,
        *,
        message_id: str,
        status: str = "",
        action_id: str = "",
        content: dict[str, Any] | None = None,
        **_ignored: Any,
    ) -> None:
        if content is not None:
            # ponytail: caller-supplied content goes out verbatim — wrapping it would
            # break schema 2.0 cards, whose elements live under body.elements.
            card = content
        else:
            text = f"Status: {status}" + (f" ({action_id})" if action_id else "")
            card = {"elements": [{"tag": "markdown", "content": text}]}
        # ponytail: keep the server-composed fallback. The card_action click path has
        # no caller content; without this the card keeps live-looking buttons forever.
        self._request_json(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{urllib.parse.quote(message_id)}",
            method="PATCH",
            headers={"Authorization": f"Bearer {self._tenant_access()}"},
            body={"content": json.dumps(card, ensure_ascii=False)},
        )

    async def send_ambiguity_notice(self, actor_id: str) -> None:
        await self.send_message(
            actor_id=actor_id,
            msg_type="text",
            content={"text": "检测到多个等待中的本地会话，请引用对应卡片后回复。"},
            uuid=secrets.token_hex(16),
        )

    def _tenant_access(self) -> str:
        with self._token_lock:
            if self._tenant_token and time.time() < self._tenant_token_expires_at:
                return self._tenant_token
            payload = self._request_json(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                method="POST",
                body={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            token = str(payload.get("tenant_access_token") or "").strip()
            if not token:
                raise RuntimeError("Feishu returned no tenant access token")
            self._tenant_token = token
            self._tenant_token_expires_at = time.time() + max(60, int(payload.get("expire") or 7200) - 60)
            return token

    @staticmethod
    def _request_json(
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            method=method,
            headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})},
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - fixed Feishu hosts.
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            retry = str(exc.headers.get("Retry-After") or "").strip()
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                error_payload = None
            code, message = _error_details(error_payload)
            raise FeishuApiError(
                exc.code,
                int(retry) if retry.isdigit() else None,
                code=code,
                message=message,
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Feishu response is not an object")
        if payload.get("code") not in {None, 0} or payload.get("error"):
            status = 400 if "/open-apis/im/v1/messages" in url else 502
            code, message = _error_details(payload)
            raise FeishuApiError(status, code=code, message=message)
        return payload

    async def ingest_event_payload(
        self, events: "RelayEvents", event_type: str, payload: dict[str, Any]
    ) -> bool:
        event = payload.get("event") or {}
        event_id = str((payload.get("header") or {}).get("event_id") or "")
        if event_type == "message":
            msg = event.get("message") or {}
            sender = ((event.get("sender") or {}).get("sender_id") or {})
            if msg.get("chat_type") != "p2p" or msg.get("message_type") != "text":
                return False
            try:
                content = json.loads(msg.get("content") or "{}")
            except (TypeError, json.JSONDecodeError):
                return False
            return await events.ingest_text(
                event_id=event_id,
                actor_id=str(sender.get("open_id") or ""),
                text=str(content.get("text") or ""),
                create_time=int(msg.get("create_time") or 0),
                parent_message_id=str(msg.get("parent_id") or msg.get("root_id") or "") or None,
            )
        if event_type in {"reaction_created", "reaction_deleted"}:
            user = event.get("user_id") or event.get("operator_id") or {}
            reaction_value = event.get("reaction_type") or {}
            return await events.ingest_reaction(
                event_id=event_id,
                actor_id=str(user.get("open_id") or ""),
                message_id=str(event.get("message_id") or ""),
                emoji_type=str(reaction_value.get("emoji_type") or ""),
                operation="create" if event_type == "reaction_created" else "delete",
            )
        if event_type == "card_action":
            action = event.get("action") or {}
            value = action.get("value") or {}
            if not isinstance(value, dict) or value.get("relay_action") is not True:
                return False
            operator = event.get("operator") or {}
            context = event.get("context") or {}
            return await events.ingest_card_action(
                actor_id=str(operator.get("open_id") or ""),
                card_id=str(value.get("card_id") or ""),
                nonce=str(value.get("nonce") or ""),
                message_id=str(context.get("open_message_id") or ""),
                action_id=str(value.get("action_id") or ""),
            )
        return False

    def start_event_stream(
        self, events: "RelayEvents", loop: asyncio.AbstractEventLoop
    ) -> Callable[[], None]:
        import lark_oapi as lark
        from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

        def submit(coro: Any, event_type: str) -> Any:
            future = asyncio.run_coroutine_threadsafe(coro, loop)

            def log_failure(done: Any) -> None:
                if not done.cancelled() and done.exception() is not None:
                    logger.error("relay_audit event=%s status=failed", event_type)

            future.add_done_callback(log_failure)
            return future

        def message(data: Any) -> None:
            payload = json.loads(lark.JSON.marshal(data))
            submit(self.ingest_event_payload(events, "message", payload), "message")

        def reaction(operation: str):
            def handle(data: Any) -> None:
                payload = json.loads(lark.JSON.marshal(data))
                event_type = "reaction_created" if operation == "create" else "reaction_deleted"
                submit(self.ingest_event_payload(events, event_type, payload), event_type)

            return handle

        def card_action(data: Any) -> Any:
            payload = json.loads(lark.JSON.marshal(data))
            accepted = submit(
                self.ingest_event_payload(events, "card_action", payload), "card_action"
            ).result(timeout=2)
            return self._toast("Recorded" if accepted else "Already resolved", "success" if accepted else "info")

        handler = (
            EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(message)
            .register_p2_im_message_reaction_created_v1(reaction("create"))
            .register_p2_im_message_reaction_deleted_v1(reaction("delete"))
            .register_p2_card_action_trigger(card_action)
            .build()
        )
        ready = threading.Event()
        stopping = threading.Event()
        errors: list[Exception] = []
        state: dict[str, Any] = {}

        def run_event_stream() -> None:
            thread_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(thread_loop)
            from lark_oapi.ws import client as ws_client

            ws_client.loop = thread_loop
            client = lark.ws.Client(
                self.app_id,
                self.app_secret,
                event_handler=handler,
                log_level=lark.LogLevel.ERROR,
                auto_reconnect=False,
            )
            state.update(loop=thread_loop, client=client)
            connect = client._connect

            async def connect_and_signal() -> None:
                await connect()
                client._auto_reconnect = True
                ready.set()

            client._connect = connect_and_signal
            try:
                client.start()
            except Exception as exc:
                if not stopping.is_set():
                    errors.append(exc)
            finally:
                ready.set()
                pending = asyncio.all_tasks(thread_loop)
                for task in pending:
                    task.cancel()
                if pending:
                    thread_loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                thread_loop.close()

        thread = threading.Thread(
            target=run_event_stream,
            name="agent-relay-feishu-ws",
            daemon=True,
        )
        thread.start()

        def stop_event_stream() -> None:
            stopping.set()
            thread_loop = state.get("loop")
            client = state.get("client")
            shutdown_error: Exception | None = None
            try:
                if thread_loop is not None and not thread_loop.is_closed():
                    if client is not None and client._conn is not None:
                        client._auto_reconnect = False
                        future = asyncio.run_coroutine_threadsafe(
                            client._disconnect(), thread_loop
                        )
                        try:
                            future.result(timeout=5)
                        except Exception as exc:
                            shutdown_error = exc
                    thread_loop.call_soon_threadsafe(thread_loop.stop)
            finally:
                thread.join(timeout=5)
            if thread.is_alive() or shutdown_error is not None:
                raise RuntimeError("Feishu event stream failed to stop") from None

        if (
            not ready.wait(EVENT_STREAM_START_TIMEOUT_SECONDS)
            or errors
            or getattr(state.get("client"), "_conn", None) is None
        ):
            stop_event_stream()
            raise RuntimeError("Feishu event stream failed to start") from None
        return stop_event_stream

    @staticmethod
    def _toast(content: str, level: str) -> Any:
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

        response = P2CardActionTriggerResponse()
        response.toast = {"type": level, "content": content}
        return response
