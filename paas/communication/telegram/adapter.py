import asyncio
import datetime
import logging
import os
from typing import Any

import httpx

from paas.communication.base import BaseAdapter
from paas.config import settings
from paas.models import Attachment, InboundMessage, Reply

log = logging.getLogger("paas.telegram")

IMPORT_MIME = {
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class TelegramAdapter(BaseAdapter):
    platform = "telegram"

    def __init__(self, token: str, handler=None) -> None:
        super().__init__()
        self.token = token
        if handler is not None:
            self.set_message_handler(handler)
        proxy = os.environ.get("TELEGRAM_PROXY", "").strip()
        if proxy:
            self._client = httpx.AsyncClient(timeout=60.0, proxy=proxy)
        else:
            self._client = httpx.AsyncClient(timeout=60.0)
        self._task: asyncio.Task | None = None
        self._offset = 0

    def _base(self) -> str:
        return f"{settings.tg_api_base.rstrip('/')}/bot{self.token}"

    async def _api(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await self._client.post(f"{self._base()}/{method}", json=params or {})
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram API {method} 失败: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API {method} 失败: {data}")
        return data

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="telegram-poll")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self._client.aclose()

    async def test(self) -> tuple[bool, str]:
        try:
            data = await self._api("getMe")
            user = data.get("result", {})
            return True, f"机器人 @{user.get('username', '')} 可用"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def send(self, chat_id: str, text: str, reply_to_msg_id: str | None = None) -> bool:
        try:
            params: dict[str, Any] = {"chat_id": int(chat_id), "text": text}
            if reply_to_msg_id:
                params["reply_to_message_id"] = int(reply_to_msg_id)
            await self._api("sendMessage", params)
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            log.warning("Telegram send failed: %s", exc)
            return False

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                data = await self._api(
                    "getUpdates",
                    {"timeout": 30, "offset": self._offset, "allowed_updates": ["message"]},
                )
                for update in data.get("result", []):
                    self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                    message = update.get("message")
                    if message:
                        await self._process_message(message)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.warning("Telegram poll error: %s", exc)
                await asyncio.sleep(3)
            await asyncio.sleep(0.2)

    async def _process_message(self, message: dict[str, Any]) -> None:
        chat_id = str(message.get("chat", {}).get("id", ""))
        sender = message.get("from") or {}
        user_id = str(sender.get("id") or chat_id)
        message_id = str(message.get("message_id") or "")
        ts = int(message.get("date") or 0)
        timestamp = (
            datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()
            if ts
            else ""
        )
        attachments: list[Attachment] = []
        message_type = "text"
        doc = message.get("document") or {}
        filename = str(doc.get("file_name") or "")
        if filename.lower().endswith((".csv", ".xlsx", ".xls")):
            message_type = "file"
            try:
                file_data = await self._api("getFile", {"file_id": doc["file_id"]})
                file_path = file_data["result"]["file_path"]
                resp = await self._client.get(
                    f"{settings.tg_api_base.rstrip('/')}/file/bot{self.token}/{file_path}"
                )
                resp.raise_for_status()
                attachments.append(
                    Attachment(
                        filename=filename,
                        content_type=str(doc.get("mime_type") or ""),
                        data=resp.content,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.warning("Telegram file download failed: %s", exc)
        msg = InboundMessage(
            platform="telegram",
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            timestamp=timestamp,
            message_type=message_type,
            content=str(message.get("text") or ""),
            attachments=attachments,
            raw=message,
        )
        self.last_message_at = timestamp or message_id
        if self._handler is None:
            return
        try:
            reply: Reply = await self._handler(msg)
            if reply and reply.reply_content:
                await self.send(chat_id, reply.reply_content, reply_to_msg_id=message_id)
        except Exception:  # noqa: BLE001
            log.exception("Telegram message handling failed")
