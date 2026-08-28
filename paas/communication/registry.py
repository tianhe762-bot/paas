import logging
from typing import Any

from paas.communication.base import BaseAdapter
from paas.communication.qq.api import QQApiClient
from paas.communication.qq.ws import QQBotClient
from paas.communication.telegram.adapter import TelegramAdapter
from paas.config import settings
from paas.router import Router
from paas.security import decrypt_json

log = logging.getLogger("paas.adapters")


class AdapterManager:
    def __init__(self, router: Router) -> None:
        self.router = router
        self._adapters: dict[str, BaseAdapter] = {}

    def get(self, platform: str) -> BaseAdapter | None:
        return self._adapters.get(platform)

    async def apply_configs(self, conn) -> None:
        rows = conn.execute(
            "SELECT platform, enabled, config_enc FROM bot_configs"
        ).fetchall()
        for row in rows:
            platform = row["platform"]
            enabled = bool(row["enabled"])
            current = self._adapters.get(platform)
            if enabled and current is None:
                try:
                    cfg = decrypt_json(row["config_enc"])
                    adapter = self._build(platform, cfg)
                    adapter.set_message_handler(self.router.handle)
                    await adapter.start()
                    self._adapters[platform] = adapter
                    self._update_status(conn, platform, "running", "")
                    log.info("Adapter %s started", platform)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Adapter %s failed to start", platform)
                    self._update_status(conn, platform, "error", str(exc))
            elif not enabled and current is not None:
                await current.stop()
                self._adapters.pop(platform, None)
                self._update_status(conn, platform, "stopped", "")
                log.info("Adapter %s stopped", platform)
        conn.commit()

    def _build(self, platform: str, cfg: dict[str, Any]) -> BaseAdapter:
        if platform == "qq":
            api = QQApiClient(
                app_id=str(cfg.get("app_id", "")).strip(),
                app_secret=str(cfg.get("app_secret", "")).strip(),
            )
            return QQBotClient(api, handler=self.router.handle)
        if platform == "telegram":
            return TelegramAdapter(token=str(cfg.get("token", "")).strip())
        raise ValueError(f"未知平台: {platform}")

    async def send(self, platform: str, chat_id: str, text: str) -> bool:
        adapter = self._adapters.get(platform)
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
        for platform in ("qq", "telegram"):
            adapter = self._adapters.get(platform)
            out.append(
                {
                    "platform": platform,
                    "running": adapter.running if adapter else False,
                    "last_error": adapter.last_error if adapter else "",
                    "last_message_at": adapter.last_message_at if adapter else "",
                }
            )
        return out

    @staticmethod
    def _update_status(conn, platform: str, status: str, error: str) -> None:
        conn.execute(
            "UPDATE bot_configs SET status = ?, last_error = ?, updated_at = datetime('now') "
            "WHERE platform = ?",
            (status, error, platform),
        )

    async def shutdown(self) -> None:
        for adapter in self._adapters.values():
            try:
                await adapter.stop()
            except Exception:  # noqa: BLE001
                log.exception("adapter stop failed")
        self._adapters.clear()

