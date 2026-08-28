import datetime


def recent_expenses(conn, user_id: str, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        """
        SELECT e.expense_date, e.amount_cents, e.tx_type, e.description,
               c.name AS category_name, c.icon AS category_icon,
               a.name AS account_name
        FROM expenses e JOIN categories c ON c.id = e.category_id
        LEFT JOIN accounts a ON a.id = e.account_id
        WHERE e.user_id = ? AND e.status = 'normal'
        ORDER BY e.id DESC
        LIMIT ?
        """,
        (user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def day_summary(conn, user_id: str, day: datetime.date) -> dict:
    day_iso = day.isoformat()
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount_cents), 0) AS total_cents,
               COALESCE(SUM(CASE WHEN tx_type IN ('income','refund') THEN amount_cents ELSE 0 END), 0) AS income_cents,
               COUNT(*) AS count
        FROM expenses
        WHERE user_id = ? AND expense_date = ? AND status = 'normal'
          AND tx_type IN ('expense','fee')
        """,
        (user_id, day_iso),
    ).fetchone()
    cats = conn.execute(
        """
        SELECT c.name, c.icon, SUM(e.amount_cents) AS total_cents, COUNT(*) AS cnt
        FROM expenses e JOIN categories c ON c.id = e.category_id
        WHERE e.user_id = ? AND e.expense_date = ? AND e.status = 'normal'
          AND e.tx_type IN ('expense','fee')
        GROUP BY c.id, c.name, c.icon
        ORDER BY total_cents DESC
        """,
        (user_id, day_iso),
    ).fetchall()
    prev = conn.execute(
        """
        SELECT COALESCE(SUM(amount_cents), 0) AS total_cents
        FROM expenses
        WHERE user_id = ? AND expense_date = ? AND status = 'normal'
          AND tx_type IN ('expense','fee')
        """,
        (user_id, (day - datetime.timedelta(days=1)).isoformat()),
    ).fetchone()
    return {
        "day": day_iso,
        "total_cents": int(row["total_cents"]),
        "income_cents": int(row["income_cents"]),
        "count": int(row["count"]),
        "yesterday_cents": int(prev["total_cents"]),
        "categories": [dict(c) for c in cats],
    }


def period_detail(
    conn,
    user_id: str,
    start: datetime.date,
    end: datetime.date,
    account_name: str | None = None,
    category_name: str | None = None,
    limit: int = 50,
) -> list[dict]:
    where = "e.user_id = ? AND e.status = 'normal' AND e.expense_date BETWEEN ? AND ?"
    params: list = [user_id, start.isoformat(), end.isoformat()]
    if account_name:
        where += " AND a.name = ?"
        params.append(account_name)
    if category_name:
        where += " AND c.name = ?"
        params.append(category_name)
    rows = conn.execute(
        f"""
        SELECT e.expense_date, e.tx_type, e.amount_cents, e.description,
               c.name AS category_name, c.icon AS category_icon,
               a.name AS account_name
        FROM expenses e JOIN categories c ON c.id = e.category_id
        LEFT JOIN accounts a ON a.id = e.account_id
        WHERE {where}
        ORDER BY e.expense_date DESC, e.id DESC LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def last_chat(conn, user_id: str, platform: str) -> str | None:
    row = conn.execute(
        """
        SELECT chat_id FROM user_chats
        WHERE user_id = ? AND platform = ?
        ORDER BY last_seen_at DESC LIMIT 1
        """,
        (user_id, platform),
    ).fetchone()
    return row["chat_id"] if row else None


def touch_chat(conn, user_id: str, platform: str, chat_id: str) -> None:
    conn.execute(
        """
        INSERT INTO user_chats (user_id, platform, chat_id, last_seen_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(user_id, platform, chat_id)
        DO UPDATE SET last_seen_at = datetime('now')
        """,
        (user_id, platform, chat_id),
    )
    conn.commit()
