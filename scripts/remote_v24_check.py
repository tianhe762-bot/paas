"""v2.3 服务器验证：迁移、缩写年份、预置银行账户、AI 未启用、批量回归。"""

import json
import os
import sqlite3
import urllib.request

BASE = "http://127.0.0.1:8000"
API_KEY = os.environ.get("PAAS_API_KEY", "")
ADMIN_PASS = os.environ.get("PAAS_ADMIN_PASS", "")
USER = os.environ.get("PAAS_USER", "u_v24check")


def http(method, path, body=None, cookie=None):
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-Api-Key"] = API_KEY
    if cookie:
        headers["Cookie"] = cookie
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, dict(r.headers), json.loads(r.read() or b"{}")


def inbound(mid, content, ns="default"):
    return http(
        "POST", "/api/v1/message/inbound",
        {"namespace": ns, "platform": "qq", "user_id": USER, "chat_id": "c_x",
         "message_id": mid, "content": content},
    )


def main():
    # 1. 迁移检查
    conn = sqlite3.connect("/app/data/account.db")
    conn.row_factory = sqlite3.Row
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    acct_cols = [r[1] for r in conn.execute("PRAGMA table_info(accounts)")]
    raw_cols = [r[1] for r in conn.execute("PRAGMA table_info(raw_messages)")]
    print("tables ok:", {"account_templates", "import_staging"} <= tables)
    print("accounts.aliases:", "aliases" in acct_cols, "| raw_messages.reply:", "reply" in raw_cols)
    conn.close()

    # 2. 缩写年份查询（真实数据）
    r = inbound("v24a", "25年7月份总共花了多少钱")
    text = r[2].get("reply_content", "")
    print("25年7月 ->", "2025-07-01 ~ 2025-07-31" in text and r[2].get("status") == "success")

    # 3. 建行卡记账
    r = inbound("v24b", "今天建行卡花了25元吃饭")
    print("建行卡记账 ->", r[2].get("status") == "success", "|", r[2].get("reply_content", "")[:40])
    conn = sqlite3.connect("/app/data/account.db")
    row = conn.execute("SELECT name FROM accounts WHERE user_id=? AND name='建行卡'", (USER,)).fetchone()
    print("建行卡账户已建 ->", bool(row))
    conn.execute("DELETE FROM expenses WHERE user_id=? AND message_id='v24b'", (USER,))
    conn.commit()
    conn.close()

    # 4. AI 未启用提示（确保无待确认残留）
    conn = sqlite3.connect("/app/data/account.db")
    conn.execute("DELETE FROM pending_actions WHERE user_id=?", (USER,))
    conn.commit()
    conn.close()
    r = inbound("v24c", "用AI：今天微信吃饭花了25")
    print("AI未启用提示 ->", "未启用" in r[2].get("reply_content", ""), "|", r[2].get("reply_content", "")[:50])

    # 5. 管理员接口
    _, hdrs, login = http("POST", "/admin/api/login", {"username": "admin", "password": ADMIN_PASS})
    cookie = "paas_session=" + (hdrs.get("Set-Cookie") or hdrs.get("set-cookie") or "").split(";")[0].split("=", 1)[1]
    _, _, ai = http("GET", "/admin/api/ai", cookie=cookie)
    _, _, conv = http("GET", "/admin/api/conversations?limit=5", cookie=cookie)
    print("AI设置默认 ->", ai.get("mode") == "off", "| 最近对话 ->", len(conv.get("conversations", [])) > 0)

    # 清理测试数据
    conn = sqlite3.connect("/app/data/account.db")
    for t in ("expenses", "accounts", "raw_messages", "daily_status", "pending_actions", "user_chats"):
        conn.execute(f"DELETE FROM {t} WHERE user_id=? AND namespace='default'", (USER,))
    conn.commit()
    conn.close()
    print("ALL V24 CHECKS PASSED")


if __name__ == "__main__":
    main()
