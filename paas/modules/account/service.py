import datetime
import json
import sqlite3
from typing import Any

from paas import timeutil
from paas.models import CategoryRow, ImportResult, ParsedItem
from paas.modules.account.importer import parse_import
from paas.modules.account.parser import ACCOUNTS, PRESET_BANKS
from paas.modules.account.queries import day_summary

ZERO_PHRASES = {
    "今天没花钱", "没花钱", "无消费", "不消费", "今天无消费", "0", "0元",
    "今天没有消费", "没有消费",
}
SKIP_PHRASES = {"跳过", "跳过今天", "不记了", "今天不记", "今天不记了"}
CONFIRM_PHRASES = {"是", "确认", "是的", "对", "记录", "确定"}
CANCEL_PHRASES = {"否", "取消", "不", "不用", "不记录", "取消记录"}

DEFAULT_ACCOUNTS = ["微信", "支付宝", "银行卡", "现金", "信用卡"]


def load_categories(conn) -> list[CategoryRow]:
    rows = conn.execute(
        "SELECT id, name, icon, keywords FROM categories ORDER BY sort_order, id"
    ).fetchall()
    return [
        CategoryRow(
            id=r["id"],
            name=r["name"],
            icon=r["icon"],
            keywords=[k for k in (r["keywords"] or "").split(",") if k],
        )
        for r in rows
    ]


def format_money(cents: int) -> str:
    return f"{abs(cents) / 100:.2f}"


def format_signed_money(cents: int) -> str:
    return f"{cents / 100:.2f}"


def _now_utc_str() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _upsert_daily_status(conn, namespace: str, user_id: str, day_iso: str, **flags: int) -> None:
    sets = ", ".join(f"{k}=excluded.{k}" for k in flags)
    cols = ", ".join(["namespace", "user_id", "status_date", *flags.keys()])
    placeholders = ", ".join(["?"] * (3 + len(flags)))
    conn.execute(
        f"""
        INSERT INTO daily_status ({cols}) VALUES ({placeholders})
        ON CONFLICT(namespace, user_id, status_date) DO UPDATE SET {sets}
        """,
        (namespace, user_id, day_iso, *flags.values()),
    )


def save_raw_message(conn, namespace: str, platform: str, message_id: str, user_id: str, content: str) -> None:
    if not content:
        content = "[文件消息]"
    conn.execute(
        """
        INSERT OR IGNORE INTO raw_messages (namespace, platform, message_id, user_id, content)
        VALUES (?, ?, ?, ?, ?)
        """,
        (namespace, platform, message_id, user_id, content[:2000]),
    )


# ---------- 账户 ----------

def list_account_templates(conn, namespace: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, aliases, initial_balance_cents, sort_order "
        "FROM account_templates WHERE namespace = ? ORDER BY sort_order, id",
        (namespace,),
    ).fetchall()
    return [dict(r) for r in rows]


def replace_account_templates(
    conn, namespace: str, templates: list[dict]
) -> tuple[bool, str]:
    """整表替换该机器人的账户模板，并同步到已有用户。"""
    existing = {
        r["name"]: r
        for r in conn.execute(
            "SELECT name, aliases, initial_balance_cents, sort_order "
            "FROM account_templates WHERE namespace = ?",
            (namespace,),
        ).fetchall()
    }
    wanted = {t["name"]: t for t in templates if t.get("name")}
    # 被移除且已被流水引用的账户禁止删除
    removed = [name for name in existing if name not in wanted]
    if removed:
        placeholders = ",".join("?" * len(removed))
        refs = conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM expenses e
            JOIN accounts a ON a.id = e.account_id
            WHERE e.namespace = ? AND a.name IN ({placeholders})
            """,
            (namespace, *removed),
        ).fetchone()["n"]
        if refs:
            return False, f"以下账户已有流水引用，无法删除：{', '.join(removed)}"
    for name, row in wanted.items():
        conn.execute(
            """
            INSERT INTO account_templates (namespace, name, aliases, initial_balance_cents, sort_order)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(namespace, name) DO UPDATE SET
                aliases = excluded.aliases,
                initial_balance_cents = excluded.initial_balance_cents,
                sort_order = excluded.sort_order
            """,
            (
                namespace,
                name,
                row.get("aliases", "") or "",
                int(row.get("initial_balance_cents", 0) or 0),
                int(row.get("sort_order", 0) or 0),
            ),
        )
    for name in removed:
        conn.execute(
            "DELETE FROM account_templates WHERE namespace = ? AND name = ?",
            (namespace, name),
        )
        conn.execute(
            "DELETE FROM accounts WHERE namespace = ? AND name = ?",
            (namespace, name),
        )
    _sync_account_templates_to_users(conn, namespace)
    conn.commit()
    return True, "已保存"


def _sync_account_templates_to_users(conn, namespace: str) -> None:
    users = [
        r["user_id"]
        for r in conn.execute(
            "SELECT DISTINCT user_id FROM accounts WHERE namespace = ?", (namespace,)
        ).fetchall()
    ]
    for user_id in users:
        existing = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM accounts WHERE namespace = ? AND user_id = ?",
                (namespace, user_id),
            ).fetchall()
        }
        for row in conn.execute(
            "SELECT name, aliases, initial_balance_cents, sort_order "
            "FROM account_templates WHERE namespace = ?",
            (namespace,),
        ).fetchall():
            if row["name"] in existing:
                conn.execute(
                    "UPDATE accounts SET aliases = ?, sort_order = ? "
                    "WHERE namespace = ? AND user_id = ? AND name = ?",
                    (row["aliases"] or "", row["sort_order"] or 0, namespace, user_id, row["name"]),
                )
            else:
                conn.execute(
                    "INSERT INTO accounts (namespace, user_id, name, aliases, initial_balance_cents, sort_order) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (namespace, user_id, row["name"], row["aliases"] or "",
                     row["initial_balance_cents"], row["sort_order"] or 0),
                )


def backfill_preview(
    conn, namespace: str, user_id: str, keyword: str
) -> dict:
    pattern = f"%{keyword}%"
    rows = conn.execute(
        """
        SELECT e.id, e.expense_date, e.amount_cents, e.description
        FROM expenses e
        WHERE e.namespace = ? AND e.user_id = ? AND e.account_id IS NULL
          AND (e.description LIKE ? OR e.raw_text LIKE ?)
        ORDER BY e.expense_date DESC LIMIT 200
        """,
        (namespace, user_id, pattern, pattern),
    ).fetchall()
    total = conn.execute(
        """
        SELECT COUNT(*) AS n FROM expenses e
        WHERE e.namespace = ? AND e.user_id = ? AND e.account_id IS NULL
          AND (e.description LIKE ? OR e.raw_text LIKE ?)
        """,
        (namespace, user_id, pattern, pattern),
    ).fetchone()["n"]
    return {
        "keyword": keyword,
        "matched": int(total),
        "samples": [dict(r) for r in rows[:10]],
    }


def backfill_apply(
    conn, namespace: str, user_id: str, mappings: list[dict]
) -> dict:
    results = []
    for m in mappings:
        keyword = (m.get("keyword") or "").strip()
        account = (m.get("account") or "").strip()
        if not keyword or not account:
            continue
        account_id = get_or_create_account(conn, namespace, user_id, account)
        pattern = f"%{keyword}%"
        cur = conn.execute(
            """
            UPDATE expenses SET account_id = ?
            WHERE namespace = ? AND user_id = ? AND account_id IS NULL
              AND (description LIKE ? OR raw_text LIKE ?)
            """,
            (account_id, namespace, user_id, pattern, pattern),
        )
        results.append({"keyword": keyword, "account": account, "applied": cur.rowcount})
    conn.commit()
    return {"results": results}

def ensure_default_accounts(conn, namespace: str, user_id: str) -> None:
    existing = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM accounts WHERE namespace = ? AND user_id = ?",
            (namespace, user_id),
        ).fetchall()
    }
    for idx, name in enumerate(DEFAULT_ACCOUNTS):
        if name not in existing:
            conn.execute(
                "INSERT INTO accounts (namespace, user_id, name, initial_balance_cents, sort_order) "
                "VALUES (?, ?, ?, 0, ?)",
                (namespace, user_id, name, idx),
            )
    for row in conn.execute(
        "SELECT name, aliases, initial_balance_cents, sort_order FROM account_templates WHERE namespace = ?",
        (namespace,),
    ).fetchall():
        if row["name"] not in existing:
            conn.execute(
                "INSERT INTO accounts (namespace, user_id, name, aliases, initial_balance_cents, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (namespace, user_id, row["name"], row["aliases"] or "", row["initial_balance_cents"], row["sort_order"] or 0),
            )
    conn.commit()


def account_keywords(conn, namespace: str, user_id: str) -> list[tuple[str, list[str]]]:
    """该用户的账户匹配清单：自定义账户（名称+别名）+ 默认账户 + 预置银行。"""
    out: list[tuple[str, list[str]]] = []
    for r in conn.execute(
        "SELECT name, aliases FROM accounts WHERE namespace = ? AND user_id = ? ORDER BY sort_order, id",
        (namespace, user_id),
    ).fetchall():
        kws = [r["name"]] + [a for a in (r["aliases"] or "").split(",") if a]
        out.append((r["name"], kws))
    names = {n for n, _ in out}
    for name, kws in list(ACCOUNTS) + list(PRESET_BANKS):
        if name not in names:
            out.append((name, kws))
    return out


def get_or_create_account(conn, namespace: str, user_id: str, name: str) -> int | None:
    name = (name or "").strip()
    if not name:
        return None
    row = conn.execute(
        "SELECT id FROM accounts WHERE namespace = ? AND user_id = ? AND name = ?",
        (namespace, user_id, name),
    ).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO accounts (namespace, user_id, name, initial_balance_cents, sort_order) "
        "VALUES (?, ?, ?, 0, 1000)",
        (namespace, user_id, name),
    )
    conn.commit()
    return cur.lastrowid


def set_account_initial_balance(
    conn, namespace: str, user_id: str, account: str, amount_cents: int
) -> int | None:
    account_id = get_or_create_account(conn, namespace, user_id, account)
    if account_id is None:
        return None
    conn.execute(
        "UPDATE accounts SET initial_balance_cents = ? WHERE id = ?",
        (amount_cents, account_id),
    )
    conn.commit()
    return account_id


def account_balances(conn, namespace: str, user_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.id, a.name, a.initial_balance_cents,
               COALESCE(SUM(
                   CASE e.tx_type
                       WHEN 'expense' THEN -e.amount_cents
                       WHEN 'fee' THEN -e.amount_cents
                       WHEN 'income' THEN e.amount_cents
                       WHEN 'refund' THEN e.amount_cents
                       WHEN 'adjust' THEN e.amount_cents
                       WHEN 'transfer_out' THEN -e.amount_cents
                       WHEN 'transfer_in' THEN e.amount_cents
                       ELSE 0 END
               ), 0) AS delta
        FROM accounts a
        LEFT JOIN expenses e
               ON e.account_id = a.id AND e.namespace = a.namespace
              AND e.user_id = a.user_id AND e.status = 'normal'
        WHERE a.namespace = ? AND a.user_id = ?
        GROUP BY a.id, a.name, a.initial_balance_cents
        ORDER BY a.sort_order, a.id
        """,
        (namespace, user_id),
    ).fetchall()
    out = []
    for r in rows:
        balance = int(r["initial_balance_cents"]) + int(r["delta"])
        out.append(
            {
                "id": r["id"],
                "name": r["name"],
                "initial_balance_cents": int(r["initial_balance_cents"]),
                "delta": int(r["delta"]),
                "balance_cents": balance,
            }
        )
    return out


def account_balance_by_name(conn, namespace: str, user_id: str, name: str) -> dict | None:
    for acc in account_balances(conn, namespace, user_id):
        if acc["name"] == name:
            return acc
    return None


def total_assets(conn, namespace: str, user_id: str) -> int:
    return sum(a["balance_cents"] for a in account_balances(conn, namespace, user_id))


# ---------- 记账 ----------

def record_items(
    conn,
    namespace: str,
    user_id: str,
    items: list[ParsedItem],
    platform: str,
    message_id: str,
    raw_text: str = "",
) -> tuple[list[ParsedItem], list[ParsedItem]]:
    ensure_default_accounts(conn, namespace, user_id)
    saved: list[ParsedItem] = []
    skipped: list[ParsedItem] = []
    last_transfer_id: int | None = None
    for idx, item in enumerate(items):
        mid = f"{message_id}:{idx}" if len(items) > 1 else message_id
        try:
            if item.tx_type == "transfer_out":
                from_id = get_or_create_account(conn, namespace, user_id, item.account_name or "未命名")
                to_id = get_or_create_account(conn, namespace, user_id, item.to_account_name or "未命名")
                cur = conn.execute(
                    """
                    INSERT INTO expenses
                        (namespace, user_id, expense_date, category_id, account_id, to_account_id,
                         tx_type, amount_cents, description, platform, message_id, raw_text, status)
                    VALUES (?, ?, ?, ?, ?, ?, 'transfer_out', ?, ?, ?, ?, ?, 'normal')
                    """,
                    (
                        namespace, user_id, item.expense_date.isoformat(), item.category_id,
                        from_id, to_id, item.amount_cents, item.description,
                        platform, mid, raw_text,
                    ),
                )
                out_id = cur.lastrowid
                last_transfer_id = out_id
                conn.execute(
                    """
                    INSERT INTO expenses
                        (namespace, user_id, expense_date, category_id, account_id, to_account_id,
                         tx_type, amount_cents, description, platform, message_id, raw_text, status, ref_id)
                    VALUES (?, ?, ?, ?, ?, ?, 'transfer_in', ?, ?, ?, ?, ?, 'normal', ?)
                    """,
                    (
                        namespace, user_id, item.expense_date.isoformat(), item.category_id,
                        to_id, from_id, item.amount_cents,
                        f"{item.description}（转入）", platform, f"{mid}:in", raw_text, out_id,
                    ),
                )
                saved.append(item)
            else:
                account_id = (
                    get_or_create_account(conn, namespace, user_id, item.account_name)
                    if item.account_name
                    else None
                )
                ref_id = last_transfer_id if item.tx_type == "fee" and last_transfer_id else None
                conn.execute(
                    """
                    INSERT INTO expenses
                        (namespace, user_id, expense_date, category_id, account_id,
                         tx_type, amount_cents, description, platform, message_id, raw_text, status, ref_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'normal', ?)
                    """,
                    (
                        namespace, user_id, item.expense_date.isoformat(), item.category_id, account_id,
                        item.tx_type, item.amount_cents, item.description,
                        platform, mid, raw_text, ref_id,
                    ),
                )
                saved.append(item)
        except sqlite3.IntegrityError:
            skipped.append(item)
    today_iso = timeutil.today().isoformat()
    if any(it.expense_date.isoformat() == today_iso for it in saved):
        _upsert_daily_status(conn, namespace, user_id, today_iso, reported=1)
    conn.commit()
    return saved, skipped


def message_already_processed(conn, namespace: str, platform: str, message_id: str) -> bool:
    row = conn.execute(
        """
        SELECT id FROM expenses
        WHERE namespace = ? AND platform = ? AND (message_id = ? OR message_id LIKE ?)
        LIMIT 1
        """,
        (namespace, platform, message_id, f"{message_id}:%"),
    ).fetchone()
    return row is not None


def find_recent_duplicate(
    conn,
    namespace: str,
    user_id: str,
    item: ParsedItem,
    window_seconds: int,
) -> bool:
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(seconds=window_seconds)
    ).strftime("%Y-%m-%d %H:%M:%S")
    account_row = (
        conn.execute(
            "SELECT id FROM accounts WHERE namespace = ? AND user_id = ? AND name = ?",
            (namespace, user_id, item.account_name),
        ).fetchone()
        if item.account_name
        else None
    )
    row = conn.execute(
        """
        SELECT id FROM expenses
        WHERE namespace = ? AND user_id = ? AND amount_cents = ? AND description = ?
          AND tx_type = ? AND (account_id IS ?) AND expense_date = ? AND created_at >= ?
        LIMIT 1
        """,
        (
            namespace,
            user_id,
            item.amount_cents,
            item.description,
            item.tx_type,
            account_row["id"] if account_row else None,
            item.expense_date.isoformat(),
            cutoff,
        ),
    ).fetchone()
    return row is not None


# ---------- 待确认动作 ----------

def get_pending(conn, namespace: str, user_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM pending_actions WHERE namespace = ? AND user_id = ?",
        (namespace, user_id),
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] <= _now_utc_str():
        conn.execute(
            "DELETE FROM pending_actions WHERE namespace = ? AND user_id = ?",
            (namespace, user_id),
        )
        conn.commit()
        return None
    return dict(row)


def set_pending(
    conn,
    namespace: str,
    user_id: str,
    action_type: str,
    payload: dict[str, Any],
    ttl_seconds: int,
) -> None:
    expires = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(seconds=ttl_seconds)
    ).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT INTO pending_actions (namespace, user_id, action_type, payload, created_at, expires_at)
        VALUES (?, ?, ?, ?, datetime('now'), ?)
        ON CONFLICT(namespace, user_id) DO UPDATE SET
            action_type = excluded.action_type,
            payload = excluded.payload,
            created_at = excluded.created_at,
            expires_at = excluded.expires_at
        """,
        (namespace, user_id, action_type, json.dumps(payload, ensure_ascii=False), expires),
    )
    conn.commit()


def clear_pending(conn, namespace: str, user_id: str) -> None:
    conn.execute(
        "DELETE FROM pending_actions WHERE namespace = ? AND user_id = ?",
        (namespace, user_id),
    )
    conn.commit()


# ---------- 状态与平账 ----------

def mark_zero(conn, namespace: str, user_id: str) -> None:
    _upsert_daily_status(conn, namespace, user_id, timeutil.iso_today(), zero_confirmed=1)
    conn.commit()


def mark_skipped(conn, namespace: str, user_id: str) -> None:
    _upsert_daily_status(conn, namespace, user_id, timeutil.iso_today(), skipped=1)
    conn.commit()


def create_adjustment(
    conn,
    namespace: str,
    user_id: str,
    account: str,
    amount_cents: int,
    note: str,
    platform: str,
    message_id: str,
    raw_text: str = "",
) -> ParsedItem | None:
    categories = load_categories(conn)
    other = next((c for c in categories if c.name == "其他"), categories[-1])
    item = ParsedItem(
        expense_date=timeutil.today(),
        category_id=other.id,
        category_name=other.name,
        category_icon=other.icon,
        account_name=account,
        tx_type="adjust",
        amount_cents=amount_cents,
        description=note or "平账",
    )
    saved, _ = record_items(conn, namespace, user_id, [item], platform, message_id, raw_text)
    return saved[0] if saved else None


def confirm_payload_items(payload: dict) -> list[ParsedItem]:
    return [ParsedItem.model_validate(it) for it in payload.get("items", [])]


# ---------- 撤销 / 修改 ----------

def find_target_record(conn, namespace: str, user_id: str, amount_cents: int | None = None) -> dict | None:
    q = (
        "SELECT e.*, c.name AS category_name, c.icon AS category_icon, "
        "a.name AS account_name FROM expenses e "
        "JOIN categories c ON c.id = e.category_id "
        "LEFT JOIN accounts a ON a.id = e.account_id "
        "WHERE e.namespace = ? AND e.user_id = ? AND e.status = 'normal'"
    )
    params: list[Any] = [namespace, user_id]
    if amount_cents is not None:
        q += " AND e.amount_cents = ?"
        params.append(amount_cents)
    q += " ORDER BY e.id DESC LIMIT 1"
    row = conn.execute(q, params).fetchone()
    return dict(row) if row else None


def void_record(conn, namespace: str, user_id: str, record_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM expenses WHERE id = ? AND namespace = ? AND user_id = ? AND status = 'normal'",
        (record_id, namespace, user_id),
    ).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE expenses SET status = 'voided' WHERE id = ?", (record_id,))
    conn.commit()
    return dict(row)


def modify_record(
    conn,
    namespace: str,
    user_id: str,
    record_id: int,
    new_amount_cents: int,
    new_description: str | None = None,
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM expenses WHERE id = ? AND namespace = ? AND user_id = ? AND status = 'normal'",
        (record_id, namespace, user_id),
    ).fetchone()
    if row is None:
        return None
    if new_description:
        conn.execute(
            "UPDATE expenses SET amount_cents = ?, description = ? WHERE id = ?",
            (new_amount_cents, new_description, record_id),
        )
    else:
        conn.execute(
            "UPDATE expenses SET amount_cents = ? WHERE id = ?",
            (new_amount_cents, record_id),
        )
    conn.commit()
    return dict(row)


# ---------- 导入 ----------

def create_import_staging(
    conn, namespace: str, user_id: str, platform: str, message_id: str,
    filename: str, items: list[ParsedItem],
) -> int:
    cur = conn.execute(
        """
        INSERT INTO import_staging (namespace, user_id, platform, message_id, filename, data_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            namespace, user_id, platform, message_id, filename,
            json.dumps([it.model_dump(mode="json") for it in items], ensure_ascii=False),
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_import_staging(conn, staging_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM import_staging WHERE id = ?", (staging_id,)
    ).fetchone()
    return dict(row) if row else None


def delete_import_staging(conn, staging_id: int) -> None:
    conn.execute("DELETE FROM import_staging WHERE id = ?", (staging_id,))
    conn.commit()


def _item_dedup_key(item: ParsedItem) -> tuple:
    return (
        item.expense_date.isoformat(),
        item.amount_cents,
        item.description,
        item.tx_type,
        item.account_name or "",
    )


def _existing_item(conn, namespace: str, user_id: str, item: ParsedItem) -> bool:
    account_row = (
        conn.execute(
            "SELECT id FROM accounts WHERE namespace = ? AND user_id = ? AND name = ?",
            (namespace, user_id, item.account_name),
        ).fetchone()
        if item.account_name
        else None
    )
    row = conn.execute(
        """
        SELECT id FROM expenses
        WHERE namespace = ? AND user_id = ? AND expense_date = ?
          AND amount_cents = ? AND description = ? AND tx_type = ? AND (account_id IS ?)
        LIMIT 1
        """,
        (
            namespace, user_id, item.expense_date.isoformat(),
            item.amount_cents, item.description, item.tx_type,
            account_row["id"] if account_row else None,
        ),
    ).fetchone()
    return row is not None


def preview_merge(conn, namespace: str, user_id: str, items: list[ParsedItem]) -> dict:
    seen: set[tuple] = set()
    new_count = 0
    skip_count = 0
    dates: list[str] = []
    expense_cents = 0
    income_cents = 0
    categories: dict[str, int] = {}
    for it in items:
        key = _item_dedup_key(it)
        if key in seen or _existing_item(conn, namespace, user_id, it):
            skip_count += 1
            continue
        seen.add(key)
        new_count += 1
        dates.append(it.expense_date.isoformat())
        if it.tx_type in ("expense", "fee"):
            expense_cents += it.amount_cents
        elif it.tx_type in ("income", "refund"):
            income_cents += it.amount_cents
        categories[it.category_name] = categories.get(it.category_name, 0) + 1
    return {
        "total": len(items),
        "new": new_count,
        "skip": skip_count,
        "date_min": min(dates) if dates else "",
        "date_max": max(dates) if dates else "",
        "expense_cents": expense_cents,
        "income_cents": income_cents,
        "categories": sorted(categories.items(), key=lambda kv: -kv[1])[:5],
    }


def merge_staged(
    conn, staging_id: int, namespace: str, user_id: str, platform: str,
) -> dict:
    staging = get_import_staging(conn, staging_id)
    if staging is None:
        return {"new": 0, "skip": 0, "errors": ["暂存数据不存在或已过期"]}
    items = [ParsedItem.model_validate(it) for it in json.loads(staging["data_json"])]
    ensure_default_accounts(conn, namespace, user_id)
    seen: set[tuple] = set()
    new_count = 0
    skip_count = 0
    row_errors: list[str] = []
    for idx, item in enumerate(items):
        mid = f"import:{platform}:{staging['message_id']}:{idx}"
        key = _item_dedup_key(item)
        if key in seen or _existing_item(conn, namespace, user_id, item):
            skip_count += 1
            continue
        seen.add(key)
        account_id = (
            get_or_create_account(conn, namespace, user_id, item.account_name)
            if item.account_name
            else None
        )
        try:
            conn.execute(
                """
                INSERT INTO expenses
                    (namespace, user_id, expense_date, category_id, account_id,
                     tx_type, amount_cents, description, platform, message_id, raw_text, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'normal')
                """,
                (
                    namespace, user_id, item.expense_date.isoformat(), item.category_id, account_id,
                    item.tx_type, item.amount_cents, item.description,
                    platform, mid, f"导入:{staging['filename']}",
                ),
            )
            new_count += 1
        except sqlite3.IntegrityError:
            skip_count += 1
            row_errors.append(f"第{idx + 1}行重复，已跳过")
    conn.execute(
        """
        INSERT INTO imports
            (namespace, user_id, platform, message_id, filename, file_type,
             total_rows, success_rows, failed_rows, errors)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            namespace, user_id, platform, staging["message_id"], staging["filename"],
            "xlsx" if staging["filename"].lower().endswith((".xlsx", ".xls")) else "csv",
            len(items), new_count, skip_count,
            "\n".join(row_errors[:50]),
        ),
    )
    delete_import_staging(conn, staging_id)
    conn.commit()
    return {"new": new_count, "skip": skip_count, "errors": row_errors}


def cleanup_stale_staging(conn) -> int:
    cur = conn.execute(
        "DELETE FROM import_staging WHERE created_at < datetime('now', '-1 day')"
    )
    conn.commit()
    return cur.rowcount


def import_file(
    conn,
    namespace: str,
    user_id: str,
    platform: str,
    message_id: str,
    filename: str,
    data: bytes,
) -> tuple[ImportResult, list[ParsedItem]]:
    categories = load_categories(conn)
    result, items = parse_import(data, filename, categories)
    ensure_default_accounts(conn, namespace, user_id)
    inserted = 0
    row_errors: list[str] = []
    for idx, item in enumerate(items):
        mid = f"import:{platform}:{message_id}:{idx}"
        account_id = (
            get_or_create_account(conn, namespace, user_id, item.account_name)
            if item.account_name
            else None
        )
        try:
            conn.execute(
                """
                INSERT INTO expenses
                    (namespace, user_id, expense_date, category_id, account_id,
                     tx_type, amount_cents, description, platform, message_id, raw_text, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'normal')
                """,
                (
                    namespace, user_id, item.expense_date.isoformat(), item.category_id, account_id,
                    item.tx_type, item.amount_cents, item.description,
                    platform, mid, f"导入:{filename}",
                ),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            row_errors.append(f"第{idx + 1}行重复，已跳过")
    conn.execute(
        """
        INSERT INTO imports
            (namespace, user_id, platform, message_id, filename, file_type,
             total_rows, success_rows, failed_rows, errors)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            namespace,
            user_id,
            platform,
            message_id,
            filename,
            "xlsx" if filename.lower().endswith((".xlsx", ".xls")) else "csv",
            result.total_rows,
            inserted,
            result.total_rows - inserted,
            "\n".join((result.errors + row_errors)[:50]),
        ),
    )
    conn.commit()
    result.success_rows = inserted
    result.failed_rows = result.total_rows - inserted
    return result, items


def summary_text(
    conn, namespace: str, user_id: str, items: list[ParsedItem],
    platform: str, message_id: str, raw_text: str = "",
) -> str:
    saved, skipped = record_items(conn, namespace, user_id, items, platform, message_id, raw_text)
    if not saved:
        return "⚠️ 该消息已记录过，请勿重复发送。"
    summary = day_summary(conn, namespace, user_id, timeutil.today())
    lines = [f"✅ 已记录 {len(saved)} 笔："]
    for it in saved:
        tag = {"expense": "支出", "income": "收入", "refund": "退款", "fee": "手续费",
               "adjust": "平账", "transfer_out": "转出", "transfer_in": "转入"}.get(it.tx_type, it.tx_type)
        acc = f"（{it.account_name}）" if it.account_name else ""
        lines.append(
            f"{it.category_icon} {tag} {it.category_name}：{format_money(it.amount_cents)} 元{acc}（{it.description}）"
        )
    lines.append(f"\n今日累计支出：{format_money(summary['total_cents'])} 元")
    if summary["income_cents"]:
        lines.append(f"今日累计收入：{format_money(summary['income_cents'])} 元")
    if skipped:
        lines.append(f"（另有 {len(skipped)} 笔为重复消息，未写入）")
    return "\n".join(lines)


# ---------- 统计 ----------

def period_stats(
    conn,
    namespace: str,
    user_id: str,
    start: datetime.date,
    end: datetime.date,
    account_name: str | None = None,
    category_name: str | None = None,
) -> dict:
    where = (
        "e.namespace = ? AND e.user_id = ? AND e.status = 'normal' "
        "AND e.expense_date BETWEEN ? AND ?"
    )
    params: list[Any] = [namespace, user_id, start.isoformat(), end.isoformat()]
    if account_name:
        where += " AND a.name = ?"
        params.append(account_name)
    if category_name:
        where += " AND c.name = ?"
        params.append(category_name)
    joins = "JOIN categories c ON c.id = e.category_id LEFT JOIN accounts a ON a.id = e.account_id"

    totals = conn.execute(
        f"""
        SELECT
          COALESCE(SUM(CASE WHEN e.tx_type IN ('expense','fee') THEN e.amount_cents ELSE 0 END), 0) AS expense_cents,
          COALESCE(SUM(CASE WHEN e.tx_type IN ('income','refund') THEN e.amount_cents ELSE 0 END), 0) AS income_cents,
          COALESCE(SUM(CASE WHEN e.tx_type = 'adjust' THEN e.amount_cents ELSE 0 END), 0) AS adjust_cents,
          COUNT(*) AS count
        FROM expenses e {joins}
        WHERE {where}
        """,
        params,
    ).fetchone()
    top_categories = conn.execute(
        f"""
        SELECT c.name, c.icon, SUM(e.amount_cents) AS total_cents, COUNT(*) AS cnt
        FROM expenses e {joins}
        WHERE {where} AND e.tx_type IN ('expense','fee')
        GROUP BY c.id, c.name, c.icon
        ORDER BY total_cents DESC LIMIT 3
        """,
        params,
    ).fetchall()
    top_day = conn.execute(
        f"""
        SELECT e.expense_date, SUM(e.amount_cents) AS total_cents
        FROM expenses e {joins}
        WHERE {where} AND e.tx_type IN ('expense','fee')
        GROUP BY e.expense_date ORDER BY total_cents DESC LIMIT 1
        """,
        params,
    ).fetchone()
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "expense_cents": int(totals["expense_cents"]),
        "income_cents": int(totals["income_cents"]),
        "adjust_cents": int(totals["adjust_cents"]),
        "count": int(totals["count"]),
        "top_categories": [dict(r) for r in top_categories],
        "top_day": dict(top_day) if top_day else None,
    }
