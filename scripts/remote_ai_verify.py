"""AI 重构远端验证：管理 API 读写 + 轻量业务回归（自动清理测试数据）。
用法：python3 scripts/remote_ai_verify.py  （在服务器 host 上运行）
"""

import json
import os
import sqlite3
import urllib.error
import urllib.request

BASE = os.environ.get("PAAS_BASE", "http://127.0.0.1:8000")
API_KEY = os.environ.get("PAAS_API_KEY", "")
ENV = "/opt/paas/.env"
DB = "/opt/paas/data/account.db"
NS = "selftest_ai_20260829"
USER = "u_ai_verify"


def env_value(key):
    try:
        with open(ENV, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def http(method, path, data=None, headers=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers=headers or {"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return r.status, json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body.decode(errors="replace")


def inbound(mid, content, msg_type="text"):
    body = {
        "namespace": NS,
        "platform": "qq",
        "user_id": USER,
        "chat_id": "c_" + USER,
        "message_id": mid,
        "content": content,
        "message_type": msg_type,
    }
    req = urllib.request.Request(
        BASE + "/api/v1/message/inbound",
        data=json.dumps(body).encode(),
        headers={"X-Api-Key": API_KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def show(label, result):
    status, data = result
    text = (data.get("reply_content") or data.get("detail") or "").replace("\n", " / ")
    print(f"[{label}] http={status} status={data.get('status')} | {text[:180]}")
    return data


def main():
    print("== 1. 管理员登录 + AI 设置 ==")
    admin_pass = env_value("ADMIN_PASSWORD")
    if not API_KEY:
        print("API_KEY 缺失，跳过管理 API 与入站测试")
        return
    login = http(
        "POST",
        "/admin/api/login",
        {"username": "admin", "password": admin_pass},
    )
    token = None
    if login[0] == 200 and login[1]:
        token = login[1].get("token")
        print(f"login ok token_len={len(token or '')}")
    else:
        print(f"login failed http={login[0]} detail={login[1]}")
    ai_headers = {"Content-Type": "application/json"}
    if token:
        ai_headers["Authorization"] = f"Bearer {token}"

    st, ai = http("GET", "/admin/api/ai", headers=ai_headers)
    print(f"GET /admin/api/ai http={st}")
    if st == 200 and ai:
        for k in sorted(ai):
            if "key" in k.lower() and ai[k]:
                print(f"  {k} = <encrypted len={len(str(ai[k]))}>")
            else:
                print(f"  {k} = {ai[k]}")
        put_payload = {k: v for k, v in ai.items()}
        st2, _ = http("PUT", "/admin/api/ai", put_payload, ai_headers)
        print(f"PUT /admin/api/ai (same values) http={st2}")
        st3, ai2 = http("GET", "/admin/api/ai", headers=ai_headers)
        same = ai2 == ai
        print(f"GET after PUT http={st3} unchanged={same}")

    failures = []

    def check(label, cond, extra=""):
        if cond:
            print(f"  [PASS] {label}")
        else:
            print(f"  [FAIL] {label} {extra}")
            failures.append(label)

    print("\n== 2. 业务回归（临时命名空间，自动清理）==")
    n = {"i": 0}

    def nid():
        n["i"] += 1
        return f"ai{n['i']}"

    r1 = show("记账", inbound(nid(), "今天微信吃饭花了25"))
    check("记账成功", r1.get("status") == "success")
    r2 = show("防重(同ID)", inbound("ai1", "今天微信吃饭花了25"))
    check("防重生效", r2.get("status") == "duplicate", str(r2.get("status")))
    show("缺账户", inbound(nid(), "午饭30"))
    show("补答账户", inbound(nid(), "支付宝"))
    show("补答时间", inbound(nid(), "昨天"))
    show("收入", inbound(nid(), "今天银行卡收入5000"))
    show("转账+手续费", inbound(nid(), "今天微信转银行卡500元，手续费0.3元"))
    show("余额查询", inbound(nid(), "微信还有多少钱"))
    show("总资产", inbound(nid(), "总资产"))
    q1 = show("2025年账单", inbound(nid(), "2025年账单"))
    check("2025年账单非记账", "使用哪个账户" not in (q1.get("reply_content") or ""))
    q2 = show("2025年7月支出", inbound(nid(), "2025年7月份总共花了多少钱"))
    check("月份查询非记账", "使用哪个账户" not in (q2.get("reply_content") or ""))
    show("删除", inbound(nid(), "删除刚才那笔"))
    show("确认删除", inbound(nid(), "是"))
    show("修改", inbound(nid(), "刚才那笔不是30，是35"))
    show("确认修改", inbound(nid(), "是"))
    show("平账", inbound(nid(), "微信平账到80"))
    show("确认平账", inbound(nid(), "是"))
    ai_manual = show("手动AI(未启用)", inbound(nid(), "用AI：今天微信吃饭花了25"))
    check("AI未启用提示", "未启用" in (ai_manual.get("reply_content") or ""))
    show("今日统计", inbound(nid(), "今天"))
    show("防抖确认", inbound(nid(), "今天微信吃饭花了25"))

    print("\n== 3. 清理测试数据 ==")
    if os.path.exists(DB):
        con = sqlite3.connect(DB)
        tables = [
            "expenses", "daily_status", "pending_actions", "user_chats",
            "imports", "accounts", "account_templates", "raw_messages",
            "import_staging",
        ]
        for t in tables:
            try:
                cur = con.execute(f"DELETE FROM {t} WHERE namespace = ?", (NS,))
                if cur.rowcount:
                    print(f"  {t}: {cur.rowcount} 行")
            except sqlite3.Error as e:
                print(f"  {t}: 清理跳过 ({e})")
        con.commit()
        con.close()
        print("清理完成")
    else:
        print(f"未找到 DB: {DB}，跳过清理")

    print(f"\n== 结果：失败 {len(failures)} 项 ==")
    if failures:
        print("  " + ", ".join(failures))
        raise SystemExit(1)
    print("  全部通过")


if __name__ == "__main__":
    main()
