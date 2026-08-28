import io

from paas.modules.account.importer import parse_import
from paas.modules.account.service import load_categories


def _csv_bytes(text: str, enc="utf-8-sig"):
    return text.encode(enc)


def test_csv_utf8(conn):
    data = _csv_bytes(
        "日期,金额,分类,备注\n"
        "2026-08-01,86.5,餐饮,吃火锅\n"
        "2026-08-02,23,交通,打车回家\n"
    )
    result, items = parse_import(data, "a.csv", load_categories(conn))
    assert result.success_rows == 2
    assert items[0].amount_cents == 8650
    assert items[1].category_name == "交通"


def test_csv_gbk(conn):
    data = _csv_bytes(
        "日期,金额,分类,备注\n"
        "2026-08-01,12.5,餐饮,早餐\n",
        enc="gbk",
    )
    result, items = parse_import(data, "b.csv", load_categories(conn))
    assert result.success_rows == 1
    assert items[0].amount_cents == 1250


def test_csv_header_not_first_row(conn):
    data = _csv_bytes(
        "某平台导出\n"
        "日期,金额,分类,备注\n"
        "2026-08-01,30,购物,日用品\n"
    )
    result, items = parse_import(data, "c.csv", load_categories(conn))
    assert result.success_rows == 1
    assert items[0].amount_cents == 3000


def test_xlsx(conn):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["日期", "金额", "分类", "备注"])
    ws.append(["2026-08-01", 88, "餐饮", "火锅"])
    ws.append(["2026-08-02", 19.9, "娱乐", "电影"])
    buf = io.BytesIO()
    wb.save(buf)
    result, items = parse_import(buf.getvalue(), "d.xlsx", load_categories(conn))
    assert result.success_rows == 2
    assert items[1].amount_cents == 1990
    assert items[1].category_name == "娱乐"


def test_negative_amount_becomes_income(conn):
    data = _csv_bytes(
        "日期,金额,分类,备注\n"
        "2026-08-01,-100,其他,工资\n"
        "2026-08-02,20,餐饮,午饭\n"
    )
    result, items = parse_import(data, "e.csv", load_categories(conn))
    assert result.total_rows == 2
    assert result.success_rows == 2
    assert result.failed_rows == 0
    assert items[0].tx_type == "income"
    assert items[0].amount_cents == 10000


def test_datetime_string_date(conn):
    """钱迹等导出的日期列带时间，如 2026-08-28 16:34:12。"""
    data = _csv_bytes(
        "日期,金额,分类,备注\n"
        "2026-08-01 16:34:12,25,餐饮,早餐\n"
        "2026/08/02 09:00,30,交通,打车\n"
    )
    result, items = parse_import(data, "f.csv", load_categories(conn))
    assert result.success_rows == 2
    assert items[0].expense_date.isoformat() == "2026-08-01"
    assert items[1].expense_date.isoformat() == "2026-08-02"


def test_type_column(conn):
    data = _csv_bytes(
        "日期,类型,金额,分类,备注\n"
        "2026-08-01,收入,5000,其他,工资\n"
        "2026-08-02,支出,25,餐饮,午饭\n"
        "2026-08-03,退款,50,其他,退货\n"
    )
    result, items = parse_import(data, "g.csv", load_categories(conn))
    assert result.success_rows == 3
    assert items[0].tx_type == "income"
    assert items[1].tx_type == "expense"
    assert items[2].tx_type == "refund"


def test_unsupported_type(conn):
    result, items = parse_import(b"hello", "a.txt", load_categories(conn))
    assert result.success_rows == 0
    assert "不支持" in result.errors[0]
