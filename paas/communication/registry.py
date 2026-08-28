import logging
from typing import Any

from paas.communication.base import BaseAdapter
from paas.communication.qq.api import QQApiClient
from paas.communication.qq.ws import QQBotClient
from paas.communication.telegram.adapter import TelegramAdapter
from paas.router import Router
from paas.security import decrypt_json

log = logging.getLogger("paas.adapters")


class AdapterManager:
    """多机器人适配器管理：每个机器人实例独立命名空间，互不干扰。"""

    def __init__(self, router: Router) -> None:
        self.router = router
        self._adapters: dict[tuple[str, str], BaseAdapter] = {}

    def get(self, platform: str, bot_id: str) -> BaseAdapter | None:
        return self._adapters.get((platform, bot_id))

    async def apply_configs(self, conn) -> None:
        rows = conn.execute(
            "SELECT id, platform, bot_id, name, enabled, config_enc "
            "FROM bot_configs ORDER BY platform, id"
        ).fetchall()
        for row in rows:
            platform = row["platform"]
            bot_id = row["bot_id"]
            key = (platform, bot_id)
            enabled = bool(row["enabled"])
            current = self._adapters.get(key)
            if enabled and current is None:
                try:
                    cfg = decrypt_json(row["config_enc"])
                    adapter = self._build(platform, bot_id, row["name"], cfg)
                    adapter.set_message_handler(self.router.handle)
                    await adapter.start()
                    self._adapters[key] = adapter
                    self._update_status(conn, key, "running", "")
                    log.info("Adapter %s/%s started", platform, bot_id)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Adapter %s/%s failed to start", platform, bot_id)
                    self._update_status(conn, key, "error", str(exc))
            elif not enabled and current is not None:
                await current.stop()
                self._adapters.pop(key, None)
                self._update_status(conn, key, "stopped", "")
                log.info("Adapter %s/%s stopped", platform, bot_id)
        conn.commit()

    def _build(self, platform: str, bot_id: str, name: str, cfg: dict[str, Any]) -> BaseAdapter:
        if platform == "qq":
            api = QQApiClient(
                app_id=str(cfg.get("app_id", "")).strip(),
                app_secret=str(cfg.get("app_secret", "")).strip(),
            )
            return QQBotClient(
                api, handler=self.router.handle, namespace=bot_id, bot_id=bot_id, bot_name=name
            )
        if platform == "telegram":
            return TelegramAdapter(
                token=str(cfg.get("token", "")).strip(),
                namespace=bot_id,
                bot_id=bot_id,
                bot_name=name,
            )
        raise ValueError(f"未知平台: {platform}")

    async def send(self, platform: str, bot_id: str, chat_id: str, text: str) -> bool:
        adapter = self._adapters.get((platform, bot_id))
        if adapter is None or not adapter.running:
            return False
        try:
            ok = await adapter.send(chat_id, text)
            if not ok:
                adapter.last_error = "发送失败"
            return ok
        except Exception as exc:  # noqa: BLE001
            adapter.last_error = str(exc)
            return False

    def status(self) -> list[dict]:
        out = []
        for (platform, bot_id), adapter in self._adapters.items():
            out.append(
                {
                    "platform": platform,
                    "bot_id": bot_id,
                    "name": adapter.bot_name or bot_id,
                    "running": adapter.running,
                    "last_error": adapter.last_error,
                    "last_message_at": adapter.last_message_at,
                }
            )
        return out

    @staticmethod
    def _update_status(conn, key: tuple[str, str], status: str, error: str) -> None:
        conn.execute(
            "UPDATE bot_configs SET status = ?, last_error = ?, updated_at = datetime('now') "
            "WHERE platform = ? AND bot_id = ?",
            (status, error, key[0], key[1]),
        )

    async def shutdown(self) -> None:
        for adapter in self._adapters.values():
            try:
                await adapter.stop()
            except Exception:  # noqa: BLE001
                log.exception("adapter stop failed")
        self._adapters.clear()

