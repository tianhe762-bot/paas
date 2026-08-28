import asyncio
import json
import logging
from collections import deque
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from paas.communication.base import BaseAdapter
from paas.communication.qq.api import QQApiClient
from paas.models import Attachment, InboundMessage, Reply

log = logging.getLogger("paas.qq.ws")

INTENTS_GROUP_AND_C2C = 1 << 25


class _Reconnect(Exception):
    pass


class QQBotClient(BaseAdapter):
    """QQ 官方机器人 WebSocket 客户端：Hello -> Identify -> 心跳 -> C2C 事件处理。"""

    platform = "qq"

    def __init__(
        self,
        api: QQApiClient,
        handler=None,
        intents: int = INTENTS_GROUP_AND_C2C,
        namespace: str = "default",
        bot_id: str = "default",
        bot_name: str = "",
    ) -> None:
        super().__init__()
        self.api = api
        self.intents = intents
        self.namespace = namespace
        self.bot_id = bot_id
        self.bot_name = bot_name
        if handler is not None:
            self.set_message_handler(handler)
        self._task: asyncio.Task | None = None
        self._ws = None
        self._session_id: str | None = None
        self._seq: int | None = None
        self._reply_seqs: dict[str, int] = {}
        self._seen: deque[str] = deque(maxlen=2000)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="qq-ws")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        await self.api.aclose()

    async def test(self) -> tuple[bool, str]:
        try:
            token = await self.api.get_access_token()
            if not token:
                return False, "无法获取 access_token"
            return True, "凭证有效"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def send(self, chat_id: str, text: str, reply_to_msg_id: str | None = None) -> bool:
        try:
            await self.api.send_c2c_message(
                openid=chat_id,
                content=text,
                msg_id=reply_to_msg_id,
                msg_seq=self._next_seq(reply_to_msg_id),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            log.warning("QQ send failed: %s", exc)
            if reply_to_msg_id:
                # 被动回复过期/被去重 → 尝试主动发送
                try:
                    await self.api.send_c2c_message(openid=chat_id, content=text)
                    return True
                except Exception as exc2:  # noqa: BLE001
                    self.last_error = str(exc2)
            return False

    def _next_seq(self, msg_id: str | None) -> int:
        if not msg_id:
            return 1
        seq = self._reply_seqs.get(msg_id, 0) + 1
        self._reply_seqs[msg_id] = seq
        if len(self._reply_seqs) > 500:
            self._reply_seqs.clear()
        return seq

    # ---------- 连接与事件循环 ----------

    async def _run_loop(self) -> None:
        backoff = 2
        while self._running:
            try:
                await self._connect_once()
                backoff = 2
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.warning("QQ WS 连接异常，%ss 后重连: %s", backoff, exc)
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _connect_once(self) -> None:
        gateway = await self.api.get_gateway_url()
        headers = {"Authorization": f"QQBot {await self.api.get_access_token()}"}
        async with websockets.connect(
            gateway, additional_headers=headers, max_size=None
        ) as ws:
            self._ws = ws
            heartbeat_task: asyncio.Task | None = None
            try:
                async for raw in ws:
                    payload = json_loads(raw)
                    op = payload.get("op")
                    if op == 10:  # Hello
                        interval = payload["d"].get("heartbeat_interval", 45000)
                        await self._identify(ws)
                        heartbeat_task = asyncio.create_task(
                            self._heartbeat_loop(ws, interval / 1000.0)
                        )
                    elif op == 0:  # Dispatch
                        self._seq = payload.get("s")
                        await self._on_dispatch(payload.get("t", ""), payload.get("d", {}))
                    elif op == 7:  # Reconnect
                        raise _Reconnect()
                    elif op == 9:  # Invalid Session
                        self._session_id = None
                        await self._identify(ws)
                    # op 11 Heartbeat ACK 无需处理
            except ConnectionClosed as exc:
                if exc.code in (4006, 4007):
                    self._session_id = None
                raise
            finally:
                self._ws = None
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass

    async def _identify(self, ws) -> None:
        token = f"QQBot {await self.api.get_access_token()}"
        if self._session_id:
            # 尝试恢复会话
            await ws.send(
                json_dumps(
                    {
                        "op": 6,
                        "d": {
                            "token": token,
                            "session_id": self._session_id,
                            "seq": self._seq,
                        },
                    }
                )
            )
            return
        await ws.send(
            json_dumps(
                {
                    "op": 2,
                    "d": {
                        "token": token,
                        "intents": self.intents,
                        "shard": [0, 1],
                        "properties": {
                            "$os": "linux",
                            "$browser": "paas",
                            "$device": "paas",
                        },
                    },
                }
            )
        )

    async def _heartbeat_loop(self, ws, interval: float) -> None:
        while self._running:
            await asyncio.sleep(interval)
            await ws.send(json_dumps({"op": 1, "d": self._seq}))

    async def _on_dispatch(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type == "READY":
            self._session_id = data.get("session_id")
            return
        if event_type != "C2C_MESSAGE_CREATE":
            return
        message_id = str(data.get("id") or "")
        if not message_id or message_id in self._seen:
            return
        self._seen.append(message_id)
        author = data.get("author") or {}
        openid = author.get("user_openid") or author.get("id") or ""
        if not openid:
            return
        attachments = []
        for att in data.get("attachments") or []:
            attachments.append(
                Attachment(
                    filename=str(att.get("filename") or ""),
                    url=str(att.get("url") or ""),
                    content_type=str(att.get("content_type") or ""),
                )
            )
        msg = InboundMessage(
            namespace=self.namespace,
            platform="qq",
            user_id=openid,
            chat_id=openid,
            message_id=message_id,
            timestamp=str(data.get("timestamp") or ""),
            message_type="file" if attachments else "text",
            content=str(data.get("content") or ""),
            attachments=attachments,
            raw=data,
        )
        self.last_message_at = msg.timestamp or msg.message_id
        if self._handler is None:
            return
        try:
            reply: Reply = await self._handler(msg)
            if reply and reply.reply_content:
                await self.send(openid, reply.reply_content, reply_to_msg_id=message_id)
        except Exception:  # noqa: BLE001
            log.exception("QQ 消息处理失败")


def json_loads(raw) -> dict:
    return json.loads(raw)


def json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
