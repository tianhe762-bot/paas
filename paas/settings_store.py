from typing import Any

DEFAULTS: dict[str, str] = {
    "timezone": "Asia/Shanghai",
    "reminder_hours": "21,22,0",
    "reminder_message": "📝 今天的消费记录了吗？回复「无消费」确认今日无消费，或直接发送消费明细；回复「跳过」跳过今日。",
    "dedup_window_seconds": "30",
    "pending_ttl_seconds": "600",
    "amount_rounding": "2",
    "backup_keep_days": "30",
    "backup_hour": "3",
    "ai_mode": "off",
    "ai_model": "qwen2.5:0.5b",
    "ai_base_url": "",
    "ai_api_key": "",
    "ai_timeout_seconds": "45",
}


def ensure_default_settings(conn) -> None:
    for key, value in DEFAULTS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )
    conn.commit()


def get_setting(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return row["value"]
    return default if default is not None else DEFAULTS.get(key)


def get_int(conn, key: str, default: int = 0) -> int:
    try:
        return int(get_setting(conn, key) or default)
    except (TypeError, ValueError):
        return default


def get_all(conn) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    merged = dict(DEFAULTS)
    for row in rows:
        merged[row["key"]] = row["value"]
    return merged


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def set_many(conn, updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        set_setting(conn, key, str(value))
