"""服务器真实数据查询验证：年份/月份报表、余额、总资产。"""

import json
import os
import urllib.request

BASE = "http://127.0.0.1:8000/api/v1/message/inbound"
KEY = os.environ.get("PAAS_API_KEY", "")
USER = os.environ.get("PAAS_USER", "17AF1C3E543307311FE7DFC84B6B5E82")


def q(mid, content):
    body = json.dumps(
        {"namespace": "default", "platform": "qq", "user_id": USER,
         "chat_id": "c_x", "message_id": mid, "content": content}
    ).encode()
    req = urllib.request.Request(BASE, data=body, headers={"X-Api-Key": KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def main():
    cases = [
        ("2025年7月份总共花了多少钱", "2025-07"),
        ("生成2025年的账单", "2025-01-01"),
        ("2025年12月", "2025-12"),
        ("我现在的余额是多少", "余额"),
        ("我总共有多少钱", "总资产"),
        ("今天", "支出"),
    ]
    for i, (content, expect) in enumerate(cases):
        r = q(f"qc{i}", content)
        text = (r.get("reply_content") or "").replace("\n", " / ")
        ok = r.get("status") == "success" and expect in text
        print(("PASS" if ok else "FAIL"), content, "->", text[:120])


if __name__ == "__main__":
    main()

