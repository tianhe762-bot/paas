import datetime
import re
from typing import Optional

from paas.models import CategoryRow, ParsedItem

CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}

ACCOUNTS: list[tuple[str, list[str]]] = [
    ("微信", ["微信", "wechat", "wx"]),
    ("支付宝", ["支付宝", "余额宝", "alipay"]),
    ("信用卡", ["信用卡", "花呗", "白条"]),
    ("银行卡", ["银行卡", "储蓄卡", "银行", "建行", "工行", "招行", "农行"]),
    ("现金", ["现金", "现钞", "钱包"]),
]

TRANSFER_RE = re.compile(
    r"转(?:账|给|到|出|入|钱)"
    r"|转\s*[0-9一二两三四五六七八九十百千万零.]+(?:元|块)?\s*(?:给|到|入)"
    r"|(?<=[\u4e00-\u9fa5])转(?=[\u4e00-\u9fa5])"
    r"|^转"
)
FEE_RE = re.compile(r"手续费|服务费")
INCOME_RE = re.compile(r"收入|赚了|赚到|收到|入账|工资|报销|红包|进账|卖了")
REFUND_RE = re.compile(r"退款|退钱|退回|退回来")

TIME_WORD_RE = re.compile(
    r"今天|昨天|前天|大前天|今早|今晚|今午|"
    r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]?|\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*[日号]?"
)

AMOUNT_RE = re.compile(
    r"(?<!\d)(\d+(?:\.\d+)?|[一二两三四五六七八九十百千万零]+)"
    r"\s*(元|块|块钱|rmb|RMB)?"
)
COLLOQ_BLOCK_RE = re.compile(r"(\d+)\s*块\s*([0-9零一二两三四五六七八九十])")
DATE_STRIP_RE = re.compile(
    r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*[日号]?|\d{1,2}\s*月\s*\d{1,2}\s*[日号]?"
)
FILLER_AFTER = "下点些次个天杯份口"

CLAUSE_SEP_RE = re.compile(
    r"[，,；;。！？!?\n]+|(?=另外|随后|然后|并且|还有|接着|再花|又花|加上|以及)"
)

DATE_WORDS = {
    "大前天": -3,
    "前天": -2,
    "昨天": -1,
    "今天": 0,
    "今晚": 0,
}
MONTH_DAY_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?")
YEAR_MONTH_DAY_RE = re.compile(
    r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?"
)

DESC_CLEAN_RE = re.compile(
    r"今天|昨天|前天|大前天|今晚|今早|今晚|早上|上午|中午|下午|晚上|凌晨|"
    r"花了|花掉|付了|用了|消费了|消费|买了|买了个|去了|吃了|点了|"
    r"(?:和(?:同事|朋友|家人|老婆|老公|对象|同学|兄弟|姐妹|室友|孩子|客户|父母|爸妈))"
)


def parse_chinese_number(val: str) -> Optional[float]:
    """将汉字数字转为浮点数；支持"三十五/二十多/一百二/两千五/三千零五"等口语。"""
    val = val.strip()
    if not val:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", val):
        return float(val)

    # 口语省略：一百二=120，两千五=2500，一万二=12000
    m = re.fullmatch(r"([一二两三四五六七八九])百([一二两三四五六七八九])", val)
    if m:
        return CN_DIGITS[m.group(1)] * 100 + CN_DIGITS[m.group(2)] * 10
    m = re.fullmatch(r"([一二两三四五六七八九])千([一二两三四五六七八九])", val)
    if m:
        return CN_DIGITS[m.group(1)] * 1000 + CN_DIGITS[m.group(2)] * 100
    m = re.fullmatch(r"([一二两三四五六七八九])万([一二两三四五六七八九])", val)
    if m:
        return CN_DIGITS[m.group(1)] * 10000 + CN_DIGITS[m.group(2)] * 1000

    total = 0.0
    current = 0.0
    for ch in val:
        if ch in CN_DIGITS:
            current = float(CN_DIGITS[ch])
        elif ch in CN_UNITS:
            unit = CN_UNITS[ch]
            if unit == 10000:
                total = (total + (current or 1.0)) * unit
                current = 0.0
            else:
                total += (current or 1.0) * unit
                current = 0.0
        else:
            return None
    return total + current


def yuan_to_cents(amount: float) -> int:
    return int(round(amount * 100))


def split_clauses(text: str) -> list[str]:
    parts = CLAUSE_SEP_RE.split(text)
    out = []
    for p in parts:
        p = p.strip(" \t，。！？")
        if not p:
            continue
        p = re.sub(
            r"^(?:另外|随后|然后|并且|还有|接着|再花|又花|加上|以及)", "", p
        ).strip(" \t，。！？")
        if p:
            out.append(p)
    return out


def parse_amount_cents(clause: str) -> Optional[int]:
    """提取金额并转为分；支持 86.5 / 86块5 / 三十五 / 二十多。"""
    clause = DATE_STRIP_RE.sub(" ", clause)  # 去掉"8月15日"等日期，避免把 8 当金额
    m = COLLOQ_BLOCK_RE.search(clause)
    if m:
        yuan = float(m.group(1))
        tail = m.group(2)
        jiao = float(CN_DIGITS[tail]) if tail in CN_DIGITS else float(tail)
        return yuan_to_cents(yuan + jiao / 10.0)

    pos = 0
    while True:
        m = AMOUNT_RE.search(clause, pos)
        if not m:
            return None
        raw = m.group(1)
        nxt = clause[m.end()] if m.end() < len(clause) else ""
        if len(raw) == 1 and raw in CN_DIGITS and nxt in FILLER_AFTER:
            # 跳过"记一下/一点/一些"等语气词里的数字
            pos = m.end()
            continue
        break
    raw = m.group(1)
    if re.fullmatch(r"\d+(\.\d+)?", raw):
        amount = float(raw)
    else:
        amount = parse_chinese_number(raw)
        if amount is None:
            return None
    if amount <= 0:
        return None
    return yuan_to_cents(amount)


def parse_amount_with_unit(text: str) -> Optional[int]:
    """只识别带明确单位（元/块）的金额，用于删除/修改目标定位，避免把"最后一笔"的"一"当金额。"""
    m = re.search(
        r"(\d+(?:\.\d+)?|[一二两三四五六七八九十百千万零]+)\s*(?:元|块|块钱|rmb|RMB)",
        text,
    )
    if not m:
        return None
    raw = m.group(1)
    if re.fullmatch(r"\d+(\.\d+)?", raw):
        return yuan_to_cents(float(raw))
    amount = parse_chinese_number(raw)
    return yuan_to_cents(amount) if amount else None


def detect_accounts(clause: str) -> list[str]:
    found: list[tuple[int, str]] = []
    for name, keywords in ACCOUNTS:
        pos = -1
        for kw in keywords:
            p = clause.find(kw)
            if p != -1 and (pos == -1 or p < pos):
                pos = p
        if pos != -1 and name not in (n for _, n in found):
            found.append((pos, name))
    found.sort(key=lambda x: x[0])
    return [name for _, name in found]


def detect_tx_type(clause: str) -> str:
    if REFUND_RE.search(clause):
        return "refund"
    if FEE_RE.search(clause):
        return "fee"
    if INCOME_RE.search(clause):
        return "income"
    if TRANSFER_RE.search(clause):
        return "transfer_out"
    return "expense"


def has_time_expression(text: str) -> bool:
    return TIME_WORD_RE.search(text) is not None


def parse_expense_date(clause: str, base_date: datetime.date) -> datetime.date:
    for word, delta in DATE_WORDS.items():
        if word in clause:
            return base_date + datetime.timedelta(days=delta)
    m = YEAR_MONTH_DAY_RE.search(clause)
    if m:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = MONTH_DAY_RE.search(clause)
    if m:
        try:
            return datetime.date(base_date.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return base_date
    return base_date


def clean_description(clause: str) -> str:
    desc = DESC_CLEAN_RE.sub(" ", clause)
    desc = AMOUNT_RE.sub(" ", desc)
    desc = COLLOQ_BLOCK_RE.sub(" ", desc)
    desc = re.sub(r"\s+", "", desc)
    desc = desc.strip("，。！？、,;:： ")
    return desc


def match_category(clause: str, categories: list[CategoryRow]) -> CategoryRow:
    best: CategoryRow | None = None
    best_score = 0.0
    for cat in categories:
        score = 0.0
        if cat.name in clause:
            score += 100.0  # 分类名直接命中，权重最高
        for kw in cat.keywords:
            if kw and kw in clause:
                score += len(kw)  # 长关键词更具体，权重更高
        if score > best_score:
            best_score = score
            best = cat
    if best is not None:
        return best
    return next((c for c in categories if c.name == "其他"), categories[-1])


def parse_expenses(
    text: str,
    categories: list[CategoryRow],
    base_date: datetime.date,
) -> list[ParsedItem]:
    items: list[ParsedItem] = []
    global_accounts = detect_accounts(text)
    global_date = parse_expense_date(text, base_date)
    for clause in split_clauses(text):
        amount_cents = parse_amount_cents(clause)
        if amount_cents is None:
            continue
        cat = match_category(clause, categories)
        desc = clean_description(clause) or cat.name
        accounts = detect_accounts(clause)
        if not accounts:
            accounts = global_accounts  # 跨子句推断：如"花了86块5元，微信支付"
        tx_type = detect_tx_type(clause)
        account_name = accounts[0] if accounts else ""
        to_account_name = ""
        if tx_type == "transfer_out":
            if len(accounts) >= 2:
                account_name, to_account_name = accounts[0], accounts[1]
            elif len(accounts) == 1 and len(global_accounts) >= 2:
                other = next((a for a in global_accounts if a != accounts[0]), "")
                trans_pos = clause.find("转")
                acc_pos = clause.find(accounts[0])
                if trans_pos != -1 and acc_pos > trans_pos:
                    # "转500元给银行卡，从微信扣"：子句账户在"转"后，是目标
                    account_name, to_account_name = other, accounts[0]
                else:
                    account_name, to_account_name = accounts[0], other
            else:
                account_name = accounts[0] if accounts else ""
        elif tx_type == "fee" and items and items[-1].tx_type == "transfer_out" and not accounts:
            # 手续费跟随上一笔转账（如"微信转银行卡500元，手续费0.3元"）
            account_name = items[-1].account_name
        clause_date = parse_expense_date(clause, base_date)
        if not has_time_expression(clause):
            clause_date = global_date  # 跨子句推断时间，如"花了25，今天"
        items.append(
            ParsedItem(
                expense_date=clause_date,
                category_id=cat.id,
                category_name=cat.name,
                category_icon=cat.icon,
                account_name=account_name,
                to_account_name=to_account_name,
                tx_type=tx_type,
                amount_cents=amount_cents,
                description=desc,
            )
        )
    return items


def parse_balance_command(text: str) -> dict:
    """解析平账指令。mode=target: 平账到 X 元；mode=amount: 平账 X 元 [备注]。"""
    body = re.sub(r"^(?:平账|对账)", "", text).strip(" ：:，,。")
    accounts = detect_accounts(body)
    account = accounts[0] if accounts else ""
    m = re.search(r"到\s*([0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十百千万零]+)", body)
    if m:
        raw = m.group(1)
        amount = float(raw) if re.fullmatch(r"\d+(\.\d+)?", raw) else parse_chinese_number(raw)
        if amount and amount > 0:
            return {"account": account, "mode": "target", "amount_cents": yuan_to_cents(amount), "note": ""}
    m = AMOUNT_RE.search(body)
    if m:
        raw = m.group(1)
        amount = float(raw) if re.fullmatch(r"\d+(\.\d+)?", raw) else parse_chinese_number(raw)
        if amount and amount > 0:
            note = AMOUNT_RE.sub(" ", body)
            note = re.sub(r"元|块|块钱|rmb|RMB", "", note).strip(" ：:，,。")
            return {"account": account, "mode": "amount", "amount_cents": yuan_to_cents(amount), "note": note}
    return {"account": account, "mode": "", "amount_cents": 0, "note": ""}


def _to_cents(raw: str) -> Optional[int]:
    amount = float(raw) if re.fullmatch(r"\d+(\.\d+)?", raw) else parse_chinese_number(raw)
    if amount is None or amount <= 0:
        return None
    return yuan_to_cents(amount)


def parse_modify_request(text: str) -> dict:
    """解析"刚才那笔不是25，是35"类修改请求，返回 {old_cents, new_cents}。"""
    old_cents = None
    new_cents = None
    m = re.search(r"不是\s*([0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十百千万零]+)", text)
    if m:
        old_cents = _to_cents(m.group(1))
    m = re.search(r"(?:改成|改为|改到|(?<!不)是)\s*([0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十百千万零]+)", text)
    if m:
        new_cents = _to_cents(m.group(1))
    return {"old_cents": old_cents, "new_cents": new_cents}


def parse_time_range(text: str, base: datetime.date) -> Optional[tuple[datetime.date, datetime.date]]:
    """识别统计时间段：今天/昨天/本周/上周/本月/上月/X月/X月Y日到X月Z日/最近N天。"""
    if "今天" in text:
        return base, base
    if "昨天" in text:
        d = base - datetime.timedelta(days=1)
        return d, d
    if "前天" in text:
        d = base - datetime.timedelta(days=2)
        return d, d
    if "上周" in text:
        monday = base - datetime.timedelta(days=base.weekday())
        prev_monday = monday - datetime.timedelta(days=7)
        return prev_monday, prev_monday + datetime.timedelta(days=6)
    if "本周" in text or "这周" in text:
        monday = base - datetime.timedelta(days=base.weekday())
        return monday, base
    if "上月" in text:
        first = base.replace(day=1)
        prev_last = first - datetime.timedelta(days=1)
        return prev_last.replace(day=1), prev_last
    if "本月" in text or "这个月" in text:
        return base.replace(day=1), base
    m = re.search(
        r"(\d{4})年(\d{1,2})月(\d{1,2})[日号]?(?:到|至|-|~|—)(\d{4})年?(\d{1,2})月(\d{1,2})[日号]?",
        text,
    )
    if m:
        try:
            s = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            e = datetime.date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
            if s <= e:
                return s, e
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})月(\d{1,2})[日号]?(?:到|至|-|~|—)(\d{1,2})月?(\d{1,2})[日号]?", text)
    if m:
        try:
            s = datetime.date(base.year, int(m.group(1)), int(m.group(2)))
            e = datetime.date(base.year, int(m.group(3)), int(m.group(4)))
            if s <= e:
                return s, e
        except ValueError:
            pass
    m = re.fullmatch(r"\s*(\d{1,2})\s*月\s*", text) or re.search(r"(\d{1,2})月的统计|(\d{1,2})月花了", text)
    if m:
        month = int(m.group(1) or m.group(2))
        try:
            first = datetime.date(base.year, month, 1)
            last = (first.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)
            return first, min(last, base)
        except ValueError:
            pass
    m = re.search(r"最近\s*(\d+)\s*[天日]", text)
    if m:
        days = int(m.group(1))
        return base - datetime.timedelta(days=days - 1), base
    m = re.search(r"(?:^|[^0-9])(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?(?:$|[^0-9])", text)
    if m:
        try:
            day = datetime.date(base.year, int(m.group(1)), int(m.group(2)))
            return day, day
        except ValueError:
            pass
    return None
