"""服务器端多机器人/多用户功能验证（配合远程部署使用）。
用法：PAAS_API_KEY=xxx PAAS_ADMIN_PASS=xxx python scripts/remote_v3_check.py
"""

import json
import os
import sqlite3
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
API_KEY = os.environ.get("PAAS_API_KEY", "")
ADMIN_PASS = os.environ.get("PAAS_ADMIN_PASS", "")


def http(method, path, body=None, cookie=None):
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-Api-Key"] = API_KEY
    if cookie:
        headers["Cookie"] = cookie
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, dict(r.headers), json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read() or b"{}")


def inbound(ns, mid, content, user="u_v3check"):
    return http(
        "POST", "/api/v1/message/inbound",
        {"namespace": ns, "platform": "qq", "user_id": user, "chat_id": "c_x",
         "message_id": mid, "content": content},
    )


def main():
    # 1. 迁移完整性
    conn = sqlite3.connect("/app/data/account.db")
    conn.row_factory = sqlite3.Row
    default_expenses = conn.execute(
        "SELECT COUNT(*) AS n FROM expenses WHERE namespace='default'"
    ).fetchone()["n"]
    bots = conn.execute("SELECT platform, bot_id, name, owner_id FROM bot_configs").fetchall()
    admin_role = conn.execute("SELECT role FROM admin_users ORDER BY id LIMIT 1").fetchone()["role"]
    print(f"迁移检查: default流水={default_expenses}, 机器人={[dict(b) for b in bots]}, 管理员角色={admin_role}")
    conn.close()
    assert default_expenses >= 3000, "历史数据丢失！"
    assert admin_role == "admin"

    # 2. 管理员登录
    status, headers, _ = http("POST", "/admin/api/login", {"username": "admin", "password": ADMIN_PASS})
    assert status == 200, f"登录失败 {status}"
    cookie = "paas_session=" + _extract_cookie(headers)
    _, _, me = http("GET", "/admin/api/me", cookie=cookie)
    print("管理员:", me)
    assert me.get("role") == "admin"

    # 3. 机器人列表
    _, _, bots_resp = http("GET", "/admin/api/bots", cookie=cookie)
    print("机器人列表:", [b["bot_id"] for b in bots_resp["bots"]])

    # 4. 创建机器人 + 数据隔离
    _, _, created = http(
        "POST", "/admin/api/bots", {"platform": "qq", "name": "验证机器人",
                                    "enabled": False, "fields": {"app_id": "v3"}}, cookie=cookie,
    )
    bot2 = created["bot_id"]
    r1 = inbound("default", "v3m1", "今天微信吃饭花了25元")
    r2 = inbound(bot2, "v3m2", "今天微信吃饭花了88元")
    assert r1[2].get("status") == "success", r1
    assert r2[2].get("status") == "success", r2
    q1 = inbound("default", "v3m3", "微信还有多少钱")
    q2 = inbound(bot2, "v3m4", "微信还有多少钱")
    print("隔离检查:", q1[2]["reply_content"].splitlines()[0], "|", q2[2]["reply_content"].splitlines()[0])
    assert "-25.00" in q1[2]["reply_content"]
    assert "-88.00" in q2[2]["reply_content"]

    # 5. 用户创建与权限
    _, _, u = http("POST", "/admin/api/users",
                   {"username": "alice_v3", "password": "alice-pass-123", "role": "user"}, cookie=cookie)
    assert u.get("ok"), u
    _, uh, ul = http("POST", "/admin/api/login", {"username": "alice_v3", "password": "alice-pass-123"})
    alice_cookie = "paas_session=" + _extract_cookie(uh)
    _, _, ub = http("GET", "/admin/api/bots", cookie=alice_cookie)
    assert ub["bots"] == [], "普通用户不应看到他人机器人"
    code, _, _ = http("GET", "/admin/api/users", cookie=alice_cookie)
    assert code == 403, "普通用户不应能访问用户管理"

    # 6. 清理
    http("DELETE", f"/admin/api/bots/{bot2}", cookie=cookie)
    http("DELETE", "/admin/api/users/99999", cookie=cookie)  # 无操作
    conn = sqlite3.connect("/app/data/account.db")
    for table in ("expenses", "accounts", "daily_status", "pending_actions", "user_chats", "raw_messages"):
        conn.execute(f"DELETE FROM {table} WHERE namespace = ? OR user_id = 'u_v3check'", (bot2,))
    conn.execute("DELETE FROM admin_users WHERE username = 'alice_v3'")
    conn.commit()
    conn.close()
    print("ALL V3 CHECKS PASSED")


def _extract_cookie(headers):
    raw = headers.get("Set-Cookie") or headers.get("set-cookie") or ""
    return raw.split(";")[0].split("=", 1)[1]


if __name__ == "__main__":
    main()
