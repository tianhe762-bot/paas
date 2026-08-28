import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from paas import settings_store, timeutil
from paas.communication.registry import AdapterManager
from paas.db import connect, execute_safe_backup, prune_backups
from paas.modules.account.queries import last_chat
from paas.security import decrypt_json

log = logging.getLogger("paas.scheduler")


class SchedulerManager:
    def __init__(self, adapter_manager: AdapterManager) -> None:
        self.adapter_manager = adapter_manager
        self._scheduler: AsyncIOScheduler | None = None
        self.running = False

    def start(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone=str(timeutil.get_timezone()))
        self._add_reminder_jobs()
        self._scheduler.add_job(self._backup_job, "cron", hour=3, minute=0, id="backup")
        self._scheduler.start()
        self.running = True
        log.info("调度器已启动")

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        self.running = False

    def reschedule(self) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.remove_job("reminder")
        except Exception:  # noqa: BLE001
            pass
        self._add_reminder_jobs()

    def _add_reminder_jobs(self) -> None:
        if self._scheduler is None:
            return
        conn = connect()
        try:
            hours_raw = settings_store.get_setting(conn, "reminder_hours", "21,22,0")
        finally:
            conn.close()
        hours = [int(h) for h in str(hours_raw).split(",") if str(h).strip()]
        for hour in hours:
            self._scheduler.add_job(
                self._reminder_job,
                "cron",
                hour=hour,
                minute=0,
                args=[hour],
                id=f"reminder_{hour}",
                replace_existing=True,
            )

    async def _reminder_job(self, hour: int) -> None:
        conn = connect()
        try:
            today_iso = timeutil.iso_today()
            message = settings_store.get_setting(
                conn, "reminder_message", "📝 今天的消费记录了吗？"
            ) or "📝 今天的消费记录了吗？"
            bots = conn.execute(
                "SELECT platform, bot_id, config_enc FROM bot_configs WHERE enabled = 1"
            ).fetchall()
            for bot in bots:
                platform = bot["platform"]
                bot_id = bot["bot_id"]
                cfg = {}
                if bot["config_enc"]:
                    try:
                        cfg = decrypt_json(bot["config_enc"])
                    except Exception:  # noqa: BLE001
                        continue
                users = [
                    r["user_id"]
                    for r in conn.execute(
                        """
                        SELECT DISTINCT user_id FROM user_chats
                        WHERE namespace = ? AND platform = ?
                          AND last_seen_at >= datetime('now', '-30 days')
                        """,
                        (bot_id, platform),
                    ).fetchall()
                ]
                for user_id in users:
                    status = conn.execute(
                        """
                        SELECT reported, zero_confirmed, skipped FROM daily_status
                        WHERE namespace = ? AND user_id = ? AND status_date = ?
                        """,
                        (bot_id, user_id, today_iso),
                    ).fetchone()
                    if status and (
                        status["reported"] or status["zero_confirmed"] or status["skipped"]
                    ):
                        continue
                    chat_id = last_chat(conn, bot_id, user_id, platform) or cfg.get("default_chat_id")
                    if not chat_id:
                        continue
                    ok = await self.adapter_manager.send(platform, bot_id, chat_id, message)
                    log.info("提醒 %s/%s -> %s (%s): %s", platform, bot_id, user_id, chat_id, ok)
                    conn.execute(
                        """
                        INSERT INTO daily_status (namespace, user_id, status_date, reminder_count)
                        VALUES (?, ?, ?, 1)
                        ON CONFLICT(namespace, user_id, status_date)
                        DO UPDATE SET reminder_count = reminder_count + 1
                        """,
                        (bot_id, user_id, today_iso),
                    )
            conn.commit()
        finally:
            conn.close()

    async def _backup_job(self) -> None:
        conn = connect()
        try:
            from paas.modules.account.service import cleanup_stale_staging

            cleanup_stale_staging(conn)
            keep_days = settings_store.get_int(conn, "backup_keep_days", 30)
            target = execute_safe_backup(conn)
            removed = prune_backups(keep_days=keep_days)
            log.info("热备份完成: %s，清理 %s 个旧备份", target, removed)
        finally:
            conn.close()
