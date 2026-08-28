import logging
import time
from typing import Any

import httpx

from paas.config import settings

log = logging.getLogger("paas.qq.api")


class QQApiError(RuntimeError):
    pass


class QQApiClient:
    """QQ 官方机器人 OpenAPI 客户端：access_token 获取/缓存、网关、发消息、下载附件。"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        api_base: str | None = None,
        token_url: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.api_base = (api_base or settings.qq_api_base).rstrip("/")
        self.token_url = token_url or settings.qq_token_url
        self._client = httpx.AsyncClient(timeout=timeout)
        self._access_token = ""
        self._expires_at = 0.0

    async def get_access_token(self) -> str:
        if self._access_token and time.time() < self._expires_at:
            return self._access_token
        if not self.app_id or not self.app_secret:
            raise QQApiError("缺少 AppID / AppSecret")
        resp = await self._client.post(
            self.token_url,
            json={"appId": self.app_id, "clientSecret": self.app_secret},
        )
        if resp.status_code != 200:
            raise QQApiError(f"获取 access_token 失败: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise QQApiError(f"获取 access_token 失败: {data}")
        self._access_token = token
        self._expires_at = time.time() + int(data.get("expires_in", 7200)) - 120
        return token

    async def _headers(self) -> dict[str, str]:
        token = await self.get_access_token()
        return {"Authorization": f"QQBot {token}"}

    async def get_gateway_url(self) -> str:
        resp = await self._client.get(
            f"{self.api_base}/gateway/bot", headers=await self._headers()
        )
        if resp.status_code != 200:
            raise QQApiError(f"获取网关失败: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        url = data.get("url")
        if not url:
            raise QQApiError(f"网关响应缺少 url: {data}")
        return url

    async def send_c2c_message(
        self,
        openid: str,
        content: str,
        msg_id: str | None = None,
        msg_seq: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"msg_type": 0, "content": content}
        if msg_id:
            body["msg_id"] = msg_id
            body["msg_seq"] = msg_seq or 1
        resp = await self._client.post(
            f"{self.api_base}/v2/users/{openid}/messages",
            headers=await self._headers(),
            json=body,
        )
        if resp.status_code != 200:
            raise QQApiError(f"发送消息失败: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        err = data.get("err_code", 0)
        if err:
            raise QQApiError(f"发送消息失败: err_code={err} msg={data.get('message')}")
        return data

    async def download(self, url: str) -> bytes:
        resp = await self._client.get(url)
        if resp.status_code == 401:
            resp = await self._client.get(url, headers=await self._headers())
        resp.raise_for_status()
        return resp.content

    async def aclose(self) -> None:
        await self._client.aclose()

