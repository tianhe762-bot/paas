"""服务器端到端自测脚本：通过入站 API 验证记账/补问/收入/转账/撤销/修改/平账/统计。
用法：PAAS_API_KEY=<key> PAAS_BASE=http://127.0.0.1:8000 python scripts/remote_selftest.py
"""

import json
import os
import urllib.error
import urllib.request


def post(base, key, user, mid, content):
    body = json.dumps(
        {
            "platform": "qq",
            "user_id": user,
            "chat_id": "c_" + user,
            "message_id": mid,
            "content": content,
        }
    ).encode()
    req = urllib.request.Request(
        base,
        data=body,
        headers={"X-Api-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def main():
    base = os.environ.get("PAAS_BASE", "http://127.0.0.1:8000/api/v1/message/inbound")
    key = os.environ["PAAS_API_KEY"]
    user = os.environ.get("PAAS_USER", "u_selftest_remote")
    counter = {"n": 0}

    def nid():
        counter["n"] += 1
        return f"r{counter['n']}"

    def show(label, result):
        status, data = result
        text = (data.get("reply_content") or "").replace("\n", " / ")
        print(f"{label} -> {data.get('status')} | {text[:140]}")

    show("记账(含账户时间)", post(base, key, user, nid(), "今天微信吃饭花了25"))
    show("缺账户", post(base, key, user, nid(), "午饭30"))
    show("回答账户", post(base, key, user, nid(), "支付宝"))
    show("回答时间", post(base, key, user, nid(), "昨天"))
    show("收入", post(base, key, user, nid(), "今天银行卡收入5000"))
    show("转账+手续费", post(base, key, user, nid(), "今天微信转银行卡500元，手续费0.3元"))
    show("余额查询(应为负数)", post(base, key, user, nid(), "微信还有多少钱"))
    show("总资产", post(base, key, user, nid(), "总资产"))
    show("删除", post(base, key, user, nid(), "删除刚才那笔"))
    show("确认删除", post(base, key, user, nid(), "是"))
    show("修改", post(base, key, user, nid(), "刚才那笔不是30，是35"))
    show("确认修改", post(base, key, user, nid(), "是"))
    show("平账到", post(base, key, user, nid(), "微信平账到80"))
    show("确认平账", post(base, key, user, nid(), "是"))
    show("今天统计", post(base, key, user, nid(), "今天"))
    show("本月统计", post(base, key, user, nid(), "这个月花了多少"))
    show("详细", post(base, key, user, nid(), "今天详细一点"))
    show("防重(重发第1条)", post(base, key, user, "r1", "今天微信吃饭花了25"))


if __name__ == "__main__":
    main()
