import datetime
import sqlite3
from pathlib import Path

from paas.config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    icon TEXT DEFAULT '',
    keywords TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    expense_date TEXT NOT NULL,
    category_id INTEGER NOT NULL,
    account_id INTEGER,
    to_account_id INTEGER,
    tx_type TEXT NOT NULL DEFAULT 'expense',
    amount_cents INTEGER NOT NULL,
    description TEXT NOT NULL,
    platform TEXT NOT NULL,
    message_id TEXT NOT NULL,
    raw_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'normal',
    ref_id INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (category_id) REFERENCES categories(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_msg ON expenses (platform, message_id);
CREATE INDEX IF NOT EXISTS idx_expense_query ON expenses (user_id, expense_date);
CREATE INDEX IF NOT EXISTS idx_expense_created ON expenses (user_id, created_at);

CREATE TABLE IF NOT EXISTS daily_status (
    user_id TEXT NOT NULL,
    status_date TEXT NOT NULL,
    reported INTEGER DEFAULT 0,
    zero_confirmed INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0,
    reminder_count INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, status_date)
);

CREATE TABLE IF NOT EXISTS pending_actions (
    user_id TEXT PRIMARY KEY,
    action_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_configs (
    platform TEXT PRIMARY KEY,
    enabled INTEGER DEFAULT 0,
    config_enc TEXT NOT NULL DEFAULT '',
    status TEXT DEFAULT 'stopped',
    last_error TEXT DEFAULT '',
    last_message_at TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_chats (
    user_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    last_seen_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, platform, chat_id)
);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    message_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_type TEXT NOT NULL,
    total_rows INTEGER DEFAULT 0,
    success_rows INTEGER DEFAULT 0,
    failed_rows INTEGER DEFAULT 0,
    errors TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    initial_balance_cents INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS raw_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    message_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(platform, message_id)
);
"""

BASE_CATEGORIES = [
    (1, "餐饮", "🍜", "饭,面,肯德基,麦当劳,汉堡,火锅,烧烤,奶茶,咖啡,午餐,晚餐,早餐,外卖,AA,吃,零食,食堂,夜宵,水果"),
    (2, "交通", "🚕", "打车,滴滴,出租车,地铁,公交,加油,停车,油费,高铁,火车,机票,骑行,单车,过路费,代驾"),
    (3, "购物", "🛒", "买,衣服,鞋,数码,淘宝,京东,拼多多,超市,日用,化妆品,护肤,家电,家具,快递"),
    (4, "娱乐", "🎮", "电影,游戏,充值,steam,网吧,门票,KTV,剧本杀,健身,运动,旅游,酒店,演唱会,会员,视频"),
    (5, "生活", "🏠", "水电,房租,物业,话费,网费,理发,洗衣,燃气,宽带,手机,维修,家政,快递费"),
    (6, "医疗", "💊", "药,医院,挂号,门诊,体检,牙科,中医,疫苗,医疗"),
    (7, "其他", "📦", ""),
]


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else Path(settings.db_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode = WAL")
    migrate_db(conn)
    for cat in BASE_CATEGORIES:
        conn.execute(
            """
            INSERT INTO categories (id, name, icon, keywords, sort_order)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                icon = excluded.icon,
                keywords = excluded.keywords,
                sort_order = excluded.sort_order
            """,
            (cat[0], cat[1], cat[2], cat[3], cat[0]),
        )
    conn.commit()


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def migrate_db(conn: sqlite3.Connection) -> None:
    """从 v1（entry_type）平滑迁移到 v2（账户/类型/状态/原始消息）。"""
    cols = _column_names(conn, "expenses")
    if "entry_type" in cols and "tx_type" not in cols:
        conn.execute("ALTER TABLE expenses RENAME COLUMN entry_type TO tx_type")
        cols = _column_names(conn, "expenses")
    additions = {
        "account_id": "INTEGER",
        "to_account_id": "INTEGER",
        "status": "TEXT NOT NULL DEFAULT 'normal'",
        "ref_id": "INTEGER",
        "raw_text": "TEXT NOT NULL DEFAULT ''",
    }
    for name, ddl in additions.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE expenses ADD COLUMN {name} {ddl}")
    conn.execute("UPDATE expenses SET tx_type='expense' WHERE tx_type IS NULL OR tx_type=''")
    conn.execute("UPDATE expenses SET status='normal' WHERE status IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_expense_status ON expenses (user_id, status)")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()


def execute_safe_backup(conn: sqlite3.Connection, backup_dir: Path | str | None = None) -> Path:
    """SQLite 官方在线备份 API：事务级热备份，绝不直接拷贝 .db 文件。"""
    bdir = Path(backup_dir) if backup_dir is not None else Path(settings.backup_dir)
    if not bdir.is_absolute():
        bdir = Path.cwd() / bdir
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    target = bdir / f"account_{stamp}.db"
    with sqlite3.connect(target) as dst:
        conn.backup(dst)
    return target


def prune_backups(backup_dir: Path | str | None = None, keep_days: int = 30) -> int:
    bdir = Path(backup_dir) if backup_dir is not None else Path(settings.backup_dir)
    if not bdir.is_absolute():
        bdir = Path.cwd() / bdir
    if not bdir.exists():
        return 0
    cutoff = datetime.datetime.now() - datetime.timedelta(days=keep_days)
    removed = 0
    for f in bdir.glob("account_*.db"):
        try:
            mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime)
            if mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed
