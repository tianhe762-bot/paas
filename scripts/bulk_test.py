"""记账模块大规模自测：3000+ 条用例，覆盖记账/删除/分类/查询/报表/余额/询问/
历史补记/导出/多平台导入/转账手续费/撤销/平账，以及语气与语序变体。

用法：
    python -m scripts.bulk_test            # 全量运行，生成 bulk_report.txt
"""

import asyncio
import datetime
import itertools
import os
import tempfile
from pathlib import Path

from paas import settings_store, timeutil
from paas.config import settings
from paas.db import connect, init_db
from paas.models import InboundMessage
from paas.modules.account import service as s
from paas.modules.account.importer import parse_import
from paas.modules.account.parser import parse_amount_cents, parse_expense_date
from paas.router import Router

TODAY = None  # 运行时由 run_bulk 设置为系统当天，保证相对日期断言正确


def mkmsg(user, mid, content):
    return InboundMessage(
        platform="bulk",
        user_id=user,
        chat_id="c_" + user,
        message_id=mid,
        content=content,
    )


class BulkCase:
    def __init__(self, section, label, steps, check=None):
        self.section = section
        self.label = label
        self.steps = steps  # [(mid, content)]
        self.check = check


def _fmt(cents):
    return f"{abs(cents) / 100:.2f}"


def build_cases():
    global TODAY
    if TODAY is None:
        TODAY = timeutil.today()
    cases = []
    counter = {"n": 0}

    def mid():
        counter["n"] += 1
        return f"b{counter['n']}"

    # ========== 1. 支出语序矩阵（时间×账户×金额×动词×分类×句式模板） ==========
    times = ["今天", "昨天", "前天", "8月20日", "大前天"]
    accounts = ["微信", "支付宝", "银行卡", "现金", "信用卡"]
    amounts = ["25", "25.5", "86块5", "0.3", "三十五", "二十多", "一百二", "10块5", "两百五"]
    verbs = ["花了", "付了", "用了", "买了"]
    categories = ["吃饭", "打车", "奶茶", "买书"]
    templates = [
        lambda t, a, v, am, c: f"{t}{a}{c}{v}{am}元",
        lambda t, a, v, am, c: f"{t}{a}{v}{am}元{c}",
        lambda t, a, v, am, c: f"{t}{v}{am}元{a}{c}",
        lambda t, a, v, am, c: f"{c}{t}{a}{v}{am}元",
        lambda t, a, v, am, c: f"{a}{t}{c}消费{am}元",
        lambda t, a, v, am, c: f"{t}{c}花了{am}元，{a}支付",
    ]
    for t, a, am, v, c in itertools.product(
        times, accounts, amounts, verbs, categories
    ):
        tmpl = templates[(counter["n"] // len(amounts)) % len(templates)]
        content = tmpl(t, a, v, am, c)
        expected_cents = parse_amount_cents(content)
        expected_date = parse_expense_date(content, TODAY)

        def check_expense(
            case_user, conn, replies,
            content=content, expected_cents=expected_cents,
            expected_date=expected_date, a=a,
        ):
            row = conn.execute(
                "SELECT * FROM expenses WHERE user_id=? ORDER BY id DESC LIMIT 1",
                (case_user,),
            ).fetchone()
            assert row is not None, f"无记录: {content}"
            assert row["amount_cents"] == expected_cents, (
                f"金额 {row['amount_cents']} != {expected_cents} ({content})"
            )
            assert row["expense_date"] == expected_date.isoformat(), (
                f"日期 {row['expense_date']} != {expected_date} ({content})"
            )
            assert row["tx_type"] == "expense"
            assert row["status"] == "normal"
            acc = s.account_balance_by_name(conn, "default", case_user, a)
            assert acc is not None and acc["balance_cents"] == -expected_cents, "余额未同步"
            # 回复真实性：回复里必须包含实际金额
            assert _fmt(expected_cents) + " 元" in replies[0].reply_content, (
                f"回复金额不符: {replies[0].reply_content[:80]}"
            )
            assert f"（{a}）" in replies[0].reply_content, "回复未含账户"

        cases.append(BulkCase("支出语序", f"支出-{content}", [(mid(), content)], check=check_expense))

    # ========== 2. 语气/句式变体 ==========
    tone_templates = [
        "帮我记一下，{t}{a}{c}{v}{am}元",
        "记一笔：{t}{a}{c}{v}{am}元",
        "记：{t}，{a}，{c}，{am}元",
        "帮我记下{t}{a}{c}{am}元",
        "{t}{a}{c}支出了{am}元",
        "花费{t}{a}{c}{am}元",
        "{t}我{a}{c}花了{am}元",
        "请记录：{t}{a}{c}{v}{am}元",
        "{a}那边{t}{c}花了{am}元",
        "刚才{t}{a}{c}{v}{am}元，帮我记上",
    ]
    for tmpl_str, t, a, am in itertools.product(
        tone_templates, times[:3], accounts[:3], amounts[:5]
    ):
        content = tmpl_str.format(t=t, a=a, c="吃饭", v="花了", am=am)
        expected_cents = parse_amount_cents(content)
        if expected_cents is None:
            continue

        def check_tone(
            case_user, conn, replies,
            content=content, expected_cents=expected_cents, a=a,
        ):
            row = conn.execute(
                "SELECT * FROM expenses WHERE user_id=? ORDER BY id DESC LIMIT 1",
                (case_user,),
            ).fetchone()
            assert row is not None, f"语气句未记录: {content}"
            assert row["amount_cents"] == expected_cents, f"语气句金额不符: {content}"
            assert _fmt(expected_cents) + " 元" in replies[0].reply_content, "语气句回复金额不符"

        cases.append(BulkCase("语气变体", f"语气-{content}", [(mid(), content)], check=check_tone))

    # ========== 3. 缺账户 / 缺时间 询问 ==========
    for content in [
        "今天吃饭花了25元", "昨天打车付了30块", "8月20日买奶茶花了15",
        "前天看电影消费40元", "帮我记一下今天午饭30", "今天买书花了55",
    ]:
        cases.append(
            BulkCase(
                "缺账户询问", f"缺账户-{content}", [(mid(), content)],
                check=lambda u, conn, replies, c=content: _expect_pending(replies[0], "账户"),
            )
        )
    for content in [
        "微信吃饭花了25元", "支付宝打车付了30块", "银行卡买奶茶花了15",
        "现金买书付了40元", "信用卡看电影花了60", "微信点了外卖35",
    ]:
        cases.append(
            BulkCase(
                "缺时间询问", f"缺时间-{content}", [(mid(), content)],
                check=lambda u, conn, replies, c=content: _expect_pending(replies[0], "什么时候"),
            )
        )
    # 缺账户后补答再缺时间
    cases.append(
        BulkCase(
            "缺账户询问", "缺账户→补答→缺时间",
            [(mid(), "午饭30"), (mid(), "微信")],
            check=lambda u, conn, replies: (
                _expect_pending(replies[0], "账户"),
                _expect_pending(replies[1], "什么时候"),
            ),
        )
    )

    # ========== 4. 收入 ==========
    income_templates = [
        "今天{account}收入{amount}元",
        "{account}今天收到{amount}元工资",
        "今天{account}入账{amount}元",
        "今天{account}收到转账{amount}元",
        "{account}今天进账{amount}元",
        "收到{amount}元，{account}，今天",
    ]
    for tpl, account, amount in itertools.product(
        income_templates, ["银行卡", "微信", "支付宝"], ["5000", "880.5", "一万"]
    ):
        content = tpl.format(account=account, amount=amount)
        expected_cents = parse_amount_cents(content)
        if expected_cents is None:
            continue

        def check_income(
            u, conn, replies, content=content,
            expected_cents=expected_cents, account=account,
        ):
            row = conn.execute(
                "SELECT * FROM expenses WHERE user_id=? ORDER BY id DESC LIMIT 1", (u,)
            ).fetchone()
            assert row is not None
            assert row["tx_type"] == "income"
            assert row["amount_cents"] == expected_cents
            acc = s.account_balance_by_name(conn, "default", u, account)
            assert acc["balance_cents"] == expected_cents

        cases.append(BulkCase("收入", f"收入-{content}", [(mid(), content)], check=check_income))

    # ========== 5. 转账 + 手续费 ==========
    transfer_templates = [
        "今天{from_acc}转{to_acc}{amount}元",
        "{from_acc}今天转到{to_acc}{amount}元",
        "今天从{from_acc}转给{to_acc}{amount}元",
        "今天{from_acc}转出{amount}元到{to_acc}",
        "今天帮我把{from_acc}转{to_acc}{amount}元",
        "今天转{amount}元给{to_acc}，从{from_acc}扣",
    ]
    for tpl, f, t in itertools.product(
        transfer_templates, ["微信", "支付宝", "银行卡"], ["银行卡", "现金", "支付宝"]
    ):
        if f == t:
            continue
        content = tpl.format(from_acc=f, to_acc=t, amount="500")

        def check_transfer(u, conn, replies, content=content, f=f, t=t):
            out_row = conn.execute(
                "SELECT * FROM expenses WHERE user_id=? AND tx_type='transfer_out' ORDER BY id DESC LIMIT 1",
                (u,),
            ).fetchone()
            in_row = conn.execute(
                "SELECT * FROM expenses WHERE user_id=? AND tx_type='transfer_in' ORDER BY id DESC LIMIT 1",
                (u,),
            ).fetchone()
            assert out_row is not None and in_row is not None, f"转账未成对: {content}"
            assert out_row["amount_cents"] == 50000
            balances = {a["name"]: a["balance_cents"] for a in s.account_balances(conn, "default", u)}
            assert balances[f] == -50000
            assert balances[t] == 50000

        cases.append(BulkCase("转账", f"转账-{content}", [(mid(), content)], check=check_transfer))

    fee_content = "今天微信转银行卡500元，手续费0.3元"
    cases.append(
        BulkCase(
            "转账手续费", "转账+手续费",
            [(mid(), fee_content)],
            check=lambda u, conn, replies: _check_transfer_with_fee(u, conn),
        )
    )
    fee2 = "支付宝今天转现金300元，手续费1.5元"
    cases.append(
        BulkCase(
            "转账手续费", "转账+手续费2",
            [(mid(), fee2)],
            check=lambda u, conn, replies: _check_transfer_with_fee(u, conn, 30000, 150),
        )
    )

    # ========== 6. 退款 ==========
    for content in [
        "今天微信退款50元", "支付宝今天收到退款30元", "今天银行卡退回来100元",
        "今天现金退款收到25", "信用卡退款80元，今天",
    ]:
        cases.append(
            BulkCase(
                "退款", f"退款-{content}", [(mid(), content)],
                check=lambda u, conn, replies, c=content: _check_refund(u, conn, c),
            )
        )

    # ========== 7. 删除/撤销（当前与往期、按金额、各种说法） ==========
    delete_verbs = ["删除", "撤销", "删掉", "作废", "取消这笔"]
    targets = ["刚才那笔", "最后一笔", "上一笔", "刚才的记账"]
    for verb, target in itertools.product(delete_verbs, targets):
        content = f"{verb}{target}"
        cases.append(
            BulkCase(
                "删除撤销", f"删除-{content}",
                [(mid(), "今天微信吃饭花了25元"), (mid(), content), (mid(), "是")],
                check=lambda u, conn, replies: _check_delete(u, conn, replies),
            )
        )
    cases.append(
        BulkCase(
            "删除撤销", "删除-按金额",
            [(mid(), "今天微信奶茶花了15元"), (mid(), "删除15元那笔"), (mid(), "是")],
            check=lambda u, conn, replies: _check_delete(u, conn, replies),
        )
    )
    # 撤销往期账单
    cases.append(
        BulkCase(
            "删除撤销", "撤销往期账单",
            [
                (mid(), "8月10日微信吃饭花了25元"),
                (mid(), "删除8月10日那笔25元"),
                (mid(), "是"),
            ],
            check=lambda u, conn, replies: _check_delete_past(u, conn, replies),
        )
    )

    # ========== 8. 修改 ==========
    for content, new_cents in [
        ("刚才那笔不是25，是35", 3500),
        ("把刚才那笔改成35", 3500),
        ("改一下最后那笔，改成35", 3500),
        ("刚才那笔记错了，是35不是25", 3500),
    ]:
        cases.append(
            BulkCase(
                "修改", f"修改-{content}",
                [(mid(), "今天微信吃饭花了25元"), (mid(), content), (mid(), "是")],
                check=lambda u, conn, replies, nc=new_cents: _check_modify(u, conn, replies, nc),
            )
        )
    cases.append(
        BulkCase(
            "修改", "修改-追问金额",
            [(mid(), "今天微信吃饭花了25元"), (mid(), "刚才那笔记错了"), (mid(), "35")],
            check=lambda u, conn, replies: _check_modify(u, conn, replies, 3500, ask_first=True),
        )
    )

    # ========== 9. 平账 ==========
    for content, target_balance in [
        ("微信平账到100", 10000),
        ("今天微信平账到80", 8000),
    ]:
        cases.append(
            BulkCase(
                "平账", f"平账-{content}",
                [(mid(), "今天微信吃饭花了25元"), (mid(), content), (mid(), "是")],
                check=lambda u, conn, replies, tb=target_balance: _check_balance_adjust(
                    u, conn, replies, tb
                ),
            )
        )
    cases.append(
        BulkCase(
            "平账", "平账-直接调整",
            [(mid(), "平账10 微信对不上"), (mid(), "是")],
            check=lambda u, conn, replies: _check_balance_direct(u, conn, replies),
        )
    )

    # ========== 10. 时间段统计 / 报表真实性 ==========
    stats_cases = [
        "今天花了多少", "昨天", "本周花了多少", "上周", "本月花了多少", "上月",
        "这个月吃饭花了多少", "8月1日到8月10日花了多少", "最近7天花了多少",
        "今天详细一点", "今天收入多少", "8月15日花了多少", "7月3日",
    ]
    for content in stats_cases:
        cases.append(
            BulkCase(
                "报表统计", f"统计-{content}",
                [(mid(), "今天微信吃饭花了25元"), (mid(), content)],
                check=lambda u, conn, replies, c=content: _check_stats(u, conn, replies[1]),
            )
        )
    # 报表数字真实性：支出/收入与实际一致
    cases.append(
        BulkCase(
            "报表统计", "报表-数字真实",
            [
                (mid(), "今天微信吃饭花了25元"),
                (mid(), "今天支付宝打车花了35元"),
                (mid(), "今天银行卡收入100元"),
                (mid(), "今天"),
            ],
            check=lambda u, conn, replies: _check_report_numbers(u, conn, replies[3], "60.00", "100.00"),
        )
    )

    # ========== 11. 初始余额 / 账户余额 ==========
    for account, amount in itertools.product(["微信", "支付宝", "银行卡"], ["1000", "250.5", "5000"]):
        cases.append(
            BulkCase(
                "余额", f"初始余额-{account}{amount}",
                [(mid(), f"设置{account}余额{amount}"), (mid(), "微信还有多少钱")],
                check=lambda u, conn, replies, a=account, am=amount: _check_initial_balance(
                    u, conn, replies, a, am
                ),
            )
        )
    for content in ["微信还有多少钱", "支付宝余额", "总资产", "所有账户"]:
        cases.append(
            BulkCase(
                "余额", f"余额查询-{content}",
                [(mid(), "今天微信吃饭花了25元"), (mid(), content)],
                check=lambda u, conn, replies, c=content: _check_balance(u, conn, replies[1]),
            )
        )

    # ========== 12. 历史补记 ==========
    for date_str, account in itertools.product(
        ["8月15日", "7月3日", "6月28日", "2026年5月20日", "3月8日", "前天", "大前天"],
        ["微信", "支付宝", "银行卡"],
    ):
        content = f"{date_str}{account}吃饭花了25元"

        def check_backfill(u, conn, replies, date_str=date_str, account=account):
            row = conn.execute(
                "SELECT * FROM expenses WHERE user_id=? ORDER BY id DESC LIMIT 1", (u,)
            ).fetchone()
            assert row is not None
            expected_date = parse_expense_date(date_str + "吃饭", TODAY)
            assert row["expense_date"] == expected_date.isoformat(), (
                f"补记日期错误: {row['expense_date']} != {expected_date}"
            )
            acc = s.account_balance_by_name(conn, "default", u, account)
            assert acc["balance_cents"] == -2500

        cases.append(BulkCase("历史补记", f"补记-{content}", [(mid(), content)], check=check_backfill))

    # ========== 13. 防重 / 防抖 ==========
    dup_mid = mid()
    cases.append(
        BulkCase(
            "防重防抖", "防重-同消息ID",
            [(dup_mid, "今天微信吃饭花了25元"), (dup_mid, "今天微信吃饭花了25元")],
            check=lambda u, conn, replies: _check_dedup(replies),
        )
    )
    cases.append(
        BulkCase(
            "防重防抖", "防抖-同内容新消息",
            [(mid(), "今天微信奶茶15元"), (mid(), "今天微信奶茶15元")],
            check=lambda u, conn, replies: _check_debounce(replies),
        )
    )

    # ========== 14. 导出命令 ==========
    cases.append(
        BulkCase(
            "导出", "导出-命令",
            [
                (mid(), "今天微信吃饭花了25元"),
                (mid(), "今天支付宝打车花了35元"),
                (mid(), "导出"),
            ],
            check=lambda u, conn, replies: _check_export_cmd(replies),
        )
    )

    # ========== 15. 多平台账单导入（直接解析） ==========
    import_cases = _build_import_cases()
    cases.extend(import_cases)
    return cases


def _build_import_cases():
    cases = []
    mid_counter = {"n": 0}

    def add(label, csv_text, expect_count, field_checks):
        def check_import(u, conn, replies):
            result, items = parse_import(
                csv_text.encode("utf-8-sig"), label + ".csv", s.load_categories(conn)
            )
            assert result.success_rows == expect_count, (
                f"{label}: 成功 {result.success_rows} != {expect_count}，错误 {result.errors[:3]}"
            )
            for it, checks in zip(items, field_checks):
                for field, value in checks.items():
                    got = getattr(it, field)
                    assert got == value, f"{label}: {field}={got!r} != {value!r}"

        mid_counter["n"] += 1
        cases.append(
            BulkCase(
                "多平台导入", label, [(f"imp{mid_counter['n']}", "忽略")],
                check=check_import,
            )
        )

    add(
        "微信支付账单",
        "交易时间,交易类型,交易对方,商品,收/支,金额,支付方式,当前状态,交易单号,商户单号,备注\n"
        "2026-08-01 12:30:00,商户消费,老王烧烤,烧烤,支出,¥86.00,微信支付,支付成功,4200001,100001,和朋友吃烧烤\n"
        "2026-08-02 08:00:00,商户消费,地铁公司,地铁,支出,¥4.00,微信支付,支付成功,4200002,100002,早高峰地铁\n",
        2,
        [
            {
                "expense_date": datetime.date(2026, 8, 1),
                "amount_cents": 8600,
                "tx_type": "expense",
                "account_name": "微信",
                "description": "和朋友吃烧烤",
            },
            {
                "expense_date": datetime.date(2026, 8, 2),
                "amount_cents": 400,
                "description": "早高峰地铁",
            },
        ],
    )
    add(
        "支付宝账单",
        "交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注\n"
        "2026-08-03 10:00:00,餐饮美食,老王饭店,xxx@xx.com,午饭,支出,¥35.00,余额宝,交易成功,alipay1,merchant1,和同事午饭\n"
        "2026-08-04 09:00:00,交通出行,滴滴出行,yyy,打车,支出,¥12.00,花呗,交易成功,alipay2,merchant2,上班打车\n",
        2,
        [
            {
                "expense_date": datetime.date(2026, 8, 3),
                "amount_cents": 3500,
                "category_name": "餐饮",
                "description": "和同事午饭",
            },
            {
                "expense_date": datetime.date(2026, 8, 4),
                "amount_cents": 1200,
                "description": "上班打车",
            },
        ],
    )
    add(
        "钱迹导出",
        "日期,类型,分类,账户,金额,备注\n"
        "2026-08-05 13:00:00,支出,餐饮,微信,25.5,早餐\n"
        "2026-08-06 18:00:00,收入,工资,银行卡,5000,八月工资\n",
        2,
        [
            {
                "expense_date": datetime.date(2026, 8, 5),
                "amount_cents": 2550,
                "account_name": "微信",
                "description": "早餐",
            },
            {
                "expense_date": datetime.date(2026, 8, 6),
                "amount_cents": 500000,
                "tx_type": "income",
                "account_name": "银行卡",
                "description": "八月工资",
            },
        ],
    )
    add(
        "随手记导出",
        "交易日期,账户,分类,金额,备注\n"
        "2026-08-07,微信,餐饮,30,午饭\n"
        "2026-08-08,银行卡,交通,18.5,地铁充值\n",
        2,
        [
            {"expense_date": datetime.date(2026, 8, 7), "amount_cents": 3000, "account_name": "微信"},
            {"expense_date": datetime.date(2026, 8, 8), "amount_cents": 1850, "account_name": "银行卡"},
        ],
    )
    add(
        "鲨鱼记账导出",
        "日期,分类,账户,金额,备注\n"
        "2026-08-09,餐饮,支付宝,22,夜宵\n",
        1,
        [
            {"expense_date": datetime.date(2026, 8, 9), "amount_cents": 2200, "account_name": "支付宝"},
        ],
    )
    # 垃圾列排除：微信昵称/对方账号/单号绝不能进备注
    add(
        "垃圾列排除",
        "交易时间,微信昵称,交易对方,对方账号,商品,收/支,金额,支付方式,交易单号,备注\n"
        "2026-08-10 10:00:00,大聪明,小明,openid_xxx,奶茶,支出,¥15.00,微信支付,单号999,下午茶\n",
        1,
        [{"description": "下午茶"}],
    )
    return cases


# ---------- 断言辅助 ----------

def _expect_pending(reply, keyword):
    assert reply.status == "pending_confirmation", f"应为待确认，实际 {reply.status}: {reply.reply_content[:60]}"
    assert keyword in reply.reply_content, f"提示缺少「{keyword}」: {reply.reply_content}"


def _check_delete(u, conn, replies):
    assert replies[1].status == "pending_confirmation", f"删除应待确认: {replies[1].status}"
    assert "确认撤销" in replies[1].reply_content
    assert replies[2].status == "success" and "已撤销" in replies[2].reply_content
    voided = conn.execute(
        "SELECT COUNT(*) AS n FROM expenses WHERE user_id=? AND status='voided'", (u,)
    ).fetchone()
    assert voided["n"] >= 1


def _check_delete_past(u, conn, replies):
    assert replies[1].status == "pending_confirmation" and "确认撤销" in replies[1].reply_content
    assert replies[2].status == "success" and "已撤销" in replies[2].reply_content
    row = conn.execute(
        "SELECT status FROM expenses WHERE user_id=? ORDER BY id DESC LIMIT 1", (u,)
    ).fetchone()
    assert row["status"] == "voided"
    assert s.account_balance_by_name(conn, "default", u, "微信")["balance_cents"] == 0


def _check_modify(u, conn, replies, new_cents, ask_first=False):
    if ask_first:
        assert replies[1].status == "pending_confirmation" and "改成多少" in replies[1].reply_content
        assert replies[2].status == "success" and "已修改" in replies[2].reply_content
    else:
        assert replies[1].status == "pending_confirmation" and "确认把" in replies[1].reply_content
        assert replies[2].status == "success" and "已修改" in replies[2].reply_content
    row = conn.execute(
        "SELECT amount_cents FROM expenses WHERE user_id=? ORDER BY id DESC LIMIT 1", (u,)
    ).fetchone()
    assert row["amount_cents"] == new_cents


def _check_transfer_with_fee(u, conn, transfer_cents=50000, fee_cents=30):
    rows = conn.execute(
        "SELECT tx_type, amount_cents FROM expenses WHERE user_id=? AND tx_type IN "
        "('transfer_out','transfer_in','fee') ORDER BY id DESC LIMIT 3",
        (u,),
    ).fetchall()
    types = {r["tx_type"] for r in rows}
    assert types == {"transfer_out", "transfer_in", "fee"}
    balances = {a["name"]: a["balance_cents"] for a in s.account_balances(conn, "default", u)}
    from_acc = conn.execute(
        "SELECT a.name FROM expenses e JOIN accounts a ON a.id=e.account_id "
        "WHERE e.user_id=? AND e.tx_type='transfer_out' ORDER BY e.id DESC LIMIT 1",
        (u,),
    ).fetchone()["name"]
    assert balances[from_acc] == -(transfer_cents + fee_cents)


def _check_refund(u, conn, content):
    row = conn.execute(
        "SELECT * FROM expenses WHERE user_id=? AND tx_type='refund' ORDER BY id DESC LIMIT 1", (u,)
    ).fetchone()
    assert row is not None, f"无退款记录: {content}"
    assert row["amount_cents"] > 0


def _check_stats(u, conn, reply):
    assert reply.status == "success", f"统计失败: {reply.status} {reply.reply_content[:60]}"
    assert "支出" in reply.reply_content or "收入" in reply.reply_content


def _check_report_numbers(u, conn, reply, expense, income):
    assert reply.status == "success"
    assert f"支出 {expense} 元" in reply.reply_content, f"报表支出不符: {reply.reply_content[:120]}"
    assert f"收入 {income} 元" in reply.reply_content, f"报表收入不符: {reply.reply_content[:120]}"


def _check_initial_balance(u, conn, replies, account, amount):
    assert replies[0].status == "success" and "初始余额" in replies[0].reply_content
    acc = s.account_balance_by_name(conn, "default", u, account)
    assert acc["balance_cents"] == int(round(float(amount) * 100))


def _check_balance(u, conn, reply):
    assert reply.status == "success"
    assert "余额" in reply.reply_content or "总资产" in reply.reply_content


def _check_dedup(replies):
    assert replies[0].status == "success"
    assert replies[1].status == "duplicate", f"防重失败: {replies[1].status} {replies[1].reply_content[:60]}"
    assert "已记录过" in replies[1].reply_content


def _check_debounce(replies):
    assert replies[0].status == "success"
    assert replies[1].status == "pending_confirmation", f"防抖失败: {replies[1].status}"
    assert "30 秒内" in replies[1].reply_content


def _check_balance_adjust(u, conn, replies, target_balance):
    assert replies[1].status == "pending_confirmation" and "平账" in replies[1].reply_content
    assert replies[2].status == "success" and "平账" in replies[2].reply_content
    acc = s.account_balance_by_name(conn, "default", u, "微信")
    assert acc["balance_cents"] == target_balance


def _check_balance_direct(u, conn, replies):
    assert replies[0].status == "pending_confirmation" and "平账" in replies[0].reply_content
    assert replies[1].status == "success" and "平账" in replies[1].reply_content


def _check_export_cmd(replies):
    assert replies[2].status == "success"
    assert "共 2 笔流水" in replies[2].reply_content


# ---------- 运行器 ----------

async def run_cases(user_id, cases):
    router = Router()
    conn = connect()
    failures = []
    for idx, case in enumerate(cases):
        case_user = f"{user_id}_{idx}"
        replies = []
        try:
            for step_mid, content in case.steps:
                reply = await router.handle(mkmsg(case_user, step_mid, content))
                replies.append(reply)
            if case.check:
                case.check(case_user, conn, replies)
        except Exception as exc:  # noqa: BLE001
            failures.append((case.section, case.label, str(exc), [r.reply_content[:100] for r in replies]))
        finally:
            s.clear_pending(conn, "default", case_user)
    await router.shutdown()
    conn.close()
    return failures


def run_bulk(db_path, secret_key_path, user_id="u_bulk"):
    global TODAY
    settings.db_path = Path(db_path)
    settings.secret_key_path = Path(secret_key_path)
    conn = connect()
    init_db(conn)
    settings_store.ensure_default_settings(conn)
    conn.close()
    TODAY = timeutil.today()
    cases = build_cases()
    failures = asyncio.run(run_cases(user_id, cases))
    return cases, failures


def main():
    tmp = Path(tempfile.mkdtemp(prefix="paas_bulk_"))
    cases, failures = run_bulk(tmp / "bulk.db", tmp / "secret.key")
    total = len(cases)
    passed = total - len(failures)
    from collections import Counter

    section_total = Counter(c.section for c in cases)
    section_failed = Counter(f[0] for f in failures)
    report = tmp / "bulk_report.txt"
    with open(report, "w", encoding="utf-8") as f:
        f.write(f"total={total} passed={passed} failed={len(failures)}\n")
        for section, n in section_total.most_common():
            f.write(f"  {section}: {n} 条，失败 {section_failed.get(section, 0)}\n")
        if failures:
            f.write("\n失败明细：\n")
            for section, label, err, replies in failures:
                f.write(f"[{section}] {label}\n  {err}\n   replies: {replies}\n")
    print(f"total={total} passed={passed} failed={len(failures)}")
    for section, n in section_total.most_common():
        print(f"  {section}: {n} 条，失败 {section_failed.get(section, 0)}")
    print(f"report: {report}")
    if failures:
        for _, label, err, _ in failures[:30]:
            print(f"[FAIL] {label} :: {err[:160]}")
        raise SystemExit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    os.environ.setdefault("SECRET_KEY", "bulk-test-secret-key")
    main()
