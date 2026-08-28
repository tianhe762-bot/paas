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
    namespace TEXT NOT NULL DEFAULT 'default',
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

CREATE TABLE IF NOT EXISTS daily_status (
    namespace TEXT NOT NULL DEFAULT 'default',
    user_id TEXT NOT NULL,
    status_date TEXT NOT NULL,
    reported INTEGER DEFAULT 0,
    zero_confirmed INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0,
    reminder_count INTEGER DEFAULT 0,
    PRIMARY KEY (namespace, user_id, status_date)
);

CREATE TABLE IF NOT EXISTS pending_actions (
    namespace TEXT NOT NULL DEFAULT 'default',
    user_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    PRIMARY KEY (namespace, user_id)
);

CREATE TABLE IF NOT EXISTS bot_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    bot_id TEXT NOT NULL,
    owner_id INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL DEFAULT '',
    enabled INTEGER DEFAULT 0,
    config_enc TEXT NOT NULL DEFAULT '',
    status TEXT DEFAULT 'stopped',
    last_error TEXT DEFAULT '',
    last_message_at TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(platform, bot_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_ai_settings (
    user_id INTEGER PRIMARY KEY,
    local_enabled INTEGER NOT NULL DEFAULT 0,
    cloud_enabled INTEGER NOT NULL DEFAULT 0,
    cloud_model TEXT NOT NULL DEFAULT '',
    cloud_base_url TEXT NOT NULL DEFAULT '',
    api_key_enc TEXT NOT NULL DEFAULT '',
    ai_order TEXT NOT NULL DEFAULT 'rules,local,cloud',
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_chats (
    namespace TEXT NOT NULL DEFAULT 'default',
    user_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    last_seen_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (namespace, user_id, platform, chat_id)
);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL DEFAULT 'default',
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
    namespace TEXT NOT NULL DEFAULT 'default',
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '',
    initial_balance_cents INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(namespace, user_id, name)
);

CREATE TABLE IF NOT EXISTS account_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL,
    name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '',
    initial_balance_cents INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(namespace, name)
);

CREATE TABLE IF NOT EXISTS raw_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL DEFAULT 'default',
    platform TEXT NOT NULL,
    message_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    reply TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(namespace, platform, message_id)
);

CREATE TABLE IF NOT EXISTS import_staging (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL DEFAULT 'default',
    user_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    message_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
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
    """从 v1/v2 平滑迁移到 v3（多机器人命名空间 + 用户角色）。"""
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
    _migrate_v3(conn)
    _migrate_v4(conn)
    conn.commit()


def _migrate_v4(conn: sqlite3.Connection) -> None:
    if "aliases" not in _column_names(conn, "accounts"):
        conn.execute("ALTER TABLE accounts ADD COLUMN aliases TEXT NOT NULL DEFAULT ''")
    if "reply" not in _column_names(conn, "raw_messages"):
        conn.execute("ALTER TABLE raw_messages ADD COLUMN reply TEXT NOT NULL DEFAULT ''")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS account_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            namespace TEXT NOT NULL,
            name TEXT NOT NULL,
            aliases TEXT NOT NULL DEFAULT '',
            initial_balance_cents INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(namespace, name)
        );
        CREATE TABLE IF NOT EXISTS import_staging (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            namespace TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            message_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            data_json TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    conn.execute("PRAGMA user_version = 4")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    # 用户角色
    if "role" not in _column_names(conn, "admin_users"):
        conn.execute("ALTER TABLE admin_users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")

    # 流水表：命名空间列 + 唯一索引
    if "namespace" not in _column_names(conn, "expenses"):
        conn.execute("ALTER TABLE expenses ADD COLUMN namespace TEXT NOT NULL DEFAULT 'default'")
    conn.execute("DROP INDEX IF EXISTS uq_platform_msg")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_ns_platform_msg ON expenses (namespace, platform, message_id)")
    conn.execute("DROP INDEX IF EXISTS idx_expense_query")
    conn.execute("DROP INDEX IF EXISTS idx_expense_created")
    conn.execute("DROP INDEX IF EXISTS idx_expense_status")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_expense_query ON expenses (namespace, user_id, expense_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_expense_created ON expenses (namespace, user_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_expense_status ON expenses (namespace, user_id, status)")

    # 需要重建主键/唯一约束的表
    _rebuild_table(
        conn, "daily_status",
        """
        CREATE TABLE daily_status (
            namespace TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            status_date TEXT NOT NULL,
            reported INTEGER DEFAULT 0,
            zero_confirmed INTEGER DEFAULT 0,
            skipped INTEGER DEFAULT 0,
            reminder_count INTEGER DEFAULT 0,
            PRIMARY KEY (namespace, user_id, status_date)
        )
        """,
        "namespace, user_id, status_date, reported, zero_confirmed, skipped, reminder_count",
        "user_id, status_date, reported, zero_confirmed, skipped, reminder_count",
        "default",
    )
    _rebuild_table(
        conn, "pending_actions",
        """
        CREATE TABLE pending_actions (
            namespace TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            action_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            PRIMARY KEY (namespace, user_id)
        )
        """,
        "namespace, user_id, action_type, payload, created_at, expires_at",
        "user_id, action_type, payload, created_at, expires_at",
        "default",
    )
    _rebuild_table(
        conn, "user_chats",
        """
        CREATE TABLE user_chats (
            namespace TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            last_seen_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (namespace, user_id, platform, chat_id)
        )
        """,
        "namespace, user_id, platform, chat_id, last_seen_at",
        "user_id, platform, chat_id, last_seen_at",
        "default",
    )
    _rebuild_table(
        conn, "accounts",
        """
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            namespace TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            initial_balance_cents INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(namespace, user_id, name)
        )
        """,
        "namespace, user_id, name, initial_balance_cents, sort_order, created_at",
        "user_id, name, initial_balance_cents, sort_order, created_at",
        "default",
    )

    # 仅加列
    if "namespace" not in _column_names(conn, "imports"):
        conn.execute("ALTER TABLE imports ADD COLUMN namespace TEXT NOT NULL DEFAULT 'default'")
    _rebuild_table(
        conn, "raw_messages",
        """
        CREATE TABLE raw_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            namespace TEXT NOT NULL DEFAULT 'default',
            platform TEXT NOT NULL,
            message_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(namespace, platform, message_id)
        )
        """,
        "namespace, platform, message_id, user_id, content, created_at",
        "platform, message_id, user_id, content, created_at",
        "default",
    )

    # 机器人表：旧单例结构 → 新多实例结构
    bcols = _column_names(conn, "bot_configs")
    if "bot_id" not in bcols:
        owner = conn.execute("SELECT id FROM admin_users ORDER BY id LIMIT 1").fetchone()
        owner_id = owner["id"] if owner else 0
        conn.execute("ALTER TABLE bot_configs RENAME TO bot_configs_old")
        conn.execute(
            """
            CREATE TABLE bot_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                bot_id TEXT NOT NULL,
                owner_id INTEGER NOT NULL DEFAULT 0,
                name TEXT NOT NULL DEFAULT '',
                enabled INTEGER DEFAULT 0,
                config_enc TEXT NOT NULL DEFAULT '',
                status TEXT DEFAULT 'stopped',
                last_error TEXT DEFAULT '',
                last_message_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(platform, bot_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO bot_configs (platform, bot_id, owner_id, name, enabled, config_enc, status, last_error, last_message_at, updated_at)
            SELECT platform, 'default', ?, platform, enabled, config_enc, status, last_error, last_message_at, updated_at
            FROM bot_configs_old
            """,
            (owner_id,),
        )
        conn.execute("DROP TABLE bot_configs_old")

    conn.execute("PRAGMA user_version = 3")


def _rebuild_table(conn, name, create_sql, new_cols, old_cols, ns_value):
    if "namespace" in _column_names(conn, name):
        return
    conn.execute(f"ALTER TABLE {name} RENAME TO {name}_old")
    conn.execute(create_sql)
    conn.execute(
        f"INSERT INTO {name} ({new_cols}) SELECT '{ns_value}', {old_cols} FROM {name}_old"
    )
    conn.execute(f"DROP TABLE {name}_old")


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
