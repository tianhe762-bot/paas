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

MAX_BOTS_PER_PLATFORM = 5


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
        "INSERT INTO admin_users (username, password_hash, role) VALUES (?, ?, 'admin')",
        (username, hash_password(password)),
    )
    conn.commit()
    return username


# ---------- 登录会话 ----------

def login(username: str, password: str) -> str | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT username, password_hash, role FROM admin_users WHERE username = ?",
            (username,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or not verify_password(password, row["password_hash"]):
        return None
    token = new_session_token()
    SESSIONS[token] = {
        "username": row["username"],
        "role": row["role"],
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


def session_info(token: str | None) -> dict | None:
    if not check_session(token):
        return None
    return SESSIONS.get(token)


def is_admin(token: str | None) -> bool:
    info = session_info(token)
    return bool(info and info.get("role") == "admin")


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


# ---------- 机器人 CRUD ----------

def _gen_bot_id() -> str:
    return "b" + secrets.token_hex(4)


def list_bots(conn, viewer_id: int, admin: bool) -> list[dict]:
    q = "SELECT * FROM bot_configs"
    params: list[Any] = []
    if not admin:
        q += " WHERE owner_id = ?"
        params.append(viewer_id)
    q += " ORDER BY platform, id"
    out = []
    for row in conn.execute(q, params).fetchall():
        d = dict(row)
        d["config"] = get_bot_fields(conn, row["bot_id"])
        out.append(d)
    return out


def get_bot(conn, bot_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM bot_configs WHERE bot_id = ?", (bot_id,)
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["config"] = get_bot_fields(conn, bot_id)
    return d


def get_bot_fields(conn, bot_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT config_enc FROM bot_configs WHERE bot_id = ?", (bot_id,)
    ).fetchone()
    defaults = {}
    stored: dict[str, Any] = {}
    if row and row["config_enc"]:
        try:
            stored = decrypt_json(row["config_enc"])
        except Exception:  # noqa: BLE001
            stored = {}
        platform = conn.execute(
            "SELECT platform FROM bot_configs WHERE bot_id = ?", (bot_id,)
        ).fetchone()
        if platform:
            defaults = dict(CONFIG_DEFAULTS.get(platform["platform"], {}))
    merged = {**defaults, **stored}
    platform = conn.execute(
        "SELECT platform FROM bot_configs WHERE bot_id = ?", (bot_id,)
    ).fetchone()
    platform_name = platform["platform"] if platform else ""
    for field in SECRET_FIELDS.get(platform_name, set()):
        merged[field] = ""
        merged[f"has_{field}"] = bool(stored.get(field))
    return merged


def create_bot(
    conn, owner_id: int, platform: str, name: str, fields: dict[str, Any], enabled: bool
) -> tuple[bool, str]:
    if platform not in CONFIG_DEFAULTS:
        return False, "未知平台"
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM bot_configs WHERE platform = ?", (platform,)
    ).fetchone()["n"]
    if count >= MAX_BOTS_PER_PLATFORM:
        return False, f"该平台最多 {MAX_BOTS_PER_PLATFORM} 个机器人"
    bot_id = _gen_bot_id()
    merged = dict(CONFIG_DEFAULTS[platform])
    for key, value in fields.items():
        if key in merged:
            merged[key] = str(value).strip()
    conn.execute(
        """
        INSERT INTO bot_configs
            (platform, bot_id, owner_id, name, enabled, config_enc, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'stopped', datetime('now'))
        """,
        (platform, bot_id, owner_id, name or platform, 1 if enabled else 0, encrypt_json(merged)),
    )
    conn.commit()
    return True, bot_id


def update_bot(
    conn, bot_id: str, fields: dict[str, Any] | None = None,
    enabled: bool | None = None, name: str | None = None,
) -> bool:
    row = conn.execute(
        "SELECT platform, config_enc FROM bot_configs WHERE bot_id = ?", (bot_id,)
    ).fetchone()
    if row is None:
        return False
    platform = row["platform"]
    existing: dict[str, Any] = {}
    if row["config_enc"]:
        try:
            existing = decrypt_json(row["config_enc"])
        except Exception:  # noqa: BLE001
            existing = {}
    merged = {**CONFIG_DEFAULTS.get(platform, {}), **existing}
    if fields:
        for key, value in fields.items():
            if key not in merged:
                continue
            if key in SECRET_FIELDS.get(platform, set()) and str(value) in ("", "••••••••"):
                continue
            merged[key] = str(value).strip()
    sets = ["config_enc = ?", "updated_at = datetime('now')"]
    params: list[Any] = [encrypt_json(merged)]
    if enabled is not None:
        sets.append("enabled = ?")
        params.append(1 if enabled else 0)
    if name is not None:
        sets.append("name = ?")
        params.append(name)
    params.append(bot_id)
    conn.execute(
        f"UPDATE bot_configs SET {', '.join(sets)}, status='stopped', last_error='' WHERE bot_id = ?",
        params,
    )
    conn.commit()
    return True


def delete_bot(conn, bot_id: str) -> bool:
    row = conn.execute(
        "SELECT platform FROM bot_configs WHERE bot_id = ?", (bot_id,)
    ).fetchone()
    if row is None:
        return False
    for table in (
        "expenses", "accounts", "daily_status", "pending_actions",
        "user_chats", "raw_messages", "imports",
    ):
        conn.execute(f"DELETE FROM {table} WHERE namespace = ?", (bot_id,))
    conn.execute("DELETE FROM bot_configs WHERE bot_id = ?", (bot_id,))
    conn.commit()
    return True


async def test_config(platform: str, fields: dict[str, Any], bot_id: str | None = None) -> tuple[bool, str]:
    if platform not in CONFIG_DEFAULTS:
        return False, "未知平台"
    merged: dict[str, Any] = dict(CONFIG_DEFAULTS[platform])
    conn = connect()
    try:
        if bot_id:
            row = conn.execute(
                "SELECT config_enc FROM bot_configs WHERE bot_id = ?", (bot_id,)
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


# ---------- 用户管理（仅管理员） ----------

def list_users(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, username, role, created_at FROM admin_users ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def create_user(conn, username: str, password: str, role: str = "user") -> tuple[bool, str]:
    username = (username or "").strip()
    if not username or len(password) < 8:
        return False, "用户名不能为空，密码至少 8 位"
    if role not in ("admin", "user"):
        return False, "角色必须是 admin 或 user"
    try:
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, hash_password(password), role),
        )
        conn.commit()
        return True, "已创建"
    except Exception as exc:  # noqa: BLE001
        return False, f"创建失败：{exc}"


def delete_user(conn, user_id: int) -> tuple[bool, str]:
    if user_id <= 0:
        return False, "无效用户"
    row = conn.execute(
        "SELECT role FROM admin_users WHERE id = ?", (user_id,)
    ).fetchone()
    if row is None:
        return False, "用户不存在"
    if row["role"] == "admin":
        others = conn.execute(
            "SELECT COUNT(*) AS n FROM admin_users WHERE role = 'admin' AND id != ?",
            (user_id,),
        ).fetchone()["n"]
        if others == 0:
            return False, "至少保留一个管理员"
    conn.execute("DELETE FROM admin_users WHERE id = ?", (user_id,))
    conn.execute("UPDATE bot_configs SET owner_id = 0 WHERE owner_id = ?", (user_id,))
    conn.commit()
    return True, "已删除"


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

