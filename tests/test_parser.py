import datetime

from paas.modules.account.parser import (
    parse_amount_cents,
    parse_balance_command,
    parse_chinese_number,
    parse_expense_date,
    parse_expenses,
    split_clauses,
)
from paas.modules.account.service import load_categories


def test_split_clauses():
    text = "今天中午和同事吃火锅花了86，打车回家23块；另外买奶茶15元"
    parts = split_clauses(text)
    assert parts == ["今天中午和同事吃火锅花了86", "打车回家23块", "买奶茶15元"]


def test_split_keeps_spaces():
    parts = split_clauses("今天 中午 吃火锅 花了 86")
    assert len(parts) == 1


def test_amounts():
    assert parse_amount_cents("86") == 8600
    assert parse_amount_cents("86.5") == 8650
    assert parse_amount_cents("86块5") == 8650
    assert parse_amount_cents("三十五") == 3500
    assert parse_amount_cents("二十多") == 2000
    assert parse_amount_cents("一百二") == 12000
    assert parse_amount_cents("两千五") == 250000
    assert parse_amount_cents("十五左右") == 1500
    assert parse_amount_cents("0.5") == 50


def test_chinese_number():
    assert parse_chinese_number("十") == 10.0
    assert parse_chinese_number("十五") == 15.0
    assert parse_chinese_number("二十三") == 23.0
    assert parse_chinese_number("一百零二") == 102.0
    assert parse_chinese_number("三百二十五") == 325.0
    assert parse_chinese_number("三千零五") == 3005.0


def test_dates():
    base = datetime.date(2026, 8, 28)
    assert parse_expense_date("昨天打车", base) == datetime.date(2026, 8, 27)
    assert parse_expense_date("前天买书", base) == datetime.date(2026, 8, 26)
    assert parse_expense_date("大前天吃饭", base) == datetime.date(2026, 8, 25)
    assert parse_expense_date("8月25号买票", base) == datetime.date(2026, 8, 25)
    assert parse_expense_date("2026年8月20日交房租", base) == datetime.date(2026, 8, 20)


def test_doc_example(conn):
    categories = load_categories(conn)
    items = parse_expenses(
        "今天中午和同事吃火锅花了86，打车回家23块", categories, datetime.date(2026, 8, 28)
    )
    assert len(items) == 2
    first, second = items
    assert first.amount_cents == 8600
    assert first.category_name == "餐饮"
    assert "吃火锅" in first.description
    assert second.amount_cents == 2300
    assert second.category_name == "交通"
    assert "回家" in second.description


def test_balance_parse():
    r = parse_balance_command("平账 10 微信对不上")
    assert r["mode"] == "amount"
    assert r["amount_cents"] == 1000
    assert r["note"] == "微信对不上"
    r2 = parse_balance_command("平账35")
    assert r2["mode"] == "amount"
    assert r2["amount_cents"] == 3500
    r3 = parse_balance_command("微信平账到90")
    assert r3["mode"] == "target"
    assert r3["account"] == "微信"
    assert r3["amount_cents"] == 9000
    assert parse_balance_command("平账")["mode"] == ""


def test_account_and_type_detection():
    from paas.modules.account.parser import detect_accounts, detect_tx_type

    assert detect_accounts("微信花了25") == ["微信"]
    assert detect_accounts("支付宝转银行卡500") == ["支付宝", "银行卡"]
    assert detect_tx_type("银行卡收入5000") == "income"
    assert detect_tx_type("微信转银行卡500") == "transfer_out"
    assert detect_tx_type("退款50") == "refund"
    assert detect_tx_type("手续费0.3") == "fee"
    assert detect_tx_type("吃饭花了25") == "expense"


def test_transfer_and_fee_parse(conn):
    from paas.modules.account.service import load_categories

    items = parse_expenses(
        "微信转银行卡500元，手续费0.3元", load_categories(conn), datetime.date(2026, 8, 28)
    )
    assert len(items) == 2
    assert items[0].tx_type == "transfer_out"
    assert items[0].account_name == "微信"
    assert items[0].to_account_name == "银行卡"
    assert items[1].tx_type == "fee"
    assert items[1].account_name == "微信"


def test_time_range_year_month():
    from paas.modules.account.parser import parse_time_range

    base = datetime.date(2026, 8, 28)
    assert parse_time_range("2025年7月份总共花了多少钱", base) == (
        datetime.date(2025, 7, 1), datetime.date(2025, 7, 31),
    )
    assert parse_time_range("2025年7月", base) == (
        datetime.date(2025, 7, 1), datetime.date(2025, 7, 31),
    )
    assert parse_time_range("2025年的账单", base) == (
        datetime.date(2025, 1, 1), datetime.date(2025, 12, 31),
    )
    assert parse_time_range("去年7月花了多少", base) == (
        datetime.date(2025, 7, 1), datetime.date(2025, 7, 31),
    )
    assert parse_time_range("今年", base) == (
        datetime.date(2026, 1, 1), base,
    )
    assert parse_time_range("7月花了多少", base) == (
        datetime.date(2026, 7, 1), datetime.date(2026, 7, 31),
    )
    assert parse_time_range("25年7月份花了多少", base) == (
        datetime.date(2025, 7, 1), datetime.date(2025, 7, 31),
    )
    assert parse_time_range("23年的账单", base) == (
        datetime.date(2023, 1, 1), datetime.date(2023, 12, 31),
    )


def test_abbreviated_year_date():
    from paas.modules.account.parser import parse_expense_date

    base = datetime.date(2026, 8, 28)
    assert parse_expense_date("25年7月5日微信吃饭花了25", base) == datetime.date(2025, 7, 5)
    assert parse_expense_date("23年7月微信吃饭花了25", base) == datetime.date(2023, 7, 1)


def test_preset_bank_and_alias():
    from paas.modules.account.parser import PRESET_BANKS, detect_accounts

    assert detect_accounts("建行卡花了25") == ["建行卡"]
    assert detect_accounts("招商银行吃饭花了25") == ["招行卡"]
    assert detect_accounts("龙卡花了25", [("建行卡", ["建行", "龙卡"])]) == ["建行卡"]
    assert PRESET_BANKS
