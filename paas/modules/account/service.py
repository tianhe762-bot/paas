import datetime
import json
import sqlite3
from typing import Any

from paas import timeutil
from paas.models import CategoryRow, ImportResult, ParsedItem
from paas.modules.account.importer import parse_import
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


def _upsert_daily_status(conn, user_id: str, day_iso: str, **flags: int) -> None:
    sets = ", ".join(f"{k}=excluded.{k}" for k in flags)
    cols = ", ".join(["user_id", "status_date", *flags.keys()])
    placeholders = ", ".join(["?"] * (2 + len(flags)))
    conn.execute(
        f"""
        INSERT INTO daily_status ({cols}) VALUES ({placeholders})
        ON CONFLICT(user_id, status_date) DO UPDATE SET {sets}
        """,
        (user_id, day_iso, *flags.values()),
    )


def save_raw_message(conn, platform: str, message_id: str, user_id: str, content: str) -> None:
    if not content:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO raw_messages (platform, message_id, user_id, content)
        VALUES (?, ?, ?, ?)
        """,
        (platform, message_id, user_id, content[:2000]),
    )


# ---------- 账户 ----------

def ensure_default_accounts(conn, user_id: str) -> None:
    existing = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM accounts WHERE user_id = ?", (user_id,)
        ).fetchall()
    }
    for idx, name in enumerate(DEFAULT_ACCOUNTS):
        if name not in existing:
            conn.execute(
                "INSERT INTO accounts (user_id, name, initial_balance_cents, sort_order) "
                "VALUES (?, ?, 0, ?)",
                (user_id, name, idx),
            )
    conn.commit()


def get_or_create_account(conn, user_id: str, name: str) -> int | None:
    name = (name or "").strip()
    if not name:
        return None
    row = conn.execute(
        "SELECT id FROM accounts WHERE user_id = ? AND name = ?", (user_id, name)
    ).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO accounts (user_id, name, initial_balance_cents, sort_order) "
        "VALUES (?, ?, 0, 1000)",
        (user_id, name),
    )
    conn.commit()
    return cur.lastrowid


def set_account_initial_balance(
    conn, user_id: str, account: str, amount_cents: int
) -> int | None:
    account_id = get_or_create_account(conn, user_id, account)
    if account_id is None:
        return None
    conn.execute(
        "UPDATE accounts SET initial_balance_cents = ? WHERE id = ?",
        (amount_cents, account_id),
    )
    conn.commit()
    return account_id


def account_balances(conn, user_id: str) -> list[dict]:
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
               ON e.account_id = a.id AND e.user_id = a.user_id AND e.status = 'normal'
        WHERE a.user_id = ?
        GROUP BY a.id, a.name, a.initial_balance_cents
        ORDER BY a.sort_order, a.id
        """,
        (user_id,),
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


def account_balance_by_name(conn, user_id: str, name: str) -> dict | None:
    for acc in account_balances(conn, user_id):
        if acc["name"] == name:
            return acc
    return None


def total_assets(conn, user_id: str) -> int:
    return sum(a["balance_cents"] for a in account_balances(conn, user_id))


# ---------- 记账 ----------

def record_items(
    conn,
    user_id: str,
    items: list[ParsedItem],
    platform: str,
    message_id: str,
    raw_text: str = "",
) -> tuple[list[ParsedItem], list[ParsedItem]]:
    """写入流水：message_id 唯一索引做防重；转账成对写入，手续费挂到上一笔转账。"""
    ensure_default_accounts(conn, user_id)
    saved: list[ParsedItem] = []
    skipped: list[ParsedItem] = []
    last_transfer_id: int | None = None
    for idx, item in enumerate(items):
        mid = f"{message_id}:{idx}" if len(items) > 1 else message_id
        try:
            if item.tx_type == "transfer_out":
                from_id = get_or_create_account(conn, user_id, item.account_name or "未命名")
                to_id = get_or_create_account(conn, user_id, item.to_account_name or "未命名")
                cur = conn.execute(
                    """
                    INSERT INTO expenses
                        (user_id, expense_date, category_id, account_id, to_account_id,
                         tx_type, amount_cents, description, platform, message_id, raw_text, status)
                    VALUES (?, ?, ?, ?, ?, 'transfer_out', ?, ?, ?, ?, ?, 'normal')
                    """,
                    (
                        user_id, item.expense_date.isoformat(), item.category_id,
                        from_id, to_id, item.amount_cents, item.description,
                        platform, mid, raw_text,
                    ),
                )
                out_id = cur.lastrowid
                last_transfer_id = out_id
                conn.execute(
                    """
                    INSERT INTO expenses
                        (user_id, expense_date, category_id, account_id, to_account_id,
                         tx_type, amount_cents, description, platform, message_id, raw_text, status, ref_id)
                    VALUES (?, ?, ?, ?, ?, 'transfer_in', ?, ?, ?, ?, ?, 'normal', ?)
                    """,
                    (
                        user_id, item.expense_date.isoformat(), item.category_id,
                        to_id, from_id, item.amount_cents,
                        f"{item.description}（转入）", platform, f"{mid}:in", raw_text, out_id,
                    ),
                )
                saved.append(item)
            else:
                account_id = (
                    get_or_create_account(conn, user_id, item.account_name)
                    if item.account_name
                    else None
                )
                ref_id = last_transfer_id if item.tx_type == "fee" and last_transfer_id else None
                conn.execute(
                    """
                    INSERT INTO expenses
                        (user_id, expense_date, category_id, account_id,
                         tx_type, amount_cents, description, platform, message_id, raw_text, status, ref_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'normal', ?)
                    """,
                    (
                        user_id, item.expense_date.isoformat(), item.category_id, account_id,
                        item.tx_type, item.amount_cents, item.description,
                        platform, mid, raw_text, ref_id,
                    ),
                )
                saved.append(item)
        except sqlite3.IntegrityError:
            skipped.append(item)
    today_iso = timeutil.today().isoformat()
    if any(it.expense_date.isoformat() == today_iso for it in saved):
        _upsert_daily_status(conn, user_id, today_iso, reported=1)
    conn.commit()
    return saved, skipped


def message_already_processed(conn, platform: str, message_id: str) -> bool:
    row = conn.execute(
        """
        SELECT id FROM expenses
        WHERE platform = ? AND (message_id = ? OR message_id LIKE ?)
        LIMIT 1
        """,
        (platform, message_id, f"{message_id}:%"),
    ).fetchone()
    return row is not None


def find_recent_duplicate(
    conn,
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
            "SELECT id FROM accounts WHERE user_id = ? AND name = ?",
            (user_id, item.account_name),
        ).fetchone()
        if item.account_name
        else None
    )
    row = conn.execute(
        """
        SELECT id FROM expenses
        WHERE user_id = ? AND amount_cents = ? AND description = ?
          AND tx_type = ? AND (account_id IS ?) AND expense_date = ? AND created_at >= ?
        LIMIT 1
        """,
        (
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

def get_pending(conn, user_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM pending_actions WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] <= _now_utc_str():
        conn.execute("DELETE FROM pending_actions WHERE user_id = ?", (user_id,))
        conn.commit()
        return None
    return dict(row)


def set_pending(
    conn,
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
        INSERT INTO pending_actions (user_id, action_type, payload, created_at, expires_at)
        VALUES (?, ?, ?, datetime('now'), ?)
        ON CONFLICT(user_id) DO UPDATE SET
            action_type = excluded.action_type,
            payload = excluded.payload,
            created_at = excluded.created_at,
            expires_at = excluded.expires_at
        """,
        (user_id, action_type, json.dumps(payload, ensure_ascii=False), expires),
    )
    conn.commit()


def clear_pending(conn, user_id: str) -> None:
    conn.execute("DELETE FROM pending_actions WHERE user_id = ?", (user_id,))
    conn.commit()


# ---------- 状态与平账 ----------

def mark_zero(conn, user_id: str) -> None:
    _upsert_daily_status(conn, user_id, timeutil.iso_today(), zero_confirmed=1)
    conn.commit()


def mark_skipped(conn, user_id: str) -> None:
    _upsert_daily_status(conn, user_id, timeutil.iso_today(), skipped=1)
    conn.commit()


def create_adjustment(
    conn,
    user_id: str,
    account: str,
    amount_cents: int,
    note: str,
    platform: str,
    message_id: str,
    raw_text: str = "",
) -> ParsedItem | None:
    """平账调整：amount_cents 有符号（正=余额增加，负=余额减少）。"""
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
    saved, _ = record_items(conn, user_id, [item], platform, message_id, raw_text)
    return saved[0] if saved else None


def confirm_payload_items(payload: dict) -> list[ParsedItem]:
    return [ParsedItem.model_validate(it) for it in payload.get("items", [])]


# ---------- 撤销 / 修改 ----------

def find_target_record(conn, user_id: str, amount_cents: int | None = None) -> dict | None:
    q = (
        "SELECT e.*, c.name AS category_name, c.icon AS category_icon, "
        "a.name AS account_name FROM expenses e "
        "JOIN categories c ON c.id = e.category_id "
        "LEFT JOIN accounts a ON a.id = e.account_id "
        "WHERE e.user_id = ? AND e.status = 'normal'"
    )
    params: list[Any] = [user_id]
    if amount_cents is not None:
        q += " AND e.amount_cents = ?"
        params.append(amount_cents)
    q += " ORDER BY e.id DESC LIMIT 1"
    row = conn.execute(q, params).fetchone()
    return dict(row) if row else None


def void_record(conn, user_id: str, record_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM expenses WHERE id = ? AND user_id = ? AND status = 'normal'",
        (record_id, user_id),
    ).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE expenses SET status = 'voided' WHERE id = ?", (record_id,))
    conn.commit()
    return dict(row)


def modify_record(
    conn,
    user_id: str,
    record_id: int,
    new_amount_cents: int,
    new_description: str | None = None,
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM expenses WHERE id = ? AND user_id = ? AND status = 'normal'",
        (record_id, user_id),
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

def import_file(
    conn,
    user_id: str,
    platform: str,
    message_id: str,
    filename: str,
    data: bytes,
) -> tuple[ImportResult, list[ParsedItem]]:
    categories = load_categories(conn)
    result, items = parse_import(data, filename, categories)
    ensure_default_accounts(conn, user_id)
    inserted = 0
    row_errors: list[str] = []
    for idx, item in enumerate(items):
        mid = f"import:{platform}:{message_id}:{idx}"
        account_id = (
            get_or_create_account(conn, user_id, item.account_name)
            if item.account_name
            else None
        )
        try:
            conn.execute(
                """
                INSERT INTO expenses
                    (user_id, expense_date, category_id, account_id,
                     tx_type, amount_cents, description, platform, message_id, raw_text, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'normal')
                """,
                (
                    user_id, item.expense_date.isoformat(), item.category_id, account_id,
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
            (user_id, platform, message_id, filename, file_type,
             total_rows, success_rows, failed_rows, errors)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
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
    conn, user_id: str, items: list[ParsedItem], platform: str, message_id: str, raw_text: str = ""
) -> str:
    saved, skipped = record_items(conn, user_id, items, platform, message_id, raw_text)
    if not saved:
        return "⚠️ 该消息已记录过，请勿重复发送。"
    summary = day_summary(conn, user_id, timeutil.today())
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
    user_id: str,
    start: datetime.date,
    end: datetime.date,
    account_name: str | None = None,
    category_name: str | None = None,
) -> dict:
    where = "e.user_id = ? AND e.status = 'normal' AND e.expense_date BETWEEN ? AND ?"
    params: list[Any] = [user_id, start.isoformat(), end.isoformat()]
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
