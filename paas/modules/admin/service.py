import csv
import io
import logging
import secrets
import time
from pathlib import Path
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
AI_LOCAL_DELETE_CONFIRM = "删除本地模型"
VALID_AI_ORDERS = {
    "rules,local,cloud", "rules,cloud,local",
    "local,rules,cloud", "local,cloud,rules",
    "cloud,rules,local", "cloud,local,rules",
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
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, role) VALUES (?, ?, 'admin')",
        (username, hash_password(password)),
    )
    conn.commit()
    _write_first_run_credentials(username, password)
    _log_first_run_welcome(username, password)
    return username


def _credential_file() -> Path:
    path = Path(settings.data_dir)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.mkdir(parents=True, exist_ok=True)
    return path / "admin_credentials.txt"


def _write_first_run_credentials(username: str, password: str) -> None:
    target = _credential_file()
    target.write_text(
        "PAAS 管理界面\n"
        "=============\n"
        "管理界面：http://<服务器IP>:8000/admin （本机访问为 http://127.0.0.1:8000/admin）\n"
        f"用户名：{username}\n"
        f"密码：{password}\n"
        "\n请登录后立即在「安全」页修改密码，并删除本文件。\n",
        encoding="utf-8",
    )
    try:
        target.chmod(0o600)
    except OSError:
        pass


def _log_first_run_welcome(username: str, password: str) -> None:
    line = "=" * 46
    log.warning(
        "\n%s\nPAAS 首次部署完成，请用以下信息登录管理界面：\n"
        "管理界面：http://<服务器IP>:8000/admin （本机访问 http://127.0.0.1:8000/admin）\n"
        "用户名：%s\n密码：%s\n"
        "（密码已同时写入 data/admin_credentials.txt，登录后请及时修改密码并删除该文件）\n%s",
        line, username, password, line,
    )


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
    q = """
        SELECT b.*, COALESCE(u.username, '无主') AS owner_name
        FROM bot_configs b
        LEFT JOIN admin_users u ON u.id = b.owner_id
    """
    params: list[Any] = []
    if not admin:
        q += " WHERE b.owner_id = ?"
        params.append(viewer_id)
    q += " ORDER BY b.platform, b.id"
    out = []
    for row in conn.execute(q, params).fetchall():
        d = dict(row)
        d["config"] = get_bot_fields(conn, row["bot_id"])
        out.append(d)
    return out


def bot_ids_for_owner(conn, owner_id: int) -> set[str]:
    """返回某登录账号拥有的全部 bot_id（namespace）。"""
    rows = conn.execute(
        "SELECT bot_id FROM bot_configs WHERE owner_id = ?", (owner_id,)
    ).fetchall()
    return {r["bot_id"] for r in rows}


def scoped_count(conn, table: str, bot_ids: set[str] | None) -> int:
    """统计某表行数；bot_ids 为 None 表示全部，空集合返回 0。"""
    if bot_ids is None:
        return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    if not bot_ids:
        return 0
    ph = ",".join("?" * len(bot_ids))
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM {table} WHERE namespace IN ({ph})",
        tuple(bot_ids),
    ).fetchone()["n"]


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
        merged[f"has_{field}"] = bool(stored.get(field))
    return merged


def create_bot(
    conn, owner_id: int, platform: str, name: str, fields: dict[str, Any], enabled: bool
) -> tuple[bool, str]:
    if platform not in CONFIG_DEFAULTS:
        return False, "未知平台"
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM bot_configs WHERE platform = ? AND owner_id = ?",
        (platform, owner_id),
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
    conn.execute("DELETE FROM user_ai_settings WHERE user_id = ?", (user_id,))
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


# ---------- AI 设置 ----------

def get_ai_settings(conn) -> dict:
    migrate_ai_settings(conn)
    s = settings_store.get_all(conn)
    return {
        "local_enabled": s.get("ai_local_enabled") == "1",
        "local_model": s.get("ai_local_model", "qwen2.5:0.5b"),
        "local_base_url": s.get("ai_local_base_url", "http://localhost:11434"),
        "cloud_enabled": s.get("ai_cloud_enabled") == "1",
        "cloud_model": s.get("ai_cloud_model", ""),
        "cloud_base_url": s.get("ai_cloud_base_url", ""),
        "has_api_key": bool(s.get("ai_cloud_api_key", "")),
        "order": s.get("ai_order", "rules,local,cloud"),
        "timeout_seconds": s.get("ai_timeout_seconds", "45"),
    }


def migrate_ai_settings(conn) -> None:
    rows = {
        r["key"]: r["value"]
        for r in conn.execute("SELECT key, value FROM settings").fetchall()
    }
    if "ai_mode" not in rows:
        return
    mode = rows.get("ai_mode", "off")
    # 注意：ensure_default_settings 在启动时已插入 ai_local_enabled=0 等新字段，
    # 所以不能以"新字段不存在"作为迁移条件；只按 ai_mode 是否存在判断。
    # 若用户升级后已手动配置过（新开关已是 1），保留用户配置，仅补齐缺省值。
    if mode == "ollama":
        if settings_store.get_setting(conn, "ai_local_enabled", "0") != "1":
            settings_store.set_setting(conn, "ai_local_enabled", "1")
        settings_store.set_setting(conn, "ai_order", "rules,local,cloud")
        if not (settings_store.get_setting(conn, "ai_local_model", "") or ""):
            settings_store.set_setting(
                conn, "ai_local_model", rows.get("ai_model") or "qwen2.5:0.5b"
            )
        if not (settings_store.get_setting(conn, "ai_local_base_url", "") or ""):
            settings_store.set_setting(
                conn, "ai_local_base_url", rows.get("ai_base_url") or "http://localhost:11434"
            )
    elif mode == "cloud":
        if settings_store.get_setting(conn, "ai_cloud_enabled", "0") != "1":
            settings_store.set_setting(conn, "ai_cloud_enabled", "1")
        settings_store.set_setting(conn, "ai_order", "rules,cloud,local")
        if not (settings_store.get_setting(conn, "ai_cloud_model", "") or ""):
            settings_store.set_setting(conn, "ai_cloud_model", rows.get("ai_model") or "")
        if not (settings_store.get_setting(conn, "ai_cloud_base_url", "") or ""):
            settings_store.set_setting(conn, "ai_cloud_base_url", rows.get("ai_base_url") or "")
        if not (settings_store.get_setting(conn, "ai_cloud_api_key", "") or ""):
            old_key = rows.get("ai_api_key") or ""
            if old_key:
                settings_store.set_setting(conn, "ai_cloud_api_key", old_key)
    conn.execute("DELETE FROM settings WHERE key='ai_mode'")
    conn.commit()


def put_ai_settings(conn, updates: dict[str, Any]) -> list[str]:
    applied: list[str] = []
    mapping = {
        "local_enabled": "ai_local_enabled",
        "local_model": "ai_local_model",
        "local_base_url": "ai_local_base_url",
        "cloud_enabled": "ai_cloud_enabled",
        "cloud_model": "ai_cloud_model",
        "cloud_base_url": "ai_cloud_base_url",
        "timeout_seconds": "ai_timeout_seconds",
    }
    for key, skey in mapping.items():
        if key in updates:
            value = updates[key]
            if isinstance(value, bool):
                value = "1" if value else "0"
            settings_store.set_setting(conn, skey, str(value))
            applied.append(skey)
    if "order" in updates:
        order = ",".join(p.strip() for p in str(updates["order"]).split(",") if p.strip())
        if order in VALID_AI_ORDERS:
            settings_store.set_setting(conn, "ai_order", order)
            applied.append("ai_order")
    if updates.get("api_key") not in (None, "", "••••••••"):
        from paas.security import encrypt_json

        settings_store.set_setting(conn, "ai_cloud_api_key", encrypt_json({"key": str(updates["api_key"])}))
        applied.append("ai_cloud_api_key")
    return applied


def get_user_ai_settings(conn, user_id: int, admin: bool) -> dict:
    """当前登录账号的 AI 配置视图；管理员即全局配置。"""
    if admin:
        return get_ai_settings(conn)
    row = conn.execute(
        "SELECT * FROM user_ai_settings WHERE user_id = ?", (user_id,)
    ).fetchone()
    return {
        "local_enabled": bool(row and row["local_enabled"]),
        "local_model": settings_store.get_setting(conn, "ai_local_model", "qwen2.5:0.5b") or "qwen2.5:0.5b",
        "local_base_url": (settings_store.get_setting(conn, "ai_local_base_url", "http://localhost:11434") or "http://localhost:11434").strip(),
        "cloud_enabled": bool(row and row["cloud_enabled"]),
        "cloud_model": (row["cloud_model"] if row else "") or "",
        "cloud_base_url": (row["cloud_base_url"] if row else "") or "",
        "has_api_key": bool(row and row["api_key_enc"]),
        "order": (row["ai_order"] if row else "") or "rules,local,cloud",
        "timeout_seconds": settings_store.get_setting(conn, "ai_timeout_seconds", "45") or "45",
    }


def put_user_ai_settings(conn, user_id: int, admin: bool, updates: dict[str, Any]) -> list[str]:
    """保存当前登录账号的 AI 配置；管理员直接写全局。"""
    if admin:
        return put_ai_settings(conn, updates)
    row = conn.execute(
        "SELECT * FROM user_ai_settings WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO user_ai_settings (user_id) VALUES (?)", (user_id,)
        )
        row = conn.execute(
            "SELECT * FROM user_ai_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
    applied: list[str] = []
    local_enabled = 1 if row["local_enabled"] else 0
    if isinstance(updates.get("local_enabled"), bool):
        local_enabled = 1 if updates["local_enabled"] else 0
        applied.append("user_ai_local_enabled")
    cloud_enabled = 1 if row["cloud_enabled"] else 0
    if isinstance(updates.get("cloud_enabled"), bool):
        cloud_enabled = 1 if updates["cloud_enabled"] else 0
        applied.append("user_ai_cloud_enabled")
    cloud_model = (row["cloud_model"] or "").strip()
    if updates.get("cloud_model") is not None:
        cloud_model = str(updates["cloud_model"]).strip()
        applied.append("user_ai_cloud_model")
    cloud_base_url = (row["cloud_base_url"] or "").strip()
    if updates.get("cloud_base_url") is not None:
        cloud_base_url = str(updates["cloud_base_url"]).strip()
        applied.append("user_ai_cloud_base_url")
    api_key_enc = row["api_key_enc"] or ""
    new_key = updates.get("api_key")
    if new_key not in (None, "", "••••••••"):
        api_key_enc = encrypt_json({"key": str(new_key)})
        applied.append("user_ai_cloud_api_key")
    ai_order = (row["ai_order"] or "").strip() or "rules,local,cloud"
    if updates.get("order") is not None:
        order = ",".join(p.strip() for p in str(updates["order"]).split(",") if p.strip())
        if order in VALID_AI_ORDERS:
            ai_order = order
            applied.append("user_ai_order")
    conn.execute(
        """
        UPDATE user_ai_settings
        SET local_enabled = ?, cloud_enabled = ?, cloud_model = ?, cloud_base_url = ?,
            api_key_enc = ?, ai_order = ?, updated_at = datetime('now')
        WHERE user_id = ?
        """,
        (local_enabled, cloud_enabled, cloud_model, cloud_base_url, api_key_enc, ai_order, user_id),
    )
    conn.commit()
    return applied


def effective_ai_settings(conn, namespace: str) -> dict:
    """按消息所属机器人的归属账号解析实际 AI 配置。

    管理员（或无对应机器人的遗留命名空间）用全局配置；普通用户用自己的
    配置行（默认全关），本地模型名/地址与超时仍取全局。
    """
    global_local_model = settings_store.get_setting(conn, "ai_local_model", "qwen2.5:0.5b") or "qwen2.5:0.5b"
    global_local_base_url = (
        settings_store.get_setting(conn, "ai_local_base_url", "http://localhost:11434") or "http://localhost:11434"
    ).strip()
    global_timeout = settings_store.get_setting(conn, "ai_timeout_seconds", "45") or "45"
    global_order = settings_store.get_setting(conn, "ai_order", "rules,local,cloud") or "rules,local,cloud"

    def global_cfg() -> dict:
        return {
            "local_enabled": settings_store.get_setting(conn, "ai_local_enabled", "0") == "1",
            "local_model": global_local_model,
            "local_base_url": global_local_base_url,
            "cloud_enabled": settings_store.get_setting(conn, "ai_cloud_enabled", "0") == "1",
            "cloud_model": settings_store.get_setting(conn, "ai_cloud_model", "") or "",
            "cloud_base_url": (settings_store.get_setting(conn, "ai_cloud_base_url", "") or "").strip(),
            "api_key_enc": settings_store.get_setting(conn, "ai_cloud_api_key", "") or "",
            "order": global_order,
            "timeout_seconds": global_timeout,
        }

    bot = conn.execute(
        "SELECT owner_id FROM bot_configs WHERE bot_id = ?", (namespace,)
    ).fetchone()
    if bot is None:
        return global_cfg()
    owner = conn.execute(
        "SELECT role FROM admin_users WHERE id = ?", (bot["owner_id"],)
    ).fetchone()
    if owner is not None and owner["role"] == "admin":
        return global_cfg()
    row = conn.execute(
        "SELECT * FROM user_ai_settings WHERE user_id = ?", (bot["owner_id"],)
    ).fetchone()
    return {
        "local_enabled": bool(row and row["local_enabled"]),
        "local_model": global_local_model,
        "local_base_url": global_local_base_url,
        "cloud_enabled": bool(row and row["cloud_enabled"]),
        "cloud_model": (row["cloud_model"] if row else "") or "",
        "cloud_base_url": (row["cloud_base_url"] if row else "") or "",
        "api_key_enc": (row["api_key_enc"] if row else "") or "",
        "order": (row["ai_order"] if row else "") or "rules,local,cloud",
        "timeout_seconds": global_timeout,
    }


def bot_users(conn, namespace: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT user_id FROM user_chats WHERE namespace = ? ORDER BY user_id",
        (namespace,),
    ).fetchall()
    return [r["user_id"] for r in rows]
