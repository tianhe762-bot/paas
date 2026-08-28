import csv
import io
import logging
import secrets
import time
from typing import Any

from paas import settings_store
from paas.communication.qq.api import QQApiClient
from paas.communication.telegram.adapter import TelegramAdapter
from paas.config import settings
from paas.db import connect, execute_safe_backup, prune_backups
from paas.security import (
    decrypt_json,
    encrypt_json,
    hash_password,
    new_session_token,
    verify_password,
)

log = logging.getLogger("paas.admin")

SESSIONS: dict[str, dict] = {}

CONFIG_DEFAULTS: dict[str, dict[str, Any]] = {
    "qq": {
        "app_id": "",
        "app_secret": "",
        "default_chat_id": "",
        "chat_scope": "private",
    },
    "telegram": {
        "token": "",
        "default_chat_id": "",
    },
}

SECRET_FIELDS = {
    "qq": {"app_secret"},
    "telegram": {"token"},
}


def ensure_admin(conn) -> str:
    row = conn.execute(
        "SELECT username FROM admin_users ORDER BY id LIMIT 1"
    ).fetchone()
    if row is not None:
        return row["username"]
    username = settings.admin_username.strip() or "admin"
    password = settings.admin_password
    if not password:
        password = secrets.token_urlsafe(12)
        log.warning(
            "未设置 ADMIN_PASSWORD，已生成初始密码（仅本次日志可见，请登录后立即修改）: %s",
            password,
        )
    conn.execute(
        "INSERT INTO admin_users (username, password_hash) VALUES (?, ?)",
        (username, hash_password(password)),
    )
    conn.commit()
    return username


# ---------- 登录会话 ----------

def login(username: str, password: str) -> str | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT username, password_hash FROM admin_users WHERE username = ?",
            (username,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or not verify_password(password, row["password_hash"]):
        return None
    token = new_session_token()
    SESSIONS[token] = {
        "username": row["username"],
        "expires_at": time.time() + settings.session_ttl_hours * 3600,
    }
    return token


def check_session(token: str | None) -> bool:
    if not token:
        return False
    sess = SESSIONS.get(token)
    if sess is None:
        return False
    if time.time() > sess["expires_at"]:
        SESSIONS.pop(token, None)
        return False
    return True


def logout(token: str | None) -> None:
    if token:
        SESSIONS.pop(token, None)


def change_password(username: str, old_password: str, new_password: str) -> tuple[bool, str]:
    if len(new_password) < 8:
        return False, "新密码至少 8 位"
    conn = connect()
    try:
        row = conn.execute(
            "SELECT password_hash FROM admin_users WHERE username = ?", (username,)
        ).fetchone()
        if row is None or not verify_password(old_password, row["password_hash"]):
            return False, "旧密码错误"
        conn.execute(
            "UPDATE admin_users SET password_hash = ? WHERE username = ?",
            (hash_password(new_password), username),
        )
        conn.commit()
        return True, "密码已更新"
    finally:
        conn.close()


# ---------- 平台配置 ----------

def get_config(conn, platform: str) -> dict[str, Any]:
    defaults = dict(CONFIG_DEFAULTS.get(platform, {}))
    row = conn.execute(
        "SELECT enabled, config_enc, status, last_error FROM bot_configs WHERE platform = ?",
        (platform,),
    ).fetchone()
    cfg = {"enabled": False, "status": "stopped", "last_error": "", "fields": defaults}
    if row is None:
        return cfg
    cfg["enabled"] = bool(row["enabled"])
    cfg["status"] = row["status"] or "stopped"
    cfg["last_error"] = row["last_error"] or ""
    try:
        stored = decrypt_json(row["config_enc"]) if row["config_enc"] else {}
    except Exception:  # noqa: BLE001
        stored = {}
    merged = {**defaults, **stored}
    for field in SECRET_FIELDS.get(platform, set()):
        if merged.get(field):
            merged[field] = ""
            merged[f"has_{field}"] = True
        else:
            merged[f"has_{field}"] = False
    cfg["fields"] = merged
    return cfg


def set_config(conn, platform: str, fields: dict[str, Any], enabled: bool) -> None:
    defaults = dict(CONFIG_DEFAULTS.get(platform, {}))
    existing: dict[str, Any] = {}
    row = conn.execute(
        "SELECT config_enc FROM bot_configs WHERE platform = ?", (platform,)
    ).fetchone()
    if row and row["config_enc"]:
        try:
            existing = decrypt_json(row["config_enc"])
        except Exception:  # noqa: BLE001
            existing = {}
    merged = {**defaults, **existing}
    for key, value in fields.items():
        if key not in defaults:
            continue
        if key in SECRET_FIELDS.get(platform, set()) and value in ("", "••••••••"):
            continue  # 掩码/空值表示保留原值
        merged[key] = str(value).strip()
    conn.execute(
        """
        INSERT INTO bot_configs (platform, enabled, config_enc, status, last_error, updated_at)
        VALUES (?, ?, ?, 'stopped', '', datetime('now'))
        ON CONFLICT(platform) DO UPDATE SET
            enabled = excluded.enabled,
            config_enc = excluded.config_enc,
            status = 'stopped',
            last_error = '',
            updated_at = datetime('now')
        """,
        (platform, 1 if enabled else 0, encrypt_json(merged)),
    )
    conn.commit()


async def test_config(platform: str, fields: dict[str, Any]) -> tuple[bool, str]:
    if platform not in CONFIG_DEFAULTS:
        return False, "未知平台"
    merged: dict[str, Any] = dict(CONFIG_DEFAULTS[platform])
    # 测试连接时，留空/掩码字段沿用已保存的配置，避免把占位符当真实密钥
    conn = connect()
    try:
        row = conn.execute(
            "SELECT config_enc FROM bot_configs WHERE platform = ?", (platform,)
        ).fetchone()
        if row and row["config_enc"]:
            try:
                stored = decrypt_json(row["config_enc"])
                merged.update({k: v for k, v in stored.items() if k in merged})
            except Exception:  # noqa: BLE001
                pass
    finally:
        conn.close()
    for key, value in fields.items():
        if key in merged and str(value).strip() not in ("", "••••••••"):
            merged[key] = str(value).strip()
    if platform == "qq":
        client = QQApiClient(
            app_id=str(merged.get("app_id", "")).strip(),
            app_secret=str(merged.get("app_secret", "")).strip(),
        )
        try:
            token = await client.get_access_token()
            await client.aclose()
            return (True, "凭证有效") if token else (False, "无法获取 access_token")
        except Exception as exc:  # noqa: BLE001
            await client.aclose()
            return False, str(exc)
    if platform == "telegram":
        token = str(merged.get("token", "")).strip()
        if not token:
            return False, "未配置 Bot Token，请先在表单填写并保存"
        adapter = TelegramAdapter(token=token)
        try:
            return await adapter.test()
        finally:
            await adapter.stop()
    return False, "未知平台"


# ---------- 设置 / 状态 / 备份 ----------

def get_settings(conn) -> dict[str, str]:
    return settings_store.get_all(conn)


def put_settings(conn, updates: dict[str, Any]) -> list[str]:
    applied: list[str] = []
    for key, value in updates.items():
        if key not in settings_store.DEFAULTS:
            continue
        if key in ("reminder_hours", "dedup_window_seconds", "pending_ttl_seconds", "backup_keep_days"):
            try:
                if key == "reminder_hours":
                    hours = [int(h) for h in str(value).split(",") if str(h).strip()]
                    if not hours:
                        continue
                else:
                    int(str(value))
            except ValueError:
                continue
        settings_store.set_setting(conn, key, str(value))
        applied.append(key)
    return applied


def backup_now(conn, keep_days: int = 30) -> str:
    target = execute_safe_backup(conn)
    removed = prune_backups(keep_days=keep_days)
    log.info("热备份完成: %s，清理旧备份 %s 个", target, removed)
    return str(target)


def import_template_csv() -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["日期", "金额", "分类", "备注"])
    writer.writerow(["2026-08-01", 86.5, "餐饮", "吃火锅"])
    writer.writerow(["2026-08-02", 23, "交通", "打车回家"])
    writer.writerow(["2026-08-03", 35, "购物", "日用品"])
    return buf.getvalue().encode("utf-8-sig")
