import datetime

from paas.models import ParsedItem
from paas.modules.account import service as s
from paas.modules.account.queries import day_summary


def make_item(amount_cents=8600, desc="吃火锅", day=None, account="微信", tx_type="expense"):
    return ParsedItem(
        expense_date=day or datetime.date(2026, 8, 28),
        category_id=1,
        category_name="餐饮",
        category_icon="🍜",
        account_name=account,
        tx_type=tx_type,
        amount_cents=amount_cents,
        description=desc,
    )


def test_dedup_by_message_id(conn):
    saved, _ = s.record_items(conn, "default", "u1", [make_item()], "qq", "m1")
    assert len(saved) == 1
    saved2, skipped2 = s.record_items(conn, "default", "u1", [make_item()], "qq", "m1")
    assert saved2 == []
    assert len(skipped2) == 1


def test_dedup_distinct_message_ids(conn):
    saved, _ = s.record_items(conn, "default", "u1", [make_item()], "qq", "m1")
    saved2, _ = s.record_items(conn, "default", "u1", [make_item()], "qq", "m2")
    assert len(saved) == 1 and len(saved2) == 1


def test_debounce_window(conn):
    saved, _ = s.record_items(conn, "default", "u1", [make_item()], "qq", "m1")
    assert saved
    assert s.find_recent_duplicate(conn, "default", "u1", make_item(), window_seconds=30) is True
    conn.execute(
        "UPDATE expenses SET created_at = datetime('now', '-2 minutes') WHERE message_id = 'm1'"
    )
    conn.commit()
    assert s.find_recent_duplicate(conn, "default", "u1", make_item(), window_seconds=30) is False


def test_pending_ttl(conn):
    s.set_pending(conn, "default", "u1", "DUPLICATE_CONFIRM", {"items": []}, ttl_seconds=600)
    assert s.get_pending(conn, "default", "u1") is not None
    s.set_pending(conn, "default", "u1", "DUPLICATE_CONFIRM", {"items": []}, ttl_seconds=-10)
    assert s.get_pending(conn, "default", "u1") is None


def test_account_balance_expense_income(conn):
    s.record_items(conn, "default", "u1", [make_item(2500, "吃饭", account="微信")], "qq", "m-e1")
    s.record_items(
        conn, "default", "u1", [make_item(500000, "工资", account="银行卡", tx_type="income")], "qq", "m-i1"
    )
    balances = {a["name"]: a["balance_cents"] for a in s.account_balances(conn, "default", "u1")}
    assert balances["微信"] == -2500
    assert balances["银行卡"] == 500000
    assert s.total_assets(conn, "default", "u1") == 497500


def test_transfer_and_fee_balance(conn):
    items = [
        make_item(50000, "转出", account="微信", tx_type="transfer_out"),
        make_item(30, "手续费", account="微信", tx_type="fee"),
    ]
    items[0].to_account_name = "银行卡"
    s.record_items(conn, "default", "u1", items, "qq", "m-t1")
    balances = {a["name"]: a["balance_cents"] for a in s.account_balances(conn, "default", "u1")}
    assert balances["微信"] == -50030
    assert balances["银行卡"] == 50000


def test_void_record_updates_balance(conn):
    saved, _ = s.record_items(conn, "default", "u1", [make_item(2500, "吃饭", account="微信")], "qq", "m-v1")
    record_id = conn.execute("SELECT id FROM expenses WHERE message_id='m-v1'").fetchone()["id"]
    assert s.account_balance_by_name(conn, "default", "u1", "微信")["balance_cents"] == -2500
    s.void_record(conn, "default", "u1", record_id)
    assert s.account_balance_by_name(conn, "default", "u1", "微信")["balance_cents"] == 0
    row = conn.execute("SELECT status FROM expenses WHERE id=?", (record_id,)).fetchone()
    assert row["status"] == "voided"


def test_modify_record_updates_balance(conn):
    s.record_items(conn, "default", "u1", [make_item(2500, "吃饭", account="微信")], "qq", "m-m1")
    record_id = conn.execute("SELECT id FROM expenses WHERE message_id='m-m1'").fetchone()["id"]
    s.modify_record(conn, "default", "u1", record_id, 3500)
    assert s.account_balance_by_name(conn, "default", "u1", "微信")["balance_cents"] == -3500


def test_balance_adjustment_not_in_consumption(conn):
    s.create_adjustment(conn, "default", "u1", "微信", -1000, "微信对不上", "qq", "m-bal")
    summary = day_summary(conn, "default", "u1", datetime.date(2026, 8, 28))
    assert summary["total_cents"] == 0
    assert summary["count"] == 0
    assert s.account_balance_by_name(conn, "default", "u1", "微信")["balance_cents"] == -1000
    row = conn.execute("SELECT tx_type, amount_cents FROM expenses WHERE message_id='m-bal'").fetchone()
    assert row["tx_type"] == "adjust"
    assert row["amount_cents"] == -1000


def test_zero_and_skip(conn):
    s.mark_zero(conn, "default", "u1")
    status = conn.execute(
        "SELECT * FROM daily_status WHERE user_id='u1' AND status_date=?",
        (datetime.date(2026, 8, 28).isoformat(),),
    ).fetchone()
    assert status["zero_confirmed"] == 1
    s.mark_skipped(conn, "default", "u1")
    status = conn.execute(
        "SELECT * FROM daily_status WHERE user_id='u1' AND status_date=?",
        (datetime.date(2026, 8, 28).isoformat(),),
    ).fetchone()
    assert status["skipped"] == 1


def test_yesterday_record_does_not_mark_today_reported(conn):
    item = make_item(day=datetime.date(2026, 8, 27))
    s.record_items(conn, "default", "u1", [item], "qq", "m-y")
    status = conn.execute(
        "SELECT reported FROM daily_status WHERE user_id='u1' AND status_date=?",
        (datetime.date(2026, 8, 28).isoformat(),),
    ).fetchone()
    assert status is None or status["reported"] == 0


def test_period_stats(conn):
    today = datetime.date(2026, 8, 28)
    s.record_items(conn, "default", "u1", [make_item(2500, "吃饭", account="微信")], "qq", "m-s1")
    s.record_items(conn, "default", "u1", [make_item(3000, "打车", day=today, account="微信")], "qq", "m-s2")
    s.record_items(
        conn, "default", "u1", [make_item(5000, "工资", day=today, account="银行卡", tx_type="income")], "qq", "m-s3"
    )
    stats = s.period_stats(conn, "default", "u1", today, today)
    assert stats["expense_cents"] == 5500
    assert stats["income_cents"] == 5000
    assert stats["count"] == 3
    stats_wx = s.period_stats(conn, "default", "u1", today, today, account_name="微信")
    assert stats_wx["expense_cents"] == 5500
    assert stats_wx["income_cents"] == 0


def test_import_csv_roundtrip(conn):
    data = (
        "日期,金额,分类,备注\n"
        "2026-08-01,86.5,餐饮,吃火锅\n"
        "2026-08-02,23,交通,打车\n"
    ).encode("utf-8-sig")
    result, items = s.import_file(conn, "default", "u1", "qq", "imp1", "ledger.csv", data)
    assert result.total_rows == 2
    assert result.success_rows == 2
    assert len(items) == 2
    assert items[0].amount_cents == 8650
    result2, _ = s.import_file(conn, "default", "u1", "qq", "imp1", "ledger.csv", data)
    assert result2.success_rows == 0
    assert result2.failed_rows == 2
