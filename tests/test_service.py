import datetime

from paas import timeutil
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


def test_unknown_account_names_and_create(conn):
    s.ensure_default_accounts(conn, "default", "u1")
    items = [
        make_item(2500, "吃饭", account="微信"),
        make_item(50000, "转出", account="微信", tx_type="transfer_out"),
    ]
    items[1].to_account_name = "建行卡"
    assert s.unknown_account_names(conn, "default", "u1", items) == ["建行卡"]
    s.create_accounts(conn, "default", "u1", ["建行卡"])
    assert "建行卡" in s.user_account_names(conn, "default", "u1")
    assert s.unknown_account_names(conn, "default", "u1", items) == []


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
        (timeutil.iso_today(),),
    ).fetchone()
    assert status["zero_confirmed"] == 1
    s.mark_skipped(conn, "default", "u1")
    status = conn.execute(
        "SELECT * FROM daily_status WHERE user_id='u1' AND status_date=?",
        (timeutil.iso_today(),),
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


def test_account_templates_init_and_sync(conn):
    ok, _ = s.replace_account_templates(
        conn, "ns1",
        [{"name": "建行卡", "aliases": "建行,龙卡", "initial_balance_cents": 100000, "sort_order": 0}],
    )
    assert ok
    # 新用户首触时自动创建模板账户
    s.ensure_default_accounts(conn, "ns1", "u_new")
    row = conn.execute(
        "SELECT * FROM accounts WHERE namespace='ns1' AND user_id='u_new' AND name='建行卡'"
    ).fetchone()
    assert row is not None
    assert row["aliases"] == "建行,龙卡"
    assert row["initial_balance_cents"] == 100000
    # 模板改名同步到已有用户
    ok, _ = s.replace_account_templates(
        conn, "ns1",
        [{"name": "建行储蓄卡", "aliases": "建行", "initial_balance_cents": 0, "sort_order": 0}],
    )
    assert ok
    renamed = conn.execute(
        "SELECT * FROM accounts WHERE namespace='ns1' AND user_id='u_new' AND name='建行储蓄卡'"
    ).fetchone()
    assert renamed is not None
    assert renamed["aliases"] == "建行"


def test_backfill(conn):
    from paas.models import ParsedItem

    item = ParsedItem(
        expense_date=datetime.date(2025, 7, 1),
        category_id=1, category_name="餐饮", category_icon="🍜",
        account_name="", tx_type="expense", amount_cents=2500, description="建行消费",
    )
    s.record_items(conn, "default", "u_bf", [item], "qq", "bf1", "原始消息含建行")
    preview = s.backfill_preview(conn, "default", "u_bf", "建行")
    assert preview["matched"] == 1
    res = s.backfill_apply(
        conn, "default", "u_bf", [{"keyword": "建行", "account": "建行卡"}]
    )
    assert res["results"][0]["applied"] == 1
    row = conn.execute(
        "SELECT account_id FROM expenses WHERE user_id='u_bf'"
    ).fetchone()
    acc = conn.execute("SELECT id FROM accounts WHERE name='建行卡'").fetchone()
    assert row["account_id"] == acc["id"]
    # 已关联流水不再被回填
    res2 = s.backfill_apply(conn, "default", "u_bf", [{"keyword": "建行", "account": "建行卡"}])
    assert res2["results"][0]["applied"] == 0


def test_import_staging_merge_dedup(conn):
    from paas.models import ParsedItem

    items = [
        ParsedItem(
            expense_date=datetime.date(2026, 8, 1), category_id=1,
            category_name="餐饮", category_icon="🍜", account_name="微信",
            tx_type="expense", amount_cents=8650, description="吃火锅",
        ),
        ParsedItem(
            expense_date=datetime.date(2026, 8, 2), category_id=2,
            category_name="交通", category_icon="🚕", account_name="微信",
            tx_type="expense", amount_cents=2300, description="打车",
        ),
    ]
    preview = s.preview_merge(conn, "default", "u_st", items)
    assert preview["new"] == 2 and preview["skip"] == 0
    sid = s.create_import_staging(conn, "default", "u_st", "qq", "st1", "a.csv", items)
    result = s.merge_staged(conn, sid, "default", "u_st", "qq")
    assert result["new"] == 2 and result["skip"] == 0
    # 再合并同内容 → 全部跳过（去重）
    sid2 = s.create_import_staging(conn, "default", "u_st", "qq", "st2", "a.csv", items)
    result2 = s.merge_staged(conn, sid2, "default", "u_st", "qq")
    assert result2["new"] == 0 and result2["skip"] == 2


def test_first_run_credentials_file(tmp_path):
    from paas.config import settings as st
    from paas.db import connect, init_db
    from paas.modules.admin import service as admin_service

    st.data_dir = tmp_path / "data"
    st.db_path = tmp_path / "a.db"
    st.secret_key_path = tmp_path / "k"
    st.admin_password = ""
    st.admin_username = "admin"
    conn = connect()
    init_db(conn)
    username = admin_service.ensure_admin(conn)
    conn.close()
    f = tmp_path / "data" / "admin_credentials.txt"
    assert f.exists()
    content = f.read_text(encoding="utf-8")
    assert "8000/admin" in content
    assert username in content
    assert "密码" in content


def test_first_run_credentials_env_password(tmp_path):
    from paas.config import settings as st
    from paas.db import connect, init_db
    from paas.modules.admin import service as admin_service

    st.data_dir = tmp_path / "data2"
    st.db_path = tmp_path / "b.db"
    st.secret_key_path = tmp_path / "k2"
    st.admin_password = "my-custom-pass-123"
    st.admin_username = "boss"
    conn = connect()
    init_db(conn)
    admin_service.ensure_admin(conn)
    conn.close()
    content = (tmp_path / "data2" / "admin_credentials.txt").read_text(encoding="utf-8")
    assert "boss" in content
    assert "my-custom-pass-123" in content
