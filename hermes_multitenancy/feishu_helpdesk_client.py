from __future__ import annotations

import base64
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator


class HelpdeskApiError(Exception):
    def __init__(self, code: int, msg: str) -> None:
        super().__init__(f"Helpdesk API error {code}: {msg}")
        self.code = code
        self.msg = msg


class HelpdeskClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        helpdesk_id: str,
        helpdesk_token: str,
        base_url: str = "https://open.feishu.cn",
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._helpdesk_id = helpdesk_id
        self._helpdesk_token = helpdesk_token
        self._base_url = base_url.rstrip("/")
        self._tenant_access_token = ""
        self._tenant_token_expire = 0
        self._tenant_token_minted_at = 0.0

    def _helpdesk_auth_header(self) -> str:
        raw = f"{self._helpdesk_id}:{self._helpdesk_token}".encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _parse_envelope_data(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "code" not in payload:
            raise ValueError("response payload missing code")
        code = int(payload["code"])
        msg = str(payload.get("msg") or "")
        if code != 0:
            raise HelpdeskApiError(code, msg)
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return {}

    def _request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 20,
        retries: int = 4,
    ) -> dict[str, Any]:
        # open.feishu.cn intermittently drops TLS handshakes / reads; transient
        # network failures must auto-recover. Real HTTP statuses (e.g. 401) are
        # NOT transient and surface immediately.
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
                with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - caller controls fixed host.
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("response payload must be a JSON object")
                return payload
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout) as exc:
                last_exc = exc
                if attempt == retries - 1:
                    break
                time.sleep(0.6 * (2 ** attempt))
        assert last_exc is not None
        raise last_exc

    def _mint_tenant_access_token(self) -> tuple[str, int]:
        url = f"{self._base_url}/open-apis/auth/v3/app_access_token/internal/"
        body = json.dumps({"app_id": self._app_id, "app_secret": self._app_secret}).encode("utf-8")
        payload = self._request_json(
            url,
            method="POST",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        if "code" not in payload:
            raise ValueError("token payload missing code")
        code = int(payload["code"])
        msg = str(payload.get("msg") or "")
        if code != 0:
            raise HelpdeskApiError(code, msg)
        token = str(payload.get("tenant_access_token") or "").strip()
        if not token:
            raise ValueError("token payload missing tenant_access_token")
        expire = int(payload.get("expire") or 0)
        if expire <= 0:
            raise ValueError("token payload missing expire")
        return token, expire

    def _get_tenant_access_token(self) -> str:
        now = time.time()
        expires_at = self._tenant_token_minted_at + self._tenant_token_expire - 60
        if self._tenant_access_token and now < expires_at:
            return self._tenant_access_token
        token, expire = self._mint_tenant_access_token()
        self._tenant_access_token = token
        self._tenant_token_expire = expire
        self._tenant_token_minted_at = now
        return token

    def _helpdesk_get(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self._get_tenant_access_token()
        url = f"{self._base_url}{path}"
        if query:
            encoded = urllib.parse.urlencode(query)
            if encoded:
                url = f"{url}?{encoded}"
        payload = self._request_json(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Lark-Helpdesk-Authorization": self._helpdesk_auth_header(),
            },
        )
        return self._parse_envelope_data(payload)

    def _iter_paginated_items(
        self,
        path: str,
        items_keys: tuple[str, ...],
        **filters: Any,
    ) -> Iterator[dict[str, Any]]:
        page_token = ""
        while True:
            query = dict(filters)
            if page_token:
                query["page_token"] = page_token
            data = self._helpdesk_get(path, query)
            items: list[dict[str, Any]] = []
            for key in items_keys:
                value = data.get(key)
                if isinstance(value, list):
                    items = [item for item in value if isinstance(item, dict)]
                    break
            if not items:
                break
            for item in items:
                yield item
            next_page_token = str(data.get("page_token") or "")
            if not next_page_token:
                break
            page_token = next_page_token

    def list_categories(self) -> list[dict[str, Any]]:
        data = self._helpdesk_get("/open-apis/helpdesk/v1/categories")
        categories = data.get("categories")
        if isinstance(categories, list):
            return [item for item in categories if isinstance(item, dict)]
        return []

    def iter_tickets(self, **filters: Any) -> Iterator[dict[str, Any]]:
        yield from self._iter_paginated_items("/open-apis/helpdesk/v1/tickets", ("tickets",), **filters)

    def iter_faqs(self, **filters: Any) -> Iterator[dict[str, Any]]:
        yield from self._iter_paginated_items("/open-apis/helpdesk/v1/faqs", ("faqs", "items"), **filters)

    def get_ticket_messages(self, ticket_id: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        page_token = ""
        path = f"/open-apis/helpdesk/v1/tickets/{urllib.parse.quote(ticket_id)}/messages"
        while True:
            query: dict[str, Any] = {}
            if page_token:
                query["page_token"] = page_token
            data = self._helpdesk_get(path, query)
            batch: list[dict[str, Any]] = []
            for key in ("messages", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    batch = [item for item in value if isinstance(item, dict)]
                    break
            if not batch:
                break
            messages.extend(batch)
            next_page_token = str(data.get("page_token") or "")
            if not next_page_token:
                break
            page_token = next_page_token
        return messages

    def send_ticket_message(self, ticket_id: str, text: str, *, msg_type: str = "text") -> dict[str, Any]:
        """Post a bot message into a ticket conversation (the reply path).

        Body shape matches inbound messages: content is a JSON string {"content": ...}.
        """
        token = self._get_tenant_access_token()
        path = f"/open-apis/helpdesk/v1/tickets/{urllib.parse.quote(ticket_id)}/messages"
        content = json.dumps({"content": text}, ensure_ascii=False)
        body = json.dumps({"msg_type": msg_type, "content": content}, ensure_ascii=False).encode("utf-8")
        payload = self._request_json(
            f"{self._base_url}{path}",
            method="POST",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Lark-Helpdesk-Authorization": self._helpdesk_auth_header(),
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        return self._parse_envelope_data(payload)
